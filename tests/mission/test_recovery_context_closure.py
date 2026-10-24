"""Recover an accepted model tool request before its local action is scheduled."""

from __future__ import annotations

import json
import socket
import time

from ora.contracts.models import LiveStartRequest
from ora.mission.context import WorkingContext
from ora.mission.conversation import read_history
from tests.mission.test_service import service, tool


def _wait_for_model_work(host, run_id: str, transport_errors: list[AssertionError]) -> None:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        host._drive_run(host._run(run_id))
        if transport_errors:
            raise transport_errors[0]
        work = host._work_for(host._control(run_id, "agent-synthesis"))
        if work and work["body"]["phase"] == "completed":
            return
        time.sleep(0.01)
    raise AssertionError("The first model work was not accepted")


def _drive_until_complete(host, run_id: str, transport_errors: list[AssertionError]) -> None:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        host._drive_run(host._run(run_id))
        if transport_errors:
            raise transport_errors[0]
        if host._run(run_id)["body"]["status"] == "completed":
            return
        time.sleep(0.01)
    raise AssertionError("The reopened mission did not complete")


def test_accepted_unfinished_tool_turn_reopens_once_then_compacts(
    tmp_path, monkeypatch,
) -> None:
    def offline_only(*_args, **_kwargs):
        raise AssertionError("Recovery closure attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", offline_only)
    monkeypatch.setattr(socket.socket, "connect_ex", offline_only)
    question = "Assess the retained synthetic observation."
    evidence = "SYNTHETIC_RECOVERY_EVIDENCE with a stated limitation."
    summary = "The synthetic observation needs a qualified conclusion."
    private_reasoning = "SYNTHETIC_PRIVATE_REASONING_SENTINEL: consider the limitation."
    requests = []
    transport_errors: list[AssertionError] = []
    active_host = None
    reopened = False

    def transport(request, *, timeout):
        try:
            body = json.loads(request.data)
            requests.append(body)
            step = len(requests)
            control = active_host._control(run.run_id, "agent-synthesis")
            call_ref = control["body"]["call_ref"]
            call = active_host.store.get(**call_ref)
            intent = active_host.store.get(
                "model_request_intent", f"model-intent:{call_ref['id']}:{call_ref['version']}"
            )
            config = active_host.store.get(**intent["body"]["configuration_ref"])
            assert active_host.store.read_blob(intent["body"]["blob"]["sha256"]) == request.data
            assert body["model"] == config["body"]["model_id"] == pinned_model
            assert body["reasoning_effort"] == pinned_profile["reasoningSettings"]["reasoning_effort"]
            assert body["include_reasoning"] == pinned_profile["reasoningSettings"]["include_reasoning"]
            assert config["body"]["provider"] == pinned_profile["selectedProvider"]
            assert config["body"]["endpoint"] == pinned_profile["selectedEndpoint"]
            assert call["body"]["skills"] == config["body"]["skills"]
            assert intent["body"]["credential_profile"] == pinned_credential["credential_profile"]
            assert intent["body"]["quota_domain"] == pinned_credential["quota_domain"]
            assert question in json.dumps(body["messages"])
            assert evidence in json.dumps(body["messages"])
            assert private_reasoning not in request.data.decode()
            if step == 1:
                assert not reopened
                response = tool("request_capability", {
                    "tool_id": "compact_conversation"
                }, step)
                response.payload["choices"][0]["message"]["reasoning"] = private_reasoning
                return response
            assert reopened
            if step == 2:
                assistants = [item for item in body["messages"]
                              if item.get("role") == "assistant" and item.get("tool_calls")]
                exchange = next(item for item in assistants
                                if item["tool_calls"][0]["id"] == "tool-1")
                call = exchange["tool_calls"][0]
                assert call["function"]["name"] == "request_capability"
                assert json.loads(call["function"]["arguments"]) == {
                    "tool_id": "compact_conversation"
                }
                results = [item for item in body["messages"] if item.get("role") == "tool"
                           and item.get("tool_call_id") == "tool-1"]
                assert len(results) == 1 and results[0]["name"] == call["function"]["name"]
                assert json.loads(results[0]["content"])["status"] == "selected_for_next_turn"
                assert "compact_conversation" in {
                    item["function"]["name"] for item in body["tools"]
                }
                return tool("compact_conversation", {"summary": summary}, step)
            if step == 3:
                index = json.loads(body["messages"][-1]["content"])
                assert index["task"]["objective"] == question
                assert index["summary"]["text"] == summary
                assert not any(item.get("name") == "request_capability"
                               for item in body["messages"] if item.get("role") == "tool")
                assistants = [item for item in body["messages"]
                              if item.get("role") == "assistant" and item.get("tool_calls")]
                compact = next(item["tool_calls"][0] for item in assistants
                               if item["tool_calls"][0]["id"] == "tool-2")
                assert compact["function"]["name"] == "compact_conversation"
                assert json.loads(compact["function"]["arguments"]) == {"summary": summary}
                results = [item for item in body["messages"] if item.get("role") == "tool"
                           and item.get("tool_call_id") == "tool-2"]
                assert len(results) == 1 and results[0]["name"] == compact["function"]["name"]
                assert json.loads(results[0]["content"])["status"] == "saved"
                return tool("publish_result", {
                    "title": "Qualified synthetic conclusion", "text": summary,
                    "citations": [], "limitations": ["Synthetic observation only"],
                }, step)
            raise AssertionError("An accepted turn was repeated")
        except AssertionError as exc:
            transport_errors.append(exc)
            raise

    host = service(tmp_path, monkeypatch, transport)
    active_host = host
    run = host.start(LiveStartRequest(
        request_id="accepted-tool-restart-compaction", question=question,
        evidence_text=evidence, agent_ids=["agent-synthesis"],
    ))
    pinned_run = host._run(run.run_id)["body"]
    pinned_model = pinned_run["profile"]["selectedModel"]
    pinned_profile = pinned_run["profile"]
    pinned_catalog = pinned_run["skill_catalog"]
    pinned_credential = host._saved_credential(host._run(run.run_id))
    try:
        monkeypatch.setattr(host, "_advance_model", lambda _control, _work: False)
        _wait_for_model_work(host, run.run_id, transport_errors)
        pending = host._control(run.run_id, "agent-synthesis")
        assert pending["body"]["phase"] == "pending_model"
        assert len(requests) == 1
        assert len(host.store.records("model_response_receipt")) == 1
        receipt = host.store.records("model_response_receipt")[0]
        assert receipt["body"]["reasoning_status"] == "returned"
        reasoning = host.store.get(**receipt["body"]["reasoning_ref"])
        assert reasoning["body"]["status"] == "returned"
        raw = host.store.get(**reasoning["body"]["raw_ref"])
        assert host.store.read_blob(raw["body"]["blob"]["sha256"]) == private_reasoning.encode()
        assert private_reasoning not in host.store.read_blob(receipt["body"]["blob"]["sha256"]).decode()
        assert any(link["relation"] == "has_restricted_reasoning"
                   and link["target"] == receipt["body"]["reasoning_ref"]
                   for link in host.store.links(receipt))
        private_artifacts = list((tmp_path / "reasoning").glob("**/reasoning_trace.txt"))
        assert len(private_artifacts) == 1
        assert private_artifacts[0].read_bytes() == private_reasoning.encode()
        assert host.store.records("context_tool_result") == []
        original_config = host._configuration(pending)["body"]
    finally:
        host.close()

    host = service(tmp_path, monkeypatch, transport)
    active_host = host
    reopened = True
    try:
        assert host._run(run.run_id)["body"]["profile"] == pinned_profile
        assert host._run(run.run_id)["body"]["skill_catalog"] == pinned_catalog
        assert host._saved_credential(host._run(run.run_id)) == pinned_credential
        assert host.store.read_blob(host.store.get(**reasoning["body"]["raw_ref"])["body"][
            "blob"
        ]["sha256"]) == private_reasoning.encode()
        assert host._configuration(host._control(run.run_id, "agent-synthesis"))["body"] == original_config
        _drive_until_complete(host, run.run_id, transport_errors)
        assert len(requests) == 3
        assert len(host.store.records("mission_capability_selection")) == 1
        assert len(host.store.records("context_summary")) == 1
        assert len(host.store.records("conversation_archive")) == 1
        archive = host.store.records("conversation_archive")[0]
        control = host._control(run.run_id, "agent-synthesis")
        scope = {"archive_id": archive["id"], "mission_id": control["body"]["mission_id"],
                 "actor_id": control["body"]["actor_id"], "task_id": control["body"]["task_id"]}
        page = read_history(WorkingContext(host.store), **scope, cursor=0, limit=1800,
                            projection_sha256="")
        chunks = [page["content"]]
        while page["next_request"]:
            args = {key: value for key, value in page["next_request"].items()
                    if key != "tool"}
            page = read_history(WorkingContext(host.store), mission_id=scope["mission_id"],
                                actor_id=scope["actor_id"], task_id=scope["task_id"], **args)
            chunks.append(page["content"])
        archived = json.loads("".join(chunks))
        assert private_reasoning not in "".join(chunks)
        assert len(archived) == 1
        assistant, result = archived[0]["messages"]
        archived_call = assistant["tool_calls"][0]
        assert archived_call["id"] == result["tool_call_id"] == "tool-1"
        assert archived_call["function"]["name"] == result["name"] == "request_capability"
        assert json.loads(archived_call["function"]["arguments"]) == {
            "tool_id": "compact_conversation"
        }
        names = [
            json.loads(host.store.read_blob(item["body"]["blob"]["sha256"]))["name"]
            for item in host.store.records("context_tool_result")
        ]
        assert names.count("request_capability") == 1
        assert names.count("compact_conversation") == 1
        assert len(host.store.records("report")) == 1
        assert host._control(run.run_id, "agent-synthesis")["body"]["phase"] == "done"
    finally:
        host.close()
