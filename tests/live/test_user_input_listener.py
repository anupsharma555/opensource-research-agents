"""Exercise user input through a real loopback listener and terminal command."""

from __future__ import annotations

import _socket
import argparse
import json
import socket
import threading
import urllib.error
import urllib.request

import pytest

from ora.contracts.models import LiveStartRequest
from ora.live import cli
from ora.live.server import LocalResearchServer
from tests.mission.test_service import service


@pytest.fixture
def loopback_only(monkeypatch):
    """Let the real HTTP stack reach this test's listener, never other hosts."""

    def connect(sock, address):
        if not isinstance(address, tuple) or address[0] != "127.0.0.1":
            raise AssertionError(f"Offline listener attempted a non-loopback destination: {address!r}")
        return _socket.socket.connect(sock, address)

    def connect_ex(sock, address):
        connect(sock, address)
        return 0

    def create_connection(address, timeout=socket._GLOBAL_DEFAULT_TIMEOUT, source_address=None):
        if not isinstance(address, tuple) or address[0] != "127.0.0.1":
            raise AssertionError(f"Offline listener attempted a non-loopback destination: {address!r}")
        client = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            if timeout is not socket._GLOBAL_DEFAULT_TIMEOUT:
                client.settimeout(timeout)
            if source_address:
                client.bind(source_address)
            client.connect(address)
            return client
        except BaseException:
            client.close()
            raise

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(socket, "create_connection", create_connection)
    monkeypatch.setattr(urllib.request, "getproxies", lambda: {})


def _post(origin, path, body, headers=None):
    request = urllib.request.Request(
        origin + path, data=json.dumps(body).encode(),
        headers={"Content-Type": "application/json", **(headers or {})},
    )
    try:
        response = urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
            request, timeout=3,
        )
    except urllib.error.HTTPError as error:
        response = error
    with response:
        return response.status, json.load(response)


def _listener(host, web_root):
    app = LocalResearchServer(host, web_root)
    thread = threading.Thread(target=app.serve_forever, daemon=True)
    thread.start()
    return app, thread


def _stop_listener(app, thread):
    app.shutdown()
    app.server_close()
    thread.join(3)
    assert not thread.is_alive()


def test_real_listener_cli_user_input_accepts_once_and_replays_after_reopen(
    tmp_path, monkeypatch, capsys, loopback_only,
):
    attempts = []

    def no_model(*_args, **_kwargs):
        attempts.append("unexpected model dispatch")
        raise AssertionError("Listener acceptance must not call a model")

    state = tmp_path / "private-state"
    state.mkdir()
    command_file = tmp_path / "command.json"
    host = service(state, monkeypatch, no_model)
    reopened = None
    app = thread = None
    try:
        run = host.start(LiveStartRequest(
            request_id="listener-start", question="Inspect a synthetic count.",
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        task = host.store.get("board_proposal", control["body"]["task_id"])
        command = {
            "requestId": "listener-input", "runId": run.run_id, "taskId": task["id"],
            "targetAgentId": control["body"]["actor_id"],
            "expectedTaskVersion": task["version"], "kind": "direction",
            "text": "Use the corrected synthetic denominator before concluding.",
        }
        command_file.write_text(json.dumps(command))
        app, thread = _listener(host, tmp_path)
        cli._write_connection(state, {"origin": app.origin, "token": app.token})
        path = "/api/live/user-inputs"
        assert _post(app.origin, path, command)[0] == 401
        assert _post(app.origin, path, command, {
            "Cookie": f"ora_session={app.token}",
        })[0] == 403
        assert _post(app.origin, path, command, {
            "Authorization": "Bearer " + app.token,
            "Origin": "https://untrusted.invalid",
        })[0] == 403
        cli.mission_main(argparse.Namespace(
            mission_action="input", state_dir=state, input=command_file,
        ), tmp_path)
        first = json.loads(capsys.readouterr().out)
        assert first["status"] == "queued_for_next_boundary"
        assert first["reused"] is False
        saved = host.store.get(**first["inputRef"])
        assert saved["body"]["request"] == command
        assert len(host.store.records("mission_user_input")) == 1
        assert host.store.records("model_request_intent") == []
        _stop_listener(app, thread)
        app = thread = None
        host.close()
        reopened = service(state, monkeypatch, no_model)
        app, thread = _listener(reopened, tmp_path)
        cli._write_connection(state, {"origin": app.origin, "token": app.token})
        cli.mission_main(argparse.Namespace(
            mission_action="input", state_dir=state, input=command_file,
        ), tmp_path)
        replay = json.loads(capsys.readouterr().out)
        assert replay["reused"] is True
        assert replay["inputRef"] == first["inputRef"]
        assert len(reopened.store.records("mission_user_input")) == 1
        assert _post(app.origin, path, {**command, "text": "Changed request identity"}, {
            "Authorization": "Bearer " + app.token,
        })[0] == 409
        assert attempts == []
    finally:
        if app is not None:
            _stop_listener(app, thread)
        (reopened or host).close()
