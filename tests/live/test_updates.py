"""Check authenticated, content-free invalidations against actual SQLite commits.

Each reconnect requests a fresh projection, so the stream is not an incremental
event history and never promises that a browser received every event.
"""

import threading
import urllib.error
import urllib.request
from types import SimpleNamespace

import pytest

from ora.contracts.models import LiveState
from ora.live.server import LocalResearchServer
from ora.state import StateStore


def _serve(host, root):
    server = LocalResearchServer(host, root)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    return server, worker


def _stop(server, worker):
    server.shutdown()
    server.server_close()
    worker.join(timeout=3)


def _empty_state(_run_id=None):
    return LiveState(enabled=True, model="synthetic", agents=[], runs=[])


def test_stream_requires_auth_and_notifies_after_a_durable_commit(tmp_path):
    store = StateStore(tmp_path / "state")
    server, worker = _serve(SimpleNamespace(store=store, state=_empty_state), tmp_path)
    try:
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(server.origin + "/api/live/updates", timeout=3)
        assert error.value.code == 401
        request = urllib.request.Request(server.origin + "/api/live/updates", headers={"Authorization": f"Bearer {server.token}"})
        state_request = urllib.request.Request(server.origin + "/api/live/state", headers={"Authorization": f"Bearer {server.token}"})
        with urllib.request.urlopen(state_request, timeout=3) as response:
            assert response.headers["X-Atlas-Updates"] == "sqlite-invalidation-v1"
        with urllib.request.urlopen(request, timeout=3) as response:
            assert response.headers["Content-Type"] == "text/event-stream"
            assert [response.readline() for _ in range(3)] == [b"event: invalidate\n", b"data: {}\n", b"\n"]
            store.transact("synthetic", {}, lambda tx: tx.put("synthetic", "one", {"text": "PRIVATE_SYNTHETIC_TEXT"}))
            frame = b"".join(response.readline() for _ in range(3))
            assert frame == b"event: invalidate\ndata: {}\n\n"
        # Reconnect always prompts a full read even without another write.
        with urllib.request.urlopen(request, timeout=3) as response:
            assert response.readline() == b"event: invalidate\n"
    finally:
        _stop(server, worker)


def test_stream_rejects_cross_origin_cookie_and_has_no_legacy_store(tmp_path):
    server, worker = _serve(SimpleNamespace(state=_empty_state), tmp_path)
    try:
        cookie = {"Cookie": f"ora_session={server.token}"}
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(urllib.request.Request(
                server.origin + "/api/live/updates", headers={**cookie, "Origin": "https://other.invalid"}
            ), timeout=3)
        assert error.value.code == 403
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(urllib.request.Request(
                server.origin + "/api/live/updates", headers=cookie
            ), timeout=3)
        assert error.value.code == 501
        with urllib.request.urlopen(urllib.request.Request(
            server.origin + "/api/live/state", headers=cookie
        ), timeout=3) as response:
            assert response.headers.get("X-Atlas-Updates") is None
    finally:
        _stop(server, worker)


def test_stream_limit_and_shutdown_release_connection(tmp_path):
    store = StateStore(tmp_path / "state")
    server, worker = _serve(SimpleNamespace(store=store), tmp_path)
    request = urllib.request.Request(
        server.origin + "/api/live/updates",
        headers={"Authorization": f"Bearer {server.token}"},
    )
    responses = []
    try:
        for _ in range(8):
            response = urllib.request.urlopen(request, timeout=3)
            assert [response.readline() for _ in range(3)] == [
                b"event: invalidate\n", b"data: {}\n", b"\n",
            ]
            responses.append(response)
        with pytest.raises(urllib.error.HTTPError) as error:
            urllib.request.urlopen(request, timeout=3)
        assert error.value.code == 503
        server.shutdown()
        assert all(response.readline() == b"" for response in responses)
    finally:
        for response in responses:
            response.close()
        server.server_close()
        worker.join(timeout=3)


def test_unchanged_mission_projection_does_not_trigger_a_refresh_feedback_loop(tmp_path, monkeypatch):
    from ora.contracts.models import LiveStartRequest
    from tests.mission.test_service import answer, drain, service

    calls = []

    def transport(request, *, timeout):
        calls.append(request)
        return answer("Synthetic provisional result without supplied evidence.", len(calls))

    host = service(tmp_path / "mission", monkeypatch, transport)
    try:
        receipt = host.start(LiveStartRequest(
            request_id="synthetic-idle-view", question="Summarize the available synthetic evidence.",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, receipt.run_id)
        initial = host.state(receipt.run_id)
        db = host.store._connect()
        try:
            version = db.execute("PRAGMA data_version").fetchone()[0]
            for _ in range(3):
                current = host.state(receipt.run_id)
                assert current.snapshot.snapshot_id == initial.snapshot.snapshot_id
                assert db.execute("PRAGMA data_version").fetchone()[0] == version
            assert len(calls) == 1
        finally:
            db.close()
    finally:
        host.close()


def test_disconnected_response_does_not_generate_a_second_error_response():
    from ora.live.server import ResearchHandler

    handler = object.__new__(ResearchHandler)
    statuses = []
    handler._headers = lambda status, **_kwargs: statuses.append(status)

    class Disconnected:
        def write(self, _payload):
            raise BrokenPipeError("Synthetic cancelled viewer")

    handler.wfile = Disconnected()
    handler._json(200, {"synthetic": True})
    assert statuses == [200]
    assert handler.close_connection is True
