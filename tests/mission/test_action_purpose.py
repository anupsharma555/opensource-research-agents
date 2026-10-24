"""A brief public action purpose travels with two consequential tool calls."""

from __future__ import annotations

import json
import time
from copy import deepcopy

import pytest

from ora.agents.capabilities import (
    COMMON_SCHEMAS,
    CapabilityCatalog,
    CapabilityError,
    content_hash,
    validate_arguments,
)
from ora.contracts.models import LiveStartRequest
from ora.mission import service as service_module
from ora.mission.call_inspection import inspect_calls
from ora.mission.tool_catalog import (
    ACTION_PURPOSE_FIELD,
    ACTION_PURPOSE_FIELD_V3,
    mission_action_purpose_capability,
    mission_action_purpose_v3_capability,
    tool_contract_matches,
)
from ora.observability.tool_activity import build_tool_activity
from ora.state import StateStore
from tests.mission.test_service import answer, drain, serialized_peer_name, service, tool


@pytest.mark.parametrize("name,arguments", [
    ("peer_request", {"target_instance_id": "another-peer", "question": "Check this claim."}),
    ("request_capability", {"tool_id": "read_context_record"}),
])
def test_only_exact_common_tools_gain_optional_bounded_purpose(name, arguments):
    old = CapabilityCatalog.with_common().tools[name]
    new = mission_action_purpose_capability(old)
    assert old.version == "1" and old.schema == COMMON_SCHEMAS[name]
    assert new.version == "2"
    assert new.schema["required"] == old.schema["required"]
    assert new.schema["properties"]["purpose"]["maxLength"] == 160
    assert new.schema["additionalProperties"] is False
    validate_arguments(new.schema, arguments)
    for value in ("Check a real uncertainty.", None, ""):
        validate_arguments(new.schema, {**arguments, "purpose": value})
    for value in ("Q" * 161, 42):
        with pytest.raises(CapabilityError):
            validate_arguments(new.schema, {**arguments, "purpose": value})
    assert tool_contract_matches(new.descriptor(), old.descriptor())
    assert not tool_contract_matches(old.descriptor(), new.descriptor())
    untouched = CapabilityCatalog.with_common().tools["board_read"]
    assert mission_action_purpose_capability(untouched) is untouched


def test_new_action_purpose_bound_keeps_other_arguments_and_old_contract(name="peer_request"):
    old = CapabilityCatalog.with_common().tools[name]
    v2 = mission_action_purpose_capability(old)
    v3 = mission_action_purpose_v3_capability(v2)
    assert v2.schema["properties"]["purpose"] == ACTION_PURPOSE_FIELD
    assert v3.version == "3" and v3.schema["properties"]["purpose"] == ACTION_PURPOSE_FIELD_V3
    assert {key: value for key, value in v3.schema["properties"].items() if key != "purpose"} == {
        key: value for key, value in v2.schema["properties"].items() if key != "purpose"
    }
    assert v3.schema["required"] == v2.schema["required"]
    args = {"target_instance_id": "another-peer", "question": "Check this claim."}
    validate_arguments(v3.schema, {**args, "purpose": "P" * 221})
    with pytest.raises(CapabilityError):
        validate_arguments(v3.schema, {**args, "purpose": "P" * 513})
    assert tool_contract_matches(v3.descriptor(), v2.descriptor())
    assert tool_contract_matches(v3.descriptor(), old.descriptor())


@pytest.mark.parametrize("name", ["peer_request", "request_capability"])
def test_legacy_matcher_rejects_other_descriptor_or_schema_drift(name):
    old = CapabilityCatalog.with_common().tools[name].descriptor()
    current = mission_action_purpose_capability(
        CapabilityCatalog.with_common().tools[name]
    ).descriptor()
    assert tool_contract_matches(current, old)
    for field, value in (
        ("permission", "other:grant"),
        ("implementation_ref", "build:other"),
        ("side_effects", "external"),
        ("version", "3"),
    ):
        assert not tool_contract_matches({**current, field: value}, old)
    changed = deepcopy(current)
    changed["schema"]["properties"]["purpose"]["maxLength"] = 240
    changed["schema_sha256"] = content_hash(changed["schema"])
    assert not tool_contract_matches(changed, old)
    required = deepcopy(current)
    required["schema"]["required"].append("purpose")
    required["schema_sha256"] = content_hash(required["schema"])
    assert not tool_contract_matches(required, old)
    changed_old = deepcopy(old)
    changed_old["schema"]["properties"]["other"] = {"type": "string"}
    changed_old["schema_sha256"] = content_hash(changed_old["schema"])
    assert not tool_contract_matches(current, changed_old)


def _drive_until(host, run_id: str, predicate) -> None:
    for _ in range(350):
        host._drive_run(host._run(run_id))
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError("Action-purpose fixture did not reach its saved boundary")


def _tools(packet: dict) -> dict:
    return {item["function"]["name"]: item["function"] for item in packet["tools"]}


def test_new_purpose_is_serialized_saved_replayed_once_without_extra_turn(tmp_path, monkeypatch):
    packets = []
    purpose = "Read a saved record to resolve this uncertainty."

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        function = _tools(packet)["request_capability"]
        assert "purpose" in function["parameters"]["properties"]
        assert "purpose" not in function["parameters"]["required"]
        if len(packets) == 1:
            return tool("request_capability", {
                "tool_id": "read_context_record", "purpose": purpose,
            }, 1)
        assert len(packets) == 2
        assistant = next(message for message in packet["messages"]
                         if message["role"] == "assistant" and message.get("tool_calls"))
        assert json.loads(assistant["tool_calls"][0]["function"]["arguments"])[
            "purpose"
        ] == purpose
        return answer("The synthetic task has no source to support a broader finding.", 2)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="action-purpose-selection", question="Assess a synthetic claim.",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id)
        assert host._run(run.run_id)["body"]["status"] == "completed"
        assert len(packets) == 2
        calls = inspect_calls(host.store, run.run_id)
        selected = [call for call in calls if call.get("response", {}).get("tool_calls")]
        assert len(selected) == 1
        arguments = json.loads(selected[0]["response"]["tool_calls"][0]["function"][
            "arguments"
        ])
        assert arguments["purpose"] == purpose
        assert len(host.store.records("mission_capability_selection")) == 1
        assert purpose not in json.dumps(build_tool_activity(host.store, run.run_id))
        assert purpose not in json.dumps([
            item["body"] for item in host.store.records("observability_export")
        ])
    finally:
        host.close()
    reopened = StateStore(tmp_path)
    assert json.loads(inspect_calls(reopened, run.run_id)[0]["response"]["tool_calls"][0][
        "function"
    ]["arguments"])["purpose"] == purpose


def test_oversize_purpose_is_rejected_locally_without_inventing_one(tmp_path, monkeypatch):
    packets = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        if len(packets) == 1:
            return tool("request_capability", {
                "tool_id": "read_context_record", "purpose": "Q" * 513,
            }, 1)
        assert len(packets) == 2
        rejected = next(json.loads(message["content"]) for message in packet["messages"]
                        if message.get("role") == "tool"
                        and message.get("name") == "request_capability")
        assert rejected["status"] == "rejected"
        assert "purpose" not in rejected
        return answer("The synthetic task has no checked source.", 2)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="action-purpose-oversize", question="Assess a synthetic claim.",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id)
        assert len(packets) == 2
        assert not host.store.records("mission_capability_selection")
    finally:
        host.close()


def test_peer_purpose_does_not_change_recipient_message_or_request_identity(tmp_path, monkeypatch):
    purpose = ("Independent critique may catch a tie-policy error before I publish. " * 4)[:221]
    question = "Check the synthetic tie policy against the selected evidence."
    packets = []
    sage_calls = 0

    def transport(request, *, timeout):
        nonlocal sage_calls
        packet = json.loads(request.data)
        packets.append(packet)
        role = serialized_peer_name(packet)
        if role == "Sage":
            sage_calls += 1
        if role == "Sage" and sage_calls == 1:
            function = _tools(packet)["peer_request"]
            assert "purpose" in function["parameters"]["properties"]
            assert function["parameters"]["properties"]["purpose"]["maxLength"] == 512
            assert len(purpose) == 221
            return tool("peer_request", {
                "target_instance_id": "agent-critique", "question": question,
                "purpose": purpose,
            }, 10)
        return answer("No further synthetic finding is supported.", 11 + len(packets))

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="action-purpose-peer", question="Assess a synthetic tie.",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        sage_id = host._control(run.run_id, "agent-synthesis")["body"]["actor_id"]
        _drive_until(host, run.run_id, lambda: bool(host.store.records("mail_request")))
        sent = host.store.records("mail_request")
        assert len(sent) == 1 and sent[0]["body"]["question"] == question
        message = host.store.get(**sent[0]["body"]["message_ref"])
        assert message["body"]["payload"] == {"question": question}
        calls = inspect_calls(host.store, run.run_id)
        authored = next(call for call in calls
                        if call["agentId"] == sage_id and call.get("response", {}).get("tool_calls"))
        assert json.loads(authored["response"]["tool_calls"][0]["function"][
            "arguments"
        ])["purpose"] == purpose
    finally:
        host.close()
    assert len(StateStore(tmp_path).records("mail_request")) == 1


def test_saved_purpose_v2_run_keeps_160_schema_and_configuration_on_reopen(
    tmp_path, monkeypatch,
):
    def no_provider(*_args, **_kwargs):
        pytest.fail("Prepared v2 intent must not contact a provider")

    host = service(tmp_path, monkeypatch, no_provider)
    try:
        run = host.start(LiveStartRequest(
            request_id="saved-purpose-v2", question="Assess a synthetic record.",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        saved_run = host._run(run.run_id)
        historical_body = dict(saved_run["body"])
        historical_body.pop("action_purpose_tool_version")
        historical_body.pop("parallel_research_version")
        host.store.transact(
            "fixture:saved-purpose-v2", {"run_id": run.run_id},
            lambda tx: tx.put("mission_run", run.run_id, historical_body,
                              expected_version=saved_run["version"]),
            mission_id=historical_body["mission_id"],
        )
        control = host._control(run.run_id, "agent-synthesis")
        catalog, _ = host._tool_registry(control)
        assert catalog.tools["peer_request"].version == "2"
        before = host._configuration(control)
        assert host._schedule_model(control)
        pointer = host._control(run.run_id, "agent-synthesis")["body"]["call_ref"]
        intent = host.store.get(
            "model_request_intent", f"model-intent:{pointer['id']}:{pointer['version']}",
        )
        request_bytes = host.store.read_blob(intent["body"]["blob"]["sha256"])
        assert _tools(json.loads(request_bytes))["peer_request"]["parameters"][
            "properties"
        ]["purpose"]["maxLength"] == 160
    finally:
        host.close()

    reopened = service(tmp_path, monkeypatch, no_provider)
    try:
        control = reopened._control(run.run_id, "agent-synthesis")
        catalog, _ = reopened._tool_registry(control)
        assert catalog.tools["peer_request"].version == "2"
        assert reopened._configuration(control)["id"] == before["id"]
        assert reopened.store.read_blob(intent["body"]["blob"]["sha256"]) == request_bytes
    finally:
        reopened.close()


@pytest.mark.parametrize("action", ["request_capability", "peer_request"])
def test_old_prepared_common_tool_call_executes_after_new_catalog_load(
    tmp_path, monkeypatch, action,
):
    real_overlay = service_module.mission_action_purpose_capability
    monkeypatch.setattr(service_module, "mission_action_purpose_capability", lambda item: item)
    old = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: pytest.fail(
        "Preparing the legacy call must not contact a provider"))
    try:
        run = old.start(LiveStartRequest(
            request_id=f"legacy-action-purpose-{action}", question="Assess a synthetic tie.",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        sage = old._control(run.run_id, "agent-synthesis")
        assert old._schedule_model(sage)
        pointer = old._control(run.run_id, "agent-synthesis")["body"]["call_ref"]
        intent = old.store.get(
            "model_request_intent", f"model-intent:{pointer['id']}:{pointer['version']}",
        )
        original_request = old.store.read_blob(intent["body"]["blob"]["sha256"])
        assert "purpose" not in _tools(json.loads(original_request))[action]["parameters"][
            "properties"
        ]
    finally:
        old.close()
    monkeypatch.setattr(service_module, "mission_action_purpose_capability", real_overlay)
    packets = []

    def transport(request, *, timeout):
        packets.append(request.data)
        if request.data == original_request:
            arguments = (
                {"tool_id": "peer_request"} if action == "request_capability" else
                {"target_instance_id": "agent-critique", "question": "Check this synthetic tie."}
            )
            return tool(action, arguments, 20)
        return answer("No additional synthetic source was supplied.", 21 + len(packets))

    host = service(tmp_path, monkeypatch, transport)
    try:
        if action == "request_capability":
            _drive_until(host, run.run_id, lambda: bool(host.store.records(
                "mission_capability_selection"
            )))
            selection = host.store.records("mission_capability_selection")[0]
            assert selection["body"]["tool_id"] == "peer_request"
        else:
            _drive_until(host, run.run_id, lambda: bool(host.store.records("mail_request")))
            assert len(host.store.records("mail_request")) == 1
        assert original_request in packets
        assert host.store.read_blob(intent["body"]["blob"]["sha256"]) == original_request
    finally:
        host.close()
