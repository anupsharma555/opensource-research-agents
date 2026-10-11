"""Saved-call model bridge fixtures use the real configuration and source store."""

from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from pathlib import Path

import pytest

from ora.agents import COMMON_TOOL_IDS, AgentRegistry, CapabilityCatalog, ConfigurationRegistry
from ora.agents.capabilities import content_hash
from ora.coordination.traces import TraceJournal
from ora.evidence import EvidenceLibrary
from ora.models import (
    DispatchUncertain,
    GroqSelectedAdapter,
    ModelSession,
    SessionError,
    SourceSelection,
)
from ora.state import StateConflict, StateStore

ROOT = Path(__file__).resolve().parents[2]
MODEL = "openai/gpt-oss-120b"
URL = "https://api.groq.com/openai/v1/chat/completions"
SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}},
          "required": ["answer"], "additionalProperties": False}


class FakeResponse:
    def __init__(self, raw: bytes):
        self.raw = raw
        self.headers = {"x-request-id": "request-fixture", "private": "ignore"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit):
        return self.raw


def response(*, reasoning: str | None = "PRIVATE_REASONING_SENTINEL") -> bytes:
    message = {"role": "assistant", "content": None,
               "tool_calls": [{"id": "tool-1", "type": "function", "function": {
                   "name": "evidence_lookup", "arguments": '{"source_id":"synthetic"}',
               }}]}
    if reasoning is not None:
        message["reasoning"] = reasoning
    return json.dumps({
        "id": "chat-fixture", "x_groq": {"id": "provider-request-fixture"},
        "model": MODEL, "choices": [{"message": message, "finish_reason": "tool_calls"}],
        "usage": {"prompt_tokens": 10, "completion_tokens": 20, "total_tokens": 30},
    }).encode()


def source_copy(store: StateStore) -> dict:
    library = EvidenceLibrary(store)
    publication = library.register_publication(
        "publication", identifiers={"doi": "10.1234/synthetic.adapter"},
        title="Synthetic abstract", verification="Fixture identifier printed in source",
    )["publication_ref"]
    encounter = library.record_encounter(
        "encounter", publication_ref=publication, retrieval_hit_ref=None,
        status="accepted", access_surface="abstract",
        retrieved_at="2026-01-02T00:00:00Z", route="synthetic-fixture",
    )["encounter_ref"]
    text = "Synthetic abstract reports a two-arm comparison."
    result = library.add_copy(
        "copy", publication, encounter, content=text.encode(), content_sha256=None,
        mime_type="text/plain", source_version="synthetic-v1",
        access_surface="abstract", retrieved_at="2026-01-02T00:00:00Z",
        rights_basis="Synthetic project-owned fixture", retain_bytes=True,
        reference="simulated://adapter-abstract", passages=[],
        inspection_state="checked", validation_record="fixture-offset-check",
    )
    return result["copy_ref"]


def setup(tmp_path: Path, *, raw: bytes | None = None,
          source_ref: dict | None = None, unknown_policy: str = "reject",
          tool_description: str = "", credential_settings: dict | None = None):
    store = StateStore(tmp_path)
    copy_ref = source_ref or source_copy(store)
    copy_record = store.get(**copy_ref)
    actor = AgentRegistry(store, ROOT).register(
        "actor", mission_id="mission", run_id="run", role_id="literature_acquisition",
        display_name="Synthetic worker",
    )
    catalog = CapabilityCatalog.with_common()
    if tool_description:
        catalog.tools["evidence_lookup"] = replace(
            catalog.tools["evidence_lookup"], description=tool_description,
        )
    registry = ConfigurationRegistry(store, ROOT, catalog)
    config = registry.save_configuration(
        "config", instance_id=actor["id"], mission_id="mission",
        task_scope="source-comparison", provider="groq", endpoint=URL, model_id=MODEL,
        settings={"max_completion_tokens": 8192, "stream": False,
                  **(credential_settings or {}),
                  "unknown_source_policy": unknown_policy,
                  "response_format": {"type": "json_schema", "json_schema": {
                      "name": "answer", "strict": True, "schema": SCHEMA,
                  }}},
        reasoning_settings={"reasoning_effort": "high", "include_reasoning": True},
        instructions="Use only saved synthetic evidence.", skill_choices={},
        grants={f"common:{name}" for name in COMMON_TOOL_IDS},
        input_refs=[{"kind": "source_copy", "id": copy_ref["id"],
                     "version": copy_ref["version"],
                     "sha256": content_hash(copy_record["body"])}],
    )
    call = registry.start_call(
        "call-op", call_id="call-1", configuration_id=config["id"],
        mission_id="mission", task_id="task", attempt_id="attempt-1",
        loaded_skill_ids=[], exposed_tool_ids=["evidence_lookup"],
        require_common_tools=False,
    )
    captured = []

    def transport(request, timeout):
        captured.append((json.loads(request.data), timeout))
        return FakeResponse(raw or response())

    adapter = GroqSelectedAdapter(ROOT, transport=transport)
    bridge = ModelSession(store, ROOT, adapter=adapter)
    return store, registry, bridge, call, config, copy_ref, captured


def select(copy_ref: dict, route: str = "direct") -> tuple[SourceSelection, ...]:
    return (SourceSelection(copy_ref, "permitted", True, route),)


def _preview_task(store: StateStore) -> None:
    store.transact(
        "preview-task", {"task_id": "task"},
        lambda tx: tx.put("board_proposal", "task", {"mission_id": "mission", "status": "open"}),
        mission_id="mission",
    )


def _preview_call(registry: ConfigurationRegistry, config: dict) -> dict:
    return registry.preview_call(
        call_id="call-preview", configuration_id=config["id"], mission_id="mission",
        task_id="task", attempt_id="attempt-preview", loaded_skill_ids=[],
        exposed_tool_ids=["evidence_lookup"], require_common_tools=False,
    )


def test_provisional_call_and_request_match_durable_preparation_without_writes(
    tmp_path: Path,
) -> None:
    store, registry, bridge, _call, config, copy_ref, captured = setup(tmp_path)
    _preview_task(store)
    before_events = len(store.events("mission"))
    before_calls = len(store.records("agent_call"))
    before_objects = {p.relative_to(tmp_path) for p in (tmp_path / "objects").rglob("*")
                      if p.is_file()}
    provisional = _preview_call(registry, config)
    messages = [{"role": "user", "content": "Inspect the saved abstract"}]
    preview = bridge.preview_request(
        call=provisional, phase="collect", messages=messages, sources=select(copy_ref),
    )
    assert len(store.events("mission")) == before_events
    assert len(store.records("agent_call")) == before_calls
    assert store.records("model_request_intent") == []
    assert {p.relative_to(tmp_path) for p in (tmp_path / "objects").rglob("*")
            if p.is_file()} == before_objects
    assert captured == []

    saved = registry.start_call(
        "preview-call-op", call_id="call-preview", configuration_id=config["id"],
        mission_id="mission", task_id="task", attempt_id="attempt-preview",
        loaded_skill_ids=[], exposed_tool_ids=["evidence_lookup"],
        require_common_tools=False,
    )
    assert provisional["body"] == saved["body"]
    after_start_events = len(store.events("mission"))
    replay = registry.start_call(
        "preview-call-op", call_id="call-preview", configuration_id=config["id"],
        mission_id="mission", task_id="task", attempt_id="attempt-preview",
        loaded_skill_ids=[], exposed_tool_ids=["evidence_lookup"],
        require_common_tools=False,
    )
    assert replay == saved and len(store.events("mission")) == after_start_events
    after_start_objects = {p.relative_to(tmp_path) for p in (tmp_path / "objects").rglob("*")
                           if p.is_file()}
    saved_preview = bridge.preview_request(
        call=saved, phase="collect", messages=messages, sources=select(copy_ref),
    )
    assert saved_preview.body == preview.body
    assert saved_preview.request_sha256 == preview.request_sha256
    assert len(store.events("mission")) == after_start_events
    assert store.records("model_request_intent") == []
    assert {p.relative_to(tmp_path) for p in (tmp_path / "objects").rglob("*")
            if p.is_file()} == after_start_objects
    actual = bridge.prepare(
        call_id=saved["id"], call_version=1, phase="collect", messages=messages,
        sources=select(copy_ref),
    )
    assert preview.body == actual.request.body
    assert preview.request_sha256 == actual.request.request_sha256


def test_provisional_request_rechecks_manifest_task_and_current_source_use(
    tmp_path: Path,
) -> None:
    store, registry, bridge, _call, config, copy_ref, _captured = setup(tmp_path)
    with pytest.raises(ValueError, match="Saved task"):
        _preview_call(registry, config)
    _preview_task(store)
    provisional = _preview_call(registry, config)
    with pytest.raises(ValueError, match="Saved configuration"):
        registry.preview_call(
            call_id="call-other", configuration_id=config["id"], mission_id="other-mission",
            task_id="task", attempt_id="attempt-other", loaded_skill_ids=[],
            exposed_tool_ids=["evidence_lookup"], require_common_tools=False,
        )
    with pytest.raises(ValueError, match="unselected capability"):
        registry.preview_call(
            call_id="call-other", configuration_id=config["id"], mission_id="mission",
            task_id="task", attempt_id="attempt-other", loaded_skill_ids=[],
            exposed_tool_ids=["not-granted"], require_common_tools=False,
        )
    altered = json.loads(json.dumps(provisional))
    altered["body"]["exposed_tools"] = []
    with pytest.raises(SessionError, match="canonical configuration"):
        bridge.preview_request(call=altered, phase="collect", messages=[],
                               sources=select(copy_ref))
    altered_hash = json.loads(json.dumps(provisional))
    altered_hash["body"]["configuration_sha256"] = "0" * 64
    with pytest.raises(SessionError, match="integrity failed"):
        bridge.preview_request(call=altered_hash, phase="collect", messages=[],
                               sources=select(copy_ref))
    restricted = dict(store.get(**copy_ref)["body"], rights_status="restricted")
    store.transact("revoke-preview-copy", {}, lambda tx: tx.put(
        "source_copy", copy_ref["id"], restricted, expected_version=copy_ref["version"],
    ))
    with pytest.raises(SessionError, match="revoked model input"):
        bridge.preview_request(call=provisional, phase="collect", messages=[],
                               sources=select(copy_ref))


def test_saved_tool_help_reaches_model_and_cannot_change_after_selection(tmp_path: Path) -> None:
    explanation = "Read a saved source by its returned ID; a publication PMID is not that ID."
    store, _registry, bridge, call, _config, copy_ref, _captured = setup(
        tmp_path, tool_description=explanation,
    )
    prepared = bridge.prepare(
        call_id=call["id"], call_version=1, phase="collect",
        messages=[{"role": "user", "content": "Inspect the saved abstract"}],
        sources=select(copy_ref),
    )
    assert prepared.request.body["tools"][0]["function"]["description"] == explanation
    body = json.loads(json.dumps(call["body"]))
    body["permitted_tools"]["evidence_lookup"]["description"] = "Changed instructions"
    store.transact("changed-description", {}, lambda tx: tx.put(
        "agent_call", call["id"], body, expected_version=1,
    ))
    with pytest.raises(SessionError, match="descriptor changed"):
        bridge.prepare(call_id=call["id"], call_version=2, phase="collect",
                       messages=[], sources=select(copy_ref))


def test_saved_selection_abstract_request_and_private_capture(tmp_path: Path,
                                                              monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    raw = json.loads(response())
    raw["usage"].update({"prompt_tokens_details": {"cached_tokens": 4},
                         "completion_tokens_details": {"reasoning_tokens": 12}})
    store, _registry, bridge, call, config, copy_ref, captured = setup(
        tmp_path, raw=json.dumps(raw).encode(),
    )
    prepared = bridge.prepare(
        call_id=call["id"], call_version=call["version"], phase="collect",
        messages=[{"role": "user", "content": "Review abstract"}], sources=select(copy_ref),
    )
    assert prepared.request.body["model"] == config["body"]["model_id"]
    assert prepared.request.body["reasoning_effort"] == "high"
    assert prepared.request.body["include_reasoning"] is True
    assert prepared.request.body["max_completion_tokens"] == 8192
    assert "Synthetic abstract reports" in json.dumps(prepared.request.body)
    intent = store.get(**prepared.intent_ref)
    assert intent["body"]["call_ref"] == {"kind": "agent_call", "id": "call-1", "version": 1}
    assert intent["body"]["configuration_ref"] == {"kind": "agent_configuration",
                                                  "id": config["id"], "version": 1}
    assert intent["body"]["request_sha256"] == prepared.request.request_sha256
    receipt_ref = bridge.invoke_capture(prepared.intent_ref)
    receipt = store.get(**receipt_ref)
    assert receipt["body"]["status"] == "tool_requested"
    assert receipt["body"]["request_sha256"] == prepared.request.request_sha256
    assert receipt["body"]["provider_request_id"] == "provider-request-fixture"
    assert receipt["body"]["usage"]["total_tokens"] == 30
    assert receipt["body"]["usage"]["prompt_tokens_details"] == {"cached_tokens": 4}
    assert receipt["body"]["usage"]["completion_tokens_details"] == {"reasoning_tokens": 12}
    assert captured[0][1] == 120 and len(captured) == 1
    assert "PRIVATE_REASONING_SENTINEL" not in json.dumps(receipt)
    payload = store.read_blob(receipt["body"]["blob"]["sha256"]).decode()
    assert "PRIVATE_REASONING_SENTINEL" not in payload
    assert "tool-1" in payload
    journal = TraceJournal(store)
    visible = journal.export("mission")
    assert "PRIVATE_REASONING_SENTINEL" not in json.dumps(visible)
    assert any(item["data"]["output_refs"] == [receipt_ref]
               and item["data"]["call_id"] == "call-1"
               and item["data"]["call_version"] == 1
               and item["data"]["configuration_ref"] == intent["body"]["configuration_ref"]
               and copy_ref in item["data"]["input_refs"]
               for item in visible["records"])
    reasoning = journal.read_reasoning(receipt["body"]["reasoning_ref"],
                                       mission_id="mission", diagnostic_granted=True)
    assert reasoning["text"] == "PRIVATE_REASONING_SENTINEL"
    assert bridge.invoke_capture(prepared.intent_ref) == receipt_ref
    assert len(captured) == 1
    marker = store.records("model_dispatch_attempt")[0]
    assert marker["body"]["status"] == "captured"
    assert marker["body"]["receipt_ref"] == receipt_ref
    resumed = ModelSession(StateStore(tmp_path), ROOT, adapter=bridge.adapter)
    assert resumed.load(prepared.intent_ref).request.body == prepared.request.body
    assert resumed.store.get(**receipt_ref)["body"]["usage"] == receipt["body"]["usage"]


@pytest.mark.parametrize("route", ["direct", "cache", "peer"])
def test_separate_source_policy_revoked_before_marker_cannot_reach_provider(tmp_path, monkeypatch, route):
    store, _registry, bridge, call, _config, copy_ref, captured = setup(tmp_path)
    policy_body = {"copy_ref": copy_ref, "rights_status": "permitted", "allowed_uses": ["model_input"]}

    def grant(tx):
        policy = tx.put("source_use", "policy", policy_body)
        tx.bind("source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}", "source_use", policy["id"])
        return policy

    store.transact("grant-use", {}, grant)
    prepared = bridge.prepare(call_id=call["id"], call_version=1, phase="collect",
                              messages=[{"role": "user", "content": "Read the abstract"}],
                              sources=select(copy_ref, route))
    original = store.transact

    def revoke_before_marker(operation_id, request, action, **kwargs):
        if operation_id.startswith("model-dispatch-mark:"):
            original("revoke-policy", {}, lambda tx: tx.put(
                "source_use", "policy", {**policy_body, "allowed_uses": ["discovery"]},
                expected_version=1))
        return original(operation_id, request, action, **kwargs)

    monkeypatch.setattr(store, "transact", revoke_before_marker)
    with pytest.raises(SessionError, match="source-use policy revoked"):
        bridge.invoke_capture(prepared.intent_ref)
    assert captured == []
    assert store.records("model_dispatch_attempt") == []
    assert bridge.load(prepared.intent_ref).request.request_sha256 == prepared.request.request_sha256


def test_checked_xml_passage_is_model_input_with_original_copy_lineage(tmp_path, monkeypatch):
    from ora.evidence.intake import IntakeService

    store = StateStore(tmp_path)
    library = EvidenceLibrary(store)
    publication = library.register_publication(
        "xml-publication", identifiers={"doi": "10.1234/synthetic.xml"},
        title="A fixture article", verification="Project-owned source identifier",
    )["publication_ref"]
    content = (b'<article><article-id pub-id-type="doi">10.1234/synthetic.xml</article-id>'
               b'<abstract><p>Forty participants were assigned to each group.</p></abstract></article>')
    inspected = IntakeService(library).inspect_response(
        "xml-intake", publication, content=content, content_type="application/xml",
        source_url="https://example.org/fixture.xml", retrieved_at="2026-01-02T00:00:00Z",
        source_version="fixture-v1", rights_status="permitted", rights_basis="Project-owned fixture",
        declared_surface="abstract", expected_doi="10.1234/synthetic.xml",
        selected_texts=["Forty participants were assigned to each group."],
    )
    store, registry, bridge, call, config, copy_ref, captured = setup(
        tmp_path, source_ref=inspected["copy_ref"],
    )
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    _preview_task(store)
    selection = (SourceSelection(copy_ref, "permitted", True,
                                 passage_refs=tuple(inspected["passage_refs"])),)
    preview = bridge.preview_request(
        call=_preview_call(registry, config), phase="collect",
        messages=[{"role": "user", "content": "Explain the reported group sizes."}],
        sources=selection,
    )
    prepared = bridge.prepare(
        call_id=call["id"], call_version=1, phase="collect",
        messages=[{"role": "user", "content": "Explain the reported group sizes."}],
        sources=selection,
    )
    assert preview.body == prepared.request.body
    assert preview.request_sha256 == prepared.request.request_sha256
    packet = json.loads(prepared.request.body["messages"][1]["content"])["source_packets"][0]
    assert packet["id"] == copy_ref["id"] and packet["version"] == copy_ref["version"]
    assert packet["copy_sha256"] == hashlib.sha256(content).hexdigest()
    assert packet["passage_refs"] == inspected["passage_refs"]
    assert packet["sha256"] == hashlib.sha256(packet["text"].encode()).hexdigest()
    assert "Forty participants" in packet["text"] and "<article>" not in packet["text"]
    receipt = bridge.invoke_capture(prepared.intent_ref)
    assert len(captured) == 1
    assert store.get(**receipt)["body"]["intent_ref"] == prepared.intent_ref
    marker = store.records("model_dispatch_attempt")[0]
    assert marker["body"]["source_use_refs"] == [inspected["source_use_ref"]]


def test_direct_and_peer_route_use_same_saved_request_boundary(tmp_path: Path,
                                                               monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    store, registry, bridge, call, config, copy_ref, _captured = setup(tmp_path)
    direct = bridge.prepare(call_id=call["id"], call_version=1, phase="collect",
                            messages=[{"role": "user", "content": "Review abstract"}],
                            sources=select(copy_ref, "direct"))
    second = registry.start_call(
        "call-two-op", call_id="call-2", configuration_id=config["id"],
        mission_id="mission", task_id="task", attempt_id="attempt-2",
        loaded_skill_ids=[], exposed_tool_ids=["evidence_lookup"],
        require_common_tools=False,
    )
    via_peer = bridge.prepare(call_id=second["id"], call_version=1, phase="collect",
                              messages=[{"role": "user", "content": "Review abstract"}],
                              sources=select(copy_ref, "peer"), parent_call_ref={
                                  "kind": "agent_call", "id": call["id"], "version": 1,
                              })
    assert direct.request.body == via_peer.request.body
    assert direct.request.request_sha256 == via_peer.request.request_sha256
    assert store.get(**via_peer.intent_ref)["body"]["source_decisions"][0]["route"] == "peer"
    assert store.get(**via_peer.intent_ref)["body"]["parent_call_ref"]["id"] == call["id"]
    bridge.invoke_capture(direct.intent_ref)
    peer_receipt = bridge.invoke_capture(via_peer.intent_ref)
    assert store.get(**peer_receipt)["body"]["configuration_ref"] == \
        store.get(**direct.intent_ref)["body"]["configuration_ref"]
    traces = TraceJournal(store).export("mission")["records"]
    child = next(item["data"] for item in traces if item["data"]["call_id"] == "call-2")
    assert child["parent_operation_id"] == "model-call:call-1:1"
    assert child["lineage_status"] == "resolved"


def test_final_schema_is_derived_from_saved_selection(tmp_path: Path) -> None:
    _store, _registry, bridge, call, _config, copy_ref, _captured = setup(tmp_path)
    prepared = bridge.prepare(call_id=call["id"], call_version=1, phase="final",
                              messages=[{"role": "user", "content": "Summarize"}],
                              sources=select(copy_ref))
    assert prepared.request.body["response_format"]["json_schema"]["schema"] == SCHEMA
    assert "tools" not in prepared.request.body
    assert prepared.request.body["stream"] is False


@pytest.mark.parametrize("route", ["direct", "cache", "peer"])
def test_explicitly_denied_copy_cannot_be_overridden(tmp_path: Path, route: str) -> None:
    store = StateStore(tmp_path)
    original = source_copy(store)
    old = store.get(**original)
    denied = store.transact("deny", {}, lambda tx: tx.put(
        "source_copy", original["id"], {**old["body"], "allow_model_input": False},
        expected_version=1))
    _store, _registry, bridge, call, _config, _copy_ref, _captured = setup(
        tmp_path, source_ref={"kind": "source_copy", "id": denied["id"],
                              "version": denied["version"]},
    )
    with pytest.raises(SessionError, match="prohibits model input"):
        bridge.prepare(call_id=call["id"], call_version=1, phase="collect",
                       messages=[{"role": "user", "content": "Read cached copy"}],
                       sources=select({"kind": "source_copy", "id": denied["id"],
                                       "version": denied["version"]}, route))


def test_wrong_source_version_and_changed_messages_are_rejected(tmp_path: Path) -> None:
    store, _registry, bridge, call, _config, copy_ref, _captured = setup(tmp_path)
    old = store.get(**copy_ref)
    next_version = store.transact("source-update", {}, lambda tx: tx.put(
        "source_copy", copy_ref["id"], {**old["body"], "source_version": "synthetic-v2"},
        expected_version=1))
    with pytest.raises(SessionError, match="missing, duplicated or changed"):
        bridge.prepare(call_id=call["id"], call_version=1, phase="collect",
                       messages=[{"role": "user", "content": "Review abstract"}],
                       sources=select({"kind": "source_copy", "id": next_version["id"],
                                       "version": 2}))
    first = bridge.prepare(call_id=call["id"], call_version=1, phase="collect",
                           messages=[{"role": "user", "content": "Review abstract"}],
                           sources=select(copy_ref))
    with pytest.raises(StateConflict):
        bridge.prepare(call_id=call["id"], call_version=1, phase="collect",
                       messages=[{"role": "user", "content": "Changed request"}],
                       sources=select(copy_ref))
    assert store.get(**first.intent_ref)["body"]["request_sha256"] == first.request.request_sha256


def test_saved_call_setting_drift_is_rejected(tmp_path: Path) -> None:
    store, _registry, bridge, call, _config, copy_ref, _captured = setup(tmp_path)
    drifted = store.transact("drift", {}, lambda tx: tx.put(
        "agent_call", call["id"],
        {**call["body"], "settings": {**call["body"]["settings"],
                                       "max_completion_tokens": 1024}},
        expected_version=1), mission_id="mission")
    with pytest.raises(SessionError, match="selection differ"):
        bridge.prepare(call_id=call["id"], call_version=drifted["version"],
                       phase="collect", messages=[{"role": "user", "content": "Review"}],
                       sources=select(copy_ref))


def test_frozen_copy_survives_new_version_but_explicit_revocation_blocks_dispatch(
    tmp_path: Path,
) -> None:
    store, _registry, bridge, call, _config, copy_ref, _captured = setup(tmp_path)
    prepared = bridge.prepare(call_id=call["id"], call_version=1, phase="collect",
                              messages=[{"role": "user", "content": "Review abstract"}],
                              sources=select(copy_ref))
    old = store.get(**copy_ref)
    newer = store.transact("correction", {}, lambda tx: tx.put(
        "source_copy", copy_ref["id"], {**old["body"], "source_version": "synthetic-v2"},
        expected_version=1))
    assert bridge.load(prepared.intent_ref, for_dispatch=True).request.body == prepared.request.body
    store.transact("revoke-model-use", {}, lambda tx: tx.put(
        "source_copy", copy_ref["id"],
        {**newer["body"], "allow_model_input": False}, expected_version=2))
    with pytest.raises(SessionError, match="revoked model input"):
        bridge.load(prepared.intent_ref, for_dispatch=True)
    # Historical reconstruction remains available to capture an in-flight reply.
    assert bridge.load(prepared.intent_ref).request.request_sha256 == prepared.request.request_sha256


def test_revocation_before_dispatch_marker_prevents_provider_call(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    store, _registry, bridge, call, _config, copy_ref, captured = setup(tmp_path)
    prepared = bridge.prepare(call_id=call["id"], call_version=1, phase="collect",
                              messages=[{"role": "user", "content": "Review abstract"}],
                              sources=select(copy_ref))
    assert bridge.load(prepared.intent_ref, for_dispatch=True).request == prepared.request
    original_transact = store.transact
    revoked = []

    def revoke_before_marker(operation_id, request, action, **kwargs):
        if operation_id.startswith("model-dispatch-mark:"):
            old = store.get("source_copy", copy_ref["id"])
            original_transact("revoke-before-marker", {}, lambda tx: tx.put(
                "source_copy", copy_ref["id"],
                {**old["body"], "allow_model_input": False},
                expected_version=old["version"]))
            revoked.append(True)
        return original_transact(operation_id, request, action, **kwargs)

    monkeypatch.setattr(store, "transact", revoke_before_marker)
    with pytest.raises(SessionError, match="revoked model input"):
        bridge.invoke_capture(prepared.intent_ref)
    assert revoked == [True]
    assert captured == []
    assert store.records("model_dispatch_attempt") == []
    assert store.records("model_response_receipt") == []


def test_nonrestrictive_new_head_dispatches_and_later_revocation_keeps_response(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    store, _registry, bridge, call, _config, copy_ref, _captured = setup(tmp_path)
    prepared = bridge.prepare(call_id=call["id"], call_version=1, phase="collect",
                              messages=[{"role": "user", "content": "Review abstract"}],
                              sources=select(copy_ref))
    old = store.get(**copy_ref)
    store.transact("newer-nonrestrictive-copy", {}, lambda tx: tx.put(
        "source_copy", copy_ref["id"],
        {**old["body"], "source_version": "synthetic-v2"}, expected_version=1))
    observed = []

    def revoke_after_marker(_request, timeout):
        marker = store.get("model_dispatch_attempt",
                           f"model-dispatch:{prepared.intent_ref['id']}")
        observed.append((timeout, marker["body"]["status"]))
        current = store.get("source_copy", copy_ref["id"])
        store.transact("revoke-after-marker", {}, lambda tx: tx.put(
            "source_copy", copy_ref["id"],
            {**current["body"], "allow_model_input": False},
            expected_version=current["version"]))
        return FakeResponse(response())

    bridge.adapter = GroqSelectedAdapter(ROOT, transport=revoke_after_marker)
    receipt_ref = bridge.invoke_capture(prepared.intent_ref)
    receipt = store.get(**receipt_ref)
    assert observed == [(120, "attempted_external_effect")]
    assert receipt["body"]["status"] == "tool_requested"
    assert store.get(**prepared.intent_ref)["body"]["source_refs"] == [copy_ref]
    assert store.get("source_copy", copy_ref["id"])["version"] == 3
    assert bridge.invoke_capture(prepared.intent_ref) == receipt_ref
    assert len(observed) == 1  # A captured response replays despite later revocation.
    with pytest.raises(SessionError, match="revoked model input"):
        bridge.load(prepared.intent_ref, for_dispatch=True)


def test_unknown_metadata_policy_is_explicit_and_never_sends_copy_bytes(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    content = "UNKNOWN_SOURCE_BYTES"
    checksum = hashlib.sha256(content.encode()).hexdigest()
    metadata = store.transact("metadata", {}, lambda tx: tx.put(
        "source_copy", "metadata-copy", {"retain_bytes": False, "blob": None,
                                         "content_sha256": checksum,
                                         "access_surface": "metadata",
                                         "rights_basis": "Unconfirmed model-use rights"}))
    copy_ref = {"kind": "source_copy", "id": metadata["id"], "version": 1}
    _store, _registry, bridge, call, _config, _, _ = setup(
        tmp_path, source_ref=copy_ref, unknown_policy="allow_metadata_only",
    )
    prepared = bridge.prepare(call_id=call["id"], call_version=1, phase="collect",
                              messages=[{"role": "user", "content": "Inspect metadata"}],
                              sources=(SourceSelection(copy_ref, "unknown", False, "cache"),))
    assert content not in json.dumps(prepared.request.body)
    assert prepared.request.body["messages"][1]["content"].find(checksum) >= 0


def test_foreign_mission_copy_is_rejected_before_request_intent(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    text = b"FOREIGN_SOURCE_BYTES"
    blob = store.put_blob(text)
    copy = store.transact("foreign-copy", {}, lambda tx: tx.put(
        "source_copy", "foreign-copy", {"mission_id": "another-mission",
                                       "retain_bytes": True, "blob": blob,
                                       "content_sha256": hashlib.sha256(text).hexdigest(),
                                       "mime_type": "text/plain", "access_surface": "abstract"}),
        mission_id="another-mission")
    copy_ref = {"kind": "source_copy", "id": copy["id"], "version": 1}
    _store, _registry, bridge, call, _config, _, _ = setup(tmp_path, source_ref=copy_ref)
    with pytest.raises(SessionError, match="another mission"):
        bridge.prepare(call_id=call["id"], call_version=1, phase="collect",
                       messages=[{"role": "user", "content": "Review"}],
                       sources=select(copy_ref))
    assert store.records("model_request_intent") == []


def test_unknown_saved_copy_use_cannot_be_promoted_by_host_decision(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    original = source_copy(store)
    old = store.get(**original)
    changed = store.transact("unknown-use", {}, lambda tx: tx.put(
        "source_copy", original["id"],
        {**old["body"], "use_record": {"use": "unknown"}}, expected_version=1))
    copy_ref = {"kind": "source_copy", "id": changed["id"], "version": 2}
    _store, _registry, bridge, call, _config, _, _ = setup(tmp_path, source_ref=copy_ref)
    with pytest.raises(SessionError, match="cannot be promoted"):
        bridge.prepare(call_id=call["id"], call_version=1, phase="collect",
                       messages=[{"role": "user", "content": "Review"}],
                       sources=select(copy_ref, "peer"))


def test_interrupted_response_has_trace_and_unknown_reasoning(tmp_path: Path,
                                                              monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    raw = b'{"model":"openai/gpt-oss-120b","choices":[{"message":{"tool_calls":['
    store, _registry, bridge, call, _config, copy_ref, _captured = setup(tmp_path, raw=raw)
    prepared = bridge.prepare(call_id=call["id"], call_version=1, phase="collect",
                              messages=[{"role": "user", "content": "Review abstract"}],
                              sources=select(copy_ref))
    receipt_ref = bridge.invoke_capture(prepared.intent_ref)
    receipt = store.get(**receipt_ref)["body"]
    assert receipt["status"] == "failed" and receipt["error_kind"] == "invalid_json"
    assert receipt["reasoning_status"] == "unknown"
    assert receipt["response_sha256"] == hashlib.sha256(raw).hexdigest()
    assert receipt["partial_response_sha256"] == hashlib.sha256(raw).hexdigest()
    assert store.get(**receipt["reasoning_ref"])["body"]["blob"] is None
    payload = store.read_blob(receipt["blob"]["sha256"]).decode()
    assert json.loads(payload)["tool_calls"] == []
    assert raw.decode() not in payload
    assert any(item["data"]["status"] == "failed"
               for item in TraceJournal(store).export("mission")["records"])


def test_two_callers_dispatch_one_intent_only_once(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    store, _registry, bridge, call, _config, copy_ref, _captured = setup(tmp_path)
    prepared = bridge.prepare(call_id=call["id"], call_version=1, phase="collect",
                              messages=[{"role": "user", "content": "Review abstract"}],
                              sources=select(copy_ref))
    entered, release = threading.Event(), threading.Event()
    seen = []

    def blocked(_request, timeout):
        seen.append(timeout)
        entered.set()
        assert release.wait(5)
        return FakeResponse(response())

    bridge.adapter = GroqSelectedAdapter(ROOT, transport=blocked)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(bridge.invoke_capture, prepared.intent_ref)
        assert entered.wait(5)
        with pytest.raises(DispatchUncertain, match="reconcile"):
            bridge.invoke_capture(prepared.intent_ref)
        release.set()
        receipt_ref = first.result(timeout=5)
    assert len(seen) == 1
    assert bridge.invoke_capture(prepared.intent_ref) == receipt_ref
    assert len(seen) == 1
    assert len(store.records("model_dispatch_attempt")) == 1
    assert store.records("model_dispatch_attempt")[0]["body"]["status"] == "captured"


def test_crash_after_attempt_marker_never_resends_automatically(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    store, _registry, bridge, call, _config, copy_ref, _captured = setup(tmp_path)
    prepared = bridge.prepare(call_id=call["id"], call_version=1, phase="collect",
                              messages=[{"role": "user", "content": "Review abstract"}],
                              sources=select(copy_ref))
    count = []

    def crash(_request, timeout):
        count.append(timeout)
        raise RuntimeError("simulated crash after durable attempt")

    bridge.adapter = GroqSelectedAdapter(ROOT, transport=crash)
    with pytest.raises(RuntimeError, match="simulated crash"):
        bridge.invoke_capture(prepared.intent_ref)
    assert len(store.records("model_dispatch_attempt")) == 1
    assert store.records("model_dispatch_attempt")[0]["body"]["status"] == \
        "attempted_external_effect"
    assert store.records("model_response_receipt") == []
    with pytest.raises(DispatchUncertain, match="reconcile"):
        bridge.invoke_capture(prepared.intent_ref)
    assert len(count) == 1


def test_capture_is_atomic_and_rejects_false_returned_model(tmp_path: Path) -> None:
    store, _registry, bridge, call, _config, copy_ref, _captured = setup(tmp_path)
    prepared = bridge.prepare(call_id=call["id"], call_version=1, phase="collect",
                              messages=[{"role": "user", "content": "Review abstract"}],
                              sources=select(copy_ref))
    parsed = bridge.adapter.decode(prepared.request, json.loads(response()))
    with pytest.raises(SessionError, match="selected model"):
        bridge.capture(prepared.intent_ref, replace(parsed, returned_model="other-model"))
    assert store.records("restricted_reasoning") == []

    class FailingJournal(TraceJournal):
        def append_tx(self, *args, **kwargs):
            raise RuntimeError("synthetic trace fault")

    bridge.journal = FailingJournal(store)
    with pytest.raises(RuntimeError, match="synthetic trace fault"):
        bridge.capture(prepared.intent_ref, parsed)
    assert store.records("restricted_reasoning") == []
    assert store.records("model_response_receipt") == []


def test_backup_restore_retains_request_content_and_restricted_reasoning(
    tmp_path: Path, monkeypatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    store, _registry, bridge, call, _config, copy_ref, _captured = setup(tmp_path / "active")
    prepared = bridge.prepare(call_id=call["id"], call_version=1, phase="collect",
                              messages=[{"role": "user", "content": "Review abstract"}],
                              sources=select(copy_ref))
    receipt_ref = bridge.invoke_capture(prepared.intent_ref)
    manifest = store.backup(tmp_path / "backup")
    restored = StateStore.restore(tmp_path / "backup", tmp_path / "restored")
    assert len(manifest["objects"]) >= 4  # source, request, content and reasoning
    resumed = ModelSession(restored, ROOT, adapter=bridge.adapter)
    assert resumed.load(prepared.intent_ref).request.body == prepared.request.body
    receipt = restored.get(**receipt_ref)["body"]
    payload = restored.read_blob(receipt["blob"]["sha256"]).decode()
    assert "tool-1" in payload and "PRIVATE_REASONING_SENTINEL" not in payload
    reasoning = TraceJournal(restored).read_reasoning(
        receipt["reasoning_ref"], mission_id="mission", diagnostic_granted=True)
    assert reasoning["text"] == "PRIVATE_REASONING_SENTINEL"


def test_credential_binding_survives_reopen_and_dispatch(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY_BACKUP", "fixture-backup")
    selected = {"credential_profile": "backup", "quota_domain": "groq-selected"}
    store, _registry, bridge, call, _config, copy_ref, captured = setup(
        tmp_path, credential_settings=selected,
    )
    prepared = bridge.prepare(call_id=call["id"], call_version=1, phase="collect",
                              messages=[{"role": "user", "content": "Inspect fixture"}],
                              sources=select(copy_ref))
    intent = store.get(**prepared.intent_ref)
    assert all(intent["body"][key] == value for key, value in selected.items())
    reopened = ModelSession(StateStore(tmp_path), ROOT, adapter=bridge.adapter)
    restored = reopened.load(prepared.intent_ref)
    assert restored.request.credential_profile == "backup"
    receipt = reopened.invoke_capture(prepared.intent_ref)
    assert len(captured) == 1
    for record in (reopened.store.get(**receipt), reopened.store.get(
        "model_dispatch_attempt", f"model-dispatch:{prepared.intent_ref['id']}"
    )):
        assert all(record["body"][key] == value for key, value in selected.items())
    assert reopened.invoke_capture(prepared.intent_ref) == receipt
    assert len(captured) == 1


@pytest.mark.parametrize("domain", ["fixture-sensitive-value", "key=fixture", {}, None, 3])
def test_configuration_rejects_invalid_quota_domain_before_save(tmp_path, domain):
    from ora.agents.configuration import ConfigurationError

    with pytest.raises(ConfigurationError, match="nonsecret"):
        setup(tmp_path, credential_settings={"credential_profile": "backup",
                                             "quota_domain": domain})
