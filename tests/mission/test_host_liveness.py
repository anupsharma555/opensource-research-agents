"""Host heartbeats identify observed process and drive intervals without research writes."""

from __future__ import annotations

import errno
import json
import os
import socket
import sqlite3
import stat
import time
from pathlib import Path

import pytest

from ora.mission import service as service_module
from ora.mission.host_liveness import HostLiveness
from ora.models import GroqSelectedAdapter, ModelSession
from ora.state import StateStore
from tests.mission.test_service import ROOT


class Clock:
    def __init__(self) -> None:
        self.wall = 1_800_000_000.0
        self.mono = 100.0

    def advance(self, seconds: float) -> None:
        self.wall += seconds
        self.mono += seconds


def _events(path: Path) -> list[dict]:
    data = path.read_text()
    if not data.endswith("\n"):
        data = data.rsplit("\n", 1)[0] + "\n"
    return [json.loads(line) for line in data.splitlines()]


def _record_count(path: Path) -> int:
    connection = sqlite3.connect(f"{path.resolve().as_uri()}?mode=ro", uri=True)
    try:
        return connection.execute("SELECT count(*) FROM records").fetchone()[0]
    finally:
        connection.close()


def test_heartbeat_separates_active_driver_from_alive_idle_host_without_sqlite_writes(tmp_path):
    StateStore(tmp_path)
    database = tmp_path / "state.sqlite3"
    before = _record_count(database)
    clock = Clock()
    monitor = HostLiveness(
        tmp_path, mode="live", start_thread=False,
        clock=lambda: clock.wall, monotonic=lambda: clock.mono,
    )
    try:
        token = monitor.begin_drive("run-synthetic")
        clock.advance(1_201)
        active = monitor.sample()
        assert active["active_drive_count"] == 1
        assert active["active_run_ids"] == ["run-synthetic"]
        assert active["oldest_active_seconds"] == 1_201
        assert active["drive_completed_count"] == 0

        monitor.finish_drive(token, changed=False, next_wake=clock.wall + 60)
        clock.advance(1_500)
        idle = monitor.sample()
        assert idle["active_drive_count"] == 0
        assert idle["drive_count"] == idle["drive_completed_count"] == 1
        assert idle["last_drive_duration_seconds"] == 1_201
        assert idle["last_drive_changed"] is False
        assert idle["last_next_wake_utc"] < idle["at_utc"]
        assert _record_count(database) == before
    finally:
        monitor.close()
    assert [item["event"] for item in _events(monitor.path)] == [
        "started", "heartbeat", "heartbeat", "stopped",
    ]
    assert stat.S_IMODE(monitor.path.stat().st_mode) == 0o600
    assert stat.S_IMODE(monitor.path.parent.stat().st_mode) == 0o700


def test_restarted_host_has_distinct_session_and_missing_samples_are_unclassified(tmp_path):
    clock = Clock()
    first = HostLiveness(tmp_path, mode="live", start_thread=False,
                         clock=lambda: clock.wall, monotonic=lambda: clock.mono)
    first.sample()
    first.close()
    clock.advance(7_200)
    second = HostLiveness(tmp_path, mode="live", start_thread=False,
                          clock=lambda: clock.wall, monotonic=lambda: clock.mono)
    try:
        second.sample()
    finally:
        second.close()
    assert first.session_id != second.session_id
    assert first.path != second.path
    assert [item["event"] for item in _events(first.path)] == [
        "started", "heartbeat", "stopped",
    ]
    assert [item["event"] for item in _events(second.path)] == [
        "started", "heartbeat", "stopped",
    ]
    assert _events(second.path)[0]["drive_count"] == 0
    assert "host_offline" not in second.path.read_text()


def test_background_heartbeat_continues_during_active_drive(tmp_path):
    monitor = HostLiveness(tmp_path, mode="live", interval_seconds=0.02)
    try:
        token = monitor.begin_drive("run-synthetic")
        deadline = time.monotonic() + 1
        while time.monotonic() < deadline:
            heartbeats = [item for item in _events(monitor.path)
                          if item["event"] == "heartbeat"]
            if heartbeats:
                break
            time.sleep(0.01)
        assert heartbeats and heartbeats[0]["active_drive_count"] == 1
        monitor.finish_drive(token, changed=True)
    finally:
        monitor.close()


def test_journal_write_failure_keeps_drive_tracking_available(tmp_path):
    monitor = HostLiveness(tmp_path, mode="live", start_thread=False)
    assert monitor._fd is not None
    try:
        # Simulate a lost local file descriptor without mutating research state.
        os.close(monitor._fd)
        monitor.sample()
        assert monitor.status()["write_error"]["errno"] == errno.EBADF
        token = monitor.begin_drive("run-synthetic")
        monitor.finish_drive(token, changed=True)
        assert monitor._snapshot("heartbeat")["drive_completed_count"] == 1
    finally:
        monitor.close()


def test_live_service_wraps_drive_and_records_only_ids_and_error_class(tmp_path, monkeypatch):
    def no_network(*_args, **_kwargs):
        raise AssertionError("Host liveness test must stay offline")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    monkeypatch.setenv("OSRA_BHEB_CONFIG", "")
    clock = Clock()
    monkeypatch.setattr(service_module, "HostLiveness", lambda state_dir, *, mode: HostLiveness(
        state_dir, mode=mode, start_thread=False,
        clock=lambda: clock.wall, monotonic=lambda: clock.mono,
    ))
    store = StateStore(tmp_path)
    session = ModelSession(
        store, ROOT,
        adapter=GroqSelectedAdapter(ROOT, transport=lambda *_a, **_kw: None),
    )
    host = service_module.MissionService(
        ROOT, tmp_path, model_session=session, execution_mode="live", start_supervisor=False,
    )
    monitor = host._host_liveness
    assert monitor is not None
    database = tmp_path / "state.sqlite3"
    before = _record_count(database)
    run = {"id": "run-synthetic", "body": {"question": "PRIVATE_SENTINEL"}}
    try:
        monkeypatch.setattr(host, "_drive_run_step", lambda value: (value is run, clock.wall + 60))
        assert host._drive_run(run) == (True, clock.wall + 60)

        def fail(_run):
            raise RuntimeError("PRIVATE_SENTINEL")

        monkeypatch.setattr(host, "_drive_run_step", fail)
        with pytest.raises(RuntimeError, match="PRIVATE_SENTINEL"):
            host._drive_run(run)
        snapshot = monitor.sample()
        assert snapshot["drive_count"] == snapshot["drive_completed_count"] == 2
        assert snapshot["drive_failed_count"] == 1
        assert snapshot["last_run_id"] == "run-synthetic"
        assert snapshot["last_drive_error_class"] == "RuntimeError"
        assert _record_count(database) == before

        def monitor_fault(_run_id):
            raise RuntimeError("PRIVATE_SENTINEL")

        monkeypatch.setattr(monitor, "begin_drive", monitor_fault)
        monkeypatch.setattr(host, "_drive_run_step", lambda _run: (False, None))
        assert host._drive_run(run) == (False, None)
        assert monitor.status()["runtime_error_class"] == "RuntimeError"
    finally:
        host.close()
    assert _events(monitor.path)[-1]["event"] == "stopped"
    assert "PRIVATE_SENTINEL" not in monitor.path.read_text()

    offline_dir = tmp_path / "offline"
    offline = service_module.MissionService(
        ROOT, offline_dir, execution_mode="offline_test", start_supervisor=False,
    )
    try:
        assert offline._host_liveness is None
        assert not (offline_dir / "host-liveness").exists()
    finally:
        offline.close()
