"""Current eligible scholarly hits expose fetching without bypassing source permissions."""

from __future__ import annotations

import json

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.mission.service import LocalToolRejection
from ora.mission.shared_materials import shared_hit_access
from ora.retrieval.providers import HTTPResult
from tests.mission.availability_assertions import request_first_names
from tests.mission.test_service import _search_spec, drain, service, tool


def _host(tmp_path, monkeypatch):
    def no_model(*_args, **_kwargs):
        raise AssertionError("Only frozen-replay test may dispatch an injected model")

    return service(tmp_path, monkeypatch, no_model)


def _start(host, name):
    return host.start(LiveStartRequest(
        request_id=name, question="Inspect a synthetic scholarly hit",
        agent_ids=["agent-synthesis"],
    ))


def _metadata_hit(host, run_id):
    host.discovery_transport = lambda *_args, **_kwargs: HTTPResult(
        200, json.dumps({"message": {"items": [{
            "DOI": "10.1234/synthetic.metadata", "title": ["Synthetic metadata only"],
        }], "total-results": 1}}).encode(), {},
    )
    found, _ = host._search(_search_spec(host, run_id, suffix="metadata"), {
        "route": "crossref.search", "query": "synthetic metadata", "limit": 1,
    })
    assert len(found["hits"]) == 1
    return found["hits"][0]["hit_id"]


def _abstract_hit(host, run_id):
    host.discovery_transport = lambda *_args, **_kwargs: HTTPResult(
        200, json.dumps({
            "hitCount": 1, "nextCursorMark": "*", "resultList": {"result": [{
                "source": "MED", "id": "123", "pmid": "123",
                "title": "Synthetic checked abstract",
                "abstractText": "Original synthetic evidence, with no private data. " * 5,
            }]},
        }).encode(), {},
    )
    found, _ = host._search(_search_spec(host, run_id, suffix="abstract"), {
        "route": "europe_pmc.search", "query": "synthetic", "limit": 1,
    })
    assert len(found["hits"]) == 1
    return found["hits"][0]["hit_id"]


def _serialized_request(host, run_id):
    control = host._control(run_id, "agent-synthesis")
    assert host._schedule_model(control)
    call = host.store.get(**host._control(run_id, "agent-synthesis")["body"]["call_ref"])
    intent = host.store.get("model_request_intent", f"model-intent:{call['id']}:{call['version']}")
    raw = host.store.read_blob(intent["body"]["blob"]["sha256"])
    return json.loads(raw), raw, call, intent


def _names(packet):
    return {item["function"]["name"] for item in packet["tools"]}


def test_fresh_crossref_metadata_attaches_fetch_beside_inspect_without_fetching(
    tmp_path, monkeypatch,
):
    host = _host(tmp_path, monkeypatch)
    try:
        run = _start(host, "fresh-metadata-fetchable")
        hit_id = _metadata_hit(host, run.run_id)
        hit = host.store.get("retrieval_hit", hit_id)
        assert hit["body"]["source"].get("content") is None
        assert shared_hit_access(host.store, hit)
        packet, _, call, _ = _serialized_request(host, run.run_id)
        assert {"inspect_hit", "fetch_source"} <= _names(packet)
        assert host._run(run.run_id)["body"]["fetch_source_autoattach_version"] == 1
        assert {"inspect_hit", "fetch_source"} <= set(call["body"]["exposed_tools"])
        availability = next(json.loads(message["content"]) for message in packet["messages"]
                            if message.get("role") == "user"
                            and "trusted_host_attached_tool_availability" in message["content"])
        assert "fetch_source" in availability["call_now_local_functions"]
        assert "fetch_source" not in request_first_names(packet, availability)
        assert host.store.records("public_document_fetch") == []
        assert host.store.records("source_copy") == []
    finally:
        host.close()


def test_no_hit_and_missing_grant_do_not_advertise_fetch(tmp_path, monkeypatch):
    no_hit = _host(tmp_path / "no-hit", monkeypatch)
    try:
        run = _start(no_hit, "no-hit")
        packet, _, _, _ = _serialized_request(no_hit, run.run_id)
        assert "fetch_source" not in _names(packet)
        assert "inspect_hit" not in _names(packet)
    finally:
        no_hit.close()

    missing = _host(tmp_path / "no-grant", monkeypatch)
    try:
        run = _start(missing, "no-fetch-grant")
        _metadata_hit(missing, run.run_id)
        control = missing._control(run.run_id, "agent-synthesis")
        catalog, _ = missing._tool_registry(control)
        catalog.tools.pop("fetch_source")
        packet, _, call, _ = _serialized_request(missing, run.run_id)
        assert "inspect_hit" in _names(packet)
        assert "fetch_source" not in _names(packet)
        assert "fetch_source" not in call["body"]["permitted_tools"]
    finally:
        missing.close()


@pytest.mark.parametrize(("field", "value"), [
    ("origin", "provider_summary"),
    ("rights_status", "restricted"),
    ("rights_status", "not_retained"),
])
def test_nonoriginal_or_restricted_hit_is_not_fetchable(
    tmp_path, monkeypatch, field, value,
):
    host = _host(tmp_path, monkeypatch)
    try:
        run = _start(host, f"unfetchable-{field}-{value}")
        hit_id = _metadata_hit(host, run.run_id)
        saved = host.store.get("retrieval_hit", hit_id)
        source = {**saved["body"]["source"], field: value}
        host.store.transact(f"mark-{field}-{value}", {}, lambda tx: tx.put(
            "retrieval_hit", hit_id, {**saved["body"], "source": source},
            expected_version=saved["version"],
        ), mission_id=host._run(run.run_id)["body"]["mission_id"])
        packet, _, _, _ = _serialized_request(host, run.run_id)
        assert "fetch_source" not in _names(packet)

        def no_public_fetch(*_args, **_kwargs):
            raise AssertionError("Ineligible hit must fail before public HTTP")

        monkeypatch.setattr("ora.mission.service.fetch_public_source", no_public_fetch)
        with pytest.raises(LocalToolRejection, match="no longer permits"):
            host._fetch_source(_search_spec(host, run.run_id, suffix="blocked-fetch"),
                               {"hit_id": hit_id})
    finally:
        host.close()


def test_superseded_hit_cannot_justify_autoattachment(tmp_path, monkeypatch):
    host = _host(tmp_path, monkeypatch)
    try:
        run = _start(host, "superseded-scholarly-hit")
        hit_id = _metadata_hit(host, run.run_id)
        old = host.store.get("retrieval_hit", hit_id)
        host.store.transact("supersede-scholarly-hit", {}, lambda tx: tx.put(
            "retrieval_hit", hit_id, old["body"], expected_version=old["version"],
        ), mission_id=host._run(run.run_id)["body"]["mission_id"])
        control = host._control(run.run_id, "agent-synthesis")
        heads = host._heads

        def stale_page(kind, mission_id=None):
            return [old] if kind == "retrieval_hit" else heads(kind, mission_id)

        monkeypatch.setattr(host, "_heads", stale_page)
        assert not host._has_fetchable_scholarly_hit(control)
        packet, _, _, _ = _serialized_request(host, run.run_id)
        assert "fetch_source" not in _names(packet)
    finally:
        host.close()


def test_revoked_hit_does_not_attach_or_fetch_before_current_use_check(
    tmp_path, monkeypatch,
):
    host = _host(tmp_path, monkeypatch)
    try:
        run = _start(host, "revoked-scholarly-hit")
        hit_id = _abstract_hit(host, run.run_id)
        inspected, _ = host._inspect_hit(
            _search_spec(host, run.run_id, suffix="inspect"), {"hit_id": hit_id},
        )
        use = host.store.resolve(
            "source_use.copy", f"{inspected['source_id']}:{inspected['source_version']}",
        )
        assert use["body"]["policy_status"] == "granted"

        def revoke(tx):
            current = tx.get("source_use", use["id"])
            return ref(tx.put("source_use", current["id"], {
                **current["body"], "policy_status": "revoked", "allowed_uses": [],
            }, expected_version=current["version"]))

        host.store.transact("revoke-fetch-hit", {}, revoke,
                            mission_id=host._run(run.run_id)["body"]["mission_id"])
        assert not shared_hit_access(host.store, host.store.get("retrieval_hit", hit_id))
        packet, _, _, _ = _serialized_request(host, run.run_id)
        assert "fetch_source" not in _names(packet)
        assert "inspect_hit" not in _names(packet)

        def no_public_fetch(*_args, **_kwargs):
            raise AssertionError("A revoked hit must fail before public HTTP")

        monkeypatch.setattr("ora.mission.service.fetch_public_source", no_public_fetch)
        with pytest.raises(LocalToolRejection, match="no longer permits"):
            host._fetch_source(_search_spec(host, run.run_id, suffix="fetch"),
                               {"hit_id": hit_id})
    finally:
        host.close()


def test_historical_run_replays_exact_prepared_request_after_reopen(tmp_path, monkeypatch):
    host = _host(tmp_path, monkeypatch)
    try:
        run = _start(host, "historical-fetch-attachment")
        saved_run = host._run(run.run_id)
        old_body = {key: value for key, value in saved_run["body"].items()
                    if key != "fetch_source_autoattach_version"}
        host.store.transact("historical-run-without-new-pin", {}, lambda tx: tx.put(
            "mission_run", saved_run["id"], old_body,
            expected_version=saved_run["version"],
        ), mission_id=saved_run["body"]["mission_id"])
        _metadata_hit(host, run.run_id)
        packet, frozen, call, intent = _serialized_request(host, run.run_id)
        assert "inspect_hit" in _names(packet)
        assert "fetch_source" not in _names(packet)
        frozen_sha = intent["body"]["request_sha256"]
        assert intent["body"]["blob"]["sha256"] == frozen_sha
    finally:
        host.close()

    sent = []

    def injected_model(request, *, timeout):
        sent.append(request.data)
        assert request.data == frozen
        return tool("publish_result", {
            "title": "Synthetic answer", "text": "Only metadata was saved.",
            "citations": [], "limitations": ["No checked source text"],
        }, 1)

    reopened = service(tmp_path, monkeypatch, injected_model)
    try:
        drain(reopened, run.run_id, limit=20)
        assert len(sent) == 1
        assert reopened.store.read_blob(intent["body"]["blob"]["sha256"]) == frozen
        assert reopened.store.get("model_request_intent", intent["id"], 1)["body"][
            "request_sha256"] == frozen_sha
        assert reopened._run(run.run_id)["body"].get("fetch_source_autoattach_version") is None
    finally:
        reopened.close()
