"""Opt-in source windows can fit whole blocks without claiming oversized delivery."""

from __future__ import annotations

import json
import math
import time

import pytest

from ora.contracts.models import LiveStartRequest
from ora.evidence.coverage import extracted_text_coverage
from ora.mission import MissionService
from ora.models import GroqSelectedAdapter, ModelSession
from ora.retrieval.providers import HTTPResult
from ora.state import StateStore
from ora.state.store import canonical
from tests.mission.test_service import ROOT, answer, drain, service, tool


@pytest.mark.parametrize("requested", [800, 3000])
def test_requested_abstract_window_enters_fitting_exact_model_input(
    tmp_path, monkeypatch, requested
):
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
            assert len(prior["preview"]["text"]) == 1900
            assert prior["next_cursor"] == {"block_offset": 0, "char_offset": 1900}
            return tool("read_source_window", {
                "source_id": prior["source_id"],
                "source_version": prior["source_version"],
                "block_offset": 0, "char_offset": 0,
                "char_limit": requested, "selection_mode": "replace",
            }, turn)
        if turn == 4:
            assert prior["requested_char_limit"] == requested
            assert prior["effective_char_limit"] == requested
            assert prior["returned_chars"] == min(requested, len(abstract))
            assert prior["next_cursor"] == (
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
        assert len(requests) >= 4
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
        input_estimate = math.ceil(len(canonical(prepared).encode("utf-8")) / 4 * 1.25)
        assert budget["observed_account_limit"] == 8000
        observed = store.get(**budget["account_limit_evidence_ref"])
        assert observed["body"]["rate_headers"]["x-ratelimit-limit-tokens"] == "8000"
        assert budget["exact_complete_request_estimate_tokens"] == input_estimate
        assert input_estimate + prior_receipt["body"]["usage"]["completion_tokens"] <= 8000
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
        run = host.start(LiveStartRequest(
            request_id="window-extension-over-cap",
            question="Read a synthetic long block",
            evidence_text="DOI: 10.1234/long-window. " + "A" * 18000,
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
