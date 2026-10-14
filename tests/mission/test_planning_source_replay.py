"""Synthetic source-use checks for prose accompanying saved planning reads."""

from __future__ import annotations

import json

import pytest

from ora.contracts.models import LiveStartRequest
from tests.mission.test_planning_integration import _drive_until, _plan, _scripted
from tests.mission.test_service import answer, drain, service, tool, two_supplied_sources


def _transitive_basis(host, run_id, text):
    direct, transitive = two_supplied_sources(host, run_id)
    control = host._control(run_id, "agent-synthesis")

    def grant(tx):
        use = tx.put("source_use", "planning-note-only-source-use", {
            "copy_ref": transitive, "allowed_uses": ["model_input"],
            "policy_status": "granted",
        })
        tx.bind("source_use.copy", f"{transitive['id']}:{transitive['version']}",
                "source_use", use["id"])
        return {}

    host.store.transact("planning-transitive-source-grant", {}, grant,
                        mission_id=control["body"]["mission_id"])
    # A checked note can carry a dependency absent from the peer's direct
    # source selection, as can already-read shared research material.
    host._update_control(control, source_refs=[direct])
    host.notes.write(
        "planning-transitive-basis", mission_id=control["body"]["mission_id"],
        actor_id=control["body"]["actor_id"], task_ref=control["body"]["proposal_ref"],
        note_id="planning-transitive-basis", category="note", text=text,
        input_refs=[transitive],
    )
    return direct, transitive


def _revoke_transitive(host, mission_id):
    def revoke(tx):
        old = tx.get("source_use", "planning-note-only-source-use")
        tx.put("source_use", old["id"], {
            **old["body"], "allowed_uses": [], "policy_status": "restricted",
        }, expected_version=old["version"])
        return {}

    host.store.transact("planning-transitive-source-revocation", {}, revoke,
                        mission_id=mission_id)


@pytest.mark.parametrize("assistant_prose", [False, True])
def test_read_plan_replay_masks_transitive_source_and_preserves_tool_pair(
    tmp_path, monkeypatch, assistant_prose,
):
    """Revoking a note-only source must not rely on direct-source archival."""
    secret = "SYNTHETIC_WITHDRAWN_PLAN_ASSISTANT_PROSE_927"
    packets = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        step = len(packets)
        if step == 1:
            return tool("activate_skill", {"skill_id": "task-planning"}, step)
        if step == 2:
            assert secret in request.data.decode()
            return tool("save_plan", {"plan": _plan(goal=secret)}, step)
        if step == 3:
            assert secret in request.data.decode()
            response = tool("read_plan", {"version": 1}, step)
            if assistant_prose:
                response.payload["choices"][0]["message"]["content"] = secret
            return response
        assert step == 4
        assert secret not in request.data.decode()
        result_position = next(
            position for position, message in enumerate(packet["messages"])
            if message.get("role") == "tool" and message.get("name") == "read_plan"
        )
        previous = packet["messages"][result_position - 1]
        result = packet["messages"][result_position]
        assert previous["role"] == "assistant"
        assert previous["content"] is None
        assert previous["tool_calls"][0]["id"] == result["tool_call_id"]
        assert json.loads(previous["tool_calls"][0]["function"]["arguments"]) == {"version": 1}
        projected = json.loads(result["content"])
        assert "text" not in projected["note"]["body"]
        return answer("The withdrawn source remains unavailable for this synthetic task.", step)

    transport = _scripted(transport)
    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id=f"transitive-plan-prose-{assistant_prose}",
            question="Continue a synthetic source-bound plan",
            evidence_text="A separate permitted direct task source remains available.",
            agent_ids=["agent-synthesis"],
        ))
        direct, transitive = _transitive_basis(host, run.run_id, secret)
        _drive_until(host, run.run_id, lambda: (
            len(packets) == 3
            and host._control(run.run_id, "agent-synthesis")["body"]["phase"] == "ready_model"
        ))
        control = host._control(run.run_id, "agent-synthesis")
        assert control["body"]["source_refs"] == [direct]
        response = host.store.get(**control["body"]["tool_turn_refs"][-1])
        call = host.store.get(**response["body"]["call_ref"])
        intent = host.store.get("model_request_intent", f"model-intent:{call['id']}:{call['version']}")
        assert transitive in intent["body"]["context_source_dependency_refs"]
        immutable_response = host.store.read_blob(response["body"]["blob"]["sha256"])

        _revoke_transitive(host, control["body"]["mission_id"])
        drain(host, run.run_id, limit=10)
        assert not transport.failures
        assert len(packets) == 4
        assert host.state(run.run_id).snapshot.mission.status == "completed"
        assert host.store.read_blob(response["body"]["blob"]["sha256"]) == immutable_response
    finally:
        host.close()


def test_prepared_read_plan_assistant_prose_rechecks_transitive_use_before_dispatch(
    tmp_path, monkeypatch,
):
    """An absent plan read can still return prose from earlier permitted notes."""
    secret = "SYNTHETIC_WITHDRAWN_PREPARED_PLAN_PROSE_638"
    packets = []

    def transport(request, *, timeout):
        packets.append(json.loads(request.data))
        step = len(packets)
        if step == 1:
            return tool("activate_skill", {"skill_id": "task-planning"}, step)
        assert step == 2 and secret in request.data.decode()
        response = tool("read_plan", {}, step)
        response.payload["choices"][0]["message"]["content"] = secret
        return response

    transport = _scripted(transport)
    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="prepared-transitive-plan-prose",
            question="Inspect whether a saved plan exists",
            evidence_text="A permitted direct task source remains available.",
            agent_ids=["agent-synthesis"],
        ))
        direct, transitive = _transitive_basis(host, run.run_id, secret)
        _drive_until(host, run.run_id, lambda: (
            len(packets) == 2
            and host._control(run.run_id, "agent-synthesis")["body"]["phase"] == "ready_model"
        ))
        control = host._control(run.run_id, "agent-synthesis")
        assert control["body"]["source_refs"] == [direct]
        # Model preparation can omit older note details. Isolate the remaining
        # saved assistant prose, with no visible note text or plan-read text to
        # make the existing working-note dispatch guard catch this incident.
        original_heads = host._heads
        monkeypatch.setattr(host, "_heads", lambda kind, mission_id=None: (
            [] if kind == "working_note" else original_heads(kind, mission_id)
        ))
        assert host._schedule_model(control)
        pending = host._control(run.run_id, "agent-synthesis")
        work = host._work_for(pending)
        spec = host.executor._spec_from(work)
        intent_ref = host._intent_for(spec)
        intent = host.store.get(**intent_ref)
        prepared = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        assert secret in json.dumps(prepared)
        assert not host.context.prepared_working_note_restrictions(intent_ref)
        assert transitive in intent["body"]["context_source_dependency_refs"]
        _revoke_transitive(host, control["body"]["mission_id"])
        result = host._model_work(spec)
        assert result.disposition == "terminal"
        assert result.payload["status"] == "source_use_restricted"
        assert result.payload["external_action"] == "none"
        assert result.payload["copy_refs"] == [transitive]
        assert len(packets) == 2
        assert not transport.failures
    finally:
        host.close()
