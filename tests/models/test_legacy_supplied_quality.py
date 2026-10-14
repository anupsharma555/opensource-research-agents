"""Legacy pasted text needs local quality provenance before a new model intent."""

from __future__ import annotations

import hashlib
import json

import pytest

from ora.coordination.board import ref
from ora.evidence import EvidenceLibrary
from ora.evidence.local_text_quality import PARSER, PARSER_VERSION
from ora.models import ModelSession, SessionError, SourceSelection
from ora.state import StateStore
from ora.state.store import canonical
from tests.mission.test_report_dispatch_dependency import _prepared_report
from tests.models.test_session import ROOT, _preview_task, setup

TEXT = "\ufeffCafe\u0301 🧪\r\nSynthetic source"
MESSAGES = [{"role": "user", "content": "Review the synthetic retained text"}]


def _raw_copy(
    store: StateStore, *, route: str = "user_supplied", mime: str = "text/plain",
    text: str = TEXT, passages: list[dict] | None = None,
) -> dict:
    library = EvidenceLibrary(store)
    publication = library.register_publication(
        "legacy-quality-publication", identifiers={}, title="Synthetic pasted text",
        verification="Local synthetic test input",
    )["publication_ref"]
    encounter = library.record_encounter(
        "legacy-quality-encounter", publication_ref=publication,
        retrieval_hit_ref=None, status="accepted", access_surface="passage",
        retrieved_at="2026-01-02T00:00:00Z", route=route,
    )["encounter_ref"]
    return library.add_copy(
        "legacy-quality-copy", publication, encounter,
        content=text.encode("utf-8"), content_sha256=None,
        mime_type=mime, source_version="legacy-v1", access_surface="passage",
        retrieved_at="2026-01-02T00:00:00Z", rights_basis="Local synthetic submission",
        retain_bytes=True, reference="local-synthetic:legacy-quality",
        passages=passages or [],
        inspection_state="checked", validation_record="legacy-host-text",
    )["copy_ref"]


def test_legacy_supplied_raw_text_gets_quality_at_intent_save_not_preview(tmp_path):
    copy_ref = _raw_copy(StateStore(tmp_path))
    store, registry, bridge, call, config, _copy, captured = setup(
        tmp_path, source_ref=copy_ref,
    )
    _preview_task(store)
    selection = (SourceSelection(copy_ref, "permitted", True),)
    messages = [{"role": "user", "content": "Review the synthetic pasted text"}]

    preview = bridge.preview_request(
        call=call, phase="collect", messages=messages, sources=selection,
    )
    assert json.loads(preview.body["messages"][1]["content"])["source_packets"][0][
        "text"
    ] == TEXT
    assert store.records("intake_quality") == []
    assert store.records("model_request_intent") == []
    assert captured == []

    prepared = bridge.prepare(
        call_id=call["id"], call_version=call["version"], phase="collect",
        messages=messages, sources=selection,
    )
    assert prepared.request.request_sha256 == preview.request_sha256
    quality = store.records("intake_quality")
    assert len(quality) == 1
    body = quality[0]["body"]
    assert body["copy_ref"] == copy_ref
    assert body["source_sha256"] == hashlib.sha256(TEXT.encode()).hexdigest()
    assert body["byte_length"] == len(TEXT.encode()) > len(TEXT)
    assert body["inspected_text_length"] == len(TEXT)
    assert body["selected_locators"] == [f"retained text; chars=0:{len(TEXT)}"]
    assert (body["parser"], body["parser_version"]) == (PARSER, PARSER_VERSION)
    assert body["completeness_scope"] == "exact_retained_bytes_only"
    assert body["truncated"] is None
    assert body["rights_status"] == "unknown_saved_copy"
    assert body["encounter_route"] == "user_supplied"
    assert "returned_content_type" not in body
    assert store.records("source_use") == []
    assert any(link["target"] == copy_ref for link in store.links(quality[0]))

    reopened = StateStore(tmp_path)
    call_two = registry.start_call(
        "legacy-quality-second-call", call_id="call-2", configuration_id=config["id"],
        mission_id="mission", task_id="task", attempt_id="attempt-2",
        loaded_skill_ids=[], exposed_tool_ids=["evidence_lookup"],
        require_common_tools=False,
    )
    ModelSession(reopened, ROOT, adapter=bridge.adapter).prepare(
        call_id=call_two["id"], call_version=1, phase="collect", messages=messages,
        sources=selection,
    )
    assert reopened.get(**ref(quality[0])) == quality[0]
    assert len(reopened.records("intake_quality")) == 1


def test_generic_checked_plain_text_uses_same_receipt_without_new_grant(tmp_path):
    copy_ref = _raw_copy(StateStore(tmp_path), route="synthetic-fixture")
    store, _registry, bridge, call, _config, _copy, _captured = setup(
        tmp_path, source_ref=copy_ref,
    )
    prepared = bridge.prepare(
        call_id=call["id"], call_version=1, phase="collect", messages=MESSAGES,
        sources=(SourceSelection(copy_ref, "permitted", True),),
    )
    quality = store.records("intake_quality")
    assert len(quality) == 1
    assert quality[0]["body"]["encounter_route"] == "synthetic-fixture"
    assert store.records("source_use") == []
    assert prepared.request.body["model"]


def test_sparse_historical_submission_attests_only_reconstructible_fields(tmp_path):
    initial = StateStore(tmp_path)
    content = b"Sparse synthetic pasted evidence."
    blob = initial.put_blob(content)

    def save(tx):
        publication = tx.put("publication", "sparse-publication", {"title": "Synthetic"})
        encounter = tx.put("source_encounter", "sparse-encounter", {
            "publication": ref(publication), "route": "user_supplied", "status": "accepted",
        })
        copy = tx.put("source_copy", "sparse-copy", {
            "publication": ref(publication), "encounter": ref(encounter),
            "mime_type": "text/plain", "rights_status": "permitted", "blob": blob,
            "content_sha256": blob["sha256"], "retain_bytes": True,
            "inspection_state": "checked", "validation_record": "historical-local-check",
            "passage_ids": [],
        })
        return ref(copy)

    copy_ref = initial.transact("sparse-supplied-copy", {}, save, mission_id="mission")
    store, _registry, bridge, call, _config, _copy, _captured = setup(
        tmp_path, source_ref=copy_ref,
    )
    bridge.prepare(
        call_id=call["id"], call_version=1, phase="collect", messages=MESSAGES,
        sources=(SourceSelection(copy_ref, "permitted", True),),
    )
    quality = store.records("intake_quality")[0]["body"]
    assert quality["copy_ref"] == copy_ref
    assert quality["source_sha256"] == blob["sha256"]
    assert quality["source_version"] is None
    assert quality["retrieved_at"] is None
    assert quality["source_url"] is None
    assert quality["rights_status"] == "permitted"
    assert quality["byte_length"] == len(content)
    assert store.records("source_use") == []


def test_sparse_bound_quality_gets_separate_exact_local_inspection(tmp_path):
    copy_ref = _raw_copy(StateStore(tmp_path))
    store, _registry, bridge, call, _config, _copy, _captured = setup(
        tmp_path, source_ref=copy_ref,
    )
    digest = hashlib.sha256(TEXT.encode()).hexdigest()

    def seed(tx):
        old_quality = tx.put("intake_quality", "old-sparse-quality", {
            "schema": "ora.evidence.intake_quality.v1", "copy_ref": copy_ref,
            "source_sha256": digest, "parser": "historical-local-parser",
            "parser_version": "historical-v1",
        })
        use = tx.put("source_use", "old-quality-use", {
            "copy_ref": copy_ref, "source_sha256": digest,
            "policy_status": "granted", "allowed_uses": ["model_input"],
            "quality_ref": ref(old_quality),
        })
        tx.bind("source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}",
                "source_use", use["id"])
        return {"quality_ref": ref(old_quality), "use_ref": ref(use)}

    saved = store.transact("seed-sparse-quality", {}, seed, mission_id="mission")
    before_quality = store.get(**saved["quality_ref"])
    before_use = store.get(**saved["use_ref"])
    bridge.prepare(
        call_id=call["id"], call_version=1, phase="collect", messages=MESSAGES,
        sources=(SourceSelection(copy_ref, "permitted", True),),
    )
    assert store.get(**saved["quality_ref"]) == before_quality
    assert store.get(**saved["use_ref"]) == before_use
    quality = store.records("intake_quality")
    assert len(quality) == 2
    local = next(item for item in quality if item["id"] != before_quality["id"])
    assert local["body"]["source_sha256"] == digest
    assert local["body"]["inspected_text_length"] == len(TEXT)
    assert local["body"]["selected_locators"] == [f"retained text; chars=0:{len(TEXT)}"]
    assert local["body"]["truncated"] is None
    assert any(link["target"] == saved["quality_ref"]
               and link["relation"] == "derived_from" for link in store.links(local))


def test_plain_text_charset_variant_keeps_exact_stored_mime(tmp_path):
    stored_mime = "text/plain; charset=utf-8"
    copy_ref = _raw_copy(StateStore(tmp_path), mime=stored_mime)
    store, _registry, bridge, call, _config, _copy, _captured = setup(
        tmp_path, source_ref=copy_ref,
    )
    bridge.prepare(
        call_id=call["id"], call_version=1, phase="collect", messages=MESSAGES,
        sources=(SourceSelection(copy_ref, "permitted", True),),
    )
    quality = store.records("intake_quality")
    assert len(quality) == 1
    assert quality[0]["body"]["stored_content_type"] == stored_mime


def test_checked_passage_with_sparse_bound_quality_keeps_exact_citation(tmp_path):
    text = "Synthetic checked passage for a legacy copy."
    locator = f"text; chars=0:{len(text)}; chars=0:{len(text)}"
    copy_ref = _raw_copy(StateStore(tmp_path), text=text, passages=[{
        "span_id": "legacy-span", "locator": locator, "text": text,
        "start": 0, "end": len(text),
    }])
    store, _registry, bridge, call, _config, _copy, _captured = setup(
        tmp_path, source_ref=copy_ref,
    )
    copy = store.get(**copy_ref)
    passage = store.get("source_passage", copy["body"]["passage_ids"][0])
    passage_ref = ref(passage)
    digest = hashlib.sha256(text.encode()).hexdigest()

    def seed(tx):
        quality = tx.put("intake_quality", "old-passage-quality", {
            "schema": "ora.evidence.intake_quality.v1", "copy_ref": copy_ref,
            "source_sha256": digest, "parser": "utf8-text",
            "parser_version": "historical-v1",
        })
        use = tx.put("source_use", "old-passage-use", {
            "copy_ref": copy_ref, "source_sha256": digest,
            "policy_status": "granted", "allowed_uses": ["model_input"],
            "quality_ref": ref(quality),
        })
        tx.bind("source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}",
                "source_use", use["id"])
        return {"quality_ref": ref(quality), "use_ref": ref(use)}

    saved = store.transact("seed-sparse-passage", {}, seed, mission_id="mission")
    selection = (SourceSelection(copy_ref, "permitted", True,
                                 passage_refs=(passage_ref,)),)
    preview = bridge.preview_request(
        call=call, phase="collect", messages=MESSAGES, sources=selection,
    )
    packet = json.loads(preview.body["messages"][1]["content"])["source_packets"][0]
    assert packet["text_basis"] == "checked_passages"
    assert packet["passage_refs"] == [passage_ref]
    before_use = store.get(**saved["use_ref"])
    before_quality = store.get(**saved["quality_ref"])
    prepared = bridge.prepare(
        call_id=call["id"], call_version=1, phase="collect", messages=MESSAGES,
        sources=selection,
    )
    assert prepared.request.request_sha256 == preview.request_sha256
    assert store.get(**saved["use_ref"]) == before_use
    assert store.get(**saved["quality_ref"]) == before_quality
    assert len(store.records("intake_quality")) == 2
    assert prepared.request.body["messages"][1] == preview.body["messages"][1]


def test_sparse_bound_nonplain_quality_cannot_be_relabelled_plain(tmp_path):
    copy_ref = _raw_copy(StateStore(tmp_path), route="synthetic-fixture", mime="text/html")
    store, _registry, bridge, call, _config, _copy, captured = setup(
        tmp_path, source_ref=copy_ref,
    )

    def seed(tx):
        quality = tx.put("intake_quality", "sparse-html-quality", {
            "copy_ref": copy_ref, "source_sha256": hashlib.sha256(TEXT.encode()).hexdigest(),
            "parser": "stdlib-htmlparser", "parser_version": "historical-v1",
        })
        use = tx.put("source_use", "sparse-html-use", {
            "copy_ref": copy_ref, "policy_status": "granted",
            "allowed_uses": ["model_input"], "quality_ref": ref(quality),
        })
        tx.bind("source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}",
                "source_use", use["id"])
        return ref(use)

    store.transact("seed-sparse-html", {}, seed, mission_id="mission")
    with pytest.raises(SessionError, match="Raw non-plain source needs"):
        bridge.prepare(
            call_id=call["id"], call_version=1, phase="collect", messages=MESSAGES,
            sources=(SourceSelection(copy_ref, "permitted", True),),
        )
    assert len(store.records("intake_quality")) == 1
    assert store.records("model_request_intent") == []
    assert captured == []


def test_nonplain_raw_text_without_existing_quality_is_rejected(tmp_path):
    copy_ref = _raw_copy(StateStore(tmp_path), route="synthetic-fixture", mime="text/html")
    store, _registry, bridge, call, _config, _copy, captured = setup(
        tmp_path, source_ref=copy_ref,
    )
    with pytest.raises(SessionError, match="needs an inspected quality receipt"):
        bridge.prepare(
            call_id=call["id"], call_version=1, phase="collect", messages=MESSAGES,
            sources=(SourceSelection(copy_ref, "permitted", True),),
        )
    assert store.records("intake_quality") == []
    assert store.records("model_request_intent") == []
    assert captured == []


def test_old_nonplain_raw_intent_stops_before_dispatch_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    copy_ref = _raw_copy(StateStore(tmp_path), route="synthetic-fixture", mime="text/html")
    store, _registry, bridge, call, config, _copy, captured = setup(
        tmp_path, source_ref=copy_ref,
    )
    preview = bridge.preview_request(
        call=call, phase="collect", messages=MESSAGES,
        sources=(SourceSelection(copy_ref, "permitted", True),),
    )
    intent_ref = _seed_old_intent(store, call, config, preview, copy_ref)
    with pytest.raises(SessionError, match="needs an inspected quality receipt"):
        bridge.invoke_capture(intent_ref)
    assert store.records("model_dispatch_attempt") == []
    assert store.records("intake_quality") == []
    assert captured == []


def _seed_old_intent(store, call, config, preview, copy_ref, *, dependencies=()):
    """Represent a previously saved request without adding a new quality field."""
    blob = store.put_blob(canonical(preview.body).encode())
    body = {
        "schema": "ora.model_request_intent.v1",
        "mission_id": "mission", "task_id": "task", "attempt_id": "attempt-1",
        "instance_id": call["body"]["instance_id"],
        "call_ref": ref(call), "configuration_ref": ref(config),
        "configuration_sha256": config["body"]["configuration_sha256"],
        "request_sha256": preview.request_sha256, "blob": blob,
        "effective_reasoning_effort": preview.body["reasoning_effort"],
        "reasoning_effort_basis": preview.manifest()["reasoning_effort_basis"],
        "phase": "collect", "parent_call_ref": None,
        "source_refs": [copy_ref] if not dependencies else [],
        "context_source_dependency_refs": list(dependencies),
        "evidence_refs": list(preview.evidence_refs),
        "source_decisions": ([{
            "copy_ref": copy_ref, "use": "permitted", "allowed_for_model": True,
            "route": "direct", "unknown_policy": "reject", "passage_refs": [],
        }] if not dependencies else []),
    }
    intent_id = f"model-intent:{call['id']}:1"
    saved = store.transact(
        f"seed-old-intent:{intent_id}", body,
        lambda tx: tx.put("model_request_intent", intent_id, body),
        mission_id="mission",
    )
    return ref(saved)


def test_old_pinned_intent_attests_at_dispatch_without_rewriting_request(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    copy_ref = _raw_copy(StateStore(tmp_path))
    store, _registry, bridge, call, config, _copy, captured = setup(
        tmp_path, source_ref=copy_ref,
    )
    preview = bridge.preview_request(
        call=call, phase="collect", messages=MESSAGES,
        sources=(SourceSelection(copy_ref, "permitted", True),),
    )
    intent_ref = _seed_old_intent(store, call, config, preview, copy_ref)
    old_intent = store.get(**intent_ref)
    assert store.records("intake_quality") == []
    assert bridge.load(intent_ref).request.request_sha256 == preview.request_sha256
    assert store.records("intake_quality") == []
    assert bridge.load(intent_ref, for_dispatch=True).request.request_sha256 == preview.request_sha256
    assert store.records("intake_quality") == []
    bridge.invoke_capture(intent_ref)
    assert len(captured) == 1
    assert captured[0][0] == preview.body
    assert json.loads(captured[0][0]["messages"][1]["content"])["source_packets"][0][
        "text"
    ] == TEXT
    assert store.get(**intent_ref) == old_intent
    assert len(store.records("intake_quality")) == 1
    assert store.records("source_use") == []
    assert len(store.records("model_dispatch_attempt")) == 1


def test_captured_old_call_returns_receipt_without_retroactive_quality(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    copy_ref = _raw_copy(StateStore(tmp_path))
    store, _registry, bridge, call, config, _copy, captured = setup(
        tmp_path, source_ref=copy_ref,
    )
    preview = bridge.preview_request(
        call=call, phase="collect", messages=MESSAGES,
        sources=(SourceSelection(copy_ref, "permitted", True),),
    )
    intent_ref = _seed_old_intent(store, call, config, preview, copy_ref)
    receipt = store.transact(
        "seed-old-captured-response", {},
        lambda tx: tx.put("model_response_receipt", f"model-response:{intent_ref['id']}", {
            "intent_ref": intent_ref, "request_sha256": preview.request_sha256,
            "status": "complete", "usage": {"total_tokens": 1},
        }),
        mission_id="mission",
    )
    assert bridge.invoke_capture(intent_ref) == ref(receipt)
    assert store.records("intake_quality") == []
    assert store.records("model_dispatch_attempt") == []
    assert captured == []


def test_derived_legacy_report_text_attests_exact_source_without_new_use(tmp_path):
    store, bridge, intent_ref, copy_ref, captured = _prepared_report(
        tmp_path, source_linked=True, legacy_supplied=True,
    )
    intent = store.get(**intent_ref)
    assert intent["body"]["source_refs"] == []
    assert intent["body"]["context_source_dependency_refs"] == [copy_ref]
    quality = store.records("intake_quality")
    assert len(quality) == 1
    assert quality[0]["body"]["copy_ref"] == copy_ref
    assert store.resolve("source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}") is None
    assert captured == []
    assert bridge.load(intent_ref, for_dispatch=True).request.request_sha256 == intent[
        "body"
    ]["request_sha256"]


@pytest.mark.parametrize("defect", ["missing", "wrong_hash"])
def test_malformed_present_quality_cannot_be_replaced_by_local_receipt(
    tmp_path, defect,
):
    copy_ref = _raw_copy(StateStore(tmp_path))
    store, _registry, bridge, call, _config, _copy, captured = setup(
        tmp_path, source_ref=copy_ref,
    )

    def malformed(tx):
        if defect == "wrong_hash":
            quality = tx.put("intake_quality", "wrong-hash-quality", {
                "copy_ref": copy_ref, "source_sha256": "0" * 64,
                "parser": "historical-local-parser", "parser_version": "historical-v1",
            })
            quality_ref = ref(quality)
        else:
            quality_ref = {"kind": "intake_quality", "id": "missing-quality", "version": 1}
        use = tx.put("source_use", "malformed-current-quality", {
            "copy_ref": copy_ref, "policy_status": "granted",
            "allowed_uses": ["model_input"],
            "quality_ref": quality_ref,
        })
        tx.bind("source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}",
                "source_use", use["id"])
        return ref(use)

    store.transact("malformed-quality-policy", {}, malformed, mission_id="mission")
    with pytest.raises(SessionError, match="quality is malformed"):
        bridge.prepare(
            call_id=call["id"], call_version=1, phase="collect", messages=MESSAGES,
            sources=(SourceSelection(copy_ref, "permitted", True),),
        )
    assert len(store.records("intake_quality")) == (1 if defect == "wrong_hash" else 0)
    assert store.records("model_request_intent") == []
    assert captured == []


@pytest.mark.parametrize("bad_field", ["byte_length", "classification"])
def test_bound_quality_wrong_field_type_cannot_be_supplemented(tmp_path, bad_field):
    copy_ref = _raw_copy(StateStore(tmp_path), text="x")
    store, _registry, bridge, call, _config, _copy, captured = setup(
        tmp_path, source_ref=copy_ref,
    )

    def seed(tx):
        quality_body = {
            "copy_ref": copy_ref, "source_sha256": hashlib.sha256(b"x").hexdigest(),
            "parser": "historical-local-parser", "parser_version": "historical-v1",
            "byte_length": 1, "classification": "passage",
        }
        quality_body[bad_field] = True
        quality = tx.put("intake_quality", "wrong-type-quality", quality_body)
        use = tx.put("source_use", "wrong-type-use", {
            "copy_ref": copy_ref, "policy_status": "granted",
            "allowed_uses": ["model_input"], "quality_ref": ref(quality),
        })
        tx.bind("source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}",
                "source_use", use["id"])
        return ref(use)

    store.transact("seed-wrong-type-quality", {}, seed, mission_id="mission")
    with pytest.raises(SessionError, match="invalid inspection metadata"):
        bridge.prepare(
            call_id=call["id"], call_version=1, phase="collect", messages=MESSAGES,
            sources=(SourceSelection(copy_ref, "permitted", True),),
        )
    assert len(store.records("intake_quality")) == 1
    assert store.records("model_request_intent") == []
    assert captured == []


def test_policy_change_at_dispatch_marker_blocks_transport(
    tmp_path, monkeypatch,
):
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    copy_ref = _raw_copy(StateStore(tmp_path))
    store, _registry, bridge, call, _config, _copy, captured = setup(
        tmp_path, source_ref=copy_ref,
    )
    prepared = bridge.prepare(
        call_id=call["id"], call_version=1, phase="collect", messages=MESSAGES,
        sources=(SourceSelection(copy_ref, "permitted", True),),
    )
    original_transact = store.transact
    changed = False

    def transact(operation_id, request, action, **kwargs):
        nonlocal changed
        if operation_id.startswith("model-dispatch-mark:") and not changed:
            changed = True

            def revoke(tx):
                use = tx.put("source_use", "late-restricted-use", {
                    "copy_ref": copy_ref, "policy_status": "restricted",
                    "allowed_uses": [],
                })
                tx.bind("source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}",
                        "source_use", use["id"])
                return ref(use)

            original_transact("late-restricted-use", {}, revoke, mission_id="mission")
        return original_transact(operation_id, request, action, **kwargs)

    monkeypatch.setattr(store, "transact", transact)
    with pytest.raises(SessionError, match="revoked model input"):
        bridge.invoke_capture(prepared.intent_ref)
    assert changed
    assert captured == []
    assert store.records("model_dispatch_attempt") == []
    assert len(store.records("intake_quality")) == 1
