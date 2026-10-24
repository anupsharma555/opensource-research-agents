"""Page guidance follows actual selected source input in serialized requests."""

from __future__ import annotations

import json

import pytest

from ora.contracts.models import LiveStartRequest
from tests.mission.test_service import service

GUIDANCE = (
    "Check source-based answers and limitations against the supplied text: "
    "preserve each measure, qualifier, population, and condition. Name "
    "supporting pages when labeled; mark inferences, and do not present "
    "plausible additions as source facts. Include the source URL when requested."
)


@pytest.mark.parametrize("with_source", [False, True])
def test_page_guidance_tracks_selected_source_packet(tmp_path, monkeypatch, with_source):
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: pytest.fail(
        "Preparing a request must not dispatch a provider call"
    ))
    try:
        text = (
            "[page=1; layout-text]\nA synthetic surface reflects light.\n\n"
            "[page=2; layout-text]\nThe measured surface stayed cooler."
        )
        run = host.start(LiveStartRequest(
            request_id=f"page-guidance-{with_source}",
            question="Summarize the supplied synthetic observation.",
            evidence_text=text if with_source else "",
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(run.run_id, "agent-synthesis")
        assert host._prepare_model(control)
        call = host._control(run.run_id, "agent-synthesis")["body"]["call_ref"]
        intent = host.store.get("model_request_intent", f"model-intent:{call['id']}:{call['version']}")
        request = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        instructions = request["messages"][0]["content"]
        assert (GUIDANCE in instructions) is with_source
        assert "Cite inspected supporting source/span IDs." in instructions
        source_packets = [
            json.loads(message["content"])["source_packets"]
            for message in request["messages"]
            if message["role"] == "user"
            and isinstance(message.get("content"), str)
            and message["content"].startswith('{"source_packets":')
        ]
        if with_source:
            assert len(source_packets) == 1
            assert len(source_packets[0]) == 1
            packet = source_packets[0][0]
            assert text in packet["text"]
            assert "page=1" in packet["text"] and "page=2" in packet["text"]
            assert intent["body"]["source_refs"]
        else:
            assert not source_packets
            assert intent["body"]["source_refs"] == []
    finally:
        host.close()
