"""Offline browser-search protocol fixtures; no provider call or source inspection."""

from __future__ import annotations

import hashlib
import importlib
import io
import json
import urllib.error
from dataclasses import replace
from pathlib import Path

import pytest

from ora.agents.capabilities import content_hash
from ora.mission.browser_search import project_browser_search, save_browser_search_hits
from ora.models.groq import (
    CallConfiguration,
    GroqSelectedAdapter,
    ModelPreflightError,
    classify_tool_failure_from_tools,
)
from ora.models.session import ModelSession, SessionError
from ora.state import StateStore
from ora.state.store import canonical

CONFIG = CallConfiguration("synthetic", "a" * 64, "Use returned results for discovery.")
SEARCH = {"type": "browser_search"}
LOCAL = {"type": "function", "function": {
    "name": "inspect_source", "parameters": {
        "type": "object", "properties": {}, "required": [], "additionalProperties": False,
    },
}}


def test_native_descriptor_400_is_not_an_unregistered_local_function():
    error = {"type": "invalid_request_error", "code": "tool_use_failed",
             "message": "attempted to call tool 'browser_search' which was not in request.tools"}
    assert classify_tool_failure_from_tools([SEARCH, LOCAL], error) == "native_tool_validation"
    assert classify_tool_failure_from_tools([LOCAL], error) == "unregistered_tool_function"
    assert classify_tool_failure_from_tools([SEARCH, LOCAL], {
        **error, "code": "other_failure",
    }) is None


def test_native_failed_generation_400_uses_descriptor_not_generated_arguments(
    tmp_path: Path, monkeypatch,
):
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    raw = json.dumps({"error": {
        "type": "invalid_request_error", "code": "tool_use_failed",
        "message": "attempted to call tool 'browser_search' which was not in request.tools",
        "failed_generation": (
            '{"name":"browser_search","arguments":'
            '"{\\"query\\":\\"synthetic docs\\",\\"topn\\":10,\\"source\\":\\"news\\"}"}'
        ),
    }}).encode()

    def rejected(_request, *, timeout):
        raise urllib.error.HTTPError("https://example.invalid", 400, "tool rejected", {},
                                     io.BytesIO(raw))

    adapter = GroqSelectedAdapter(tmp_path, transport=rejected)
    outcome = adapter.invoke(prepare(adapter))
    assert outcome.status == "failed" and outcome.http_status == 400
    assert outcome.error_kind == "native_tool_validation"
    assert outcome.response_sha256 == hashlib.sha256(raw).hexdigest()
    assert outcome.provider_error is not None
    assert set(outcome.provider_error) == {"type", "code", "message"}
    assert outcome.failed_generation_status == "returned"
    assert outcome.failed_generation == json.loads(raw)["error"]["failed_generation"]
    assert outcome.executable_tool_calls == ()
    assert outcome.failed_generation not in repr(outcome)


def prepare(adapter: GroqSelectedAdapter, tools=(SEARCH, LOCAL), **kwargs):
    return adapter.prepare(
        CONFIG, call_id="synthetic-call", attempt_id="synthetic-attempt",
        task_category="collect", phase="collect",
        messages=[{"role": "user", "content": "Find a source"}], tools=tools, **kwargs,
    )


def response(content: str) -> dict:
    return {"id": "synthetic-response", "model": "openai/gpt-oss-120b",
            "x_groq": {"id": "synthetic-request"},
            "choices": [{"message": {"role": "assistant", "content": content},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 18,
                      "total_tokens": 30}}


def saved_response(prepared, outcome, *, metadata=None):
    intent_ref = {"kind": "model_request_intent", "id": "intent", "version": 1}
    receipt_ref = {"kind": "model_response_receipt", "id": "receipt", "version": 1}
    request_raw = canonical(prepared.body).encode()
    private_raw = canonical({"content": outcome.content, "tool_calls": outcome.tool_calls,
                             "provider_metadata": metadata}).encode()
    request_sha = hashlib.sha256(request_raw).hexdigest()
    private_sha = hashlib.sha256(private_raw).hexdigest()
    intent = {"body": {"phase": "collect", "request_sha256": request_sha,
                       "blob": {"sha256": request_sha}}}
    receipt = {"body": {"intent_ref": intent_ref, "request_sha256": request_sha,
                        "response_sha256": outcome.response_sha256,
                        "status": outcome.status, "blob": {"sha256": private_sha}}}

    class Saved:
        def get(self, **ref):
            if ref == intent_ref:
                return intent
            if ref == receipt_ref:
                return receipt
            return None

        def read_blob(self, digest):
            return {request_sha: request_raw, private_sha: private_raw}[digest]

    return Saved(), receipt_ref


def durable_response(tmp_path: Path, prepared, outcome):
    store = StateStore(tmp_path)
    request_raw = canonical(prepared.body).encode()
    private_raw = canonical({"content": outcome.content, "tool_calls": outcome.tool_calls,
                             "provider_metadata": outcome.provider_metadata}).encode()
    request_blob = store.put_blob(request_raw)
    private_blob = store.put_blob(private_raw)

    def save(tx):
        intent = tx.put("model_request_intent", "synthetic-intent", {
            "mission_id": "synthetic-mission", "task_id": "synthetic-task",
            "instance_id": "synthetic-actor", "phase": "collect",
            "request_sha256": prepared.request_sha256, "blob": request_blob,
        })
        receipt = tx.put("model_response_receipt", "synthetic-receipt", {
            "mission_id": "synthetic-mission", "task_id": "synthetic-task",
            "intent_ref": {key: intent[key] for key in ("kind", "id", "version")},
            "request_sha256": prepared.request_sha256,
            "response_sha256": outcome.response_sha256,
            "status": outcome.status, "blob": private_blob,
        })
        return {key: receipt[key] for key in ("kind", "id", "version")}

    receipt_ref = store.transact("synthetic-response-save", {}, save,
                                 mission_id="synthetic-mission")
    return store, receipt_ref


def test_browser_search_and_local_function_share_one_hashed_collect_request(tmp_path: Path):
    adapter = GroqSelectedAdapter(tmp_path, transport=lambda *_a, **_kw: pytest.fail("network"))
    prepared = prepare(adapter)
    assert prepared.body["tools"] == [SEARCH, LOCAL]
    assert prepared.body["reasoning_effort"] == "low"
    assert prepared.manifest()["effective_reasoning_effort"] == "low"
    assert prepared.manifest()["reasoning_effort_basis"] == "browser_search"
    assert prepared.manifest()["requested_settings"]["reasoning_effort"] == "low"
    assert prepared.body["tool_choice"] == "auto"
    assert "response_format" not in prepared.body
    assert prepared.request_sha256 != prepare(adapter, tools=(LOCAL,)).request_sha256
    ordinary = prepare(adapter, tools=(LOCAL,))
    assert ordinary.body["reasoning_effort"] == "medium"
    assert ordinary.manifest()["reasoning_effort_basis"] == "selected_configuration"
    assert ordinary.manifest()["effective_reasoning_effort"] == "medium"
    text = "Snippet 〖2†L6-L10〗 https://example.org/article. More text."
    payload = response(text)
    payload["choices"][0]["message"]["annotations"] = [{"marker": "〖2†L6-L10〗"}]
    payload["x_groq"]["search_receipt"] = {"synthetic": True}
    outcome = adapter.decode(prepared, payload)
    assert outcome.status == "complete"
    assert outcome.executable_tool_calls == ()
    assert outcome.usage["total_tokens"] == 30
    assert outcome.response_sha256
    assert outcome.provider_request_id == "synthetic-request"
    assert outcome.provider_metadata["message"]["annotations"] == [
        {"marker": "〖2†L6-L10〗"}]
    assert outcome.provider_metadata["response"]["x_groq"]["search_receipt"] == {
        "synthetic": True}
    saved, receipt_ref = saved_response(prepared, outcome, metadata=outcome.provider_metadata)
    projected = project_browser_search(saved, receipt_ref, limit=12)
    assert projected["content"] == text[:12]
    assert projected["next_offset"] == 12
    full = project_browser_search(saved, receipt_ref, limit=12000)
    assert full["explicit_urls"] == ["https://example.org/article"]
    assert full["explicit_citation_markers"] == ["〖2†L6-L10〗"]
    assert projected["next_request"]["content_sha256"] == full["content_sha256"]
    assert full["provider_metadata_present"] is True
    assert projected["source_inspection"] == "not_performed"
    assert projected["search_execution"] == "not_independently_confirmed"
    assert projected["response_sha256"] == outcome.response_sha256


def test_browser_search_only_and_opaque_marker_are_discovery_only(tmp_path: Path):
    adapter = GroqSelectedAdapter(tmp_path)
    prepared = prepare(adapter, tools=(SEARCH,))
    outcome = adapter.decode(prepared, response("Result 〖1†L1-L4〗"))
    saved, receipt_ref = saved_response(prepared, outcome)
    projected = project_browser_search(saved, receipt_ref)
    assert projected["explicit_urls"] == []
    assert projected["explicit_citation_markers"] == ["〖1†L1-L4〗"]
    assert outcome.executable_tool_calls == ()


def test_browser_search_effort_is_per_call_and_saved_body_round_trips(tmp_path: Path):
    adapter = GroqSelectedAdapter(tmp_path)
    search = prepare(adapter, tools=(SEARCH, LOCAL))
    followup = adapter.prepare(
        CONFIG, call_id="synthetic-followup", attempt_id="synthetic-followup-attempt",
        parent_call_id=search.call_id, task_category="collect", phase="collect",
        messages=[{"role": "user", "content": "Inspect the lead"}], tools=(LOCAL,),
    )
    assert search.body["reasoning_effort"] == "low"
    assert followup.body["reasoning_effort"] == "medium"
    assert followup.manifest()["reasoning_effort_basis"] == "selected_configuration"
    assert search.configuration_sha256 == followup.configuration_sha256 == CONFIG.configuration_sha256
    assert search.credential_profile == followup.credential_profile
    assert search.request_sha256 == hashlib.sha256(canonical(search.body).encode()).hexdigest()
    assert followup.request_sha256 == hashlib.sha256(canonical(followup.body).encode()).hexdigest()

    schema = {"type": "object", "properties": {}, "required": [],
              "additionalProperties": False}
    final = adapter.prepare(
        CONFIG, call_id="synthetic-final", attempt_id="synthetic-final-attempt",
        parent_call_id=followup.call_id, task_category="final", phase="final",
        messages=[{"role": "user", "content": "Finish"}], schema_name="result", schema=schema,
    )
    assert final.body["reasoning_effort"] == "medium"
    assert final.manifest()["reasoning_effort_basis"] == "selected_configuration"


def test_explicit_high_configuration_restores_after_browser_low_override(tmp_path: Path):
    adapter = GroqSelectedAdapter(tmp_path)
    older = replace(CONFIG, reasoning_effort="high")
    browser = adapter.prepare(
        older, call_id="old-browser", attempt_id="old-browser-attempt",
        task_category="collect", phase="collect",
        messages=[{"role": "user", "content": "Find a lead"}], tools=(SEARCH, LOCAL),
    )
    ordinary = adapter.prepare(
        older, call_id="old-ordinary", attempt_id="old-ordinary-attempt",
        parent_call_id=browser.call_id, task_category="collect", phase="collect",
        messages=[{"role": "user", "content": "Inspect the lead"}], tools=(LOCAL,),
    )
    assert browser.body["reasoning_effort"] == "low"
    assert browser.manifest()["reasoning_effort_basis"] == "browser_search"
    assert ordinary.body["reasoning_effort"] == "high"
    assert ordinary.manifest()["reasoning_effort_basis"] == "selected_configuration"


def test_literal_url_leads_save_metadata_only_hits_with_exact_provenance(tmp_path: Path):
    adapter = GroqSelectedAdapter(tmp_path)
    prepared = prepare(adapter)
    content = ("Leads https://example.org/article and https://example.org/article. "
               "Unsafe http://example.org/insecure and "
               "https://user:pass@example.org/private and "
               "https://example.org/?api_key=secret. Opaque 〖1†L1-L3〗")
    outcome = adapter.decode(prepared, response(content))
    store, receipt_ref = durable_response(tmp_path / "store", prepared, outcome)
    first = save_browser_search_hits(store, receipt_ref)
    second = save_browser_search_hits(store, receipt_ref)
    assert first == second
    assert len(first["hits"]) == 1
    lead = first["hits"][0]
    assert lead["original_url"] == "https://example.org/article"
    assert lead["source_inspection"] == "not_performed"
    hit = store.get(**lead["hit_ref"])
    assert hit["body"]["mission_id"] == "synthetic-mission"
    assert hit["body"]["task_id"] == "synthetic-task"
    assert hit["body"]["agent_instance_id"] == "synthetic-actor"
    assert hit["body"]["route"] == "groq.browser_search.url_leads"
    assert hit["body"]["source"]["content"] is None
    assert hit["body"]["source"]["access_surface"] == "metadata"
    assert hit["body"]["source"]["rights_status"] == "unknown"
    assert any(link["relation"] == "derived_from"
               and link["target"] == receipt_ref for link in store.links(hit))
    assert len(store.records("retrieval_hit")) == 1


def test_opaque_browser_response_creates_no_retrieval_hit(tmp_path: Path):
    adapter = GroqSelectedAdapter(tmp_path)
    prepared = prepare(adapter)
    outcome = adapter.decode(prepared, response("Only opaque marker 〖1†L1-L3〗"))
    store, receipt_ref = durable_response(tmp_path / "store", prepared, outcome)
    result = save_browser_search_hits(store, receipt_ref)
    assert result["status"] == "no_literal_fetchable_urls"
    assert store.records("retrieval_hit") == []


def test_provider_executed_browser_result_urls_are_bounded_discovery_leads(tmp_path: Path):
    adapter = GroqSelectedAdapter(tmp_path)
    prepared = prepare(adapter, tools=(SEARCH,))
    payload = response("Only provider citation 【1†L4-L9】; no literal URL in assistant text.")
    payload["choices"][0]["message"]["executed_tools"] = [
        {"index": 0, "name": "browser.search", "type": "browser_search",
         "search_results": {"results": [
             {"url": "https://example.org/search", "title": "Search result"},
             {"url": "https://user:pass@example.org/private"},
             {"url": "https://example.org/?api_key=secret"},
         ]}},
        {"index": 1, "name": "browser.open", "type": "browser.open",
         "search_results": {"results": [{"url": "https://example.org/open"}]}},
    ]
    outcome = adapter.decode(prepared, payload)
    store, receipt_ref = durable_response(tmp_path / "store", prepared, outcome)
    projected = project_browser_search(store, receipt_ref)
    assert projected["explicit_urls"] == []
    assert projected["explicit_citation_markers"] == ["【1†L4-L9】"]
    assert projected["provider_metadata_url_lead_count"] == 2
    saved = save_browser_search_hits(store, receipt_ref)
    assert saved["status"] == "url_leads_saved"
    assert [hit["original_url"] for hit in saved["hits"]] == [
        "https://example.org/search", "https://example.org/open",
    ]
    for hit in saved["hits"]:
        record = store.get(**hit["hit_ref"])
        assert record["body"]["source"]["content"] is None
        assert record["body"]["source"]["access_surface"] == "metadata"
        assert any(link["relation"] == "derived_from" and link["target"] == receipt_ref
                   for link in store.links(record))
    assert store.records("source_copy") == []


def test_provider_metadata_url_leads_stop_at_sixteen(tmp_path: Path):
    adapter = GroqSelectedAdapter(tmp_path)
    prepared = prepare(adapter, tools=(SEARCH,))
    payload = response("Only citation 【1†L1-L2】")
    payload["choices"][0]["message"]["executed_tools"] = [{
        "index": 0, "name": "browser.search", "type": "browser_search",
        "search_results": {"results": [
            {"url": f"https://example.org/result-{index}"} for index in range(40)
        ]},
    }]
    outcome = adapter.decode(prepared, payload)
    store, receipt_ref = durable_response(tmp_path / "store", prepared, outcome)
    assert project_browser_search(store, receipt_ref)["provider_metadata_url_lead_count"] == 16
    assert len(save_browser_search_hits(store, receipt_ref)["hits"]) == 16


def test_legacy_browser_leads_replay_preserves_old_hit_subset_and_conversion(
    tmp_path: Path, monkeypatch,
):
    browser_module = importlib.import_module("ora.mission.browser_search")
    adapter = GroqSelectedAdapter(tmp_path)
    prepared = prepare(adapter, tools=(SEARCH,))
    payload = response("Older answer https://example.org/old 【1†L1-L2】")
    payload["choices"][0]["message"]["executed_tools"] = [{
        "index": 0, "name": "browser.search", "type": "browser_search",
        "search_results": {"results": [{"url": "https://example.org/new-metadata"}]},
    }]
    outcome = adapter.decode(prepared, payload)
    store, receipt_ref = durable_response(tmp_path / "store", prepared, outcome)
    original_record_delivery = browser_module.record_delivery

    def legacy_record_delivery(store, request, mapped, **kwargs):
        old_request = {**request, "filters": {**request["filters"],
                                              "conversion": "literal_url_leads_only"}}
        return original_record_delivery(store, old_request, mapped, **kwargs)

    with monkeypatch.context() as legacy:
        legacy.setattr(browser_module, "_provider_metadata_urls", lambda _private: [])
        legacy.setattr(browser_module, "record_delivery", legacy_record_delivery)
        old = save_browser_search_hits(store, receipt_ref)
    assert [hit["original_url"] for hit in old["hits"]] == ["https://example.org/old"]
    delivery = store.get(**old["delivery_ref"])
    assert delivery["body"]["envelope"]["request"]["filters"]["conversion"] == (
        "literal_url_leads_only"
    )
    before = {kind: len(store.records(kind)) for kind in (
        "retrieval_hit", "retrieval_delivery", "browser_search_leads",
    )}
    assert project_browser_search(store, receipt_ref)["provider_metadata_url_lead_count"] == 1
    assert save_browser_search_hits(store, receipt_ref) == old
    assert {kind: len(store.records(kind)) for kind in before} == before
    assert all(hit["original_url"] != "https://example.org/new-metadata"
               for hit in save_browser_search_hits(store, receipt_ref)["hits"])


def test_legacy_delivery_committed_before_lead_link_is_reconciled(
    tmp_path: Path, monkeypatch,
):
    browser_module = importlib.import_module("ora.mission.browser_search")
    adapter = GroqSelectedAdapter(tmp_path)
    prepared = prepare(adapter, tools=(SEARCH,))
    payload = response("Old literal https://example.org/old 【1†L1-L2】")
    payload["choices"][0]["message"]["executed_tools"] = [{
        "index": 0, "name": "browser.search", "type": "browser_search",
        "search_results": {"results": [{"url": "https://example.org/new-metadata"}]},
    }]
    outcome = adapter.decode(prepared, payload)
    store, receipt_ref = durable_response(tmp_path / "store", prepared, outcome)
    original_record_delivery = browser_module.record_delivery
    original_transact = store.transact

    def legacy_record_delivery(store, request, mapped, **kwargs):
        old_request = {**request, "filters": {**request["filters"],
                                              "conversion": "literal_url_leads_only"}}
        return original_record_delivery(store, old_request, mapped, **kwargs)

    def crash_before_link(operation_id, *args, **kwargs):
        if operation_id.startswith("browser-search-hit-provenance-"):
            raise RuntimeError("injected crash after retrieval delivery commit")
        return original_transact(operation_id, *args, **kwargs)

    with monkeypatch.context() as legacy:
        legacy.setattr(browser_module, "_provider_metadata_urls", lambda _private: [])
        legacy.setattr(browser_module, "record_delivery", legacy_record_delivery)
        legacy.setattr(store, "transact", crash_before_link)
        with pytest.raises(RuntimeError, match="injected crash"):
            save_browser_search_hits(store, receipt_ref)
    assert len(store.records("retrieval_delivery")) == 1
    assert len(store.records("retrieval_hit")) == 1
    assert store.records("browser_search_leads") == []
    original_delivery = store.records("retrieval_delivery")[0]
    assert original_delivery["body"]["envelope"]["request"]["filters"]["conversion"] == (
        "literal_url_leads_only"
    )
    reconciled = save_browser_search_hits(store, receipt_ref)
    assert reconciled["delivery_ref"] == {
        key: original_delivery[key] for key in ("kind", "id", "version")
    }
    assert [hit["original_url"] for hit in reconciled["hits"]] == ["https://example.org/old"]
    assert len(store.records("retrieval_delivery")) == 1
    assert len(store.records("retrieval_hit")) == 1
    assert len(store.records("browser_search_leads")) == 1
    assert save_browser_search_hits(store, receipt_ref) == reconciled


def test_old_response_without_saved_urls_can_gain_metadata_leads(tmp_path: Path, monkeypatch):
    browser_module = importlib.import_module("ora.mission.browser_search")
    adapter = GroqSelectedAdapter(tmp_path)
    prepared = prepare(adapter, tools=(SEARCH,))
    payload = response("Only opaque citation 【1†L1-L2】")
    payload["choices"][0]["message"]["executed_tools"] = [{
        "index": 0, "name": "browser.open", "type": "browser.open",
        "search_results": {"results": [{"url": "https://example.org/metadata"}]},
    }]
    outcome = adapter.decode(prepared, payload)
    store, receipt_ref = durable_response(tmp_path / "store", prepared, outcome)
    with monkeypatch.context() as legacy:
        legacy.setattr(browser_module, "_provider_metadata_urls", lambda _private: [])
        assert save_browser_search_hits(store, receipt_ref)["status"] == "no_literal_fetchable_urls"
    assert store.records("browser_search_leads") == []
    saved = save_browser_search_hits(store, receipt_ref)
    assert [hit["original_url"] for hit in saved["hits"]] == [
        "https://example.org/metadata",
    ]


def test_saved_browser_lead_with_changed_task_scope_is_rejected_on_replay(tmp_path: Path):
    adapter = GroqSelectedAdapter(tmp_path)
    prepared = prepare(adapter, tools=(SEARCH,))
    outcome = adapter.decode(prepared, response("Lead https://example.org/old"))
    store, receipt_ref = durable_response(tmp_path / "store", prepared, outcome)
    save_browser_search_hits(store, receipt_ref)
    old = store.get("browser_search_leads", f"browser-leads:{receipt_ref['id']}")
    store.transact("test-changed-browser-lead-scope", {}, lambda tx: tx.put(
        "browser_search_leads", old["id"],
        {**old["body"], "task_id": "other-task"}, expected_version=old["version"],
    ))
    with pytest.raises(ValueError, match="response or task scope"):
        save_browser_search_hits(store, receipt_ref)
    assert len(store.records("retrieval_delivery")) == 1


@pytest.mark.parametrize("executed_tools", [
    None,
    [{"index": 0, "name": "unknown", "type": "browser_search",
      "search_results": {"results": [{"url": "https://example.org/unknown"}]}}],
    [{"index": 0, "name": "browser.search", "type": "browser_search",
      "search_results": {"results": {"url": "https://example.org/malformed"}}}],
    [{"index": 0, "name": "browser.search", "type": "browser_search",
      "search_results": {"results": [
          {"url": "http://example.org/insecure"},
          {"url": "https://127.0.0.1/private"},
          {"url": "https://example.local/private"},
          {"url": "https://example.org/?api_key=secret"},
      ]}}],
])
def test_unknown_malformed_or_private_executed_metadata_is_not_a_source(
    tmp_path: Path, executed_tools,
):
    adapter = GroqSelectedAdapter(tmp_path)
    prepared = prepare(adapter, tools=(SEARCH,))
    payload = response("Only citation 【1†L1-L2】")
    if executed_tools is not None:
        payload["choices"][0]["message"]["executed_tools"] = executed_tools
    outcome = adapter.decode(prepared, payload)
    store, receipt_ref = durable_response(tmp_path / "store", prepared, outcome)
    projected = project_browser_search(store, receipt_ref)
    assert projected["provider_metadata_url_lead_count"] == 0
    result = save_browser_search_hits(store, receipt_ref)
    assert result["status"] == "no_literal_fetchable_urls"
    assert store.records("retrieval_hit") == []


@pytest.mark.parametrize("tools,problem", [
    (({"type": "code_interpreter"},), "unsupported tool type"),
    ((SEARCH, SEARCH), "only once"),
    (({"type": "browser_search", "function": {}},), "unsupported tool type"),
])
def test_other_builtin_shapes_fail_before_dispatch(tmp_path: Path, tools, problem):
    adapter = GroqSelectedAdapter(tmp_path, transport=lambda *_a, **_kw: pytest.fail("network"))
    with pytest.raises(ModelPreflightError, match=problem):
        prepare(adapter, tools=tools)


def test_browser_search_stream_and_strict_final_are_rejected(tmp_path: Path):
    adapter = GroqSelectedAdapter(tmp_path)
    with pytest.raises(ModelPreflightError, match="non-streaming"):
        adapter.prepare(replace(CONFIG, stream=True), call_id="c", attempt_id="a",
                        task_category="collect", phase="collect",
                        messages=[{"role": "user", "content": "Find"}], tools=(SEARCH,))
    with pytest.raises(ModelPreflightError, match="no tools"):
        adapter.prepare(CONFIG, call_id="c", attempt_id="a", task_category="final",
                        phase="final", messages=[{"role": "user", "content": "Finish"}],
                        tools=(SEARCH,), schema_name="final", schema={
                            "type": "object", "properties": {}, "required": [],
                            "additionalProperties": False,
                        })


def test_session_uses_only_exact_saved_provider_descriptor():
    schema = {"type": "object", "properties": {}, "required": [],
              "additionalProperties": False}
    descriptor = {"tool_id": "browser_search", "schema": schema,
                  "schema_sha256": content_hash(schema),
                  "execution_model": "provider_builtin"}
    call = {"body": {"exposed_tools": ["browser_search"],
                     "permitted_tools": {"browser_search": descriptor}}}
    assert ModelSession._tools(call) == (SEARCH,)
    descriptor["execution_model"] = "local"
    with pytest.raises(SessionError, match="descriptor is invalid"):
        ModelSession._tools(call)
    descriptor["execution_model"] = "provider_builtin"
    descriptor["schema"] = {"type": "object", "properties": {"query": {"type": "string"}}}
    with pytest.raises(SessionError, match="descriptor changed"):
        ModelSession._tools(call)
