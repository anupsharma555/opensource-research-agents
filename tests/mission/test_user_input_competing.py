"""Qualify competing saved user inputs at a real mission model boundary."""

from __future__ import annotations

import json

import pytest

from ora.contracts.models import LiveStartRequest
from ora.mission.context import ContextError
from tests.mission.availability_assertions import request_first_names
from tests.mission.test_service import answer, service
from tests.mission.test_user_input_delivery import _command, _until


def _submit(host, run_id, text, request_id, kind="direction"):
    return host.submit_user_input(_command(host, run_id, text, kind).model_copy(
        update={"request_id": request_id},
    ))


def test_successive_directions_and_contradictory_paste_survive_reopen_and_read(
    tmp_path, monkeypatch,
):
    first_bytes, packets, errors, receipts = [], [], [], {}
    long_direction = "Latest direction: compare the corrected synthetic totals. " * 460
    contradictory_paste = "Unverified pasted note claims the opposite total. " * 520
    host = None
    reopened = None

    def transport(request, *, timeout):
        try:
            raw = bytes(request.data)
            packet = json.loads(raw)
            packets.append(packet)
            if len(packets) == 1:
                first_bytes.append(raw)
                assert len(host.store.records("model_dispatch_attempt")) == 1
                assert long_direction not in raw.decode()
                receipts["short"] = _submit(host, run.run_id, "Initial short direction.", "compete-short")
                receipts["middle"] = _submit(host, run.run_id, "Intermediate correction.", "compete-middle")
                receipts["latest"] = _submit(host, run.run_id, long_direction, "compete-long")
                receipts["paste"] = _submit(
                    host, run.run_id, contradictory_paste, "compete-paste",
                    "unverified_pasted_evidence",
                )
                return answer("Earlier synthetic answer before amendments.", 1)
            assert len(packets) == 2
            index = json.loads(packet["messages"][-1]["content"])
            assert "current_user_direction" not in index
            availability = next(
                json.loads(message["content"]) for message in packet["messages"]
                if message.get("role") == "user"
                and json.loads(message["content"]).get("authority")
                == "trusted_host_attached_tool_availability"
            )
            tools = {item["function"]["name"] for item in packet["tools"]}
            assert set(availability["call_now_local_functions"]) == tools
            # Exact current directions take priority without losing the
            # catalog-backed route to deferred capabilities.
            assert request_first_names(packet, availability)
            assert {"discover_capabilities", "request_capability"} <= tools
            assert "read_context_page" in tools
            if cue := index.get("current_user_inputs"):
                assert cue["origin"] == "authenticated_local_user"
                assert cue["direction"]["ref"] == receipts["latest"].input_ref.model_dump()
                assert cue["unverified_paste"]["ref"] == receipts["paste"].input_ref.model_dump()
                assert cue["direction"]["status"] in {"prefix_only", "read_required"}
                assert cue["unverified_paste"]["status"] == "unverified_prefix_only"
                assert long_direction.startswith(cue["direction"]["prefix"])
                assert contradictory_paste.startswith(cue["unverified_paste"]["prefix"])
                assert cue["reader"]["inclusion_id"] == index["older_records"]["inclusion_id"]
                assert cue["reader"]["tool"] == "read_context_record"
                assert "read_context_record" in tools
            else:
                assert index["user_input_guidance"]["count"] == 4
            assert "authenticated_local_user" in json.dumps(index)
            assert "source_use" not in json.dumps(
                index.get("current_user_inputs", index.get("user_input_guidance", {})))
            return answer("Amendments need assessment before a conclusion.", 2)
        except BaseException as exc:
            errors.append(repr(exc))
            raise

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="competing-input-run",
            question="Assess a synthetic total using the supplied baseline.",
            agent_ids=["agent-synthesis"],
        ))
        host._update_control(host._control(run.run_id, "agent-synthesis"), input_budget={
            "observed_limit_tokens": 8000, "target_fraction": .70,
            "basis": "qualification_imposed_limit", "actual_provider_observation": False,
        })
        _until(host, run.run_id, lambda: len(packets) == 2)
        assert errors == []
        assert len(first_bytes) == 1
        intents = host.store.records("model_request_intent")
        first = next(item for item in intents if host.store.read_blob(
            item["body"]["blob"]["sha256"],
        ) == first_bytes[0])
        assert host.store.get("model_dispatch_attempt", f"model-dispatch:{first['id']}")
        assert host.store.read_blob(first["body"]["blob"]["sha256"]) == first_bytes[0]
        second = next(item for item in intents if item["id"] != first["id"])
        assert json.loads(host.store.read_blob(second["body"]["blob"]["sha256"])) == packets[1]
        inclusion = host.store.get("context_inclusion", f"context:{second['id']}")
        latest_ref = receipts["latest"].input_ref.model_dump()
        paste_ref = receipts["paste"].input_ref.model_dump()
        assert any(item.get("fetch") == latest_ref for item in inclusion["body"]["omitted"])
        assert any(item.get("fetch") == paste_ref for item in inclusion["body"]["omitted"])
        assert len(host.store.records("source_copy")) == 0
        first_profile = host._run(run.run_id)["body"]["profile"]
        host.close()
        reopened = service(tmp_path, monkeypatch, lambda *_a, **_k: None)
        control = reopened._control(run.run_id, "agent-synthesis")
        assert reopened._run(run.run_id)["body"]["profile"] == first_profile
        assert reopened.store.read_blob(first["body"]["blob"]["sha256"]) == first_bytes[0]
        scope = dict(
            mission_id=control["body"]["mission_id"], actor_id=control["body"]["actor_id"],
            task_id=control["body"]["task_id"],
            inclusion_ref={key: inclusion[key] for key in ("kind", "id", "version")},
        )
        for pointer, expected in ((latest_ref, long_direction), (paste_ref, contradictory_paste)):
            parts, offset = [], 0
            while True:
                window = reopened.context.read_omitted_record(
                    **scope, pointer=pointer, offset=offset, limit=2000,
                )
                parts.append(window["text"])
                if window["next_offset"] is None:
                    break
                offset = window["next_offset"]
            assert json.loads("".join(parts))["text"] == expected
        for changed_scope in ({"actor_id": "foreign-actor"}, {"task_id": "foreign-task"}):
            with pytest.raises(ContextError):
                reopened.context.read_omitted_record(
                    **{**scope, **changed_scope}, pointer=latest_ref, offset=0, limit=2000,
                )
        foreign_run = reopened.start(LiveStartRequest(
            request_id="foreign-competing-input-run",
            question="An independent synthetic task.", agent_ids=["agent-critique"],
        ))
        foreign = reopened._control(foreign_run.run_id, "agent-critique")
        with pytest.raises(ContextError):
            reopened.context.read_omitted_record(
                **{**scope, "mission_id": foreign["body"]["mission_id"],
                   "actor_id": foreign["body"]["actor_id"],
                   "task_id": foreign["body"]["task_id"]},
                pointer=latest_ref, offset=0, limit=2000,
            )
        assert reopened.store.records("source_copy") == []
    finally:
        (reopened or host).close()
