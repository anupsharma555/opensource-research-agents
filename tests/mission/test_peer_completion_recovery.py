"""A failed peer request remains visible until the peer work is resolved."""

from __future__ import annotations

import json
import time

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from tests.mission.test_service import answer, serialized_peer_name, service, tool


def _drive_until(host, run_id: str, predicate) -> None:
    for _ in range(300):
        host._drive_run(host._run(run_id))
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError("Saved peer completion boundary did not advance")


def test_failed_peer_request_keeps_direct_text_provisional_across_restart(
    tmp_path, monkeypatch,
) -> None:
    requests = []

    def transport(request, *, timeout):
        body = json.loads(request.data)
        requests.append(body)
        step = len(requests)
        if step == 1:
            return tool("peer_request", {
                "target_instance_id": "Mxra", "question": "Independently check this answer.",
            }, step)
        if step == 2:
            assert "does not identify another registered peer" in json.dumps(body["messages"])
            return answer("A useful provisional answer awaiting independent review.", step)
        if step == 3:
            messages = json.dumps(body["messages"])
            assert "failed_peer_request_still_unresolved" in messages
            assert "direct_answer_provisional_pending_peer_action" in messages
            candidate = host._control(run.run_id, "agent-synthesis")["body"][
                "provisional_direct_report_ref"
            ]
            feedback = next(
                json.loads(item["content"])["host_feedback"]
                for item in body["messages"]
                if item["role"] == "user" and '"host_feedback"' in item["content"]
            )
            assert feedback["candidate_ref"] == candidate
            assert "A useful provisional answer" not in messages
            return tool("request_capability", {"tool_id": "propose_scope_completion"}, step)
        if step == 4:
            assert "propose_scope_completion" in {
                item["function"]["name"] for item in body["tools"]
            }
            return tool("propose_scope_completion", {
                "reason": "Independent review was unavailable; retain the limitation explicitly.",
                "decisions": [],
            }, step)
        assert step == 5
        return answer("Revised scoped answer with the missing review disclosed.", step)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="failed-peer-direct-restart", question="Ask Mxra to check this answer",
            agent_ids=["agent-synthesis"],
        ))
        _drive_until(host, run.run_id, lambda: bool(
            host._control(run.run_id, "agent-synthesis")["body"].get(
                "provisional_direct_report_ref"
            )
        ))
        control = host._control(run.run_id, "agent-synthesis")
        candidate = control["body"]["provisional_direct_report_ref"]
        assert host.store.get(**candidate)["body"]["body"].startswith("A useful provisional")
        assert control["body"]["phase"] == "ready_model"
        assert len(control["body"]["peer_action_obligations"]) == 1
        assert host.store.get("board_proposal", control["body"]["task_id"])["body"][
            "status"
        ] == "open"
        assert host._run(run.run_id)["body"]["status"] == "active"
        assert not host.store.records("mail_request")
        assert len(requests) == 2
    finally:
        host.close()

    resumed = service(tmp_path, monkeypatch, transport)
    try:
        _drive_until(resumed, run.run_id, lambda: resumed._run(run.run_id)["body"][
            "status"
        ] == "completed")
        assert len(requests) == 5
        control = resumed._control(run.run_id, "agent-synthesis")
        assert control["body"]["phase"] == "done"
        assert control["body"]["report_ref"] != candidate
        assert resumed.store.get(**candidate)["body"]["body"].startswith(
            "A useful provisional"
        )
        dispositions = resumed.store.records("mission_scope_disposition")
        assert len(dispositions) == 1
        assert dispositions[0]["body"]["source_control_ref"]["version"] > (
            control["body"]["peer_action_obligations"][0]["after_control_version"]
        )
    finally:
        resumed.close()


def test_invitation_and_unanswered_request_do_not_resolve_failed_peer_action(
    tmp_path, monkeypatch,
) -> None:
    requests = []
    host = None
    run = None

    def transport(request, *, timeout):
        body = json.loads(request.data)
        if serialized_peer_name(body) == "Mxra":
            return tool("respond_invitation", {"decision": "declined"}, 90)
        requests.append(body)
        step = len(requests)
        if step == 1:
            return tool("peer_request", {
                "target_instance_id": "Mxra", "question": "Check an exact synthetic claim.",
            }, step)
        if step == 2:
            return answer("Provisional claim before peer review.", step)
        if step == 3:
            return tool("request_capability", {"tool_id": "invite_peer"}, step)
        if step == 4:
            control = host._control(run.run_id, "agent-synthesis")
            proposal = host.store.get("board_proposal", control["body"]["task_id"])
            return tool("invite_peer", {
                "proposal_id": proposal["id"], "proposal_version": proposal["version"],
                "recipient_alias": "agent-critique", "objective": "Check the claim",
                "expected_artifact": "Independent review",
            }, step)
        if step == 5:
            return answer("Still provisional after an invitation alone.", step)
        if step == 6:
            return tool("peer_request", {
                "target_instance_id": "agent-critique", "question": "Check the claim.",
            }, step)
        return answer("The request is saved, but its answer is pending.", step)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="failed-peer-invitation-only", question="Seek peer review",
            agent_ids=["agent-synthesis"],
        ))
        _drive_until(host, run.run_id, lambda: (
            len(requests) >= 5 and host._control(run.run_id, "agent-synthesis")[
                "body"
            ].get("provisional_direct_report_ref") is not None
            and len(host.store.records("report")) >= 2
        ))
        control = host._control(run.run_id, "agent-synthesis")
        assert host.store.records("board_invitation")
        assert not host.store.records("mail_request")
        assert len(host._unresolved_peer_actions(control)) == 1
        assert host.store.get("board_proposal", control["body"]["task_id"])["body"][
            "status"
        ] == "open"
        assert host._run(run.run_id)["body"]["status"] == "active"
        _drive_until(host, run.run_id, lambda: bool(
            host.store.records("mail_request")
            and host._control(run.run_id, "agent-synthesis")["body"][
                "peer_action_obligations"
            ][0].get("request_ref")
        ))
        control = host._control(run.run_id, "agent-synthesis")
        action = control["body"]["peer_action_obligations"][0]
        assert action["request_ref"]["id"] == host.store.records("mail_request")[0]["id"]
        assert len(host._unresolved_peer_actions(control)) == 1
    finally:
        host.close()


def test_reviewed_peer_answer_allows_a_later_direct_revision(tmp_path, monkeypatch) -> None:
    sage_requests = []

    def transport(request, *, timeout):
        body = json.loads(request.data)
        if serialized_peer_name(body) == "Mxra":
            return answer("Separate initial peer work.", 90)
        sage_requests.append(body)
        step = len(sage_requests)
        if step == 1:
            return tool("peer_request", {
                "target_instance_id": "agent-critique", "question": " ",
            }, step)
        if step == 2:
            return answer("Provisional comparison before a reviewed peer answer.", step)
        if step == 3:
            return tool("peer_request", {
                "target_instance_id": "agent-critique",
                "question": "Independently check the synthetic comparison.",
            }, step)
        assert step == 4
        assert "failed_peer_request_still_unresolved" not in json.dumps(body["messages"])
        return answer("Revised comparison after the accepted independent review.", step)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="failed-peer-reviewed-answer", question="Request a peer check",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        _drive_until(host, run.run_id, lambda: bool(
            host._control(run.run_id, "agent-synthesis")["body"].get(
                "provisional_direct_report_ref"
            )
        ))
        sage = host._control(run.run_id, "agent-synthesis")
        candidate = sage["body"]["provisional_direct_report_ref"]
        assert len(host._unresolved_peer_actions(sage)) == 1
        _drive_until(host, run.run_id, lambda: bool(
            host._control(run.run_id, "agent-synthesis")["body"][
                "peer_action_obligations"
            ][0].get("request_ref")
        ))
        sage = host._control(run.run_id, "agent-synthesis")
        action = sage["body"]["peer_action_obligations"][0]
        request = host.store.get("mail_request", action["request_ref"]["id"])
        assert request["body"]["status"] == "unanswered"
        assert len(host._unresolved_peer_actions(sage)) == 1
        peer_report = host._save(
            "report", "synthetic-independent-peer-answer",
            {"mission_id": sage["body"]["mission_id"], "run_id": run.run_id,
             "body": "The independent peer checked the comparison."},
            mission_id=sage["body"]["mission_id"],
        )
        answered = host.mailbox.record_answer(
            "synthetic-reviewed-peer-answer",
            mission_id=sage["body"]["mission_id"], request_id=request["id"],
            answer_id="synthetic-peer-answer", responder_id=request["body"]["recipient_id"],
            artifact_ref=ref(peer_report),
        )
        reviewed = host.mailbox.review_answer(
            "synthetic-reviewed-peer-disposition",
            mission_id=sage["body"]["mission_id"], request_id=request["id"],
            reviewer_id=sage["body"]["actor_id"],
            answer_id=answered["answer_ref"]["id"], disposition="answered",
            reason="Use the independent check in the revision",
        )
        assert reviewed["status"] == "accepted"
        current_request = host.store.get("mail_request", request["id"])
        assert current_request["body"]["status"] == "answered"
        assert current_request["body"]["accepted_answer_ref"] == answered["answer_ref"]
        assert host._unresolved_peer_actions(sage) == []
        _drive_until(host, run.run_id, lambda: host._control(
            run.run_id, "agent-synthesis"
        )["body"]["phase"] == "done")
        final = host._control(run.run_id, "agent-synthesis")
        assert final["body"]["report_ref"] != candidate
        assert host.store.get("board_proposal", final["body"]["task_id"])["body"][
            "status"
        ] == "completed"
        assert len(sage_requests) == 4
    finally:
        host.close()


def test_scope_disposition_before_failure_does_not_erase_new_peer_obligation(
    tmp_path, monkeypatch,
) -> None:
    calls = []

    def transport(request, *, timeout):
        calls.append(json.loads(request.data))
        if len(calls) == 1:
            return tool("peer_request", {
                "target_instance_id": "Mxra", "question": "Check the claim.",
            }, 1)
        return answer("A direct candidate after the later failed request.", len(calls))

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="stale-peer-scope-disposition", question="Ask a peer to check",
            agent_ids=["agent-synthesis"],
        ))
        initial = host._control(run.run_id, "agent-synthesis")
        old = host.work.record_scope_disposition(
            "synthetic-scope-before-peer-failure", run_ref=ref(host._run(run.run_id)),
            actor_id=initial["body"]["actor_id"], source_control_ref=ref(initial),
            decisions=[], reason="An earlier scope assessment",
        )
        assert old["status"] == "recorded"
        _drive_until(host, run.run_id, lambda: bool(
            host._control(run.run_id, "agent-synthesis")["body"].get(
                "provisional_direct_report_ref"
            )
        ))
        current = host._control(run.run_id, "agent-synthesis")
        assert old["disposition_ref"] != current["body"].get("scope_disposition_ref")
        assert len(host._unresolved_peer_actions(current)) == 1
        assert current["body"]["peer_action_obligations"][0][
            "after_control_version"
        ] > initial["version"]
        assert host._run(run.run_id)["body"]["status"] == "active"
        assert host.store.get("board_proposal", current["body"]["task_id"])["body"][
            "status"
        ] == "open"
    finally:
        host.close()


def test_publish_result_needs_review_or_later_reasoned_scope_disposition(
    tmp_path, monkeypatch,
) -> None:
    calls = []
    contribution = {
        "title": "Synthetic comparison", "text": "A bounded comparison.",
        "citations": [], "limitations": ["Independent review was unavailable"],
    }

    def transport(request, *, timeout):
        body = json.loads(request.data)
        calls.append(body)
        step = len(calls)
        if step == 1:
            return tool("peer_request", {
                "target_instance_id": "Mxra", "question": "Review the comparison.",
            }, step)
        if step == 2:
            return tool("publish_result", contribution, step)
        assert step == 3
        assert "failed_peer_request_still_unresolved" in json.dumps(body["messages"])
        return tool("publish_result", {
            **contribution,
            "scope_completion": {
                "reason": "Mxra was not a registered peer; publish the bounded result with that limitation.",
                "decisions": [],
            },
        }, step)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="failed-peer-publish-scope", question="Seek a peer check",
            agent_ids=["agent-synthesis"],
        ))
        _drive_until(host, run.run_id, lambda: bool(
            len(host.store.records("context_tool_result")) >= 2
            and host._control(run.run_id, "agent-synthesis")["body"]["phase"]
            == "ready_model"
        ))
        control = host._control(run.run_id, "agent-synthesis")
        assert len(control["body"]["peer_action_obligations"]) == 1
        assert len(host._unresolved_peer_actions(control)) == 1
        rejected = next(
            item for item in host.store.records("context_tool_result")
            if json.loads(host.store.read_blob(item["body"]["blob"]["sha256"]))[
                "name"
            ] == "publish_result"
        )
        rejection = json.loads(host.store.read_blob(rejected["body"]["blob"]["sha256"]))
        result = json.loads(rejection["content"])
        assert result["status"] == "rejected"
        assert result["field"] == "scope_completion"
        assert "review" in result["next_action"]
        assert not host.store.records("report")
        assert not host.store.records("mission_scope_disposition")
        assert host.store.get("board_proposal", control["body"]["task_id"])["body"][
            "status"
        ] == "open"
        assert host._run(run.run_id)["body"]["status"] == "active"

        _drive_until(host, run.run_id, lambda: host._control(
            run.run_id, "agent-synthesis"
        )["body"]["phase"] == "done")
        final = host._control(run.run_id, "agent-synthesis")
        report = host.store.get(**final["body"]["report_ref"])
        assert report["body"]["body"] == contribution["text"]
        assert host.store.get("board_proposal", final["body"]["task_id"])["body"][
            "status"
        ] == "completed"
        dispositions = host.store.records("mission_scope_disposition")
        assert len(dispositions) == 1
        assert dispositions[0]["body"]["source_control_ref"]["version"] > (
            final["body"]["peer_action_obligations"][0]["after_control_version"]
        )
        assert len(calls) == 3
    finally:
        host.close()
