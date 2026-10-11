"""Check private launch records, safe file boundaries and viewer credential isolation."""

from __future__ import annotations

import argparse
import io
import json
import os
import stat
from pathlib import Path

import pytest

from ora.live import cli


def test_native_viewer_receives_session_url_without_provider_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
) -> None:
    state_dir = tmp_path / ".local" / "live"
    state_dir.mkdir(parents=True)
    origin = "http://127.0.0.1:4242"
    token = "synthetic-local-session"
    launch_url = f"{origin}/live#token={token}"
    (state_dir / "connection.json").write_text(json.dumps({
        "origin": origin, "token": token, "launchUrl": launch_url,
    }))
    binary = (tmp_path / "desktop/src-tauri/target/debug/bundle/macos"
              / "Research Atlas Sample.app/Contents/MacOS/research_atlas_prototype")
    binary.parent.mkdir(parents=True)
    binary.touch()
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-test-key")
    monkeypatch.setenv("GROQ_API_KEY_BACKUP", "synthetic-backup-key")
    monkeypatch.setenv("ORA_TEST_KEEP", "kept")
    launched: list[tuple[list[str], dict]] = []

    def fake_popen(command: list[str], **kwargs: object) -> object:
        launched.append((command, kwargs))
        return object()

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(cli, "_request_state", lambda _, **kwargs: {"enabled": True, "agents": [], "runs": []})
    cli.main(argparse.Namespace(state_dir=state_dir, live_action="open", browser=False), tmp_path)

    assert len(launched) == 1
    command, options = launched[0]
    assert command == [str(binary)]
    viewer_env = options["env"]
    assert isinstance(viewer_env, dict)
    assert viewer_env["ORA_LIVE_URL"] == launch_url
    assert viewer_env["ORA_LIVE_STATE_DIR"] == str(state_dir)
    assert viewer_env["ORA_TEST_KEEP"] == "kept"
    assert "GROQ_API_KEY" not in viewer_env
    assert "GROQ_API_KEY_BACKUP" not in viewer_env
    assert os.environ["GROQ_API_KEY"] == "synthetic-test-key"


def test_connection_replace_preserves_existing_symlink_target(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("keep this unrelated content")
    record = state_dir / "connection.json"
    record.symlink_to(unrelated)

    cli._write_connection(state_dir, {"origin": "http://127.0.0.1:4242", "token": "synthetic"})

    assert unrelated.read_text() == "keep this unrelated content"
    assert not record.is_symlink()
    assert json.loads(record.read_text())["token"] == "synthetic"
    assert stat.S_IMODE(record.stat().st_mode) == 0o600
    assert not list(state_dir.glob(".connection-*"))


def _saved_connection(tmp_path: Path, **overrides) -> Path:
    state = tmp_path / ".local" / "selected"
    state.mkdir(parents=True)
    record = {
        "origin": "http://127.0.0.1:4242", "token": "test-session",
        "launchUrl": "http://127.0.0.1:4242/live#token=test-session",
    }
    record.update(overrides)
    (state / "connection.json").write_text(json.dumps(record))
    return state


def test_connect_authenticates_without_starting_work(tmp_path, monkeypatch, capsys):
    state = _saved_connection(tmp_path, workspaceId="workspace-one")
    checked = []

    def read_state(connection, *, timeout):
        checked.append((connection["origin"], timeout))
        return {"enabled": True, "agents": [], "runs": [], "workspaceId": "workspace-one"}

    monkeypatch.setattr(cli, "_request_state", read_state)
    cli.main(argparse.Namespace(state_dir=state, live_action="connect"), tmp_path)
    assert checked == [("http://127.0.0.1:4242", 45)]
    assert capsys.readouterr().out == "http://127.0.0.1:4242/live#token=test-session\n"


def test_connect_rejects_wrong_workspace(tmp_path, monkeypatch):
    state = _saved_connection(tmp_path, workspaceId="workspace-one")
    monkeypatch.setattr(cli, "_request_state", lambda _, **kwargs: {
        "enabled": True, "agents": [], "runs": [], "workspaceId": "workspace-two",
    })
    with pytest.raises(ValueError, match="does not match"):
        cli.connect_url(state)


def test_reconnect_extends_only_its_authenticated_state_read(tmp_path, monkeypatch):
    state = _saved_connection(tmp_path, workspaceId="workspace-one")
    requests = []

    class Opener:
        def open(self, request, *, timeout):
            requests.append((request.get_method(), request.full_url,
                             request.get_header("Authorization"), timeout))
            return io.BytesIO(json.dumps({
                "enabled": True, "agents": [], "runs": [], "workspaceId": "workspace-one",
            }).encode())

    monkeypatch.setattr(cli.urllib.request, "build_opener", lambda handler: Opener())
    cli._request_state(cli._connection(state))
    assert cli.connect_url(state) == "http://127.0.0.1:4242/live#token=test-session"
    assert requests == [
        ("GET", "http://127.0.0.1:4242/api/live/state", "Bearer test-session", 10),
        ("GET", "http://127.0.0.1:4242/api/live/state", "Bearer test-session", 45),
    ]


def test_reconnect_timeout_does_not_skip_verification_or_retry(tmp_path, monkeypatch):
    state = _saved_connection(tmp_path, workspaceId="workspace-one")
    timeouts = []

    class Opener:
        def open(self, request, *, timeout):
            timeouts.append(timeout)
            raise TimeoutError

    monkeypatch.setattr(cli.urllib.request, "build_opener", lambda handler: Opener())
    with pytest.raises(ValueError, match="host is unavailable"):
        cli.connect_url(state)
    assert timeouts == [45]


@pytest.mark.parametrize("origin", [
    "https://127.0.0.1:4242", "http://localhost:4242", "http://127.0.0.1:4242?redirect=1",
    "http://user@127.0.0.1:4242", "http://127.0.0.1:4242#fragment", "http://127.0.0.1:bad",
])
def test_connect_rejects_invalid_origin_before_network(tmp_path, monkeypatch, origin):
    state = _saved_connection(tmp_path, origin=origin)
    monkeypatch.setattr(cli, "_request_state", lambda _: pytest.fail("network should not run"))
    with pytest.raises(ValueError, match="Invalid local server"):
        cli.connect_url(state)


def test_host_lock_refuses_symlink_without_touching_target(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("keep this unrelated content")
    (state_dir / "host.lock").symlink_to(unrelated)

    with pytest.raises(ValueError, match="cannot be opened safely"):
        cli._open_host_lock(state_dir)

    assert unrelated.read_text() == "keep this unrelated content"
