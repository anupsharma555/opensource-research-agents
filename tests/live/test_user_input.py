"""Exercise authenticated user-input ingress through the real no-listener handler."""

import argparse
import json
import urllib.request
from io import BytesIO
from types import SimpleNamespace

from ora.contracts.models import LiveStartRequest, LiveUserInputReceipt
from ora.live import cli
from ora.live.engine import LiveError
from tests.live.test_server import _post_without_listener
from tests.mission.test_service import service

PATH = "/api/live/user-inputs"
AUTH = {"Authorization": "Bearer synthetic-token"}


def _body():
    return {
        "requestId": "synthetic-user-input-1", "runId": "run-1", "taskId": "task-1",
        "targetAgentId": "agent-instance-1", "expectedTaskVersion": 2,
        "kind": "direction", "text": "Check the synthetic denominator before concluding.",
    }


class _StubInputHost:
    def __init__(self):
        self.accepted = []

    def submit_user_input(self, command):
        if command.request_id == "conflict":
            raise LiveError("Request identity conflict", 409)
        if command.target_agent_id == "foreign-agent":
            raise LiveError("Target agent is unavailable", 404)
        self.accepted.append(command)
        return LiveUserInputReceipt(
            request_id=command.request_id, run_id=command.run_id,
            task_id=command.task_id, target_agent_id=command.target_agent_id,
            status="queued_for_next_boundary",
            input_ref={"kind": "mission_user_input", "id": "input-1", "version": 1},
            task_ref={"kind": "board_proposal", "id": command.task_id, "version": 2},
        )


def test_user_input_route_requires_local_auth_and_rejects_spoofed_fields():
    host = _StubInputHost()
    body = _body()

    def post(payload, headers=None):
        return _post_without_listener(host, PATH, payload, headers=headers)

    assert post(body)[0] == 401
    cookie = {"Cookie": "ora_session=synthetic-token"}
    assert post(body, cookie)[0] == 403
    assert post(body, {**cookie, "Origin": "https://untrusted.invalid"})[0] == 403
    assert post(body, {"Authorization": "Bearer incorrect"})[0] == 401
    assert post(body, {**AUTH, "Host": "untrusted.invalid"})[0] == 403
    for changed in (
        {"origin": "research_peer"}, {"senderId": "agent-instance-2"},
        {"evidenceRefs": []}, {"sourceUse": "granted"},
        {"targetAgentId": ""}, {"expectedTaskVersion": 0},
    ):
        assert post({**body, **changed}, AUTH)[0] == 400
    assert host.accepted == []

    assert post({**body, "requestId": "conflict"}, AUTH)[0] == 409
    assert post({**body, "targetAgentId": "foreign-agent"}, AUTH)[0] == 404
    status, headers, receipt = post(body, AUTH)
    assert status == 202 and headers["Cache-Control"] == "no-store"
    assert receipt["status"] == "queued_for_next_boundary"
    assert receipt["inputRef"] == {
        "kind": "mission_user_input", "id": "input-1", "version": 1,
    }
    cookie_status, _, cookie_receipt = post(
        {**body, "requestId": "synthetic-user-input-2"},
        {**cookie, "Origin": "http://127.0.0.1:34567"},
    )
    assert cookie_status == 202
    assert cookie_receipt["requestId"] == "synthetic-user-input-2"
    assert len(host.accepted) == 2


def test_user_input_route_is_explicitly_unavailable_on_legacy_host():
    status, _, response = _post_without_listener(
        SimpleNamespace(), PATH, _body(), headers=AUTH,
    )
    assert status == 501
    assert "unavailable" in response["error"].lower()


def test_terminal_input_sends_exact_text_and_target_to_authenticated_route(
    tmp_path, capsys, monkeypatch,
):
    parser = argparse.ArgumentParser()
    cli.configure_mission(parser)
    parsed = parser.parse_args(["input", "--input", str(tmp_path / "input.json")])
    assert parsed.mission_action == "input"
    monkeypatch.setattr(cli, "_connection", lambda _state: {
        "origin": "http://127.0.0.1:34567", "token": "synthetic-token",
    })
    captured = []

    class Opener:
        def open(self, request, timeout):
            captured.append((request, timeout))
            return BytesIO(json.dumps({"status": "queued_for_next_boundary"}).encode())

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_args: Opener())
    command = tmp_path / "input.json"
    command.write_text(json.dumps(_body()))
    cli.mission_main(argparse.Namespace(
        mission_action="input", state_dir=tmp_path, input=command,
    ), tmp_path)
    assert json.loads(capsys.readouterr().out)["status"] == "queued_for_next_boundary"
    assert len(captured) == 1
    sent, timeout = captured[0]
    assert sent.full_url == "http://127.0.0.1:34567/api/live/user-inputs"
    assert sent.get_header("Authorization") == "Bearer synthetic-token"
    assert sent.get_header("Content-type") == "application/json"
    assert json.loads(sent.data) == _body()
    assert timeout == 15


def test_authenticated_route_persists_exact_target_and_replays_after_reopen(
    tmp_path, monkeypatch,
):
    model_attempts = []

    def no_model_call(*_args, **_kwargs):
        model_attempts.append("unexpected model transport")
        raise AssertionError("The surface acceptance test dispatched a model call")

    host = service(tmp_path, monkeypatch, no_model_call)
    reopened = None
    try:
        run = host.start(LiveStartRequest(
            request_id="synthetic-surface-start", question="Inspect a synthetic count",
            evidence_text="Synthetic sample count: 2 of 4.",
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        task = host.store.get("board_proposal", control["body"]["task_id"])
        body = {
            **_body(), "runId": run.run_id, "taskId": task["id"],
            "targetAgentId": control["body"]["actor_id"],
            "expectedTaskVersion": task["version"],
            "kind": "unverified_pasted_evidence",
        }
        source_count = len(host.store.records("source_copy"))
        assert _post_without_listener(host, PATH, {**body, "taskId": "foreign-task"},
                                      headers=AUTH)[0] in {404, 409}
        assert _post_without_listener(host, PATH, {**body, "expectedTaskVersion":
                                      task["version"] + 1}, headers=AUTH)[0] == 409
        assert _post_without_listener(host, PATH, {**body, "targetAgentId":
                                      "foreign-agent"}, headers=AUTH)[0] in {404, 409}
        assert host.store.records("mission_user_input") == []
        status, _, first = _post_without_listener(host, PATH, body, headers=AUTH)
        assert status == 202
        assert first["status"] == "queued_for_next_boundary"
        assert first["reused"] is False
        saved = host.store.get(**first["inputRef"])
        assert saved["body"]["text"] == body["text"]
        assert saved["body"]["kind"] == body["kind"]
        assert saved["body"]["task_ref"]["id"] == body["taskId"]
        assert saved["body"]["actor_id"] == body["targetAgentId"]
        assert saved["body"]["origin"] == "authenticated_local_user"
        assert len(host.store.records("mission_user_input")) == 1
        assert len(host.store.records("source_copy")) == source_count
        host.close()
        reopened = service(tmp_path, monkeypatch, no_model_call)
        status, _, replay = _post_without_listener(reopened, PATH, body, headers=AUTH)
        assert status == 202 and replay["reused"] is True
        assert replay["inputRef"] == first["inputRef"]
        assert _post_without_listener(reopened, PATH, {**body, "text": "Changed"},
                                      headers=AUTH)[0] == 409
        assert len(reopened.store.records("mission_user_input")) == 1
        assert reopened.store.records("model_request_intent") == []
        assert reopened.store.records("model_response_receipt") == []
        assert model_attempts == []
    finally:
        (reopened or host).close()
