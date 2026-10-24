"""Known local effects survive reopen while an unknown model response is fenced."""

from __future__ import annotations

import json
import socket
import time

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from tests.mission.test_service import service, tool


def _drive_until(host, run_id: str, predicate, *, timeout: float = 10) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        host._drive_run(host._run(run_id))
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("Known/uncertain recovery did not reach its saved boundary")


def _tool_results(host, name: str) -> list[tuple[dict, dict]]:
    matches = []
    for item in host.store.records("context_tool_result"):
        message = json.loads(host.store.read_blob(item["body"]["blob"]["sha256"]))
        if message["name"] == name:
            matches.append((item, json.loads(message["content"])))
    return matches


def _last_tool(request: dict, name: str) -> dict:
    message = next(
        item for item in reversed(request["messages"])
        if item.get("role") == "tool" and item.get("name") == name
    )
    return json.loads(message["content"])


def test_known_tool_result_is_reused_across_unknown_model_recovery(
    tmp_path, monkeypatch,
) -> None:
    network_attempts = []

    def no_network(*_args, **_kwargs):
        network_attempts.append("socket connection")
        raise AssertionError("Offline recovery case attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    calls: list[dict] = []
    tool_invocations: list[tuple[str, str]] = []
    saved: dict = {}
    active_host = None

    def observe_tools(host) -> None:
        original = host.executor.handlers[("mission-tool", "1")]

        def observed(spec):
            control = host._control_for_spec(spec)
            tool_invocations.append((control["body"]["tool_call"]["name"], spec.work_id))
            return original(spec)

        host.executor.handlers[("mission-tool", "1")] = observed

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        calls.append(packet)
        step = len(calls)
        if step == 1:
            assert "write_note" not in {item["function"]["name"] for item in packet["tools"]}
            return tool("request_capability", {"tool_id": "write_note"}, step)
        if step == 2:
            assert "write_note" in {item["function"]["name"] for item in packet["tools"]}
            return tool("write_note", {
                "note_id": "synthetic-known-observation",
                "category": "checkpoint",
                "text": "The synthetic comparison still needs source validation.",
                "expected_version": 0,
            }, step)
        if step == 3:
            for key in (
                "model", "max_completion_tokens", "include_reasoning",
                "reasoning_effort", "stream", "parallel_tool_calls",
            ):
                assert packet[key] == calls[0][key]
            result = _last_tool(packet, "write_note")
            assert result["status"] == "saved"
            assert result["note_ref"] == saved["note_ref"]
            assert saved["message_ref"]["id"] in json.dumps(packet["messages"])
            delivered = active_host.store.get("mail_message", saved["message_ref"]["id"])
            index = json.loads(packet["messages"][-1]["content"])
            assert ref(delivered) in index["message_refs"]
            assert any(
                detail["type"] == "message" and detail["ref"] == ref(delivered)
                and detail["body"]["payload"] == saved["notice"]
                for detail in index["details"]
            )
            saved["unknown_request"] = packet
            raise RuntimeError("Synthetic response lost after model dispatch")
        assert step == 4, "An uncertain original dispatch must not be replayed"
        for key in (
            "model", "max_completion_tokens", "include_reasoning",
            "reasoning_effort", "stream", "parallel_tool_calls",
        ):
            assert packet[key] == calls[0][key]
        assert saved["note_ref"]["id"] in json.dumps(packet["messages"])
        assert "model_response_lost" in json.dumps(packet["messages"])
        assert saved["message_ref"]["id"] in json.dumps(packet["messages"])
        return tool("publish_result", {
            "title": "Bounded synthetic checkpoint",
            "text": "The saved comparison still needs source validation.",
            "citations": [],
            "limitations": ["No verified source was supplied"],
        }, step)

    host = service(tmp_path, monkeypatch, transport)
    active_host = host
    observe_tools(host)
    run = host.start(LiveStartRequest(
        request_id="known-then-unknown-recovery",
        question="Record a synthetic checkpoint, then give a bounded assessment.",
        agent_ids=["agent-synthesis"],
    ))
    saved["run_id"] = run.run_id
    try:
        _drive_until(host, run.run_id, lambda: (
            host.store.get("working_note", "synthetic-known-observation") is not None
            and host._control(run.run_id, "agent-synthesis")["body"]["phase"]
            == "ready_model"
        ))
        assert len(calls) == 2
        note = host.store.get("working_note", "synthetic-known-observation")
        saved["note_ref"] = ref(note)
        saved_result, saved_content = _tool_results(host, "write_note")[0]
        assert saved_content["status"] == "saved"
        assert saved_content["note_ref"] == saved["note_ref"]
        saved["tool_result_ref"] = ref(saved_result)
        saved["tool_result_bytes"] = host.store.read_blob(
            saved_result["body"]["blob"]["sha256"]
        )
        note_artifacts = [
            item for item in host.store.records("executor_artifact")
            if saved["note_ref"] in item["body"].get("output_refs", [])
        ]
        assert len(note_artifacts) == 1
        saved["note_artifact_ref"] = ref(note_artifacts[0])
        note_receipts = [
            item for item in host.store.records("executor_receipt")
            if item["body"]["work_id"] == note_artifacts[0]["body"]["work_id"]
        ]
        assert len(note_receipts) == 1
        saved["note_receipt_ref"] = ref(note_receipts[0])
        control = host._control(run.run_id, "agent-synthesis")
        task = host.store.get("board_proposal", control["body"]["task_id"])
        notice = {"type": "synthetic_notification", "text": "Review the saved checkpoint."}
        saved["notice"] = notice
        message_args = dict(
            mission_id=control["body"]["mission_id"],
            message_id="synthetic-known-result-notice",
            sender_id=control["body"]["actor_id"],
            recipient_id=control["body"]["actor_id"],
            correlation_id="synthetic-known-result", payload=notice,
            task_ref=ref(task), evidence_refs=[],
        )
        sent = host.mailbox.send_message("synthetic-known-result-send", **message_args)
        assert host.mailbox.send_message("synthetic-known-result-send", **message_args) == sent
        saved["message_ref"] = sent["message_ref"]
        saved["message_args"] = message_args
        saved["profile"] = host._run(run.run_id)["body"]["profile"]
        saved["configuration"] = host._configuration(control)["body"]
        saved["run_ref"] = ref(host._run(run.run_id))
        saved["task_ref"] = ref(task)
        assert len(host.store.records("working_note")) == 1
        assert len(_tool_results(host, "write_note")) == 1
        assert len(host.store.records("mail_message")) == 1
        assert [name for name, _work_id in tool_invocations].count("write_note") == 1
    finally:
        host.close()

    active_host = None
    host = service(tmp_path, monkeypatch, transport)
    active_host = host
    observe_tools(host)
    try:
        assert ref(host._run(run.run_id)) == saved["run_ref"]
        assert host.mailbox.send_message(
            "synthetic-known-result-send", **saved["message_args"]
        )["message_ref"] == saved["message_ref"]
        assert host.store.get(**saved["note_ref"]) == note
        assert host.store.get("working_note", note["id"]) == note
        assert host.store.get(**saved["tool_result_ref"]) == saved_result
        assert host.store.get("context_tool_result", saved_result["id"]) == saved_result
        assert host.store.get(**saved["note_artifact_ref"]) == note_artifacts[0]
        assert host.store.get(**saved["note_receipt_ref"]) == note_receipts[0]
        assert host.store.get("executor_artifact", note_artifacts[0]["id"]) == note_artifacts[0]
        assert host.store.get("executor_receipt", note_receipts[0]["id"]) == note_receipts[0]
        assert host.store.read_blob(saved_result["body"]["blob"]["sha256"]) == saved[
            "tool_result_bytes"
        ]
        _drive_until(host, run.run_id, lambda: any(
            item["body"]["phase"] == "uncertain"
            for item in host.store.records("capacity_operation")
        ))
        assert len(calls) == 3
        assert len(host.store.records("working_note")) == 1
        assert host.store.get("working_note", note["id"]) == note
        assert len(_tool_results(host, "write_note")) == 1
        assert len(host.store.records("mail_message")) == 1
        assert [name for name, _work_id in tool_invocations].count("write_note") == 1
        control = host._control(run.run_id, "agent-synthesis")
        unknown_call = control["body"]["call_ref"]
        unknown_intent = host.store.get(
            "model_request_intent",
            f"model-intent:{unknown_call['id']}:{unknown_call['version']}",
        )
        assert json.loads(host.store.read_blob(unknown_intent["body"]["blob"][
            "sha256"
        ])) == saved["unknown_request"]
        assert host.store.get(
            "model_response_receipt", f"model-response:{unknown_intent['id']}"
        ) is None
        dispatch = host.store.get("model_dispatch_attempt", f"model-dispatch:{unknown_intent['id']}")
        assert dispatch["body"]["status"] == "attempted_external_effect"
        saved["unknown_call_ref"] = unknown_call
        saved["unknown_intent_ref"] = ref(unknown_intent)
        saved["unknown_request_hash"] = unknown_intent["body"]["request_sha256"]
    finally:
        host.close()

    active_host = None
    host = service(tmp_path, monkeypatch, transport)
    active_host = host
    observe_tools(host)
    try:
        assert len(calls) == 3
        assert host._run(run.run_id)["body"]["profile"] == saved["profile"]
        assert host._configuration(host._control(run.run_id, "agent-synthesis"))[
            "body"
        ] == saved["configuration"]
        _drive_until(host, run.run_id, lambda: (
            host._run(run.run_id)["body"]["status"] == "completed"
        ), timeout=12)
        assert len(calls) == 4
        assert host.store.get(**saved["unknown_intent_ref"])["body"][
            "request_sha256"
        ] == saved["unknown_request_hash"]
        assert host.store.get(
            "model_response_receipt", f"model-response:{saved['unknown_intent_ref']['id']}"
        ) is None
        losses = host.store.records("model_response_lost")
        assert len(losses) == 1
        assert losses[0]["body"]["call_ref"] == saved["unknown_call_ref"]
        assert losses[0]["body"]["intent_ref"] == saved["unknown_intent_ref"]
        assert losses[0]["body"]["request_sha256"] == saved["unknown_request_hash"]
        assert losses[0]["body"]["remote_response"] == "unknown"
        assert losses[0]["body"]["usage"] == "unknown_conservative_quota_charge_retained"
        final_call = host._control(run.run_id, "agent-synthesis")["body"]["call_ref"]
        assert final_call != saved["unknown_call_ref"]
        assert host.store.get("mission_call_budget", final_call["id"])["body"][
            "model_response_lost_ref"
        ] == ref(losses[0])
        assert host.store.get(**saved["note_ref"]) == note
        assert host.store.get("working_note", note["id"]) == note
        assert len(host.store.records("working_note")) == 1
        assert len(_tool_results(host, "write_note")) == 1
        assert len(host.store.records("mail_message")) == 1
        assert len(host.store.records("report")) == 1
        assert host.store.get(**saved["tool_result_ref"]) == saved_result
        assert host.store.get("context_tool_result", saved_result["id"]) == saved_result
        assert host.store.get(**saved["note_artifact_ref"]) == note_artifacts[0]
        assert host.store.get(**saved["note_receipt_ref"]) == note_receipts[0]
        assert host.store.get("executor_artifact", note_artifacts[0]["id"]) == note_artifacts[0]
        assert host.store.get("executor_receipt", note_receipts[0]["id"]) == note_receipts[0]
        assert [name for name, _work_id in tool_invocations].count("write_note") == 1
        for _ in range(3):
            assert host._drive_run(host._run(run.run_id)) == (False, None)
        assert len(calls) == 4
        assert len(host.store.records("report")) == 1
        assert len(host.store.records("model_response_lost")) == 1
        assert network_attempts == []
    finally:
        host.close()
