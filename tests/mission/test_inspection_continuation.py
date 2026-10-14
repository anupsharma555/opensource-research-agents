"""An inspected short abstract stays paged until the agent reads its checked copy."""

from __future__ import annotations

import hashlib
import json
import time
from types import SimpleNamespace

import pytest

from ora.contracts.models import LiveStartRequest
from ora.models.token_estimate import TOKENIZER_BASIS, estimate_request_tokens
from ora.retrieval.providers import HTTPResult
from tests.mission.test_service import _search_spec, answer, drain, service, tool

TAIL = " END_SYNTHETIC_ABSTRACT: the later observation limits interpretation."
PREFIX = "DOI: 10.1234/short-abstract. This is fictional source text. "
TITLE = "Fictional research article about bounded source reader examples"
FILL = " ".join(
    f"Synthetic observation {index:03d} compares records {index * 7 % 97:02d}."
    for index in range(100)
)
ABSTRACT = PREFIX + FILL[:2671 - len(PREFIX) - len(TAIL)] + TAIL


def _discovery_for(text: str):
    return HTTPResult(200, json.dumps({
        "hitCount": 1, "nextCursorMark": "*", "resultList": {"result": [{
            "source": "MED", "id": "321", "pmid": "321",
            "doi": "10.1234/short-abstract", "title": TITLE,
            "abstractText": text, "isOpenAccess": "Y",
        }]},
    }).encode(), {})


def _discovery(_method, _url, *, body=None, headers=None):
    return _discovery_for(ABSTRACT)


def _last_result(packet: dict, name: str) -> dict:
    return json.loads(next(
        message["content"] for message in reversed(packet["messages"])
        if message["role"] == "tool" and message.get("name") == name
    ))


def _source_packet(packet: dict) -> dict:
    for message in packet["messages"]:
        if message["role"] == "user":
            try:
                sources = json.loads(message["content"]).get("source_packets", [])
            except (TypeError, ValueError, AttributeError):
                continue
            if sources:
                return sources[0]
    raise AssertionError("A selected checked source packet is missing")


def test_short_abstract_option_reads_one_complete_checked_window_under_budget(
    tmp_path, monkeypatch,
):
    import ora.mission.dispatch as dispatch_module

    assert len(ABSTRACT) == 2671 and ABSTRACT.index(TAIL) > 1900
    requests = []
    option = {}

    def model(request, *, timeout):
        packet = json.loads(request.data)
        requests.append(packet)
        turn = len(requests)
        if turn == 1:
            return tool("search_scholarly", {
                "route": "europe_pmc.search", "query": "fictional short abstract", "limit": 1,
            }, turn)
        if turn == 2:
            result = _last_result(packet, "search_scholarly")
            return tool("inspect_hit", {"hit_id": result["hits"][0]["hit_id"]}, turn)
        if turn == 3:
            inspected = _last_result(packet, "inspect_hit")
            assert inspected["status"] == "inspected"
            assert inspected["preview_scope"] == "partial_retained_extracted_text"
            assert inspected["next_cursor"] == {"block_offset": 0, "char_offset": 1900}
            assert inspected["next_request"]["tool"] == "read_source_window"
            assert inspected["next_request"]["arguments"] == {
                "source_id": inspected["source_id"],
                "source_version": inspected["source_version"],
                "block_offset": 0, "char_offset": 1900,
                "char_limit": 1900, "selection_mode": "append",
            }
            assert "does not mean the source ends" in inspected["next_request"]["note"]
            option.update(inspected["complete_retained_abstract_request"])
            assert option["tool"] == "read_source_window"
            assert option["arguments"] == {
                "source_id": inspected["source_id"],
                "source_version": inspected["source_version"],
                "block_offset": 0, "char_offset": 0,
                "char_limit": 2671, "selection_mode": "replace",
            }
            assert "complete request fits" in option["note"]
            assert ABSTRACT[:1900] in _source_packet(packet)["text"]
            assert TAIL not in _source_packet(packet)["text"]
            response = tool(option["tool"], option["arguments"], turn)
            response.payload["usage"] = {
                "prompt_tokens": 5, "completion_tokens": 215, "total_tokens": 220,
            }
            response.headers = {
                "x-request-id": "offline-fixture", "x-ratelimit-limit-tokens": "8000",
            }
            return response
        assert turn == 4
        selected = _source_packet(packet)
        assert ABSTRACT in selected["text"]
        assert selected["text"].count(ABSTRACT) == 1
        assert TAIL in selected["text"]
        estimate = estimate_request_tokens(request.data)
        assert estimate.basis == TOKENIZER_BASIS
        assert estimate.tokens + 215 <= 8000
        window = _last_result(packet, "read_source_window")
        assert window["returned_chars"] == 2671
        assert window["next_cursor"] is None
        return answer("The fictional complete abstract was read.", turn)

    host = service(tmp_path, monkeypatch, model)
    host.discovery_transport = _discovery
    try:
        run = host.start(LiveStartRequest(
            request_id="complete-short-abstract", question="Read the fictional abstract",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id, limit=26)
        if len(requests) == 3:
            waits = [
                item for item in host.store.records("capacity_operation")
                if item["body"].get("kind") == "model"
                and item["body"].get("phase") == "waiting"
                and item["body"].get("wake_at")
            ]
            assert waits
            offset = max(0.0, max(item["body"]["wake_at"] for item in waits)
                         - time.time() + .01)
            host.store.clock = lambda: time.time() + offset
            monkeypatch.setattr(
                dispatch_module, "time", SimpleNamespace(time=host.store.clock),
            )
            drain(host, run.run_id, limit=26)
        assert len(requests) == 4
        assert host._run(run.run_id)["body"]["status"] == "completed"
        windows = host.store.records("source_read_window")
        assert len(windows) == 1
        full_ref = windows[0]["body"]["passage_refs"][0]
        assert host.store.get(**full_ref)["body"]["text"] == ABSTRACT
        copy_ref = windows[0]["body"]["copy_ref"]
        original_id = host.store.get(**copy_ref)["body"]["passage_ids"][0]
        assert len(host.store.get("source_passage", original_id)["body"]["text"]) == 1900
        selected = host._control(run.run_id, "agent-synthesis")["body"][
            "selected_passage_refs"
        ][copy_ref["id"]]
        assert selected == [full_ref]
    finally:
        host.close()


def test_saved_short_abstract_hint_does_not_override_revoked_use(tmp_path, monkeypatch):
    requests = []
    host = None

    def model(request, *, timeout):
        packet = json.loads(request.data)
        requests.append(packet)
        turn = len(requests)
        if turn == 1:
            return tool("search_scholarly", {
                "route": "europe_pmc.search", "query": "fictional short abstract", "limit": 1,
            }, turn)
        if turn == 2:
            result = _last_result(packet, "search_scholarly")
            return tool("inspect_hit", {"hit_id": result["hits"][0]["hit_id"]}, turn)
        if turn == 3:
            inspected = _last_result(packet, "inspect_hit")
            assert inspected["status"] == "inspected"
            selected = inspected["complete_retained_abstract_request"]
            use = host.store.resolve(
                "source_use.copy", f"{inspected['source_id']}:{inspected['source_version']}",
            )
            assert use["body"]["policy_status"] == "granted"

            def revoke(tx):
                current = tx.get(**{key: use[key] for key in ("kind", "id", "version")})
                tx.put("source_use", use["id"], {
                    **current["body"], "policy_status": "revoked", "allowed_uses": [],
                }, expected_version=use["version"])
                return {"revoked": True}

            host.store.transact("revoke-before-short-abstract-read", {}, revoke,
                                mission_id=host._run(run.run_id)["body"]["mission_id"])
            return tool(selected["tool"], selected["arguments"], turn)
        return answer("The source is now unavailable for model input.", turn)

    host = service(tmp_path, monkeypatch, model)
    host.discovery_transport = _discovery
    try:
        run = host.start(LiveStartRequest(
            request_id="revoked-short-abstract", question="Read only permitted text",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id, limit=26)
        assert len(requests) >= 3
        assert host.store.records("source_read_window") == []
        results = []
        for row in host.store.records("context_tool_result"):
            message = json.loads(host.store.read_blob(row["body"]["blob"]["sha256"]))
            if message.get("name") == "read_source_window":
                results.append(json.loads(message["content"]))
        assert len(results) == 1
        assert results[0]["status"] in {"rejected", "source_unavailable"}
        assert "Source policy does not grant model input use" in results[0]["reason"]
        assert len(requests) == 4
        assert ABSTRACT[:1900] not in json.dumps(requests[3])
        assert TAIL not in json.dumps(requests[3])
    finally:
        host.close()


@pytest.mark.parametrize("length", [520, 16001])
def test_read_hint_does_not_claim_whole_abstract_for_already_read_or_oversized_copy(
    tmp_path, monkeypatch, length,
):
    text = ("Fictional abstract observations. " * (length // 31 + 2))[:length]
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    host.discovery_transport = lambda *_args, **_kwargs: _discovery_for(text)
    try:
        run = host.start(LiveStartRequest(
            request_id=f"source-reader-boundary-{length}",
            question="Inspect fictional source text", agent_ids=["agent-synthesis"],
        ))
        spec = _search_spec(host, run.run_id, suffix=f"boundary-{length}")
        found, _ = host._search(spec, {
            "route": "europe_pmc.search", "query": "fictional source", "limit": 1,
        })
        inspected, refs = host._inspect_hit(spec, {"hit_id": found["hits"][0]["hit_id"]})
        assert inspected["status"] == "inspected" and refs
        assert "complete_retained_abstract_request" not in inspected
        if length == 520:
            assert inspected["preview_scope"] == "complete_retained_extracted_text"
            assert inspected["next_cursor"] is None
            assert "next_request" not in inspected
        else:
            assert inspected["preview_scope"] == "partial_retained_extracted_text"
            assert inspected["next_cursor"] == {"block_offset": 0, "char_offset": 1900}
            assert inspected["next_request"]["tool"] == "read_source_window"
    finally:
        host.close()


def test_skipped_prefix_last_block_is_not_claimed_as_complete_source(tmp_path, monkeypatch):
    long_block = "The fictional observation continues. " * 8
    html = (
        f"<html><title>{TITLE}</title><main><h1>{TITLE}</h1>"
        "<p>DOI: 10.1234/short-abstract</p><h2>Abstract</h2>"
        "<p>Brief lead.</p><p>" + long_block + "</p></main></html>"
    ).encode()
    url = "https://example.org/fictional-abstract"
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    host.discovery_transport = _discovery
    try:
        run = host.start(LiveStartRequest(
            request_id="source-reader-skipped-prefix", question="Inspect fictional HTML",
            agent_ids=["agent-synthesis"],
        ))
        spec = _search_spec(host, run.run_id, suffix="skipped-prefix")
        found, _ = host._search(spec, {
            "route": "europe_pmc.search", "query": "fictional source", "limit": 1,
        })
        inspected, refs = host._inspect_hit(
            spec, {"hit_id": found["hits"][0]["hit_id"]}, fetched={
                "status": 200, "content": html,
                "content_sha256": hashlib.sha256(html).hexdigest(),
                "byte_length": len(html), "content_type": "text/html",
                "requested_url": url, "final_url": url, "redirect_chain": [],
                "access_basis": "public_https_unauthenticated", "authenticated": False,
                "retrieved_at": "2026-10-13T00:00:00Z",
            },
        )
        assert inspected["status"] == "inspected" and refs
        assert inspected["classification"] == "abstract"
        assert inspected["preview"]["block_offset"] > 0
        assert inspected["next_cursor"] is None
        assert inspected["preview_scope"] == "partial_retained_extracted_text"
        assert "complete_retained_abstract_request" not in inspected
    finally:
        host.close()
