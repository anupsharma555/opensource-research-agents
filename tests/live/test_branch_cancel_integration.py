"""Exercise the local cancellation handler and saved mission service without sockets or providers."""

import socket

import pytest

from ora.contracts.models import LiveStartRequest
from tests.live.test_server import _post_without_listener
from tests.mission.test_service import service


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    attempts = []

    def forbidden(*_args, **_kwargs):
        attempts.append("socket connection")
        raise AssertionError("Offline cancellation attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    yield
    assert attempts == []


def test_authenticated_branch_cancel_is_saved_and_replayed_without_dispatch(tmp_path, monkeypatch):
    def no_provider_call(_request, *, timeout):
        raise AssertionError("Cancellation must not dispatch a model call")

    host = service(tmp_path, monkeypatch, no_provider_call)
    try:
        selected = host.start(LiveStartRequest(
            request_id="synthetic-selected-start", question="Compare two lines of work",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        unrelated = host.start(LiveStartRequest(
            request_id="synthetic-other-start", question="Separate question",
            agent_ids=["agent-synthesis"],
        ))
        target = host._control(selected.run_id, "agent-synthesis")
        sibling = host._control(selected.run_id, "agent-critique")
        other = host._control(unrelated.run_id, "agent-synthesis")
        task = host.store.get("board_proposal", target["body"]["task_id"])
        assert task is not None
        body = {
            "requestId": "synthetic-cancel-request", "runId": selected.run_id,
            "taskId": task["id"], "expectedTaskVersion": task["version"],
            "reason": "Stop the selected line of work",
        }
        auth = {"Authorization": "Bearer synthetic-token"}
        status, _, receipt = _post_without_listener(
            host, "/api/live/branch-cancellations", body, headers=auth,
        )
        assert status == 202
        assert receipt["status"] == "cancelled"
        assert receipt["taskRef"]["id"] == task["id"]
        assert host.store.get(**{
            "kind": receipt["cancellationRef"]["kind"],
            "id": receipt["cancellationRef"]["id"],
            "version": receipt["cancellationRef"]["version"],
        }) is not None

        replay_status, _, replay = _post_without_listener(
            host, "/api/live/branch-cancellations", body, headers=auth,
        )
        assert replay_status == 202
        assert replay == {**receipt, "reused": True}
        assert host._control(selected.run_id, "agent-synthesis")["body"]["phase"] == "cancelled"
        assert host._control(selected.run_id, "agent-critique") == sibling
        assert host._control(unrelated.run_id, "agent-synthesis") == other
        state = host.state(selected.run_id)
        tasks = {item.id: item for item in state.snapshot.tasks}
        assert tasks[task["id"]].status == "cancelled"
        assert tasks[sibling["body"]["task_id"]].status != "cancelled"
        assert state.snapshot.mission.status == "active"
    finally:
        host.close()
