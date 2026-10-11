"""A parent-report read remains a child-local tool result, including saved legacy repair."""

from __future__ import annotations

import json
import time
from dataclasses import replace

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.mission.context import ContextError
from ora.mission.projection import build_snapshot
from ora.state import StateConflict
from tests.mission.test_followup_preparation_recovery import _parent_and_child
from tests.mission.test_service import answer, service, tool


@pytest.mark.parametrize(
    "saved_legacy_result,interrupt_after_accept,interleave_control,tamper",
    [(False, False, False, None), (True, False, False, None),
     (True, True, False, None), (True, False, True, None),
     (True, False, False, "hash"), (True, False, False, "response")],
)
def test_followup_parent_report_read_accepts_local_result_without_repeating_model(
    tmp_path, monkeypatch, saved_legacy_result, interrupt_after_accept,
    interleave_control, tamper,
):
    requests = []

    def transport(request, *, timeout):
        body = json.loads(request.data)
        requests.append(body)
        if len(requests) == 1:
            return answer("A bounded synthetic parent finding with an explicit limitation.", 1)
        if len(requests) == 2:
            index = json.loads(body["messages"][-1]["content"])
            target = index["followup_target"]["target_ref"]
            return tool("read_context_record", {
                "inclusion_id": index["older_records"]["inclusion_id"],
                "kind": target["kind"], "id": target["id"],
                "version": target["version"], "offset": 0, "limit": 900,
            }, 2)
        return answer("The saved parent finding needs qualification.", 3)

    host = service(tmp_path, monkeypatch, transport)
    try:
        followup = _parent_and_child(host)
        child = host.followup(followup)
        original_handler = host.executor.handlers[("mission-tool", "1")]

        if saved_legacy_result:
            def old_read_handler(spec):
                observed = original_handler(spec)
                if (observed.payload or {}).get("name") != "read_context_record":
                    return observed
                current = host._control(child.run_id, "agent-critique")
                args = current["body"]["tool_call"]["arguments"]
                inclusion = {"kind": "context_inclusion", "id": args["inclusion_id"],
                             "version": 1}
                parent = {key: args[key] for key in ("kind", "id", "version")}
                return replace(observed, output_refs=(inclusion, parent))

            host.executor.handlers[("mission-tool", "1")] = old_read_handler

        for _ in range(400):
            host._drive_run(host._run(child.run_id))
            control = host._control(child.run_id, "agent-critique")
            ready = control["body"]["phase"] == "ready_model" and control["body"][
                "tool_turn_refs"
            ]
            unaccepted = control["body"]["phase"] == "waiting" and control["body"].get(
                "waiting_reason"
            ) == "External result was recorded but task acceptance failed"
            if ready or unaccepted:
                break
            time.sleep(.005)
        else:
            pytest.fail("The exact parent-report read did not reach an accepted or saved boundary")
        assert len(requests) == 2
        if saved_legacy_result:
            assert control["body"]["tool_call"]["name"] == "read_context_record"
            assert unaccepted
            work_id = host._tool_id(control, control["body"]["tool_call"]["id"])
            work = next(item for item in host.store.records("executor_work")
                        if item["body"]["spec"]["work_id"] == work_id)
            observation = host.store.get(**work["body"]["result_ref"])
            assert observation["body"]["reason_code"] == "board_rejected"
            assert observation["body"]["capacity_reconciled"] is True
            projected, _, _, _ = build_snapshot(
                host.store, host._run(child.run_id), host._controls(child.run_id),
                snapshot_id="synthetic-unaccepted-result-view",
            )
            tool_attempt = next(item for item in projected["attempts"]
                                if item["id"] == work["body"]["spec"]["attempt_id"])
            assert tool_attempt["toolCalls"] == [
                {"name": "read_context_record", "provider": None,
                 "status": "result_unaccepted"}
            ]
            if tamper == "hash":
                original_get = host.store.get

                def bad_hash(kind, id, version=None):
                    saved = original_get(kind, id, version)
                    if kind == "executor_unaccepted_result" and id == observation["id"]:
                        return {**saved, "body": {**saved["body"],
                                "result_sha256": "0" * 64}}
                    return saved

                monkeypatch.setattr(host.store, "get", bad_hash)
                untrusted, _, _, _ = build_snapshot(
                    host.store, host._run(child.run_id), host._controls(child.run_id),
                    snapshot_id="synthetic-tampered-result-view",
                )
                untrusted_attempt = next(item for item in untrusted["attempts"]
                                         if item["id"] == tool_attempt["id"])
                assert untrusted_attempt["toolCalls"][0]["status"] == "waiting"
                assert host._recover_unaccepted_context_read(control) is False
                monkeypatch.setattr(host.store, "get", original_get)
            if tamper == "response":
                response = host.store.get(**control["body"]["response_ref"])
                digest = response["body"]["blob"]["sha256"]
                original_read_blob = host.store.read_blob

                def changed_tool_call(sha256):
                    raw = original_read_blob(sha256)
                    if sha256 != digest:
                        return raw
                    value = json.loads(raw)
                    value["tool_calls"][0]["id"] = "other-tool-call"
                    return json.dumps(value).encode()

                monkeypatch.setattr(host.store, "read_blob", changed_tool_call)
                assert host._recover_unaccepted_context_read(control) is False
                monkeypatch.setattr(host.store, "read_blob", original_read_blob)
            if tamper:
                assert host.store.records("mission_context_read_recovery") == []
                assert len(requests) == 2
            if interrupt_after_accept:
                original_save = host.context.save_tool_result

                def interrupted_save(*args, **kwargs):
                    raise RuntimeError("Synthetic interruption after local board acceptance")

                monkeypatch.setattr(host.context, "save_tool_result", interrupted_save)
                with pytest.raises(RuntimeError, match="Synthetic interruption"):
                    host._recover_unaccepted_context_read(control)
                monkeypatch.setattr(host.context, "save_tool_result", original_save)
                assert len([item for item in host.store.records("board_outcome")
                            if item["id"].startswith("context-read-recovery-outcome-")]) == 1
                assert len(requests) == 2
                accepted_view, _, _, _ = build_snapshot(
                    host.store, host._run(child.run_id), host._controls(child.run_id),
                    snapshot_id="synthetic-accepted-before-context-result-view",
                )
                accepted_attempt = next(item for item in accepted_view["attempts"]
                                        if item["id"] == tool_attempt["id"])
                assert accepted_attempt["toolCalls"][0]["status"] == "acceptance_recorded"
            if interleave_control:
                original_save = host.context.save_tool_result

                def concurrent_control_change(*args, **kwargs):
                    saved = original_save(*args, **kwargs)
                    current = host._control(child.run_id, "agent-critique")
                    host._update_control(
                        current, feedback={"synthetic_concurrent_change": True}
                    )
                    return saved

                monkeypatch.setattr(host.context, "save_tool_result", concurrent_control_change)
                with pytest.raises(StateConflict, match="changed before local continuation"):
                    host._recover_unaccepted_context_read(control)
                monkeypatch.setattr(host.context, "save_tool_result", original_save)
                control = host._control(child.run_id, "agent-critique")
                assert control["body"]["feedback"] == {
                    "synthetic_concurrent_change": True
                }
                assert control["body"]["phase"] == "waiting"
            assert host._recover_unaccepted_context_read(control)
            control = host._control(child.run_id, "agent-critique")
            recovered_view, _, _, _ = build_snapshot(
                host.store, host._run(child.run_id), host._controls(child.run_id),
                snapshot_id="synthetic-recovered-result-view",
            )
            recovered_attempt = next(item for item in recovered_view["attempts"]
                                     if item["id"] == tool_attempt["id"])
            assert recovered_attempt["toolCalls"][0]["status"] not in {
                "waiting", "result_unaccepted"
            }
            recovery = host.store.records("mission_context_read_recovery")
            assert len(recovery) == 1
            assert recovery[0]["body"]["observation_ref"] == ref(observation)
            outcome = next(item for item in host.store.records("board_outcome")
                           if item["id"].startswith("context-read-recovery-outcome-"))
            assert outcome["body"]["result_refs"] == [ref(recovery[0])]
            assert outcome["body"]["completes_task"] is False
            assert host._recover_unaccepted_context_read(control) is False
            assert host.store.get(**ref(observation)) == observation
        else:
            assert ready
            work = next(item for item in host.store.records("executor_work")
                        if item["body"]["spec"]["work_id"]
                        == host._tool_id(control, "tool-2"))
            artifact = host.store.get(**work["body"]["result_ref"])
            assert artifact["body"]["output_refs"] == []
        assert control["body"]["phase"] == "ready_model"
        assert len(control["body"]["tool_turn_refs"]) == 1
        result = host.store.get(
            "context_tool_result", f"tool-result:{control['body']['response_ref']['id']}"
        )
        assert result is not None
        saved_message = json.loads(host.store.read_blob(result["body"]["blob"]["sha256"]))
        saved_window = json.loads(saved_message["content"])
        assert saved_window["ref"]["kind"] == "report"
        assert saved_window["returned_chars"] > 0

        # A further model input receives the saved read, with no model replay
        # during local board reconciliation.
        for _ in range(300):
            host._drive_run(host._run(child.run_id))
            if len(requests) >= 3:
                break
            time.sleep(.005)
        else:
            pytest.fail("The next child model call did not receive the saved read")
        tool_results = [item for item in requests[2]["messages"] if item["role"] == "tool"]
        assert any(json.loads(item["content"])["ref"] == saved_window["ref"]
                   for item in tool_results)
    finally:
        host.close()


@pytest.mark.parametrize("observed_limit", [8000, 10000])
def test_source_free_followup_keeps_latest_bounded_report_read_after_prior_turns(
    tmp_path, monkeypatch, observed_limit
):
    requests = []

    def transport(request, *, timeout):
        body = json.loads(request.data)
        requests.append(body)
        if len(requests) == 1:
            return answer("Synthetic parent finding. " + "Detail. " * 320, 1)
        index = json.loads(body["messages"][-1]["content"])
        if len(requests) in (2, 3):
            return tool("read_context_page", {
                "inclusion_id": index["older_records"]["inclusion_id"],
                "cursor": 0, "limit": 1,
            }, len(requests))
        if len(requests) == 4:
            target = index["followup_target"]["target_ref"]
            return tool("read_context_record", {
                "inclusion_id": index["older_records"]["inclusion_id"],
                **target, "offset": 0, "limit": 2000,
            }, 4)
        return answer("No provider call is needed for this preparation test.", 5)

    host = service(tmp_path, monkeypatch, transport)
    try:
        child = host.followup(_parent_and_child(host))
        for _ in range(500):
            host._drive_run(host._run(child.run_id))
            control = host._control(child.run_id, "agent-critique")
            if (
                len(requests) == 4
                and control["body"]["phase"] == "ready_model"
                and len(control["body"]["tool_turn_refs"]) == 3
            ):
                break
            time.sleep(.005)
        else:
            pytest.fail("The latest bounded report read was not saved")
        latest = control["body"]["tool_turn_refs"][-1]
        result = host.store.get("context_tool_result", f"tool-result:{latest['id']}")
        saved = json.loads(host.store.read_blob(result["body"]["blob"]["sha256"]))
        assert json.loads(saved["content"])["returned_chars"] == 2000
        assert host._current_context_read_turn(latest, control)

        def revoked(_source_refs):
            raise ContextError("Synthetic current source-use revocation")

        with monkeypatch.context() as patch:
            patch.setattr(host.context, "require_current_source_use", revoked)
            assert host._current_context_read_turn(latest, control) is False

        host._update_control(
            control, input_budget={"observed_limit_tokens": observed_limit,
                                   "target_fraction": .70}
        )
        control = host._control(child.run_id, "agent-critique")
        assert host._schedule_model(control, force_soft_recovery=True)
        prepared_control = host._control(child.run_id, "agent-critique")
        call_id = prepared_control["body"]["call_ref"]["id"]
        intent = host.store.get("model_request_intent", f"model-intent:{call_id}:1")
        prepared = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        assert len(requests) == 4  # Preparation made no provider call.
        assert intent["body"]["source_refs"] == []
        assert prepared["messages"][-2]["role"] == "tool"
        assert prepared["messages"][-2]["content"] == saved["content"]
        assert prepared["messages"][-3]["tool_calls"][0]["id"] == saved["tool_call_id"]
        assert all(item["role"] != "tool" for item in prepared["messages"][:-2])
        budget = host.store.get("mission_call_budget", call_id)["body"]
        assert budget["source_turn_mode"] == "source_free_recovery"
        assert budget["exact_complete_request_estimate_tokens"] <= (
            observed_limit - 6
        )
        if observed_limit == 8000:
            assert budget["preview_admission_target_tokens"] > 5600
        assert budget["current_tool_turn_ref"] == latest
    finally:
        host.close()


def test_reading_existing_same_mission_report_does_not_complete_task(
    tmp_path, monkeypatch
):
    """The executor treats a read projection as data, not a newly published report."""
    requests = []
    saved_report = None

    def transport(request, *, timeout):
        body = json.loads(request.data)
        requests.append(body)
        if len(requests) == 1:
            index = json.loads(body["messages"][-1]["content"])
            return tool("read_context_record", {
                "inclusion_id": index["older_records"]["inclusion_id"],
                "kind": "report", "id": saved_report["id"],
                "version": saved_report["version"], "offset": 0, "limit": 600,
            }, 1)
        return answer("The existing report remains an input to this task.", 2)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="read-existing-local-report", question="Review an existing result",
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        saved_report = host._save(
            "report", "existing-local-report",
            {"mission_id": control["body"]["mission_id"], "run_id": run.run_id,
             "title": "Existing synthetic result", "body": "Previously saved finding.",
             "citations": [], "limitations": ["Synthetic test result"]},
            mission_id=control["body"]["mission_id"],
        )
        def checked_local_read(**kwargs):
            assert kwargs["pointer"] == saved_report
            return {"schema": "ora.context_record_window.v1", "ref": saved_report,
                    "offset": kwargs["offset"], "requested_limit": kwargs["limit"],
                    "effective_limit": kwargs["limit"], "returned_chars": 22,
                    "next_offset": None, "total_chars": 22,
                    "projection_sha256": "synthetic-read-projection",
                    "text": "Previously saved finding"}

        # Authorization for actual omitted records is covered separately in
        # test_context; this fixture isolates executor output semantics.
        monkeypatch.setattr(host.context, "read_omitted_record", checked_local_read)
        for _ in range(300):
            host._drive_run(host._run(run.run_id))
            control = host._control(run.run_id, "agent-synthesis")
            if control["body"]["phase"] == "ready_model" and control["body"]["tool_turn_refs"]:
                break
            time.sleep(.005)
        else:
            pytest.fail("Existing-report read did not complete its local tool turn")
        assert len(requests) == 1
        assert control["body"].get("report_ref") is None
        assert host.store.get("board_proposal", control["body"]["task_id"])["body"][
            "status"
        ] == "open"
        assert host._current_context_read_turn(control["body"]["tool_turn_refs"][-1], control)
        work_id = host._tool_id(control, "tool-1")
        work = next(item for item in host.store.records("executor_work")
                    if item["body"]["spec"]["work_id"] == work_id)
        assert host.store.get(**work["body"]["result_ref"])["body"]["output_refs"] == []
        for _ in range(300):
            host._drive_run(host._run(run.run_id))
            if len(requests) >= 2:
                break
            time.sleep(.005)
        else:
            pytest.fail("Saved local read did not reach a subsequent model input")
        assert any(
            json.loads(message["content"])["ref"] == saved_report
            for message in requests[1]["messages"] if message["role"] == "tool"
        )
    finally:
        host.close()
