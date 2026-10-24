"""Exercise real loopback HTTP boundaries without calling a model.

The tests distinguish session setup, accepted commands and later saved state,
and verify that conflicting requests keep their precise error status.
"""

import argparse
import json
import threading
import urllib.error
import urllib.request
from email.message import Message
from io import BytesIO
from types import SimpleNamespace

import pytest

from ora.contracts.models import LiveBranchCancelReceipt, LiveReceipt, LiveState
from ora.live import cli
from ora.live.engine import LiveError
from ora.live.server import LocalResearchServer, ResearchHandler


class StubHost:
    def __init__(self):
        self.started = []
        self.reasoning_reads = []
        self.peer_reads = []
        self.branch_cancellations = []

    def start(self, request):
        if request.request_id == "conflict":
            raise LiveError("Request identity conflict", 409)
        self.started.append(request)
        return LiveReceipt(request_id=request.request_id, run_id="run-1", task_id="task-1")

    def followup(self, request):
        raise LiveError("Unknown saved parent", 404)

    def cancel_branch(self, request):
        if request.run_id != "run-1":
            raise LiveError("Run is unavailable", 404)
        if request.task_id != "task-1":
            raise LiveError("Task is unavailable in this run", 404)
        if request.expected_task_version != 2:
            raise LiveError("Task version changed", 409)
        if request.request_id == "conflict":
            raise LiveError("Request identity conflict", 409)
        self.branch_cancellations.append(request)
        return LiveBranchCancelReceipt(
            request_id=request.request_id, run_id=request.run_id, task_id=request.task_id,
            status="cancelled",
            cancellation_ref={"kind": "mission_branch_cancellation", "id": "cancel-1", "version": 1},
            task_ref={"kind": "board_proposal", "id": request.task_id, "version": 2},
        )

    def state(self, run_id=None):
        if run_id == "missing":
            raise LiveError("Unknown run", 404)
        return LiveState(enabled=True, model="test-provider", agents=[], runs=[])

    def calls(self, run_id, attempt_id=None):
        return [{"runId": run_id, "attemptId": attempt_id, "content": "saved output"}]

    def returned_reasoning(self, run_id, call_id, *, diagnostic_granted=False):
        assert diagnostic_granted is True
        self.reasoning_reads.append((run_id, call_id))
        return {
            "runId": run_id, "callId": call_id, "agentId": "agent-1",
            "attemptId": "attempt-1", "status": "available",
            "text": "SYNTHETIC_RETURNED_REASONING", "origin": "provider_returned",
            "redacted": False, "reason": "returned",
        }

    def peer_record(self, run_id, kind, record_id, version):
        self.peer_reads.append((run_id, kind, record_id, version))
        return {
            "runId": run_id, "originRunId": run_id,
            "reference": {"kind": kind, "id": record_id, "version": version},
            "status": "available", "content": {"text": "Synthetic saved message"},
            "contentReference": {"kind": kind, "id": record_id, "version": version},
            "redacted": False,
        }


@pytest.fixture
def server(tmp_path):
    (tmp_path / "index.html").write_text("<html>Local app</html>")
    host = StubHost()
    app = LocalResearchServer(host, tmp_path)
    thread = threading.Thread(target=app.serve_forever, daemon=True)
    thread.start()
    try:
        yield app, host
    finally:
        app.shutdown()
        app.server_close()
        thread.join()


def request(app, path, body=None, headers=None):
    options = {"Content-Type": "application/json", **(headers or {})}
    raw = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(app.origin + path, data=raw, headers=options)
    try:
        response = urllib.request.urlopen(req, timeout=3)
    except urllib.error.HTTPError as error:
        response = error
    with response:
        data = response.read().decode()
        return response.status, response.headers, json.loads(data) if data.startswith("{") else data


def test_session_and_read_do_not_start_work(server):
    app, host = server
    assert request(app, "/live")[0] == 200
    assert request(app, "/api/live/state")[0] == 401
    status, headers, _ = request(app, "/api/live/session", {"token": app.token},
                                 {"Origin": app.origin})
    assert status == 200
    cookie = headers["Set-Cookie"]
    assert "HttpOnly" in cookie and "SameSite=Strict" in cookie
    assert request(app, "/api/live/state", headers={"Cookie": cookie.split(";", 1)[0]})[0] == 200
    assert not host.started
    assert "no-referrer" == headers["Referrer-Policy"]


def test_only_authenticated_local_commands_are_accepted(server):
    app, host = server
    body = {"requestId": "new", "question": "Explain the supplied counts", "evidenceText": "2/4",
            "agentIds": ["agent-synthesis"]}
    assert request(app, "/api/live/start", body)[0] == 401
    cookie = {"Cookie": f"ora_session={app.token}"}
    assert request(app, "/api/live/start", body, cookie)[0] == 403
    assert request(app, "/api/live/start", body,
                   {**cookie, "Authorization": "Bearer incorrect"})[0] == 401
    assert request(app, "/api/live/start", body,
                   {**cookie, "Origin": "https://untrusted.invalid"})[0] == 403
    status, _, saved = request(app, "/api/live/start", body, {**cookie, "Origin": app.origin})
    assert status == 202 and saved["runId"] == "run-1"
    assert len(host.started) == 1
    assert request(app, "/api/live/start", body,
                   {"Authorization": "Bearer " + app.token, "Host": "untrusted.invalid"})[0] == 403


def test_typed_errors_and_private_paths(server):
    app, _ = server
    auth = {"Authorization": "Bearer " + app.token}
    assert request(app, "/api/live/state?runId=missing", headers=auth)[0] == 404
    body = {"requestId": "conflict", "question": "Explain", "evidenceText": "2/4",
            "agentIds": ["agent-synthesis"]}
    assert request(app, "/api/live/start", body, auth)[0] == 409
    assert request(app, "/api/live/start", {"requestId": "invalid"}, auth)[0] == 400
    assert request(app, "/api/live/calls", headers=auth)[0] == 400
    assert request(app, "/api/live/calls?runId=run-1&attemptId=attempt-1", headers=auth)[2]["records"][0]["attemptId"] == "attempt-1"
    assert request(app, "/%2e%2e/.env")[0] == 404


def _post_without_listener(host, path, body, *, token="synthetic-token", headers=None):
    """Call the real request handler with in-memory HTTP streams."""
    origin = "http://127.0.0.1:34567"
    request_headers = Message()
    for name, value in {
        "Host": "127.0.0.1:34567", "Content-Type": "application/json",
        **(headers or {}),
    }.items():
        request_headers[name] = value
    raw = json.dumps(body).encode()
    request_headers["Content-Length"] = str(len(raw))
    handler = object.__new__(ResearchHandler)
    handler.server = SimpleNamespace(origin=origin, token=token, research_host=host)
    handler.headers = request_headers
    handler.path = path
    handler.rfile = BytesIO(raw)
    handler.wfile = BytesIO()
    response = {}
    handler.send_response = lambda status: response.update(status=status)
    handler.send_header = lambda name, value: response.setdefault("headers", {}).update({name: value})
    handler.end_headers = lambda: None
    handler.do_POST()
    return response["status"], response["headers"], json.loads(handler.wfile.getvalue())


def test_branch_cancellation_uses_authenticated_exact_task_route():
    host = StubHost()
    body = {
        "requestId": "cancel-1", "runId": "run-1", "taskId": "task-1",
        "expectedTaskVersion": 2, "reason": "Stop this selected branch",
    }
    path = "/api/live/branch-cancellations"

    def post(payload, headers=None):
        return _post_without_listener(host, path, payload, headers=headers)

    assert post(body)[0] == 401
    cookie = {"Cookie": "ora_session=synthetic-token"}
    assert post(body, cookie)[0] == 403
    assert post(body, {**cookie, "Origin": "https://untrusted.invalid"})[0] == 403
    assert post(body, {"Authorization": "Bearer incorrect"})[0] == 401
    auth = {"Authorization": "Bearer synthetic-token"}
    assert post(body, {**auth, "Host": "untrusted.invalid"})[0] == 403
    assert post({**body, "expectedTaskVersion": 0}, auth)[0] == 400
    assert post({**body, "unexpected": True}, auth)[0] == 400
    assert post({**body, "runId": "other-run"}, auth)[0] == 404
    assert post({**body, "taskId": "other-task"}, auth)[0] == 404
    assert post({**body, "expectedTaskVersion": 1}, auth)[0] == 409
    assert post({**body, "requestId": "conflict"}, auth)[0] == 409
    assert host.branch_cancellations == []
    status, headers, receipt = post(body, auth)
    assert status == 202 and headers["Cache-Control"] == "no-store"
    assert receipt == {
        "requestId": "cancel-1", "runId": "run-1", "taskId": "task-1",
        "status": "cancelled", "reused": False,
        "cancellationRef": {"kind": "mission_branch_cancellation", "id": "cancel-1", "version": 1},
        "taskRef": {"kind": "board_proposal", "id": "task-1", "version": 2},
    }
    assert len(host.branch_cancellations) == 1


def test_branch_cancel_is_explicitly_unavailable_on_legacy_host():
    body = {
        "requestId": "cancel-1", "runId": "run-1", "taskId": "task-1",
        "expectedTaskVersion": 2, "reason": "Stop this selected branch",
    }
    status, _, response = _post_without_listener(
        SimpleNamespace(), "/api/live/branch-cancellations", body,
        headers={"Authorization": "Bearer synthetic-token"},
    )
    assert status == 501
    assert response == {
        "error": "Branch cancellation is unavailable for this local runtime."
    }


def test_reasoning_route_requires_exact_authenticated_local_read(server):
    app, host = server
    path = "/api/live/reasoning?runId=run-1&callId=call-1"
    assert request(app, path)[0] == 401
    assert request(app, path, headers={"Authorization": "Bearer incorrect"})[0] == 401
    auth = {"Authorization": "Bearer " + app.token}
    assert request(app, path, headers={**auth, "Origin": "https://untrusted.invalid"})[0] == 403
    assert request(app, path, headers={**auth, "Host": "untrusted.invalid"})[0] == 403
    assert request(app, "/api/live/reasoning?runId=run-1", headers=auth)[0] == 400
    assert request(app, path + "&callId=call-2", headers=auth)[0] == 400
    assert host.reasoning_reads == []
    status, headers, result = request(app, path, headers=auth)
    assert status == 200 and headers["Cache-Control"] == "no-store"
    assert result["text"] == "SYNTHETIC_RETURNED_REASONING"
    assert host.reasoning_reads == [("run-1", "call-1")]
    assert "SYNTHETIC_RETURNED_REASONING" not in json.dumps(
        request(app, "/api/live/calls?runId=run-1", headers=auth)[2]
    )


def test_peer_record_route_requires_authenticated_exact_selection_and_no_store(server):
    app, host = server
    path = "/api/live/peer-record?runId=run-1&kind=mail_message&id=message-1&version=2"
    assert request(app, path)[0] == 401
    auth = {"Authorization": "Bearer " + app.token}
    assert request(app, path, headers={**auth, "Origin": "https://untrusted.invalid"})[0] == 403
    assert request(app, path, headers={**auth, "Host": "untrusted.invalid"})[0] == 403
    for invalid in (
        "/api/live/peer-record?runId=run-1&kind=mail_message&id=message-1",
        path + "&id=message-2", path + "&extra=1",
        path.replace("version=2", "version=0"),
        path.replace("version=2", "version=-1"),
    ):
        assert request(app, invalid, headers=auth)[0] == 400
    assert host.peer_reads == []
    status, headers, result = request(app, path, headers=auth)
    assert status == 200 and headers["Cache-Control"] == "no-store"
    assert host.peer_reads == [("run-1", "mail_message", "message-1", 2)]
    assert result["content"] == {"text": "Synthetic saved message"}


def test_headless_start_and_reconnect_use_same_authenticated_host(server, tmp_path, capsys):
    app, host = server
    state = tmp_path / ".local" / "state"
    state.mkdir(parents=True)
    cli._write_connection(state, {"origin": app.origin, "token": app.token,
                                  "launchUrl": app.launch_url})
    command = tmp_path / "request.json"
    command.write_text(json.dumps({"requestId": "stable-request", "question": "Find the trial abstract",
                                   "agentIds": ["agent-acquisition"]}))
    cli.mission_main(argparse.Namespace(mission_action="start", state_dir=state, input=command), tmp_path)
    assert json.loads(capsys.readouterr().out)["runId"] == "run-1"
    assert host.started[0].request_id == "stable-request"
    assert host.started[0].evidence_text == ""
    assert cli.connect_url(state) == app.launch_url
    assert len(host.started) == 1  # Reading/reconnecting never submits research.


def test_headless_branch_cancel_uses_the_local_authenticated_route(tmp_path, capsys, monkeypatch):
    parser = argparse.ArgumentParser()
    cli.configure_mission(parser)
    parsed = parser.parse_args(["cancel-branch", "--input", str(tmp_path / "cancel.json")])
    assert parsed.mission_action == "cancel-branch"
    connection = {"origin": "http://127.0.0.1:34567", "token": "synthetic-token"}
    monkeypatch.setattr(cli, "_connection", lambda _state: connection)
    captured = []

    class Opener:
        def open(self, req, timeout):
            captured.append((req, timeout))
            return BytesIO(json.dumps({"status": "cancelled"}).encode())

    monkeypatch.setattr(urllib.request, "build_opener", lambda *_args: Opener())
    command = tmp_path / "cancel.json"
    command.write_text(json.dumps({
        "requestId": "operator-cancel", "runId": "run-1", "taskId": "task-1",
        "expectedTaskVersion": 2, "reason": "End this selected branch",
    }))
    cli.mission_main(argparse.Namespace(
        mission_action="cancel-branch", state_dir=tmp_path, input=command,
    ), tmp_path)
    receipt = json.loads(capsys.readouterr().out)
    assert receipt["status"] == "cancelled"
    sent, timeout = captured[0]
    assert sent.full_url == "http://127.0.0.1:34567/api/live/branch-cancellations"
    assert sent.get_header("Authorization") == "Bearer synthetic-token"
    assert json.loads(sent.data)["expectedTaskVersion"] == 2
    assert timeout == 15
