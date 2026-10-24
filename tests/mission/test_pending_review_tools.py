"""A currently readable own peer answer exposes its existing read/review tools."""

from __future__ import annotations

import json

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.mission.tool_catalog import DEFERABLE_CORE_TOOLS
from ora.models.token_estimate import estimate_request_tokens
from tests.mission.test_service import answer, service


def _names(packet: dict) -> set[str]:
    return {item["function"]["name"] for item in packet["tools"]
            if item.get("type") == "function"}


def _setup(tmp_path, monkeypatch, *, report_body="A source-free synthetic assessment."):
    host = service(tmp_path, monkeypatch, lambda _request, *, timeout: answer("unused", 1))
    run = host.start(LiveStartRequest(
        request_id="pending-review-tools-synthetic", question="Review a synthetic peer report.",
        agent_ids=["agent-synthesis", "agent-critique"],
        required_agent_ids=["agent-synthesis", "agent-critique"],
    ))
    requester = host._control(run.run_id, "agent-synthesis")
    responder = host._control(run.run_id, "agent-critique")
    mission_id = requester["body"]["mission_id"]
    peer_task = host.store.get("board_proposal", responder["body"]["task_id"])

    def save(tx):
        report = tx.put("report", "synthetic-peer-report", {
            "mission_id": mission_id, "title": "Synthetic peer report",
            "body": report_body, "citations": [],
            "limitations": ["Fixture only"],
        })
        outcome = tx.put("board_outcome", "synthetic-peer-outcome", {
            "mission_id": mission_id, "proposal_ref": ref(peer_task),
            "result_refs": [ref(report)],
        })
        tx.put("board_proposal", peer_task["id"], {
            **peer_task["body"], "status": "completed", "outcome_ref": ref(outcome),
        }, expected_version=peer_task["version"])
        tx.put("mission_task_control", responder["id"], {
            **responder["body"], "phase": "done", "report_ref": ref(report),
        }, expected_version=responder["version"])
        return ref(report)

    report_ref = host.store.transact("synthetic-peer-report-save", {}, save,
                                     mission_id=mission_id)
    request_id = "synthetic-own-request"
    sent = host.mailbox.send_request(
        "synthetic-peer-request", mission_id=mission_id, request_id=request_id,
        message_id="synthetic-peer-message",
        requester_id=requester["body"]["actor_id"],
        recipient_id=responder["body"]["actor_id"],
        question="Does the synthetic assessment answer this question?",
        expected_artifact="A short report", cause_ref=requester["body"]["proposal_ref"],
        payload={"question": "Does the synthetic assessment answer this question?"},
        task_ref=requester["body"]["proposal_ref"],
    )
    assert sent["status"] == "accepted"
    host.mailbox.mark_delivered("synthetic-peer-delivery", mission_id=mission_id,
                                message_id="synthetic-peer-message")
    saved = host.mailbox.record_answer(
        "synthetic-peer-answer", mission_id=mission_id, request_id=request_id,
        answer_id="synthetic-answer", responder_id=responder["body"]["actor_id"],
        artifact_ref=report_ref,
    )
    assert saved["status"] == "accepted"
    requester = host._control(run.run_id, "agent-synthesis")
    config = host._configuration(requester)
    return host, requester, config, saved


def _pressure_names(host, control, config):
    names, unavailable = host._exposed_tools(control, config, defer_core_tools=True)
    assert unavailable == []
    return set(names)


def test_pending_answer_attaches_only_existing_relevant_schemas_in_serialized_request(
    tmp_path, monkeypatch,
):
    host, control, config, saved = _setup(tmp_path, monkeypatch)
    try:
        baseline = _pressure_names(host, control, config)
        assert {"read_context_record", "review_peer_answer"} <= baseline
        assert (DEFERABLE_CORE_TOOLS - {"read_context_record", "review_peer_answer"}).isdisjoint(
            baseline
        )
        assert host._prepare_model(control, defer_core_tools=True)
        current = host._control(control["body"]["run_id"], "agent-synthesis")
        call = host.store.get(**current["body"]["call_ref"])
        intent = host.store.get(
            "model_request_intent", f"model-intent:{call['id']}:{call['version']}",
        )
        packet = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        names = _names(packet)
        assert {"read_context_record", "review_peer_answer"} <= names
        assert (DEFERABLE_CORE_TOOLS - {"read_context_record", "review_peer_answer"}).isdisjoint(
            names
        )
        assert names == set(call["body"]["exposed_tools"])
        assert saved["request_ref"]["id"] in json.dumps(packet["messages"])
    finally:
        host.close()


@pytest.mark.parametrize("change", ["closed", "wrong_owner", "wrong_task", "stale_answer",
                                    "unreadable", "invitation", "no_grant", "native_browser"])
def test_pending_auto_attachment_requires_current_valid_scope(tmp_path, monkeypatch, change):
    host, control, config, saved = _setup(tmp_path, monkeypatch)
    try:
        request = host.store.get("mail_request", saved["request_ref"]["id"])
        if change in {"closed", "wrong_owner", "wrong_task", "stale_answer"}:
            updates = {}
            if change == "closed":
                updates["status"] = "answered"
            elif change == "wrong_owner":
                updates["requester_id"] = "another-peer"
            elif change == "wrong_task":
                updates["cause_ref"] = {"kind": "board_proposal", "id": "other-task",
                                        "version": 1}
            else:
                updates["answer_refs"] = [{"kind": "mail_answer", "id": "missing",
                                           "version": 1}]
            host.store.transact(f"synthetic-{change}", {}, lambda tx: tx.put(
                "mail_request", request["id"], {**request["body"], **updates},
                expected_version=request["version"],
            ), mission_id=control["body"]["mission_id"])
        elif change == "unreadable":
            report = host.store.get("report", "synthetic-peer-report")
            host.store.transact("synthetic-unreadable", {}, lambda tx: tx.put(
                "report", report["id"], {**report["body"], "body": "changed-current-report"},
                expected_version=report["version"],
            ), mission_id=control["body"]["mission_id"])
            # The old exact report still exists; revoke access through its
            # origin control instead of assuming a new head invalidates it.
            responder = host._control(control["body"]["run_id"], "agent-critique")
            host._update_control(responder, report_ref=None)
        elif change == "invitation":
            control = {**control, "body": {**control["body"],
                                           "control_type": "invitation_review"}}
        elif change == "no_grant":
            config = {**config, "body": {**config["body"], "permitted_tools": {
                key: value for key, value in config["body"]["permitted_tools"].items()
                if key not in {"read_context_record", "review_peer_answer"}
            }}}
        else:
            control = {**control, "body": {**control["body"],
                "requested_capabilities": ["browser_search"]}}
        names = _pressure_names(host, control, config)
        assert {"read_context_record", "review_peer_answer"}.isdisjoint(names)
    finally:
        host.close()


def test_explicit_release_stays_released_while_answer_pending(tmp_path, monkeypatch):
    host, control, config, _saved = _setup(tmp_path, monkeypatch)
    try:
        assert "review_peer_answer" in _pressure_names(host, control, config)
        receipt = host.store.transact("synthetic-review-release", {}, lambda tx: tx.put(
            "mission_capability_selection", "synthetic-review-release", {
                "mission_id": control["body"]["mission_id"],
                "run_id": control["body"]["run_id"],
                "task_id": control["body"]["task_id"],
                "actor_id": control["body"]["actor_id"],
                "action": "release_capability", "tool_id": "review_peer_answer",
            },
        ), mission_id=control["body"]["mission_id"])
        control = {**control, "body": {**control["body"],
            "capability_selection_refs": [ref(receipt)]}}
        names = _pressure_names(host, control, config)
        assert "read_context_record" in names
        assert "review_peer_answer" not in names
        assert "review_peer_answer" not in host._exposed_tools(
            control, config, defer_core_tools=True,
            released_for_next_turn="review_peer_answer",
        )[0]
        selected = {**control, "body": {**control["body"],
            "requested_capabilities": ["review_peer_answer"]}}
        assert "review_peer_answer" in _pressure_names(host, selected, config)
    finally:
        host.close()


def _prepared_packet(host, control):
    assert host._prepare_model(control, defer_core_tools=True)
    current = host._control(control["body"]["run_id"], "agent-synthesis")
    call = host.store.get(**current["body"]["call_ref"])
    intent = host.store.get(
        "model_request_intent", f"model-intent:{call['id']}:{call['version']}",
    )
    raw = host.store.read_blob(intent["body"]["blob"]["sha256"])
    packet = json.loads(raw)
    index = json.loads(packet["messages"][-1]["content"])
    budget = host.store.get("mission_call_budget", call["id"])["body"]
    return packet, index, estimate_request_tokens(raw).tokens, budget


def test_readable_pending_review_uses_saved_rate_window_for_complete_report(
    tmp_path, monkeypatch,
):
    report_body = (
        "Synthetic pilot A 4.1 C, pilot B 2.3 C; nonrandom placement limits causality. "
        * 18
    )
    baseline, control, _, saved = _setup(
        tmp_path / "baseline", monkeypatch, report_body=report_body,
    )
    try:
        _, index, full_input, _ = _prepared_packet(baseline, control)
        assert index["peer_answer_previews"][0]["report"]["body"] == report_body
    finally:
        baseline.close()

    host, control, _, saved = _setup(
        tmp_path / "rate-window", monkeypatch, report_body=report_body,
    )
    try:
        prior_completion = 379
        observed_limit = full_input + prior_completion + 80
        receipt = host.store.transact(
            "synthetic-prior-completion", {},
            lambda tx: tx.put("model_response_receipt", "synthetic-prior-completion", {
                "mission_id": control["body"]["mission_id"],
                "task_id": control["body"]["task_id"],
                "status": "tool_requested",
                "usage": {"completion_tokens": prior_completion},
            }), mission_id=control["body"]["mission_id"],
        )
        host._update_control(
            control, response_ref=ref(receipt),
            input_budget={"observed_limit_tokens": observed_limit,
                          "target_fraction": 0.70},
        )
        packet, index, actual_input, budget = _prepared_packet(
            host, host._control(control["body"]["run_id"], "agent-synthesis"),
        )
        card = next(item for item in index.get("peer_answer_previews", [])
                    if item["answer_ref"] == saved["answer_ref"])
        assert card["report"]["body"] == report_body
        assert card["report_delivery"] == "complete_in_input"
        assert {"read_context_record", "review_peer_answer"} <= _names(packet)
        assert actual_input + prior_completion <= observed_limit
        stage = budget["request_stage_token_estimate"]
        assert stage["rate_input_headroom_tokens"] == 0
        assert stage["admission_target_tokens"] == observed_limit - prior_completion
        assert stage["estimated_demand_tokens"] == actual_input + prior_completion
    finally:
        host.close()


def test_oversized_peer_report_keeps_reader_cue_and_ordinary_headroom(
    tmp_path, monkeypatch,
):
    host, control, _, saved = _setup(
        tmp_path, monkeypatch, report_body="Synthetic report. " * 180,
    )
    try:
        host._update_control(
            control,
            input_budget={"observed_limit_tokens": 8000,
                          "target_fraction": 0.70},
        )
        packet, index, actual_input, budget = _prepared_packet(
            host, host._control(control["body"]["run_id"], "agent-synthesis"),
        )
        assert not index.get("peer_answer_previews")
        cue = next(item for item in index["peer_answer_review_cues"]
                   if item["answer_ref"] == saved["answer_ref"])
        assert cue["read_request"]["tool"] == "read_context_record"
        assert "read_context_record" in _names(packet)
        stage = budget["request_stage_token_estimate"]
        assert stage["rate_input_headroom_tokens"] == 800
        assert actual_input + 800 <= 8000
    finally:
        host.close()


def test_unreadable_peer_report_cannot_use_review_rate_exception(
    tmp_path, monkeypatch,
):
    host, control, _, _ = _setup(tmp_path, monkeypatch)
    try:
        responder = host._control(control["body"]["run_id"], "agent-critique")
        host._update_control(responder, report_ref=None)
        host._update_control(
            host._control(control["body"]["run_id"], "agent-synthesis"),
            input_budget={"observed_limit_tokens": 8000,
                          "target_fraction": 0.70},
        )
        _, index, _, budget = _prepared_packet(
            host, host._control(control["body"]["run_id"], "agent-synthesis"),
        )
        assert not index.get("peer_answer_previews")
        assert budget["request_stage_token_estimate"]["rate_input_headroom_tokens"] == 800
    finally:
        host.close()
