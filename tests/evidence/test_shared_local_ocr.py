"""Shared local OCR work retains separate peer source and permission receipts."""

from __future__ import annotations

import hashlib
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor

import pytest

from ora.coordination.board import ref
from ora.evidence import document_ocr
from ora.evidence.document_ocr import (
    ocr_public_document_page,
    read_public_document_ocr,
    render_public_document_page,
)
from ora.evidence.intake import IntakeService
from ora.evidence.library import EvidenceError, EvidenceLibrary
from ora.evidence.shared_local_extraction import KIND, shared_ocr_result
from ora.state import StateStore
from ora.state.store import canonical
from tests.evidence.test_document_ocr import SCOPE, saved_public_pdf, synthetic_pdf


def _second_document(store, original_ref, scope):
    original = store.get(**original_ref)
    source = original["body"]

    def save(tx):
        delivery = tx.put("retrieval_delivery", "other-delivery", {})
        hit = tx.put(
            "retrieval_hit",
            "other-hit",
            {
                **scope,
                "agent_instance_id": scope["actor_id"],
                "route": "exa.search",
                "delivery_ref": ref(delivery),
                "source": {
                    "origin": "original_source",
                    "original_url": source["requested_url"],
                    "rights_status": "unknown",
                    "content_sha256": None,
                },
            },
        )
        tx.link(hit, delivery, "derived_from")
        document = tx.put(
            "public_document_fetch",
            "other-document",
            {**source, **scope, "hit_ref": ref(hit), "response_ref": ref(delivery)},
        )
        tx.link(document, hit, "fetched_from_browser_hit")
        tx.link(document, delivery, "derived_from_browser_response")
        return ref(document)

    return store.transact("other-document", {}, save, mission_id=scope["mission_id"])


@pytest.mark.skipif(
    not all(shutil.which(tool) for tool in ("pdfinfo", "pdftoppm")),
    reason="local PDF renderer unavailable",
)
def test_two_peer_views_share_one_ocr_process_but_keep_separate_receipts(tmp_path, monkeypatch):
    store = StateStore(tmp_path)
    original = saved_public_pdf(store, synthetic_pdf("Synthetic shared OCR page"))
    other_scope = {**SCOPE, "actor_id": "other-peer", "task_id": "other-task"}
    other = _second_document(store, original, other_scope)
    first_page = render_public_document_page(
        store, original, **SCOPE, page_number=1, operation_id="first-render"
    )
    second_page = render_public_document_page(
        store, other, **other_scope, page_number=1, operation_id="second-render"
    )
    assert first_page["image_sha256"] == second_page["image_sha256"]
    monkeypatch.setattr(
        document_ocr,
        "_selective_engine_identity",
        lambda: {
            "version": "synthetic",
            "binary_sha256": "b" * 64,
            "traineddata_sha256": "c" * 64,
            "language": "eng",
        },
    )
    calls = []
    original_run = document_ocr._run

    def local_ocr(argv, **kwargs):
        if argv[0] == "tesseract":
            calls.append(argv)
            return subprocess.CompletedProcess(argv, 0, b"Synthetic shared OCR page", b"")
        return original_run(argv, **kwargs)

    monkeypatch.setattr(document_ocr, "_run", local_ocr)
    first = ocr_public_document_page(
        store,
        first_page["page_image_ref"],
        **SCOPE,
        operation_id="first-ocr",
        shared_enabled=True,
    )
    second = ocr_public_document_page(
        store,
        second_page["page_image_ref"],
        **other_scope,
        operation_id="second-ocr",
        shared_enabled=True,
    )
    assert len(calls) == 1
    assert first["ocr_ref"] != second["ocr_ref"]
    assert len(store.records(KIND)) == 1
    first_record, second_record = store.get(**first["ocr_ref"]), store.get(**second["ocr_ref"])
    assert (
        first_record["body"]["shared_extraction_ref"]
        == second_record["body"]["shared_extraction_ref"]
    )
    assert first_record["body"]["task_id"] == SCOPE["task_id"]
    assert second_record["body"]["task_id"] == other_scope["task_id"]
    with pytest.raises(EvidenceError, match="another peer task"):
        read_public_document_ocr(store, second["ocr_ref"], **SCOPE)
    assert "text" not in read_public_document_ocr(store, second["ocr_ref"], **other_scope)
    inspected = IntakeService(EvidenceLibrary(store)).inspect_public_document(
        "inspect-other", other, **other_scope
    )
    assert inspected["model_input_status"] == "permitted"
    assert read_public_document_ocr(store, second["ocr_ref"], **other_scope)["text"] == (
        "Synthetic shared OCR page"
    )
    use = store.get(**inspected["source_use_ref"])
    store.transact(
        "revoke-other",
        {},
        lambda tx: tx.put(
            "source_use",
            use["id"],
            {**use["body"], "allowed_uses": [], "policy_status": "revoked"},
            expected_version=use["version"],
        ),
        mission_id=SCOPE["mission_id"],
    )
    with pytest.raises(EvidenceError, match="not permitted|permitted original lead"):
        read_public_document_ocr(store, second["ocr_ref"], **other_scope)


def test_atomic_claim_reuses_after_restart_and_fences_expired_owner(tmp_path):
    now = [100.0]
    store = StateStore(tmp_path, clock=lambda: now[0])
    identity = {
        "mission_id": "mission",
        "image_sha256": "a" * 64,
        "engine_identity": {"binary_sha256": "b" * 64},
        "layout": "auto",
        "psm": 3,
    }
    calls = []

    def compute():
        calls.append(1)
        return "exact text", None, None

    first = shared_ocr_result(store, identity=identity, compute=compute)
    reopened = StateStore(tmp_path, clock=lambda: now[0])
    second = shared_ocr_result(reopened, identity=identity, compute=compute)
    assert first == second
    assert calls == [1]

    later_identity = {**identity, "layout": "block", "psm": 6}

    def stale_owner():
        now[0] += 40
        recovered = shared_ocr_result(
            reopened,
            identity=later_identity,
            compute=lambda: ("new owner text", None, None),
        )
        assert recovered[0] == "new owner text"
        return "stale owner text", None, None

    recovered = shared_ocr_result(store, identity=later_identity, compute=stale_owner)
    assert recovered[0] == "new owner text"
    assert len(store.records(KIND)) == 2


def test_concurrent_identical_claim_runs_local_computation_once(tmp_path):
    store = StateStore(tmp_path)
    identity = {
        "mission_id": "mission",
        "image_sha256": "d" * 64,
        "engine_identity": {"binary_sha256": "e" * 64},
        "layout": "auto",
        "psm": 3,
    }
    calls = []

    def compute():
        import time

        calls.append(1)
        time.sleep(0.15)
        return "shared result", None, None

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(
            pool.map(
                lambda _: shared_ocr_result(store, identity=identity, compute=compute), range(2)
            )
        )
    assert results[0] == results[1]
    assert calls == [1]
    db = store._connect()
    try:
        claims = db.execute(
            "SELECT COUNT(*) FROM operations WHERE id LIKE 'shared-ocr-claim:%'"
        ).fetchone()[0]
    finally:
        db.close()
    assert claims <= 2  # A single atomic race is possible; polling does not write.


def test_live_claim_never_reports_pending_as_completed(tmp_path, monkeypatch):
    from ora.evidence import shared_local_extraction as shared

    store = StateStore(tmp_path, clock=lambda: 100.0)
    identity = {
        "mission_id": "mission",
        "image_sha256": "f" * 64,
        "engine_identity": {"binary_sha256": "g" * 64},
        "layout": "auto",
        "psm": 3,
    }
    key = "shared-ocr:" + hashlib.sha256(canonical(identity).encode()).hexdigest()[:32]
    store.transact(
        "synthetic-active-claim",
        {},
        lambda tx: tx.put(
            KIND,
            key,
            {
                "identity": identity,
                "phase": "claimed",
                "generation": 1,
                "owner_token": "synthetic",
                "lease_until": 135.0,
            },
        ),
        mission_id="mission",
    )
    monkeypatch.setattr(shared, "WAIT_SECONDS", 0.01)
    db = store._connect()
    try:
        before = db.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
    finally:
        db.close()
    with pytest.raises(EvidenceError, match="still pending"):
        shared_ocr_result(
            store,
            identity=identity,
            compute=lambda: pytest.fail("A live owner must not dispatch another OCR"),
        )
    db = store._connect()
    try:
        after = db.execute("SELECT COUNT(*) FROM operations").fetchone()[0]
    finally:
        db.close()
    assert after == before


def test_known_local_failure_can_be_retried_with_new_fenced_generation(tmp_path):
    store = StateStore(tmp_path)
    identity = {
        "mission_id": "mission",
        "image_sha256": "h" * 64,
        "engine_identity": {"binary_sha256": "i" * 64},
        "layout": "auto",
        "psm": 3,
    }

    def fail():
        raise EvidenceError("Synthetic local OCR failed")

    with pytest.raises(EvidenceError, match="Synthetic local OCR failed"):
        shared_ocr_result(store, identity=identity, compute=fail)
    failed = store.records(KIND)[0]
    assert failed["body"]["phase"] == "failed_known"
    result = shared_ocr_result(
        store, identity=identity, compute=lambda: ("recovered text", None, None)
    )
    assert result[0] == "recovered text"
    assert store.records(KIND)[0]["body"]["generation"] == 2
