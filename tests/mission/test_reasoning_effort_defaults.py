"""Fresh medium missions and previously saved high missions keep their own settings."""

from __future__ import annotations

import json

import ora.mission.service as service_module
from ora.contracts.models import LiveFollowupRequest, LiveStartRequest
from tests.mission.test_service import drain, service, tool


def _prepared(host, run_id: str) -> tuple[dict, dict]:
    control = host._control(run_id, "agent-synthesis")
    assert host._schedule_model(control)
    call = host.store.get(**host._control(run_id, "agent-synthesis")["body"]["call_ref"])
    intent = host.store.get("model_request_intent", f"model-intent:{call['id']}:{call['version']}")
    packet = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
    return intent, packet


def test_fresh_mission_serializes_medium_and_reopen_keeps_saved_selection(
    tmp_path, monkeypatch,
) -> None:
    request = LiveStartRequest(
        request_id="fresh-medium-effort", question="Inspect a synthetic source",
        agent_ids=["agent-synthesis"],
    )
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    try:
        run_id = host.start(request).run_id
        assert host._run(run_id)["body"]["profile"]["reasoningSettings"] == {
            "reasoning_effort": "medium", "include_reasoning": True,
        }
        intent, packet = _prepared(host, run_id)
        assert packet["reasoning_effort"] == "medium"
        assert packet["include_reasoning"] is True
        assert intent["body"]["effective_reasoning_effort"] == "medium"
        exact_ref = {key: intent[key] for key in ("kind", "id", "version")}
        exact_hash = intent["body"]["request_sha256"]
    finally:
        host.close()

    reopened = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    try:
        assert reopened.start(request).reused is True
        assert reopened.store.get(**exact_ref)["body"]["request_sha256"] == exact_hash
        assert reopened.session.load(exact_ref).request.body["reasoning_effort"] == "medium"
        assert reopened._run(run_id)["body"]["profile"]["reasoningSettings"][
            "reasoning_effort"
        ] == "medium"
    finally:
        reopened.close()


def test_existing_high_run_and_its_followup_keep_high_after_default_changes(
    tmp_path, monkeypatch,
) -> None:
    packets = []

    def transport(request, *, timeout):
        packets.append(json.loads(request.data))
        return tool("publish_result", {
            "title": "Synthetic finding", "text": "Saved synthetic result.",
            "citations": [], "limitations": ["Synthetic fixture"],
        }, len(packets))

    host = service(tmp_path, monkeypatch, transport)
    try:
        parent_request = LiveStartRequest(
            request_id="saved-high-parent", question="Produce a synthetic finding",
            agent_ids=["agent-synthesis"],
        )
        with monkeypatch.context() as prior_default:
            prior_default.setattr(service_module, "DEFAULT_REASONING_EFFORT", "high")
            parent_id = host.start(parent_request).run_id
        assert service_module.DEFAULT_REASONING_EFFORT == "medium"
        assert host.start(parent_request).reused is True
        intent, prepared = _prepared(host, parent_id)
        assert prepared["reasoning_effort"] == "high"
        assert intent["body"]["effective_reasoning_effort"] == "high"
        drain(host, parent_id)
        assert packets and packets[0]["reasoning_effort"] == "high"
        snapshot = host.state(parent_id).snapshot
        target = snapshot.artifacts[0]
        child = host.followup(LiveFollowupRequest(
            request_id="saved-high-followup", parent_run_id=parent_id,
            parent_snapshot_id=snapshot.snapshot_id,
            target_id=target.id, target_version=target.version,
            question="Review the saved synthetic finding",
            agent_ids=["agent-synthesis"],
        ))
        assert host._run(child.run_id)["body"]["profile"]["reasoningSettings"] == {
            "reasoning_effort": "high", "include_reasoning": True,
        }
        _intent, child_packet = _prepared(host, child.run_id)
        assert child_packet["reasoning_effort"] == "high"
        assert child_packet["include_reasoning"] is True
    finally:
        host.close()
