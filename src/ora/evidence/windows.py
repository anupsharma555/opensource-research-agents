"""On-demand, exact reading windows over one retained inspected source copy.

The source copy remains immutable. A window adds only selected checked passages
and a small navigation record; scientific support and table validity remain for
the agents to judge from the original source context.
"""

from __future__ import annotations

import hashlib
import re
import uuid
from typing import Any

from .intake import IntakeService, _require_model_use_record, extract_copy_blocks
from .library import EvidenceError, EvidenceLibrary
from .navigation import source_navigation

_COPY_CLASSES = frozenset({"abstract", "passage", "partial", "full_text", "navigation_document", "web_document"})
_MAX_BLOCKS_PER_WINDOW = 20
_MAX_CHARS_PER_BLOCK = 16000
_MAX_CHARS_PER_WINDOW = 64000
_CHAR_SPAN = re.compile(r"; chars=(\d+):(\d+)$")
_PDF_PAGE = re.compile(r"^page=(\d+); (?:layout-text|reading-order-text); chars=\d+:\d+$")


def _ref(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record[key] for key in ("kind", "id", "version")}


def _exact_copy_ref(pointer: dict[str, Any]) -> None:
    if (not isinstance(pointer, dict) or set(pointer) != {"kind", "id", "version"}
            or pointer["kind"] != "source_copy" or not isinstance(pointer["id"], str)
            or not pointer["id"] or type(pointer["version"]) is not int
            or pointer["version"] < 1):
        raise EvidenceError("Exact source-copy reference is required")


def _window_slices(
    blocks: list[Any], *, block_offset: int, block_limit: int,
    char_offset: int, char_limit: int,
) -> tuple[list[dict[str, Any]], dict[str, int] | None, list[dict[str, Any]]]:
    total = len(blocks)
    selected: list[dict[str, Any]] = []
    omissions: list[dict[str, Any]] = []
    if block_offset >= total:
        omissions.append({"reason": "block_offset_past_inspected_end", "total_blocks": total})
        return selected, None, omissions
    first = blocks[block_offset]
    if char_offset > len(first.text):
        raise EvidenceError("Character offset exceeds the selected block")
    if char_offset == len(first.text):
        next_block = block_offset + 1
        omissions.append({"reason": "selected_block_exhausted", "block_offset": block_offset})
        if next_block < total:
            omissions.append({"reason": "remaining_blocks", "count": total - next_block})
            return selected, {"block_offset": next_block, "char_offset": 0}, omissions
        return selected, None, omissions
    if char_offset:
        omissions.append({"reason": "prior_characters_outside_window", "block_offset": block_offset,
                          "characters": char_offset})
    next_cursor = None
    for index in range(block_offset, min(total, block_offset + block_limit)):
        block = blocks[index]
        start = char_offset if index == block_offset else 0
        end = min(len(block.text), start + char_limit)
        if start >= end:
            continue
        selected.append({"block_index": index, "locator": f"{block.locator}; chars={start}:{end}",
                         "text": block.text[start:end], "start": start, "end": end})
        if end < len(block.text):
            next_cursor = {"block_offset": index, "char_offset": end}
            omissions.append({"reason": "remaining_characters_in_block", "block_offset": index,
                              "characters": len(block.text) - end})
            break
    if next_cursor is None:
        next_block = block_offset + block_limit
        if next_block < total:
            next_cursor = {"block_offset": next_block, "char_offset": 0}
            omissions.append({"reason": "remaining_blocks", "count": total - next_block})
    return selected, next_cursor, omissions


def reading_progress(
    passages: list[dict[str, Any]], next_cursor: dict[str, int] | None,
    *, mime_type: str, total_blocks: int,
) -> str:
    """Describe this extracted-text window without asserting whole-source reading."""
    positions = []
    pages = []
    for item in passages:
        locator = item.get("locator")
        block = item.get("block_index")
        span = _CHAR_SPAN.search(locator) if isinstance(locator, str) else None
        if type(block) is not int or block < 0 or span is None:
            raise EvidenceError("Reading progress needs exact selected locators")
        positions.append((block, int(span.group(1)), int(span.group(2))))
        if mime_type == "application/pdf":
            page = _PDF_PAGE.fullmatch(locator)
            if page is None:
                raise EvidenceError("PDF reading progress needs physical page locators")
            pages.append(int(page.group(1)))
    if not positions:
        return (
            "No text at this position; more retained text follows at next_request."
            if next_cursor is not None else
            "No text at this position; whole-source reading remains unverified."
        )
    first, last = positions[0], positions[-1]
    block_label = (
        f"block {first[0] + 1}/{total_blocks} chars {first[1]}:{first[2]}"
        if first[0] == last[0] else
        f"blocks {first[0] + 1}-{last[0] + 1}/{total_blocks} "
        f"chars {first[1]}..{last[2]} (see locators)"
    )
    if mime_type == "application/pdf":
        physical = list(dict.fromkeys(pages))
        label = (f"PDF page {physical[0]}" if len(physical) == 1 else
                 "PDF pages " + ",".join(map(str, physical)))
        label += f", {block_label}; total pages unknown."
    else:
        label = f"Extracted {block_label}; see section locators."
    if next_cursor is not None:
        return label + " More retained text: continue or search saved passages if useful."
    return label + " End of retained text only; whole source reading unverified."


def read_source_window(
    library: EvidenceLibrary, operation_id: str, copy_ref: dict[str, Any], *,
    block_offset: int = 0, block_limit: int = 3,
    char_offset: int = 0, char_limit: int = 4000,
) -> dict[str, Any]:
    """Save and return a bounded exact reading window at any inspected block.

    ``next_cursor`` can be passed as ``block_offset``/``char_offset`` in a new
    operation. Per-window bounds protect tool responses; they impose no cap on
    how far into a permitted original source a model may inspect.
    """
    _exact_copy_ref(copy_ref)
    if (not isinstance(operation_id, str) or not operation_id
            or type(block_offset) is not int or block_offset < 0
            or type(block_limit) is not int or not 1 <= block_limit <= _MAX_BLOCKS_PER_WINDOW
            or type(char_offset) is not int or char_offset < 0
            or type(char_limit) is not int or not 1 <= char_limit <= _MAX_CHARS_PER_BLOCK
            or block_limit * char_limit > _MAX_CHARS_PER_WINDOW):
        raise EvidenceError("Reading window needs bounded nonnegative offsets and positive limits")
    store = library.store
    service = IntakeService(library)
    copy = store.get(**copy_ref)
    if copy is None or copy["body"].get("inspection_state") != "checked":
        raise EvidenceError("Exact checked source copy is missing")
    use_ref = service._current_model_use(copy_ref)
    use = store.get(**use_ref)
    quality_ref = use["body"].get("quality_ref")
    quality = store.get(**quality_ref) if isinstance(quality_ref, dict) else None
    if quality is not None and quality["body"].get("classification") == "image_document":
        raise EvidenceError(
            "Image document has no exact selectable text; use read_document_ocr for a selected page"
        )
    if (quality is None or quality["kind"] != "intake_quality"
            or quality["body"].get("classification") not in _COPY_CLASSES):
        raise EvidenceError("Exact inspected source quality is missing or unusable")
    blob = copy["body"].get("blob")
    if not isinstance(blob, dict):
        raise EvidenceError("Reading window requires retained permitted source bytes")
    content = store.read_blob(blob["sha256"])
    digest = hashlib.sha256(content).hexdigest()
    if (digest != copy["body"].get("content_sha256")
            or digest != use["body"].get("source_sha256")
            or digest != quality["body"].get("source_sha256")):
        raise EvidenceError("Source copy, quality and use hashes disagree")
    blocks, parser = extract_copy_blocks(content, copy["body"])
    selected, next_cursor, omissions = _window_slices(
        blocks, block_offset=block_offset, block_limit=block_limit,
        char_offset=char_offset, char_limit=char_limit,
    )
    navigation = source_navigation(
        blocks, mime_type=copy["body"]["mime_type"], block_offset=block_offset,
    )
    if quality["body"].get("truncated"):
        omissions.append({"reason": "original_source_is_partial"})
    progress = reading_progress(selected, next_cursor, mime_type=copy["body"]["mime_type"],
                                total_blocks=len(blocks))
    reviewed_table = quality["body"].get("table_check")
    selected_pages = {
        int(match.group(1)) for item in selected
        if (match := re.match(r"page=(\d+)\b", item["locator"]))
    }
    current_page_check = (
        reviewed_table if isinstance(reviewed_table, dict)
        and reviewed_table.get("page") in selected_pages else None
    )
    request = {
        "schema": "ora.evidence.read_window_request.v1", "copy_ref": copy_ref,
        "content_sha256": digest, "block_offset": block_offset, "block_limit": block_limit,
        "char_offset": char_offset, "char_limit": char_limit,
    }

    def save(tx: Any) -> dict[str, Any]:
        exact_copy = tx.get(**copy_ref)
        if exact_copy is None or exact_copy["body"].get("content_sha256") != digest:
            raise EvidenceError("Exact source copy changed during window construction")
        current_use = tx.resolve("source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}")
        transaction_use_ref = _require_model_use_record(current_use, copy_ref)
        if transaction_use_ref != use_ref:
            raise EvidenceError("Source-use policy changed during window construction")
        exact_quality = tx.get(**quality_ref)
        if exact_quality is None or exact_quality["body"].get("source_sha256") != digest:
            raise EvidenceError("Source-quality record changed during window construction")
        passage_refs = []
        passage_records = []
        previews = []
        for item in selected:
            locator = item["locator"]
            stable = hashlib.sha256(
                f"{copy_ref['id']}:{copy_ref['version']}:{locator}:{item['text']}".encode("utf-8")
            ).hexdigest()[:32]
            passage_id = f"{copy_ref['id']}:window-{stable}"
            existing = tx.get("source_passage", passage_id)
            if existing is None:
                passage = tx.put("source_passage", passage_id, {
                    "span_id": f"window-{stable}", "locator": locator, "text": item["text"],
                    "copy_ref": copy_ref, "inspection_state": "checked",
                    "validation_record": f"ora-read-window-v1:{parser}:exact-substring",
                })
                tx.link(passage, exact_copy, "depends_on")
            else:
                passage = existing
                if (passage["body"].get("copy_ref") != copy_ref
                        or passage["body"].get("locator") != locator
                        or passage["body"].get("text") != item["text"]
                        or passage["body"].get("inspection_state") != "checked"):
                    raise EvidenceError("Existing window passage disagrees with exact source")
            passage_records.append(passage)
            passage_refs.append(_ref(passage))
            previews.append({"passage_ref": _ref(passage), "locator": locator,
                             "text_preview": item["text"], "block_index": item["block_index"]})
        window = tx.put("source_read_window", f"read-window-{uuid.uuid4().hex}", {
            **request, "quality_ref": quality_ref, "source_use_ref": use_ref,
            "parser": parser, "total_blocks": len(blocks), "next_cursor": next_cursor,
            "reading_progress": progress,
            "omissions": omissions, "passage_refs": passage_refs,
            "selected_locators": [item["locator"] for item in selected],
        })
        tx.link(window, exact_copy, "depends_on")
        tx.link(window, current_use, "depends_on")
        tx.link(window, exact_quality, "depends_on")
        for passage in passage_records:
            tx.link(window, passage, "includes_passage")
        return {
            "window_ref": _ref(window), "source_id": copy_ref["id"], "copy_ref": copy_ref,
            "content_sha256": digest, "quality_ref": quality_ref,
            "source_use_ref": use_ref, "passage_refs": passage_refs,
            "passages": previews, "total_blocks": len(blocks),
            "reading_progress": progress,
            "next_cursor": next_cursor, "omissions": omissions,
            "navigation": navigation,
            "source_selection_omissions": quality["body"].get("selection_omissions", []),
            "table_context": {
                "rendered_original_check": current_page_check,
                "reviewed_page_elsewhere": (
                    reviewed_table.get("page") if isinstance(reviewed_table, dict)
                    and current_page_check is None else None
                ),
                "caution": "PDF layout text alone does not validate a table cell; inspect the rendered original page and its header, units, denominator and footnote.",
            } if copy["body"]["mime_type"] == "application/pdf" else None,
        }

    result = store.transact(operation_id, request, save)
    if service._current_model_use(copy_ref) != result["source_use_ref"]:
        raise EvidenceError("Source-use policy changed before window return")
    return result
