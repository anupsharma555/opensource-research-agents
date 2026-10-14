"""A pasted source records exact submitted-text quality without source claims."""

from __future__ import annotations

import hashlib

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.evidence import EvidenceError, EvidenceLibrary, checked_model_text, read_source_window
from ora.evidence.coverage import extracted_text_coverage
from ora.evidence.intake import EXACT_SUBMISSION_PARSER_VERSION, EXACT_SUBMISSION_VALIDATION
from tests.mission.test_service import service


def test_supplied_quality_preserves_utf8_bytes_bom_offsets_and_revocation_on_restart(
    tmp_path, monkeypatch,
):
    supplied = "\ufeffCafé 🧪\r\nLine two"

    def no_model(*_args, **_kwargs):
        pytest.fail("Supplied quality inspection must not dispatch a model")

    host = service(tmp_path, monkeypatch, no_model)
    try:
        run = host.start(LiveStartRequest(
            request_id="supplied-quality-exact-utf8",
            question="Inspect only the synthetic submitted text",
            evidence_text=supplied, agent_ids=["agent-synthesis"],
        ))
        copy_ref = host._control(run.run_id, "agent-synthesis")["body"]["source_refs"][0]
        copy = host.store.get(**copy_ref)
        passage = host.store.get("source_passage", copy["body"]["passage_ids"][0])
        use = host.store.resolve("source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}")
        quality_ref = use["body"]["quality_ref"]
        quality = host.store.get(**quality_ref)
        body = quality["body"]
        exact_locator = f"submitted text; chars=0:{len(supplied)}"
        assert body["copy_ref"] == copy_ref
        assert body["source_sha256"] == hashlib.sha256(supplied.encode("utf-8")).hexdigest()
        assert body["byte_length"] == len(supplied.encode("utf-8")) > len(supplied)
        assert body["inspected_text_length"] == len(supplied)
        assert body["stored_content_type"] == copy["body"]["mime_type"] == "text/plain"
        assert body["source_version"] == copy["body"]["source_version"] == "submitted-v1"
        assert body["parser"] == EXACT_SUBMISSION_VALIDATION
        assert body["parser_version"] == EXACT_SUBMISSION_PARSER_VERSION
        assert body["selected_locators"] == [exact_locator]
        assert body["completeness_scope"] == "exact_submitted_text_only"
        assert "original source identity" in body["classification_reason"]
        assert not {"returned_content_type", "detected_content_type", "mime_mismatch", "identity_basis"} & body.keys()
        assert passage["body"]["text"] == supplied
        assert passage["body"]["locator"] == exact_locator
        assert passage["body"]["end"] == len(supplied)
        assert any(link["target"] == copy_ref and link["relation"] == "depends_on"
                   for link in host.store.links(quality))
        packet = checked_model_text(EvidenceLibrary(host.store), copy_ref, [ref(passage)])
        assert packet["passages"][0]["text"] == supplied
        coverage = extracted_text_coverage(host.store, copy_ref, [ref(passage)], [])
        assert coverage["status"] == "complete_extracted_text"
        assert coverage["covered_chars"] == len(supplied.removeprefix("\ufeff"))
        assert coverage["original_source_partial"] is True
        window = read_source_window(host.evidence, "quality-partial-window", copy_ref,
                                    block_offset=0, char_offset=0, char_limit=5)
        coverage = extracted_text_coverage(host.store, copy_ref, window["passage_refs"], [])
        assert coverage["status"] == "incomplete_extracted_text"
        assert coverage["covered_chars"] == 5
        assert host.store.records("model_request_intent") == []
    finally:
        host.close()

    reopened = service(tmp_path, monkeypatch, no_model)
    try:
        assert reopened._supplied_source(reopened._run(run.run_id)) == copy_ref
        assert reopened.store.get(**quality_ref) == quality
        assert len(reopened.store.records("intake_quality")) == 1
        assert len(reopened.store.records("source_use")) == 1

        def revoke(tx):
            current = tx.get("source_use", use["id"])
            updated = tx.put("source_use", current["id"], {
                **current["body"], "policy_status": "revoked", "allowed_uses": [],
            }, expected_version=current["version"])
            return ref(updated)

        revoked_ref = reopened.store.transact("revoke-supplied-quality", {}, revoke)
        assert reopened._supplied_source(reopened._run(run.run_id)) == copy_ref
        assert reopened.store.resolve(
            "source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}"
        )["version"] == revoked_ref["version"]
        assert len(reopened.store.records("intake_quality")) == 1
        with pytest.raises(EvidenceError, match="does not grant model input"):
            read_source_window(EvidenceLibrary(reopened.store), "revoked-supplied-window", copy_ref)
        with pytest.raises(EvidenceError, match="does not grant model input"):
            checked_model_text(EvidenceLibrary(reopened.store), copy_ref, [ref(passage)])
    finally:
        reopened.close()
