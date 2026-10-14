"""Opt-in source windows can fit whole blocks without claiming oversized delivery."""

from __future__ import annotations

import hashlib
import json
import time
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from ora.contracts.models import LiveStartRequest
from ora.evidence.coverage import extracted_text_coverage
from ora.mission import MissionService
from ora.models import GroqSelectedAdapter, ModelSession
from ora.models.token_estimate import (
    TOKENIZER_BASIS,
    estimate_context_tokens,
    estimate_request_tokens,
)
from ora.retrieval.providers import HTTPResult
from ora.state import StateStore
from ora.state.store import canonical
from tests.mission.test_service import ROOT, answer, drain, service, tool


@pytest.mark.parametrize("requested", [800, 3000])
def test_requested_abstract_window_enters_fitting_exact_model_input(
    tmp_path, monkeypatch, requested
):
    import ora.mission.dispatch as dispatch_module

    abstract = "DOI: 10.1234/window-extension. "
    abstract += "A" * (2661 - len(abstract))
    requests = []

    def model_transport(request, *, timeout):
        body = json.loads(request.data)
        requests.append(body)
        turn = len(requests)
        if turn == 1:
            response = tool("search_scholarly", {
                "route": "europe_pmc.search", "query": "synthetic abstract", "limit": 1,
            }, turn)
            response.headers = {
                "x-request-id": "offline-fixture",
                "x-ratelimit-limit-tokens": "8000",
            }
            return response
        prior = json.loads(next(
            message["content"] for message in reversed(body["messages"])
            if message["role"] == "tool"
        ))
        if turn == 2:
            return tool("inspect_hit", {"hit_id": prior["hits"][0]["hit_id"]}, turn)
        if turn == 3:
            saved_result = store.get(**prior["saved_tool_result_ref"])
            original = json.loads(store.read_blob(saved_result["body"]["blob"]["sha256"]))
            original_preview = json.loads(original["content"])["preview"]["text"]
            packet = json.loads(body["messages"][1]["content"])["source_packets"][0]
            assert len(original_preview) == 1900
            assert packet["text"].count(original_preview) == 1
            assert prior["preview"]["text_status"] == "exact_checked_passage_in_selected_source_packet"
            assert prior["preview"]["text_sha256"] == hashlib.sha256(original_preview.encode()).hexdigest()
            assert prior["preview"].get("text") is None
            assert prior["next_cursor"] == {"block_offset": 0, "char_offset": 1900}
            return tool("read_source_window", {
                "source_id": prior["source_id"],
                "source_version": prior["source_version"],
                "block_offset": 0, "char_offset": 0,
                "char_limit": requested, "selection_mode": "replace",
            }, turn)
        if turn == 4:
            saved_result = store.get(**prior["saved_tool_result_ref"])
            original = json.loads(store.read_blob(saved_result["body"]["blob"]["sha256"]))
            original_window = json.loads(original["content"])
            packet = json.loads(body["messages"][1]["content"])["source_packets"][0]
            assert original_window["requested_char_limit"] == requested
            assert original_window["effective_char_limit"] == requested
            assert prior["returned_chars"] == min(requested, len(abstract))
            assert packet["text"].count(original_window["passages"][0]["text_preview"]) == 1
            assert prior["next_cursor"] == original_window["next_cursor"] == (
                None if requested >= len(abstract)
                else {"block_offset": 0, "char_offset": requested}
            )
        return answer("The saved checked passage supports only its observed scope.", turn)

    def discovery_transport(method, url, *, body=None, headers=None):
        packet = {"hitCount": 1, "nextCursorMark": "*", "resultList": {"result": [{
            "source": "MED", "id": "123", "pmid": "123",
            "doi": "10.1234/window-extension", "title": "Synthetic abstract",
            "abstractText": abstract, "isOpenAccess": "Y",
        }]}}
        return HTTPResult(200, json.dumps(packet).encode(), {})

    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    store = StateStore(tmp_path)
    session = ModelSession(store, ROOT, adapter=GroqSelectedAdapter(
        ROOT, transport=model_transport,
    ))
    host = MissionService(
        ROOT, tmp_path, model_session=session,
        discovery_transport=discovery_transport,
        execution_mode="offline_test", start_supervisor=False,
    )
    try:
        run = host.start(LiveStartRequest(
            request_id=f"window-extension-{requested}",
            question="Read the synthetic abstract", agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id, limit=30)
        if len(requests) == 3:
            waiting = [
                item for item in store.records("capacity_operation")
                if item["body"].get("kind") == "model"
                and item["body"].get("phase") == "waiting"
                and item["body"].get("wait_reason") == "quota"
            ]
            assert len(waiting) == 1
            pending = waiting[0]["body"]
            intent = store.get(
                "model_request_intent", f"model-intent:{pending['call_id']}:1",
            )
            assert intent is not None
            assert store.get("model_dispatch_attempt", f"model-dispatch:{intent['id']}") is None
            receipts = store.records("model_response_receipt")
            assert len(receipts) == 3
            assert all(item["body"]["status"] == "tool_requested" for item in receipts)
            assert [item["body"]["usage"]["total_tokens"] for item in receipts] == [11] * 3
            quota = store.get("capacity_quota", "groq-selected")
            assert quota["body"]["observed_token_limit"] == 8000
            assert pending["estimated_tokens"] <= 8000
            assert pending["estimated_tokens"] + 33 > 8000
            assert pending["wake_at"] > store.clock()
            offset = max(0.0, pending["wake_at"] - time.time() + 0.01)
            host.store.clock = lambda: time.time() + offset
            monkeypatch.setattr(
                dispatch_module, "time", SimpleNamespace(time=host.store.clock),
            )
            drain(host, run.run_id, limit=30)
        assert len(requests) >= 4
        assert not store.records("model_response_lost")
        assert host._run(run.run_id)["body"]["status"] == "completed"
        window = store.records("source_read_window")[0]
        copy_ref = window["body"]["copy_ref"]
        selected = host._control(run.run_id, "agent-synthesis")["body"][
            "selected_passage_refs"
        ][copy_ref["id"]]
        assert selected == window["body"]["passage_refs"]
        coverage = extracted_text_coverage(store, copy_ref, selected, [])
        assert coverage["status"] == (
            "complete_extracted_text" if requested >= len(abstract)
            else "incomplete_extracted_text"
        )
        prepared = requests[3]
        intent = next(
            item for item in store.records("model_request_intent")
            if json.loads(store.read_blob(item["body"]["blob"]["sha256"])) == prepared
        )
        budget = store.get("mission_call_budget", intent["body"]["call_ref"]["id"])[
            "body"
        ]
        parent_call_ref = intent["body"]["parent_call_ref"]
        prior_receipt = store.get(
            "model_response_receipt",
            f"model-response:model-intent:{parent_call_ref['id']}:{parent_call_ref['version']}",
        )
        request_bytes = store.read_blob(intent["body"]["blob"]["sha256"])
        assert request_bytes == canonical(prepared).encode("utf-8")
        saved_estimate = intent["body"]["request_token_estimate"]
        assert saved_estimate["basis"] == TOKENIZER_BASIS
        input_estimate = estimate_request_tokens(
            request_bytes, basis=saved_estimate["basis"],
        )
        assert asdict(input_estimate) == saved_estimate
        assert budget["request_token_estimate"] == saved_estimate
        assert budget["observed_account_limit"] == 8000
        observed = store.get(**budget["account_limit_evidence_ref"])
        assert observed["body"]["rate_headers"]["x-ratelimit-limit-tokens"] == "8000"
        assert budget["exact_complete_request_estimate_tokens"] == input_estimate.tokens
        assert input_estimate.tokens + prior_receipt["body"]["usage"]["completion_tokens"] <= 8000
        packets = json.loads(prepared["messages"][1]["content"])["source_packets"]
        assert len(packets) == 1
        assert packets[0]["passage_refs"] == selected
        assert abstract[: min(requested, len(abstract))] in packets[0]["text"]
        assert prepared["model"] == requests[0]["model"]
        assert prepared["reasoning_effort"] == requests[0]["reasoning_effort"]
        if requested >= len(abstract):
            assert packets[0]["text"].count(abstract) == 1
        assert any(
            item["body"].get("source_turn_mode") != "source_free_recovery"
            and item["body"].get("current_tool_turn_ref")
            for item in store.records("mission_call_budget")
        )
        if requested == 800:
            inspection_result = next(
                item for item in store.records("context_tool_result")
                if json.loads(store.read_blob(item["body"]["blob"]["sha256"]))["name"]
                == "inspect_hit"
            )
            inspection_response = inspection_result["body"]["response_ref"]
            inspection_call = next(
                store.get(**item["body"]["call_ref"])
                for item in store.records("model_request_intent")
                if json.loads(store.read_blob(item["body"]["blob"]["sha256"])) == requests[2]
            )
            control = host._control(run.run_id, "agent-synthesis")
            initial_selection = {
                **control,
                "body": {**control["body"], "selected_passage_refs": {}},
            }
            exchange, _ = host.context._tool_exchange(inspection_response)
            assert host.context._compact_selected_inspection_preview(
                inspection_response, exchange[1], call=inspection_call,
                control=initial_selection, selected_source_refs=(),
            ) is None  # An inventory-only call has no selected source packet.
            assert host.context._compact_selected_inspection_preview(
                inspection_response, exchange[1], call=inspection_call,
                control=initial_selection, selected_source_refs=(copy_ref,),
            ) is not None
            use_ref = store.resolve(
                "source_use.copy", f"{copy_ref['id']}:{copy_ref['version']}"
            )

            def revoke(tx):
                use = tx.get(**{key: use_ref[key] for key in ("kind", "id", "version")})
                tx.put("source_use", use["id"], {
                    **use["body"], "policy_status": "revoked", "allowed_uses": [],
                }, expected_version=use["version"])
                return {"revoked": True}

            store.transact("revoke-inspection-after-delivery", {}, revoke)
            withheld, _ = host.context._tool_exchange(inspection_response)
            assert json.loads(withheld[1]["content"])["status"] == "data_unavailable_due_source_use"
            assert host.context._compact_selected_inspection_preview(
                inspection_response, exchange[1], call=inspection_call,
                control=initial_selection, selected_source_refs=(copy_ref,),
            ) is None
    finally:
        host.close()


def test_over_16k_request_pages_and_does_not_claim_oversized_model_delivery(
    tmp_path, monkeypatch
):
    requests = []
    source_ref = None

    def transport(request, *, timeout):
        body = json.loads(request.data)
        requests.append(body)
        return tool("read_source_window", {
            "source_id": source_ref["id"], "source_version": source_ref["version"],
            "block_offset": 0, "char_offset": 0, "char_limit": 20000,
            "selection_mode": "replace",
        }, len(requests))

    host = service(tmp_path, monkeypatch, transport)
    try:
        varied = "".join(
            f" Case {number:05d}; value {(number * 7919) % 100000:05d}; "
            f"score {(number * 1009) % 100000:05d};"
            for number in range(900)
        )
        run = host.start(LiveStartRequest(
            request_id="window-extension-over-cap",
            question="Read a synthetic long block",
            evidence_text="DOI: 10.1234/long-window. " + varied[:18000],
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        source_ref = control["body"]["source_refs"][0]
        for _ in range(400):
            host._drive_run(host._run(run.run_id))
            control = host._control(run.run_id, "agent-synthesis")
            if control["body"]["phase"] == "ready_model" and control["body"][
                "tool_turn_refs"
            ]:
                break
            time.sleep(.01)
        else:
            pytest.fail("The requested long window was not saved")

        result_record = host.store.records("context_tool_result")[0]
        exchange = json.loads(host.store.read_blob(result_record["body"]["blob"]["sha256"]))
        window_result = json.loads(exchange["content"])
        assert window_result["requested_char_limit"] == 20000
        assert window_result["effective_char_limit"] == 16000
        assert window_result["returned_chars"] == 16000
        assert window_result["next_request"]["char_offset"] == 16000
        assert window_result["next_request"]["char_limit"] == 20000
        window_ref = window_result["window_ref"]
        assert host.store.get(**window_ref)["body"]["char_limit"] == 16000
        assert host._control(run.run_id, "agent-synthesis")["body"][
            "selected_passage_refs"
        ][source_ref["id"]] == window_result["passage_refs"]

        host._update_control(
            control, input_budget={"observed_limit_tokens": 8000, "target_fraction": .70},
        )
        assert host._schedule_model(host._control(run.run_id, "agent-synthesis"))
        call_ref = host._control(run.run_id, "agent-synthesis")["body"]["call_ref"]
        intent = host.store.get(
            "model_request_intent", f"model-intent:{call_ref['id']}:{call_ref['version']}"
        )
        prepared = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        budget = host.store.get("mission_call_budget", call_ref["id"])["body"]
        selected_text = "".join(item["text_preview"] for item in window_result["passages"])
        assert len(selected_text) == 16000
        assert estimate_context_tokens(selected_text) > 6000
        assert estimate_context_tokens(selected_text) + budget["fixed_input_estimate"] > 8000
        assert budget["observed_account_limit"] == 8000
        assert budget["source_turn_mode"] == "source_free_recovery"
        assert intent["body"]["source_decisions"] == []
        assert "source_packets" not in json.dumps(prepared["messages"])
        index = json.loads(prepared["messages"][-1]["content"])
        navigation = index["source_read_delivery"]
        if navigation.get("details_in_current_tool_result"):
            navigation = next(
                item for message in prepared["messages"] if message["role"] == "tool"
                if (item := json.loads(message["content"])).get("schema")
                == "ora.source_window_delivery.v1"
            )
        assert navigation["source_text_in_this_call"] is False
        assert navigation["past_model_delivery"] == "not_established_by_saved_read"
        assert navigation["window_ref"] == window_ref
        assert navigation["returned_chars"] == 16000
        assert navigation["first_uncovered_cursor"]["char_offset"] == 16000
        assert len(requests) == 1  # Preparation alone did not call the provider.
    finally:
        host.close()
