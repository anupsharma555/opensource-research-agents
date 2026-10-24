"""The optional corpus is selected by a peer and source text passes intake."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ora.contracts.models import LiveStartRequest
from ora.evidence import EvidenceError
from ora.evidence.windows import read_source_window
from ora.mission import MissionService
from ora.mission.bheb_tools import sync_bheb_source_uses
from ora.models import GroqSelectedAdapter, ModelSession
from ora.retrieval.providers import HTTPResult
from ora.state import StateStore
from tests.mission.test_service import ROOT, answer, drain, tool
from tests.retrieval.test_bheb import MATCHED, _fixture, _response


def _host(tmp_path: Path, monkeypatch, *, source: bool, model_transport,
          matched: bool = True):
    corpus_dir = tmp_path / "synthetic-corpus"
    corpus_dir.mkdir()
    config, attrs, text = _fixture(corpus_dir, monkeypatch, source=source)
    if source:
        policy_path = corpus_dir / "policy.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy["sources"]["synthetic-1"]["declared_surface"] = "abstract"
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
    monkeypatch.setenv("OSRA_BHEB_CONFIG", str(config))
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-model-key")
    state_path = tmp_path / "state"
    store = StateStore(state_path)
    session = ModelSession(store, ROOT, adapter=GroqSelectedAdapter(ROOT, transport=model_transport))
    def bheb_transport(*_args, **_kwargs):
        if matched:
            return _response(attrs)
        response = json.loads(_response(attrs).body)
        response["data"][0]["content"][0]["text"] = "Unmatched provider excerpt"
        return HTTPResult(200, json.dumps(response).encode(), {})

    host = MissionService(
        ROOT, state_path, model_session=session,
        bheb_transport=bheb_transport,
        execution_mode="offline_test", start_supervisor=False,
    )
    return host, text, corpus_dir


def _last_result(packet: dict, name: str) -> dict:
    return next(
        json.loads(item["content"])
        for item in reversed(packet["messages"])
        if item.get("role") == "tool" and item.get("name") == name
    )


def test_runtime_reuses_exact_query_but_keeps_diverse_queries_and_refresh(tmp_path, monkeypatch):
    packets = []
    results = []
    queries = [
        {"query": "fictional adherence"},
        {"query": "fictional adherence"},
        {"query": "fictional safety"},
        {"query": "fictional adherence", "refresh": True},
    ]

    def model_transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        if len(packets) == 1:
            return tool("request_capability", {"tool_id": "search_bheb_corpus"}, 1)
        schema = next(item["function"] for item in packet["tools"]
                      if item["function"]["name"] == "search_bheb_corpus")
        assert schema["parameters"]["properties"]["refresh"]["type"] == "boolean"
        assert "search_scholarly" in {item["function"]["name"] for item in packet["tools"]}
        if len(packets) > 2:
            results.append(_last_result(packet, "search_bheb_corpus"))
        index = len(packets) - 2
        if index < len(queries):
            return tool("search_bheb_corpus", queries[index], len(packets))
        return answer("The saved searches were compared.", len(packets))

    host, _, _ = _host(tmp_path, monkeypatch, source=False, model_transport=model_transport)
    calls = []
    original = host.bheb.transport

    def counted(*args, **kwargs):
        calls.append(1)
        return original(*args, **kwargs)

    host.bheb.transport = counted
    try:
        run = host.start(LiveStartRequest(
            request_id="synthetic-bheb-reuse", question="Compare fictional adherence and safety.",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id, limit=20)
        assert [item["reuse_status"] for item in results] == [
            "fresh", "reused", "fresh", "refreshed",
        ]
        assert len({item["operation_id"] for item in results}) == 4
        assert len(calls) == 3
    finally:
        host.close()


def test_peer_selects_private_search_and_only_checked_synthetic_text_enters_model(
    tmp_path, monkeypatch,
):
    packets = []
    selected = {}
    source_text = MATCHED

    def model_transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        tools = {item["function"]["name"] for item in packet["tools"]}
        if len(packets) == 1:
            assert "discover_capabilities" in tools
            assert "search_scholarly" in tools
            assert "search_bheb_corpus" not in tools
            assert "inspect_bheb_hit" not in tools
            assert "search_bheb_corpus" in packet["messages"][0]["content"]
            return tool("request_capability", {"tool_id": "search_bheb_corpus"}, 1)
        if len(packets) == 2:
            assert "search_bheb_corpus" in tools
            selection = _last_result(packet, "request_capability")
            assert selection["corpus_summary"]["source_count"] == 1
            assert selection["corpus_summary"]["filter_coverage_counts"]["evaluation_stage"] == 0
            assert "similarity" in selection["selection_guidance"].lower()
            assert "vs_synthetic" not in json.dumps(selection)
            assert "synthetic-test-key" not in json.dumps(selection)
            assert str(tmp_path) not in json.dumps(selection)
            return tool("search_bheb_corpus", {"query": "fictional behavioral outcome"}, 2)
        if len(packets) == 3:
            result = _last_result(packet, "search_bheb_corpus")
            assert result["status"] == "completed" and len(result["hits"]) == 1
            assert source_text not in json.dumps(result)
            selected.update(hit_id=result["hits"][0]["hit_id"],
                            search_operation_id=result["operation_id"])
            assert "inspect_bheb_hit" not in tools
            return tool("request_capability", {"tool_id": "inspect_bheb_hit"}, 3)
        if len(packets) == 4:
            assert "inspect_bheb_hit" in tools
            return tool("inspect_bheb_hit", selected, 4)
        result = _last_result(packet, "inspect_bheb_hit")
        assert result["status"] == "inspected"
        assert source_text in json.dumps(packet["messages"])
        assert "Synthetic self-authored introduction." not in result.get("preview", "")
        assert result["passages"]
        return answer("The synthetic source was inspected.", len(packets))

    host, _, corpus_dir = _host(tmp_path, monkeypatch, source=True, model_transport=model_transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="synthetic-bheb-path", question="Review a fictional behavioral outcome.",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id, limit=20)
        assert len(packets) >= 5
        assert host.store.records("bheb_source_binding"), [
            item["content"] for packet in packets for item in packet["messages"]
            if item.get("role") == "tool" and item.get("name") == "inspect_bheb_hit"
        ]
        copy = host.store.records("source_copy")[0]
        copy_ref = {key: copy[key] for key in ("kind", "id", "version")}
        policy_path = corpus_dir / "policy.json"
        policy = json.loads(policy_path.read_text(encoding="utf-8"))
        policy["revoked"] = True
        policy_path.write_text(json.dumps(policy), encoding="utf-8")
        sync_bheb_source_uses(host)
        use = host.store.resolve("source_use.copy", f"{copy['id']}:{copy['version']}")
        assert use["body"]["policy_status"] == "revoked"
        with pytest.raises(EvidenceError):
            read_source_window(host.evidence, "after-bheb-revocation", copy_ref)
    finally:
        host.close()


def test_unmatched_excerpt_opens_reader_without_prepending_source_prefix(
    tmp_path, monkeypatch,
):
    packets = []
    selected = {}

    def model_transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        if len(packets) == 1:
            return tool("request_capability", {"tool_id": "search_bheb_corpus"}, 1)
        if len(packets) == 2:
            return tool("search_bheb_corpus", {"query": "fictional outcome"}, 2)
        if len(packets) == 3:
            result = _last_result(packet, "search_bheb_corpus")
            selected.update(hit_id=result["hits"][0]["hit_id"],
                            search_operation_id=result["operation_id"])
            return tool("request_capability", {"tool_id": "inspect_bheb_hit"}, 3)
        if len(packets) == 4:
            return tool("inspect_bheb_hit", selected, 4)
        if len(packets) == 5:
            result = _last_result(packet, "inspect_bheb_hit")
            assert result["status"] == "source_ready_for_read"
            assert not host.store.records("source_passage")
            assert "Synthetic self-authored introduction." not in json.dumps(packet["messages"])
            selected.update(source_id=result["source_id"], source_version=result["source_version"])
            return tool("request_capability", {"tool_id": "read_source_window"}, 5)
        if len(packets) == 6:
            return tool("read_source_window", {
                "source_id": selected["source_id"],
                "source_version": selected["source_version"],
                "block_offset": 0, "char_offset": 0, "char_limit": 1000,
                "selection_mode": "replace",
            }, 6)
        assert _last_result(packet, "read_source_window")["passages"]
        return answer("A specific source window was inspected.", len(packets))

    host, _, _ = _host(tmp_path, monkeypatch, source=True,
                       model_transport=model_transport, matched=False)
    try:
        run = host.start(LiveStartRequest(
            request_id="synthetic-bheb-unmatched", question="Review a fictional outcome.",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id, limit=20)
        control = host._control(run.run_id, "agent-synthesis")["body"]
        assert len(packets) >= 7, control.get("waiting_reason")
    finally:
        host.close()


def test_missing_source_manifest_keeps_vector_result_as_lead(tmp_path, monkeypatch):
    packets = []
    selected = {}

    def model_transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        if len(packets) == 1:
            return tool("request_capability", {"tool_id": "search_bheb_corpus"}, 1)
        if len(packets) == 2:
            return tool("search_bheb_corpus", {"query": "fictional outcome"}, 2)
        if len(packets) == 3:
            result = _last_result(packet, "search_bheb_corpus")
            selected.update(hit_id=result["hits"][0]["hit_id"],
                            search_operation_id=result["operation_id"])
            return tool("request_capability", {"tool_id": "inspect_bheb_hit"}, 3)
        if len(packets) == 4:
            return tool("inspect_bheb_hit", selected, 4)
        result = _last_result(packet, "inspect_bheb_hit")
        assert result["status"] == "metadata_only"
        assert not host.store.records("source_copy")
        return answer("Only an unverified corpus lead was available.", len(packets))

    host, _, _ = _host(tmp_path, monkeypatch, source=False, model_transport=model_transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="synthetic-bheb-lead", question="Review a fictional behavioral outcome.",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id, limit=20)
        assert len(packets) >= 5
    finally:
        host.close()


def test_selected_publication_year_uses_integer_schema_and_rejects_string(
    tmp_path, monkeypatch,
):
    packets = []
    bheb_requests = []

    def model_transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        if len(packets) == 1:
            return tool("request_capability", {"tool_id": "search_bheb_corpus"}, 1)
        if len(packets) == 2:
            descriptor = next(item["function"] for item in packet["tools"]
                              if item["function"]["name"] == "search_bheb_corpus")
            year = descriptor["parameters"]["properties"]["filters"]["properties"]["publication_year"]
            assert year["type"] == "integer" and year["minimum"] == 1900
            return tool("search_bheb_corpus", {
                "query": "fictional outcome", "filters": {"publication_year": 2025},
            }, 2)
        if len(packets) == 3:
            assert _last_result(packet, "search_bheb_corpus")["status"] == "completed"
            return tool("search_bheb_corpus", {
                "query": "fictional outcome", "filters": {"publication_year": "2025"},
            }, 3)
        result = _last_result(packet, "search_bheb_corpus")
        assert result.get("status") != "completed"
        return answer("The string year was rejected.", len(packets))

    host, _, _ = _host(tmp_path, monkeypatch, source=False, model_transport=model_transport)
    original = host.bheb.transport

    def counted(*args, **kwargs):
        bheb_requests.append(kwargs["body"])
        return original(*args, **kwargs)

    host.bheb.transport = counted
    try:
        run = host.start(LiveStartRequest(
            request_id="synthetic-bheb-year", question="Search a fictional publication year.",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id, limit=20)
        assert len(bheb_requests) == 1
        assert bheb_requests[0]["filters"]["value"] == 2025
    finally:
        host.close()


def test_policy_flip_after_prepared_source_load_stops_model_dispatch(
    tmp_path, monkeypatch,
):
    packets = []
    selected = {}

    def model_transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        if len(packets) == 1:
            return tool("request_capability", {"tool_id": "search_bheb_corpus"}, 1)
        if len(packets) == 2:
            return tool("search_bheb_corpus", {"query": "fictional outcome"}, 2)
        if len(packets) == 3:
            result = _last_result(packet, "search_bheb_corpus")
            selected.update(hit_id=result["hits"][0]["hit_id"],
                            search_operation_id=result["operation_id"])
            return tool("request_capability", {"tool_id": "inspect_bheb_hit"}, 3)
        if len(packets) == 4:
            return tool("inspect_bheb_hit", selected, 4)
        assert MATCHED not in json.dumps(packet["messages"])
        return answer("The source permission changed before use.", len(packets))

    host, _, corpus_dir = _host(tmp_path, monkeypatch, source=True,
                                model_transport=model_transport)
    original_load = host.session.load
    changed = False

    def flip_after_load(intent_ref, *, for_dispatch=False):
        nonlocal changed
        prepared = original_load(intent_ref, for_dispatch=for_dispatch)
        intent = host.store.get(**intent_ref)
        if (for_dispatch and not changed
                and any(item["kind"] == "source_copy"
                        for item in intent["body"]["source_refs"])):
            policy_path = corpus_dir / "policy.json"
            policy = json.loads(policy_path.read_text(encoding="utf-8"))
            policy["revoked"] = True
            policy_path.write_text(json.dumps(policy), encoding="utf-8")
            changed = True
        return prepared

    host.session.load = flip_after_load
    try:
        run = host.start(LiveStartRequest(
            request_id="synthetic-bheb-final-policy", question="Review a fictional outcome.",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id, limit=20)
        assert changed and len(packets) >= 4
        use = host.store.records("source_use")[0]
        assert host.store.get("source_use", use["id"])["body"]["policy_status"] == "revoked"
        source_intents = [
            item for item in host.store.records("model_request_intent")
            if any(ref["kind"] == "source_copy" for ref in item["body"]["source_refs"])
        ]
        assert source_intents
        assert host.store.get(
            "model_dispatch_attempt", f"model-dispatch:{source_intents[-1]['id']}"
        ) is None
    finally:
        host.close()


def test_unconfigured_run_does_not_advertise_private_corpus(tmp_path, monkeypatch):
    monkeypatch.setattr("ora.mission.service.bheb_settings", lambda _root: {})
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-model-key")
    packets = []

    def model_transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        assert "search_bheb_corpus" not in packet["messages"][0]["content"]
        assert "inspect_bheb_hit" not in packet["messages"][0]["content"]
        assert "search_bheb_corpus" not in {
            item["function"]["name"] for item in packet["tools"]
        }
        return answer("No private corpus is configured.", len(packets))

    state_path = tmp_path / "state"
    session = ModelSession(
        StateStore(state_path), ROOT,
        adapter=GroqSelectedAdapter(ROOT, transport=model_transport),
    )
    host = MissionService(ROOT, state_path, model_session=session,
                          execution_mode="offline_test", start_supervisor=False)
    try:
        run = host.start(LiveStartRequest(
            request_id="no-bheb-config", question="Review a fictional outcome.",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id)
        assert packets
        assert host._run(run.run_id)["body"]["bheb_corpus_enabled"] is False
    finally:
        host.close()
