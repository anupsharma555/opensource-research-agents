"""Keep a short current direction visible under an observed small token window."""

from __future__ import annotations

import json
import time

import pytest

from ora.contracts.models import LiveStartRequest
from tests.mission.test_service import answer, service, tool
from tests.mission.test_user_input_delivery import _command


def _until(host, run_id, predicate, *, seconds=4):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        host._drive_run(host._run(run_id))
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError("Synthetic run did not reach its expected saved boundary")


@pytest.mark.parametrize("draft_chars", [40, 400, 1000])
def test_short_direction_precedes_optional_details_after_stale_publish(
    tmp_path, monkeypatch, draft_chars,
):
    packets, saved = [], {}
    prior_text = ("The three-copy total is 9 GB. " + "Earlier draft detail. " * 60)[:draft_chars]
    update = (
        "The requirement changed to 4 complete copies. Give both the original "
        "3-copy total and the revised 4-copy total in decimal GB, with the arithmetic."
    )

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        if len(packets) == 1:
            saved["input"] = host.submit_user_input(_command(host, run.run_id, update))
            response = tool("publish_result", {
                "title": "Initial storage total", "text": prior_text,
                "citations": [], "limitations": [],
            }, 1)
            response.headers = {
                "x-request-id": "synthetic-first", "x-ratelimit-limit-tokens": "8000",
                "x-ratelimit-remaining-tokens": "7000",
                "x-ratelimit-reset-tokens": "1m",
            }
            return response
        assert len(packets) == 2
        index = json.loads(packet["messages"][-1]["content"])
        card = index["current_user_direction"]
        assert card["text"] == update
        assert card["origin"] == "authenticated_local_user"
        assert card["kind"] == "direction"
        assert card["ref"] == saved["input"].input_ref.model_dump()
        assert update in json.dumps(packet["messages"])
        assert not any(item["type"] == "user_input" for item in index["details"])
        saved["second_index"] = index
        return answer("Three copies need 9 GB; four copies need 12 GB.", 2)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="short-direction-after-publish",
            question="A local project has 12 audio files, each 250 MB. We need "
            "3 complete copies of every file. What is the total storage in decimal GB?",
            agent_ids=["agent-synthesis"],
        ))
        _until(host, run.run_id, lambda: (
            "input" in saved
            and host._control(run.run_id, "agent-synthesis")["body"]["phase"] == "ready_model"
        ))
        control = host._control(run.run_id, "agent-synthesis")
        first_response = host.store.records("model_response_receipt")[0]
        assert first_response["body"]["rate_headers"]["x-ratelimit-limit-tokens"] == "8000"
        host._update_control(control, input_budget={
            "observed_limit_tokens": 8000, "target_fraction": .70,
            "basis": "injected_provider_header_fixture", "actual_provider_observation": True,
        })
        _until(host, run.run_id, lambda: host._run(run.run_id)["body"]["status"] == "completed")
        assert len(packets) == 2
        second = max(host.store.records("model_request_intent"), key=lambda x: x["created_at"])
        call = host.store.get(**second["body"]["call_ref"])
        inclusion = host.store.get("context_inclusion", f"context:{second['id']}")
        assert second["body"]["request_token_estimate"]["tokens"] <= 5600
        assert saved["input"].input_ref.model_dump() in inclusion["body"]["included_refs"]
        assert call["body"]["task_id"] == saved["input"].task_id
        accepted = host._control(run.run_id, "agent-synthesis")["body"]["report_ref"]
        assert "12 GB" in host.store.get(**accepted)["body"]["body"]
        first_tool = host.store.records("context_tool_result")[0]
        tool_message = json.loads(host.store.read_blob(first_tool["body"]["blob"]["sha256"]))
        assert json.loads(tool_message["content"])["status"] == "not_executed"
    finally:
        host.close()


def test_newer_long_direction_does_not_label_older_short_one_current(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    try:
        run = host.start(LiveStartRequest(
            request_id="latest-long-user-direction", question="Inspect a synthetic note",
            agent_ids=["agent-synthesis"],
        ))
        host.submit_user_input(_command(host, run.run_id, "Earlier short direction.").model_copy(
            update={"request_id": "earlier-short"}))
        long_text = "Later long direction. " * 1000
        latest = host.submit_user_input(_command(
            host, run.run_id, long_text,
        ).model_copy(update={"request_id": "later-long"}))
        control = host._control(run.run_id, "agent-synthesis")
        host._update_control(control, input_budget={
            "observed_limit_tokens": 8000, "target_fraction": .70,
            "basis": "injected_provider_header_fixture", "actual_provider_observation": True,
        })
        assert host._schedule_model(host._control(run.run_id, "agent-synthesis"))
        control = host._control(run.run_id, "agent-synthesis")
        call = control["body"]["call_ref"]
        intent = host.store.get("model_request_intent", f"model-intent:{call['id']}:{call['version']}")
        packet = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        index = json.loads(packet["messages"][-1]["content"])
        card = index["current_user_inputs"]["direction"]
        assert card["ref"] == latest.input_ref.model_dump()
        assert card["status"] in {"prefix_only", "read_required"}
        assert card["prefix"] == long_text[:len(card["prefix"])]
        assert len(card["prefix"]) < len(long_text)
        inclusion = host.store.get("context_inclusion", f"context:{intent['id']}")
        assert any(item.get("fetch") == latest.input_ref.model_dump()
                   for item in inclusion["body"]["omitted"])
        window = host.context.read_omitted_record(
            mission_id=control["body"]["mission_id"],
            actor_id=control["body"]["actor_id"], task_id=control["body"]["task_id"],
            inclusion_ref={key: inclusion[key] for key in ("kind", "id", "version")},
            pointer=latest.input_ref.model_dump(), offset=0, limit=2000,
        )
        assert long_text[:500] in window["text"]
    finally:
        host.close()


def test_new_short_direction_replaces_old_current_card(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    try:
        run = host.start(LiveStartRequest(
            request_id="latest-short-user-direction", question="Inspect a synthetic note",
            agent_ids=["agent-synthesis"],
        ))
        host.submit_user_input(_command(host, run.run_id, "Earlier short direction.").model_copy(
            update={"request_id": "earlier-current"}))
        latest = host.submit_user_input(_command(
            host, run.run_id, "Later short direction overrides the earlier request.",
        ).model_copy(update={"request_id": "later-current"}))
        assert host._schedule_model(host._control(run.run_id, "agent-synthesis"))
        control = host._control(run.run_id, "agent-synthesis")
        call = control["body"]["call_ref"]
        intent = host.store.get("model_request_intent", f"model-intent:{call['id']}:{call['version']}")
        packet = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        index = json.loads(packet["messages"][-1]["content"])
        assert index["current_user_direction"]["ref"] == latest.input_ref.model_dump()
        assert index["current_user_direction"]["text"] == (
            "Later short direction overrides the earlier request."
        )
    finally:
        host.close()


def test_observed_small_context_allowance_keeps_current_direction(tmp_path, monkeypatch):
    """A saved stale publication fits with the direction under a 1,461-token cap."""
    question = (
        "A local project has 12 audio files, each 250 MB. It needs three complete "
        "copies of every file. What is the total storage in decimal GB? Explain "
        "the arithmetic and any assumptions before reporting the result."
    )
    update = (
        "The requirement changed to 4 complete copies. Give both the original "
        "3-copy total and revised 4-copy total in decimal GB, with the arithmetic."
    )
    original_args = {
        "title": "Initial storage calculation",
        "text": (
            "Twelve files at 250 MB each occupy 3,000 MB for one complete copy. "
            "Three copies therefore occupy 9,000 MB, or 9 GB in decimal units. "
            + "This calculation counts complete copies of every file. " * 12
        ),
        "citations": [],
        "limitations": ["Decimal units use 1 GB = 1,000 MB."],
    }
    packets, saved = [], {}

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        if len(packets) == 1:
            saved["input"] = host.submit_user_input(_command(host, run.run_id, update))
            response = tool("publish_result", original_args, 1)
            response.payload["usage"] = {
                "prompt_tokens": 4457, "completion_tokens": 599, "total_tokens": 5056,
                "completion_tokens_details": {"reasoning_tokens": 415},
            }
            response.headers = {
                "x-request-id": "replayed-first", "x-ratelimit-limit-tokens": "8000",
                "x-ratelimit-remaining-tokens": "7000", "x-ratelimit-reset-tokens": "1m",
            }
            return response
        assert len(packets) == 2
        index = json.loads(packet["messages"][-1]["content"])
        assert index["current_user_direction"]["text"] == update
        assert index["current_user_direction"]["ref"] == saved["input"].input_ref.model_dump()
        saved["seen"] = True
        return answer("Three copies need 9 GB; four copies need 12 GB.", 2)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="saved-first-publication-replay", question=question,
            agent_ids=["agent-synthesis"],
        ))
        _until(host, run.run_id, lambda: (
            "input" in saved and host._control(run.run_id, "agent-synthesis")["body"]["phase"] == "ready_model"
        ))
        control = host._control(run.run_id, "agent-synthesis")
        host._update_control(control, input_budget={
            "observed_limit_tokens": 8000, "target_fraction": .70,
            "basis": "injected_provider_header_fixture", "actual_provider_observation": True,
        })
        original_assemble = host.context.assemble
        effective_allowances = []

        def assemble_at_observed_allowance(*args, **kwargs):
            kwargs["allowance"] = min(kwargs["allowance"], 1461)
            effective_allowances.append(kwargs["allowance"])
            return original_assemble(*args, **kwargs)

        monkeypatch.setattr(host.context, "assemble", assemble_at_observed_allowance)
        _until(host, run.run_id, lambda: host._run(run.run_id)["body"]["status"] == "completed")
        assert saved.get("seen") and len(packets) == 2
        assert effective_allowances and all(0 < value <= 1461 for value in effective_allowances)
        second = max(host.store.records("model_request_intent"), key=lambda x: x["created_at"])
        assert second["body"]["request_token_estimate"]["tokens"] <= 5600
    finally:
        host.close()
