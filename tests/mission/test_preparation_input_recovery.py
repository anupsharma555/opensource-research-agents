"""A new exact peer answer can wake one failed local preparation without churn."""

from __future__ import annotations

import time

import pytest

from ora.contracts.models import LiveStartRequest, LiveUserInputRequest
from ora.coordination.board import ref
from ora.mission.context import PACKER_VERSION
from ora.mission.service import ESTIMATOR_VERSION
from tests.mission.test_service import service


def _waiting(host, run_id: str, alias: str, *, budget: bool = True) -> dict:
    control = host._control(run_id, alias)
    call_id, _ = host._model_ids(control, control["body"]["sequence"] + 1)
    if budget:
        host._save(
            "mission_call_budget", call_id,
            {"mission_id": control["body"]["mission_id"], "run_id": run_id,
             "estimator_version": ESTIMATOR_VERSION, "packer_version": PACKER_VERSION},
            mission_id=control["body"]["mission_id"],
        )
    host._update_control(
        control, phase="waiting", work_id=None,
        waiting_reason="ContextOverflow: this branch needs local repair",
        local_host_wait=host._local_host_wait_marker(control, "ContextOverflow"),
    )
    return host._control(run_id, alias)


@pytest.mark.parametrize("arrival_timing", ["after_wait", "during_preparation"])
def test_current_builder_retries_once_for_new_same_task_peer_answer(
    tmp_path, monkeypatch, arrival_timing,
):
    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: (_ for _ in ()).throw(
        AssertionError("Preparation recovery cannot dispatch")))
    try:
        run = host.start(LiveStartRequest(
            request_id="preparation-new-peer-answer", question="Review synthetic evidence.",
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis", "agent-critique"],
        ))
        requester = host._control(run.run_id, "agent-synthesis")
        responder = host._control(run.run_id, "agent-critique")
        peer_task = host.store.get("board_proposal", responder["body"]["task_id"])
        mission_id = requester["body"]["mission_id"]

        def save_report(tx):
            report = tx.put("report", "new-answer-report", {
                "mission_id": mission_id, "title": "Synthetic peer assessment",
                "body": "The synthetic comparison needs a timepoint limitation.",
                "citations": [], "limitations": ["Synthetic fixture only"],
            })
            outcome = tx.put("board_outcome", "new-answer-outcome", {
                "mission_id": mission_id, "proposal_ref": ref(peer_task),
                "result_refs": [ref(report)],
            })
            tx.put("board_proposal", peer_task["id"], {
                **peer_task["body"], "status": "completed", "outcome_ref": ref(outcome),
            }, expected_version=peer_task["version"])
            tx.put("mission_task_control", responder["id"], {
                **responder["body"], "phase": "done", "report_ref": ref(report),
            }, expected_version=responder["version"])
            return ref(report)

        report_ref = host.store.transact(
            "new-answer-report-save", {}, save_report, mission_id=mission_id,
        )
        request_id = "synthetic-preparation-request"
        sent = host.mailbox.send_request(
            "new-answer-request-send", mission_id=mission_id,
            request_id=request_id, message_id="new-answer-message",
            requester_id=requester["body"]["actor_id"],
            recipient_id=responder["body"]["actor_id"],
            question="Which synthetic timepoint is supported?",
            expected_artifact="Source-bound assessment",
            cause_ref=requester["body"]["proposal_ref"],
            task_ref=requester["body"]["proposal_ref"], payload={},
        )
        assert sent["status"] == "accepted"
        host.mailbox.mark_delivered(
            "new-answer-request-delivered", mission_id=mission_id,
            message_id="new-answer-message",
        )
        if arrival_timing == "after_wait":
            waiting = _waiting(host, run.run_id, "agent-synthesis")
            assert not host._recover_preparation(waiting)
            assert host._control(run.run_id, "agent-synthesis") == waiting
        time.sleep(.002)
        answered = host.mailbox.record_answer(
            "new-answer-recorded", mission_id=mission_id,
            request_id=request_id, answer_id="new-answer",
            responder_id=responder["body"]["actor_id"], artifact_ref=report_ref,
        )
        assert answered["status"] == "accepted"
        if arrival_timing == "during_preparation":
            time.sleep(.002)
            waiting = _waiting(host, run.run_id, "agent-synthesis")
            assert host.store.get(**answered["answer_ref"])["created_at"] < waiting[
                "created_at"
            ]
        old_budget = host.store.get("mission_call_budget", host._model_ids(
            waiting, waiting["body"]["sequence"] + 1,
        )[0])
        assert host._recover_preparation(waiting)
        ready = host._control(run.run_id, "agent-synthesis")
        assert ready["body"]["phase"] == "ready_model"
        assert ready["body"]["waiting_reason"] is None
        assert ready["body"]["local_host_wait"] is None
        assert ready["body"].get("preparation_repair_attempted_for") is None
        assert host.store.get(**ref(old_budget)) == old_budget
        assert host.store.records("model_request_intent") == []
        assert host.store.records("model_dispatch_attempt") == []

        # A second failed preparation after the same answer is a new local
        # checkpoint. It must not wake again on unchanged peer state.
        host._update_control(
            ready, phase="waiting",
            waiting_reason="ContextOverflow: this branch needs local repair",
            local_host_wait=host._local_host_wait_marker(ready, "ContextOverflow"),
        )
        repeated = host._control(run.run_id, "agent-synthesis")
        assert not host._recover_preparation(repeated)
        assert host._control(run.run_id, "agent-synthesis") == repeated
    finally:
        host.close()


@pytest.mark.parametrize("budget", [False, True])
def test_authenticated_new_input_retries_without_unrelated_peer_churn(
    tmp_path, monkeypatch, budget,
):
    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: (_ for _ in ()).throw(
        AssertionError("Preparation recovery cannot dispatch")))
    try:
        run = host.start(LiveStartRequest(
            request_id=f"preparation-user-input-{budget}",
            question="Check a synthetic claim.",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        waiting = _waiting(host, run.run_id, "agent-synthesis", budget=budget)
        assert not host._recover_preparation(waiting)
        other = host._control(run.run_id, "agent-critique")
        other_task = host.store.get("board_proposal", other["body"]["task_id"])
        host.submit_user_input(LiveUserInputRequest(
            request_id=f"unrelated-input-{budget}", run_id=run.run_id,
            task_id=other_task["id"], target_agent_id=other["body"]["actor_id"],
            expected_task_version=other_task["version"], kind="direction",
            text="Check the other synthetic task.",
        ))
        assert not host._recover_preparation(waiting)

        time.sleep(.002)
        own_task = host.store.get("board_proposal", waiting["body"]["task_id"])
        host.submit_user_input(LiveUserInputRequest(
            request_id=f"relevant-input-{budget}", run_id=run.run_id,
            task_id=own_task["id"], target_agent_id=waiting["body"]["actor_id"],
            expected_task_version=own_task["version"], kind="direction",
            text="Consider the added synthetic timepoint.",
        ))
        assert host._recover_preparation(waiting)
        ready = host._control(run.run_id, "agent-synthesis")
        assert ready["body"]["phase"] == "ready_model"
        host._update_control(
            ready, phase="waiting",
            waiting_reason="ContextOverflow: this branch needs local repair",
        )
        assert not host._recover_preparation(host._control(run.run_id, "agent-synthesis"))
        assert host.store.records("model_dispatch_attempt") == []
    finally:
        host.close()
