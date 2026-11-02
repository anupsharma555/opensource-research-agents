"""Qwen packing uses its saved surrogate units and system-role inclusion."""

from __future__ import annotations

import hashlib
import json

import pytest

from ora.agents.capabilities import content_hash
from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.mission.context import ContextError, WorkingContext
from ora.mission.service import _context_counter_for_request_basis
from ora.models.profiles import GPT_OSS_MODEL, QWEN_MODEL
from ora.models.token_estimate import (
    QWEN_SURROGATE_BASIS,
    QWEN_TEMPLATE_BASIS,
    QWEN_TEMPLATE_LOW_BASIS,
    TOKENIZER_BASIS,
    estimate_qwen_context_tokens,
    estimate_request_tokens,
)
from ora.state import StateConflict, StateStore
from ora.state.store import canonical
from tests.mission.test_context import fixture
from tests.mission.test_selected_window_admission import _saved_tool_request
from tests.mission.test_service import service


def test_qwen_fragment_counter_rejects_mixed_or_noncanonical_units():
    value = canonical([{"role": "user", "content": "Synthetic bounded question"}])
    counter = _context_counter_for_request_basis(QWEN_SURROGATE_BASIS, QWEN_MODEL)
    assert counter(value) == len(value.encode()) + 128
    assert _context_counter_for_request_basis(QWEN_TEMPLATE_BASIS, QWEN_MODEL)(
        value
    ) == estimate_qwen_context_tokens(value)
    assert _context_counter_for_request_basis(QWEN_TEMPLATE_LOW_BASIS, QWEN_MODEL)(
        value
    ) == estimate_qwen_context_tokens(value, reasoning_effort="low")
    with pytest.raises(StateConflict, match="selected estimate basis"):
        _context_counter_for_request_basis(TOKENIZER_BASIS, QWEN_MODEL)
    with pytest.raises(StateConflict, match="canonical messages"):
        counter('[ {"role": "user", "content": "Synthetic bounded question"} ]')


def test_low_qwen_saved_request_uses_native_basis_without_changing_source_bytes(
    tmp_path, monkeypatch,
):
    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: (_ for _ in ()).throw(
        AssertionError("Low Qwen preparation must not dispatch a model")))
    try:
        source_text = "SYNTHETIC_LOW_QWEN_SOURCE: checked comparison at week eight."
        run = host.start(LiveStartRequest(
            request_id="qwen-low-native-source-packing",
            question="Assess the supplied synthetic comparison and its limits.",
            evidence_text=source_text,
            agent_ids=["agent-synthesis"],
            agent_models={"agent-synthesis": {
                "model": QWEN_MODEL, "reasoning_effort": "low",
                "max_completion_tokens": 960,
            }},
        ))
        control = host._control(run.run_id, "agent-synthesis")
        assert host._schedule_model(control)
        prepared = host._control(run.run_id, "agent-synthesis")
        call = host.store.get(**prepared["body"]["call_ref"])
        intent = host.store.get(
            "model_request_intent", f"model-intent:{call['id']}:{call['version']}",
        )
        raw = host.store.read_blob(intent["body"]["blob"]["sha256"])
        packet = json.loads(raw)
        budget = host.store.get("mission_call_budget", call["id"])["body"]
        estimate = estimate_request_tokens(raw)
        assert packet["reasoning_effort"] == "low"
        assert packet["reasoning_format"] == "parsed"
        assert estimate.basis == QWEN_TEMPLATE_LOW_BASIS
        assert budget["estimate_basis"] == estimate.basis
        assert budget["request_stage_token_estimate"]["final_input_tokens"] == (
            estimate.tokens
        )
        assert budget["request_stage_token_estimate"]["stages"][-1][
            "cumulative_tokens"
        ] == estimate.tokens
        assert sum(stage["marginal_tokens"] for stage in budget[
            "request_stage_token_estimate"
        ]["stages"]) == estimate.tokens
        assert estimate_request_tokens(raw, basis=QWEN_TEMPLATE_LOW_BASIS) == estimate
        source_packets = [source for message in packet["messages"]
                          if message["role"] == "user" and message["content"].startswith("{")
                          for source in json.loads(message["content"]).get(
                              "source_packets", [],
                          )]
        assert len(source_packets) == 1
        assert source_packets[0]["text"].splitlines()[-1] == source_text
        assert host.session.load(ref(intent), for_dispatch=True).request.request_sha256 == (
            intent["body"]["request_sha256"]
        )
        assert host.store.records("model_dispatch_attempt") == []
    finally:
        host.close()


def test_mixed_peer_models_prepare_exact_source_and_incoming_question(tmp_path, monkeypatch):
    qwen_intent_ref = None
    qwen_request_bytes = None
    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: (_ for _ in ()).throw(
        AssertionError("Offline preparation must not dispatch a model")))
    try:
        run = host.start(LiveStartRequest(
            request_id="qwen-context-mixed-peers",
            question="Compare the permitted synthetic observation with a peer.",
            evidence_text="DOI: 10.1234/qwen-context. SYNTHETIC_QWEN_SOURCE " + "A" * 750,
            agent_ids=["agent-synthesis", "agent-critique"],
            agent_models={"agent-critique": {"model": QWEN_MODEL}},
        ))
        sender = host._control(run.run_id, "agent-synthesis")
        recipient = host._control(run.run_id, "agent-critique")
        question = "Which exact synthetic source span supports the comparison?"
        sent = host.mailbox.send_request(
            "qwen-context-peer-question", mission_id=sender["body"]["mission_id"],
            request_id="qwen-context-peer-question",
            message_id="qwen-context-peer-message",
            requester_id=sender["body"]["actor_id"],
            recipient_id=recipient["body"]["actor_id"], question=question,
            expected_artifact="A source-bounded reply",
            cause_ref=sender["body"]["proposal_ref"],
            task_ref=sender["body"]["proposal_ref"], payload={},
        )
        assert sent["status"] == "accepted"
        assert host.mailbox.mark_delivered(
            "qwen-context-peer-deliver", mission_id=sender["body"]["mission_id"],
            message_id="qwen-context-peer-message",
        )["status"] == "accepted"
        host._update_control(recipient, input_budget={
            "observed_limit_tokens": 60_000, "target_fraction": 1.0,
            "basis": "qualification_imposed_limit",
        })
        for alias, model, role in (
            ("agent-synthesis", GPT_OSS_MODEL, "developer"),
            ("agent-critique", QWEN_MODEL, "system"),
        ):
            control = host._control(run.run_id, alias)
            assert host._schedule_model(control)
            prepared = host._control(run.run_id, alias)
            call = host.store.get(**prepared["body"]["call_ref"])
            intent = host.store.get(
                "model_request_intent", f"model-intent:{call['id']}:{call['version']}",
            )
            raw = host.store.read_blob(intent["body"]["blob"]["sha256"])
            packet = json.loads(raw)
            budget = host.store.get("mission_call_budget", call["id"])["body"]
            estimate = estimate_request_tokens(raw)
            assert packet["model"] == model
            assert packet["messages"][0] == {
                "role": role,
                "content": host.store.get(**intent["body"]["configuration_ref"])["body"][
                    "instructions"
                ],
            }
            assert budget["request_token_estimate"]["tokens"] == estimate.tokens
            assert budget["request_stage_token_estimate"]["final_input_tokens"] == (
                estimate.tokens
            )
            assert budget["request_stage_token_estimate"]["stages"][-1][
                "cumulative_tokens"
            ] == estimate.tokens
            assert sum(stage["marginal_tokens"] for stage in budget[
                "request_stage_token_estimate"
            ]["stages"]) == estimate.tokens
            assert estimate.basis == (
                QWEN_TEMPLATE_BASIS if model == QWEN_MODEL else TOKENIZER_BASIS
            )
            if model == QWEN_MODEL:
                assert budget["estimate_basis"] == QWEN_TEMPLATE_BASIS
                assert budget["request_stage_token_estimate"][
                    "component_allocation_method"
                ] == "raw_synthetic_prefix_signed_diagnostic"
                assert budget["observed_account_limit"] == 60_000
                assert estimate.tokens <= 60_000
                assert estimate_request_tokens(
                    raw, basis=QWEN_SURROGATE_BASIS,
                ).basis == QWEN_SURROGATE_BASIS
                qwen_intent_ref = ref(intent)
                qwen_request_bytes = raw
            packets = [item for message in packet["messages"]
                       if message.get("role") == "user"
                       for item in json.loads(message["content"]).get("source_packets", [])]
            assert len(packets) == 1
            assert "SYNTHETIC_QWEN_SOURCE" in packets[0]["text"]
            assert len(packets[0]["passage_refs"]) == 1
            index = json.loads(packet["messages"][-1]["content"])
            assert index["task"]["objective"]
            if alias == "agent-critique":
                assert index["incoming_request"]["question"] == question
            inclusion = host.store.get("context_inclusion", f"context:{intent['id']}", 1)
            assert inclusion["body"]["request_sha256"] == intent["body"][
                "request_sha256"
            ]
            assert host.session.load(ref(intent), for_dispatch=True).request.request_sha256 == (
                intent["body"]["request_sha256"]
            )
            assert not host.store.records("model_dispatch_attempt")
    finally:
        host.close()
    assert qwen_intent_ref is not None and qwen_request_bytes is not None
    reopened = service(tmp_path, monkeypatch, lambda *_a, **_kw: (_ for _ in ()).throw(
        AssertionError("Reopening must not dispatch a model")))
    try:
        assert reopened.session.load(
            qwen_intent_ref, for_dispatch=True,
        ).request.request_sha256 == hashlib.sha256(qwen_request_bytes).hexdigest()
        assert reopened.store.read_blob(reopened.store.get(**qwen_intent_ref)["body"][
            "blob"
        ]["sha256"]) == qwen_request_bytes
    finally:
        reopened.close()


def test_two_qwen_peers_keep_three_checked_sources_through_tool_continuation(
    tmp_path, monkeypatch,
):
    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: (_ for _ in ()).throw(
        AssertionError("Offline preparation must not dispatch a model")))
    try:
        texts = [
            "SYNTHETIC_FIRST_REPORT: cohort A, comparison and timing remain uncertain.",
            "SYNTHETIC_SECOND_REPORT: cohort B has a distinct comparison.",
            "SYNTHETIC_FOLLOWUP_REPORT: follow-up belongs to cohort B.",
        ]
        run = host.start(LiveStartRequest(
            request_id="qwen-three-source-tool-continuation",
            question="Compare three synthetic reports and ask a peer to check one source point.",
            scope="Keep cohorts and follow-up reports distinct.",
            evidence_text=texts[0],
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis", "agent-critique"],
            agent_models={alias: {"model": QWEN_MODEL, "reasoning_effort": "medium",
                                  "max_completion_tokens": 1024}
                          for alias in ("agent-synthesis", "agent-critique")},
        ))
        saved_run = host._run(run.run_id)
        source_refs = list(saved_run["body"]["source_refs"])
        for number, source_text in enumerate(texts[1:], 2):
            source_refs.append(host._supplied_source({
                **saved_run,
                "id": f"{run.run_id}-synthetic-report-{number}",
                "body": {**saved_run["body"], "profile": {
                    **saved_run["body"]["profile"], "evidenceText": source_text,
                }},
            }))
        assert host._source_policy_decisions(source_refs) == (source_refs, [])
        host.store.transact("qwen-link-three-sources", {}, lambda tx: tx.put(
            "mission_run", run.run_id,
            {**saved_run["body"], "source_refs": source_refs},
            expected_version=saved_run["version"],
        ), mission_id=saved_run["body"]["mission_id"])
        for alias in ("agent-synthesis", "agent-critique"):
            host._update_control(host._control(run.run_id, alias), source_refs=source_refs)

        def prepared_packet(alias: str):
            control = host._control(run.run_id, alias)
            assert host._schedule_model(control)
            call = host.store.get(**host._control(run.run_id, alias)["body"]["call_ref"])
            intent = host.store.get(
                "model_request_intent", f"model-intent:{call['id']}:{call['version']}",
            )
            raw = host.store.read_blob(intent["body"]["blob"]["sha256"])
            packet = json.loads(raw)
            budget = host.store.get("mission_call_budget", call["id"])["body"]
            estimate = estimate_request_tokens(raw)
            assert estimate.basis == QWEN_TEMPLATE_BASIS
            assert packet["model"] == QWEN_MODEL
            assert packet["messages"][0]["role"] == "system"
            assert packet["reasoning_effort"] == "medium"
            assert packet["max_completion_tokens"] == 1024
            assert budget["request_token_estimate"]["tokens"] == estimate.tokens
            assert budget["request_stage_token_estimate"]["final_input_tokens"] == estimate.tokens
            assert budget["request_stage_token_estimate"][
                "component_allocation_method"
            ] == "raw_synthetic_prefix_signed_diagnostic"
            assert budget["request_stage_token_estimate"]["stages"][-1][
                "cumulative_tokens"
            ] == estimate.tokens
            assert sum(stage["marginal_tokens"] for stage in budget[
                "request_stage_token_estimate"
            ]["stages"]) == estimate.tokens
            assert host.session.load(ref(intent), for_dispatch=True).request.request_sha256 == (
                intent["body"]["request_sha256"]
            )
            sources = [source for message in packet["messages"]
                       if message["role"] == "user" and message["content"].startswith("{")
                       for source in json.loads(message["content"]).get("source_packets", [])]
            assert {source["id"] for source in sources} == {
                pointer["id"] for pointer in source_refs
            }
            assert {source["id"]: source["version"] for source in sources} == {
                pointer["id"]: pointer["version"] for pointer in source_refs
            }
            assert {source["id"]: source["copy_sha256"] for source in sources} == {
                pointer["id"]: host.store.get(**pointer)["body"]["content_sha256"]
                for pointer in source_refs
            }
            for source_text in texts:
                assert any(source_text in source["text"] for source in sources)
            return control, intent, packet

        sender, _, _ = prepared_packet("agent-synthesis")
        recipient, _, _ = prepared_packet("agent-critique")
        question = "Does the follow-up report reuse cohort B's participants?"
        sent = host.mailbox.send_request(
            "qwen-three-source-peer-question", mission_id=sender["body"]["mission_id"],
            request_id="qwen-three-source-peer-question",
            message_id="qwen-three-source-peer-message",
            requester_id=sender["body"]["actor_id"],
            recipient_id=recipient["body"]["actor_id"], question=question,
            expected_artifact="Source-bounded answer",
            cause_ref=sender["body"]["proposal_ref"],
            task_ref=sender["body"]["proposal_ref"], payload={},
        )
        assert sent["status"] == "accepted"
        assert host.mailbox.mark_delivered(
            "qwen-three-source-peer-delivery", mission_id=sender["body"]["mission_id"],
            message_id="qwen-three-source-peer-message",
        )["status"] == "accepted"
        recipient = host._control(run.run_id, "agent-critique")
        response, result = _saved_tool_request(
            host, recipient, response_id="qwen-three-source-board-read-response",
            tool_id="qwen-three-source-board-read-call", name="board_read",
            arguments={}, result={"synthetic_board_note": "Keep both cohort IDs distinct."},
        )
        host._update_control(
            recipient, phase="ready_model", response_ref=response,
            tool_turn_refs=[*recipient["body"]["tool_turn_refs"], response],
        )
        _, continuation, packet = prepared_packet("agent-critique")
        current = next(json.loads(message["content"]) for message in packet["messages"]
                       if message["role"] == "user" and message["content"].startswith("{")
                       and json.loads(message["content"]).get("authority") == "current_records")
        assert current["incoming_request"]["question"] == question
        assistant = [message for message in packet["messages"] if message["role"] == "assistant"]
        outcomes = [message for message in packet["messages"] if message["role"] == "tool"]
        assert any(call["id"] == "qwen-three-source-board-read-call"
                   for message in assistant for call in message["tool_calls"])
        assert any(message["tool_call_id"] == "qwen-three-source-board-read-call"
                   and "Keep both cohort IDs distinct" in message["content"]
                   for message in outcomes)
        assert host.store.get(**result) is not None
        assert continuation["body"]["request_sha256"] == hashlib.sha256(
            host.store.read_blob(continuation["body"]["blob"]["sha256"])
        ).hexdigest()
        assert len(host.store.records("model_dispatch_attempt")) == 0
    finally:
        host.close()


@pytest.mark.parametrize("role,valid", [("system", True), ("developer", False)])
def test_inclusion_requires_qwen_system_role_from_saved_configuration(
    tmp_path, role, valid,
):
    store = StateStore(tmp_path)
    _old_call, task_ref = fixture(store)

    def qwen_call(tx):
        config_body = {
            "mission_id": "m", "instance_id": "peer", "provider": "groq",
            "endpoint": "https://example.invalid", "model_id": QWEN_MODEL,
            "settings": {"max_completion_tokens": 512},
            "reasoning_settings": {"reasoning_effort": "medium",
                                   "reasoning_format": "parsed"},
            "instructions": "Synthetic Qwen instructions",
        }
        config_body["configuration_sha256"] = content_hash(config_body)
        config = tx.put("agent_configuration", "qwen-config", config_body)
        call = tx.put("agent_call", "qwen-call", {
            "mission_id": "m", "instance_id": "peer", "task_id": "task",
            "configuration_id": config["id"],
            "configuration_sha256": config_body["configuration_sha256"],
        })
        return ref(call)

    call_ref = store.transact("qwen-context-call", {}, qwen_call, mission_id="m")
    context = WorkingContext(store)
    pack = context.assemble(call_ref, task_ref)
    request = {"messages": [
        {"role": role, "content": "Synthetic Qwen instructions"}, *pack.messages,
    ]}
    encoded = canonical(request).encode()
    blob = store.put_blob(encoded)
    intent_id = f"model-intent:{call_ref['id']}:{call_ref['version']}"

    def save(tx):
        return ref(tx.put("model_request_intent", intent_id, {
            "mission_id": "m", "call_ref": call_ref,
            "configuration_ref": pack.configuration_ref, "source_refs": [],
            "context_source_dependency_refs": list(
                context.context_source_dependency_refs(pack)
            ),
            "request_sha256": hashlib.sha256(encoded).hexdigest(), "blob": blob,
        }))

    intent_ref = store.transact("qwen-context-intent", {}, save, mission_id="m")
    if valid:
        assert context.save_inclusion("qwen-context-inclusion", pack, intent_ref)
    else:
        with pytest.raises(ContextError, match="instructions differ"):
            context.save_inclusion("qwen-context-inclusion", pack, intent_ref)
