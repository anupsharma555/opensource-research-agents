"""Private offline probe of search-child creation racing exact parent cancellation."""

from __future__ import annotations

import asyncio
import socket
import threading

import pytest

from ora.contracts.models import LiveBranchCancelRequest, LiveStartRequest
from ora.coordination.board import ref
from ora.coordination.executor import WorkSpec
from ora.retrieval.web_search import identity
from ora.state import StateError
from tests.mission.test_service import service


@pytest.fixture(autouse=True)
def no_network(monkeypatch):
    attempts = []

    def forbidden(*_args, **_kwargs):
        attempts.append("socket connection")
        raise AssertionError("Search child race probe attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", forbidden)
    monkeypatch.setattr(socket.socket, "connect_ex", forbidden)
    yield
    assert attempts == []


@pytest.mark.parametrize("search_path", ["shared", "legacy"])
@pytest.mark.parametrize("held_stage", ["before_child_proposal", "before_child_work"])
def test_search_child_cannot_escape_parent_cancel(tmp_path, monkeypatch, held_stage, search_path):
    host = service(tmp_path, monkeypatch,
                   lambda *_args, **_kwargs: pytest.fail("No model request is expected"))
    release = threading.Event()
    entered = threading.Event()
    errors = []
    results = []
    thread = None
    try:
        run = host.start(LiveStartRequest(
            request_id=f"search-child-{search_path}-{held_stage}",
            question="Synthetic search scope",
            agent_ids=["agent-synthesis"],
        ))
        if search_path == "legacy":
            saved_run = host._run(run.run_id)
            historical = dict(saved_run["body"])
            historical.pop("parallel_research_version")
            historical.pop("reader_action_purpose_tool_version")
            host.store.transact(
                f"fixture:legacy-search:{held_stage}", {"run_id": run.run_id},
                lambda tx: tx.put("mission_run", run.run_id, historical,
                                  expected_version=saved_run["version"]),
                mission_id=historical["mission_id"],
            )
        control = host._control(run.run_id, "agent-synthesis")
        task = host.store.get("board_proposal", control["body"]["task_id"])
        config = host._configuration(control)
        spec = WorkSpec(
            work_id=f"parent-search-{held_stage}",
            mission_id=control["body"]["mission_id"], run_id=run.run_id,
            proposal_ref=ref(task), actor_id=control["body"]["actor_id"],
            configuration_ref=ref(config), input_refs=(),
            handler_id="mission-tool", handler_version="1", kind="compute",
            lane=control["body"]["actor_id"], attempt_id=f"parent-search-{held_stage}",
        )
        operation_id = identity("web-search-", spec.mission_id, spec.work_id)
        if search_path == "legacy":
            child_id = identity("web-query-", operation_id, 0, "exa")

        if held_stage == "before_child_proposal":
            original = host.board.propose

            def gate(operation_id, **kwargs):
                prefix = "web-shared-propose:" if search_path == "shared" else "web-propose:"
                if operation_id.startswith(prefix):
                    entered.set()
                    assert release.wait(10), "Held child proposal was not released"
                return original(operation_id, **kwargs)

            monkeypatch.setattr(host.board, "propose", gate)
        else:
            original = host.executor.submit

            def gate(child):
                handler = "web-shared-query" if search_path == "shared" else "web-query"
                if child.handler_id == handler:
                    entered.set()
                    assert release.wait(10), "Held child work was not released"
                return original(child)

            monkeypatch.setattr(host.executor, "submit", gate)

        def start_search():
            try:
                results.append(host.web_search.start(spec, {
                    "mode": "quick", "provider": "exa",
                    "information_need": "Find a synthetic source",
                    "queries": ["synthetic query"],
                }))
            except BaseException as error:
                errors.append(error)

        thread = threading.Thread(target=start_search, daemon=True)
        thread.start()
        assert entered.wait(10), "Search child did not reach controlled interleaving"
        operation = host.store.get("web_search_operation", operation_id)
        assert operation is not None
        if search_path == "shared":
            child_id = operation["body"]["items"][0]
        if held_stage == "before_child_work":
            assert host.store.get("board_proposal", child_id)["body"]["status"] == "open"
        else:
            assert host.store.get("board_proposal", child_id) is None

        receipt = host.cancel_branch(LiveBranchCancelRequest(
            request_id=f"cancel-search-{search_path}-{held_stage}", run_id=run.run_id,
            task_id=task["id"], expected_task_version=task["version"],
            reason="Stop this exact synthetic search task",
        ))
        assert receipt.status == "cancelled"
        operation = host.store.get("web_search_operation", operation_id)
        assert operation["body"]["cancel_requested"] is True
    finally:
        release.set()
        if thread is not None:
            thread.join(12)
        host.close()

    assert thread is not None and not thread.is_alive()
    assert errors == [], errors
    assert len(results) == 1 and results[0]["status"] == "cancelled"
    reopened = service(tmp_path, monkeypatch,
                       lambda *_args, **_kwargs: pytest.fail("No model request is expected"))
    try:
        child = reopened.store.get("board_proposal", child_id)
        if search_path == "legacy" and child is not None:
            assert child["body"]["status"] == "cancelled"
        if search_path == "shared":
            assert child is not None and child["body"]["status"] == "open"
            assert child["body"]["branch_of"] is None
        else:
            assert not any(
                work["body"]["spec"].get("work_id") == child_id
                and work["body"].get("phase") == "pending"
                for work in reopened.store.records("executor_work")
            )
        before = [
            (item["id"], item["version"])
            for item in reopened.store.records("web_search_operation")
        ]
        asyncio.run(reopened.executor.run_ready(spec.mission_id))
        if search_path == "shared":
            assert reopened.store.get("web_shared_query_job", child_id)["body"][
                "phase"] == "known_no_dispatch"
        for _ in range(3):
            reopened._drive_run(reopened._run(run.run_id))
        assert [
            (item["id"], item["version"])
            for item in reopened.store.records("web_search_operation")
        ] == before
        assert reopened._run(run.run_id)["body"]["status"] == "cancelled"
    finally:
        reopened.close()


def test_delayed_search_start_after_parent_cancel_creates_no_pending_operation(
    tmp_path, monkeypatch,
):
    host = service(tmp_path, monkeypatch,
                   lambda *_args, **_kwargs: pytest.fail("No model request is expected"))
    try:
        run = host.start(LiveStartRequest(
            request_id="delayed-search-start", question="Synthetic delayed search",
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        task = host.store.get("board_proposal", control["body"]["task_id"])
        config = host._configuration(control)
        old_spec = WorkSpec(
            work_id="captured-before-cancel", mission_id=control["body"]["mission_id"],
            run_id=run.run_id, proposal_ref=ref(task),
            actor_id=control["body"]["actor_id"], configuration_ref=ref(config),
            input_refs=(), handler_id="mission-tool", handler_version="1",
            kind="compute", lane=control["body"]["actor_id"],
            attempt_id="captured-before-cancel",
        )
        receipt = host.cancel_branch(LiveBranchCancelRequest(
            request_id="cancel-before-search-start", run_id=run.run_id,
            task_id=task["id"], expected_task_version=task["version"],
            reason="Stop this exact synthetic search task",
        ))
        assert receipt.status == "cancelled"
        with pytest.raises(StateError, match="Search source task is no longer open"):
            host.web_search.start(old_spec, {
                "mode": "quick", "provider": "exa",
                "information_need": "Find a synthetic source",
                "queries": ["synthetic query"],
            })
        assert host.store.records("web_search_operation") == []
        assert host.store.records("web_search_invocation") == []
        assert host.store.records("web_search_item") == []
    finally:
        host.close()
