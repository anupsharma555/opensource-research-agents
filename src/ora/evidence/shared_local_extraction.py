"""Fenced local computation cache for exact OCR images in one mission.

This record owns only reusable derived bytes. Every caller still creates its
own source-bound OCR receipt and rechecks permission before delivering text.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from typing import Callable

from ora.state.store import canonical

from .library import EvidenceError

KIND = "shared_local_ocr_extraction"
LEASE_SECONDS = 35
WAIT_SECONDS = 29.5


def _id(identity: dict) -> str:
    return "shared-ocr:" + hashlib.sha256(canonical(identity).encode()).hexdigest()[:32]


def _read(store, record: dict, identity: dict) -> tuple[str, bytes | None, dict | None, dict]:
    body = record["body"]
    if body.get("identity") != identity or body.get("phase") != "completed":
        raise EvidenceError("Shared OCR extraction identity changed")
    text_info = body.get("text_blob")
    if not isinstance(text_info, dict) or not isinstance(text_info.get("sha256"), str):
        raise EvidenceError("Shared OCR text is unavailable")
    text_bytes = store.read_blob(text_info["sha256"])
    if hashlib.sha256(text_bytes).hexdigest() != body.get("text_sha256"):
        raise EvidenceError("Shared OCR text changed")
    try:
        text = text_bytes.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise EvidenceError("Shared OCR text is not UTF-8") from exc
    tsv_info = body.get("tsv_blob")
    tsv = store.read_blob(tsv_info["sha256"]) if isinstance(tsv_info, dict) else None
    if tsv is not None and hashlib.sha256(tsv).hexdigest() != body.get("tsv_sha256"):
        raise EvidenceError("Shared OCR word geometry changed")
    return text, tsv, body.get("quality"), {key: record[key] for key in ("kind", "id", "version")}


def shared_ocr_result(
    store,
    *,
    identity: dict,
    compute: Callable[[], tuple[str, bytes | None, dict | None]],
) -> tuple[str, bytes | None, dict | None, dict]:
    """Compute once per exact identity, or wait boundedly for its active owner.

    A crashed owner can be replaced after its lease. Its late result cannot
    overwrite the replacement because the completion transaction checks the
    claim generation and token.
    """
    record_id = _id(identity)
    mission_id = identity["mission_id"]
    deadline = time.monotonic() + WAIT_SECONDS
    while True:
        current = store.get(KIND, record_id)
        if current is not None:
            body = current["body"]
            if body.get("identity") != identity:
                raise EvidenceError("Shared OCR extraction identity changed")
            if body.get("phase") == "completed":
                return _read(store, current, identity)
            if body.get("phase") == "claimed" and body.get("lease_until", 0) > store.clock():
                if time.monotonic() >= deadline:
                    raise EvidenceError("Shared OCR extraction is still pending")
                time.sleep(0.05)
                continue
        token = uuid.uuid4().hex

        def claim(tx):
            old = tx.get(KIND, record_id)
            if old is not None:
                body = old["body"]
                if body.get("identity") != identity:
                    raise EvidenceError("Shared OCR extraction identity changed")
                if body.get("phase") == "completed":
                    return {
                        "status": "completed",
                        "record_ref": {key: old[key] for key in ("kind", "id", "version")},
                    }
                if body.get("phase") == "claimed" and body.get("lease_until", 0) > store.clock():
                    return {"status": "pending"}
                generation = body["generation"] + 1
            else:
                generation = 1
            saved = tx.put(
                KIND,
                record_id,
                {
                    "schema": "ora.evidence.shared_local_ocr_extraction.v1",
                    "mission_id": mission_id,
                    "identity": identity,
                    "phase": "claimed",
                    "generation": generation,
                    "owner_token": token,
                    "lease_until": store.clock() + LEASE_SECONDS,
                },
                expected_version=old["version"] if old else 0,
            )
            return {"status": "claimed", "generation": generation, "version": saved["version"]}

        state = store.transact(
            f"shared-ocr-claim:{record_id}:{token}",
            {"identity": identity, "token": token},
            claim,
            mission_id=mission_id,
        )
        if state["status"] == "completed":
            return _read(store, store.get(**state["record_ref"]), identity)
        if state["status"] == "pending":
            if time.monotonic() >= deadline:
                raise EvidenceError("Shared OCR extraction is still pending")
            time.sleep(0.05)
            continue
        try:
            text, tsv, quality = compute()
            text_bytes = text.encode("utf-8")
            text_blob = store.put_blob(text_bytes)
            tsv_blob = store.put_blob(tsv) if tsv is not None else None
        except Exception:

            def fail(tx):
                current = tx.get(KIND, record_id)
                if current is None or current["body"].get("owner_token") != token:
                    return {"status": "lost_claim"}
                tx.put(
                    KIND,
                    record_id,
                    {**current["body"], "phase": "failed_known", "lease_until": None},
                    expected_version=current["version"],
                )
                return {"status": "failed_known"}

            store.transact(
                f"shared-ocr-fail:{record_id}:{token}",
                {"identity": identity, "token": token},
                fail,
                mission_id=mission_id,
            )
            raise

        def complete(tx):
            current = tx.get(KIND, record_id)
            if (
                current is None
                or current["body"].get("phase") != "claimed"
                or current["body"].get("generation") != state["generation"]
                or current["body"].get("owner_token") != token
            ):
                return {"status": "lost_claim"}
            saved = tx.put(
                KIND,
                record_id,
                {
                    **current["body"],
                    "phase": "completed",
                    "lease_until": None,
                    "text_blob": text_blob,
                    "text_sha256": hashlib.sha256(text_bytes).hexdigest(),
                    "tsv_blob": tsv_blob,
                    "tsv_sha256": hashlib.sha256(tsv).hexdigest() if tsv is not None else None,
                    "quality": quality,
                },
                expected_version=current["version"],
            )
            return {
                "status": "completed",
                "record_ref": {key: saved[key] for key in ("kind", "id", "version")},
            }

        finished = store.transact(
            f"shared-ocr-complete:{record_id}:{token}",
            {"identity": identity, "token": token},
            complete,
            mission_id=mission_id,
        )
        if finished["status"] == "completed":
            return _read(store, store.get(**finished["record_ref"]), identity)
        if time.monotonic() >= deadline:
            raise EvidenceError("Shared OCR extraction claim changed before completion")
