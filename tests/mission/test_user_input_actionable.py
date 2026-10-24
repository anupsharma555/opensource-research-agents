"""A tight next turn identifies long task input and its exact scoped read path."""

from __future__ import annotations

import json

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.mission.context import ContextError
from tests.mission.test_service import answer, service, tool
from tests.mission.test_user_input_delivery import _command, _until


def test_long_direction_and_unverified_note_have_separate_read_routes_under_cap(
    tmp_path, monkeypatch,
):
    direction = (
        "Revise this same task after a return. Three of the original eighteen cartons "
        "went back to the supplier. Report originally ordered, physically remaining, "
        "and participant-distributable quantities separately. " * 8
    )
    note = (
        "Unverified pasted worksheet note: every originally ordered kit remains "
        "available. There is no author, receipt, date, or audit record for this claim. " * 8
    )
    packets, saved = [], {}

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        if len(packets) == 1:
            saved["direction"] = host.submit_user_input(
                _command(host, run.run_id, direction).model_copy(
                    update={"request_id": "current-long-direction"}
                )
            )
            saved["note"] = host.submit_user_input(
                _command(host, run.run_id, note, "unverified_pasted_evidence").model_copy(
                    update={"request_id": "current-unverified-note"}
                )
            )
            response = tool("publish_result", {
                "title": "Initial count", "text": "The original count is 432 kits.",
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
        group = index["current_user_inputs"]
        assert group["origin"] == "authenticated_local_user"
        assert group["read"] == "reader + selected ref"
        assert group["reader"] == {
            "tool": "read_context_record",
            "inclusion_id": index["older_records"]["inclusion_id"],
            "offset": 0, "limit": 2000,
        }
        for key, receipt, full_text in (
            ("direction", saved["direction"], direction),
            ("unverified_paste", saved["note"], note),
        ):
            card = group[key]
            assert card["ref"] == receipt.input_ref.model_dump()
            assert card["prefix"] == full_text[:len(card["prefix"])]
            assert len(card["prefix"]) < len(full_text)
            assert {**group["reader"], **card["ref"]} == {
                "tool": "read_context_record",
                "inclusion_id": index["older_records"]["inclusion_id"],
                **receipt.input_ref.model_dump(), "offset": 0, "limit": 2000,
            }
        assert group["direction"]["status"] in {"prefix_only", "read_required"}
        assert group["unverified_paste"]["status"] == "unverified_prefix_only"
        assert direction not in json.dumps(packet) and note not in json.dumps(packet)
        saved["index"] = index
        return answer("The later inputs require an exact read before a final count.", 2)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="long-input-actionable", question="Count the kits in eighteen cartons.",
            agent_ids=["agent-synthesis"],
        ))
        _until(host, run.run_id, lambda: (
            "direction" in saved
            and host._control(run.run_id, "agent-synthesis")["body"]["phase"] == "ready_model"
        ))
        control = host._control(run.run_id, "agent-synthesis")
        host._update_control(control, input_budget={
            "observed_limit_tokens": 8000, "target_fraction": .70,
            "basis": "injected_provider_header_fixture", "actual_provider_observation": True,
        })
        original_assemble = host.context.assemble
        effective_allowances = []

        def cap_context(*args, **kwargs):
            kwargs["allowance"] = min(kwargs["allowance"], 1135)
            effective_allowances.append(kwargs["allowance"])
            return original_assemble(*args, **kwargs)

        monkeypatch.setattr(host.context, "assemble", cap_context)
        _until(host, run.run_id, lambda: host._run(run.run_id)["body"]["status"] == "completed")
        assert effective_allowances and max(effective_allowances) <= 1135
        second = max(host.store.records("model_request_intent"), key=lambda item: item["created_at"])
        assert second["body"]["request_token_estimate"]["tokens"] <= 5600
        inclusion = host.store.get("context_inclusion", f"context:{second['id']}")
        for receipt, full_text in ((saved["direction"], direction), (saved["note"], note)):
            pointer = receipt.input_ref.model_dump()
            assert pointer not in inclusion["body"]["included_refs"]
            assert any(item.get("fetch") == pointer for item in inclusion["body"]["omitted"])
            read = host.context.read_omitted_record(
                mission_id=control["body"]["mission_id"],
                actor_id=control["body"]["actor_id"],
                task_id=control["body"]["task_id"],
                inclusion_ref=ref(inclusion), pointer=pointer,
                offset=0, limit=2000,
            )
            assert full_text in read["text"]
            with pytest.raises(ContextError):
                host.context.read_omitted_record(
                    mission_id=control["body"]["mission_id"],
                    actor_id="foreign-actor", task_id=control["body"]["task_id"],
                    inclusion_ref=ref(inclusion), pointer=pointer,
                    offset=0, limit=2000,
                )
    finally:
        host.close()


def test_deferred_reader_cue_requires_capability_before_exact_read(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    try:
        run = host.start(LiveStartRequest(
            request_id="deferred-long-input-reader", question="Review a synthetic tally.",
            agent_ids=["agent-synthesis"],
        ))
        long_text = "A later task-scoped correction needs exact reading. " * 400
        receipt = host.submit_user_input(_command(host, run.run_id, long_text))
        control = host._control(run.run_id, "agent-synthesis")
        host._update_control(control, input_budget={
            "observed_limit_tokens": 8000, "target_fraction": .70,
            "basis": "injected_provider_header_fixture", "actual_provider_observation": True,
        })
        assert host._prepare_model(
            host._control(run.run_id, "agent-synthesis"), defer_core_tools=True,
        )
        control = host._control(run.run_id, "agent-synthesis")
        call = host.store.get(**control["body"]["call_ref"])
        intent = host.store.get(
            "model_request_intent", f"model-intent:{call['id']}:{call['version']}",
        )
        packet = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        index = json.loads(packet["messages"][-1]["content"])
        card = index["current_user_inputs"]
        assert card["direction"]["ref"] == receipt.input_ref.model_dump()
        exposed = set(call["body"]["exposed_tools"])
        assert "read_context_record" not in exposed
        assert "request_capability" in exposed
        assert card["reader_setup"] == "request_capability(tool_id=read_context_record) first"
        assert {**card["reader"], **card["direction"]["ref"]} == {
            "tool": "read_context_record",
            "inclusion_id": index["older_records"]["inclusion_id"],
            **receipt.input_ref.model_dump(), "offset": 0, "limit": 2000,
        }
    finally:
        host.close()
