"""Two real peer waits can expose a cycle and continue by a model-chosen action."""

from __future__ import annotations

import json
import socket
import time

from ora.contracts.models import LiveStartRequest
from tests.mission.test_service import serialized_peer_name, service, tool


def _drive_until(host, run_id: str, predicate, transport_failures=None) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        host._drive_run(host._run(run_id))
        if transport_failures:
            raise transport_failures[0]
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError("Reciprocal peer run did not reach its saved boundary")


def _last_tool(body: dict) -> tuple[str, dict]:
    for message in reversed(body["messages"]):
        if message["role"] == "tool":
            return message["name"], json.loads(message["content"])
    return "", {}  # A completed peer can be reactivated with a fresh conversation.


def test_reciprocal_peer_waits_replan_then_read_review_and_finish(tmp_path, monkeypatch):
    def no_network(*_args, **_kwargs):
        raise AssertionError("Synthetic peer cycle made an external network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    turns = {"Sage": 0, "Mxra": 0}
    requests = {}
    observed = {"cycle_feedback": False, "read": set(), "reviewed": set()}
    report_text = {
        "Sage": ("This synthetic observation permits only a limited response."
                 + " Additional synthetic detail." * 75),
        "Mxra": ("The peer response is limited, and this branch reaches the same narrow conclusion."
                 + " Additional synthetic detail." * 75),
    }
    read_parts = {"Sage": [], "Mxra": []}
    host = None
    run = None
    transport_failures = []

    def transport(request, *, timeout):
        body = json.loads(request.data)
        role = serialized_peer_name(body)
        other = "Mxra" if role == "Sage" else "Sage"
        target = "agent-critique" if role == "Sage" else "agent-synthesis"
        turns[role] += 1
        step = turns[role]
        response_index = step if role == "Sage" else 100 + step
        if step == 1:
            assert "peer_request" in {
                item["function"]["name"] for item in body["tools"]
            }
            return tool("peer_request", {
                "target_instance_id": target,
                "question": f"Please independently check {role}'s synthetic limitation.",
            }, response_index)
        if step == 2:
            name, result = _last_tool(body)
            assert name == "peer_request" and result["status"] == "request_saved"
            requests[role] = result["request_id"]
            return tool("request_capability", {"tool_id": "wait_for_dependencies"}, response_index)
        if step == 3:
            outgoing = host.store.get("mail_request", requests[role])
            target_control = host._control(run.run_id, target)
            context = json.loads(body["messages"][-1]["content"])
            assert any(item["ref"]["id"] == outgoing["id"]
                       for item in context["request_dispositions"])
            assert "wait_for_dependencies" in {
                item["function"]["name"] for item in body["tools"]
            }
            return tool("wait_for_dependencies", {"dependencies": [{
                "kind": "peer_request", "request_id": outgoing["id"],
                "request_version": outgoing["version"],
                "target_control_id": target_control["id"],
                "target_control_version": target_control["version"],
            }], "reason": "Wait for this peer's independent check."}, response_index)

        if role == "Sage" and step == 4:
            feedback = next(
                json.loads(message["content"])["host_feedback"]
                for message in body["messages"]
                if message["role"] == "user"
                and '"wait_needs_peer_replan"' in message["content"]
            )
            assert feedback["status"] == "wait_needs_peer_replan"
            assert {edge["input_ref"]["id"] for edge in feedback["cycles"][0]["edges"]} == (
                set(requests.values())
            )
            assert len(host.store.records("mission_wait_decision")) >= 2
            assert not any(item["body"].get("accepted_answer_ref")
                           for item in host.store.records("mail_request"))
            observed["cycle_feedback"] = True
            return tool("publish_result", {
                "title": "Sage's independent limited response",
                "text": report_text[role],
                "citations": [], "limitations": ["No external evidence was supplied"],
                "answer_request_id": requests[other],
                "scope_completion": {
                    "reason": "The reciprocal wait prevents further progress; answer the incoming "
                              "question independently and disclose the unreviewed outgoing request.",
                    "decisions": [],
                },
            }, response_index)

        if role == "Mxra" and step == 4:
            # Depending on scheduling, this peer may see the cycle before Sage's reply.
            outgoing = host.store.get("mail_request", requests[role])
            if not outgoing["body"]["answer_refs"]:
                assert "wait_needs_peer_replan" in json.dumps(body["messages"])
                target_control = host._control(run.run_id, target)
                return tool("wait_for_dependencies", {"dependencies": [{
                    "kind": "peer_request", "request_id": outgoing["id"],
                    "request_version": outgoing["version"],
                    "target_control_id": target_control["id"],
                    "target_control_version": target_control["version"],
                }], "reason": "Allow the other peer's independent continuation."}, response_index)

        context = json.loads(body["messages"][-1]["content"])
        outgoing = host.store.get("mail_request", requests[role])
        name, result = _last_tool(body)
        if name == "read_context_record":
            assert result["report_text_status"] == "available"
            read_parts[role].append(result["text"])
            if result["next_offset"] is not None:
                next_read = dict(result["next_request"])
                return tool(next_read.pop("tool"), next_read, response_index)
            assert json.loads("".join(read_parts[role]))["body"] == report_text[other]
            observed["read"].add(role)
            assert "review_peer_answer" in {
                item["function"]["name"] for item in body["tools"]
            }
            return tool("review_peer_answer", {
                "request_id": outgoing["id"],
                "answer_id": outgoing["body"]["answer_refs"][0]["id"],
                "disposition": "answered",
                "reason": "The saved limited response addresses the requested check.",
            }, response_index)
        if name == "review_peer_answer":
            assert result["status"] == "review_saved"
            assert outgoing["body"]["accepted_answer_ref"] == outgoing["body"]["answer_refs"][0]
            observed["reviewed"].add(role)
            return tool("publish_result", {
                "title": f"{role}'s independent continuation",
                "text": report_text[role],
                "citations": [], "limitations": ["No external evidence was supplied"],
            }, response_index)
        if role not in observed["read"]:
            assert outgoing["body"]["status"] in {"awaiting_review", "partial_awaiting_review"}
            assert outgoing["body"]["accepted_answer_ref"] is None
            answer_ref = outgoing["body"]["answer_refs"][0]
            peer_report_ref = host.store.get(**answer_ref)["body"]["artifact_ref"]
            cue = next(item for item in context["peer_answer_review_cues"]
                       if item["answer_ref"] == answer_ref)
            assert cue["request_ref"]["id"] == outgoing["id"]
            assert cue["read_request"]["id"] == peer_report_ref["id"]
            assert "peer_answer_previews" not in context
            assert report_text[other] not in json.dumps(body["messages"])
            material = next(item for item in context["shared_materials"]["entries"]
                            if item["ref"] == peer_report_ref)
            read = material["read_request"]
            assert read == cue["read_request"]
            assert read["tool"] == "read_context_record"
            assert "read_context_record" in {
                item["function"]["name"] for item in body["tools"]
            }
            return tool("read_context_record", {
                key: value for key, value in read.items() if key != "tool"
            }, response_index)
        raise AssertionError(f"Unexpected {role} continuation after {name}")

    def recording_transport(request, *, timeout):
        try:
            return transport(request, timeout=timeout)
        except Exception as exc:
            transport_failures.append(exc)
            raise

    host = service(tmp_path, monkeypatch, recording_transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="synthetic-reciprocal-peer-wait",
            question="Check a synthetic limitation with reciprocal peer requests",
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis", "agent-critique"],
        ))
        _drive_until(host, run.run_id, lambda: host._run(run.run_id)["body"]["status"] == "completed",
                     transport_failures)
        assert observed == {"cycle_feedback": True, "read": {"Sage", "Mxra"},
                            "reviewed": {"Sage", "Mxra"}}
        assert len({item["body"]["actor_id"] for item in host.store.records(
            "mission_wait_decision"
        )}) == 2
        assert len(host.store.records("mail_request")) == 2
        assert len(host.store.records("mail_answer")) == 2
        sage_outgoing = host.store.get("mail_request", requests["Sage"])
        mxra_outgoing = host.store.get("mail_request", requests["Mxra"])
        assert sage_outgoing["body"]["accepted_answer_ref"] == sage_outgoing["body"][
            "answer_refs"
        ][0]
        assert mxra_outgoing["body"]["accepted_answer_ref"] == mxra_outgoing["body"][
            "answer_refs"
        ][0]
        assert len(host.store.records("mission_scope_disposition")) == 1
        controls = [host._control(run.run_id, agent)
                    for agent in ("agent-synthesis", "agent-critique")]
        assert all(item["body"]["phase"] == "done" for item in controls)
        reports = [host.store.get(**item["body"]["report_ref"]) for item in controls]
        assert len({item["id"] for item in reports}) == 2
    finally:
        host.close()
