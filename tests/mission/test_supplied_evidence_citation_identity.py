"""Supplied source labels are data; current checked IDs authorize citations."""

from __future__ import annotations

import json

import pytest

from ora.contracts.models import LiveStartRequest
from ora.mission.service import LocalToolRejection
from tests.mission.test_service import service

SOURCE_TEXT = (
    "Retained synthetic audit note.\n"
    "Source URL: https://example.invalid/audit-note\n"
    "Excerpt 1 — section=Methods; block=A; chars=0:48\n"
    "A highest value identifies a row containing that value.\n"
    "Excerpt 2 — section=Methods; block=B; chars=0:57\n"
    "When rows tie, the associated label is unspecified by this note.\n"
)
HISTORICAL_ID = "source-copy-historical-fixture"


def test_neutral_supplied_text_keeps_current_citation_identity(tmp_path, monkeypatch):
    def no_transport(*_args, **_kwargs):
        raise AssertionError("Source and citation validation must stay offline")

    host = service(tmp_path, monkeypatch, no_transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="neutral-supplied-identity",
            question="Assess only the synthetic audit note and state tie limits.",
            scope="Use the supplied note; do not fetch or execute anything.",
            evidence_text=SOURCE_TEXT,
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis", "agent-critique"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        copy_ref = control["body"]["source_refs"][0]
        copy = host.store.get(**copy_ref)
        assert host.store.read_blob(copy["body"]["blob"]["sha256"]) == SOURCE_TEXT.encode()
        passages = [
            host.store.get("source_passage", passage_id)
            for passage_id in copy["body"]["passage_ids"]
        ]
        assert passages and all(
            passage["body"]["inspection_state"] == "checked"
            and passage["body"]["copy_ref"] == copy_ref
            for passage in passages
        )
        assert HISTORICAL_ID not in SOURCE_TEXT

        assert host._schedule_model(control)
        prepared = host._control(run.run_id, "agent-synthesis")
        work = host._work_for(prepared)
        spec = host.executor._spec_from(work)
        call_ref = prepared["body"]["call_ref"]
        intent = host.store.get(
            "model_request_intent", f"model-intent:{call_ref['id']}:{call_ref['version']}",
        )
        packet = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        selected = next(
            json.loads(message["content"])["source_packets"]
            for message in packet["messages"]
            if message["role"] == "user"
            and isinstance(message.get("content"), str)
            and message["content"].startswith('{"source_packets"')
        )
        assert any(item["id"] == copy["id"] for item in selected)
        serialized = json.dumps(packet["messages"])
        assert "Excerpt 1" in serialized and "Excerpt 2" in serialized
        assert HISTORICAL_ID not in serialized

        current = {"source_id": copy["id"], "span_id": passages[0]["body"]["span_id"]}
        assert current["source_id"] in serialized
        assert current["span_id"] in serialized
        _eligible, cited, _valid = host._validate_report_citations(
            spec, {"text": "The synthetic note leaves ties unresolved.",
                   "citations": [current]},
        )
        assert cited == [(current["source_id"], current["span_id"])]
        with pytest.raises(LocalToolRejection) as rejected:
            host._validate_report_citations(
                spec, {"text": "The synthetic note leaves ties unresolved.",
                       "citations": [{"source_id": HISTORICAL_ID,
                                      "span_id": current["span_id"]}]},
            )
        assert rejected.value.field == "citations"
        assert current in rejected.value.details["eligible_citations"]
        assert host.store.records("model_dispatch_attempt") == []
    finally:
        host.close()
