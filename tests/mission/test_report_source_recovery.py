"""A revoked source-linked report is a local pre-dispatch repair, not an unknown call."""

from __future__ import annotations

import asyncio
import json

import pytest

from ora.agents import COMMON_TOOL_IDS
from ora.coordination.board import ref
from ora.coordination.executor import WorkSpec
from ora.mission.context import WorkingContext
from ora.mission.service import LEGACY_QUOTA_DOMAIN, MissionService
from ora.models import SessionError
from tests.mission.test_report_dispatch_dependency import _prepared_report
from tests.models.test_session import MODEL, ROOT, URL


def _spec(store, intent_ref: dict, work_id: str) -> WorkSpec:
    body = store.get(**intent_ref)["body"]
    call_ref = body["call_ref"]
    return WorkSpec(
        work_id=work_id, mission_id="mission", run_id="report-child",
        proposal_ref=ref(store.get("board_proposal", body["task_id"])),
        actor_id=body["instance_id"], configuration_ref=body["configuration_ref"],
        input_refs=(), handler_id="mission-model", handler_version="1", kind="model",
        lane=body["instance_id"], quota_scopes=(LEGACY_QUOTA_DOMAIN,),
        estimated_tokens=100, attempt_id=body["attempt_id"],
        call_id=call_ref["id"], call_version=call_ref["version"],
    )


def _prepared_host(tmp_path, monkeypatch, *, source_linked: bool = True):
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    store, bridge, intent_ref, source_ref, captured = _prepared_report(
        tmp_path, source_linked=source_linked,
    )
    host = MissionService(
        ROOT, tmp_path, model_session=bridge, start_supervisor=False,
        execution_mode="offline_test",
    )
    # The minimal source-linked report fixture predates profile selection. Keep
    # this offline executor exercise on its synthetic legacy quota scope.
    monkeypatch.setattr(host, "_selected_credential", lambda _run: {
        "quota_domain": LEGACY_QUOTA_DOMAIN,
    })
    monkeypatch.setattr(bridge.adapter.credential_profiles, "is_enabled", lambda _name: True)

    def task_shape(tx):
        old = tx.get("board_proposal", "report-task")
        return ref(tx.put(old["kind"], old["id"], {
            **old["body"], "next_action": "Review the saved report",
        }, expected_version=old["version"]))

    store.transact("complete-report-task-shape", {}, task_shape, mission_id="mission")
    return host, bridge, intent_ref, source_ref, captured, _spec(
        store, intent_ref, "report-only-model-work",
    )


def _revoke(store) -> None:
    def update(tx):
        old = tx.get("source_use", "report-use")
        return ref(tx.put("source_use", old["id"], {
            **old["body"], "allowed_uses": [], "policy_status": "restricted",
        }, expected_version=old["version"]))

    store.transact("revoke-report-use", {}, update, mission_id="mission")


def _sibling_spec(host: MissionService, bridge) -> WorkSpec:
    store = host.store
    sibling = host.registry.register(
        "report-sibling-actor", mission_id="mission", run_id="report-child",
        role_id="literature_acquisition", display_name="Synthetic sibling",
    )
    report_intent = store.get("model_request_intent", "model-intent:report-context-call:1")
    original = store.get(**report_intent["body"]["configuration_ref"])
    config = host.configurations.save_configuration(
        "report-sibling-config", instance_id=sibling["id"], mission_id="mission",
        task_scope="separate-synthetic-task", provider="groq", endpoint=URL,
        model_id=MODEL, settings=original["body"]["settings"],
        reasoning_settings=original["body"]["reasoning_settings"],
        instructions="Answer the separate synthetic task.", skill_choices={},
        grants={f"common:{name}" for name in COMMON_TOOL_IDS}, input_refs=[],
    )

    def task(tx):
        return ref(tx.put("board_proposal", "report-sibling-task", {
            "mission_id": "mission", "status": "open",
            "objective": "Separate task", "next_action": "Continue independently",
        }))

    store.transact("report-sibling-task", {}, task, mission_id="mission")
    call = host.configurations.start_call(
        "report-sibling-call", call_id="report-sibling-call",
        configuration_id=config["id"], mission_id="mission",
        task_id="report-sibling-task", attempt_id="report-sibling-attempt",
        loaded_skill_ids=[], exposed_tool_ids=["evidence_lookup"],
        require_common_tools=False,
    )
    prepared = bridge.prepare(
        call_id=call["id"], call_version=call["version"], phase="collect",
        messages=[{"role": "user", "content": "Separate synthetic task"}], sources=(),
    )
    return _spec(store, prepared.intent_ref, "report-sibling-model-work")


def test_report_only_revocation_requeues_with_truthful_context_and_sibling_progress(
    tmp_path, monkeypatch,
) -> None:
    host, bridge, intent_ref, source_ref, captured, spec = _prepared_host(tmp_path, monkeypatch)
    try:
        sibling = _sibling_spec(host, bridge)
        _revoke(host.store)
        host.executor.submit(spec)
        host.executor.submit(sibling)
        asyncio.run(host.executor.run_ready("mission"))

        marker_id = f"model-dispatch:{intent_ref['id']}"
        assert host.store.get("model_dispatch_attempt", marker_id) is None
        assert host.store.get("model_response_receipt", f"model-response:{intent_ref['id']}") is None
        blocks = host.store.records("mission_source_block")
        assert len(blocks) == 1
        block = blocks[0]
        assert block["body"]["context_dependency_refs"] == [source_ref]
        assert block["body"]["external_effect"] == "not_dispatched"
        assert host.capacity.get(host.executor._capacity_id(spec))["phase"] == "failed"
        assert host.store.get(
            "executor_work", host.executor._work_record_id(sibling)
        )["body"]["phase"] == "completed"
        assert len(captured) == 1  # Only the independent sibling reached local transport.

        control = host.store.get("mission_task_control", "report-child-control")
        host._update_control(
            control, phase="pending_model", work_id=spec.work_id,
            call_ref=host.store.get(**intent_ref)["body"]["call_ref"],
        )
        assert host._advance(host.store.get("mission_task_control", "report-child-control"))
        current = host.store.get("mission_task_control", "report-child-control")
        assert current["body"]["phase"] == "ready_model"
        assert current["body"]["feedback"]["unavailable_context_source_refs"] == [source_ref]
        assert "Earlier report text is now unavailable" in current["body"]["feedback"][
            "next_action"
        ]

        old_call = host.store.get(**host.store.get(**intent_ref)["body"]["call_ref"])
        next_call = host.configurations.start_call(
            "report-recovered-call", call_id="report-recovered-call",
            configuration_id=old_call["body"]["configuration_id"], mission_id="mission",
            task_id="report-task", attempt_id="report-recovered-attempt",
            loaded_skill_ids=[], exposed_tool_ids=["evidence_lookup"],
            require_common_tools=False,
        )
        pack = WorkingContext(host.store).assemble(
            ref(next_call), ref(host.store.get("board_proposal", "report-task")),
            control_ref=ref(current),
        )
        visible = json.dumps(pack.messages)
        assert "SYNTHETIC_REPORT_TEXT_FOR_FOLLOWUP" not in visible
        assert "Synthetic follow-up" in visible
        assert "data_unavailable_due_source_use" in visible
        assert current["body"]["feedback"] in json.loads(
            host._model_base_history(current, [])[0]["content"]
        ).values()
    finally:
        host.close()


def test_report_revocation_during_session_preflight_is_local_no_effect(
    tmp_path, monkeypatch,
) -> None:
    host, bridge, intent_ref, source_ref, captured, spec = _prepared_host(tmp_path, monkeypatch)
    try:
        original = bridge.invoke_capture

        def revoke_then_invoke(pointer):
            _revoke(host.store)
            return original(pointer)

        monkeypatch.setattr(bridge, "invoke_capture", revoke_then_invoke)
        result = host._model_work(spec)
        assert result.disposition == "terminal"
        assert result.payload["external_action"] == "none"
        assert host.store.records("mission_source_block")[0]["body"][
            "context_dependency_refs"
        ] == [source_ref]
        assert host.store.records("model_dispatch_attempt") == []
        assert captured == []
    finally:
        host.close()


@pytest.mark.parametrize("source_linked", [True, False])
def test_allowed_and_nondependent_report_requests_still_dispatch(
    tmp_path, monkeypatch, source_linked: bool,
) -> None:
    host, _bridge, _intent_ref, _source_ref, captured, spec = _prepared_host(
        tmp_path, monkeypatch, source_linked=source_linked,
    )
    try:
        result = host._model_work(spec)
        assert result.disposition in {"continue_task", "completed"}
        assert len(captured) == 1
        assert host.store.records("mission_source_block") == []
        assert len(host.store.records("model_dispatch_attempt")) == 1
    finally:
        host.close()


def test_wrong_parent_lineage_is_not_report_revocation(
    tmp_path, monkeypatch,
) -> None:
    host, bridge, intent_ref, source_ref, captured, spec = _prepared_host(tmp_path, monkeypatch)
    try:
        def change_parent(tx):
            child = tx.get("mission_run", "report-child")
            report = tx.get("report", "report-parent-output")
            wrong = tx.put("research_snapshot", "report-wrong-snapshot", {
                "mission_id": "parent-mission", "inspection": {
                    "acceptedOutputReferences": [ref(report)],
                    "sources": [{"copyReference": {**source_ref, "sha256": "0" * 64}}],
                },
            })
            return ref(tx.put("mission_run", child["id"], {
                **child["body"], "parent_snapshot_id": wrong["id"],
            }, expected_version=child["version"]))

        host.store.transact("wrong-parent-lineage", {}, change_parent, mission_id="mission")
        _revoke(host.store)
        with pytest.raises(SessionError, match="unavailable for model input"):
            host._model_work(spec)
        assert host.store.records("mission_source_block") == []
        assert host.store.records("model_dispatch_attempt") == []
        assert captured == []
    finally:
        host.close()


def test_dispatch_marker_does_not_become_no_effect_recovery(
    tmp_path, monkeypatch,
) -> None:
    host, bridge, intent_ref, _source_ref, captured, spec = _prepared_host(
        tmp_path, monkeypatch,
    )
    try:
        def marker_then_fail(_pointer):
            def save(tx):
                return ref(tx.put("model_dispatch_attempt", f"model-dispatch:{intent_ref['id']}", {
                    "mission_id": "mission", "intent_ref": intent_ref,
                    "status": "attempted_external_effect",
                }))

            host.store.transact("synthetic-dispatch-marker", {}, save, mission_id="mission")
            _revoke(host.store)
            raise SessionError("Synthetic late failure after dispatch marker")

        monkeypatch.setattr(bridge, "invoke_capture", marker_then_fail)
        with pytest.raises(SessionError, match="after dispatch marker"):
            host._model_work(spec)
        assert host.store.records("mission_source_block") == []
        assert captured == []
    finally:
        host.close()
