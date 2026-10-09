"""Exercise real loopback HTTP boundaries without calling a model.

The tests distinguish session setup, accepted commands and later saved state,
and verify that conflicting requests keep their precise error status.
"""

import json
import threading
import urllib.error
import urllib.request

import pytest

from ora.contracts.models import LiveReceipt, LiveState
from ora.live.engine import LiveError
from ora.live.server import LocalResearchServer


class StubHost:
    def __init__(self):
        self.started = []

    def start(self, request):
        if request.request_id == "conflict":
            raise LiveError("Request identity conflict", 409)
        self.started.append(request)
        return LiveReceipt(request_id=request.request_id, run_id="run-1", task_id="task-1")

    def followup(self, request):
        raise LiveError("Unknown saved parent", 404)

    def state(self, run_id=None):
        if run_id == "missing":
            raise LiveError("Unknown run", 404)
        return LiveState(enabled=True, model="test-provider", agents=[], runs=[])

    def calls(self, run_id, attempt_id=None):
        return [{"runId": run_id, "attemptId": attempt_id, "content": "saved output"}]


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
