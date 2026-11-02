"""The host owns its selected pool without taking ownership of injected transports."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from ora.live.cli import _create_host
from ora.mission import MissionService
from ora.retrieval import http_transport

ROOT = Path(__file__).resolve().parents[2]


def test_selected_pool_is_shared_and_closed_by_its_host(tmp_path, monkeypatch):
    closed = []
    pool = SimpleNamespace(close=lambda: closed.append(True))
    monkeypatch.setattr(http_transport, "PooledHTTPTransport", lambda: pool)
    host = MissionService(ROOT, tmp_path, execution_mode="offline_test",
                          start_supervisor=False, discovery_http="httpx")
    assert host.discovery_transport is pool
    assert host.web_search.transport is pool
    host.close()
    assert closed == [True]


def test_default_and_injected_transport_ownership_are_preserved(tmp_path):
    closed = []
    injected = SimpleNamespace(close=lambda: closed.append(True))
    host = MissionService(ROOT, tmp_path, execution_mode="offline_test",
                          start_supervisor=False, discovery_transport=injected)
    try:
        assert host.discovery_transport is injected
    finally:
        host.close()
    assert closed == []


def test_conflicting_or_unsupported_choices_fail_before_host_creation(tmp_path):
    with pytest.raises(ValueError, match="not both"):
        MissionService(ROOT, tmp_path, discovery_transport=lambda: None, discovery_http="httpx")
    with pytest.raises(ValueError, match="only for the mission"):
        _create_host(ROOT, tmp_path, SimpleNamespace(runtime="legacy", discovery_http="httpx"))
    assert not (tmp_path / "state.sqlite3").exists()
