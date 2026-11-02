"""A saved report can finish after its executor claim releases, including after restart."""

from __future__ import annotations

import threading
import time

import pytest

from ora.contracts.models import LiveStartRequest
from tests.mission.test_service import service, tool


@pytest.mark.parametrize("restart", [False, True])
def test_saved_report_waits_for_exact_claim_release_then_completes_once(
    tmp_path, monkeypatch, restart,
):
    calls = []

    def transport(_request, *, timeout):
        calls.append(timeout)
        assert len(calls) == 1, "Claim recovery must not ask the model to publish again"
        return tool("publish_result", {
            "title": "Synthetic recovered report",
            "text": "One saved result after the claim becomes available.",
            "citations": [],
            "limitations": ["Synthetic source-free test"],
        }, 1)

    host = service(tmp_path, monkeypatch, transport)
    reopened = None
    original_release = host.executor._release_board
    held = []
    release_intercepted = threading.Event()

    def hold_tool_release(spec, fence):
        if spec.handler_id == "mission-tool":
            held.append((spec, fence))
            release_intercepted.set()
            return
        return original_release(spec, fence)

    monkeypatch.setattr(host.executor, "_release_board", hold_tool_release)
    try:
        run = host.start(LiveStartRequest(
            request_id=f"synthetic-held-claim-{restart}",
            question="Publish one synthetic source-free result",
            agent_ids=["agent-synthesis"],
        ))
        for _ in range(200):
            host._drive_run(host._run(run.run_id))
            current = host._control(run.run_id, "agent-synthesis")
            if (
                release_intercepted.is_set()
                and current["body"].get("waiting_reason")
                == "Saved report awaits current task claim release"
            ):
                break
            release_intercepted.wait(0.01)
        else:
            pytest.fail("The held executor claim did not reach saved waiting state")

        assert current["body"]["phase"] == "waiting"
        assert current["body"]["tool_call"]["name"] == "publish_result"
        assert len(host.store.records("report")) == 1
        assert len(host.store.records("model_response_receipt")) == 1
        assert len(held) == 1
        host._drive_run(host._run(run.run_id))
        assert host._control(run.run_id, "agent-synthesis")["body"]["phase"] == "waiting"
        assert host.store.get("board_proposal", current["body"]["task_id"])["body"][
            "status"
        ] == "open"
        # Release the exact saved claim after waiting is durable. No wall-clock
        # race or lease-expiry delay determines this transition.
        original_release(*held[0])
        if restart:
            host.close()
            reopened = service(tmp_path, monkeypatch, transport)
            active = reopened
            assert active._control(run.run_id, "agent-synthesis")["body"]["phase"] == "waiting"
        else:
            active = host
        for _ in range(100):
            active._drive_run(active._run(run.run_id))
            if active._run(run.run_id)["body"]["status"] == "completed":
                break
            time.sleep(0.01)
        else:
            pytest.fail("A released saved report did not complete the task")
        assert len(calls) == 1
        assert len(active.store.records("report")) == 1
        assert len(active.store.records("model_response_receipt")) == 1
        control = active._control(run.run_id, "agent-synthesis")
        report_ref = control["body"]["report_ref"]
        accepted = [
            outcome for outcome in active.store.records("board_outcome")
            if report_ref in outcome["body"]["result_refs"]
            and outcome["body"]["completes_task"]
        ]
        assert len(accepted) == 1
        proposal = active.store.get("board_proposal", control["body"]["task_id"])
        assert proposal["body"]["status"] == "completed"
        assert proposal["body"]["outcome_ref"] == {
            "kind": "board_outcome", "id": accepted[0]["id"], "version": accepted[0]["version"],
        }
        assert control["body"]["phase"] == "done"
        assert active.state(run.run_id).snapshot.mission.status == "completed"
    finally:
        if reopened is not None:
            reopened.close()
        host.close()
