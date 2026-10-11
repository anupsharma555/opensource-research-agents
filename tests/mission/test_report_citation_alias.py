"""Accept only exact, unambiguous passage-record aliases in reports."""

import json
from dataclasses import replace

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.state import StateError
from tests.mission.test_service import drain, service, tool, two_supplied_sources


def _prepared(tmp_path, monkeypatch, *, second_source=False):
    def no_model_call(*_args, **_kwargs):
        raise AssertionError("Report validation must not call a model")

    host = service(tmp_path, monkeypatch, no_model_call)
    run = host.start(LiveStartRequest(
        request_id="exact-report-alias",
        question="Assess synthetic evidence",
        evidence_text="A synthetic inspected source passage for this report.",
        agent_ids=["agent-synthesis"],
    ))
    if second_source:
        two_supplied_sources(host, run.run_id)
    control = host._control(run.run_id, "agent-synthesis")
    assert host._schedule_model(control)
    work = host._work_for(host._control(run.run_id, "agent-synthesis"))
    spec = host.executor._spec_from(work)
    sources = list(spec.input_refs)
    assert host.store.get("model_request_intent", f"model-intent:{spec.call_id}:1")
    return host, spec, sources


def _arguments(source_id, span_id):
    return {
        "title": "Synthetic contribution",
        "text": "A bounded synthetic finding.",
        "citations": [{"source_id": source_id, "span_id": span_id}],
        "limitations": ["Synthetic fixture"],
    }


@pytest.mark.parametrize("citation_form", ["record_id", "canonical_span_id"])
def test_exact_passage_record_id_and_canonical_span_save_the_same_citation(
    tmp_path, monkeypatch, citation_form
):
    host, spec, sources = _prepared(tmp_path, monkeypatch)
    try:
        source = host.store.get(**sources[0])
        passage = host.store.get("source_passage", source["body"]["passage_ids"][0])
        requested = (
            passage["id"] if citation_form == "record_id" else passage["body"]["span_id"]
        )
        arguments = _arguments(source["id"], requested)
        report = host.store.get(**host._report(spec, arguments))
        assert arguments["citations"][0]["span_id"] == requested
        assert report["body"]["citations"] == [
            {"sourceId": source["id"], "spanId": passage["body"]["span_id"]}
        ]
        assert report["body"]["citation_input_provenance"][0]["passage_ref"] == ref(passage)
    finally:
        host.close()


@pytest.mark.parametrize("invalid", ["unknown", "cross_source", "ambiguous_versions"])
def test_unknown_cross_source_and_ambiguous_version_aliases_still_reject(
    tmp_path, monkeypatch, invalid
):
    host, spec, sources = _prepared(
        tmp_path, monkeypatch, second_source=invalid == "cross_source"
    )
    try:
        first = host.store.get(**sources[0])
        passage = host.store.get("source_passage", first["body"]["passage_ids"][0])
        requested_id = passage["id"]
        if invalid == "unknown":
            requested_id = "source-passage-unknown"
        elif invalid == "cross_source":
            second = host.store.get(**sources[1])
            requested_id = second["body"]["passage_ids"][0]
        else:
            def revise(tx):
                prior = tx.get(**sources[0])
                return ref(tx.put(
                    "source_copy", prior["id"],
                    {**prior["body"], "source_version": "synthetic-v2"},
                    expected_version=prior["version"],
                ))

            newer = host.store.transact("ambiguous-source-version", {}, revise)
            spec = replace(spec, input_refs=(*spec.input_refs, newer))
        with pytest.raises(StateError, match="exact inspected source and span IDs"):
            host._report(spec, _arguments(first["id"], requested_id))
        assert host.store.records("report") == []
    finally:
        host.close()


def test_same_record_id_at_two_passage_versions_is_not_an_alias(tmp_path, monkeypatch):
    host, spec, sources = _prepared(tmp_path, monkeypatch)
    try:
        source = host.store.get(**sources[0])
        first = host.store.get("source_passage", source["body"]["passage_ids"][0])

        def revise_and_window(tx):
            newer = tx.put(
                "source_passage", first["id"], first["body"],
                expected_version=first["version"],
            )
            window = tx.put(
                "source_read_window", "exact-earlier-passage-window",
                {"copy_ref": sources[0], "passage_refs": [ref(first)]},
            )
            return {"newer": ref(newer), "window": ref(window)}

        saved = host.store.transact(
            "same-passage-two-versions", {}, revise_and_window,
            mission_id=spec.mission_id,
        )
        assert saved["newer"]["id"] == ref(first)["id"]
        assert saved["newer"]["version"] != first["version"]
        control = host._control(spec.run_id, "agent-synthesis")
        host._update_control(control, read_window_refs=[saved["window"]])

        with pytest.raises(StateError, match="exact inspected source and span IDs"):
            host._report(spec, _arguments(source["id"], first["id"]))
        assert host.store.records("report") == []

        canonical = _arguments(source["id"], first["body"]["span_id"])
        report = host.store.get(**host._report(spec, canonical))
        assert report["body"]["citations"] == [
            {"sourceId": source["id"], "spanId": first["body"]["span_id"]}
        ]
    finally:
        host.close()


def test_invalid_citation_gives_scoped_repair_before_any_scope_write(tmp_path, monkeypatch):
    calls = []
    selected = {}

    def transport(request, *, timeout):
        body = json.loads(request.data)
        calls.append(body)
        if len(calls) == 1:
            return tool("publish_result", {
                **_arguments(selected["source_id"], "source-passage-unknown"),
                "scope_completion": {"reason": "Synthetic scope checked", "decisions": []},
            }, 1)
        rejected = json.loads(next(
            item["content"] for item in reversed(body["messages"])
            if item["role"] == "tool"
        ))
        assert rejected["status"] == "rejected"
        assert rejected["field"] == "citations"
        assert rejected["external_action"] == "none"
        assert rejected["eligible_citations"] == [{
            "source_id": selected["source_id"], "span_id": selected["span_id"],
        }]
        assert rejected["next_action"]
        return tool("publish_result", _arguments(
            selected["source_id"], selected["span_id"]
        ), 2)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="citation-repair",
            question="Assess one synthetic inspected source",
            evidence_text="A synthetic inspected source passage for this report.",
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        copy = host.store.get(**control["body"]["source_refs"][0])
        passage = host.store.get("source_passage", copy["body"]["passage_ids"][0])
        selected.update(source_id=copy["id"], span_id=passage["body"]["span_id"])
        drain(host, run.run_id, limit=20)
        assert len(calls) == 2
        assert len(host.store.records("report")) == 1
        assert host.store.records("mission_scope_disposition") == []
    finally:
        host.close()
