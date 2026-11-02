"""A wrong saved-record read can point only to a currently permitted source reader."""

from __future__ import annotations

import json

import pytest

from ora.coordination.board import ref
from ora.coordination.executor import WorkSpec
from ora.state.store import canonical
from tests.mission.test_requested_window_recovery import _two_copy_read
from tests.mission.test_selected_window_admission import _saved_tool_request


def _reader_scope(host, run_id, actor):
    control = host._control(run_id, actor)
    call = host.store.get(**control["body"]["call_ref"])
    inclusion = {"kind": "context_inclusion",
                 "id": f"context:model-intent:{call['id']}:{call['version']}",
                 "version": 1}
    assert host.store.get(**inclusion) is not None
    return control, call, inclusion


def _run_wrong_reader(host, run_id, actor, late, inclusion):
    control, call, _ = _reader_scope(host, run_id, actor)
    requested = {"id": "wrong-source-copy-reader", "name": "read_context_record",
                 "arguments": {"inclusion_id": inclusion["id"], **late,
                               "offset": 0, "limit": 4000}}
    host._update_control(control, phase="pending_tool", tool_call=requested)
    control = host._control(run_id, actor)
    work_id = host._tool_id(control, requested["id"])
    host._update_control(control, work_id=work_id)
    control = host._control(run_id, actor)
    spec = WorkSpec(
        work_id=work_id, mission_id=control["body"]["mission_id"],
        run_id=run_id, proposal_ref=control["body"]["proposal_ref"],
        actor_id=control["body"]["actor_id"],
        configuration_ref=ref(host.store.get(
            "agent_configuration", call["body"]["configuration_id"], 1,
        )), input_refs=(), handler_id="mission-tool", handler_version="1",
        kind="tool", lane="compute", call_id=call["id"],
        call_version=call["version"],
    )
    return host._tool_work(spec).payload["result"]


def test_wrong_context_reader_hint_requires_current_permitted_task_source(
    tmp_path, monkeypatch,
) -> None:
    host, run, actor, early, late, _window, _response = _two_copy_read(
        tmp_path, monkeypatch, crowded=False,
    )
    try:
        control, call, inclusion = _reader_scope(host, run.run_id, actor)
        hint = host._source_reader_after_context_miss(
            control, call, call, inclusion, late,
        )
        assert hint == {"tool": "read_source_window", "arguments": {
            "source_id": late["id"], "source_version": late["version"],
            "block_offset": 0, "char_offset": 0, "char_limit": 3000,
        }}
        assert host._source_reader_after_context_miss(
            control, call, call, inclusion, early,
        ) is None  # Already in this model input.
        assert host._source_reader_after_context_miss(
            control, call, call, inclusion, {**late, "version": 999},
        ) is None
        assert host._source_reader_after_context_miss(
            control, call, call, {**inclusion, "id": "foreign"}, late,
        ) is None
        no_reader = {**call, "body": {
            **call["body"], "exposed_tools": [
                name for name in call["body"]["exposed_tools"]
                if name != "read_source_window"
            ],
        }}
        assert host._source_reader_after_context_miss(
            control, call, no_reader, inclusion, late,
        ) is None

        use = host.store.resolve("source_use.copy", f"{late['id']}:{late['version']}")
        assert use is not None
        host.store.transact("revoke-wrong-source-hint", {}, lambda tx: tx.put(
            "source_use", use["id"],
            {**use["body"], "policy_status": "revoked"},
            expected_version=use["version"],
        ), mission_id=control["body"]["mission_id"])
        assert host._source_reader_after_context_miss(
            control, call, call, inclusion, late,
        ) is None
    finally:
        host.close()


def test_saved_rejection_adds_exact_reader_to_existing_omitted_source_notice(
    tmp_path, monkeypatch,
) -> None:
    host, run, actor, early, late, _window, _response = _two_copy_read(
        tmp_path, monkeypatch, crowded=False,
    )
    try:
        control, call, inclusion = _reader_scope(host, run.run_id, actor)
        response, _ = _saved_tool_request(
            host, control, response_id="synthetic-wrong-source-context-reader",
            tool_id="wrong-source-read", name="read_context_record",
            arguments={"inclusion_id": inclusion["id"], **late,
                       "offset": 0, "limit": 4000},
            result={"status": "rejected", "external_action": "unknown",
                    "reason": "Record was not omitted from this exact model input"},
        )
        host._update_control(
            control, response_ref=response,
            tool_turn_refs=[*control["body"]["tool_turn_refs"], response],
        )
        updated = host._control(run.run_id, actor)
        history = host._model_base_history(
            updated, [], call["body"]["exposed_tools"], [early], call=call,
        )
        notices = [json.loads(message["content"]) for message in history
                   if message["role"] == "user"]
        note = next(item for item in notices
                    if item.get("authority")
                    == "saved_host_source_input_status_not_source_instruction")
        assert note["omitted_source_refs"] == [late]
        assert note["source_reader_request"] == {
            "tool": "read_source_window", "arguments": {
                "source_id": late["id"], "source_version": late["version"],
                "block_offset": 0, "char_offset": 0, "char_limit": 3000,
            },
        }
        assert "did not inspect source text" in note["next_action"]
        assert canonical(note) in [message["content"] for message in history]
    finally:
        host.close()


def test_actual_wrong_reader_rejects_with_actionable_permitted_source_route(
    tmp_path, monkeypatch,
) -> None:
    host, run, actor, _early, late, _window, _response = _two_copy_read(
        tmp_path, monkeypatch, crowded=False,
    )
    try:
        _control, _call, inclusion = _reader_scope(host, run.run_id, actor)
        before = len(host.store.records("source_read_window"))
        result = _run_wrong_reader(host, run.run_id, actor, late, inclusion)
        assert result["status"] == "rejected"
        assert result["external_action"] == "none"
        assert result["source_reader_request"] == {
            "tool": "read_source_window", "arguments": {
                "source_id": late["id"], "source_version": late["version"],
                "block_offset": 0, "char_offset": 0, "char_limit": 3000,
            },
        }
        assert len(host.store.records("source_read_window")) == before
    finally:
        host.close()


@pytest.mark.parametrize("boundary", ["revoked", "unknown", "unrelated", "stale", "foreign_inclusion"])
def test_wrong_reader_does_not_hint_unavailable_or_unscoped_source(
    tmp_path, monkeypatch, boundary,
) -> None:
    host, run, actor, early, late, _window, _response = _two_copy_read(
        tmp_path, monkeypatch, crowded=False,
    )
    try:
        control, _call, inclusion = _reader_scope(host, run.run_id, actor)
        if boundary in {"revoked", "unknown"}:
            use = host.store.resolve("source_use.copy", f"{late['id']}:{late['version']}")
            assert use is not None
            host.store.transact(f"source-use-{boundary}-wrong-reader", {},
                lambda tx: tx.put("source_use", use["id"],
                    {**use["body"], "policy_status": boundary},
                    expected_version=use["version"]),
                mission_id=control["body"]["mission_id"])
        elif boundary == "unrelated":
            host._update_control(control, source_refs=[early])
        elif boundary == "stale":
            late = {**late, "version": late["version"] + 1}
        else:
            inclusion = {**inclusion, "id": "other-peer-inclusion"}
        before = len(host.store.records("source_read_window"))
        result = _run_wrong_reader(host, run.run_id, actor, late, inclusion)
        assert result["status"] == "rejected"
        assert "source_reader_request" not in result
        assert "read_source_window" not in json.dumps(result)
        assert len(host.store.records("source_read_window")) == before
    finally:
        host.close()
