"""Offline local route and terminal request for whole-run cancellation."""

import argparse
import json
import urllib.request
from io import BytesIO
from types import SimpleNamespace

from ora.contracts.models import LiveRunCancelReceipt, LiveStartRequest
from ora.live import cli
from tests.live.test_server import _post_without_listener
from tests.mission.test_service import service


class RunHost:
    def __init__(self):
        self.commands = []

    def cancel_run(self, request):
        self.commands.append(request)
        return LiveRunCancelReceipt(
            request_id=request.request_id, run_id=request.run_id,
            status="cancelled",
            cancellation_ref={"kind": "mission_run_cancellation", "id": "cancel-1", "version": 1},
            run_ref={"kind": "mission_run", "id": request.run_id, "version": 2},
        )


def test_run_cancel_route_reuses_existing_local_auth():
    host = RunHost()
    body = {"requestId": "stop-1", "runId": "run-1", "expectedRunVersion": 1,
            "reason": "Stop synthetic run"}
    route = "/api/live/run-cancellations"
    def post(payload, headers=None):
        return _post_without_listener(host, route, payload, headers=headers)
    assert post(body)[0] == 401
    assert post(body, {"Cookie": "ora_session=synthetic-token"})[0] == 403
    assert post(body, {"Authorization": "Bearer wrong"})[0] == 401
    auth = {"Authorization": "Bearer synthetic-token"}
    assert post({**body, "expectedRunVersion": 0}, auth)[0] == 400
    assert post({**body, "extra": True}, auth)[0] == 400
    assert host.commands == []
    status, _, receipt = post(body, auth)
    assert status == 202
    assert receipt["cancellationRef"]["kind"] == "mission_run_cancellation"
    assert receipt["runRef"] == {"kind": "mission_run", "id": "run-1", "version": 2}
    assert len(host.commands) == 1
    legacy_status, _, legacy = _post_without_listener(
        SimpleNamespace(), route, body, headers=auth,
    )
    assert legacy_status == 501
    assert legacy["error"] == "Run cancellation is unavailable for this local runtime."


def test_terminal_run_cancel_uses_exact_authenticated_route(tmp_path, capsys, monkeypatch):
    parser = argparse.ArgumentParser()
    cli.configure_mission(parser)
    parsed = parser.parse_args(["cancel-run", "--input", str(tmp_path / "stop.json")])
    assert parsed.mission_action == "cancel-run"
    monkeypatch.setattr(cli, "_connection", lambda _state: {
        "origin": "http://127.0.0.1:34567", "token": "synthetic-token",
    })
    captured = []

    class Opener:
        def open(self, request, timeout):
            captured.append((request, timeout))
            return BytesIO(json.dumps({"status": "cancelled"}).encode())

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_args: Opener())
    command = tmp_path / "stop.json"
    command.write_text(json.dumps({
        "requestId": "stop-1", "runId": "run-1", "expectedRunVersion": 2,
        "reason": "Stop synthetic run",
    }))
    cli.mission_main(argparse.Namespace(
        mission_action="cancel-run", state_dir=tmp_path, input=command,
    ), tmp_path)
    assert json.loads(capsys.readouterr().out)["status"] == "cancelled"
    sent, timeout = captured[0]
    assert sent.full_url == "http://127.0.0.1:34567/api/live/run-cancellations"
    assert sent.get_header("Authorization") == "Bearer synthetic-token"
    assert json.loads(sent.data)["expectedRunVersion"] == 2
    assert timeout == 15


def test_real_handler_saves_run_stop_and_replays_receipt(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    try:
        run = host.start(LiveStartRequest(
            request_id="handler-run-stop", question="Synthetic handler integration",
            agent_ids=["agent-synthesis"],
        ))
        summary = next(item for item in host.state(run.run_id).runs if item.id == run.run_id)
        body = {
            "requestId": "handler-stop-1", "runId": run.run_id,
            "expectedRunVersion": summary.version,
            "reason": "Stop remaining synthetic work",
        }
        auth = {"Authorization": "Bearer synthetic-token"}
        route = "/api/live/run-cancellations"
        status, _, receipt = _post_without_listener(host, route, body, headers=auth)
        assert status == 202
        assert receipt["runRef"]["id"] == run.run_id
        assert host.store.get("mission_run_cancellation", "handler-stop-1") is not None
        repeated_status, _, repeated = _post_without_listener(host, route, body, headers=auth)
        assert repeated_status == 202
        assert repeated == {**receipt, "reused": True}
        assert host.state(run.run_id).snapshot.mission.status == "cancelled"
        assert host.state(run.run_id).runs[0].status == "cancelled"
    finally:
        host.close()
