"""A new-run reader can state an optional public purpose without changing the read."""

from __future__ import annotations

import json

import pytest

from ora.agents.capabilities import (
    Capability,
    CapabilityCatalog,
    CapabilityError,
    validate_arguments,
)
from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.mission.call_inspection import inspect_calls
from ora.mission.tool_catalog import (
    ACTION_PURPOSE_FIELD_V3,
    TOOL_SCHEMAS,
    mission_reader_purpose_capability,
)
from ora.state import StateStore
from tests.mission.test_service import answer, drain, service, tool

_OMITTED = object()
_SOURCE = "DOI: 10.1234/synthetic. Synthetic trial methods and uncertain results."


def _reader(packet: dict) -> dict:
    return next(item["function"] for item in packet["tools"]
                if item["function"]["name"] == "read_source_window")


def test_reader_overlay_changes_only_optional_schema_and_version() -> None:
    old = CapabilityCatalog.with_common().tools.get("read_source_window")
    assert old is None  # This is a mission reader, not a common tool.
    mission = Capability(
        "read_source_window", "1", TOOL_SCHEMAS["read_source_window"],
        "mission:read_source_window",
    )
    updated = mission_reader_purpose_capability(mission)
    assert updated.version == "2"
    assert updated.schema["required"] == mission.schema["required"]
    assert updated.schema["additionalProperties"] is False
    assert updated.schema["properties"]["purpose"] == ACTION_PURPOSE_FIELD_V3
    assert {key: value for key, value in updated.schema["properties"].items()
            if key != "purpose"} == mission.schema["properties"]
    arguments = {
        "source_id": "synthetic-copy", "source_version": 1,
        "block_offset": 0, "char_offset": 0, "char_limit": 600,
    }
    validate_arguments(updated.schema, arguments)
    validate_arguments(updated.schema, {**arguments, "purpose": None})
    validate_arguments(updated.schema, {**arguments, "purpose": "Check trial design."})
    for invalid in ("X" * 513, 42):
        with pytest.raises(CapabilityError):
            validate_arguments(updated.schema, {**arguments, "purpose": invalid})


@pytest.mark.parametrize("purpose", [
    pytest.param("Check the exact trial comparison before synthesis.", id="stated"),
    pytest.param(None, id="null"),
    pytest.param(_OMITTED, id="omitted"),
])
def test_new_run_reader_purpose_is_optional_and_one_read_replays(
    tmp_path, monkeypatch, purpose,
) -> None:
    packets = []
    copy_ref = {}

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        reader = _reader(packet)
        assert "purpose" in reader["parameters"]["properties"]
        assert reader["parameters"]["properties"]["purpose"]["maxLength"] == 512
        assert "purpose" not in reader["parameters"]["required"]
        assert "ordinary assistant content" in packet["messages"][0]["content"]
        assert any(_SOURCE in (message.get("content") or "")
                   for message in packet["messages"])
        if len(packets) == 1:
            arguments = {
                "source_id": copy_ref["id"], "source_version": copy_ref["version"],
                "block_offset": 0, "char_offset": 0, "char_limit": len(_SOURCE),
                "selection_mode": "replace",
            }
            if purpose is not _OMITTED:
                arguments["purpose"] = purpose
            return tool("read_source_window", arguments, 1)
        return answer("Synthetic source only; no broader inference.", 2)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="reader-purpose-" + ("omitted" if purpose is _OMITTED
                                          else "null" if purpose is None else "stated"),
            question="Assess this synthetic trial and its limitations.",
            evidence_text=_SOURCE, agent_ids=["agent-synthesis"],
        ))
        assert host._run(run.run_id)["body"]["reader_action_purpose_tool_version"] == 2
        copy_ref.update(host._control(run.run_id, "agent-synthesis")["body"]["source_refs"][0])
        drain(host, run.run_id)
        assert len(packets) == 2
        calls = inspect_calls(host.store, run.run_id)
        assert len(calls) == 2
        saved_args = json.loads(calls[0]["response"]["tool_calls"][0]["function"][
            "arguments"
        ])
        if purpose is _OMITTED:
            assert "purpose" not in saved_args
        else:
            assert saved_args["purpose"] == purpose
        assert len(host.store.records("source_read_window")) == 1
        assert len(host.store.records("context_tool_result")) == 1
        call = host.store.get(**host._control(run.run_id, "agent-synthesis")[
            "body"
        ]["call_ref"])
        assert call["body"]["permitted_tools"]["read_source_window"]["version"] == "2"
    finally:
        host.close()
    reopened = StateStore(tmp_path)
    replayed = json.loads(inspect_calls(reopened, run.run_id)[0]["response"][
        "tool_calls"
    ][0]["function"]["arguments"])
    assert replayed == saved_args


@pytest.mark.parametrize("purpose", ["X" * 513, 42])
def test_invalid_reader_purpose_rejects_without_source_effect(
    tmp_path, monkeypatch, purpose,
) -> None:
    packets = []
    copy_ref = {}

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        if len(packets) == 1:
            return tool("read_source_window", {
                "source_id": copy_ref["id"], "source_version": copy_ref["version"],
                "block_offset": 0, "char_offset": 0, "char_limit": len(_SOURCE),
                "purpose": purpose,
            }, 1)
        return answer("Invalid purpose did not read the source.", 2)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id=f"invalid-reader-purpose-{type(purpose).__name__}",
            question="Assess this synthetic trial.", evidence_text=_SOURCE,
            agent_ids=["agent-synthesis"],
        ))
        copy_ref.update(host._control(run.run_id, "agent-synthesis")["body"]["source_refs"][0])
        drain(host, run.run_id)
        assert len(packets) == 2
        assert host.store.records("source_read_window") == []
        result = host.store.records("context_tool_result")[0]
        wrapper = json.loads(host.store.read_blob(result["body"]["blob"]["sha256"]))
        assert json.loads(wrapper["content"])["status"] == "rejected"
    finally:
        host.close()


def test_purpose_text_does_not_make_an_exact_repeat_new_reading_progress(
    tmp_path, monkeypatch,
) -> None:
    calls = []
    copy_ref = {}

    def transport(request, *, timeout):
        calls.append(json.loads(request.data))
        if len(calls) <= 2:
            return tool("read_source_window", {
                "source_id": copy_ref["id"], "source_version": copy_ref["version"],
                "block_offset": 0, "char_offset": 0, "char_limit": len(_SOURCE),
                "selection_mode": "replace",
                "purpose": ("Inspect this synthetic source." if len(calls) == 1
                            else "Recheck the same exact location."),
            }, len(calls))
        return answer("The repeated read adds no new source location.", 3)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="reader-purpose-repeat",
            question="Assess a synthetic source.", evidence_text=_SOURCE,
            agent_ids=["agent-synthesis"],
        ))
        copy_ref.update(host._control(run.run_id, "agent-synthesis")["body"]["source_refs"][0])
        drain(host, run.run_id)
        assert len(calls) == 3
        outcomes = [
            json.loads(json.loads(host.store.read_blob(item["body"]["blob"]["sha256"]))[
                "content"
            ]) for item in host.store.records("context_tool_result")
        ]
        assert len(outcomes) == 2
        repeated = next(item for item in outcomes
                        if item["progress_feedback"]["exact_window_repeated"])
        assert repeated["progress_feedback"]["new_checked_location_chars"] == 0
        assert repeated["progress_feedback"]["prior_window_ref"]
    finally:
        host.close()


def test_prepared_v1_reader_run_reopens_with_exact_original_schema(
    tmp_path, monkeypatch,
) -> None:
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: answer("unused", 1))
    try:
        run = host.start(LiveStartRequest(
            request_id="prepared-v1-reader", question="Assess a synthetic trial.",
            evidence_text=_SOURCE, agent_ids=["agent-synthesis"],
        ))
        saved_run = host._run(run.run_id)

        def saved_v1(tx):
            current = tx.get("mission_run", saved_run["id"])
            return tx.put("mission_run", current["id"], {
                key: value for key, value in current["body"].items()
                if key not in {"reader_action_purpose_tool_version", "peer_turn_tool_version"}
            }, expected_version=current["version"])

        host.store.transact("synthetic-saved-v1-reader", {}, saved_v1,
                            mission_id=saved_run["body"]["mission_id"])
        control = host._control(run.run_id, "agent-synthesis")
        assert host._schedule_model(control)
        current = host._control(run.run_id, "agent-synthesis")
        call = host.store.get(**current["body"]["call_ref"])
        intent = host.store.get(
            "model_request_intent", f"model-intent:{call['id']}:{call['version']}"
        )
        configuration = host.store.get(**intent["body"]["configuration_ref"])
        raw = host.store.read_blob(intent["body"]["blob"]["sha256"])
        assert call["body"]["permitted_tools"]["read_source_window"]["version"] == "1"
        assert "purpose" not in _reader(json.loads(raw))["parameters"]["properties"]
    finally:
        host.close()

    reopened = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: answer("unused", 1))
    try:
        control = reopened._control(run.run_id, "agent-synthesis")
        catalog, _ = reopened._tool_registry(control)
        assert catalog.tools["read_source_window"].version == "1"
        same = reopened.store.get("model_request_intent", intent["id"], intent["version"])
        assert reopened.store.read_blob(same["body"]["blob"]["sha256"]) == raw
        assert reopened.store.get(**same["body"]["configuration_ref"]) == configuration
        assert reopened.session.load(ref(same), for_dispatch=True) is not None
    finally:
        reopened.close()
