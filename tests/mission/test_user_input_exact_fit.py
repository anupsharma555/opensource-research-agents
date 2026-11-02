"""Credit a complete current direction only when its exact text fits the packet."""

from __future__ import annotations

import json

from ora.contracts.models import LiveStartRequest
from ora.models.token_estimate import estimate_request_tokens
from tests.mission.test_service import service
from tests.mission.test_user_input_delivery import _command


def test_moderate_direction_fits_exactly_before_optional_detail(tmp_path, monkeypatch):
    direction = (
        "For this same planning task, revise the count after the supplied change. "
        "Separate the original amount, the amount physically retained, and the "
        "amount available for the stated use. Explain each calculation and keep "
        "the unverified note separate from the user's correction. " * 5
    )
    assert len(direction) > 400
    note = (
        "Unverified pasted note from an unspecified worksheet: an earlier total "
        "is claimed to remain available. No author or underlying record is supplied."
    )
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    try:
        run = host.start(LiveStartRequest(
            request_id="exact-moderate-direction",
            question="A synthetic project has a revised count. Prepare its short note.",
            scope="Use only the supplied project facts; do not retrieve external sources.",
            agent_ids=["agent-synthesis"],
        ))
        amendment = host.submit_user_input(_command(host, run.run_id, direction))
        paste = host.submit_user_input(
            _command(host, run.run_id, note, "unverified_pasted_evidence").model_copy(
                update={"request_id": "exact-moderate-paste"}
            )
        )
        control = host._control(run.run_id, "agent-synthesis")
        host._update_control(control, input_budget={
            "observed_limit_tokens": 8000, "target_fraction": .70,
            "basis": "injected_provider_header_fixture", "actual_provider_observation": True,
        })
        assert host._prepare_model(host._control(run.run_id, "agent-synthesis"))
        control = host._control(run.run_id, "agent-synthesis")
        call = control["body"]["call_ref"]
        intent = host.store.get("model_request_intent", f"model-intent:{call['id']}:{call['version']}")
        raw = host.store.read_blob(intent["body"]["blob"]["sha256"])
        packet = json.loads(raw)
        index = json.loads(packet["messages"][-1]["content"])
        inclusion = host.store.get("context_inclusion", f"context:{intent['id']}")

        card = index["current_user_direction"]
        assert card == {
            "ref": amendment.input_ref.model_dump(),
            "origin": "authenticated_local_user",
            "kind": "direction", "text": direction,
        }
        assert amendment.input_ref.model_dump() in inclusion["body"]["included_refs"]
        assert not any(item.get("fetch") == amendment.input_ref.model_dump()
                       for item in inclusion["body"].get("omitted", []))
        assert packet["max_completion_tokens"] == 8192
        budget = host.store.get("mission_call_budget", call["id"])["body"]
        estimate = estimate_request_tokens(raw).tokens
        assert estimate <= budget["request_stage_token_estimate"]["admission_target_tokens"]
        assert (estimate + budget["prior_completion_reservation"]
                + budget["rate_input_headroom_tokens"] <= 8000)
        assert index["current_unverified_paste"] == {
            "ref": paste.input_ref.model_dump(),
            "origin": "authenticated_local_user",
            "kind": "unverified_pasted_evidence", "text": note,
        }
        assert paste.input_ref.model_dump() in inclusion["body"]["included_refs"]
        assert not any(item.get("fetch") == paste.input_ref.model_dump()
                       for item in inclusion["body"].get("omitted", []))
    finally:
        host.close()
