"""A stale queued model request can be replanned before any external dispatch."""

from __future__ import annotations

import json
import time
from dataclasses import asdict

import pytest

from ora.contracts.models import LiveStartRequest
from ora.mission import service as service_module
from ora.models.token_estimate import TOKENIZER_BASIS, estimate_request_tokens
from ora.state.store import canonical
from tests.mission.test_requested_window_recovery import _two_copy_read
from tests.mission.test_service import service, tool
from tests.mission.test_source_rich_preparation import _three_source_host


def test_stale_whole_supplied_source_free_intent_replans_before_dispatch(
    tmp_path, monkeypatch,
):
    host, run, sources, _texts = _three_source_host(
        tmp_path, monkeypatch, items=20, limit=7600,
    )
    try:
        # This queued source-free intent represents the earlier ID-only
        # catalog. The new full-purpose v2 profile is tested separately; its
        # larger initial packet is not the historical replan boundary.
        saved_run = host._run(run.run_id)
        host.store.transact(
            "synthetic-stale-v1-catalog", {},
            lambda tx: tx.put("mission_run", run.run_id, {
                **saved_run["body"], "tool_catalog_projection_version": 1,
            }, expected_version=saved_run["version"]),
            mission_id=saved_run["body"]["mission_id"],
        )
        control = host._control(run.run_id, "agent-synthesis")
        original_plan = service_module.plan_source_turn

        def old_source_free_plan(store, pointer, **kwargs):
            plan = original_plan(store, pointer, **kwargs)
            return {**plan, "mode": "source_free_recovery", "source_inputs": [],
                    "reason": "synthetic_prior_packer_limit"}

        monkeypatch.setattr(service_module, "plan_source_turn", old_source_free_plan)
        assert host._schedule_model(control)
        pending = host._control(run.run_id, "agent-synthesis")
        assert not any(pending["body"].get("selected_passage_refs", {}).values())
        old_call = host.store.get(**pending["body"]["call_ref"])
        old_intent = host.store.get(
            "model_request_intent", f"model-intent:{old_call['id']}:{old_call['version']}",
        )
        old_work = host._work_for(pending)
        host.executor._prepare(pending["body"]["mission_id"])
        old_budget = host.store.get("mission_call_budget", old_call["id"])
        assert old_budget["body"]["source_turn_mode"] == "source_free_recovery"
        host._save("mission_call_budget", old_call["id"], {
            **old_budget["body"], "estimator_version": "older_packer",
        }, mission_id=pending["body"]["mission_id"])
        monkeypatch.setattr(service_module, "plan_source_turn", original_plan)

        assert host._supersede_stale_source_free_intent(pending)
        revised = host._control(run.run_id, "agent-synthesis")
        assert revised["body"]["phase"] == "ready_model"
        assert host.store.get("executor_work", old_work["id"])["body"]["phase"] == "superseded"
        assert host.store.get("model_request_intent", old_intent["id"]) == old_intent
        assert host.store.get("model_dispatch_attempt",
                              f"model-dispatch:{old_intent['id']}") is None
        assert host._schedule_model(revised)
        successor = host._control(run.run_id, "agent-synthesis")
        call = host.store.get(**successor["body"]["call_ref"])
        intent = host.store.get(
            "model_request_intent", f"model-intent:{call['id']}:{call['version']}",
        )
        assert [item["id"] for item in intent["body"]["source_refs"]] == [
            item["id"] for item in sources
        ]
    finally:
        host.close()


@pytest.mark.parametrize("capacity_acquired", [False, True])
def test_stale_source_free_intent_is_fenced_only_before_dispatch(
    tmp_path, monkeypatch, capacity_acquired
):
    requests = []
    source_ref = None

    def transport(request, *, timeout):
        requests.append(json.loads(request.data))
        return tool("read_source_window", {
            "source_id": source_ref["id"], "source_version": source_ref["version"],
            "block_offset": 0, "char_offset": 0, "char_limit": 800,
            "selection_mode": "replace",
        }, len(requests))

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="queued-prepared-source-free", question="Inspect a synthetic source",
            evidence_text="Synthetic source with a bounded first window. " * 100,
            agent_ids=["agent-synthesis"],
        ))
        source_ref = host._control(run.run_id, "agent-synthesis")["body"]["source_refs"][0]
        for _ in range(250):
            host._drive_run(host._run(run.run_id))
            control = host._control(run.run_id, "agent-synthesis")
            if control["body"]["phase"] == "ready_model" and control["body"]["tool_turn_refs"]:
                break
            time.sleep(.005)
        else:
            pytest.fail("The checked source window was not saved")
        assert len(requests) == 1
        assert control["body"]["selected_passage_refs"][source_ref["id"]]
        last_response = host.store.get(**control["body"]["response_ref"])
        prior_completion = last_response["body"]["usage"]["completion_tokens"]
        assert prior_completion > 0
        host._update_control(
            control, input_budget={"observed_limit_tokens": 12000, "target_fraction": .70}
        )
        control = host._control(run.run_id, "agent-synthesis")
        original_plan = service_module.plan_source_turn
        monkeypatch.setattr(service_module, "plan_source_turn", lambda store, pointer, **_:
                            original_plan(store, pointer, source_input_allowance_tokens=1))
        assert host._schedule_model(control)
        pending = host._control(run.run_id, "agent-synthesis")
        old_ref = pending["body"]["call_ref"]
        old_intent = host.store.get("model_request_intent", f"model-intent:{old_ref['id']}:1")
        old_work = host._work_for(pending)
        spec = host.executor._spec_from(old_work)
        host.executor._prepare(pending["body"]["mission_id"])
        cap_id = host.executor._capacity_id(spec)
        cap = host.store.get("capacity_operation", cap_id)
        assert cap["body"]["phase"] in {"queued", "waiting"}
        old_budget = host.store.get("mission_call_budget", old_ref["id"])
        assert old_budget["body"]["source_turn_mode"] == "source_free_recovery"
        host._save(
            "mission_call_budget", old_ref["id"],
            {**old_budget["body"], "estimator_version": "older_estimator",
             "packer_version": "older_packer"},
            mission_id=pending["body"]["mission_id"],
        )
        if capacity_acquired:
            host._save(
                "capacity_operation", cap_id,
                {**cap["body"], "phase": "admitted", "owner": "synthetic-other-owner",
                 "reservation": True},
                mission_id=pending["body"]["mission_id"],
            )
            assert host._supersede_stale_source_free_intent(pending) is False
            assert host._work_for(pending) == old_work
            assert host.store.get("model_request_intent", old_intent["id"]) == old_intent
            return
        assert host._supersede_stale_source_free_intent(pending) is True
        revised = host._control(run.run_id, "agent-synthesis")
        assert revised["body"]["phase"] == "ready_model"
        assert revised["body"]["work_id"] is None
        assert revised["body"]["sequence"] == pending["body"]["sequence"]
        assert host.store.get("executor_work", old_work["id"])["body"]["phase"] == "superseded"
        assert host.store.get("capacity_operation", cap_id)["body"]["phase"] == "canceled"
        assert host.store.get("agent_attempt", spec.attempt_id)["body"]["status"] == "superseded"
        assert host.store.get("model_request_intent", old_intent["id"]) == old_intent
        assert host.store.get("model_dispatch_attempt", f"model-dispatch:{old_intent['id']}") is None
        assert host.store.get("model_response_receipt", f"model-response:{old_intent['id']}") is None
        assert len(requests) == 1
        assert host._supersede_stale_source_free_intent(revised) is False
        monkeypatch.setattr(service_module, "plan_source_turn", original_plan)
        assert host._schedule_model(revised)
        successor = host._control(run.run_id, "agent-synthesis")["body"]["call_ref"]
        assert successor != old_ref
        next_intent = host.store.get("model_request_intent", f"model-intent:{successor['id']}:1")
        assert next_intent is not None
        assert next_intent["body"]["source_decisions"]
        next_body = json.loads(host.store.read_blob(next_intent["body"]["blob"]["sha256"]))
        assert "source_packets" in json.dumps(next_body["messages"])
        next_budget = host.store.get("mission_call_budget", successor["id"])["body"]
        assert next_budget["prior_completion_reservation"] == prior_completion
        assert next_budget["prior_completion_response_ref"] == control["body"]["response_ref"]
        next_control = host._control(run.run_id, "agent-synthesis")
        next_spec = host.executor._spec_from(host._work_for(next_control))
        estimate = estimate_request_tokens(canonical(next_body).encode())
        assert estimate.basis == TOKENIZER_BASIS
        assert next_intent["body"]["request_token_estimate"] == asdict(estimate)
        assert next_budget["estimated_tokens_with_completion_reservation"] == (
            estimate.tokens + prior_completion
        )
        assert next_spec.estimated_tokens == estimate.tokens + prior_completion
        assert len(requests) == 1  # Neither supersession nor preparation dispatched a model call.
    finally:
        host.close()


@pytest.mark.parametrize("boundary", ["safe", "acquired", "attempted", "revoked"])
def test_stale_checked_window_intent_is_replanned_only_before_dispatch(
    tmp_path, monkeypatch, boundary,
):
    host, run, actor, early, _late, window, _response = _two_copy_read(
        tmp_path, monkeypatch, window_chars=1800, crowded=False,
    )
    try:
        control = host._control(run.run_id, actor)
        host._update_control(control, input_budget={
            "observed_limit_tokens": 6800, "target_fraction": 1.0,
        })
        assert host._schedule_model(host._control(run.run_id, actor))
        pending = host._control(run.run_id, actor)
        old_ref = pending["body"]["call_ref"]
        intent_id = f"model-intent:{old_ref['id']}:{old_ref['version']}"
        old_intent = host.store.get("model_request_intent", intent_id)
        old_work = host._work_for(pending)
        spec = host.executor._spec_from(old_work)
        host.executor._prepare(pending["body"]["mission_id"])
        cap_id = host.executor._capacity_id(spec)
        cap = host.store.get("capacity_operation", cap_id)
        assert cap["body"]["phase"] in {"queued", "waiting"}
        budget = host.store.get("mission_call_budget", old_ref["id"])
        assert budget["body"]["source_turn_mode"] == "requested_window_recovery"
        assert old_intent["body"]["source_refs"] == [early]
        assert old_intent["body"]["source_decisions"][0]["passage_refs"] == window["passage_refs"]
        host._save(
            "mission_call_budget", old_ref["id"],
            {**budget["body"], "estimator_version": "older_estimator"},
            mission_id=pending["body"]["mission_id"],
        )
        if boundary == "acquired":
            host._save("capacity_operation", cap_id, {
                **cap["body"], "phase": "admitted", "owner": "synthetic-other-owner",
                "reservation": True,
            }, mission_id=pending["body"]["mission_id"])
        elif boundary == "attempted":
            host._save("capacity_operation", cap_id, {
                **cap["body"], "attempts": [{"id": "synthetic-claimed-attempt"}],
            }, mission_id=pending["body"]["mission_id"])
        elif boundary == "revoked":
            use_ref = host.context._source_status(host.store.get(**early))[
                "current_source_use_ref"
            ]

            def revoke(tx):
                previous = tx.get(**use_ref)
                updated = tx.put(previous["kind"], previous["id"], {
                    **previous["body"], "allowed_uses": [],
                    "policy_status": "requires_source_policy_resolution",
                }, expected_version=previous["version"])
                return {"use_ref": {key: updated[key] for key in ("kind", "id", "version")}}

            host.store.transact("synthetic-replan-revoke", {}, revoke,
                                mission_id=pending["body"]["mission_id"])
        if boundary != "safe":
            assert host._supersede_stale_source_free_intent(pending) is False
            assert host._work_for(pending) == old_work
        else:
            assert host._supersede_stale_source_free_intent(pending) is True
            revised = host._control(run.run_id, actor)
            assert revised["body"]["phase"] == "ready_model"
            assert revised["body"]["work_id"] is None
            assert host.store.get("executor_work", old_work["id"])["body"]["phase"] == "superseded"
            assert host.store.get("capacity_operation", cap_id)["body"]["phase"] == "canceled"
            assert host.store.get("agent_attempt", spec.attempt_id)["body"]["status"] == "superseded"
            assert host._schedule_model(revised)
            successor = host._control(run.run_id, actor)["body"]["call_ref"]
            assert successor != old_ref
            assert host.store.get("model_request_intent", f"model-intent:{successor['id']}:1")
        assert host.store.get("model_request_intent", intent_id) == old_intent
        assert host.store.get("model_dispatch_attempt", f"model-dispatch:{intent_id}") is None
        assert host.store.get("model_response_receipt", f"model-response:{intent_id}") is None
    finally:
        host.close()


def test_saved_window_choices_reach_successive_pinned_model_inputs_with_peer_request(
    tmp_path, monkeypatch
):
    """A source-free navigation turn can lead to new checked text, then another window."""
    requests = []
    source_ref = None

    def transport(request, *, timeout):
        body = json.loads(request.data)
        requests.append(body)
        choices = [
            (0, 1600, "replace"),
            (1600, 1600, "append"),
            (1600, 800, "replace"),
            (1600, 1200, "replace"),
        ]
        offset, limit, mode = choices[min(len(requests) - 1, len(choices) - 1)]
        return tool("read_source_window", {
            "source_id": source_ref["id"], "source_version": source_ref["version"],
            "block_offset": 0, "char_offset": offset, "char_limit": limit,
            "selection_mode": mode,
        }, len(requests))

    host = service(tmp_path, monkeypatch, transport)
    try:
        opening = "DOI: 10.1234/continuation. "
        source_text = opening + "A" * (1600 - len(opening)) + "B" * 1600 + "C" * 1040
        run = host.start(LiveStartRequest(
            request_id="saved-window-choice-chain", question="Review a synthetic abstract",
            evidence_text=source_text, agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        source_ref = control["body"]["source_refs"][0]

        def through_saved_window(count):
            for _ in range(400):
                host._drive_run(host._run(run.run_id))
                current = host._control(run.run_id, "agent-synthesis")
                if (current["body"]["phase"] == "ready_model"
                        and len(current["body"]["tool_turn_refs"]) == count):
                    return current
                time.sleep(.005)
            pytest.fail(f"Window {count} did not reach a saved ready boundary")

        control = through_saved_window(1)
        host._update_control(
            control, input_budget={"observed_limit_tokens": 12000, "target_fraction": .70}
        )
        control = host._control(run.run_id, "agent-synthesis")
        assert host._schedule_model(control)
        first_continuation = host._control(run.run_id, "agent-synthesis")
        first_intent = host.store.get(
            "model_request_intent", f"model-intent:{first_continuation['body']['call_ref']['id']}:1"
        )
        first_body = json.loads(host.store.read_blob(first_intent["body"]["blob"]["sha256"]))
        assert first_intent["body"]["source_decisions"]
        assert "A" * 1000 in json.dumps(first_body["messages"])
        control = through_saved_window(2)

        # This explicit source-free choice tests the saved navigation handoff;
        # it is not a scientific rule or a fixed window-count limit.
        assert host._schedule_model(control, force_soft_recovery=True)
        recovery = host._control(run.run_id, "agent-synthesis")
        recovery_intent = host.store.get(
            "model_request_intent", f"model-intent:{recovery['body']['call_ref']['id']}:1"
        )
        recovery_body = json.loads(host.store.read_blob(recovery_intent["body"]["blob"]["sha256"]))
        recovery_index = json.loads(recovery_body["messages"][-1]["content"])
        assert recovery_intent["body"]["source_decisions"] == []
        navigation = recovery_index["source_read_delivery"]
        if navigation.get("details_in_current_tool_result"):
            navigation = next(
                item for message in recovery_body["messages"] if message["role"] == "tool"
                if (item := json.loads(message["content"])).get("schema")
                == "ora.source_window_delivery.v1"
            )
        assert navigation["source_text_in_this_call"] is False
        assert navigation["past_model_delivery"] == "not_established_by_saved_read"
        assert navigation["saved_read_succeeded"] is True
        assert navigation["coverage_basis"] == "saved_reading_not_model_delivery"
        assert navigation["first_uncovered_cursor"]["char_offset"] == 3200
        assert navigation["latest_window_next_cursor"]["char_offset"] == 3200
        assert navigation["window_ref"]["kind"] == "source_read_window"
        inventory_notice = next(
            item for message in recovery_body["messages"] if message["role"] == "user"
            if (item := json.loads(message["content"])).get("source_context_mode")
            == "source_free_recovery"
        )
        first_inventory_item = inventory_notice["inventory"]["items"][0]
        assert "saved_reading" not in first_inventory_item
        assert first_inventory_item["saved_reading_fetch"] == "read_source_inventory"
        assert first_inventory_item["fetch"]["tool"] == "read_source_window"
        assert "B" * 1600 not in json.dumps(recovery_body["messages"])
        control = through_saved_window(3)

        # Open peer work is material context that must survive the near-limit
        # selected-window admission or be exposed by an exact omission pointer.
        recipient = host.registry.register(
            "synthetic-peer-for-window-continuation",
            mission_id=control["body"]["mission_id"], run_id=run.run_id,
            role_id="independent_methods_reviewer_critic", display_name="Synthetic Peer",
        )
        sent = host.mailbox.send_request(
            "synthetic-window-peer-send",
            mission_id=control["body"]["mission_id"],
            request_id="synthetic-window-peer-request",
            message_id="synthetic-window-peer-message",
            requester_id=control["body"]["actor_id"], recipient_id=recipient["id"],
            question="Check the interpretation of the later result",
            expected_artifact="Independent review", cause_ref=control["body"]["proposal_ref"],
            payload={"question": "Check the interpretation of the later result"},
            task_ref=control["body"]["proposal_ref"],
        )
        assert sent["status"] == "accepted"
        assert host._schedule_model(control)
        selected = host._control(run.run_id, "agent-synthesis")
        selected_call = selected["body"]["call_ref"]
        selected_intent = host.store.get(
            "model_request_intent", f"model-intent:{selected_call['id']}:1"
        )
        selected_body = json.loads(host.store.read_blob(selected_intent["body"]["blob"]["sha256"]))
        assert selected_intent["body"]["source_decisions"]
        selected_packets = json.loads(selected_body["messages"][1]["content"])["source_packets"]
        assert len(selected_packets) == 1
        assert "B" * 800 in json.dumps(selected_packets)
        assert "A" * 1000 not in json.dumps(selected_packets)
        assert (selected_packets[0]["passage_refs"]
                == selected_intent["body"]["source_decisions"][0]["passage_refs"])
        inclusion = host.store.get("context_inclusion", f"context:{selected_intent['id']}")
        assert inclusion["body"]["source_refs"] == [source_ref]
        assert ("synthetic-window-peer-request" in json.dumps(selected_body["messages"])
                or any(item["ref"]["id"] == "synthetic-window-peer-request"
                       for item in inclusion["body"].get("omitted", [])))
        assert host.store.get("mail_request", "synthetic-window-peer-request")

        control = through_saved_window(4)
        assert host._schedule_model(control)
        later = host._control(run.run_id, "agent-synthesis")
        later_intent = host.store.get(
            "model_request_intent", f"model-intent:{later['body']['call_ref']['id']}:1"
        )
        later_body = json.loads(host.store.read_blob(later_intent["body"]["blob"]["sha256"]))
        later_packets = json.loads(later_body["messages"][1]["content"])["source_packets"]
        assert len(later_packets) == 1
        assert "B" * 1200 in json.dumps(later_packets)
        assert "A" * 1000 not in json.dumps(later_packets)
        assert (later_packets[0]["passage_refs"]
                == later_intent["body"]["source_decisions"][0]["passage_refs"])
        assert len(requests) == 4  # Preparation did not repeat a provider call.
    finally:
        host.close()
