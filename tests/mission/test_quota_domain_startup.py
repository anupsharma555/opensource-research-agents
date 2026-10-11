"""Offline mission startup respects an explicit account-scope cutover and old holds."""

import json
from pathlib import Path

import pytest

import ora.mission.service as service_module
from ora.contracts.models import LiveStartRequest
from ora.credentials import CredentialProfileError, GroqCredentialProfiles
from ora.mission import MissionService
from ora.models import GroqSelectedAdapter, ModelSession
from ora.state import StateStore

ROOT = Path(__file__).resolve().parents[2]


def profile_config(root, *, split=False):
    target = root / ".local/groq-credential-profiles.json"
    target.parent.mkdir(parents=True, exist_ok=True)
    backup = {"enabled": False}
    if split:
        backup.update({
            "quota_domain": "groq-verified-backup",
            "quota_domain_verified": True,
        })
    target.write_text(json.dumps({"version": 1, "profiles": {
        "primary": {"enabled": True}, "backup": backup,
    }}))


def host(state_dir, config_root, monkeypatch, *, transport=None):
    profiles = GroqCredentialProfiles(config_root)
    monkeypatch.setattr(service_module, "GroqCredentialProfiles", lambda _root: profiles)

    def no_provider_call(_request, *, timeout):
        pytest.fail("Offline startup and operator holds must not call a provider")

    adapter = GroqSelectedAdapter(ROOT, transport=transport or no_provider_call)
    adapter.credential_profiles = profiles
    session = ModelSession(StateStore(state_dir), ROOT, adapter=adapter)
    return MissionService(
        ROOT, state_dir, model_session=session,
        execution_mode="offline_test", start_supervisor=False,
    )


def request(name, profile):
    return LiveStartRequest(
        request_id=name, question=f"Synthetic question for {name}",
        agent_ids=["agent-synthesis"], credential_profile=profile,
    )


class Answer:
    headers = {"x-request-id": "synthetic-primary-only"}

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit):
        return json.dumps({
            "id": "synthetic-primary-answer", "model": "openai/gpt-oss-120b",
            "choices": [{"message": {"role": "assistant", "content": "Synthetic answer."},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 5, "completion_tokens": 5, "total_tokens": 10},
        }).encode()


def test_existing_workspace_adds_backup_scope_and_holds_old_backup_call(tmp_path, monkeypatch):
    state_dir = tmp_path / "state"
    config_root = tmp_path / "profile-config"
    profile_config(config_root)
    old = host(state_dir, config_root, monkeypatch)
    primary_request = request("legacy-primary", "primary")
    backup_request = request("legacy-backup", "backup")
    try:
        primary = old.start(primary_request)
        backup = old.start(backup_request)
        assert old._schedule_model(old._control(backup.run_id, "agent-synthesis"))
        prepared = old._control(backup.run_id, "agent-synthesis")["body"]
        intent_id = f"model-intent:{prepared['call_ref']['id']}:{prepared['call_ref']['version']}"
        intent = old.store.get("model_request_intent", intent_id)
        original_policy = old.store.get("capacity_config", "default")
        assert original_policy["version"] == 1
    finally:
        old.close()

    profile_config(config_root, split=True)
    calls = []

    def primary_only_transport(request, *, timeout):
        calls.append(request)
        return Answer()

    monkeypatch.setenv("GROQ_API_KEY", "synthetic-primary-token")
    reopened = host(state_dir, config_root, monkeypatch, transport=primary_only_transport)
    try:
        assert reopened.store.get("capacity_config", "default")["version"] == 2
        bridge = reopened.store.get("capacity_quota_bridge", "groq-verified-backup")
        assert bridge["body"]["prior_config_ref"]["version"] == original_policy["version"]
        assert reopened.start(backup_request).reused is True
        assert reopened._selected_credential(reopened._run(primary.run_id))["quota_domain"] == "groq-selected"
        with pytest.raises(CredentialProfileError, match="disagree"):
            reopened._selected_credential(reopened._run(backup.run_id))
        reopened._drive_run(reopened._run(backup.run_id))
        held = reopened._control(backup.run_id, "agent-synthesis")["body"]
        assert held["phase"] == "waiting"
        assert "explicit transition" in held["waiting_reason"]
        assert held["operator_credential_hold"]["phase"] == prepared["phase"]
        assert held["call_ref"] == prepared["call_ref"]
        assert reopened.store.get("model_request_intent", intent_id) == intent
        assert reopened.store.get("model_response_receipt", f"model-response:{intent_id}") is None
        new_backup = reopened.start(request("new-backup-held", "backup"))
        assert reopened._saved_credential(reopened._run(new_backup.run_id))["quota_domain"] == "groq-verified-backup"
        reopened._drive_run(reopened._run(new_backup.run_id))
        assert "paused by operator" in reopened._control(
            new_backup.run_id, "agent-synthesis",
        )["body"]["waiting_reason"]
        assert calls == []
        for _ in range(12):
            reopened._drive_run(reopened._run(primary.run_id))
            if reopened._run(primary.run_id)["body"]["status"] == "completed":
                break
        assert reopened._run(primary.run_id)["body"]["status"] == "completed"
        assert len(calls) == 1
    finally:
        reopened.close()

    again = host(state_dir, config_root, monkeypatch)
    try:
        assert again.store.get("capacity_config", "default")["version"] == 2
        assert again.store.get("capacity_quota_bridge", "groq-verified-backup") == bridge
    finally:
        again.close()


def test_fresh_split_and_unchanged_shared_workspace_need_no_migration(tmp_path, monkeypatch):
    split_config = tmp_path / "split-config"
    profile_config(split_config, split=True)
    fresh = host(tmp_path / "fresh-state", split_config, monkeypatch)
    try:
        config = fresh.store.get("capacity_config", "default")
        assert config["version"] == 1
        assert set(config["body"]["quotas"]) == {"groq-selected", "groq-verified-backup"}
        assert fresh.store.get("capacity_quota_bridge", "groq-verified-backup") is None
    finally:
        fresh.close()

    shared_config = tmp_path / "shared-config"
    profile_config(shared_config)
    first = host(tmp_path / "shared-state", shared_config, monkeypatch)
    try:
        run = first.start(request("still-shared", "primary"))
        config = first.store.get("capacity_config", "default")
    finally:
        first.close()
    reopened = host(tmp_path / "shared-state", shared_config, monkeypatch)
    try:
        assert reopened.store.get("capacity_config", "default") == config
        assert reopened.store.get("capacity_quota_bridge", "groq-verified-backup") is None
        assert reopened._selected_credential(reopened._run(run.run_id))["quota_domain"] == "groq-selected"
    finally:
        reopened.close()
