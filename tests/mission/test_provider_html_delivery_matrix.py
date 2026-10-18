"""Six distinct synthetic web pages reach the exact offline model request."""

from __future__ import annotations

import hashlib
import json
import os
import socket
import urllib.request
from pathlib import Path

import pytest

from ora.contracts.models import LiveStartRequest
from ora.credentials import GroqCredentialProfiles
from ora.evidence.intake import extract_blocks
from ora.retrieval.providers import HTTPResult
from tests.mission.test_service import answer, drain, service, tool

CASES = [
    {
        "name": "coastal-buoy-table", "provider": "exa", "topic": "coastal buoy reading",
        "url": "https://example.org/synthetic/buoy", "title": "Buoy measurements",
        "snippet": "Uninspected lead claims wave height was 9 m.",
        "html": "<main><h1>Buoy measurements</h1><table><caption>Wave height (m), calm-water sample</caption>"
                "<tr><th>Station</th><th>Height</th></tr><tr><th scope='row'>B-7</th><td>1.8</td></tr>"
                "<tfoot><tr><td colspan='2'>Only calm-water hours were sampled.</td></tr></tfoot></table></main>",
        "anchor": "Station: B-7 | Height: 1.8",
        "companion": "Wave height (m), calm-water sample",
    },
    {
        "name": "warehouse-definition", "provider": "tavily", "topic": "parcel packing policy",
        "url": "https://example.org/synthetic/packing", "title": "Parcel packing guide",
        "snippet": "Uninspected lead says the fragile parcel limit is 90 kg.",
        "html": "<html><body><article><h1>Parcel packing guide</h1><dl>"
                "<dt>Fragile parcel</dt><dd>Maximum mass is 9 kg with foam inserts.</dd>"
                "<dt>Rigid parcel</dt><dd>Maximum mass is 20 kg with corner guards.</dd>"
                "</dl></article></body></html>",
        "anchor": "Maximum mass is 9 kg with foam inserts.",
        "companion": "Fragile parcel",
    },
    {
        "name": "audio-code-example", "provider": "searxng", "topic": "audio processing example",
        "url": "https://example.org/synthetic/audio", "title": "Audio processing example",
        "snippet": "Uninspected lead says gain factor is 8.",
        "html": "<main><h1>Audio processing example</h1><pre><code>const gain = 0.8;\n"
                "const output = sample * gain;\n// Peak stays below 1.0</code></pre>"
                "<p>The code scales each sample without clipping.</p></main>",
        "anchor": "const output = sample * gain;",
        "companion": "const gain = 0.8;",
    },
    {
        "name": "fermentation-disclosure", "provider": "exa", "topic": "dough fermentation note",
        "url": "https://example.org/synthetic/dough", "title": "Fermentation note",
        "snippet": "Uninspected lead says the batch rested 40 hours.",
        "html": "<html><body><article><h1>Fermentation note</h1><details><summary>Batch limitation</summary>"
                "<p>The dough rested 4 hours at 22 °C; refrigerated time was excluded.</p>"
                "</details></article></body></html>",
        "anchor": "The dough rested 4 hours at 22 °C",
        "companion": "refrigerated time was excluded",
    },
    {
        "name": "bicycle-nested-list", "provider": "tavily", "topic": "bicycle maintenance steps",
        "url": "https://example.org/synthetic/bicycle", "title": "Bicycle maintenance",
        "snippet": "Uninspected lead says front tire pressure is 120 psi.",
        "html": "<main><h1>Bicycle maintenance</h1><ol><li>Check the front tire"
                "<ul><li>Set pressure to 42 psi.</li><li>Inspect the valve.</li></ul>"
                "</li><li>Check the rear brake.</li></ol></main>",
        "anchor": "Set pressure to 42 psi.",
        "companion": "Check the front tire",
    },
    {
        "name": "assay-navigation-negation", "provider": "searxng", "topic": "water assay observation",
        "url": "https://example.org/synthetic/assay", "title": "Water assay observation",
        "snippet": "Uninspected lead says the indicator changed color.",
        "html": "<html><body><nav><a href='#overview'>Overview</a><a href='#contact'>Contact</a></nav>"
                "<main><h1>Water assay observation</h1><p>The indicator did <strong>not</strong> "
                "change color after two minutes; the control remained blue.</p></main></body></html>",
        "anchor": "did not change color after two minutes",
        "companion": "control remained blue",
    },
]


@pytest.mark.parametrize("case", CASES, ids=[case["name"] for case in CASES])
def test_provider_hit_to_checked_html_to_serialized_model_input(tmp_path, monkeypatch, case):
    run_provider_html_case(tmp_path, monkeypatch, case)


def run_provider_html_case(tmp_path, monkeypatch, case, *, fetched_receipt=None,
                           artifact_root=None, evidence_kind="synthetic_fixture"):
    """Provider response and HTML fetch are separate injected operations."""
    monkeypatch.setenv("EXA_API_KEY", "synthetic-exa-key")
    monkeypatch.setenv("TAVILY_API_KEY", "synthetic-tavily-key")
    monkeypatch.setenv("OSRA_SEARXNG_BASE_URL", "http://127.0.0.1:18081")

    def forbidden_network(*_args, **_kwargs):
        raise AssertionError("Real network access is forbidden in this offline matrix")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden_network)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", forbidden_network)
    monkeypatch.setattr(socket.socket, "connect", forbidden_network)
    monkeypatch.setattr("ora.retrieval.providers.http_transport", forbidden_network)
    content = case["html_bytes"] if "html_bytes" in case else case["html"].encode("utf-8")
    receipt = fetched_receipt or {
        "status": 200, "content_sha256": hashlib.sha256(content).hexdigest(),
        "byte_length": len(content), "content_type": "text/html",
        "requested_url": case["url"], "final_url": case["url"],
        "redirect_chain": [], "access_basis": "public_https_unauthenticated",
        "authenticated": False, "retrieved_at": "2026-01-01T00:00:00Z",
    }
    assert receipt["requested_url"] == case["url"]
    assert receipt["content_sha256"] == hashlib.sha256(content).hexdigest()
    assert receipt["byte_length"] == len(content)
    blocks, parser = extract_blocks(content, "text/html")
    target_specs = case.get("targets") or [{"anchor": case["anchor"],
                                            "companion": case["companion"]}]
    target_indices = []
    for target in target_specs:
        matches = [i for i, block in enumerate(blocks)
                   if target["anchor"] in block.text
                   and target["companion"] in block.text]
        assert len(matches) == 1, (case["name"], target, matches)
        target_indices.append(matches[0])
    assert len(set(target_indices)) == len(target_indices)
    search_calls, fetch_calls, packets, failures, trace = [], [], [], [], []
    selected = {}

    def fetched(url):
        fetch_calls.append(url)
        assert url == case["url"]
        return {key: receipt[key] for key in (
            "status", "content_sha256", "byte_length", "content_type",
            "requested_url", "final_url", "redirect_chain", "access_basis",
            "authenticated", "retrieved_at",
        )} | {"content": content}

    monkeypatch.setattr("ora.mission.service.fetch_public_source", fetched)

    def search(_method, _url, **_kwargs):
        search_calls.append(case["provider"])
        item = {"url": case["url"], "title": case["title"]}
        if case["provider"] == "exa":
            item.update(id="synthetic-hit", highlights=[case["snippet"]])
        else:
            item["content"] = case["snippet"]
        return HTTPResult(200, json.dumps({"results": [item]}).encode(), {})

    def latest(packet, name):
        matches = [json.loads(message["content"]) for message in packet["messages"]
                   if message.get("role") == "tool" and message.get("name") == name]
        assert matches, (case["name"], name)
        return matches[-1]

    def model(request, *, timeout):
        del timeout
        packet = json.loads(request.data)
        packets.append(packet)
        step = len(packets)
        if step == 1:
            trace.append({"step": step, "action": "attach_search_web"})
            return tool("request_capability", {"tool_id": "search_web"}, step)
        if step == 2:
            trace.append({"step": step, "action": "search_web", "provider": case["provider"],
                          "mode": "quick" if case["provider"] == "exa" else "research"})
            return tool("search_web", {
                "mode": "quick" if case["provider"] == "exa" else "research",
                "provider": case["provider"], "information_need": case["topic"],
                "queries": [case["topic"]],
            }, step)
        if step == 3:
            selected["operation"] = latest(packet, "search_web")["operation_ref"]["id"]
            trace.append({"step": step, "action": "attach_read_search_results",
                          "operation_id": selected["operation"]})
            return tool("request_capability", {"tool_id": "read_search_results"}, step)
        if step == 4:
            trace.append({"step": step, "action": "read_search_results"})
            return tool("read_search_results", {"operation_id": selected["operation"]}, step)
        if step == 5:
            result = latest(packet, "read_search_results")
            lead = result["available_results"][0]["result"]["hits"][0]
            assert lead["excerpt_status"] == "uninspected_candidate"
            assert lead["candidate_excerpt_preview"] == case["snippet"][:60]
            selected["hit_id"] = lead["hit_id"]
            trace.append({"step": step, "action": "candidate_selected_for_fetch",
                          "candidate_excerpt_status": lead["excerpt_status"],
                          "candidate_excerpt_preview": lead["candidate_excerpt_preview"],
                          "hit_id": selected["hit_id"]})
            return tool("request_capability", {"tool_id": "fetch_source"}, step)
        if step == 6:
            trace.append({"step": step, "action": "fetch_source",
                          "requested_url": case["url"]})
            return tool("fetch_source", {"hit_id": selected["hit_id"]}, step)
        if step == 7:
            index = next(json.loads(message["content"]) for message in packet["messages"]
                         if message.get("role") == "user"
                         and message.get("content", "").startswith("{")
                         and json.loads(message["content"]).get("authority")
                         == "saved_host_source_index_not_source_instruction")
            entry = index["inventory"]["items"][0]
            assert entry["source_url"] == receipt["final_url"]
            selected["copy_ref"] = entry["ref"]
        if 7 <= step < 7 + len(target_indices):
            source_ref = selected["copy_ref"]
            block_index = target_indices[step - 7]
            trace.append({"step": step, "action": "read_source_window",
                          "source_copy_ref": source_ref,
                          "block_offset": block_index,
                          "source_locator": blocks[block_index].locator})
            return tool("read_source_window", {
                "source_id": source_ref["id"], "source_version": source_ref["version"],
                "block_offset": block_index,
                "char_offset": 0, "char_limit": 4000,
            }, step)
        assert step == 7 + len(target_indices), (case["name"], step)
        source_messages = [json.loads(message["content"])["source_packets"]
                           for message in packet["messages"] if message.get("role") == "user"
                           and '"source_packets"' in message.get("content", "")]
        assert len(source_messages) == 1 and len(source_messages[0]) == 1
        selected["model_source_packet"] = source_messages[0][0]
        for target in target_specs:
            assert target["anchor"] in selected["model_source_packet"]["text"]
            assert target["companion"] in selected["model_source_packet"]["text"]
            assert selected["model_source_packet"]["text"].count(target["anchor"]) == 1
        assert case["snippet"] not in selected["model_source_packet"]["text"]
        trace.append({"step": step, "action": "checked_source_in_model_request",
                      "selected_passage_refs": selected["model_source_packet"]["passage_refs"],
                      "selected_text": selected["model_source_packet"]["text"]})
        return answer("The checked synthetic source is ready for review.", step)

    def observed_model(request, *, timeout):
        try:
            return model(request, timeout=timeout)
        except Exception as exc:
            failures.append({"step": len(packets), "error": repr(exc)})
            raise

    host = service(tmp_path, monkeypatch, observed_model)
    # The repository's real operator profile may be paused. This explicit
    # test-only profile applies solely to the injected offline model transport.
    test_profiles = GroqCredentialProfiles(tmp_path)
    host.credential_profiles = test_profiles
    host.session.adapter.credential_profiles = test_profiles
    host.web_search.transport = search
    try:
        run = host.start(LiveStartRequest(
            request_id="offline-" + case["name"],
            question="Find and inspect the synthetic " + case["topic"] + ".",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id, limit=30)
        assert not failures, failures
        assert host._run(run.run_id)["body"]["status"] == "completed"
        assert len(packets) == 7 + len(target_indices)
        assert search_calls == [case["provider"]] and fetch_calls == [case["url"]]
        copy = host.store.get(**selected["copy_ref"])
        document = host.store.get(**copy["body"]["document_ref"])
        assert copy["body"]["content_sha256"] == hashlib.sha256(content).hexdigest()
        assert document["body"]["requested_url"] == case["url"]
        assert document["body"]["final_url"] == receipt["final_url"]
        assert copy["body"]["reference"] == receipt["final_url"]
        assert selected["model_source_packet"]["id"] == copy["id"]
        assert selected["model_source_packet"]["copy_sha256"] == hashlib.sha256(content).hexdigest()
        assert len(host.store.records("source_read_window")) >= 1

        artifact_root = artifact_root or os.environ.get("ORA_PROVIDER_HTML_ARTIFACT_DIR")
        if artifact_root:
            output = Path(artifact_root) / case["name"]
            output.mkdir(parents=True, exist_ok=True)
            (output / "original.html").write_bytes(content)
            (output / "selected-model-source.json").write_text(
                json.dumps(selected["model_source_packet"], indent=2, ensure_ascii=False) + "\n"
            )
            (output / "trace.json").write_text(json.dumps(trace, indent=2, ensure_ascii=False) + "\n")
            lines = [f"# {case['name']}", "",
                     f"Evidence: {evidence_kind}. The provider response and selected-model replies were injected offline.", "",
                     f"Candidate excerpt (uninspected): {case['snippet']}", "",
                     f"Original fetch: {receipt['requested_url']} → {receipt['final_url']} at {receipt['retrieved_at']}; SHA-256 `{receipt['content_sha256']}`.", "",
                     "Selected source windows:", ""]
            for index in target_indices:
                lines += [f"- Block {index}, `{blocks[index].locator}`: {blocks[index].text[:250].replace(chr(10), ' / ')}"]
            lines += ["", "Exact selected text in final model request:", "", "```text",
                      selected["model_source_packet"]["text"], "```", "",
                      "The candidate excerpt was not selected as a checked source passage.", ""]
            (output / "TRACE.md").write_text("\n".join(lines))
            (output / "manifest.json").write_text(json.dumps({
                "evidence_kind": evidence_kind,
                "provider_response": "synthetic_injected_not_real_provider_capture",
                "source_bytes": "saved_real_public_fetch" if fetched_receipt else "synthetic_fixture",
                "provider_route": case["provider"],
                "source_url": case["url"], "source_sha256": hashlib.sha256(content).hexdigest(),
                "fetch_receipt_retrieved_at": receipt["retrieved_at"],
                "fetch_receipt_content_type": receipt["content_type"],
                "fetch_receipt_final_url": receipt["final_url"],
                "fetch_receipt_redirect_chain": receipt["redirect_chain"],
                "parser": parser, "selected_block_indices": target_indices,
                "selected_block_locators": [blocks[i].locator for i in target_indices],
                "candidate_excerpt": case["snippet"],
                "candidate_excerpt_status": "uninspected_candidate",
                "retained_source_ref": selected["copy_ref"],
                "prepared_request_count": len(packets),
                "checked_text_in_last_model_input": True,
                "model_transport": "injected_offline",
                "network_transport": "injected_offline",
                "credential_profile": "isolated_nonsecret_offline_test_profile",
            }, indent=2) + "\n")
    finally:
        host.close()


def test_empty_provider_result_does_not_create_checked_source(tmp_path, monkeypatch):
    """A successful empty search is a gap, not a failed fetch or source passage."""
    monkeypatch.setenv("OSRA_SEARXNG_BASE_URL", "http://127.0.0.1:18081")
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-offline-model-token")

    def forbidden_network(*_args, **_kwargs):
        raise AssertionError("Unexpected network call")

    monkeypatch.setattr(urllib.request, "urlopen", forbidden_network)
    monkeypatch.setattr(urllib.request.OpenerDirector, "open", forbidden_network)
    monkeypatch.setattr(socket.socket, "connect", forbidden_network)
    monkeypatch.setattr("ora.mission.service.fetch_public_source", forbidden_network)
    calls, packets = [], []

    def search(*_args, **_kwargs):
        calls.append("searxng")
        return HTTPResult(200, b'{"results":[]}', {})

    def model(request, *, timeout):
        del timeout
        packet = json.loads(request.data)
        packets.append(packet)
        step = len(packets)
        if step == 1:
            return tool("request_capability", {"tool_id": "search_web"}, step)
        if step == 2:
            return tool("search_web", {"mode": "research", "provider": "searxng",
                                       "information_need": "synthetic empty index",
                                       "queries": ["synthetic unmatched topic"]}, step)
        if step == 3:
            operation = next(json.loads(message["content"])["operation_ref"]["id"]
                             for message in packet["messages"]
                             if message.get("role") == "tool"
                             and message.get("name") == "search_web")
            selected["operation"] = operation
            return tool("request_capability", {"tool_id": "read_search_results"}, step)
        if step == 4:
            return tool("read_search_results", {"operation_id": selected["operation"]}, step)
        assert step == 5
        result = next(json.loads(message["content"]) for message in packet["messages"]
                      if message.get("role") == "tool"
                      and message.get("name") == "read_search_results")
        assert result["status"] == "completed"
        assert result["available_results"][0]["result"]["hits"] == []
        assert all('"source_packets"' not in message.get("content", "")
                   for message in packet["messages"] if message.get("role") == "user")
        return answer("The injected index returned no candidate; no source was checked.", step)

    selected = {}
    host = service(tmp_path, monkeypatch, model)
    test_profiles = GroqCredentialProfiles(tmp_path)
    host.credential_profiles = test_profiles
    host.session.adapter.credential_profiles = test_profiles
    host.web_search.transport = search
    try:
        run = host.start(LiveStartRequest(
            request_id="offline-empty-index", question="Search the synthetic empty index.",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id, limit=20)
        assert host._run(run.run_id)["body"]["status"] == "completed"
        assert calls == ["searxng"] and len(packets) == 5
        assert host.store.records("public_document_fetch") == []
        assert host.store.records("source_copy") == []
        assert host.store.records("source_read_window") == []
        artifact_root = os.environ.get("ORA_PROVIDER_HTML_ARTIFACT_DIR")
        if artifact_root:
            output = Path(artifact_root) / "empty-provider-result"
            output.mkdir(parents=True, exist_ok=True)
            (output / "manifest.json").write_text(json.dumps({
                "synthetic_fixture": True,
                "provider_response": "synthetic_injected_empty_searxng",
                "expected": "completed empty candidate result; no fetch or checked source packet",
                "actual": "completed empty candidate result; no fetch or checked source packet",
                "search_calls": len(calls), "fetch_calls": 0,
                "saved_source_copies": 0, "prepared_requests": len(packets),
                "model_transport": "injected_offline",
                "credential_profile": "isolated_nonsecret_offline_test_profile",
            }, indent=2) + "\n")
    finally:
        host.close()
