"""Credential selection pins identity and quota sharing without live requests."""

import json
from dataclasses import replace

import pytest

from ora.agents.configuration import ConfigurationError, _check_settings
from ora.credentials import CredentialProfileError, GroqCredentialProfiles
from ora.live.provider import CredentialUnavailable, GroqProvider
from ora.models import CallConfiguration, GroqSelectedAdapter


def mapping(root, entries):
    target = root / ".local/groq-credential-profiles.json"
    target.parent.mkdir(exist_ok=True)
    target.write_text(json.dumps({"version": 1, "profiles": entries}))


def test_defaults_share_historical_quota_without_reading_keys(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "fixture-primary")
    monkeypatch.setenv("GROQ_API_KEY_BACKUP", "fixture-backup")
    profiles = GroqCredentialProfiles(tmp_path)
    assert profiles.selection("primary") == {
        "credential_profile": "primary", "quota_domain": "groq-selected",
    }
    assert profiles.selection("backup")["quota_domain"] == "groq-selected"
    assert profiles.scopes() == ("groq-selected",)


def test_explicit_mapping_and_reopen_preserve_domain_identity(tmp_path):
    mapping(tmp_path, {"backup": {
        "quota_domain": "groq-fixture-secondary", "quota_domain_verified": True,
    }})
    first = GroqCredentialProfiles(tmp_path)
    selected = first.selection("backup")
    assert selected["quota_domain"] == "groq-fixture-secondary"
    assert GroqCredentialProfiles(tmp_path).selection("backup") == selected
    assert first.scopes() == ("groq-fixture-secondary", "groq-selected")
    assert first.verified_backup_transition() == {
        "from_scope": "groq-selected", "to_scope": "groq-fixture-secondary",
    }
    mapping(tmp_path, {"backup": {
        "quota_domain": "groq-selected", "quota_domain_verified": True,
    }})
    with pytest.raises(CredentialProfileError, match="disagree"):
        GroqCredentialProfiles(tmp_path).validate_binding(**dict(
            profile_id="backup", quota_domain=selected["quota_domain"],
        ))


def test_distinct_backup_scope_requires_explicit_verified_mapping(tmp_path):
    assert GroqCredentialProfiles(tmp_path).verified_backup_transition() is None
    mapping(tmp_path, {"backup": {"enabled": False}})
    assert GroqCredentialProfiles(tmp_path).verified_backup_transition() is None
    mapping(tmp_path, {"backup": {
        "enabled": False, "quota_domain": "groq-selected", "quota_domain_verified": True,
    }})
    assert GroqCredentialProfiles(tmp_path).verified_backup_transition() is None
    mapping(tmp_path, {
        "primary": {"quota_domain": "groq-primary", "quota_domain_verified": True},
        "backup": {"quota_domain": "groq-backup", "quota_domain_verified": True},
    })
    assert GroqCredentialProfiles(tmp_path).verified_backup_transition() is None


def test_operator_hold_preserves_profile_binding_and_shared_quota(tmp_path):
    mapping(tmp_path, {"primary": {"enabled": False}, "backup": {"enabled": True}})
    profiles = GroqCredentialProfiles(tmp_path)
    assert not profiles.is_enabled("primary")
    assert profiles.is_enabled("backup")
    assert profiles.selection("primary")["quota_domain"] == "groq-selected"
    assert profiles.selection("backup")["quota_domain"] == "groq-selected"
    profiles.validate_binding("primary", "groq-selected")
    mapping(tmp_path, {"primary": {"enabled": True}})
    assert not profiles.is_enabled("primary")  # Restart is the explicit activation boundary.
    assert GroqCredentialProfiles(tmp_path).is_enabled("primary")


def test_operator_run_selection_is_optional_and_does_not_override_profile_hold(tmp_path):
    assert GroqCredentialProfiles(tmp_path).is_run_enabled("primary", "run-fixture-old")
    mapping(tmp_path, {
        "primary": {"enabled": True, "allowed_run_ids": ["run-fixture-new"]},
        "backup": {"enabled": False, "allowed_run_ids": ["run-fixture-new"]},
    })
    profiles = GroqCredentialProfiles(tmp_path)
    assert profiles.is_run_enabled("primary", "run-fixture-new")
    assert not profiles.is_run_enabled("primary", "run-fixture-old")
    assert not profiles.is_run_enabled("backup", "run-fixture-new")
    assert profiles.selection("primary")["quota_domain"] == "groq-selected"
    mapping(tmp_path, {"primary": {"allowed_run_ids": []}})
    assert not GroqCredentialProfiles(tmp_path).is_run_enabled("primary", "run-fixture-new")
    assert profiles.is_run_enabled("primary", "run-fixture-new")  # Startup snapshot is stable.


@pytest.mark.parametrize("entry", [
    {"quota_domain": "groq-other", "quota_domain_verified": False},
    {"quota_domain": "groq-other", "quota_domain_verified": 1},
    {"quota_domain": "private@example.invalid", "quota_domain_verified": True},
    {"quota_domain": "groq-other", "quota_domain_verified": True, "key": "private-token"},
    {"enabled": "false"},
    {"enabled": False, "quota_domain": "groq-other"},
    {"allowed_run_ids": None},
    {"allowed_run_ids": "run-fixture"},
    {"allowed_run_ids": ["run-fixture", "run-fixture"]},
    {"allowed_run_ids": ["private@example.invalid"]},
    {"allowed_run_ids": [True]},
])
def test_invalid_unverified_or_secret_mapping_is_rejected_without_echo(tmp_path, entry):
    mapping(tmp_path, {"backup": entry})
    with pytest.raises(CredentialProfileError) as caught:
        GroqCredentialProfiles(tmp_path)
    assert "private" not in str(caught.value)


def test_selected_key_has_no_fallback_and_rotation_keeps_profile(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "fixture-primary")
    monkeypatch.delenv("GROQ_API_KEY_BACKUP", raising=False)
    resolver = GroqProvider(tmp_path)
    with pytest.raises(CredentialUnavailable, match="unavailable"):
        resolver._key(profile="backup")
    (tmp_path / ".env").write_text('GROQ_API_KEY_BACKUP="fixture-backup"\n')
    assert resolver._key(profile="backup") == "fixture-backup"
    monkeypatch.setenv("GROQ_API_KEY_BACKUP", "fixture-rotated")
    assert resolver._key(profile="backup") == "fixture-rotated"
    assert resolver._key() == "fixture-primary"
    monkeypatch.setenv("GROQ_API_KEY_BACKUP", "invalid key with spaces")
    with pytest.raises(CredentialUnavailable, match="invalid"):
        resolver._key(profile="backup")


def test_profile_settings_allow_only_nonsecret_selection_labels():
    _check_settings({"credential_profile": "primary"})
    _check_settings({"credential_profile": "backup"})
    for value in ("fixture-sensitive-token", {"key": "primary"}, ["backup"], None):
        with pytest.raises(ConfigurationError, match="nonsecret"):
            _check_settings({"credential_profile": value})


def test_prepared_binding_selects_key_without_sending_profile_to_provider(tmp_path, monkeypatch):
    monkeypatch.setenv("GROQ_API_KEY", "fixture-primary")
    monkeypatch.setenv("GROQ_API_KEY_BACKUP", "fixture-backup")
    selected = CallConfiguration("cfg", "a" * 64, "Return the fixture answer.",
                                 credential_profile="backup")
    observed = []

    class Response:
        headers = {}

        def __enter__(self):
            return self

        def __exit__(self, *_):
            return None

        def read(self, _):
            return json.dumps({"id": "fixture-response", "model": selected.model_id,
                               "choices": [{"finish_reason": "stop", "message": {
                                   "role": "assistant", "content": '{"answer":"ok"}',
                               }}], "usage": {"prompt_tokens": 2, "completion_tokens": 3,
                                              "total_tokens": 5}}).encode()

    def transport(request, **_):
        observed.append(request)
        assert request.get_header("Authorization") == "Bearer fixture-backup"
        body = json.loads(request.data)
        assert "credential_profile" not in body and "quota_domain" not in body
        assert body["reasoning_effort"] == "medium"
        return Response()

    adapter = GroqSelectedAdapter(tmp_path, transport=transport)
    prepared = adapter.prepare(
        selected, call_id="call", attempt_id="attempt", task_category="fixture", phase="final",
        messages=[{"role": "user", "content": "Answer"}], schema_name="answer",
        schema={"type": "object", "properties": {"answer": {"type": "string"}},
                "required": ["answer"], "additionalProperties": False},
    )
    assert prepared.manifest()["credential_profile"] == "backup"
    assert adapter.invoke(prepared).status == "complete"
    assert len(observed) == 1
    mismatch = adapter.invoke(replace(prepared, quota_domain="groq-unverified"))
    assert mismatch.error_kind == "credential_profile_mismatch"
    assert len(observed) == 1
    mapping(tmp_path, {"backup": {"enabled": False}})
    paused = GroqSelectedAdapter(tmp_path, transport=transport).invoke(prepared)
    assert paused.error_kind == "credential_profile_paused"
    assert len(observed) == 1
