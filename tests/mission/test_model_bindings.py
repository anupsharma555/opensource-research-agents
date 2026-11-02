"""Saved peer model choices remain exact through starts, invitations and follow-ups."""

import json

import pytest
from pydantic import ValidationError

import ora.mission.service as service_module
from ora.contracts.models import LiveFollowupRequest, LiveStartRequest
from ora.coordination.board import ref
from ora.mission.model_bindings import make_agent_model_bindings, resolve_agent_model
from ora.models.profiles import GPT_OSS_MODEL, QWEN_MODEL
from ora.state import StateError
from tests.mission.test_service import answer, drain, service


def _request(request_id="model-bindings", *, selected=None, choices=None):
    return LiveStartRequest(
        request_id=request_id, question="Check a synthetic claim",
        agent_ids=selected or ["agent-synthesis"], agent_models=choices,
    )


def test_low_effort_is_a_qwen_only_new_run_choice():
    qwen = _request("qwen-low-binding", choices={
        "agent-synthesis": {"model": QWEN_MODEL, "reasoning_effort": "low"},
    })
    assert qwen.agent_models["agent-synthesis"].reasoning_effort == "low"
    assert make_agent_model_bindings(
        ["agent-synthesis"], {"agent-synthesis": {
            "model": QWEN_MODEL, "reasoningEffort": "low",
        }},
    )["agent-synthesis"]["reasoningEffort"] == "low"
    with pytest.raises(ValidationError, match="GPT-OSS agent choice"):
        _request("gpt-low-rejected", choices={
            "agent-synthesis": {
                "model": GPT_OSS_MODEL, "reasoning_effort": "low",
            },
        })
    with pytest.raises(ValueError, match="unsupported"):
        make_agent_model_bindings(
            ["agent-synthesis"], {"agent-synthesis": {
                "model": GPT_OSS_MODEL, "reasoningEffort": "low",
            }},
        )


def test_start_pins_each_alias_and_serializes_distinct_model_requests(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    try:
        request = _request(selected=["agent-synthesis", "agent-critique"], choices={
            "agent-critique": {"model": QWEN_MODEL},
        })
        run_id = host.start(request).run_id
        run = host._run(run_id)["body"]
        assert run["agent_model_binding_version"] == 1
        assert set(run["profile"]["agentModelBindings"]) == set(host.role_choices)
        assert run["profile"]["agentModelBindings"]["agent-synthesis"] == {
            "model": GPT_OSS_MODEL, "reasoningEffort": "medium", "maxCompletionTokens": 8192,
        }
        assert run["profile"]["agentModelBindings"]["agent-critique"] == {
            "model": QWEN_MODEL, "reasoningEffort": "medium", "maxCompletionTokens": 512,
        }
        for alias, model in (("agent-synthesis", GPT_OSS_MODEL),
                             ("agent-critique", QWEN_MODEL)):
            control = host._control(run_id, alias)
            config = host._configuration(control)["body"]
            assert config["model_id"] == model
            assert config["settings"]["max_completion_tokens"] == (
                512 if model == QWEN_MODEL else 8192
            )
            assert config["reasoning_settings"] == (
                {"reasoning_effort": "medium", "reasoning_format": "parsed"}
                if model == QWEN_MODEL else
                {"reasoning_effort": "medium", "include_reasoning": True}
            )
            assert host._schedule_model(control)
            call = host.store.get(**host._control(run_id, alias)["body"]["call_ref"])
            intent = host.store.get("model_request_intent", f"model-intent:{call['id']}:1")
            packet = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
            assert packet["model"] == model
            assert packet["reasoning_effort"] == "medium"
            assert ("reasoning_format" in packet) == (model == QWEN_MODEL)
        state = host.state(run_id)
        assert state.model == "Multiple selected models"
        assert {item.id: item.model for item in state.agents}["agent-critique"] == QWEN_MODEL
    finally:
        host.close()


def test_invited_peer_uses_saved_alias_binding(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    try:
        run_id = host.start(_request(choices={"agent-critique": {"model": QWEN_MODEL}})).run_id
        sender = host._control(run_id, "agent-synthesis")
        invitation = host.work.invite(
            "model-binding-invite", run_ref=ref(host._run(run_id)),
            actor_id=sender["body"]["actor_id"], source_control_ref=ref(sender),
            proposal_ref=sender["body"]["proposal_ref"],
            recipient_alias="agent-critique", objective="Review the synthetic claim",
            expected_artifact="Brief critique", invitation_id="model-binding-invitation",
        )
        review = host.store.get(**invitation["review_control_ref"])
        assert review["body"]["alias"] == "agent-critique"
        assert host._configuration(review)["body"]["model_id"] == QWEN_MODEL
        assert {item.id: item.model for item in host.state(run_id).agents}[
            "agent-critique"
        ] == QWEN_MODEL
    finally:
        host.close()


def test_followup_inherits_full_catalog_and_only_changes_credential(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda request, *, timeout: answer("Synthetic answer.", 1))
    try:
        parent_id = host.start(_request(choices={"agent-critique": {
            "model": QWEN_MODEL, "reasoning_effort": "high",
            "max_completion_tokens": 1024,
        }})).run_id
        drain(host, parent_id)
        snapshot = host.state(parent_id).snapshot
        target = snapshot.artifacts[0]
        child = host.followup(LiveFollowupRequest(
            request_id="model-binding-followup", parent_run_id=parent_id,
            parent_snapshot_id=snapshot.snapshot_id, target_id=target.id,
            target_version=target.version, question="Review the synthetic answer",
            agent_ids=["agent-critique"], credential_profile="backup",
        ))
        parent = host._run(parent_id)["body"]
        child_body = host._run(child.run_id)["body"]
        assert child_body["agent_model_binding_version"] == 1
        assert child_body["profile"]["agentModelBindings"] == parent["profile"][
            "agentModelBindings"
        ]
        config = host._configuration(host._control(child.run_id, "agent-critique"))["body"]
        assert config["model_id"] == QWEN_MODEL
        assert config["reasoning_settings"] == {
            "reasoning_effort": "high", "reasoning_format": "parsed",
        }
        assert config["settings"]["max_completion_tokens"] == 1024
        assert config["settings"]["credential_profile"] == "backup"
        assert parent["profile"]["modelSettings"]["credential_profile"] == "primary"
    finally:
        host.close()


def test_reopen_uses_saved_bindings_after_defaults_change(tmp_path, monkeypatch):
    request = _request("model-binding-reopen", choices={"agent-critique": {
        "model": QWEN_MODEL,
    }})
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    try:
        run_id = host.start(request).run_id
        original = host._run(run_id)["body"]["profile"]["agentModelBindings"]
    finally:
        host.close()
    monkeypatch.setattr(service_module, "DEFAULT_REASONING_EFFORT", "high")
    monkeypatch.setattr(service_module, "DEFAULT_MODEL_SETTINGS", {
        "max_completion_tokens": 4096, "stream": False, "unknown_source_policy": "reject",
    })
    reopened = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    try:
        assert reopened.start(request).reused is True
        assert reopened._run(run_id)["body"]["profile"]["agentModelBindings"] == original
        assert reopened._configuration(reopened._control(run_id, "agent-synthesis"))["body"][
            "reasoning_settings"
        ]["reasoning_effort"] == "medium"
        assert reopened._configuration(reopened._control(run_id, "agent-synthesis"))["body"][
            "settings"
        ]["max_completion_tokens"] == 8192
    finally:
        reopened.close()


def test_legacy_run_uses_own_saved_profile_and_prior_configuration():
    legacy = {"profile": {
        "selectedProvider": "groq", "selectedEndpoint": service_module.URL,
        "selectedModel": GPT_OSS_MODEL,
        "modelSettings": {"max_completion_tokens": 3072, "stream": False,
                          "unknown_source_policy": "reject"},
        "reasoningSettings": {"reasoning_effort": "high", "include_reasoning": True},
    }}
    selected = resolve_agent_model(legacy, "agent-critique")
    assert selected["model_id"] == GPT_OSS_MODEL
    assert selected["settings"]["max_completion_tokens"] == 3072
    assert selected["reasoning_settings"]["reasoning_effort"] == "high"
    legacy["profile"].pop("selectedModel")
    assert resolve_agent_model(legacy, "agent-critique", prior_configuration={
        "body": {"model_id": GPT_OSS_MODEL},
    })["model_id"] == GPT_OSS_MODEL


def test_unknown_alias_invalid_choice_and_tampered_binding_fail(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    try:
        with pytest.raises(ValueError, match="configured role aliases"):
            host.start(_request("unknown-model-alias", choices={
                "agent-absent": {"model": QWEN_MODEL},
            }))
        with pytest.raises(ValueError, match="different mission settings"):
            first = _request("model-replay", choices={"agent-critique": {"model": QWEN_MODEL}})
            host.start(first)
            host.start(_request("model-replay", choices={
                "agent-critique": {"model": GPT_OSS_MODEL},
            }))
        run_id = host.start(_request("tampered-binding", choices={
            "agent-synthesis": {"model": QWEN_MODEL},
        })).run_id
        run = host._run(run_id)["body"]
        bad = {**run, "profile": {**run["profile"], "agentModelBindings": {
            **run["profile"]["agentModelBindings"],
            "agent-synthesis": {"model": QWEN_MODEL, "reasoningEffort": "medium",
                                "maxCompletionTokens": 20000},
        }}}
        with pytest.raises(StateError, match="unsupported"):
            resolve_agent_model(bad, "agent-synthesis")
    finally:
        host.close()


def test_prior_call_rejects_reasoning_binding_drift(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    try:
        run_id = host.start(_request("reasoning-binding-drift")).run_id
        control = host._control(run_id, "agent-synthesis")
        assert host._schedule_model(control)
        control = host._control(run_id, "agent-synthesis")
        run = host._run(run_id)
        bindings = dict(run["body"]["profile"]["agentModelBindings"])
        bindings["agent-synthesis"] = {
            **bindings["agent-synthesis"], "reasoningEffort": "high",
        }
        modified = {**run["body"], "profile": {
            **run["body"]["profile"], "agentModelBindings": bindings,
        }}
        host.store.transact(
            "synthetic-reasoning-binding-drift", {},
            lambda tx: tx.put("mission_run", run_id, modified,
                              expected_version=run["version"]),
            mission_id=run["body"]["mission_id"],
        )
        with pytest.raises(StateError, match="disagree with the prior call"):
            host._configuration(control)
    finally:
        host.close()


@pytest.mark.parametrize("choice", [
    {"model": "unknown/model"},
    {"model": QWEN_MODEL, "max_completion_tokens": 16385},
    {"model": GPT_OSS_MODEL, "reasoning_effort": "low"},
])
def test_wire_rejects_unsupported_model_choice(choice):
    with pytest.raises(ValueError):
        _request("invalid-wire-choice", choices={"agent-synthesis": choice})
