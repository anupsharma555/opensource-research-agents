"""Regressions where host mechanics must not halt independent peer work."""

from __future__ import annotations

import time

from ora.contracts.models import LiveStartRequest
from tests.mission.test_service import (
    answer,
    revoke_source,
    service,
    tool,
    two_supplied_sources,
)
from tests.mission.test_work import propose, setup


def test_continue_scope_decision_survives_optional_segment_progress(tmp_path):
    store, _board, _registry, work, run_ref, actor, control_ref, root_ref = setup(tmp_path)
    optional = propose(work, run_ref, actor, control_ref, root_ref)
    decision = work.record_scope_disposition(
        "scope-with-running-optional",
        run_ref=run_ref,
        actor_id=actor["id"],
        source_control_ref=control_ref,
        decisions=[{"control_ref": optional["control_ref"], "disposition": "continue"}],
        reason="The requested answer can close while this independent branch continues",
    )
    assert work.disposition_is_current(decision["disposition_ref"])

    control = store.get(**optional["control_ref"])
    store.transact(
        "optional-segment-started",
        {"control_ref": optional["control_ref"]},
        lambda tx: tx.put(
            "mission_task_control",
            control["id"],
            {**control["body"], "phase": "pending_model"},
            expected_version=control["version"],
        ),
        mission_id="synthetic-mission",
    )
    assert [item["control_ref"]["id"] for item in work.open_optional_work(run_ref["id"])] == [
        optional["control_ref"]["id"]
    ]
    assert work.disposition_is_current(decision["disposition_ref"])


def test_restart_with_removed_requested_tool_keeps_core_peer_turn(tmp_path, monkeypatch):
    host = service(
        tmp_path, monkeypatch,
        lambda request, *, timeout: answer("The peer can choose another method.", 1),
    )
    receipt = host.start(
        LiveStartRequest(
            request_id="removed-requested-tool",
            question="Assess the evidence independently",
            agent_ids=["agent-synthesis"],
        )
    )
    control = host._control(receipt.run_id, "agent-synthesis")
    host._update_control(control, requested_capabilities=["write_note"])
    host.close()

    restarted = service(
        tmp_path, monkeypatch,
        lambda request, *, timeout: answer("The peer can choose another method.", 2),
    )
    try:
        # Simulate a compatible deployment in which this optional tool was removed.
        restarted.catalog.tools.pop("write_note")
        control = restarted._control(receipt.run_id, "agent-synthesis")
        assert restarted._schedule_model(control)
        intent = restarted.store.records("model_request_intent")[0]
        call = restarted.store.get(**intent["body"]["call_ref"])
        assert "write_note" not in call["body"]["exposed_tools"]
        assert "request_capability" in call["body"]["exposed_tools"]
    finally:
        restarted.close()


def test_excluded_copy_cannot_be_cited_from_allowed_model_turn(tmp_path, monkeypatch):
    selected = {}

    def transport(request, *, timeout):
        return tool(
            "publish_result",
            {
                "title": "Synthetic result",
                "text": "A claim attributed to an excluded source",
                "citations": [
                    {"source_id": selected["revoked"]["id"], "span_id": selected["span_id"]}
                ],
                "limitations": ["Synthetic fixture"],
            },
            1,
        )

    host = service(tmp_path, monkeypatch, transport)
    try:
        receipt = host.start(
            LiveStartRequest(
                request_id="excluded-citation",
                question="Assess the evidence",
                agent_ids=["agent-synthesis"],
                evidence_text="Revoked synthetic passage.",
            )
        )
        revoked, allowed = two_supplied_sources(host, receipt.run_id)
        selected["revoked"] = revoked
        selected["span_id"] = host.store.get(
            "source_passage", host.store.get(**revoked)["body"]["passage_ids"][0]
        )["body"]["span_id"]
        revoke_source(host, revoked, host._run(receipt.run_id)["body"]["mission_id"])

        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and not (
            host.store.records("context_tool_result") or host.store.records("report")
        ):
            host._drive_run(host._run(receipt.run_id))
            time.sleep(0.01)
        assert (
            host.store.records("context_tool_result") or host.store.records("report")
        ), "Publication tool was not processed"
        intent = host.store.records("model_request_intent")[0]
        assert [item["copy_ref"]["id"] for item in intent["body"]["source_decisions"]] == [
            allowed["id"]
        ]
        assert not host.store.records("report")
    finally:
        host.close()
