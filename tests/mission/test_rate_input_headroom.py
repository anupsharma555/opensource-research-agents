"""Rate packing leaves explicit estimator headroom without dropping checked text."""

import json

from ora.models.token_estimate import estimate_request_tokens
from tests.mission.test_selected_window_admission import _ready_selected_window


def test_complete_source_request_keeps_headroom_before_provider_probe(tmp_path, monkeypatch):
    host, run, actor, copy_ref, window, *_ = _ready_selected_window(
        tmp_path, monkeypatch, window_chars=2000, crowded=True,
    )
    try:
        control = host._control(run.run_id, actor)
        host._update_control(control, input_budget={
            "observed_limit_tokens": 8000, "target_fraction": 0.70,
        })
        assert host._schedule_model(host._control(run.run_id, actor))
        control = host._control(run.run_id, actor)
        call = host.store.get(**control["body"]["call_ref"])
        intent = host.store.get("model_request_intent", f"model-intent:{call['id']}:{call['version']}")
        raw = host.store.read_blob(intent["body"]["blob"]["sha256"])
        packet = json.loads(raw)
        budget = host.store.get("mission_call_budget", call["id"])["body"]
        stage = budget["request_stage_token_estimate"]
        assert budget["rate_admission_policy"] == "estimate_pacing"
        assert stage["rate_input_headroom_tokens"] == 800
        assert estimate_request_tokens(raw).tokens + stage["prior_completion_reservation"] + 800 <= 8000
        assert copy_ref in intent["body"]["source_refs"]
        assert intent["body"]["source_decisions"][0]["passage_refs"] == window["passage_refs"]
        assert packet["max_completion_tokens"] == 8192
        assert stage["final_input_tokens"] == estimate_request_tokens(raw).tokens
    finally:
        host.close()
