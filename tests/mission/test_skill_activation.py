"""Exercise durable, agent-selected skill and tool exposure without providers."""

from __future__ import annotations

import copy
import hashlib
import json
import shutil
import time

import pytest

from ora.contracts.models import LiveStartRequest
from ora.mission.skill_activation import checked_registry
from ora.state import StateError
from tests.mission.test_service import ROOT, answer, drain, service, tool


def test_optional_activation_release_and_explicit_tools_survive_restart(tmp_path, monkeypatch):
    requests = []

    def transport(request, *, timeout):
        body = json.loads(request.data)
        requests.append(body)
        step = len(requests)
        names = {entry["function"]["name"] for entry in body["tools"]}
        instructions = body["messages"][0]["content"]
        assert "Use exact source and span identifiers" in instructions
        assert "Link a narrow claim" in instructions or "# Evidence linking" in instructions
        if step == 1:
            assert "study-appraisal" in instructions
            assert "# Study appraisal" not in instructions
            assert "write_note" not in names
            return tool("activate_skill", {"skill_id": "study-appraisal"}, step)
        if step in {2, 3, 4}:
            assert "# Study appraisal" in instructions
            assert {"write_note", "read_note", "search_notes"} <= names
        if step == 2:
            return tool("write_note", {"note_id": "appraisal", "category": "hypothesis",
                        "text": "A synthetic preliminary assessment.", "expected_version": 0}, step)
        if step == 3:
            return tool("request_capability", {"tool_id": "read_note"}, step)
        if step == 4:
            return tool("release_skill", {"skill_id": "study-appraisal"}, step)
        assert "# Study appraisal" not in instructions
        assert "write_note" not in names and "search_notes" not in names
        if step == 5:
            assert "read_note" in names
            return tool("read_note", {"note_id": "appraisal"}, step)
        if step == 6:
            result = json.loads(next(m for m in reversed(body["messages"]) if m["role"] == "tool")["content"])
            assert result["note"]["body"]["text"] == "A synthetic preliminary assessment."
            return tool("release_capability", {"tool_id": "read_note"}, step)
        assert step == 7 and "read_note" not in names
        return answer("A bounded synthetic assessment with uncertainty.", step)

    host = service(tmp_path, monkeypatch, transport)
    run = host.start(LiveStartRequest(request_id="skill-lifecycle", question="Appraise synthetic evidence",
                                     agent_ids=["agent-critique"]))
    for _ in range(300):
        host._drive_run(host._run(run.run_id))
        control = host._control(run.run_id, "agent-critique")
        if control["body"].get("active_skill_ids") and control["body"]["phase"] == "ready_model":
            break
        time.sleep(0.01)
    else:
        raise AssertionError("Activation did not reach durable next-call state")
    assert len(requests) == 1
    pins = copy.deepcopy(host._run(run.run_id)["body"]["skill_catalog"])
    host.close()
    resumed = service(tmp_path, monkeypatch, transport)
    try:
        drain(resumed, run.run_id, limit=25)
        assert resumed.state(run.run_id).snapshot.mission.status == "completed"
        assert len(requests) == 7
        assert resumed._run(run.run_id)["body"]["skill_catalog"] == pins
        control = resumed._control(run.run_id, "agent-critique")
        assert control["body"]["active_skill_ids"] == []
        assert control["body"]["requested_capabilities"] == []
        calls = resumed.store.records("agent_call")
        assert any("study-appraisal" in c["body"]["skills"] for c in calls)
        assert any("study-appraisal" not in c["body"]["skills"] for c in calls)
        assert len({(body["model"], body["reasoning_effort"]) for body in requests}) == 1
    finally:
        resumed.close()
    after_release = service(tmp_path, monkeypatch, transport)
    try:
        control = after_release._control(run.run_id, "agent-critique")
        config = after_release._configuration(control)
        exposed, _ = after_release._exposed_tools(control, config)
        assert "study-appraisal" not in config["body"]["skills"]
        assert "read_note" not in exposed
    finally:
        after_release.close()


@pytest.mark.parametrize("skill_id", ["unknown-skill", "study-appraisal", "evidence-linking"])
def test_unknown_ungranted_and_mandatory_activation_rejected(tmp_path, monkeypatch, skill_id):
    requests = []

    def transport(request, *, timeout):
        body = json.loads(request.data)
        requests.append(body)
        if len(requests) == 1:
            return tool("activate_skill", {"skill_id": skill_id}, 1)
        result = json.loads(next(m for m in reversed(body["messages"]) if m["role"] == "tool")["content"])
        assert result["status"] == "rejected"
        assert "# Study appraisal" not in body["messages"][0]["content"]
        return answer("Continue a bounded synthetic contribution.", 2)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(request_id="ungranted-skill", question="Check synthetic evidence",
                                         agent_ids=["agent-synthesis"]))
        drain(host, run.run_id)
        assert len(requests) == 2
        assert not host._control(run.run_id, "agent-synthesis")["body"].get("active_skill_ids")
    finally:
        host.close()


def test_pinned_skill_drift_rejected_and_saved_configuration_preserved(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda request, *, timeout: answer("Unused", 1))
    try:
        run = host.start(LiveStartRequest(request_id="skill-drift", question="Synthetic question",
                                         agent_ids=["agent-critique"]))
        control = host._control(run.run_id, "agent-critique")
        pins = copy.deepcopy(host._run(run.run_id)["body"]["skill_catalog"]["independent_methods_reviewer_critic"])
        changed_root = tmp_path / "changed-package"
        for relative in ("runtime_skills", "roles", "src/ora/skills"):
            shutil.copytree(ROOT / relative, changed_root / relative)
        concise = changed_root / "runtime_skills/study_appraisal/CONCISE.md"
        concise.write_text(concise.read_text() + "\nChanged synthetic instruction.\n")
        manifest_path = concise.parent / "manifest.json"
        manifest = json.loads(manifest_path.read_text())
        manifest["instruction_variants"]["concise"]["sha256"] = hashlib.sha256(concise.read_bytes()).hexdigest()
        manifest_path.write_text(json.dumps(manifest))
        with pytest.raises(StateError, match="Pinned skill package changed"):
            checked_registry(changed_root, "independent_methods_reviewer_critic", pins, ["study-appraisal"])
        with monkeypatch.context() as changed:
            changed.setattr(host, "project_root", changed_root)
            active = {**control, "body": {**control["body"], "active_skill_ids": ["study-appraisal"]}}
            with pytest.raises(StateError, match="Pinned skill package changed"):
                host._configuration(active)
        config = host._configuration(control)
        assert "study-appraisal" not in config["body"]["skills"]
        assert host.store.get(**{key: config[key] for key in ("kind", "id", "version")}) == config
    finally:
        host.close()


def test_legacy_run_keeps_eager_instructions_without_lifecycle_exposure(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda request, *, timeout: answer("Unused", 1))
    try:
        receipt = host.start(LiveStartRequest(request_id="legacy-skills", question="Synthetic question",
                                             agent_ids=["agent-critique"]))
        run = host._run(receipt.run_id)
        body = {key: value for key, value in run["body"].items()
                if key not in {"skill_lifecycle_version", "skill_catalog"}}
        host._save("mission_run", run["id"], body, mission_id=body["mission_id"], operation_id="legacy-fixture")
        control = host._control(receipt.run_id, "agent-critique")
        config = host._configuration(control)
        assert "study-appraisal" in config["body"]["skills"]
        assert "# Study appraisal" in config["body"]["instructions"]
        exposed, _ = host._exposed_tools(control, config)
        assert "activate_skill" not in exposed
    finally:
        host.close()


def test_legacy_selection_receipt_replays_after_interrupted_control_update(tmp_path, monkeypatch):
    requests = []
    retained = []

    def transport(request, *, timeout):
        body = json.loads(request.data)
        requests.append(body)
        if len(requests) == 1:
            return tool("request_capability", {"tool_id": "read_note"}, 1)
        assert "read_note" in {item["function"]["name"] for item in body["tools"]}
        return answer("Synthetic recovery completed.", 2)

    host = service(tmp_path, monkeypatch, transport)
    try:
        receipt = host.start(LiveStartRequest(request_id="legacy-selection-replay", question="Synthetic question",
                                             agent_ids=["agent-synthesis"]))
        run = host._run(receipt.run_id)
        legacy = {key: value for key, value in run["body"].items()
                  if key not in {"skill_lifecycle_version", "skill_catalog"}}
        host._save("mission_run", run["id"], legacy, mission_id=legacy["mission_id"], operation_id="legacy-fixture")
        advance = host._advance_tool

        def interrupted(control, work):
            from ora.coordination.board import ref
            from ora.mission.service import _key

            if not retained:
                body = control["body"]
                prior = host.store.get(**body["call_ref"])
                selection = host._save(
                    "mission_capability_selection", _key("capability-selection-", work["id"]),
                    {"mission_id": body["mission_id"], "run_id": body["run_id"],
                     "task_id": body["task_id"], "actor_id": body["actor_id"],
                     "source_call_ref": body["call_ref"], "source_work_ref": ref(work),
                     "tool_id": "read_note", "prior_exposed_tools": prior["body"]["exposed_tools"],
                     "next_exposed_tools": sorted(set(prior["body"]["exposed_tools"]) | {"read_note"}),
                     "reason": "Agent requested a granted tool for its next model turn"},
                    mission_id=body["mission_id"], operation_id=f"mission-capability-selection:{work['id']}",
                )
                retained.append(host.store.get(**selection))
                return False
            return advance(control, work)

        monkeypatch.setattr(host, "_advance_tool", interrupted)
        drain(host, receipt.run_id)
        assert len(requests) == 2
        assert host.state(receipt.run_id).snapshot.mission.status == "completed"
        assert host.store.records("mission_capability_selection") == retained
    finally:
        host.close()


def test_old_oversized_result_exposes_reader_after_restart_without_new_flag(tmp_path, monkeypatch):
    requests = []

    def transport(request, *, timeout):
        body = json.loads(request.data)
        requests.append(body)
        step = len(requests)
        if step == 1:
            assert "read_tool_result" not in {item["function"]["name"] for item in body["tools"]}
            return tool("request_capability", {"tool_id": "read_note"}, step)
        if step == 2:
            return tool("read_note", {"note_id": "large-existing-note"}, step)
        message = next(item for item in reversed(body["messages"]) if item["role"] == "tool")
        result = json.loads(message["content"])
        assert "read_tool_result" in {item["function"]["name"] for item in body["tools"]}
        if step == 3:
            assert message["name"] == "read_note"
            assert result["schema"] == "ora.saved_tool_result_window.v1"
            continuation = result["next_request"]
            return tool("read_tool_result", {key: value for key, value in continuation.items()
                                             if key != "tool"}, step)
        assert step == 4 and message["name"] == "read_tool_result"
        assert result["offset"] > 0
        return answer("The saved synthetic note was recoverable after restart.", step)

    host = service(tmp_path, monkeypatch, transport)
    run = host.start(LiveStartRequest(request_id="legacy-paging-exposure", question="Read synthetic notes",
                                     agent_ids=["agent-synthesis"]))
    control = host._control(run.run_id, "agent-synthesis")
    host.notes.write("large-note", mission_id=control["body"]["mission_id"],
                     actor_id=control["body"]["actor_id"], task_ref=control["body"]["proposal_ref"],
                     note_id="large-existing-note", category="note", text="Synthetic note details. " * 400)
    for _ in range(300):
        host._drive_run(host._run(run.run_id))
        control = host._control(run.run_id, "agent-synthesis")
        if (control["body"]["phase"] == "ready_model"
                and len(control["body"]["tool_turn_refs"]) == 2):
            break
        time.sleep(0.01)
    else:
        raise AssertionError("Oversized saved result did not reach next-call state")
    assert len(requests) == 2
    legacy_body = {key: value for key, value in control["body"].items()
                   if key != "pageable_tool_results_available"}
    host._save("mission_task_control", control["id"], legacy_body,
               mission_id=legacy_body["mission_id"], operation_id="legacy-paging-control")
    host.close()
    resumed = service(tmp_path, monkeypatch, transport)
    try:
        drain(resumed, run.run_id)
        assert len(requests) == 4
        assert resumed.state(run.run_id).snapshot.mission.status == "completed"
        assert resumed._control(run.run_id, "agent-synthesis")["body"]["pageable_tool_results_available"]
    finally:
        resumed.close()
