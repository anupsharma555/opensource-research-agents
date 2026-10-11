"""Follow checked source text through a direct response into Atlas citation links."""

import json

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from tests.mission.test_service import answer, drain, service, tool


@pytest.mark.parametrize("marker_kind", ["exact", "unknown", "mixed"])
def test_direct_answer_preserves_only_exact_selected_passage_links(
    tmp_path, monkeypatch, marker_kind
):
    calls = []
    selected = {}

    def transport(request, *, timeout):
        body = json.loads(request.data)
        calls.append(body)
        if len(calls) == 1:
            return tool("read_source_window", {
                "source_id": selected["source"]["id"], "source_version": 1,
                "block_offset": 0, "char_offset": 0, "char_limit": 800,
                "selection_mode": "replace",
            }, 1)
        window = host.store.records("source_read_window")[0]
        selected["passage"] = host.store.get(**window["body"]["passage_refs"][0])
        marker = selected["passage"]["id"] if marker_kind != "unknown" else "source-unselected"
        extra = " Another finding 【source-unknown:span-other】." if marker_kind == "mixed" else ""
        return answer(f"The synthetic passage reports a comparison. 【{marker}】{extra}", 2)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="direct-citation-links", question="Describe the supplied comparison",
            evidence_text="DOI: 10.1234/direct-example. A synthetic comparison was reported.",
            agent_ids=["agent-synthesis"],
        ))
        selected["source"] = host._control(run.run_id, "agent-synthesis")["body"]["source_refs"][0]
        drain(host, run.run_id, limit=20)
        state = host.state(run.run_id).snapshot
        assert state.mission.status == "completed"
        assert len(calls) == 2  # Saving an exact link adds no conversion-only model call.
        report = host.store.records("report")[0]
        artifact = next(item for item in state.artifacts if item.id == report["id"])
        if marker_kind == "unknown":
            assert report["body"]["citations"] == []
            assert artifact.citations == []
            assert report["body"]["limitations"]
            return
        passage = selected["passage"]
        assert report["body"]["citations"] == [{
            "sourceId": selected["source"]["id"], "spanId": passage["body"]["span_id"],
        }]
        provenance = report["body"]["citation_input_provenance"][0]
        intent = host.store.get(**provenance["producing_intent_ref"])
        assert provenance["passage_ref"] == ref(passage)
        assert provenance["copy_ref"] == selected["source"]
        assert provenance["input_status"] == "exact_passage_in_producing_call"
        assert json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"])) == calls[1]
        assert passage["body"]["text"] in json.dumps(calls[1]["messages"])
        assert len(artifact.citations) == 1
        assert bool(report["body"]["limitations"]) == (marker_kind == "mixed")
    finally:
        host.close()
