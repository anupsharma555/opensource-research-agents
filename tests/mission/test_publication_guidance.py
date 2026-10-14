"""Publication guidance reaches the model without imposing a peer exchange."""

from __future__ import annotations

import json

import pytest

from ora.contracts.models import LiveStartRequest
from tests.mission.test_service import answer, drain, service, tool


@pytest.mark.parametrize("publication_mode", ["direct_text", "publish_tool"])
def test_prepared_guidance_explains_shared_outcome_and_keeps_direct_completion(
    tmp_path, monkeypatch, publication_mode,
):
    packets = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        instructions = packet["messages"][0]["content"]
        assert "publish_result ends your current peer task" in instructions
        assert "may close the run once other required work is done" in instructions
        assert "saved question's requested roles and outcome" in instructions
        assert "needs or could benefit from an alternative perspective" in instructions
        assert "read the relevant peer contribution or seek a focused response" in instructions
        assert "distinct, complementary perspective" in instructions
        assert "alternatives or counterevidence within the saved scope" in instructions
        assert "Keep peer requests focused" in instructions
        assert "exact saved references when available" in instructions
        assert "agreement, qualification and revision are all valid outcomes" in instructions
        assert "a catalog title alone does not establish its findings" in instructions
        assert "peer input you actually used" in instructions
        assert "finish directly when peer input would not add useful value" in instructions
        attached = {item["function"]["name"] for item in packet["tools"]}
        assert {"peer_request", "read_context_record", "publish_result"} <= attached
        text = "An independent synthetic contribution with no peer review claimed."
        if publication_mode == "publish_tool":
            return tool("publish_result", {
                "title": "Independent synthetic note", "text": text,
                "citations": [], "limitations": ["Synthetic source-free control"],
            }, len(packets))
        return answer(text, len(packets))

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="publication-guidance-independent-control",
            question="Give two independent preliminary views; no joint review is needed.",
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis", "agent-critique"],
        ))
        drain(host, run.run_id)
        assert len(packets) == 2
        assert len({packet["messages"][0]["content"].split("\n\n")[0]
                    for packet in packets}) == 2
        assert host._run(run.run_id)["body"]["status"] == "completed"
        assert all(item["body"]["phase"] == "done" for item in host._controls(run.run_id))
        assert len(host.store.records("report")) == 2
        assert not host.store.records("mail_request")
        assert not host.store.records("mission_scope_disposition")
    finally:
        host.close()


def test_source_free_summary_repairs_record_id_used_as_source_citation(tmp_path, monkeypatch):
    packets = []

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        instructions = packet["messages"][0]["content"]
        assert "Reading a peer report does not itself inspect its underlying sources" in instructions
        publication = next(item["function"] for item in packet["tools"]
                           if item["function"]["name"] == "publish_result")
        citation_schema = publication["parameters"]["properties"]["citations"]
        assert "Peer-report and note IDs are not source IDs" in citation_schema["description"]
        arguments = {
            "title": "Synthetic memo summary", "text": "The task memo reports 8 complete records.",
            "citations": [], "limitations": ["Task memo only; no original source was inspected."],
        }
        if len(packets) == 1:
            arguments["citations"] = [{"source_id": "report-synthetic-memo", "span_id": "full"}]
        else:
            rejected = json.loads(next(message["content"] for message in reversed(packet["messages"])
                                       if message["role"] == "tool"))
            assert rejected["status"] == "rejected"
            assert rejected["eligible_citations"] == []
            assert "use citations: []" in rejected["next_action"]
            assert rejected["external_action"] == "none"
            assert host.store.records("report") == []
        return tool("publish_result", arguments, len(packets))

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="source-free-citation-repair",
            question="Summarize this synthetic task memo: 8 records are complete. State the evidence limit.",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id)
        assert len(packets) == 2
        reports = host.store.records("report")
        assert len(reports) == 1 and reports[0]["body"]["citations"] == []
        assert host._run(run.run_id)["body"]["status"] == "completed"
    finally:
        host.close()
