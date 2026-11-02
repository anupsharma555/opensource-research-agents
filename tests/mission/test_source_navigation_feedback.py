"""Repeated saved-source actions report real progress in serialized tool replies."""

import hashlib
import json

import pytest

from ora.contracts.models import LiveStartRequest
from ora.models.token_estimate import estimate_context_tokens, estimate_request_tokens
from ora.retrieval.providers import HTTPResult
from tests.mission.test_service import answer, drain, service, tool


@pytest.mark.parametrize("legacy_window_result", [False, True])
def test_repeated_html_read_and_fetch_report_no_new_work(
    tmp_path, monkeypatch, legacy_window_result,
):
    html = (b"<html><head><title>Synthetic navigation example</title></head><body>"
            b"<h1>Intro!</h1><p>Short opening.</p><h2>Measurements</h2>"
            b"<p>The synthetic value is 17 units.</p></body></html>")
    fetched_urls = []
    packets = []
    request_tokens = []
    chosen = {}

    def fetch(url):
        fetched_urls.append(url)
        return {
            "status": 200, "content": html,
            "content_sha256": hashlib.sha256(html).hexdigest(),
            "byte_length": len(html), "content_type": "text/html",
            "requested_url": url, "final_url": url, "redirect_chain": [],
            "access_basis": "public_https_unauthenticated", "authenticated": False,
            "retrieved_at": "2026-10-15T00:00:00Z",
        }

    monkeypatch.setattr("ora.mission.service.fetch_public_source", fetch)
    monkeypatch.setenv("EXA_API_KEY", "synthetic-search-key")
    if legacy_window_result:
        from ora.mission import service as service_module

        original_read = service_module.read_source_window

        def read_without_new_metadata(*args, **kwargs):
            return {key: value for key, value in original_read(*args, **kwargs).items()
                    if key != "navigation"}

        monkeypatch.setattr(service_module, "read_source_window", read_without_new_metadata)

    def latest(packet, name):
        matches = [json.loads(item["content"]) for item in packet["messages"]
                   if item.get("role") == "tool" and item.get("name") == name]
        return matches[-1]

    def model(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        request_tokens.append(estimate_request_tokens(request.data).tokens)
        step = len(packets)
        if step == 1:
            return tool("search_scholarly", {
                "route": "exa.search", "query": "synthetic navigation example", "limit": 1,
            }, step)
        if step == 2:
            chosen["hit_id"] = latest(packet, "search_scholarly")["hits"][0]["hit_id"]
            return tool("request_capability", {"tool_id": "fetch_source"}, step)
        if step == 3:
            return tool("fetch_source", {"hit_id": chosen["hit_id"]}, step)
        if step == 4:
            copy = host.store.records("source_copy")[0]
            chosen["copy_ref"] = {key: copy[key] for key in ("kind", "id", "version")}
            return tool("request_capability", {"tool_id": "read_source_window"}, step)
        if step == 5:
            return tool("read_source_window", {
                "source_id": chosen["copy_ref"]["id"],
                "source_version": chosen["copy_ref"]["version"],
                "block_offset": 0, "char_offset": 0, "char_limit": 2000,
            }, step)
        if step == 6:
            first_read = latest(packet, "read_source_window")
            assert first_read["navigation"]["scope"] == "navigation_only_not_checked_claim_evidence"
            if legacy_window_result:
                assert first_read["navigation"]["sections_unavailable_reason"] == (
                    "legacy_window_result_without_outline"
                )
            else:
                assert first_read["navigation"]["next_section_request"]["block_offset"] == 2
            assert first_read["progress_feedback"]["new_checked_location_chars"] == 6
            return tool("read_source_window", {
                "source_id": chosen["copy_ref"]["id"],
                "source_version": chosen["copy_ref"]["version"],
                "block_offset": 0, "char_offset": 0, "char_limit": 2000,
            }, step)
        if step == 7:
            repeated = latest(packet, "read_source_window")
            assert repeated["progress_feedback"]["exact_window_repeated"] is True
            assert repeated["progress_feedback"]["new_checked_location_chars"] == 0
            return tool("fetch_source", {"hit_id": chosen["hit_id"]}, step)
        assert step == 8
        reused = latest(packet, "fetch_source")
        assert reused["fetch_feedback"]["new_http_request"] is False
        assert reused["fetch_feedback"]["latest_read"] is not None
        return answer("The retained source is available for further reading.", step)

    host = service(tmp_path, monkeypatch, model)
    host.discovery_transport = lambda *_args, **_kwargs: HTTPResult(200, json.dumps({
        "results": [{"id": "synthetic-navigation", "url": "https://example.org/synthetic",
                     "title": "Synthetic navigation example"}],
    }).encode(), {})
    try:
        run = host.start(LiveStartRequest(
            request_id="source-feedback", question="Inspect the synthetic example.",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id, limit=30)
        assert len(packets) == 8
        source_tool_names = {"fetch_source", "read_source_window"}
        assert all(sum(message.get("role") == "tool"
                       and message.get("name") in source_tool_names
                       for message in packet["messages"]) == 1
                   for packet in packets[5:])
        assert packets[5]["tools"] == packets[6]["tools"] == packets[7]["tools"]
        repeated_reply = next(message for message in packets[6]["messages"]
                              if message.get("role") == "tool"
                              and message.get("name") == "read_source_window")
        # Repeating the same window adds less context than replaying its full reply.
        assert request_tokens[6] - request_tokens[5] < estimate_context_tokens(
            repeated_reply["content"]
        )
        assert request_tokens[7] <= request_tokens[6]
        assert fetched_urls == ["https://example.org/synthetic"]
        replies = []
        for record in host.store.records("context_tool_result"):
            item = json.loads(host.store.read_blob(record["body"]["blob"]["sha256"]))
            if item.get("name") in {"fetch_source", "read_source_window"}:
                replies.append((item["name"], json.loads(item["content"])))
        fetches = [result for name, result in replies if name == "fetch_source"]
        reads = [result for name, result in replies if name == "read_source_window"]
        assert len(fetches) == len(reads) == 2
        first_fetch = next(item for item in fetches if item["fetch_feedback"]["new_http_request"])
        reused_fetch = next(item for item in fetches if not item["fetch_feedback"]["new_http_request"])
        first_read = next(item for item in reads if not item["progress_feedback"]["exact_window_repeated"])
        repeated_read = next(item for item in reads if item["progress_feedback"]["exact_window_repeated"])
        assert first_fetch["copy_ref"] == reused_fetch["copy_ref"]
        assert reused_fetch["fetch_feedback"]["retained_copy_reused"] is True
        assert reused_fetch["fetch_feedback"]["latest_read"]["window_ref"] == repeated_read["window_ref"]
        assert first_read["returned_chars"] == 6
        if legacy_window_result:
            assert first_read["navigation"]["sections_unavailable_reason"] == (
                "legacy_window_result_without_outline"
            )
        else:
            assert first_read["navigation"]["next_section_request"]["block_offset"] == 2
        assert first_read["navigation"]["scope"] == "navigation_only_not_checked_claim_evidence"
        assert repeated_read["progress_feedback"]["new_checked_location_chars"] == 0
        assert repeated_read["next_request"]["block_offset"] == 1
        assert len(host.store.records("source_copy")) == 1
        assert len(host.store.records("source_passage")) == 1
    finally:
        host.close()
