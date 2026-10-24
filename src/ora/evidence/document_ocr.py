"""Bounded local rendering and OCR of one exact saved public-document page.

The image and OCR text are derived observations. Neither is an inspected
passage or a source-use grant; callers must recheck current use before model
delivery or scientific interpretation.
"""

from __future__ import annotations

import csv
import hashlib
import io
import re
import shutil
import statistics
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Any

from ora.coordination.board import ref
from ora.evidence.hit_use import hit_allows_reinspection
from ora.state.store import canonical

from .intake import (
    _EXPLICIT_USE_PROHIBITION,
    MAX_COPY_BYTES,
    _public_fetch_use_policy,
    detected_content_type,
    extract_blocks,
)
from .library import EvidenceError
from .ocr_images import (
    OCR_MAX_IMAGE_BYTES,
    OCR_RENDER_VERSION,
    normalized_region,
    prepare_ocr_raster,
    traineddata_digest,
)
from .shared_local_extraction import shared_ocr_result

PAGE_IMAGE_KIND = "public_document_page_image"
PAGE_OCR_KIND = "public_document_page_ocr"
MAX_PDF_PAGES = 200
MAX_PAGE_DIMENSION = 2400
MAX_PAGE_PIXELS = 5_000_000
MAX_PAGE_IMAGE_BYTES = 12 * 1024 * 1024
MAX_OCR_TEXT_CHARS = 20_000
MAX_OCR_TSV_BYTES = 2 * 1024 * 1024
MAX_COMMAND_OUTPUT_BYTES = 2 * 1024 * 1024
MAX_COMMAND_STDERR_BYTES = 64 * 1024
RENDER_SCALE_TO = 1600
RENDER_VERSION = "poppler-selected-page-png-v1"
OCR_VERSION = "tesseract-eng-psm3-v1"
SELECTIVE_OCR_VERSION = "tesseract-eng-layout-v2"


def _identity(prefix: str, *values: Any) -> str:
    return prefix + hashlib.sha256(canonical(values).encode()).hexdigest()[:32]


def _run(argv: list[str], *, content: bytes | None = None,
         timeout: int = 30, output_limit: int = MAX_COMMAND_OUTPUT_BYTES,
         file_limit: int | None = None) -> subprocess.CompletedProcess:
    """Capture bounded child output and cap generated file growth in the child."""
    ceiling = max(output_limit, MAX_COMMAND_STDERR_BYTES, file_limit or 0)
    executable = shutil.which(argv[0])
    if executable is None:
        raise EvidenceError(f"Local document reader unavailable: {argv[0]} is not installed")
    # A fresh single-threaded Python child sets the file limit, then execs the
    # fixed local program. Avoid preexec_fn in the multithreaded mission host.
    limit_exec = (
        "import os,resource,sys;"
        "size=int(sys.argv[1]);"
        "resource.setrlimit(resource.RLIMIT_FSIZE,(size,size));"
        "os.execv(sys.argv[2],sys.argv[2:])"
    )
    command = [sys.executable, "-c", limit_exec, str(ceiling), executable,
               *argv[1:]]
    with tempfile.TemporaryDirectory(prefix="ora-reader-") as directory:
        output_path = Path(directory) / "stdout"
        error_path = Path(directory) / "stderr"
        try:
            with output_path.open("wb") as output, error_path.open("wb") as error:
                finished = subprocess.run(
                    command, input=content, stdout=output, stderr=error,
                    timeout=timeout, check=False,
                )
        except subprocess.TimeoutExpired as exc:
            raise EvidenceError(f"Local document reader timed out: {argv[0]}") from exc
        if (output_path.stat().st_size > output_limit
                or error_path.stat().st_size > MAX_COMMAND_STDERR_BYTES):
            raise EvidenceError(f"Local document reader output exceeds the bound: {argv[0]}")
        return subprocess.CompletedProcess(
            argv, finished.returncode, output_path.read_bytes(), error_path.read_bytes(),
        )


def _image_dimensions(content: bytes, mime_type: str) -> tuple[int, int]:
    if mime_type == "image/png":
        if len(content) < 24 or not content.startswith(b"\x89PNG\r\n\x1a\n"):
            raise EvidenceError("PNG page bytes are malformed")
        return int.from_bytes(content[16:20], "big"), int.from_bytes(content[20:24], "big")
    if mime_type == "image/jpeg":
        if not content.startswith(b"\xff\xd8\xff"):
            raise EvidenceError("JPEG page bytes are malformed")
        position = 2
        while position + 4 <= len(content):
            if content[position] != 0xFF:
                position += 1
                continue
            marker = content[position + 1]
            position += 2
            if marker in {0xD8, 0xD9} or 0xD0 <= marker <= 0xD7:
                continue
            if position + 2 > len(content):
                break
            length = int.from_bytes(content[position:position + 2], "big")
            if length < 2 or position + length > len(content):
                break
            if marker in {0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                          0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF}:
                if length < 7:
                    break
                height = int.from_bytes(content[position + 3:position + 5], "big")
                width = int.from_bytes(content[position + 5:position + 7], "big")
                return width, height
            position += length
        raise EvidenceError("JPEG page dimensions are unavailable")
    raise EvidenceError("Selected image format is unsupported")


def _bounded_image(content: bytes, mime_type: str) -> None:
    if not content or len(content) > MAX_PAGE_IMAGE_BYTES:
        raise EvidenceError("Selected page image exceeds the byte limit")
    width, height = _image_dimensions(content, mime_type)
    if (width < 1 or height < 1 or width > MAX_PAGE_DIMENSION
            or height > MAX_PAGE_DIMENSION or width * height > MAX_PAGE_PIXELS):
        raise EvidenceError("Selected page image exceeds the pixel limit")


def current_document_page_use(
    store, document_ref: dict, *, mission_id: str, run_id: str,
    actor_id: str, task_id: str,
) -> dict:
    """Revalidate exact bytes, origin and selected/current model-use decision."""
    backing_store = getattr(store, "store", store)
    if (not isinstance(document_ref, dict)
            or set(document_ref) != {"kind", "id", "version"}
            or document_ref.get("kind") != "public_document_fetch"):
        raise EvidenceError("Page reading needs an exact public-document reference")
    document = store.get(**document_ref)
    if document is None or ref(document) != document_ref:
        raise EvidenceError("Exact public document is unavailable")
    body = document["body"]
    if any(body.get(key) != value for key, value in (
        ("mission_id", mission_id), ("run_id", run_id),
        ("actor_id", actor_id), ("task_id", task_id),
    )):
        raise EvidenceError("Public document belongs to another peer task")
    if body.get("origin_kind") == "parent_source_asset":
        from .source_assets import current_asset_use

        asset, _parent = current_asset_use(store, body["parent_asset_ref"], scope={
            "mission_id": mission_id, "run_id": run_id,
            "actor_id": actor_id, "task_id": task_id,
        })
        if (asset["body"].get("image_url") != body.get("requested_url")
                or asset["body"].get("parent_copy_ref") != body.get("parent_copy_ref")
                or asset["body"].get("parent_sha256") != body.get("parent_sha256")
                or asset["body"].get("caption_sha256") != body.get("caption_sha256")):
            raise EvidenceError("Figure image differs from its parent source")
    else:
        hit = store.get(**body["hit_ref"])
        response = store.get(**body["response_ref"])
        if (hit is None or response is None or hit != store.get("retrieval_hit", hit["id"])
                or hit["body"].get("source", {}).get("original_url") != body.get("requested_url")
                or hit["body"].get("source", {}).get("rights_status") in {"restricted", "not_retained"}
                or body.get("hit_rights_status") in {"restricted", "not_retained"}
                or not hit_allows_reinspection(store, hit)
                or not any(link["relation"] == "derived_from"
                           and link["target"] == body["response_ref"]
                           for link in backing_store.links(hit))):
            raise EvidenceError("Public document lost its permitted original lead")
    content = backing_store.read_blob(body["blob"]["sha256"])
    source_hash = hashlib.sha256(content).hexdigest()
    if (not content or len(content) > MAX_COPY_BYTES
            or source_hash != body.get("content_sha256")
            or len(content) != body.get("byte_length")
            or detected_content_type(content, body["content_type"])
            != body.get("detected_content_type")):
        raise EvidenceError("Public document bytes disagree with the exact fetch")
    mime_type = body["detected_content_type"]
    if mime_type not in {"application/pdf", "image/png", "image/jpeg"}:
        raise EvidenceError("Local page rendering supports PDF, PNG and JPEG only")
    receipt = {
        "status": body["status"], "access_basis": body.get("access_basis"),
        "authenticated": body.get("authenticated"), "content": content,
        "content_sha256": source_hash, "byte_length": len(content),
        "content_type": body["content_type"], "retrieved_at": body["checked_at"],
        "requested_url": body["requested_url"], "final_url": body["final_url"],
        "redirect_chain": body["redirect_chain"],
    }
    policy = _public_fetch_use_policy(
        receipt, content=content, source_url=body["final_url"],
        content_type=body["content_type"], retrieved_at=body["checked_at"],
        classification="empty", identity_basis=(
            f"verified_source:public_document:{document['id']}:{document['version']}:sha256={source_hash}"
        ), expected_doi=None, blocks=[], caller_rights_status="unknown",
    )
    if not policy["public_fetch_verified"]:
        raise EvidenceError("Public fetch origin no longer verifies")
    copy = store.resolve(
        "source_copy.public_document", f"{document_ref['id']}:{document_ref['version']}"
    )
    model_status, use_ref = "not_granted", None
    if copy is not None:
        if (copy["body"].get("document_ref") != document_ref
                or copy["body"].get("content_sha256") != source_hash):
            raise EvidenceError("Public-document copy differs from its origin")
        from ora.mission.context import WorkingContext

        status = WorkingContext(store)._source_status(copy)
        if (status["selected_model_input_status"] != "permitted"
                or status["current_model_input_status"] != "permitted"):
            raise EvidenceError("Current or selected public-document source use is not permitted")
        model_status = "permitted"
        use_ref = status["selected_source_use_ref"]
    if mime_type == "application/pdf":
        # Reuse the exact intake inspection for this source version. Permission is
        # still resolved anew above; sparse pre-intake documents are checked here.
        quality = None
        if use_ref is not None:
            use = store.get(**use_ref)
            quality_ref = use["body"].get("quality_ref") if use else None
            quality = store.get(**quality_ref) if isinstance(quality_ref, dict) else None
        if (quality is not None
                and quality["body"].get("document_ref") == document_ref
                and quality["body"].get("source_sha256") == source_hash
                and quality["body"].get("page_restriction") is False
                and quality["body"].get("classification") in {"web_document", "image_document"}):
            prohibited = False
        else:
            try:
                blocks, _ = extract_blocks(content, mime_type)
            except EvidenceError as exc:
                raise EvidenceError("PDF source cannot be checked for use restrictions") from exc
            prohibited = any(_EXPLICIT_USE_PROHIBITION.search(block.text) for block in blocks)
        if prohibited:
            raise EvidenceError("PDF source prohibits model input")
    return {
        "document_ref": document_ref, "source_sha256": source_hash,
        "mime_type": mime_type, "model_input_status": model_status,
        "permission_basis": "current_public_document_copy_source_use" if use_ref
        else "verified_public_fetch_without_model_input_grant",
        "source_use_ref": use_ref, "source_copy_ref": ref(copy) if copy else None,
        "content": content,
    }


def render_public_document_page(
    store, document_ref: dict, *, mission_id: str, run_id: str,
    actor_id: str, task_id: str, page_number: int, operation_id: str,
) -> dict:
    """Render one page locally and save its exact derived image."""
    if type(page_number) is not int or not 1 <= page_number <= MAX_PDF_PAGES:
        raise EvidenceError("Selected page number is outside the local bound")
    if not isinstance(operation_id, str) or not operation_id:
        raise EvidenceError("Page rendering needs an operation ID")
    checked = current_document_page_use(
        store, document_ref, mission_id=mission_id, run_id=run_id,
        actor_id=actor_id, task_id=task_id,
    )
    content = checked.pop("content")
    mime_type = checked["mime_type"]
    image_ref_id = _identity("public-page-image-", document_ref, page_number,
                             RENDER_VERSION)
    existing = store.get(PAGE_IMAGE_KIND, image_ref_id)
    if existing is not None:
        saved = existing["body"]
        if (saved.get("document_ref") != document_ref
                or saved.get("source_sha256") != checked["source_sha256"]
                or saved.get("page_number") != page_number
                or any(saved.get(key) != value for key, value in (
                    ("mission_id", mission_id), ("run_id", run_id),
                    ("actor_id", actor_id), ("task_id", task_id),
                ))):
            raise EvidenceError("Saved page image differs from this exact source")
        image = store.read_blob(saved["blob"]["sha256"])
        if (hashlib.sha256(image).hexdigest() != saved["image_sha256"]
                or len(image) != saved["byte_length"]):
            raise EvidenceError("Saved page image bytes changed")
        _bounded_image(image, saved["mime_type"])
        return {**{key: checked[key] for key in (
            "document_ref", "source_sha256", "model_input_status", "permission_basis",
            "source_use_ref", "source_copy_ref",
        )}, "page_image_ref": ref(existing),
            "image_sha256": saved["image_sha256"], "page_number": page_number,
            "mime_type": saved["mime_type"], "request_operation_id": operation_id}
    if mime_type == "application/pdf":
        with tempfile.TemporaryDirectory(prefix="ora-page-") as directory:
            source = Path(directory) / "source.pdf"
            source.write_bytes(content)
            info = _run(["pdfinfo", str(source)], timeout=10)
            match = re.search(rb"(?m)^Pages:\s*(\d+)\s*$", info.stdout)
            if info.returncode != 0 or match is None:
                raise EvidenceError("PDF page count is unavailable")
            pages = int(match.group(1))
            if pages > MAX_PDF_PAGES or page_number > pages:
                raise EvidenceError("Selected PDF page is outside the saved document")
            prefix = Path(directory) / "selected"
            rendered = _run([
                "pdftoppm", "-f", str(page_number), "-l", str(page_number),
                "-singlefile", "-scale-to", str(RENDER_SCALE_TO),
                "-png", str(source), str(prefix),
            ], timeout=30, file_limit=MAX_PAGE_IMAGE_BYTES)
            if rendered.returncode != 0:
                raise EvidenceError("Selected PDF page could not be rendered")
            image_path = Path(directory) / "selected.png"
            if not image_path.exists() or image_path.stat().st_size > MAX_PAGE_IMAGE_BYTES:
                raise EvidenceError("Selected PDF page exceeds the image byte limit")
            image = image_path.read_bytes()
        image_mime = "image/png"
    else:
        if page_number != 1:
            raise EvidenceError("A saved image has only page one")
        image, image_mime = content, mime_type
    _bounded_image(image, image_mime)
    image_hash = hashlib.sha256(image).hexdigest()
    blob = store.put_blob(image)
    image_body = {
        "schema": "ora.evidence.public_document_page_image.v1",
        "document_ref": document_ref, "mission_id": mission_id, "run_id": run_id,
        "actor_id": actor_id, "task_id": task_id,
        "source_sha256": checked["source_sha256"], "page_number": page_number,
        "image_sha256": image_hash, "mime_type": image_mime,
        "byte_length": len(image), "blob": blob,
        "renderer": RENDER_VERSION if mime_type == "application/pdf" else "original-image-v1",
        "model_input_status": checked["model_input_status"],
        "permission_basis": checked["permission_basis"],
    }
    def save(tx):
        if tx.get(**document_ref) is None:
            raise EvidenceError("Public document changed before page image commit")
        saved = tx.put(PAGE_IMAGE_KIND, image_ref_id, image_body)
        tx.link(saved, tx.get(**document_ref), "derived_from")
        return ref(saved)
    page_image_ref = store.transact(
        "render-page:" + image_ref_id, image_body, save, mission_id=mission_id,
    )
    return {**{key: checked[key] for key in (
        "document_ref", "source_sha256", "model_input_status", "permission_basis",
        "source_use_ref", "source_copy_ref",
    )}, "page_image_ref": page_image_ref, "image_sha256": image_hash,
        "page_number": page_number, "mime_type": image_mime,
        "request_operation_id": operation_id}


def render_public_document_ocr_page(
    store, document_ref: dict, *, mission_id: str, run_id: str,
    actor_id: str, task_id: str, page_number: int, operation_id: str,
    region: dict | None = None,
) -> dict:
    """Prepare a bounded, optionally cropped OCR raster without changing vision renders."""
    if type(page_number) is not int or not 1 <= page_number <= MAX_PDF_PAGES:
        raise EvidenceError("Selected page number is outside the local bound")
    if not isinstance(operation_id, str) or not operation_id:
        raise EvidenceError("Page rendering needs an operation ID")
    region = normalized_region(region)
    checked = current_document_page_use(
        store, document_ref, mission_id=mission_id, run_id=run_id,
        actor_id=actor_id, task_id=task_id,
    )
    content = checked.pop("content")
    mime_type = checked["mime_type"]
    image_ref_id = _identity(
        "public-page-image-", document_ref, page_number, region, OCR_RENDER_VERSION,
    )
    existing = store.get(PAGE_IMAGE_KIND, image_ref_id)
    if existing is not None:
        saved = existing["body"]
        if (saved.get("document_ref") != document_ref
                or saved.get("source_sha256") != checked["source_sha256"]
                or saved.get("page_number") != page_number
                or saved.get("renderer") != OCR_RENDER_VERSION
                or saved.get("ocr_preparation", {}).get("region") != region
                or any(saved.get(key) != value for key, value in (
                    ("mission_id", mission_id), ("run_id", run_id),
                    ("actor_id", actor_id), ("task_id", task_id),
                ))):
            raise EvidenceError("Saved OCR page differs from this exact source")
        image = store.read_blob(saved["blob"]["sha256"])
        if (hashlib.sha256(image).hexdigest() != saved["image_sha256"]
                or len(image) != saved["byte_length"]):
            raise EvidenceError("Saved OCR page image bytes changed")
        return {**{key: checked[key] for key in (
            "document_ref", "source_sha256", "model_input_status", "permission_basis",
            "source_use_ref", "source_copy_ref",
        )}, "page_image_ref": ref(existing), "image_sha256": saved["image_sha256"],
            "page_number": page_number, "mime_type": saved["mime_type"],
            "ocr_preparation": saved["ocr_preparation"],
            "request_operation_id": operation_id}
    if mime_type == "application/pdf":
        with tempfile.TemporaryDirectory(prefix="ora-ocr-page-") as directory:
            source = Path(directory) / "source.pdf"
            source.write_bytes(content)
            info = _run(["pdfinfo", str(source)], timeout=10)
            match = re.search(rb"(?m)^Pages:\s*(\d+)\s*$", info.stdout)
            if info.returncode != 0 or match is None:
                raise EvidenceError("PDF page count is unavailable")
            pages = int(match.group(1))
            if pages > MAX_PDF_PAGES or page_number > pages:
                raise EvidenceError("Selected PDF page is outside the saved document")
            prefix = Path(directory) / "selected"
            rendered = _run([
                "pdftoppm", "-f", str(page_number), "-l", str(page_number),
                "-singlefile", "-scale-to", "3200", "-png", str(source), str(prefix),
            ], timeout=30, file_limit=OCR_MAX_IMAGE_BYTES)
            if rendered.returncode != 0:
                raise EvidenceError("Selected PDF page could not be rendered for OCR")
            image_path = prefix.with_suffix(".png")
            if not image_path.exists() or image_path.stat().st_size > OCR_MAX_IMAGE_BYTES:
                raise EvidenceError("Selected OCR page exceeds the image byte bound")
            raster = image_path.read_bytes()
        image, preparation = prepare_ocr_raster(
            raster, mime_type="image/png", region=region, enlarge_small=False,
        )
        preparation["pdf_scale_to"] = 3200
    else:
        if page_number != 1:
            raise EvidenceError("A saved image has only page one")
        image, preparation = prepare_ocr_raster(
            content, mime_type=mime_type, region=region, enlarge_small=True,
        )
        preparation["source_image_sha256"] = checked["source_sha256"]
    image_hash = hashlib.sha256(image).hexdigest()
    blob = store.put_blob(image)
    image_body = {
        "schema": "ora.evidence.public_document_page_image.v1",
        "document_ref": document_ref, "mission_id": mission_id, "run_id": run_id,
        "actor_id": actor_id, "task_id": task_id,
        "source_sha256": checked["source_sha256"], "page_number": page_number,
        "image_sha256": image_hash, "mime_type": "image/png",
        "byte_length": len(image), "blob": blob, "renderer": OCR_RENDER_VERSION,
        "ocr_preparation": preparation,
        "model_input_status": checked["model_input_status"],
        "permission_basis": checked["permission_basis"],
    }
    def save(tx):
        if tx.get(**document_ref) is None:
            raise EvidenceError("Public document changed before OCR page commit")
        saved = tx.put(PAGE_IMAGE_KIND, image_ref_id, image_body)
        tx.link(saved, tx.get(**document_ref), "derived_from")
        return ref(saved)
    page_image_ref = store.transact(
        "render-page:" + image_ref_id, image_body, save, mission_id=mission_id,
    )
    return {**{key: checked[key] for key in (
        "document_ref", "source_sha256", "model_input_status", "permission_basis",
        "source_use_ref", "source_copy_ref",
    )}, "page_image_ref": page_image_ref, "image_sha256": image_hash,
        "page_number": page_number, "mime_type": "image/png",
        "ocr_preparation": preparation, "request_operation_id": operation_id}


def _selective_engine_identity() -> dict:
    """Bind fresh OCR cache entries to the installed engine and language bytes."""
    version = _run(["tesseract", "--version"], timeout=5)
    languages = _run(["tesseract", "--list-langs"], timeout=5)
    if version.returncode != 0 or languages.returncode != 0:
        raise EvidenceError("Local OCR engine identity is unavailable")
    version_line = (version.stdout + version.stderr).decode("utf-8", errors="replace").splitlines()
    listed = languages.stdout.decode("utf-8", errors="replace")
    match = re.search(r'List of available languages in "([^"]+)"', listed)
    if not version_line or match is None or "eng" not in listed.splitlines():
        raise EvidenceError("English OCR engine identity is unavailable")
    digest = traineddata_digest(Path(match.group(1)) / "eng.traineddata")
    executable = shutil.which("tesseract")
    if executable is None:
        raise EvidenceError("Local OCR executable is unavailable")
    binary = Path(executable)
    if binary.stat().st_size > 64 * 1024 * 1024:
        raise EvidenceError("Local OCR executable exceeds the identity bound")
    return {"version": version_line[0], "language": "eng",
            "traineddata_sha256": digest,
            "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest()}


def _selective_ocr(image: bytes, psm: int) -> tuple[str, bytes, dict]:
    """Generate text and word boxes from one bounded Tesseract process."""
    with tempfile.TemporaryDirectory(prefix="ora-ocr-output-") as directory:
        prefix = Path(directory) / "selected"
        completed = _run(
            ["tesseract", "stdin", str(prefix), "-l", "eng", "--psm", str(psm),
             "txt", "tsv"],
            content=image, timeout=30, output_limit=64 * 1024,
            file_limit=MAX_OCR_TSV_BYTES,
        )
        if completed.returncode != 0:
            raise EvidenceError("Local OCR rejected the selected page image")
        text_path = prefix.with_suffix(".txt")
        tsv_path = prefix.with_suffix(".tsv")
        if not text_path.is_file() or not tsv_path.is_file():
            raise EvidenceError("Local OCR did not return text and word geometry")
        if text_path.stat().st_size > MAX_OCR_TEXT_CHARS * 4:
            raise EvidenceError("Local OCR text exceeds the selected-page limit")
        if tsv_path.stat().st_size > MAX_OCR_TSV_BYTES:
            raise EvidenceError("Local OCR word geometry exceeds the selected-page limit")
        text_bytes, tsv_bytes = text_path.read_bytes(), tsv_path.read_bytes()
    try:
        text = text_bytes.decode("utf-8").strip()
        rows = csv.DictReader(io.StringIO(tsv_bytes.decode("utf-8")), delimiter="\t")
        confidences = []
        for row in rows:
            if row.get("level") == "5" and (row.get("text") or "").strip():
                confidence = float(row["conf"])
                if 0 <= confidence <= 100:
                    confidences.append(confidence)
    except (UnicodeDecodeError, ValueError, KeyError, csv.Error) as exc:
        raise EvidenceError("Local OCR word geometry is invalid") from exc
    if len(text) > MAX_OCR_TEXT_CHARS:
        raise EvidenceError("Local OCR text exceeds the selected-page limit")
    quality = {
        "word_boxes": len(confidences),
        "median_engine_confidence": round(statistics.median(confidences), 2)
        if confidences else None,
        "confidence_is_accuracy": False,
        "quality_scope": "selected OCR image; engine estimate, not verified text",
    }
    return text, tsv_bytes, quality


def ocr_public_document_page(
    store, page_image_ref: dict, *, mission_id: str, run_id: str,
    actor_id: str, task_id: str, operation_id: str, layout: str = "auto",
    shared_enabled: bool = False,
) -> dict:
    """OCR one saved page locally; output is unverified derived text."""
    image_record = store.get(**page_image_ref) if (
        isinstance(page_image_ref, dict) and set(page_image_ref) == {"kind", "id", "version"}
        and page_image_ref.get("kind") == PAGE_IMAGE_KIND
    ) else None
    if image_record is None or any(image_record["body"].get(key) != value for key, value in (
        ("mission_id", mission_id), ("run_id", run_id),
        ("actor_id", actor_id), ("task_id", task_id),
    )):
        raise EvidenceError("Saved page image belongs to another peer task")
    if not isinstance(operation_id, str) or not operation_id:
        raise EvidenceError("Page OCR needs an operation ID")
    if layout not in {"auto", "block", "sparse"}:
        raise EvidenceError("OCR layout must be auto, block or sparse")
    body = image_record["body"]
    checked = current_document_page_use(
        store, body["document_ref"], mission_id=mission_id, run_id=run_id,
        actor_id=actor_id, task_id=task_id,
    )
    checked.pop("content")
    if checked["source_sha256"] != body.get("source_sha256"):
        raise EvidenceError("Saved OCR source bytes changed")
    if checked["source_sha256"] != body["source_sha256"]:
        raise EvidenceError("Page image source hash changed")
    selective_render = body.get("renderer") in {
        "poppler-ocr-page-3200-region-v1", OCR_RENDER_VERSION,
    }
    selective = selective_render or layout != "auto"
    psm = 6 if layout == "block" or (
        layout == "auto" and body.get("ocr_preparation", {}).get("region") is not None
    ) else 11 if layout == "sparse" else 3
    engine_identity = _selective_engine_identity() if selective or shared_enabled else None
    record_id = _identity(
        "public-page-ocr-", page_image_ref,
        SELECTIVE_OCR_VERSION if selective else OCR_VERSION,
        layout, psm, engine_identity,
    ) if selective or shared_enabled else _identity("public-page-ocr-", page_image_ref, OCR_VERSION)
    existing = store.get(PAGE_OCR_KIND, record_id)
    if existing is not None:
        return {**read_public_document_ocr(
            store, ref(existing), mission_id=mission_id, run_id=run_id,
            actor_id=actor_id, task_id=task_id,
        ), "request_operation_id": operation_id}
    image = store.read_blob(body["blob"]["sha256"])
    if (hashlib.sha256(image).hexdigest() != body["image_sha256"]
            or len(image) != body["byte_length"]):
        raise EvidenceError("Saved page image bytes changed")
    if selective_render:
        preparation = body.get("ocr_preparation", {})
        if (preparation.get("image_sha256") != body["image_sha256"]
                or body["mime_type"] != "image/png"):
            raise EvidenceError("Prepared OCR image provenance changed")
        from io import BytesIO

        from PIL import Image

        with Image.open(BytesIO(image)) as opened:
            dimensions = list(opened.size)
        if dimensions != preparation.get("prepared_dimensions"):
            raise EvidenceError("Prepared OCR image dimensions changed")
    else:
        _bounded_image(image, body["mime_type"])
    def compute():
        if selective:
            return _selective_ocr(image, psm)
        completed = _run(
            ["tesseract", "stdin", "stdout", "-l", "eng", "--psm", "3"],
            content=image, timeout=30, output_limit=MAX_OCR_TEXT_CHARS * 4,
        )
        if completed.returncode != 0:
            raise EvidenceError("Local OCR rejected the selected page image")
        try:
            recognized = completed.stdout.decode("utf-8").strip()
        except UnicodeDecodeError as exc:
            raise EvidenceError("Local OCR returned non-UTF-8 text") from exc
        if len(recognized) > MAX_OCR_TEXT_CHARS:
            raise EvidenceError("Local OCR text exceeds the selected-page limit")
        return recognized, None, None

    shared_ref = None
    if shared_enabled:
        identity = {
            "mission_id": mission_id,
            "image_sha256": body["image_sha256"],
            "image_byte_length": body["byte_length"],
            "mime_type": body["mime_type"],
            "engine_identity": engine_identity,
            "engine": SELECTIVE_OCR_VERSION if selective else OCR_VERSION,
            "layout": layout,
            "psm": psm,
            "preparation": body.get("ocr_preparation"),
        }
        text, tsv_bytes, quality, shared_ref = shared_ocr_result(
            store, identity=identity, compute=compute,
        )
    else:
        text, tsv_bytes, quality = compute()
    if len(text) > MAX_OCR_TEXT_CHARS or (
        tsv_bytes is not None and len(tsv_bytes) > MAX_OCR_TSV_BYTES
    ):
        raise EvidenceError("Shared OCR output exceeds the selected-page limit")
    prohibition_detected = bool(_EXPLICIT_USE_PROHIBITION.search(text))
    delivery_status = (
        "restricted" if prohibition_detected else checked["model_input_status"]
    )
    permission_basis = (
        "selected_page_ocr_explicit_use_prohibition" if prohibition_detected
        else checked["permission_basis"]
    )
    digest = hashlib.sha256(text.encode()).hexdigest()
    tsv_digest = hashlib.sha256(tsv_bytes).hexdigest() if tsv_bytes is not None else None
    tsv_blob = store.put_blob(tsv_bytes) if tsv_bytes is not None else None
    ocr_body = {
        "schema": "ora.evidence.public_document_page_ocr.v2" if selective
        else "ora.evidence.public_document_page_ocr.v1",
        "page_image_ref": page_image_ref, "document_ref": body["document_ref"],
        "mission_id": mission_id, "run_id": run_id, "actor_id": actor_id,
        "task_id": task_id, "source_sha256": body["source_sha256"],
        "page_number": body["page_number"], "image_sha256": body["image_sha256"],
        "text": text, "text_sha256": digest,
        "locator": f"page={body['page_number']}; local-ocr-text",
        "status": "ocr_restricted" if prohibition_detected else "ocr_unverified",
        "engine": SELECTIVE_OCR_VERSION if selective else OCR_VERSION,
        "explicit_use_prohibition_detected": prohibition_detected,
        "model_input_status": delivery_status,
        "permission_basis": permission_basis,
    }
    if shared_ref is not None:
        ocr_body["shared_extraction_ref"] = shared_ref
    if selective:
        ocr_body.update({
            "tsv_sha256": tsv_digest, "tsv_blob": tsv_blob,
            "tsv_byte_length": len(tsv_bytes), "ocr_quality": quality,
            "layout": layout, "psm": psm,
            "engine_identity": engine_identity,
            "ocr_preparation": body.get("ocr_preparation"),
            "restriction_screen_scope": "selected_region" if (
                body.get("ocr_preparation", {}) or {}
            ).get("region") is not None else "selected_page",
        })
    def save(tx):
        exact_image = tx.get(**page_image_ref)
        if exact_image is None or exact_image["body"]["image_sha256"] != body["image_sha256"]:
            raise EvidenceError("Page image changed before OCR commit")
        saved = tx.put(PAGE_OCR_KIND, record_id, ocr_body)
        tx.link(saved, exact_image, "derived_from")
        if shared_ref is not None:
            shared = tx.get(**shared_ref)
            if shared is None or shared["body"].get("phase") != "completed":
                raise EvidenceError("Shared OCR extraction changed before peer commit")
            tx.link(saved, shared, "derived_from")
        return ref(saved)
    ocr_ref = store.transact("page-ocr:" + record_id, ocr_body, save,
                             mission_id=mission_id)
    return {
        **read_public_document_ocr(
            store, ocr_ref, mission_id=mission_id, run_id=run_id,
            actor_id=actor_id, task_id=task_id,
        ),
        "request_operation_id": operation_id,
    }


def read_public_document_ocr(
    store, ocr_ref: dict, *, mission_id: str, run_id: str,
    actor_id: str, task_id: str,
) -> dict:
    """Deliver saved OCR text only under current exact source-use permission."""
    record = store.get(**ocr_ref) if (
        isinstance(ocr_ref, dict) and set(ocr_ref) == {"kind", "id", "version"}
        and ocr_ref.get("kind") == PAGE_OCR_KIND
    ) else None
    if record is None or any(record["body"].get(key) != value for key, value in (
        ("mission_id", mission_id), ("run_id", run_id),
        ("actor_id", actor_id), ("task_id", task_id),
    )):
        raise EvidenceError("Saved OCR belongs to another peer task")
    body = record["body"]
    image = store.get(**body["page_image_ref"])
    if (image is None or image["kind"] != PAGE_IMAGE_KIND
            or image["body"].get("document_ref") != body["document_ref"]
            or image["body"].get("image_sha256") != body["image_sha256"]
            or body.get("text_sha256") != hashlib.sha256(body.get("text", "").encode()).hexdigest()):
        raise EvidenceError("Saved OCR provenance differs from its page image")
    if body.get("tsv_sha256") is not None:
        blob = body.get("tsv_blob")
        if not isinstance(blob, dict) or "sha256" not in blob:
            raise EvidenceError("Saved OCR word geometry is unavailable")
        backing_store = getattr(store, "store", store)
        tsv = backing_store.read_blob(blob["sha256"])
        if (hashlib.sha256(tsv).hexdigest() != body["tsv_sha256"]
                or len(tsv) != body.get("tsv_byte_length")):
            raise EvidenceError("Saved OCR word geometry changed")
    checked = current_document_page_use(
        store, body["document_ref"], mission_id=mission_id, run_id=run_id,
        actor_id=actor_id, task_id=task_id,
    )
    checked.pop("content")
    if checked["source_sha256"] != body["source_sha256"]:
        raise EvidenceError("Saved OCR source bytes changed")
    prohibited = bool(
        body.get("explicit_use_prohibition_detected")
        or _EXPLICIT_USE_PROHIBITION.search(body["text"])
    )
    delivery_status = "restricted" if prohibited else checked["model_input_status"]
    result = {
        "ocr_ref": ocr_ref, "document_ref": body["document_ref"],
        "page_image_ref": body["page_image_ref"], "page_number": body["page_number"],
        "source_sha256": body["source_sha256"], "image_sha256": body["image_sha256"],
        "text_sha256": body["text_sha256"],
        "status": "ocr_restricted" if prohibited else "ocr_unverified",
        "model_input_status": delivery_status,
        "permission_basis": "selected_page_ocr_explicit_use_prohibition" if prohibited
        else checked["permission_basis"],
    }
    if body.get("schema") == "ora.evidence.public_document_page_ocr.v2":
        result.update({
            "engine": body["engine"], "layout": body["layout"], "psm": body["psm"],
            "engine_identity": body["engine_identity"],
            "ocr_preparation": body["ocr_preparation"],
            "region": (body["ocr_preparation"] or {}).get("region"),
            "restriction_screen_scope": body["restriction_screen_scope"],
            "ocr_quality": body["ocr_quality"],
            "tsv_sha256": body["tsv_sha256"],
        })
    if delivery_status == "permitted":
        result["text"] = body["text"]
    return result
