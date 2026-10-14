"""Planning is discoverable in saved developer requests without forced selection."""

from __future__ import annotations

import hashlib
import json

import pytest

from ora.contracts.models import LiveStartRequest
from ora.live.engine import ResearchHost
from tests.live.test_engine import FakeProvider, wait_done
from tests.mission.test_capability_catalog import _index
from tests.mission.test_service import ROOT, answer, drain, service, tool

LABEL = "## Planning skill: task-planning"


def _saved_packets(host, dispatch_bytes):
    intents = host.store.records("model_request_intent")
    assert len(intents) == len(dispatch_bytes)
    dispatched = {hashlib.sha256(raw).hexdigest(): raw for raw in dispatch_bytes}
    saved_packets = {}
    for intent in intents:
        body = intent["body"]
        raw = host.store.read_blob(body["blob"]["sha256"])
        assert raw == dispatched[body["request_sha256"]]
        assert hashlib.sha256(raw).hexdigest() == body["request_sha256"]
        saved_packets[body["request_sha256"]] = json.loads(raw)
    return [saved_packets[hashlib.sha256(raw).hexdigest()] for raw in dispatch_bytes]


@pytest.mark.parametrize("selection", ["skip", "activate", "request_tool"])
def test_serialized_developer_instructions_label_optional_planning_with_full_catalog(
    tmp_path, monkeypatch, selection,
):
    dispatch_bytes = []

    def transport(request, *, timeout):
        dispatch_bytes.append(request.data)
        step = len(dispatch_bytes)
        if selection == "activate":
            if step == 1:
                return tool("discover_capabilities", {}, step)
            if step == 2:
                return tool("activate_skill", {"skill_id": "task-planning"}, step)
        if selection == "request_tool" and step == 1:
            return tool("request_capability", {"tool_id": "read_plan"}, step)
        return answer("A concise provisional answer is sufficient for this synthetic task.", step)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id=f"planning-description-{selection}",
            question="Give a short provisional answer using only this question.",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id)
        assert host.state(run.run_id).snapshot.mission.status == "completed"
        expected_calls = {"skip": 1, "activate": 3, "request_tool": 2}[selection]
        assert len(dispatch_bytes) == expected_calls
        packets = _saved_packets(host, dispatch_bytes)
        for packet in packets:
            assert packet["model"] == "openai/gpt-oss-120b"
            developer = packet["messages"][0]
            assert developer["role"] == "developer"
            instructions = developer["content"]
            assert instructions.count(LABEL) == 1
            section = instructions.split(LABEL + "\n", 1)[1].split("\n\n", 1)[0]
            assert len(section.split()) <= 100
            assert "complex, uncertain or dependent tasks" in section
            assert "Simple tasks may skip planning" in section
            assert "Plans are advisory" in section
            assert "No planning call is required" in section
            assert {"discover_capabilities", "activate_skill", "request_capability",
                    "save_plan", "read_plan"} <= set(section.replace("/", " ").split())
            catalog = _index(instructions)
            assert len(catalog["tools"]) == 45
            assert {"browser_search", "search_scholarly", "propose_work", "revise_work",
                    "peer_request", "wait_for_dependencies", "save_plan", "read_plan"} <= set(catalog["tools"])
        initial = packets[0]
        attached = {item["function"]["name"] for item in initial["tools"]}
        assert {"save_plan", "read_plan"}.isdisjoint(attached)
        assert "# Task planning" not in initial["messages"][0]["content"]
        assert _index(initial["messages"][0]["content"])["skills"]["task-planning"]["state"] == "available_to_activate"
        control = host._control(run.run_id, "agent-synthesis")["body"]
        last_attached = {item["function"]["name"] for item in packets[-1]["tools"]}
        if selection == "activate":
            assert control["active_skill_ids"] == ["task-planning"]
            assert "# Task planning" in packets[-1]["messages"][0]["content"]
            assert {"save_plan", "read_plan"} <= last_attached
        else:
            assert not control.get("active_skill_ids")
            assert "# Task planning" not in packets[-1]["messages"][0]["content"]
            if selection == "request_tool":
                assert "read_plan" in last_attached and "save_plan" not in last_attached
        assert not host.store.records("working_note")
    finally:
        host.close()


@pytest.mark.parametrize("availability", ["old_catalog", "legacy_lifecycle", "missing_tools"])
def test_saved_requests_do_not_advertise_planning_without_skill_and_tool_availability(
    tmp_path, monkeypatch, availability,
):
    dispatch_bytes = []

    def transport(request, *, timeout):
        dispatch_bytes.append(request.data)
        return answer("A bounded synthetic answer needs no planning checkpoint.", 1)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id=f"planning-unavailable-{availability}", question="Answer briefly.",
            agent_ids=["agent-synthesis"],
        ))
        if availability == "missing_tools":
            host.catalog.tools.pop("save_plan")
            host.catalog.tools.pop("read_plan")
        else:
            saved = host._run(run.run_id)
            body = saved["body"]
            if availability == "legacy_lifecycle":
                body.pop("skill_lifecycle_version")
            else:
                for role_pins in body["skill_catalog"].values():
                    role_pins.pop("task-planning", None)
            host._save(
                "mission_run", run.run_id, body, mission_id=body["mission_id"],
                operation_id=f"old-planning-description-{availability}",
            )
        drain(host, run.run_id)
        assert host.state(run.run_id).snapshot.mission.status == "completed"
        assert len(dispatch_bytes) == 1
        packet = _saved_packets(host, dispatch_bytes)[0]
        assert packet["messages"][0]["role"] == "developer"
        assert LABEL not in packet["messages"][0]["content"]
        assert "# Task planning" not in packet["messages"][0]["content"]
        assert {"save_plan", "read_plan"}.isdisjoint(_index(packet["messages"][0]["content"])["tools"])
    finally:
        host.close()


def test_supplied_evidence_legacy_runtime_does_not_receive_mission_planning_instructions(tmp_path):
    messages = []

    class Provider(FakeProvider):
        def complete(self, prompt, tools=None):
            messages.append(prompt)
            return self._response(content="The supplied synthetic observation is limited.")

    host = ResearchHost(ROOT, tmp_path, Provider())
    try:
        run = host.start(LiveStartRequest(
            request_id="legacy-planning-description", question="Summarize the supplied observation.",
            evidence_text="A synthetic report observes a change without a comparison group.",
            agent_ids=["agent-synthesis"],
        ))
        _, status = wait_done(host, run.run_id)
        assert status == "completed" and len(messages) == 1
        assert messages[0][0]["role"] == "system"
        assert LABEL not in messages[0][0]["content"]
        assert "task-planning" not in messages[0][0]["content"]
        assert "save_plan" not in messages[0][0]["content"]
        assert "read_plan" not in messages[0][0]["content"]
    finally:
        host.close()
