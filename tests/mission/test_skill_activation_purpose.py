"""Optional skill activation can carry one public intent without changing its effect."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import replace

import pytest

from ora.agents.capabilities import CapabilityError, content_hash, validate_arguments
from ora.contracts.models import LiveStartRequest
from ora.mission.call_inspection import inspect_calls
from ora.mission.tool_catalog import (
    ACTION_PURPOSE_FIELD,
    LEGACY_ACTIVATE_SKILL_SCHEMA,
    TOOL_SCHEMAS,
    tool_contract_matches,
)
from ora.observability.tool_activity import build_tool_activity
from ora.state import StateStore
from tests.mission.availability_assertions import request_first_names
from tests.mission.test_service import answer, drain, service, tool


def _activation_schema(packet: dict) -> dict:
    return next(item["function"]["parameters"] for item in packet["tools"]
                if item.get("function", {}).get("name") == "activate_skill")


def test_activation_purpose_schema_is_optional_bounded_and_exactly_legacy_compatible(
    tmp_path, monkeypatch,
):
    schema = TOOL_SCHEMAS["activate_skill"]
    assert schema["required"] == ["skill_id"]
    assert schema["properties"]["purpose"] == ACTION_PURPOSE_FIELD
    assert TOOL_SCHEMAS["release_skill"]["properties"] == {
        "skill_id": {"type": "string", "minLength": 1},
    }
    for value in (None, "", "Compare design threats before judging the result."):
        validate_arguments(schema, {"skill_id": "study-appraisal", "purpose": value})
    validate_arguments(schema, {"skill_id": "study-appraisal"})
    for value in ("Q" * 161, 17):
        with pytest.raises(CapabilityError):
            validate_arguments(schema, {"skill_id": "study-appraisal", "purpose": value})

    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: pytest.fail(
        "Catalog inspection must not contact a provider"))
    try:
        current = host.catalog.tools["activate_skill"]
        assert current.version == "2"
        old = replace(current, version="1", schema=LEGACY_ACTIVATE_SKILL_SCHEMA)
        assert tool_contract_matches(current.descriptor(), old.descriptor())
        assert not tool_contract_matches(old.descriptor(), current.descriptor())
        for field, value in (
            ("permission", "other:grant"),
            ("implementation_ref", "build:other"),
            ("version", "3"),
        ):
            assert not tool_contract_matches({**current.descriptor(), field: value}, old.descriptor())
        changed = deepcopy(current.descriptor())
        changed["schema"]["properties"]["purpose"]["maxLength"] = 240
        changed["schema_sha256"] = content_hash(changed["schema"])
        assert not tool_contract_matches(changed, old.descriptor())
        assert host.planning_v1_catalog.tools["activate_skill"].version == "2"
        assert host.legacy_catalog.tools["activate_skill"].version == "2"
    finally:
        host.close()


def test_activation_purpose_is_saved_and_skill_loads_in_same_two_turns(tmp_path, monkeypatch):
    purpose = "Compare design threats before judging the reported result."
    packets = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        if len(packets) == 1:
            schema = _activation_schema(packet)
            assert schema["properties"]["purpose"] == ACTION_PURPOSE_FIELD
            assert schema["required"] == ["skill_id"]
            functions = {item["function"]["name"]: item["function"]
                         for item in packet["tools"]}
            request_description = functions["request_capability"]["description"]
            assert "request-first local tool_id absent" in request_description
            assert "call attached tools directly" in request_description
            availability = next(
                json.loads(message["content"])
                for message in packet["messages"]
                if message.get("role") == "user"
                and '"authority":"trusted_host_attached_tool_availability"'
                in str(message.get("content", ""))
            )
            assert "activate_skill" in functions
            assert "activate_skill" in availability["call_now_local_functions"]
            assert "activate_skill" not in request_first_names(packet, availability)
            assert "# Study appraisal" not in packet["messages"][0]["content"]
            return tool("activate_skill", {
                "skill_id": "study-appraisal", "purpose": purpose,
            }, 1)
        assert len(packets) == 2
        assert "# Study appraisal" in packet["messages"][0]["content"]
        assert "write_note" in {item["function"]["name"] for item in packet["tools"]}
        assistant = next(message for message in packet["messages"]
                         if message["role"] == "assistant" and message.get("tool_calls"))
        assert json.loads(assistant["tool_calls"][0]["function"]["arguments"])[
            "purpose"
        ] == purpose
        return answer("The synthetic study needs a cautious design appraisal.", 2)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="skill-activation-purpose", question="Appraise a synthetic study.",
            agent_ids=["agent-critique"],
        ))
        drain(host, run.run_id)
        assert len(packets) == 2
        calls = inspect_calls(host.store, run.run_id)
        assert len(calls) == 2
        arguments = json.loads(calls[0]["response"]["tool_calls"][0]["function"][
            "arguments"
        ])
        assert arguments["purpose"] == purpose
        assert len(host.store.records("context_tool_result")) == 1
        assert purpose not in json.dumps(build_tool_activity(host.store, run.run_id))
        assert purpose not in json.dumps([
            item["body"] for item in host.store.records("observability_export")
        ])
    finally:
        host.close()
    assert json.loads(inspect_calls(StateStore(tmp_path), run.run_id)[0]["response"][
        "tool_calls"
    ][0]["function"]["arguments"])["purpose"] == purpose


def test_oversize_activation_purpose_rejects_without_loading_skill(tmp_path, monkeypatch):
    packets = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        if len(packets) == 1:
            return tool("activate_skill", {
                "skill_id": "study-appraisal", "purpose": "Q" * 161,
            }, 1)
        result = next(json.loads(message["content"]) for message in packet["messages"]
                      if message.get("role") == "tool"
                      and message.get("name") == "activate_skill")
        assert result["status"] == "rejected"
        assert "# Study appraisal" not in packet["messages"][0]["content"]
        return answer("No source was supplied for the synthetic claim.", 2)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="oversize-skill-purpose", question="Appraise a synthetic claim.",
            agent_ids=["agent-critique"],
        ))
        drain(host, run.run_id)
        assert len(packets) == 2
        assert host._control(run.run_id, "agent-critique")["body"]["active_skill_ids"] == []
    finally:
        host.close()


def test_old_prepared_activation_call_resumes_after_new_catalog_load(tmp_path, monkeypatch):
    old = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: pytest.fail(
        "Preparing an old call must not contact a provider"))
    try:
        active = old.catalog.tools["activate_skill"]
        old.catalog.tools["activate_skill"] = replace(
            active, version="1", schema=LEGACY_ACTIVATE_SKILL_SCHEMA,
        )
        run = old.start(LiveStartRequest(
            request_id="old-pinned-activation", question="Appraise a synthetic study.",
            agent_ids=["agent-critique"],
        ))
        def before_parallel_catalog(tx):
            saved = tx.get("mission_run", run.run_id)
            body = saved["body"]
            # The legacy activation catalog predates the parallel catalog pin.
            body.pop("parallel_research_version", None)
            body.pop("reader_action_purpose_tool_version", None)
            return tx.put("mission_run", run.run_id, body, expected_version=saved["version"])

        old.store.transact("old-activation-catalog", {}, before_parallel_catalog)
        control = old._control(run.run_id, "agent-critique")
        assert old._schedule_model(control)
        call_ref = old._control(run.run_id, "agent-critique")["body"]["call_ref"]
        intent = old.store.get("model_request_intent", f"model-intent:{call_ref['id']}:{call_ref['version']}")
        original_request = old.store.read_blob(intent["body"]["blob"]["sha256"])
        assert _activation_schema(json.loads(original_request)) == LEGACY_ACTIVATE_SKILL_SCHEMA
    finally:
        old.close()

    requests = []

    def transport(request, *, timeout):
        requests.append(request.data)
        packet = json.loads(request.data)
        if request.data == original_request:
            return tool("activate_skill", {"skill_id": "study-appraisal"}, 1)
        assert "# Study appraisal" in packet["messages"][0]["content"]
        return answer("The preliminary synthetic appraisal remains limited.", 2)

    resumed = service(tmp_path, monkeypatch, transport)
    try:
        drain(resumed, run.run_id)
        assert original_request in requests
        assert len(requests) == 2
        assert resumed._run(run.run_id)["body"]["status"] == "completed"
        assert resumed.store.read_blob(intent["body"]["blob"]["sha256"]) == original_request
    finally:
        resumed.close()
