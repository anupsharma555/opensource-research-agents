"""Attest otherwise-authorized raw local text without granting a source use.

The receipt describes bytes validated now. It does not reconstruct an unknown
historical parser, external response type, publication identity or permission.
"""

from __future__ import annotations

import hashlib

from ora.state import Transaction
from ora.state.store import canonical

from .library import EvidenceError

PARSER = "strict-utf8-decode"
PARSER_VERSION = "ora-local-retained-utf8-v1"


def _ref(record: dict) -> dict:
    return {key: record[key] for key in ("kind", "id", "version")}


def _exact_record(tx: Transaction, pointer: object, kind: str) -> dict | None:
    if (
        type(pointer) is not dict
        or set(pointer) != {"kind", "id", "version"}
        or pointer.get("kind") != kind
        or not isinstance(pointer.get("id"), str)
        or type(pointer.get("version")) is not int
        or pointer["version"] < 1
    ):
        return None
    return tx.get(**pointer)


def _quality_id(copy_ref: dict) -> str:
    digest = hashlib.sha256(canonical(copy_ref).encode("utf-8")).hexdigest()[:32]
    return f"local-text-quality-{digest}"


def attest_local_text_quality_tx(
    tx: Transaction, copy_ref: dict, current_use_ref: dict | None,
    *, require_bound_quality: bool = False,
) -> dict:
    """Return exact current quality for a raw copy, creating only a local receipt.

    The model boundary has already authorized this exact copy and passes the
    current source-use ref, if any. We repeat its binding and byte checks within
    the caller's intent/dispatch transaction. Other source routes are untouched.
    """
    copy = tx.get(**copy_ref)
    if copy is None or copy["kind"] != "source_copy":
        raise EvidenceError("Exact raw source copy is unavailable")
    body = copy["body"]
    encounter = _exact_record(tx, body.get("encounter"), "source_encounter")
    if encounter is None:
        raise EvidenceError("Raw source encounter is unavailable")
    if (
        encounter["body"].get("status") not in {"accepted", "partial"}
        or encounter["body"].get("publication") != body.get("publication")
        or body.get("inspection_state") != "checked"
        or not isinstance(body.get("validation_record"), str)
        or not body["validation_record"]
        or body.get("retain_bytes") is not True
        or body.get("rights_status") not in (None, "permitted")
    ):
        raise EvidenceError("Saved raw source cannot be attested as checked local text")
    bound = tx.resolve("source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}")
    if (_ref(bound) if bound else None) != current_use_ref:
        raise EvidenceError("Raw source-use binding changed during quality attestation")
    if bound and (
        bound["body"].get("copy_ref") != copy_ref
        or bound["body"].get("policy_status") not in (None, "granted")
        or not isinstance(bound["body"].get("allowed_uses"), list)
        or "model_input" not in bound["body"]["allowed_uses"]
    ):
        raise EvidenceError("Raw source lacks a current model-use permission")
    blob = body.get("blob")
    if not isinstance(blob, dict) or set(blob) != {"sha256", "size"}:
        raise EvidenceError("Raw source lacks exact retained bytes")
    content = tx.store.read_blob(blob["sha256"])
    digest = hashlib.sha256(content).hexdigest()
    if (
        digest != body.get("content_sha256")
        or digest != blob["sha256"]
        or len(content) != blob["size"]
    ):
        raise EvidenceError("Raw source bytes disagree with their saved hash")
    existing_ref = bound["body"].get("quality_ref") if bound else None
    if require_bound_quality and existing_ref is None:
        raise EvidenceError("Selected passage needs its current inspected quality receipt")
    prior_quality = None
    if existing_ref is not None:
        prior_quality = _exact_record(tx, existing_ref, "intake_quality")
        quality_body = prior_quality["body"] if prior_quality else None
        if (
            quality_body is None
            or quality_body.get("source_sha256") != digest
            or quality_body.get("copy_ref", copy_ref) != copy_ref
            or quality_body.get("source_version", body.get("source_version"))
            != body.get("source_version")
            or quality_body.get("stored_content_type", body.get("mime_type"))
            != body.get("mime_type")
            or quality_body.get("byte_length", len(content)) != len(content)
            or bound["body"].get("source_sha256", digest) != digest
            or not isinstance(quality_body.get("parser"), str)
            or quality_body["parser"] in {"", "unavailable"}
            or not isinstance(quality_body.get("parser_version"), str)
            or not quality_body["parser_version"]
        ):
            raise EvidenceError("Current raw source quality is malformed")
        inspected_length = quality_body.get("inspected_text_length")
        locators = quality_body.get("selected_locators")
        clipped = quality_body.get("truncated")
        declared_bytes = quality_body.get("byte_length")
        classification = quality_body.get("classification")
        if (
            (declared_bytes is not None and (
                type(declared_bytes) is not int or declared_bytes < 0
            ))
            or (classification is not None and (
                not isinstance(classification, str) or not classification
            ))
            or (inspected_length is not None and (
                type(inspected_length) is not int or inspected_length < 0
            ))
            or (locators is not None and (
                not isinstance(locators, list)
                or any(not isinstance(item, str) for item in locators)
            ))
            or (clipped is not None and type(clipped) is not bool)
        ):
            raise EvidenceError("Current raw source quality has invalid inspection metadata")
        complete = (
            type(declared_bytes) is int
            and type(inspected_length) is int
            and isinstance(locators, list)
            and isinstance(classification, str)
            and (type(clipped) is bool or quality_body.get("completeness_scope") in {
                "exact_retained_bytes_only", "exact_submitted_text_only",
            })
        )
        if complete:
            return _ref(prior_quality)

    mime = body.get("mime_type")
    if not isinstance(mime, str) or mime.split(";", 1)[0].strip().lower() != "text/plain":
        raise EvidenceError("Raw non-plain source needs an inspected quality receipt")
    try:
        decoded = content.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvidenceError("Raw source is not exact UTF-8 text") from exc

    quality_body = {
        "schema": "ora.evidence.intake_quality.v1",
        "copy_ref": copy_ref,
        "source_sha256": digest,
        "byte_length": len(content),
        "stored_content_type": body["mime_type"],
        "source_version": body.get("source_version"),
        "retrieved_at": body.get("retrieved_at"),
        "parser": PARSER,
        "parser_version": PARSER_VERSION,
        "classification": body.get("access_surface"),
        "classification_reason": (
            "Exact retained local UTF-8 text validated now; historical extraction, "
            "remote response identity and original-source completeness are unknown"
        ),
        "encounter_route": encounter["body"].get("route"),
        "source_url": body.get("reference"),
        "inspected_text_length": len(decoded),
        "selected_locators": [f"retained text; chars=0:{len(decoded)}"],
        "completeness_scope": "exact_retained_bytes_only",
        "rights_status": body.get("rights_status") or "unknown_saved_copy",
        "rights_basis": body.get("rights_basis"),
        "truncated": None,
        "selection_omissions": [],
        "table_check": None,
    }
    record_id = _quality_id(copy_ref)
    existing = tx.get("intake_quality", record_id)
    if existing is not None:
        if existing["body"] != quality_body or existing["version"] != 1:
            raise EvidenceError("Saved local-text source quality changed")
        if prior_quality is not None:
            tx.link(existing, prior_quality, "derived_from")
        return _ref(existing)
    quality = tx.put("intake_quality", record_id, quality_body)
    tx.link(quality, copy, "depends_on")
    if prior_quality is not None:
        tx.link(quality, prior_quality, "derived_from")
    return _ref(quality)
