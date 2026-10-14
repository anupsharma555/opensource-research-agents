"""Script optional task planning through the normal offline mission boundary.

These synthetic transports select tools through the full capability catalog.
They prove persistence and continuation mechanics, not scientific planning quality.
"""

from __future__ import annotations

import copy
import json
import time

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from tests.mission.test_capability_catalog import _index
from tests.mission.test_service import answer, drain, revoke_source, service, tool


def _attached(packet):
    return {entry["function"]["name"] for entry in packet["tools"]}


def _last_tool(packet):
    message = next(item for item in reversed(packet["messages"]) if item["role"] == "tool")
    return message["name"], json.loads(message["content"])


def _scripted(transport):
    """Expose script assertions that the model adapter correctly captures as failures."""
    failures = []

    def checked(*args, **kwargs):
        try:
            return transport(*args, **kwargs)
        except (AssertionError, KeyError) as exc:
            failures.append(repr(exc))
            raise

    checked.failures = failures
    return checked


def _plan(*, links=(), goal="Compare synthetic evidence and unresolved alternatives"):
    return {
        "goal": goal,
        "completion_criteria": ["Preserve inspected evidence and state unresolved limitations"],
        "uncertainties": ["The initial source excerpt may omit relevant counterevidence"],
        "status": "active",
        "steps": [
            {"id": "inspect", "description": "Read the relevant source window",
             "status": "todo", "depends_on": [], "linked_refs": list(links)},
            {"id": "compare", "description": "Compare evidence and peer response",
             "status": "blocked", "depends_on": ["inspect"], "linked_refs": []},
        ],
        "linked_refs": list(links),
        "revision_reason": "Initial task-proportionate continuation plan",
    }


def _drive_until(host, run_id, predicate):
    deadline = time.monotonic() + 15
    while time.monotonic() < deadline:
        host._drive_run(host._run(run_id))
        if predicate():
            return
        time.sleep(.01)
    raise AssertionError("Scripted planning boundary was not reached")


@pytest.mark.parametrize("agent_id,catalog_count", [
    ("agent-synthesis", 45), ("agent-critique", 45),
])
def test_simple_completion_keeps_full_catalog_and_needs_no_plan(
    tmp_path, monkeypatch, agent_id, catalog_count,
):
    packets = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        catalog = _index(packet["messages"][0]["content"])
        assert len(catalog["tools"]) == catalog_count
        assert {"save_plan", "read_plan", "browser_search", "calculate", "propose_work"} <= set(catalog["tools"])
        assert {"save_plan", "read_plan"}.isdisjoint(_attached(packet))
        assert catalog["skills"]["task-planning"]["state"] == "available_to_activate"
        return answer("A concise provisional response is sufficient for this synthetic task.", 1)

    transport = _scripted(transport)
    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id=f"simple-without-plan-{agent_id}", question="Give a brief provisional answer",
            agent_ids=[agent_id],
        ))
        drain(host, run.run_id)
        assert not transport.failures
        assert host.state(run.run_id).snapshot.mission.status == "completed"
        assert len(packets) == 1
        assert not host.store.records("working_note")
        before = host.store.records("executor_work")
        assert host._drive_run(host._run(run.run_id)) == (False, None)
        assert host.store.records("executor_work") == before
        assert len(packets) == 1
    finally:
        host.close()


def test_plan_revision_peer_links_compaction_and_restart_preserve_execution_authority(
    tmp_path, monkeypatch,
):
    packets = []
    peer_packets = []
    plans = []
    source = None
    peer_request = None
    proposal = None
    window_ref = None
    read_chunks = []
    completed_saved = False

    def transport(request, *, timeout):
        nonlocal window_ref, completed_saved
        packet = json.loads(request.data)
        if "independent methods reviewer critic peer." in packet["messages"][0]["content"]:
            peer_packets.append(packet)
            return tool("publish_result", {
                "title": "Synthetic peer review", "text": "Compare the complete source before claiming absence.",
                "citations": [], "limitations": ["Synthetic fixture"],
                "answer_request_id": peer_request["id"],
            }, 100 + len(peer_packets))
        packets.append(packet)
        step = len(packets)
        names = _attached(packet)
        if step == 1:
            assert {"save_plan", "read_plan"}.isdisjoint(names)
            return tool("activate_skill", {"skill_id": "task-planning"}, step)
        assert {"save_plan", "read_plan"} <= names
        if step == 2:
            plans.append(_plan(links=[proposal, source, peer_request]))
            return tool("save_plan", {"plan": plans[-1], "expected_version": 0}, step)
        if step == 7:
            assert any(
                item["role"] == "user" and '"summary"' in item.get("content", "")
                for item in packet["messages"]
            )
            return tool("read_plan", {}, step)
        name, result = _last_tool(packet)
        if step == 3:
            assert name == "save_plan" and result["note_ref"]["version"] == 1
            return tool("read_source_window", {
                "source_id": source["id"], "source_version": source["version"],
                "block_offset": 0, "char_offset": 0, "char_limit": 100,
            }, step)
        if step == 4:
            assert name == "read_source_window" and result["passages"]
            window_ref = result["window_ref"]
            revised = copy.deepcopy(plans[-1])
            revised["steps"][0]["status"] = "done"
            revised["steps"][1]["status"] = "in_progress"
            revised["steps"][1]["linked_refs"] = [window_ref]
            revised["revision_reason"] = "The inspected source window changes the next comparison"
            plans.append(revised)
            return tool("save_plan", {"plan": revised, "expected_version": 1}, step)
        if step == 5:
            assert name == "save_plan" and result["note_ref"]["version"] == 2
            return tool("request_capability", {"tool_id": "compact_conversation"}, step)
        if step == 6:
            assert "compact_conversation" in names
            return tool("compact_conversation", {
                "summary": "Continue the current task. The durable plan retains evidence and peer links; compare inspected text and report limits.",
            }, step)
        if step >= 8 and not completed_saved:
            assert name in {"read_plan", "read_tool_result"}
            if result.get("schema") == "ora.saved_tool_result_window.v1":
                read_chunks.append(result["text"])
                if result.get("next_request"):
                    next_request = {key: value for key, value in result["next_request"].items() if key != "tool"}
                    assert "read_tool_result" in names
                    return tool("read_tool_result", next_request, step)
                result = json.loads("".join(read_chunks))
            assert result["status"] == "available"
            assert result["note"]["version"] == 2
            assert json.loads(result["note"]["body"]["text"]) == plans[-1]
            assert result["effective_state"]["task_status"] == "open"
            completed = copy.deepcopy(plans[-1])
            completed["status"] = "completed"
            completed["steps"][1]["status"] = "done"
            completed["revision_reason"] = "The bounded comparison is ready for a provisional report"
            plans.append(completed)
            completed_saved = True
            return tool("save_plan", {"plan": completed, "expected_version": 2}, step)
        assert completed_saved and name in {"save_plan", "review_peer_answer"}
        if name == "save_plan":
            assert result["planning_status"] == "advisory_not_task_completion"
            context = json.loads(packet["messages"][-1]["content"])
            disposition = next(item for item in context["request_dispositions"] if item["ref"]["id"] == peer_request["id"])
            return tool("review_peer_answer", {
                "request_id": peer_request["id"], "answer_id": disposition["answer_refs"][0]["id"],
                "disposition": "answered", "reason": "The saved review answers the focused question",
            }, step)
        return tool("publish_result", {
            "title": "Synthetic comparison", "text": "Inspected evidence and peer input remain provisional.",
            "citations": [], "limitations": ["Synthetic fixture; scientific support is not qualified"],
        }, step)

    transport = _scripted(transport)
    host = service(tmp_path, monkeypatch, transport)
    run = host.start(LiveStartRequest(
        request_id="plan-complex-continuation", question="Compare evidence and peer alternatives",
        evidence_text="Synthetic initial evidence requires a careful comparison of its limitations. " * 4,
        agent_ids=["agent-synthesis", "agent-critique"],
    ))
    control = host._control(run.run_id, "agent-synthesis")
    peer = host._control(run.run_id, "agent-critique")
    source = control["body"]["source_refs"][0]
    proposal = control["body"]["proposal_ref"]
    sent = host.mailbox.send_request(
        "planning-fixture-peer-request", mission_id=control["body"]["mission_id"],
        request_id="planning-peer-question", message_id="planning-peer-message",
        requester_id=control["body"]["actor_id"], recipient_id=peer["body"]["actor_id"],
        question="Which source limitations could change this comparison?",
        expected_artifact="Focused source limitation review", cause_ref=proposal,
        task_ref=proposal, payload={"question": "Review the source limitation"}, evidence_refs=[source],
    )
    assert sent["status"] == "accepted"
    peer_request = sent["request_ref"]
    try:
        _drive_until(host, run.run_id, lambda: (
            len(packets) == 6
            and host._control(run.run_id, "agent-synthesis")["body"]["phase"] == "ready_model"
        ))
        notes = host.store.records("working_note")
        assert len(notes) == 1 and notes[0]["version"] == 2
        note_id = notes[0]["id"]
        assert source in notes[0]["body"]["input_refs"]
        assert any(
            link["relation"] == "plan_basis" and link["target"] == peer_request
            for link in host.store.links(notes[0])
        )
        assert len(host._controls(run.run_id)) == 2
        # Local dependencies are advisory: the proposal itself is unchanged.
        assert host.store.get(**proposal)["version"] == 1
        assert host.store.get(**proposal)["body"]["depends_on"] == []
    finally:
        host.close()

    resumed = service(tmp_path, monkeypatch, transport)
    try:
        drain(resumed, run.run_id, limit=12)
        assert not transport.failures
        assert resumed.state(run.run_id).snapshot.mission.status == "completed", {
            "calls": len(packets), "peer_calls": len(peer_packets),
            "controls": [{key: control["body"].get(key) for key in ("phase", "waiting_reason", "feedback")}
                         for control in resumed._controls(run.run_id)],
        }
        assert len(packets) >= 9 and len(peer_packets) == 1
        current = resumed.store.get("working_note", note_id)
        assert current["version"] == 3
        assert json.loads(current["body"]["text"]) == plans[-1]
        assert json.loads(resumed.store.get("working_note", note_id, 1)["body"]["text"]) == plans[0]
        assert json.loads(resumed.store.get("working_note", note_id, 2)["body"]["text"]) == plans[1]
        assert len(resumed.store.records("source_read_window")) == 1
        assert resumed._control(run.run_id, "agent-synthesis")["body"]["conversation_summary_ref"]
        assert len({(packet["model"], packet["reasoning_effort"]) for packet in packets}) == 1
        work_before = resumed.store.records("executor_work")
        call_count = len(packets)
        assert resumed._drive_run(resumed._run(run.run_id)) == (False, None)
        assert resumed.store.records("executor_work") == work_before
        assert len(packets) == call_count
    finally:
        resumed.close()


def test_individual_plan_tool_request_does_not_activate_planning_skill(tmp_path, monkeypatch):
    packets = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        step = len(packets)
        catalog = _index(packet["messages"][0]["content"])
        assert catalog["skills"]["task-planning"]["state"] == "available_to_activate"
        if step == 1:
            assert "read_plan" not in _attached(packet)
            return tool("request_capability", {"tool_id": "read_plan"}, step)
        assert "read_plan" in _attached(packet) and "save_plan" not in _attached(packet)
        if step == 2:
            name, result = _last_tool(packet)
            assert name == "request_capability" and result["status"] == "selected_for_next_turn"
            return tool("read_plan", {}, step)
        assert step == 3
        name, result = _last_tool(packet)
        assert name == "read_plan" and result["status"] == "absent" and result["note"] is None
        return answer("No saved plan is needed for this bounded task.", step)

    transport = _scripted(transport)
    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="independent-plan-reader", question="Check whether a saved plan exists",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id)
        assert not transport.failures
        assert len(packets) == 3
        assert host.state(run.run_id).snapshot.mission.status == "completed"
        assert not host.store.records("working_note")
        assert not host._control(run.run_id, "agent-synthesis")["body"]["active_skill_ids"]
    finally:
        host.close()


@pytest.mark.parametrize("revoke_after_read", [False, True])
def test_revoked_plan_source_is_hidden_from_read_and_saved_tool_argument_replay(
    tmp_path, monkeypatch, revoke_after_read,
):
    packets = []
    source = None
    secret = "SYNTHETIC_WITHDRAWN_PLANNING_CONTENT_731"

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        step = len(packets)
        if step == 1:
            return tool("activate_skill", {"skill_id": "task-planning"}, step)
        if step == 2:
            assert secret in request.data.decode()
            return tool("save_plan", {
                "plan": _plan(links=[source], goal=secret), "expected_version": 0,
            }, step)
        if step == 3:
            if revoke_after_read:
                assert secret in request.data.decode()
            else:
                assert secret not in request.data.decode()
            return tool("read_plan", {}, step)
        assert secret not in request.data.decode()
        if step == 4 and revoke_after_read:
            name, replayed = _last_tool(packet)
            assert name == "read_plan"
            assert "text" not in replayed["note"]["body"]
            return tool("read_plan", {}, step)
        name, result = _last_tool(packet)
        assert step == (5 if revoke_after_read else 4), {
            "last_tool": name, "status": result.get("status"), "reason": result.get("reason"),
        }
        assert name == "read_plan" and result["status"] == "data_unavailable_due_source_use"
        assert "text" not in result["note"]["body"]
        return tool("read_plan", {}, step)

    transport = _scripted(transport)
    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id=f"revoked-planning-source-{revoke_after_read}", question="Retain a source-bound task plan",
            evidence_text=secret, agent_ids=["agent-synthesis"],
        ))
        source = host._control(run.run_id, "agent-synthesis")["body"]["source_refs"][0]
        _drive_until(host, run.run_id, lambda: (
            len(packets) == (3 if revoke_after_read else 2)
            and bool(host.store.records("working_note"))
            and host._control(run.run_id, "agent-synthesis")["body"]["phase"] == "ready_model"
        ))
        note = host.store.records("working_note")[0]
        assert secret in note["body"]["text"]
        revoke_source(host, source, host._run(run.run_id)["body"]["mission_id"])
        _drive_until(host, run.run_id, lambda: (
            len(packets) == (5 if revoke_after_read else 4)
            and host._control(run.run_id, "agent-synthesis")["body"]["phase"] == "ready_model"
        ))
        assert not transport.failures
        assert len(packets) == (5 if revoke_after_read else 4)
        # Immutable retained history remains intact while current input is redacted.
        assert secret in host.store.get(**ref(note))["body"]["text"]
        saved = host._run(run.run_id)
        host._save("mission_run", saved["id"], {**saved["body"], "status": "cancelled"},
                   mission_id=saved["body"]["mission_id"])
        assert host._drive_run(saved) == (False, None)
        assert len(packets) == (5 if revoke_after_read else 4)
    finally:
        host.close()


def test_cancelled_run_retains_saved_plan_without_new_dispatch(tmp_path, monkeypatch):
    packets = []

    def transport(request, *, timeout):
        packets.append(json.loads(request.data))
        if len(packets) == 1:
            return tool("activate_skill", {"skill_id": "task-planning"}, 1)
        assert len(packets) == 2
        return tool("save_plan", {"plan": _plan(), "expected_version": 0}, 2)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="cancel-saved-plan", question="Retain unfinished planning work",
            agent_ids=["agent-synthesis"],
        ))
        _drive_until(host, run.run_id, lambda: (
            len(packets) == 2 and bool(host.store.records("working_note"))
            and host._control(run.run_id, "agent-synthesis")["body"]["phase"] == "ready_model"
        ))
        saved = host._run(run.run_id)
        note = host.store.records("working_note")[0]
        host._save("mission_run", saved["id"], {**saved["body"], "status": "cancelled"},
                   mission_id=saved["body"]["mission_id"])
        work = host.store.records("executor_work")
        assert host._drive_run(saved) == (False, None)
        assert host.store.records("executor_work") == work
        assert host.store.get(**ref(note)) == note
        assert len(packets) == 2
    finally:
        host.close()


def test_plan_churn_rejection_leaves_publication_usable(tmp_path, monkeypatch):
    packets = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        step = len(packets)
        assert _index(packet["messages"][0]["content"])["skills"]["task-planning"]["state"] == "available_to_activate"
        if step == 1:
            return tool("request_capability", {"tool_id": "save_plan"}, step)
        assert "save_plan" in _attached(packet) and "read_plan" not in _attached(packet)
        if 2 <= step <= 5:
            plan = _plan(goal=f"Substantive synthetic plan revision {step}")
            return tool("save_plan", {"plan": plan, "expected_version": step - 2}, step)
        assert step == 6
        name, result = _last_tool(packet)
        assert name == "save_plan" and result["status"] == "rejected"
        assert "allowance exhausted" in result["reason"]
        return tool("publish_result", {
            "title": "Bounded source-free result", "text": "A provisional contribution completes this small task.",
            "citations": [], "limitations": ["Synthetic fixture"],
        }, step)

    transport = _scripted(transport)
    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="plan-churn-local-rejection", question="Prepare a bounded provisional contribution",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id)
        assert not transport.failures
        assert len(packets) == 6
        assert host.state(run.run_id).snapshot.mission.status == "completed"
        note = host.store.records("working_note")[0]
        assert note["version"] == 3
        assert note["body"]["planning"]["writes_without_progress"] == 3
        assert len(host._controls(run.run_id)) == 1
    finally:
        host.close()


def test_paused_plan_and_peer_wait_survive_restart_without_dispatch(tmp_path, monkeypatch):
    packets = []
    accepted = {}
    host = None
    run = None

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        step = len(packets)
        assert "independent methods reviewer critic peer." not in packet["messages"][0]["content"]
        if step == 1:
            return tool("activate_skill", {"skill_id": "task-planning"}, step)
        if step == 2:
            return tool("peer_request", {
                "target_instance_id": "agent-critique", "question": "Review this synthetic uncertainty",
            }, step)
        if step == 3:
            name, result = _last_tool(packet)
            assert name == "peer_request" and result["status"] == "request_saved"
            accepted.update(result)
            plan = _plan(links=[result["request_ref"]])
            plan["status"] = "paused"
            return tool("save_plan", {"plan": plan, "expected_version": 0}, step)
        if step == 4:
            name, result = _last_tool(packet)
            assert name == "save_plan" and result["status"] == "saved"
            return tool("request_capability", {"tool_id": "wait_for_dependencies"}, step)
        assert step == 5 and "wait_for_dependencies" in _attached(packet)
        request_ref = ref(host.store.get("mail_request", accepted["request_id"]))
        target_ref = ref(host._control(run.run_id, "agent-critique"))
        return tool("wait_for_dependencies", {
            "reason": "The next useful comparison depends on this requested independent review",
            "dependencies": [{"kind": "peer_request", "request_id": request_ref["id"],
                              "request_version": request_ref["version"],
                              "target_control_id": target_ref["id"],
                              "target_control_version": target_ref["version"]}],
        }, step)

    transport = _scripted(transport)
    host = service(tmp_path, monkeypatch, transport)
    run = host.start(LiveStartRequest(
        request_id="paused-plan-durable-wait", question="Compare an unresolved peer uncertainty",
        agent_ids=["agent-synthesis", "agent-critique"],
    ))
    recipient = host._control(run.run_id, "agent-critique")
    host._update_control(recipient, phase="waiting", waiting_reason="Synthetic peer work is held")
    try:
        _drive_until(host, run.run_id, lambda: (
            len(packets) == 5 and host._control(run.run_id, "agent-synthesis")["body"].get("wait_for")
        ))
        assert not transport.failures
        work = host.store.records("executor_work")
        note = host.store.records("working_note")[0]
        assert json.loads(note["body"]["text"])["status"] == "paused"
        assert len(host.store.records("mission_wait_decision")) == 1
        assert host._control(run.run_id, "agent-synthesis")["body"]["phase"] == "waiting"
        for _ in range(5):
            host._drive_run(host._run(run.run_id))
        assert len(packets) == 5 and host.store.records("executor_work") == work
    finally:
        host.close()

    reopened = service(tmp_path, monkeypatch, transport)
    try:
        for _ in range(5):
            reopened._drive_run(reopened._run(run.run_id))
        assert len(packets) == 5 and reopened.store.records("executor_work") == work
        assert len(reopened._controls(run.run_id)) == 2
        assert reopened.store.get(**ref(note)) == note
        saved = reopened._run(run.run_id)
        reopened._save("mission_run", saved["id"], {**saved["body"], "status": "cancelled"},
                       mission_id=saved["body"]["mission_id"])
        assert reopened._drive_run(saved) == (False, None)
        assert len(packets) == 5 and reopened.store.records("executor_work") == work
    finally:
        reopened.close()
