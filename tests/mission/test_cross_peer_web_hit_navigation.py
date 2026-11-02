"""A sibling web lead can be read as metadata without implying fetch authority."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from ora.contracts.models import LiveStartRequest
from ora.mission.service import LocalToolRejection
from ora.retrieval.providers import HTTPResult
from ora.state import StateError
from tests.mission.test_service import _search_spec, service


def test_direct_web_hit_hint_respects_task_scope(tmp_path, monkeypatch):
    monkeypatch.setenv("EXA_API_KEY", "synthetic-exa-key")
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    host.discovery_transport = lambda *_args, **_kwargs: HTTPResult(
        200, json.dumps({"results": [{
            "id": "synthetic-web-lead",
            "url": "https://example.org/synthetic-report.pdf",
            "title": "Synthetic web lead",
            "highlights": ["Uninspected discovery teaser."],
        }]}).encode(), {},
    )
    try:
        run = host.start(LiveStartRequest(
            request_id="web-hit-task-scope", question="Inspect synthetic public leads",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        owner = _search_spec(host, run.run_id, suffix="owner")
        found, _ = host._search(owner, {
            "route": "exa.search", "query": "synthetic web lead", "limit": 1,
        })
        hit_id = found["hits"][0]["hit_id"]

        own_result, _ = host._inspect_hit(owner, {"hit_id": hit_id})
        assert own_result["status"] == "metadata_only"
        assert "fetch_source" in own_result["inspection_scope"]

        reader = host._control(run.run_id, "agent-critique")["body"]
        sibling = replace(owner, work_id="offline-tool-sibling",
                          actor_id=reader["actor_id"],
                          proposal_ref=reader["proposal_ref"])
        sibling_result, _ = host._inspect_hit(sibling, {"hit_id": hit_id})
        assert sibling_result["status"] == "metadata_only"
        assert "fetch_source" not in sibling_result["inspection_scope"]
        assert "search_evidence_library" in sibling_result["inspection_scope"]
        with pytest.raises(StateError, match="outside this peer task"):
            host._fetch_source(sibling, {"hit_id": hit_id})

        other = host.start(LiveStartRequest(
            request_id="another-web-hit-mission", question="Other synthetic mission",
            agent_ids=["agent-synthesis"],
        ))
        with pytest.raises(LocalToolRejection):
            host._inspect_hit(_search_spec(host, other.run_id, suffix="other"),
                              {"hit_id": hit_id})
        assert host.store.records("public_document_fetch") == []
    finally:
        host.close()
