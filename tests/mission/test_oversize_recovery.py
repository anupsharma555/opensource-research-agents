"""Verify an oversized model input recovers through a smaller, source-linked turn."""

import json
import math
import time
import urllib.error
from io import BytesIO

import pytest

from ora.contracts.models import LiveStartRequest
from ora.models.token_estimate import TOKENIZER_BASIS, estimate_request_tokens
from tests.mission.test_service import MODEL, answer, service, tool


@pytest.mark.parametrize("fresh_rejection", [False, True])
def test_413_recovery_changes_request_and_delivers_selected_source_window(
    tmp_path, monkeypatch, fresh_rejection,
):
    sent = []
    sent_bytes = []
    source_ref = None

    def transport(request, *, timeout):
        sent_bytes.append(bytes(request.data))
        sent.append(json.loads(request.data))
        if len(sent) == 1:
            raise urllib.error.HTTPError(
                "https://example.invalid", 413, "too large",
                {"x-ratelimit-limit-tokens": "8000", "Retry-After": "0.01"},
                BytesIO(b'{"error":{"code":"request_too_large"}}'),
            )
        if len(sent) == 2:
            return tool("read_source_window", {
                "source_id": source_ref["id"], "source_version": source_ref["version"],
                "block_offset": 0, "char_offset": 0, "char_limit": 800,
                "selection_mode": "replace",
            }, 2)
        assert len(sent) == 3, "Recovery must not need another identical source read"
        if fresh_rejection:
            raise urllib.error.HTTPError(
                "https://example.invalid", 413, "too large",
                {"x-ratelimit-limit-tokens": "8000", "Retry-After": "0.01"},
                BytesIO(b'{"error":{"code":"request_too_large"}}'),
            )
        return answer("The supplied test passage is available for assessment.", 3)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="oversize-source-recovery",
            question="Inspect the beginning of the supplied test source.",
            evidence_text="DOI: 10.1234/oversize. RECOVERY_PASSAGE " + "A" * 30000,
            agent_ids=["agent-synthesis"],
        ))
        source_ref = host._control(run.run_id, "agent-synthesis")["body"]["source_refs"][0]
        source_before = host.store.get(**source_ref)
        assert host._schedule_model(host._control(run.run_id, "agent-synthesis"))
        control = host._control(run.run_id, "agent-synthesis")
        work = host._work_for(control)
        spec = host.executor._spec_from(work)
        capacity_id = host.executor._capacity_id(spec)
        host.dispatch.poll(control["body"]["mission_id"])
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            failed = host.store.get(
                "model_response_receipt",
                f"model-response:model-intent:{spec.call_id}:{spec.call_version}",
            )
            capacity = host.capacity.get(capacity_id)
            if failed and capacity and capacity["phase"] == "failed":
                break
            time.sleep(.01)
        assert failed and failed["body"]["http_status"] == 413
        assert math.ceil(len(sent_bytes[0]) / 4 * 1.25) > 8000
        host._update_control(
            host._control(run.run_id, "agent-synthesis"),
            phase="waiting", waiting_reason="Saved old host left provider 413 waiting",
        )
        host._drive_run(host._run(run.run_id))
        repaired = host._control(run.run_id, "agent-synthesis")["body"]
        assert repaired["input_budget"]["observed_limit_tokens"] == 8000
        assert repaired["phase"] in {"retry_at", "ready_model"}
        assert host._effective_input_budget(host._control(run.run_id, "agent-synthesis"))["basis"] == (
            "provider_413_with_retry_after_and_token_rate_headers"
        )
        if repaired["phase"] == "retry_at":
            time.sleep(.03)

        # Advance the disposable quota ledger one fixed window after the
        # rejected request, then another only after the corrected tool runs.
        # A continuously advancing clock makes each new reservation recede.
        ledger_now = time.time()
        host.store.clock = lambda: ledger_now + 61
        for _ in range(300):
            host._drive_run(host._run(run.run_id))
            if len(sent) >= 2 and host.store.records("context_tool_result"):
                break
            time.sleep(.005)
        else:
            pytest.fail("Corrected source-window tool turn was not saved")
        assert len(sent) == 2
        assert len(sent_bytes[1]) < len(sent_bytes[0])
        estimate = estimate_request_tokens(sent_bytes[1])
        assert estimate.basis == TOKENIZER_BASIS
        assert estimate.tokens <= 8000
        assert "RECOVERY_PASSAGE" not in json.dumps(sent[1]["messages"])
        second_response = next(
            item for item in host.store.records("model_response_receipt")
            if item["body"]["status"] == "tool_requested"
        )
        accepted_control = host._control(run.run_id, "agent-synthesis")
        resolved = host._effective_input_budget(accepted_control)
        assert resolved["basis"] == "provider_accepted_smaller_413_repair"
        assert resolved["resolved_repair_response_ref"] == {
            "kind": "model_response_receipt", "id": second_response["id"], "version": 1,
        }
        assert resolved["observed_limit_tokens"] == 8000
        assert accepted_control["body"]["input_budget"]["response_ref"] == {
            "kind": "model_response_receipt", "id": failed["id"], "version": 1,
        }
        without_accepted = {**accepted_control, "body": {
            **accepted_control["body"], "response_ref": None, "tool_turn_refs": [],
        }}
        assert host._effective_input_budget(without_accepted) == repaired["input_budget"]
        wrong_actor = {**accepted_control, "body": {
            **accepted_control["body"], "actor_id": "unrelated-actor",
        }}
        assert host._effective_input_budget(wrong_actor) == repaired["input_budget"]
        budget = host.store.get(
            "mission_call_budget", second_response["body"]["call_ref"]["id"]
        )["body"]
        assert budget["source_turn_mode"] == "source_free_recovery"
        assert budget["request_token_estimate"]["tokens"] == estimate.tokens
        assert budget["request_token_estimate"]["basis"] == estimate.basis
        assert budget["source_inventory_ref"] is not None
        window = host.store.records("source_read_window")
        assert len(window) == 1
        assert window[0]["body"]["copy_ref"] == source_ref
        saved_tool = host.store.records("context_tool_result")
        assert len(saved_tool) == 1
        tool_message = json.loads(host.store.read_blob(saved_tool[0]["body"][
            "blob"
        ]["sha256"]))
        tool_result = json.loads(tool_message["content"])
        assert tool_message["name"] == "read_source_window"
        assert tool_result["requested_char_limit"] == 800
        assert tool_result["effective_char_limit"] == 800
        assert tool_result["returned_chars"] == 800
        assert tool_result["window_ref"]["id"] == window[0]["id"]

        host.store.clock = lambda: ledger_now + 122
        for _ in range(300):
            host._drive_run(host._run(run.run_id))
            if len(sent) >= 3:
                break
            time.sleep(.005)
        else:
            pytest.fail("Requested narrow source window was not sent to the model")
        assert len(sent) == 3
        third_intent = next(
            item for item in host.store.records("model_request_intent")
            if json.loads(host.store.read_blob(item["body"]["blob"]["sha256"])) == sent[2]
        )
        assert any(
            decision.get("allowed_for_model") is True
            and tool_result["passage_refs"][0] in decision.get("passage_refs", [])
            for decision in third_intent["body"]["source_decisions"]
        )
        assert "RECOVERY_PASSAGE" in json.dumps(sent[2]["messages"])
        third_call = third_intent["body"]["call_ref"]
        third_receipt = host.store.get(
            "model_response_receipt",
            f"model-response:model-intent:{third_call['id']}:{third_call['version']}",
        )
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline and third_receipt is None:
            time.sleep(.01)
            third_receipt = host.store.get(
                "model_response_receipt",
                f"model-response:model-intent:{third_call['id']}:{third_call['version']}",
            )
        assert third_receipt is not None
        if fresh_rejection:
            work = next(
                item for item in host.store.records("executor_work")
                if item["body"]["spec"].get("call_id") == third_call["id"]
            )
            cap_id = host.executor._capacity_id(host.executor._spec_from(work))
            cap = host.capacity.get(cap_id)
            deadline = time.monotonic() + 4
            while time.monotonic() < deadline and not (
                third_receipt and cap and cap["phase"] == "failed"
            ):
                time.sleep(.01)
                third_receipt = host.store.get(
                    "model_response_receipt",
                    f"model-response:model-intent:{third_call['id']}:{third_call['version']}",
                )
                cap = host.capacity.get(cap_id)
            assert third_receipt and third_receipt["body"]["http_status"] == 413
            assert cap and cap["phase"] == "failed"
            current = host._control(run.run_id, "agent-synthesis")
            third_ref = {
                "kind": "model_response_receipt", "id": third_receipt["id"], "version": 1,
            }
            if (current["body"].get("input_budget") or {}).get("response_ref") != third_ref:
                assert current["body"].get("work_id") == work["body"]["spec"]["work_id"]
                assert host._repair_oversize(current, work, cap)
            newest = host._control(run.run_id, "agent-synthesis")["body"]["input_budget"]
            assert newest["response_ref"] == third_ref
            assert newest["target_fraction"] == .70
            assert newest["response_ref"] != repaired["input_budget"]["response_ref"]
            assert host._effective_input_budget(
                host._control(run.run_id, "agent-synthesis")
            )["basis"] == "provider_413_with_retry_after_and_token_rate_headers"
            return
        unrelated_response = third_receipt
        not_repair = {**host._control(run.run_id, "agent-synthesis"), "body": {
            **host._control(run.run_id, "agent-synthesis")["body"],
            "response_ref": {"kind": "model_response_receipt", "id": unrelated_response["id"],
                             "version": 1},
            "tool_turn_refs": [],
        }}
        assert host._effective_input_budget(not_repair) == repaired["input_budget"]
        def newer_limit(tx):
            current = tx.get("model_response_receipt", unrelated_response["id"])
            return tx.put(
                "model_response_receipt", current["id"],
                {**current["body"], "rate_headers": {
                    **(current["body"].get("rate_headers") or {}),
                    "x-ratelimit-limit-tokens": "9000",
                }}, expected_version=current["version"],
            )

        newer = host.store.transact(
            "synthetic-newer-accepted-limit", {}, newer_limit,
            mission_id=host._run(run.run_id)["body"]["mission_id"],
        )
        newest_control = {**host._control(run.run_id, "agent-synthesis"), "body": {
            **host._control(run.run_id, "agent-synthesis")["body"],
            "response_ref": {"kind": "model_response_receipt", "id": newer["id"],
                             "version": newer["version"]},
        }}
        current_limit = host._effective_input_budget(newest_control)
        assert current_limit["basis"] == "provider_accepted_smaller_413_repair"
        assert current_limit["observed_limit_tokens"] == 9000
        assert current_limit["limit_evidence_ref"] == newest_control["body"]["response_ref"]

        def later_rate_limit(tx):
            current = tx.get("model_response_receipt", unrelated_response["id"])
            return tx.put(
                "model_response_receipt", current["id"],
                {**current["body"], "status": "error", "http_status": 429,
                 "rate_headers": {"x-ratelimit-limit-tokens": "7000"}},
                expected_version=current["version"],
            )

        limited = host.store.transact(
            "synthetic-newer-rate-header", {}, later_rate_limit,
            mission_id=host._run(run.run_id)["body"]["mission_id"],
        )
        paced_control = {**newest_control, "body": {
            **newest_control["body"],
            "response_ref": {"kind": "model_response_receipt", "id": limited["id"],
                             "version": limited["version"]},
        }}
        paced = host._effective_input_budget(paced_control)
        assert paced["basis"] == "provider_accepted_smaller_413_repair"
        assert paced["observed_limit_tokens"] == 7000
        assert paced["limit_evidence_ref"] == paced_control["body"]["response_ref"]
        assert all(item["model"] == MODEL for item in sent)
        assert all(item["reasoning_effort"] == "high" for item in sent)
        assert host.store.get(**source_ref) == source_before
        receipts = host.store.records("model_response_receipt")
        assert sum(item["body"].get("http_status") == 413 for item in receipts) == 1
        assert len(host.store.records("model_request_intent")) == 3
    finally:
        host.close()
