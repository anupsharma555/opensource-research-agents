"""An unread peer answer resumes through the saved mission after a clean host reopen."""

from __future__ import annotations

import json
import socket
import time
from pathlib import Path

import ora
from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from tests.mission.test_service import ROOT, answer, serialized_peer_name, service, tool

PEER_TEXT = (
    "The synthetic comparison has an unresolved measurement limitation."
    + " Additional synthetic detail." * 75
)
SOURCE_TEXT = "Synthetic source: the comparison has an unresolved measurement limitation."
SCOPE = "Use only the supplied synthetic observation and disclose its limits."


def _drive_until(host, run_id: str, predicate) -> None:
    for _ in range(500):
        host._drive_run(host._run(run_id))
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError("Peer reply restart case did not reach its saved boundary")


def _last_tool(request: dict) -> tuple[str, dict]:
    for message in reversed(request["messages"]):
        if message["role"] == "tool":
            return message["name"], json.loads(message["content"])
    raise AssertionError("Expected a saved tool result in the serialized model request")


def _disposition(request: dict) -> dict:
    index = json.loads(request["messages"][-1]["content"])
    return index["request_dispositions"][0]


def _saved_tool_names(host) -> list[str]:
    return [
        json.loads(host.store.read_blob(item["body"]["blob"]["sha256"]))["name"]
        for item in host.store.records("context_tool_result")
    ]


def test_unread_peer_reply_survives_clean_host_reopen_and_is_accepted(
    tmp_path, monkeypatch,
) -> None:
    # Any accidental provider or retrieval connection fails in this offline case.
    network_attempts = []

    def no_network(*_args, **_kwargs):
        network_attempts.append("socket connection")
        raise AssertionError("Offline peer restart case attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    assert Path(ora.__file__).resolve().is_relative_to(ROOT / "src")

    turns = {"Sage": 0, "Mxra": 0}
    captured: list[tuple[str, dict]] = []
    saved: dict = {}
    read_parts = []
    active_host = None

    def transport(request, *, timeout):
        body = json.loads(request.data)
        role = serialized_peer_name(body)
        turns[role] += 1
        step = turns[role]
        captured.append((role, body))
        if role == "Mxra":
            assert step == 1, "Completed peer work must not be repeated after reopen"
            peer_request = active_host.store.records("mail_request")[0]
            context = json.loads(body["messages"][-1]["content"])
            assert context["request_dispositions"][0]["ref"]["id"] == peer_request["id"]
            incoming = context["incoming_request"]
            assert incoming["ref"] == ref(peer_request)
            assert incoming["recipient_id"] == peer_request["body"]["recipient_id"]
            assert incoming["question"] == "Independently assess the synthetic comparison."
            assert SOURCE_TEXT in json.dumps(body["messages"])
            return tool("publish_result", {
                "title": "Independent measurement review",
                "text": PEER_TEXT,
                "citations": [saved["citation"]],
                "limitations": ["Synthetic source only"],
                "answer_request_id": peer_request["id"],
            }, 90)
        if step == 1:
            assert saved["question"] in json.dumps(body["messages"])
            assert SCOPE in json.dumps(body["messages"])
            assert SOURCE_TEXT in json.dumps(body["messages"])
            return tool("peer_request", {
                "target_instance_id": "agent-critique",
                "question": "Independently assess the synthetic comparison.",
            }, 1)
        if step == 2:
            name, result = _last_tool(body)
            assert name == "peer_request" and result["status"] == "request_saved"
            saved["request_id"] = result["request_id"]
            assert _disposition(body)["status"] == "unanswered"
            return tool("request_capability", {"tool_id": "wait_for_dependencies"}, 2)
        if step == 3:
            current = _disposition(body)
            assert current["status"] == "unanswered"
            assert "wait_for_dependencies" in {
                item["function"]["name"] for item in body["tools"]
            }
            return tool("wait_for_dependencies", {
                "dependencies": [{
                    "kind": "peer_request",
                    "request_id": current["ref"]["id"],
                    "request_version": current["ref"]["version"],
                    "target_control_id": current["recipient_control_ref"]["id"],
                    "target_control_version": current["recipient_control_ref"]["version"],
                }],
                "reason": "Await the independent assessment before finalizing.",
            }, 3)
        if step == 4:
            # The original host closed before this request was assembled.
            assert saved["reopened"] is True
            requester = active_host._control(saved["run_id"], "agent-synthesis")
            call_ref = requester["body"]["call_ref"]
            intent = active_host.store.get(
                "model_request_intent", f"model-intent:{call_ref['id']}:{call_ref['version']}"
            )
            assert json.loads(active_host.store.read_blob(intent["body"]["blob"]["sha256"])) == body
            assert saved["question"] in json.dumps(body["messages"])
            assert SCOPE in json.dumps(body["messages"])
            assert SOURCE_TEXT in json.dumps(body["messages"])
            source_packets = json.loads(body["messages"][1]["content"])["source_packets"]
            assert any(
                packet["id"] == saved["source_ref"]["id"]
                and packet["version"] == saved["source_ref"]["version"]
                for packet in source_packets
            )
            current = _disposition(body)
            assert current["ref"]["id"] == saved["request_id"]
            assert current["status"] in {"awaiting_review", "partial_awaiting_review"}
            assert current["answer_refs"] == [saved["answer_ref"]]
            index = json.loads(body["messages"][-1]["content"])
            assert saved["answer_ref"] in index["request_answer_refs"]
            assert any(
                detail["type"] == "request_answer"
                and detail["ref"] == saved["answer_ref"]
                for detail in index["details"]
            )
            # The complete report exceeds the preview cap, so use its reader.
            assert "peer_answer_previews" not in index
            assert PEER_TEXT not in json.dumps(body["messages"])
            peer_material = next(
                entry for entry in index["shared_materials"]["entries"]
                if entry["ref"] == saved["peer_report_ref"]
            )
            read_request = peer_material["read_request"]
            assert read_request["tool"] == "read_context_record"
            assert "read_context_record" in {
                item["function"]["name"] for item in body["tools"]
            }
            return tool("read_context_record", {
                key: value for key, value in read_request.items() if key != "tool"
            }, 4)
        name, result = _last_tool(body)
        if name == "read_context_record":
            assert result["report_text_status"] == "available"
            assert result["ref"] == saved["peer_report_ref"]
            read_parts.append(result["text"])
            if result["next_offset"] is not None:
                next_read = dict(result["next_request"])
                return tool(next_read.pop("tool"), next_read, step)
            assert json.loads("".join(read_parts))["body"] == PEER_TEXT
            current = _disposition(body)
            assert current["answer_refs"] == [saved["answer_ref"]]
            assert "review_peer_answer" in {
                item["function"]["name"] for item in body["tools"]
            }
            return tool("review_peer_answer", {
                "request_id": current["ref"]["id"],
                "answer_id": saved["answer_ref"]["id"],
                "disposition": "answered",
                "reason": "The saved report directly addresses the requested limitation.",
            }, step)
        assert name == "review_peer_answer" and result["status"] == "review_saved"
        assert _disposition(body)["accepted_answer_ref"] == saved["answer_ref"]
        return answer(
            "The independent peer found an unresolved measurement limitation "
            f"【{saved['source_ref']['id']}::{saved['citation']['span_id']}】; "
            "the synthetic source cannot support a broader conclusion.", step,
        )

    host = service(tmp_path, monkeypatch, transport)
    active_host = host
    try:
        request = LiveStartRequest(
            request_id="unread-peer-reply-restart",
            question="Assess the synthetic comparison and get independent peer review.",
            scope=SCOPE,
            evidence_text=SOURCE_TEXT,
            agent_ids=["agent-synthesis", "agent-critique"],
        )
        saved["question"] = request.question
        run = host.start(request)
        saved["run_id"] = run.run_id
        original_run = host._run(run.run_id)
        assert original_run["body"]["question"] == request.question
        assert original_run["body"]["scope"] == SCOPE
        original_run_profile = host._run(run.run_id)["body"]["profile"]
        original_credential = host._saved_credential(host._run(run.run_id))
        original_configs = {
            agent_id: host._configuration(host._control(run.run_id, agent_id))["body"]
            for agent_id in request.agent_ids
        }
        source = host.store.get(**host._control(run.run_id, "agent-synthesis")["body"][
            "source_refs"
        ][0])
        passage = host.store.get("source_passage", source["body"]["passage_ids"][0])
        saved["source_ref"] = ref(source)
        saved["citation"] = {
            "source_id": source["id"], "span_id": passage["body"]["span_id"],
        }
        recipient = host._control(run.run_id, "agent-critique")
        host._update_control(
            recipient, phase="retry_at", retry_at=time.time() + 3600,
            waiting_reason="Fixture holds peer until requester waits",
        )
        _drive_until(host, run.run_id, lambda: (
            turns["Sage"] == 3
            and bool(host._control(run.run_id, "agent-synthesis")["body"].get("wait_for"))
        ))
        requester = host._control(run.run_id, "agent-synthesis")
        assert requester["body"]["phase"] == "waiting"
        assert requester["body"]["wait_for"][0]["request_ref"]["id"] == saved["request_id"]
        assert turns["Mxra"] == 0
        recipient = host._control(run.run_id, "agent-critique")
        host._update_control(recipient, phase="ready_model", waiting_reason=None, retry_at=None)
        _drive_until(host, run.run_id, lambda: bool(
            host.store.get("mail_request", saved["request_id"])["body"]["answer_refs"]
        ))
        peer_answer = host.store.get("mail_request", saved["request_id"])["body"][
            "answer_refs"
        ][0]
        saved["answer_ref"] = peer_answer
        saved["peer_report_ref"] = host.store.get(**peer_answer)["body"]["artifact_ref"]
        peer_report = host.store.get(**saved["peer_report_ref"])
        assert peer_report["body"]["body"] == PEER_TEXT
        assert peer_report["body"]["citations"] == [{
            "sourceId": saved["citation"]["source_id"],
            "spanId": saved["citation"]["span_id"],
        }]
        assert peer_report["body"]["citation_input_provenance"][0][
            "input_status"
        ] == "exact_passage_in_producing_call"
        pending = host.store.get("mail_request", saved["request_id"])
        assert pending["body"]["status"] == "awaiting_review"
        assert pending["body"]["accepted_answer_ref"] is None
        assert pending["body"].get("reviewer_id") is None
        assert host._control(run.run_id, "agent-synthesis")["body"]["phase"] == "waiting"
        assert turns == {"Sage": 3, "Mxra": 1}
        assert _saved_tool_names(host).count("peer_request") == 1
        assert "read_context_record" not in _saved_tool_names(host)
        assert "review_peer_answer" not in _saved_tool_names(host)
        assert len(host.store.records("mail_request")) == 1
        assert len(host.store.records("mail_answer")) == 1
        assert len(host.store.records("report")) == 1
        assert host._run(run.run_id)["body"]["status"] == "active"
        original_run_ref = ref(host._run(run.run_id))
        original_controls = {
            agent_id: host._control(run.run_id, agent_id)
            for agent_id in request.agent_ids
        }
        original_tasks = {
            agent_id: host.store.get(
                "board_proposal", original_controls[agent_id]["body"]["task_id"]
            )
            for agent_id in request.agent_ids
        }
        assert all(task["body"]["objective"] == request.question for task in original_tasks.values())
        peer_actor_id = original_controls["agent-critique"]["body"]["actor_id"]
        assert sum(
            item["body"]["instance_id"] == peer_actor_id
            for item in host.store.records("agent_call")
        ) == 1
    finally:
        host.close()

    # A second service/session opens the same private StateStore. No prior host
    # object, report body, or tool result is passed into its continuation.
    active_host = None
    saved["reopened"] = True
    resumed = service(tmp_path, monkeypatch, transport)
    active_host = resumed
    try:
        assert ref(resumed._run(saved["run_id"])) == original_run_ref
        assert resumed._run(saved["run_id"])["body"]["question"] == request.question
        assert resumed._run(saved["run_id"])["body"]["scope"] == SCOPE
        assert resumed._run(saved["run_id"])["body"]["profile"] == original_run_profile
        assert resumed._saved_credential(resumed._run(saved["run_id"])) == original_credential
        for agent_id in request.agent_ids:
            current = resumed._control(saved["run_id"], agent_id)
            assert ref(current) == ref(original_controls[agent_id])
            assert current["body"]["actor_id"] == original_controls[agent_id]["body"][
                "actor_id"
            ]
            assert current["body"]["source_refs"] == original_controls[agent_id]["body"][
                "source_refs"
            ]
            assert resumed.store.get("board_proposal", current["body"]["task_id"]) == (
                original_tasks[agent_id]
            )
        assert {
            agent_id: resumed._configuration(resumed._control(saved["run_id"], agent_id))[
                "body"
            ]
            for agent_id in request.agent_ids
        } == original_configs
        assert len(resumed.store.records("mail_request")) == 1
        assert len(resumed.store.records("mail_answer")) == 1
        assert len(resumed.store.records("report")) == 1
        _drive_until(resumed, saved["run_id"], lambda: (
            resumed._run(saved["run_id"])["body"]["status"] == "completed"
        ))
        requester = resumed._control(saved["run_id"], "agent-synthesis")
        assert requester["body"]["phase"] == "done"
        reviewed = resumed.store.get("mail_request", saved["request_id"])
        assert reviewed["body"]["accepted_answer_ref"] == saved["answer_ref"]
        assert reviewed["body"]["reviewer_id"] == requester["body"]["actor_id"]
        assert reviewed["body"]["review"]["reason"] == (
            "The saved report directly addresses the requested limitation."
        )
        assert len(resumed.store.records("mail_request")) == 1
        assert len(resumed.store.records("mail_answer")) == 1
        assert len(resumed.store.records("report")) == 2
        assert _saved_tool_names(resumed).count("peer_request") == 1
        assert _saved_tool_names(resumed).count("read_context_record") == 2
        assert len(read_parts) == 2  # The exact report needed two bounded pages.
        assert _saved_tool_names(resumed).count("review_peer_answer") == 1
        assert sum(
            item["body"]["instance_id"] == peer_actor_id
            for item in resumed.store.records("agent_call")
        ) == 1
        final_ref = requester["body"]["report_ref"]
        assert final_ref != saved["peer_report_ref"]
        final = resumed.store.get(**final_ref)
        assert "independent peer found an unresolved measurement limitation" in final["body"]["body"]
        assert final["body"]["citations"] == [{
            "sourceId": saved["citation"]["source_id"],
            "spanId": saved["citation"]["span_id"],
        }]
        assert resumed.store.get("board_proposal", requester["body"]["task_id"])[
            "body"
        ]["status"] == "completed"
        assert turns == {"Sage": 7, "Mxra": 1}
        assert [role for role, _body in captured] == [
            "Sage", "Sage", "Sage", "Mxra", "Sage", "Sage", "Sage", "Sage",
        ]
        assert network_attempts == []
    finally:
        resumed.close()
