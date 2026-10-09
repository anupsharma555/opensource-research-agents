"""Check private launch records, safe file boundaries and viewer credential isolation."""

from __future__ import annotations

import argparse
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
    monkeypatch.setenv("ORA_TEST_KEEP", "kept")
    launched: list[tuple[list[str], dict]] = []

    def fake_popen(command: list[str], **kwargs: object) -> object:
        launched.append((command, kwargs))
        return object()

    monkeypatch.setattr(cli.subprocess, "Popen", fake_popen)
    cli.main(argparse.Namespace(state_dir=state_dir, live_action="open", browser=False), tmp_path)

    assert len(launched) == 1
    command, options = launched[0]
    assert command == [str(binary)]
    viewer_env = options["env"]
    assert isinstance(viewer_env, dict)
    assert viewer_env["ORA_LIVE_URL"] == launch_url
    assert viewer_env["ORA_TEST_KEEP"] == "kept"
    assert "GROQ_API_KEY" not in viewer_env
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


def test_host_lock_refuses_symlink_without_touching_target(tmp_path: Path) -> None:
    state_dir = tmp_path / "state"
    state_dir.mkdir()
    unrelated = tmp_path / "unrelated.txt"
    unrelated.write_text("keep this unrelated content")
    (state_dir / "host.lock").symlink_to(unrelated)

    with pytest.raises(ValueError, match="cannot be opened safely"):
        cli._open_host_lock(state_dir)

    assert unrelated.read_text() == "keep this unrelated content"
