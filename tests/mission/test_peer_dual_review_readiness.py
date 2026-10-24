"""Rehearse two peer reviews through actual serialized mission inputs.

Questions are staged mechanically; scripted model replies use publish_result.
The injected chooser uses only the current packet to select a route, so this is
not natural model behavior.
"""

from __future__ import annotations

import json
import os
import socket
import time
from pathlib import Path

from ora.contracts.models import LiveStartRequest
from tests.mission.test_peer_review_path_efficiency import (
    _complete_visible_report,
    _visible_report_windows,
)
from tests.mission.test_service import serialized_peer_name, service, tool

AGENTS = {"Sage": "agent-synthesis", "Mxra": "agent-critique"}
QUESTIONS = {
    "Sage": "Check whether the synthetic finding separates observation from inference.",
    "Mxra": "Check whether the synthetic finding states its evidence limit.",
}
REPORTS = {
    "Sage": (
        "Independent check: the observation is limited to the supplied synthetic record. "
        "Inference: a broader population claim would require additional records. "
        "Counterpoint: an alternative explanation remains open. "
        "Recommendation: preserve the narrow conclusion and name the missing comparison."
    ),
    "Mxra": (
        "Independent check: the supplied synthetic record supports a narrow descriptive "
        "finding only. Its measurement provenance, sample selection, and external "
        "replication are unavailable. A competing explanation is compatible with "
        "the record. The report should retain this limitation and avoid a causal "
        "or clinical generalization."
    ),
}


def _drive_until(host, run_id: str, predicate) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        host._drive_run(host._run(run_id))
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError({
        "status": host._run(run_id)["body"]["status"],
        "phase": {role: host._control(run_id, agent)["body"]["phase"]
                  for role, agent in AGENTS.items()},
        "waiting_reason": {role: host._control(run_id, agent)["body"].get("waiting_reason")
                           for role, agent in AGENTS.items()},
        "feedback": {role: host._control(run_id, agent)["body"].get("feedback")
                     for role, agent in AGENTS.items()},
    })


def _tool_results(packet: dict) -> list[tuple[str, dict]]:
    return [(message["name"], json.loads(message["content"]))
            for message in packet["messages"] if message.get("role") == "tool"]


def test_two_peers_receive_substantive_answers_and_review_exact_requests(
    tmp_path, monkeypatch,
) -> None:
    evidence_dir = Path(os.environ.get("ORA_DUAL_REHEARSAL_EVIDENCE_DIR", tmp_path / "evidence"))
    def no_network(*_args, **_kwargs):
        raise AssertionError("Offline dual review must not open a socket")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    packets: list[dict] = []
    decisions: list[dict] = []
    failures: list[str] = []

    def choose(request, *, timeout):
        packet = json.loads(request.data)
        assert packet["model"] == "openai/gpt-oss-120b"
        assert packet["reasoning_effort"] == "medium"
        role = serialized_peer_name(packet)
        index = json.loads(packet["messages"][-1]["content"])
        names = {item["function"]["name"] for item in packet["tools"]}
        results = _tool_results(packet)
        packets.append({"role": role, "request": packet})
        routes = [item for key in ("peer_answer_previews", "peer_answer_review_cues")
                  for item in index.get(key, [])]
        routes += [result["peer_review"] for name, result in results
                   if name == "read_context_record"
                   and isinstance(result.get("peer_review"), dict)]
        details = index.get("details", [])
        outgoing_messages = {item["id"] for item in
                             index.get("outgoing_request_message_refs", [])}
        own_requests = [item for item in details if item.get("type") == "request"
                        and item["body"]["message_ref"]["id"] in outgoing_messages]
        incoming = index.get("incoming_request")
        if incoming and incoming["status"] == "unanswered":
            if own_requests:
                assert len(own_requests) == 1
                assert own_requests[0]["body"]["question"] == QUESTIONS[role]
            incoming_request_id = incoming["ref"]["id"]
            assert incoming["question"] == QUESTIONS[
                "Mxra" if role == "Sage" else "Sage"
            ]
            assert "publish_result" in names
            choice = ("publish_result", {
                "title": f"{role} synthetic independent check",
                "text": REPORTS[role], "citations": [],
                "limitations": ["Synthetic fixture only"],
                "answer_request_id": incoming_request_id,
                "scope_completion": {
                    "reason": "Answer the incoming peer question before reviewing the outgoing one.",
                    "decisions": [],
                },
            })
            decisions.append({"role": role, "tool": choice[0], "arguments": choice[1]})
            return tool(*choice, len(decisions))
        for item in own_requests:
            if not item["body"]["answer_refs"]:
                continue
            answer_ref = item["body"]["answer_refs"][0]
            answer_detail = next(entry for entry in details
                                 if entry.get("ref") == answer_ref)
            report_ref = answer_detail["body"]["artifact_ref"]
            routes.append({
                "question": item["body"]["question"],
                "request_ref": item["ref"],
                "answer_ref": answer_ref,
                "report_ref": report_ref,
                "read_request": {
                    "tool": "read_context_record",
                    "inclusion_id": index["older_records"]["inclusion_id"],
                    **report_ref, "offset": 0, "limit": 2000,
                },
            })
        owned_routes = routes
        assert len({item["request_ref"]["id"] for item in owned_routes}) == 1, (
            role, routes, own_requests,
        )
        own_request_id = owned_routes[0]["request_ref"]["id"]
        assert own_request_id == f"dual-request-{role.lower()}"
        assert all(item["question"] == QUESTIONS[role] for item in owned_routes)
        own = [item for item in routes if item.get("request_ref", {}).get("id") ==
               own_request_id]
        assert len({item["answer_ref"]["id"] for item in own}) == 1, (role, index)
        route = own[0]
        assert route["request_ref"]["id"] == f"dual-request-{role.lower()}"
        if "owner" in route:
            assert route["owner"] == "you"
        assert all(item.get("request_ref", {}).get("id") == own_request_id
                   for item in routes)
        preview = next((item for item in index.get("peer_answer_previews", [])
                        if item["answer_ref"] == route["answer_ref"]), None)
        if preview is None:
            assert route["answer_ref"] in index.get("request_answer_refs", [])
        else:
            assert route["answer_ref"] not in index.get("request_answer_refs", [])
        inline = preview is not None and preview.get("report", {}).get("body") == REPORTS[
            "Mxra" if role == "Sage" else "Sage"
        ]
        report_ref = route.get("report_ref")
        if report_ref is None:
            report_ref = next((item["body"]["artifact_ref"] for item in details
                               if item.get("ref") == route["answer_ref"]), None)
        if report_ref is None and route.get("read_request"):
            read_ref = route["read_request"]
            if read_ref["kind"] == "report":
                report_ref = {key: read_ref[key] for key in ("kind", "id", "version")}
        windows = _visible_report_windows(packet, report_ref) if report_ref else []
        complete = _complete_visible_report(windows)
        if complete is not None:
            assert json.loads(complete)["body"] == REPORTS[
                "Mxra" if role == "Sage" else "Sage"
            ]

        if not inline and complete is None:
            if windows:
                read = windows[-1].get("next_request")
                assert read is not None, "Missing earlier report pages in current input"
            else:
                read = route.get("read_request")
            assert read is not None
            read = dict(read)
            selected = read.pop("tool")
            if selected not in names:
                assert "request_capability" in names
                choice = ("request_capability", {"tool_id": selected})
            else:
                choice = (selected, read)
        elif not any(name == "review_peer_answer" for name, _ in results):
            if "review_peer_answer" in names:
                choice = ("review_peer_answer", {
                    "request_id": route["request_ref"]["id"],
                    "answer_id": route["answer_ref"]["id"],
                    "disposition": "answered",
                    "reason": "The complete synthetic assessment addresses this exact question.",
                })
            else:
                assert "request_capability" in names
                choice = ("request_capability", {"tool_id": "review_peer_answer"})
        else:
            raise AssertionError("Chooser continued after a saved review")
        decisions.append({"role": role, "tool": choice[0], "arguments": choice[1]})
        assert len([item for item in decisions if item["role"] == role]) <= 6
        return tool(*choice, len(decisions))

    def transport(request, *, timeout):
        try:
            return choose(request, timeout=timeout)
        except Exception as exc:
            failures.append(f"{type(exc).__name__}: {exc}")
            raise

    host = service(tmp_path, monkeypatch, transport)
    # A frozen local runtime can carry an operator allowlist for different run IDs.
    # This test's adapter transport is injected and has no provider connection.
    monkeypatch.setattr(host.credential_profiles, "is_run_enabled", lambda *_args: True)
    monkeypatch.setattr(host.session.adapter.credential_profiles,
                        "is_enabled", lambda *_args: True)
    try:
        run = host.start(LiveStartRequest(
            request_id="dual-offline-review-readiness",
            question="Compare two narrow synthetic checks and incorporate each peer answer.",
            agent_ids=list(AGENTS.values()), required_agent_ids=list(AGENTS.values()),
        ))
        run_id = run.run_id
        mission_id = host._run(run_id)["body"]["mission_id"]
        for role, agent_id in AGENTS.items():
            host._update_control(host._control(run_id, agent_id), phase="retry_at",
                                 retry_at=time.time() + 86400,
                                 waiting_reason="Fixture stages the two answered requests")
        for role, agent_id in AGENTS.items():
            requester = host._control(run_id, agent_id)
            responder = host._control(run_id, AGENTS["Mxra" if role == "Sage" else "Sage"])
            request_id = f"dual-request-{role.lower()}"
            sent = host.mailbox.send_request(
                f"dual-send-{role.lower()}", mission_id=mission_id,
                request_id=request_id, message_id=f"dual-message-{role.lower()}",
                requester_id=requester["body"]["actor_id"],
                recipient_id=responder["body"]["actor_id"],
                question=QUESTIONS[role], expected_artifact="A synthetic assessment",
                cause_ref=requester["body"]["proposal_ref"],
                payload={"question": QUESTIONS[role]},
                task_ref=requester["body"]["proposal_ref"],
            )
            assert sent["status"] == "accepted", sent
            delivered = host.mailbox.mark_delivered(
                f"dual-delivery-{role.lower()}", mission_id=mission_id,
                message_id=f"dual-message-{role.lower()}",
            )
            assert delivered["status"] == "accepted", delivered
        for agent_id in AGENTS.values():
            host._update_control(host._control(run_id, agent_id), phase="ready_model",
                                 retry_at=None, waiting_reason=None)
        try:
            _drive_until(host, run_id,
                         lambda: bool(failures) or all(
                             host.store.get("mail_request", f"dual-request-{role.lower()}")[
                                 "body"]["status"] == "answered" for role in AGENTS
                         ))
        except AssertionError as exc:
            raise AssertionError({"state": str(exc), "choices": [
                (item["role"], item["tool"]) for item in decisions]}) from exc
        evidence_dir.mkdir(parents=True, exist_ok=True)
        (evidence_dir / "debug-sequence.json").write_text(json.dumps({
            "failures": failures, "choices": decisions, "packets": packets,
        }, indent=2) + "\n")
        assert not failures, failures
        assert len(packets) == len(decisions)
        assert len(host.store.records("mail_request")) == 2
        assert len(host.store.records("mail_answer")) == 2
        assert {item["tool"] for item in decisions} <= {
            "read_context_record", "request_capability", "review_peer_answer", "publish_result",
        }
        assert len(host.store.records("report")) == 2
        for role, agent_id in AGENTS.items():
            request = host.store.get("mail_request", f"dual-request-{role.lower()}")
            assert request["body"]["status"] == "answered"
            assert request["body"]["accepted_answer_ref"] == request["body"]["answer_refs"][0]
            assert request["body"]["reviewer_id"] == host._control(
                run_id, agent_id)["body"]["actor_id"]
            assert request["body"]["review"]["reason"] == (
                "The complete synthetic assessment addresses this exact question."
            )
            assert request["version"] > 3
            assert [item["tool"] for item in decisions if item["role"] == role] == [
                "publish_result", "review_peer_answer",
            ]
            answer = host.store.get(**request["body"]["accepted_answer_ref"])
            report = host.store.get(**answer["body"]["artifact_ref"])
            assert report["body"]["body"] == REPORTS[
                "Mxra" if role == "Sage" else "Sage"
            ]
        review_events = [event for event in host.store.events(mission_id)
                         if event["type"] == "peer_request_reviewed"]
        assert len(review_events) == 2
        assert {event["body"]["request_ref"]["id"] for event in review_events} == {
            f"dual-request-{role.lower()}" for role in AGENTS
        }
        checkpoint = evidence_dir / f"state-{os.getpid()}-{time.time_ns()}"
        host.store.backup(checkpoint)
        (evidence_dir / "measurement.json").write_text(json.dumps({
            "basis": "offline_staged_questions_scripted_publish_replies_packet_only_choices",
            "run_id": run_id, "state_dir": str(checkpoint), "provider_calls": 0,
            "run_status_at_review_boundary": host._run(run_id)["body"]["status"],
            "transport_exceptions": failures, "choices": decisions,
            "serialized_requests": packets,
        }, indent=2) + "\n")
    finally:
        host.close()
