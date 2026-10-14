"""Exercise one real shared mission path with an injected offline model transport."""

from __future__ import annotations

import asyncio
import hashlib
import json
import subprocess
import threading
import time
import urllib.error
from io import BytesIO
from pathlib import Path

import pytest

from ora.contracts.models import LiveFollowupRequest, LiveStartRequest
from ora.coordination.board import ref
from ora.coordination.executor import WorkSpec
from ora.evidence.intake import extract_blocks
from ora.mission import MissionService
from ora.mission.context import PACKER_VERSION, ContextOverflow
from ora.mission.service import ESTIMATOR_VERSION, _visible_title_identity
from ora.models import GroqSelectedAdapter, ModelSession
from ora.models.token_estimate import TOKENIZER_BASIS, estimate_request_tokens
from ora.retrieval.providers import HTTPResult
from ora.state import StateStore

ROOT = Path(__file__).resolve().parents[2]
MODEL = "openai/gpt-oss-120b"


def _dense_synthetic_rows(length: int) -> str:
    """Keep source-pressure fixtures varied under the actual tokenizer."""
    return " ".join(
        f"row{i:04d}: x={i * 317 % 997}; y={i * 409 % 991}; z={i * 613 % 983}."
        for i in range(length // 20 + 1)
    )[:length]


class Response:
    headers = {"x-request-id": "offline-fixture"}

    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit):
        return json.dumps(self.payload).encode()


def answer(text: str, index: int) -> Response:
    return Response(
        {
            "id": f"offline-{index}",
            "model": MODEL,
            "choices": [
                {"message": {"role": "assistant", "content": text}, "finish_reason": "stop"}
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11},
        }
    )


def tool(name: str, arguments: dict, index: int) -> Response:
    return Response(
        {
            "id": f"offline-{index}",
            "model": MODEL,
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": f"tool-{index}",
                                "type": "function",
                                "function": {"name": name, "arguments": json.dumps(arguments)},
                            }
                        ],
                    },
                    "finish_reason": "tool_calls",
                }
            ],
            "usage": {"prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11},
        }
    )


def service(tmp_path: Path, monkeypatch, transport) -> MissionService:
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    store = StateStore(tmp_path)
    session = ModelSession(store, ROOT, adapter=GroqSelectedAdapter(ROOT, transport=transport))
    return MissionService(
        ROOT, tmp_path, model_session=session, execution_mode="offline_test", start_supervisor=False
    )


def drain(host: MissionService, run_id: str, limit: int = 12) -> None:
    for _ in range(limit * 20):
        changed, wake = host._drive_run(host._run(run_id))
        if host._run(run_id)["body"]["status"] == "completed":
            return
        if not changed:
            if wake:
                time.sleep(min(0.01, max(0.001, wake - time.time())))
            else:
                time.sleep(0.01)  # A dispatch thread can finish without a scheduled wake.


def two_supplied_sources(host: MissionService, run_id: str) -> tuple[dict, dict]:
    control = host._control(run_id, "agent-synthesis")
    original = control["body"]["source_refs"][0]
    source = host.store.get(**original)
    original_passage = host.store.get("source_passage", source["body"]["passage_ids"][0])
    second_id = f"source-copy-second-{run_id[-12:]}"
    second_text = "A second independent permitted synthetic source."
    blob = host.store.put_blob(second_text.encode())
    passage_id = f"{second_id}:supplied-1"

    def save(tx):
        second = tx.put(
            "source_copy",
            second_id,
            {
                **source["body"],
                "blob": blob,
                "content_sha256": blob["sha256"],
                "source_version": "synthetic-second-v1",
                "passage_ids": [passage_id],
            },
        )
        tx.put(
            "source_passage",
            passage_id,
            {
                **original_passage["body"],
                "copy_ref": ref(second),
                "text": second_text,
                "start": 0,
                "end": len(second_text),
                "locator": f"submitted text; chars=0:{len(second_text)}",
            },
        )
        return ref(second)

    second_ref = host.store.transact(
        f"synthetic-second-source:{run_id}",
        {},
        save,
        mission_id=control["body"]["mission_id"],
    )
    host._update_control(control, source_refs=[original, second_ref])
    return original, second_ref


def revoke_source(host: MissionService, pointer: dict, mission_id: str) -> dict:
    def revoke(tx):
        current = tx.get(**pointer)
        return ref(
            tx.put(
                "source_copy",
                pointer["id"],
                {
                    **current["body"],
                    "model_use": "restricted",
                },
                expected_version=pointer["version"],
            )
        )

    return host.store.transact(
        f"synthetic-revoke:{pointer['id']}",
        {},
        revoke,
        mission_id=mission_id,
    )


def test_question_only_start_direct_answer_and_exact_followup(tmp_path, monkeypatch):
    calls = []

    def transport(request, *, timeout):
        calls.append(json.loads(request.data))
        return answer("Only a provisional answer is possible without evidence.", len(calls))

    host = service(tmp_path, monkeypatch, transport)
    request = LiveStartRequest(
        request_id="mission-direct", question="What is known?", agent_ids=["agent-synthesis"]
    )
    receipt = host.start(request)
    assert host.start(request).reused is True
    with pytest.raises(ValueError, match="different mission settings"):
        host.start(request.model_copy(update={"question": "A different question"}))
    drain(host, receipt.run_id)
    state = host.state(receipt.run_id)
    assert len(calls) == 1
    assert state.snapshot.proof_kind == "offline_test"
    assert state.snapshot.mission.status == "completed"
    assert (
        state.snapshot.artifacts[0].producer_attempt_id
        == host.calls(receipt.run_id)[0]["attemptId"]
    )
    parent = state.snapshot
    artifact = parent.artifacts[0]
    child = host.followup(
        LiveFollowupRequest(
            request_id="mission-child",
            parent_run_id=receipt.run_id,
            parent_snapshot_id=parent.snapshot_id,
            target_id=artifact.id,
            target_version=artifact.version,
            question="Review that answer",
            agent_ids=["agent-critique"],
        )
    )
    assert child.run_id != receipt.run_id
    assert host.state(child.run_id).snapshot.parent_snapshot_id == parent.snapshot_id
    assert host.state(receipt.run_id).snapshot.snapshot_id == parent.snapshot_id
    reopened = MissionService(
        ROOT,
        tmp_path,
        model_session=host.session,
        execution_mode="offline_test",
        start_supervisor=False,
    )
    assert reopened.state(receipt.run_id).snapshot.snapshot_id == parent.snapshot_id
    assert reopened.state().workspace_id == host.state().workspace_id
    assert len(reopened.calls(receipt.run_id)) == 1


def test_saved_run_keeps_selected_model_and_settings_after_default_drift(tmp_path, monkeypatch):
    import ora.mission.service as service_module

    host = service(tmp_path, monkeypatch, lambda request, *, timeout: answer("unused", 1))
    request = LiveStartRequest(
        request_id="mission-pinned-defaults",
        question="Retain the selected provider settings",
        agent_ids=["agent-synthesis"],
    )
    receipt = host.start(request)
    selected = host._run(receipt.run_id)["body"]["profile"]
    original_model, original_endpoint = selected["selectedModel"], selected["selectedEndpoint"]
    monkeypatch.setattr(service_module, "MODEL", "synthetic-different-default")
    monkeypatch.setattr(service_module, "URL", "https://example.org/new-default")
    assert host.start(request).reused is True
    control = host._control(receipt.run_id, "agent-synthesis")
    config = host._configuration(control)["body"]
    assert config["model_id"] == original_model
    assert config["endpoint"] == original_endpoint
    assert config["settings"] == selected["modelSettings"]


def test_explicit_backup_is_pinned_and_followup_inherits_or_changes_profile(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GROQ_API_KEY_BACKUP", "synthetic-backup-token")
    calls = []

    def transport(request, *, timeout):
        calls.append(json.loads(request.data))
        return answer("Synthetic source-free answer.", len(calls))

    host = service(tmp_path, monkeypatch, transport)
    try:
        primary = host.start(LiveStartRequest(
            request_id="profile-primary", question="Synthetic parent",
            agent_ids=["agent-synthesis"],
        ))
        assert host.start(LiveStartRequest(
            request_id="profile-primary", question="Synthetic parent",
            agent_ids=["agent-synthesis"], credential_profile="primary",
        )).reused is True
        drain(host, primary.run_id)
        parent = host.state(primary.run_id).snapshot
        target = parent.artifacts[0]
        common = dict(
            parent_run_id=primary.run_id, parent_snapshot_id=parent.snapshot_id,
            target_id=target.id, target_version=target.version,
            question="Synthetic follow-up", agent_ids=["agent-synthesis"],
        )
        inherited = host.followup(LiveFollowupRequest(
            request_id="profile-inherited", **common,
        ))
        backup = host.followup(LiveFollowupRequest(
            request_id="profile-backup-child", credential_profile="backup", **common,
        ))
        assert host.followup(LiveFollowupRequest(
            request_id="profile-backup-child", credential_profile="backup", **common,
        )).reused is True
        assert host._run(inherited.run_id)["body"]["profile"]["modelSettings"]["credential_profile"] == "primary"
        saved_backup = host._run(backup.run_id)["body"]["profile"]
        assert saved_backup["modelSettings"]["credential_profile"] == "backup"
        assert saved_backup["modelSettings"]["quota_domain"] == "groq-selected"
        assert saved_backup["selectedModel"] == host._run(primary.run_id)["body"]["profile"]["selectedModel"]
        assert saved_backup["reasoningSettings"]["reasoning_effort"] == "high"
        drain(host, backup.run_id)
        assert host._run(backup.run_id)["body"]["status"] == "completed"
        receipts = [
            item for item in host.store.records("model_response_receipt")
            if item["body"].get("mission_id") == host._run(backup.run_id)["body"]["mission_id"]
        ]
        assert len(receipts) == 1
        assert receipts[0]["body"]["credential_profile"] == "backup"
        assert receipts[0]["body"]["quota_domain"] == "groq-selected"
        with pytest.raises(ValueError, match="different follow-up settings"):
            host.followup(LiveFollowupRequest(
                request_id="profile-backup-child", credential_profile="primary", **common,
            ))
        with pytest.raises(ValueError, match="different follow-up settings"):
            host.followup(LiveFollowupRequest(
                request_id="profile-backup-child", **common,
            ))
        backup_start = LiveStartRequest(
            request_id="profile-backup-start", question="Synthetic backup start",
            agent_ids=["agent-synthesis"], credential_profile="backup",
        )
        host.start(backup_start)
        assert host.start(backup_start).reused is True
        with pytest.raises(ValueError, match="different mission settings"):
            host.start(LiveStartRequest(
                request_id="profile-backup-start", question="Synthetic backup start",
                agent_ids=["agent-synthesis"],
            ))
    finally:
        host.close()


def test_new_role_names_are_defaults_while_saved_identity_and_role_stay_separate(
    tmp_path, monkeypatch
):
    host = service(
        tmp_path, monkeypatch,
        lambda request, *, timeout: pytest.fail("Naming must not call a provider"),
    )
    expected = {
        "literature_acquisition": "Nova",
        "evidence_preparation_synthesis": "Sage",
        "independent_methods_reviewer_critic": "Mxra",
        "questions_hypotheses_ranking": "Iris",
        "experimental_design": "Aria",
        "data_management": "Remy",
        "analysis": "Quinn",
        "project_collaboration_planning": "Orion",
        "shared_research_memory_completion": "Zora",
    }
    try:
        aliases = list(host.role_choices)
        assert {role_id for _name, role_id in host.role_choices.values()} == set(expected)
        created = host.start(LiveStartRequest(
            request_id="named-roster", question="Synthetic roster task", agent_ids=aliases,
        ))
        saved = {}
        for alias in aliases:
            control = host._control(created.run_id, alias)
            actor = host.store.get("agent_instance", control["body"]["actor_id"], 1)
            role_id = actor["body"]["role_id"]
            name = actor["body"]["display_name"]
            assert name == expected[role_id]
            assert not any(letter in name.lower() for letter in "fl")
            assert host._configuration(control)["body"]["instructions"].startswith(
                f"You are {name}, a {role_id.replace('_', ' ')} peer."
            )
            saved[alias] = name
        summary = next(item for item in host.state(created.run_id).runs if item.id == created.run_id)
        assert summary.requested_agent_names == saved

        # A previously saved name remains bound to its own ID after defaults
        # change; invitations resolve the requested actor from the exact control.
        critic_alias = "agent-critique"
        role_id = host.role_choices[critic_alias][1]
        host.role_choices[critic_alias] = ("Mira", role_id)
        host.work.role_choices[critic_alias] = ("Mira", role_id)
        legacy_request = LiveStartRequest(
            request_id="saved-old-name", question="Synthetic historical task",
            agent_ids=[critic_alias],
        )
        historical = host.start(legacy_request)
        old_control = host._control(historical.run_id, critic_alias)
        old_id = old_control["body"]["actor_id"]
        host.role_choices[critic_alias] = ("Mxra", role_id)
        host.work.role_choices[critic_alias] = ("Mxra", role_id)
        assert host.start(legacy_request).reused is True
        assert host._control(historical.run_id, critic_alias)["body"]["actor_id"] == old_id
        assert host.store.get("agent_instance", old_id, 1)["body"]["display_name"] == "Mira"
        assert host._configuration(old_control)["body"]["instructions"].startswith(
            "You are Mira, a independent methods reviewer critic peer."
        )
        recipient, requested = host.work._recipient(
            host._run(historical.run_id), critic_alias, "synthetic-invitation"
        )
        assert requested and recipient["id"] == old_id
        historical_state = host.state(historical.run_id)
        historical_summary = next(
            item for item in historical_state.runs if item.id == historical.run_id
        )
        assert historical_summary.requested_agent_names[critic_alias] == "Mira"
        assert next(
            item for item in historical_state.agents if item.id == critic_alias
        ).name == "Mxra"  # New-agent picker uses current defaults.
        assert next(
            item for item in historical_state.snapshot.agents if item.id == old_id
        ).name == "Mira"  # Saved-run history uses the immutable instance.
    finally:
        host.close()


def test_operator_profile_hold_preserves_queued_primary_call_while_backup_runs(
    tmp_path, monkeypatch
):
    monkeypatch.setenv("GROQ_API_KEY_BACKUP", "synthetic-backup-token")
    requests = []

    def transport(request, *, timeout):
        requests.append(json.loads(request.data))
        return answer("Synthetic scoped answer.", len(requests))

    host = service(tmp_path, monkeypatch, transport)
    try:
        primary = host.start(LiveStartRequest(
            request_id="operator-held-primary", question="Synthetic primary task",
            agent_ids=["agent-synthesis"],
        ))
        control = host._control(primary.run_id, "agent-synthesis")
        assert host._schedule_model(control)
        queued = host._control(primary.run_id, "agent-synthesis")["body"]
        assert queued["phase"] == "pending_model"
        intent_id = f"model-intent:{queued['call_ref']['id']}:{queued['call_ref']['version']}"
        assert host.store.get("model_request_intent", intent_id)

        monkeypatch.setattr(
            host.credential_profiles, "is_enabled", lambda profile: profile == "backup"
        )
        host._drive_run(host._run(primary.run_id))
        held = host._control(primary.run_id, "agent-synthesis")["body"]
        assert held["phase"] == "waiting"
        assert held["waiting_reason"] == "Primary credential paused by operator"
        assert held["operator_credential_hold"]["phase"] == "pending_model"
        assert held["work_id"] == queued["work_id"]
        assert held["call_ref"] == queued["call_ref"]
        prepared_call = host.store.get(**queued["call_ref"])
        attempt_id = prepared_call["body"]["attempt_id"]
        assert host.store.get("agent_attempt", attempt_id)["body"]["status"] == "running"
        held_snapshot = host.state(primary.run_id).snapshot
        held_attempt = next(item for item in held_snapshot.attempts if item.id == attempt_id)
        assert held_attempt.status == "waiting"
        held_provenance = next(item for item in held_snapshot.inspection.attempts
                               if item.attempt_id == attempt_id)
        assert held_provenance.explanation.startswith("Operator paused this prepared model call.")
        assert host.store.get("model_response_receipt", f"model-response:{intent_id}") is None
        for _ in range(3):
            host._drive_run(host._run(primary.run_id))
        assert requests == []

        backup = host.start(LiveStartRequest(
            request_id="operator-enabled-backup", question="Synthetic backup task",
            agent_ids=["agent-synthesis"], credential_profile="backup",
        ))
        drain(host, backup.run_id)
        assert host._run(backup.run_id)["body"]["status"] == "completed"
        assert len(requests) == 1
        assert host.store.get("model_request_intent", intent_id)

        monkeypatch.setattr(host.credential_profiles, "is_enabled", lambda _profile: True)
        drain(host, primary.run_id)
        assert host._run(primary.run_id)["body"]["status"] == "completed", (
            host._control(primary.run_id, "agent-synthesis")["body"]["phase"],
            host._control(primary.run_id, "agent-synthesis")["body"].get("waiting_reason"),
            len(requests),
        )
        assert len(requests) == 2
        assert host._control(primary.run_id, "agent-synthesis")["body"]["call_ref"] == queued["call_ref"]
        assert len([
            item for item in host.store.records("model_request_intent")
            if item["body"].get("mission_id") == host._run(primary.run_id)["body"]["mission_id"]
        ]) == 1
    finally:
        host.close()


def test_verified_distinct_quota_domains_do_not_share_headers_or_reservations(
    tmp_path, monkeypatch
):
    import ora.mission.service as service_module

    class VerifiedProfiles:
        domains = {"primary": "groq-primary-test", "backup": "groq-backup-test"}

        def __init__(self, _root):
            pass

        def selection(self, profile_id="primary"):
            return {"credential_profile": profile_id,
                    "quota_domain": self.domains[profile_id]}

        def scopes(self):
            return ("groq-selected", *self.domains.values())

        def validate_binding(self, profile_id, quota_domain):
            if self.domains[profile_id] != quota_domain:
                raise ValueError("Saved profile/domain binding differs")

        def is_enabled(self, _profile_id):
            return True

        def is_run_enabled(self, profile_id, _run_id):
            return self.is_enabled(profile_id)

    monkeypatch.setattr(service_module, "GroqCredentialProfiles", VerifiedProfiles)
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-primary-token")
    monkeypatch.setenv("GROQ_API_KEY_BACKUP", "synthetic-backup-token")

    def transport(request, *, timeout):
        result = answer("Synthetic answer.", 1)
        result.headers = {
            "x-request-id": "offline-profile-fixture",
            "x-ratelimit-limit-tokens": "8000",
            "x-ratelimit-remaining-tokens": "7000",
            "x-ratelimit-reset-tokens": "1m",
        }
        return result

    store = StateStore(tmp_path)
    adapter = GroqSelectedAdapter(ROOT, transport=transport)
    adapter.credential_profiles = VerifiedProfiles(ROOT)
    session = ModelSession(store, ROOT, adapter=adapter)
    host = MissionService(
        ROOT, tmp_path, model_session=session, execution_mode="offline_test",
        start_supervisor=False,
    )
    try:
        primary = host.start(LiveStartRequest(
            request_id="domain-primary", question="Synthetic primary",
            agent_ids=["agent-synthesis"],
        ))
        backup = host.start(LiveStartRequest(
            request_id="domain-backup", question="Synthetic backup",
            agent_ids=["agent-synthesis"], credential_profile="backup",
        ))
        drain(host, primary.run_id)
        assert host._effective_input_budget(
            host._control(backup.run_id, "agent-synthesis")
        ) == {}  # Primary's observed input ceiling cannot size backup work.
        drain(host, backup.run_id)
        scopes = {
            tuple(item["body"]["quota_scopes"])
            for item in host.store.records("capacity_operation")
            if item["body"].get("kind") == "model"
        }
        assert scopes == {("groq-primary-test",), ("groq-backup-test",)}
        receipts = host.store.records("model_response_receipt")
        assert {item["body"]["quota_domain"] for item in receipts} == {
            "groq-primary-test", "groq-backup-test",
        }
        assert host.store.get("capacity_quota", "groq-primary-test")
        assert host.store.get("capacity_quota", "groq-backup-test")
    finally:
        host.close()


def test_incompatible_workflow_stays_inspectable_without_blocking_compatible_sibling(
    tmp_path, monkeypatch
):
    calls = []

    def transport(request, *, timeout):
        calls.append(json.loads(request.data))
        return answer("A synthetic sibling result.", len(calls))

    host = service(tmp_path, monkeypatch, transport)
    incompatible = host.start(LiveStartRequest(
        request_id="workflow-future", question="Future saved workflow",
        agent_ids=["agent-synthesis"],
    ))
    compatible = host.start(LiveStartRequest(
        request_id="workflow-compatible", question="Compatible saved workflow",
        agent_ids=["agent-synthesis"],
    ))

    def revise(tx):
        old = tx.get("mission_run", incompatible.run_id)
        tx.put("mission_run", old["id"], {
            **old["body"], "workflow_version": "unknown-future-workflow",
        }, expected_version=old["version"])
        old = tx.get("mission_run", compatible.run_id)
        tx.put("mission_run", old["id"], {
            key: value for key, value in old["body"].items() if key != "workflow_version"
        }, expected_version=old["version"])
        return {"updated": True}

    host.store.transact("fixture-workflow-versions", {}, revise)
    host.close()
    store = StateStore(tmp_path)
    session = ModelSession(store, ROOT, adapter=GroqSelectedAdapter(ROOT, transport=transport))
    reopened = MissionService(
        ROOT, tmp_path, model_session=session, execution_mode="offline_test",
        start_supervisor=True,
    )
    try:
        for _ in range(200):
            diagnostics = [
                item for item in reopened.store.records("mission_host_diagnostic")
                if item["body"].get("run_id") == incompatible.run_id
            ]
            if diagnostics and reopened._run(compatible.run_id)["body"]["status"] == "completed":
                break
            time.sleep(0.01)
        assert diagnostics and diagnostics[0]["body"]["error_class"] == "StateError"
        assert reopened._run(incompatible.run_id)["body"]["status"] == "active"
        assert reopened.state(incompatible.run_id).snapshot is not None
        assert reopened.store.records("model_response_receipt") and len(calls) == 1
        assert not any(
            item["body"].get("mission_id")
            == reopened._run(incompatible.run_id)["body"]["mission_id"]
            for item in reopened.store.records("agent_call")
        )
        with pytest.raises(ValueError, match="compatible migration"):
            reopened.start(LiveStartRequest(
                request_id="workflow-future", question="Future saved workflow",
                agent_ids=["agent-synthesis"],
            ))
    finally:
        reopened.close()


def test_projection_upgrade_preserves_history_and_exact_followup_parent(tmp_path, monkeypatch):
    from ora.mission import projection
    from ora.state.inspection import load_snapshot

    calls = []

    def transport(request, *, timeout):
        calls.append(json.loads(request.data))
        return answer("A provisional saved contribution.", len(calls))

    host = service(tmp_path, monkeypatch, transport)
    parent_receipt = host.start(
        LiveStartRequest(
            request_id="projection-parent",
            question="Inspect supplied scope",
            agent_ids=["agent-synthesis"],
        )
    )
    drain(host, parent_receipt.run_id)
    parent = host.state(parent_receipt.run_id).snapshot
    original = load_snapshot(host.store, parent.snapshot_id)
    report = parent.artifacts[0]
    child = host.followup(
        LiveFollowupRequest(
            request_id="projection-child",
            parent_run_id=parent_receipt.run_id,
            parent_snapshot_id=parent.snapshot_id,
            target_id=report.id,
            target_version=report.version,
            question="Reconsider that exact answer",
            agent_ids=["agent-critique"],
        )
    )
    monkeypatch.setattr(projection, "PROJECTION_VERSION", "test-upgraded-projection")
    reopened = service(tmp_path, monkeypatch, transport)
    new_parent = reopened.state(parent_receipt.run_id).snapshot
    assert new_parent.snapshot_id != parent.snapshot_id
    assert load_snapshot(reopened.store, parent.snapshot_id) == original
    assert reopened.state(child.run_id).comparison_snapshot.snapshot_id == parent.snapshot_id
    assert (
        reopened.store.get("mission_snapshot_head", parent_receipt.run_id)["body"][
            "projection_version"
        ]
        == "test-upgraded-projection"
    )
    assert len(calls) == 1
    assert len(reopened.store.records("agent_call")) == 1


def test_one_projection_refresh_failure_does_not_hide_other_runs(tmp_path, monkeypatch):
    import ora.mission.service as service_module
    from ora.mission import projection
    from ora.state import StateError

    host = service(tmp_path, monkeypatch, lambda request, timeout: answer("Recorded", 1))
    receipts = [
        host.start(
            LiveStartRequest(
                request_id=f"view-{index}",
                question=f"Question {index}",
                agent_ids=["agent-synthesis"],
            )
        )
        for index in range(2)
    ]
    originals = {r.run_id: host.state(r.run_id).snapshot.snapshot_id for r in receipts}
    monkeypatch.setattr(projection, "PROJECTION_VERSION", "test-repaired-projection")
    original_persist = service_module.persist_snapshot

    def controlled_failure(store, run, controls):
        if run["id"] == receipts[0].run_id:
            raise StateError("Injected damaged local view dependency")
        return original_persist(store, run, controls)

    monkeypatch.setattr(service_module, "persist_snapshot", controlled_failure)
    reopened = service(tmp_path, monkeypatch, lambda request, timeout: answer("Unused", 1))
    assert reopened.state(receipts[0].run_id).snapshot.snapshot_id == originals[receipts[0].run_id]
    assert reopened.state(receipts[1].run_id).snapshot.snapshot_id != originals[receipts[1].run_id]
    assert len(reopened.store.records("mission_host_diagnostic")) == 1
    assert not reopened.store.records("agent_call")


def test_two_model_tool_turns_use_saved_result_and_new_permits(tmp_path, monkeypatch):
    requests = []

    def transport(request, *, timeout):
        body = json.loads(request.data)
        requests.append(body)
        if len(requests) == 1:
            return tool("discover_capabilities", {}, 1)
        return tool(
            "publish_result",
            {
                "title": "Scoped output",
                "text": "No source was supplied.",
                "citations": [],
                "limitations": ["No source"],
            },
            2,
        )

    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(
        LiveStartRequest(
            request_id="mission-tools", question="Assess scope", agent_ids=["agent-synthesis"]
        )
    )
    drain(host, receipt.run_id)
    snapshot = host.state(receipt.run_id).snapshot
    assert snapshot.mission.status == "completed"
    assert len(host.calls(receipt.run_id)) == 2
    assert len(host.store.records("executor_receipt")) == 4  # model and tool each settle
    assert len(snapshot.artifacts) == 1
    assert requests[0]["model"] == requests[1]["model"] == MODEL
    assert requests[0]["reasoning_effort"] == requests[1]["reasoning_effort"] == "high"
    assert any(
        item["role"] == "tool" and "discover_capabilities" in item["content"]
        for item in requests[1]["messages"]
    )


def test_next_model_turn_waits_for_observed_token_reset_without_a_429(tmp_path, monkeypatch):
    dispatched_at = []

    def transport(request, *, timeout):
        dispatched_at.append(time.monotonic())
        if len(dispatched_at) == 1:
            response = tool("discover_capabilities", {}, 1)
            response.headers = {
                "x-ratelimit-remaining-requests": "999",
                "x-ratelimit-reset-requests": "1h",
                "x-ratelimit-limit-tokens": "8000",
                "x-ratelimit-remaining-tokens": "1",
                "x-ratelimit-reset-tokens": "0.08s",
            }
            return response
        return tool(
            "publish_result",
            {
                "title": "Scoped output",
                "text": "No source was supplied.",
                "citations": [],
                "limitations": ["No source"],
            },
            2,
        )

    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(
        LiveStartRequest(
            request_id="mission-observed-headroom",
            question="Assess scope",
            agent_ids=["agent-synthesis"],
        )
    )
    drain(host, receipt.run_id)
    assert host.state(receipt.run_id).snapshot.mission.status == "completed"
    assert len(dispatched_at) == 2
    assert dispatched_at[1] - dispatched_at[0] >= 0.06
    rate_receipts = [
        item for item in host.store.records("model_response_receipt")
        if item["body"]["rate_headers"].get("x-ratelimit-remaining-tokens") == "1"
    ]
    assert len(rate_receipts) == 1
    assert host.store.get("capacity_quota", "groq-selected")["body"]["tokens_reset_at"] > 0
    model_reservations = [
        item["body"]["estimated_tokens"]
        for item in host.store.records("capacity_operation")
        if item["body"]["kind"] == "model"
    ]
    assert len(model_reservations) == 2
    assert all(0 < reserved < 8192 for reserved in model_reservations)
    budgets = host.store.records("mission_call_budget")
    assert sorted(item["body"]["observed_account_limit"] or 0 for item in budgets) == [0, 8000]
    assert all(item["status"] == "tool_requested" for item in host.calls(receipt.run_id))


def test_fast_peer_next_model_turn_proceeds_while_sibling_call_is_held(tmp_path, monkeypatch):
    slow_started = threading.Event()
    release_slow = threading.Event()
    fast_second = threading.Event()
    sage_calls = 0

    def transport(request, *, timeout):
        nonlocal sage_calls
        body = json.loads(request.data)
        if "Mxra" in body["messages"][0]["content"]:
            slow_started.set()
            assert release_slow.wait(5)
            return answer("Mxra's independent contribution.", 30)
        sage_calls += 1
        if sage_calls == 1:
            assert slow_started.wait(5)
            return tool("discover_capabilities", {}, 1)
        fast_second.set()
        return answer("Sage's second model turn completed.", 31)

    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    store = StateStore(tmp_path)
    session = ModelSession(store, ROOT, adapter=GroqSelectedAdapter(ROOT, transport=transport))
    host = MissionService(
        ROOT, tmp_path, model_session=session, execution_mode="offline_test", start_supervisor=True
    )
    try:
        receipt = host.start(
            LiveStartRequest(
                request_id="mission-fast-while-slow",
                question="Two independent contributions",
                agent_ids=["agent-synthesis", "agent-critique"],
            )
        )
        assert fast_second.wait(4), "Fast peer's next model turn waited for the slow sibling"
        assert slow_started.is_set() and not release_slow.is_set()
        assert host._control(receipt.run_id, "agent-synthesis")["body"]["phase"] in {
            "pending_model",
            "done",
        }
        release_slow.set()
        deadline = time.monotonic() + 4
        while time.monotonic() < deadline:
            if host.state(receipt.run_id).snapshot.mission.status == "completed":
                break
            time.sleep(0.02)
        assert host.state(receipt.run_id).snapshot.mission.status == "completed"
    finally:
        release_slow.set()
        host.close()


def test_unchanged_quota_wait_does_not_append_control_versions(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda request, *, timeout: answer("unused", 1))
    try:
        receipt = host.start(
            LiveStartRequest(
                request_id="mission-unchanged-quota-wait",
                question="Wait for the saved provider capacity",
                agent_ids=["agent-synthesis"],
            )
        )
        control = host._control(receipt.run_id, "agent-synthesis")
        assert host._schedule_model(control)
        monkeypatch.setattr(host.capacity, "get", lambda _operation_id: {"phase": "waiting"})
        first = host._control(receipt.run_id, "agent-synthesis")
        assert host._advance(first) is True
        waiting = host._control(receipt.run_id, "agent-synthesis")
        assert waiting["version"] == first["version"] + 1
        assert waiting["body"]["waiting_reason"] == "Provider quota or resource wait"
        for _ in range(8):
            assert host._advance(host._control(receipt.run_id, "agent-synthesis")) is False
        current = host._control(receipt.run_id, "agent-synthesis")
        assert current["version"] == waiting["version"]
        assert current["body"] == waiting["body"]
    finally:
        host.close()


def test_cancelled_run_is_not_reopened_by_driver_or_peer_activation(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch,
                   lambda *_a, **_kw: pytest.fail("Cancelled mission dispatched a call"))
    try:
        run = host.start(LiveStartRequest(
            request_id="cancelled-continuation", question="Retain unfinished work",
            agent_ids=["agent-synthesis"],
        ))
        saved = host._run(run.run_id)
        host._save("mission_run", saved["id"], {**saved["body"], "status": "cancelled"},
                   mission_id=saved["body"]["mission_id"])
        cancelled = host._run(run.run_id)
        before = host._controls(run.run_id)
        assert host._drive_run(saved) == (False, None)  # Stale caller cannot reopen it.
        assert host._activate_peer_obligations(cancelled) is False
        assert host._maybe_complete(cancelled) is False
        assert host._controls(run.run_id) == before
        assert host.store.records("model_request_intent") == []
        assert host._run(run.run_id) == cancelled
    finally:
        host.close()


@pytest.mark.parametrize("completion", ["direct", "tool"])
def test_accepted_report_clears_stale_operational_wait(tmp_path, monkeypatch, completion):
    response = (
        answer("A bounded direct result.", 1)
        if completion == "direct"
        else tool(
            "publish_result",
            {
                "title": "Bounded tool result",
                "text": "A checked contribution.",
                "citations": [],
                "limitations": ["Synthetic fixture"],
            },
            1,
        )
    )
    host = service(tmp_path, monkeypatch, lambda request, *, timeout: response)
    try:
        receipt = host.start(
            LiveStartRequest(
                request_id=f"mission-clear-wait-{completion}",
                question="Complete a bounded contribution",
                agent_ids=["agent-synthesis"],
            )
        )
        mission_id = host._run(receipt.run_id)["body"]["mission_id"]
        control = host._control(receipt.run_id, "agent-synthesis")
        assert host._schedule_model(control)
        asyncio.run(host.executor.run_ready(mission_id))
        control = host._control(receipt.run_id, "agent-synthesis")
        if completion == "tool":
            assert host._advance(control)
            control = host._control(receipt.run_id, "agent-synthesis")
            assert host._schedule_tool(control)
            asyncio.run(host.executor.run_ready(mission_id))
            control = host._control(receipt.run_id, "agent-synthesis")
        host._update_control(
            control,
            waiting_reason="Provider quota or resource wait",
            retry_at=1234567890.0,
        )
        assert host._advance(host._control(receipt.run_id, "agent-synthesis"))
        done = host._control(receipt.run_id, "agent-synthesis")["body"]
        assert done["phase"] == "done"
        assert done["waiting_reason"] is None
        assert done["retry_at"] is None
        assert done["report_ref"] is not None
        host._update_control(
            host._control(receipt.run_id, "agent-synthesis"),
            waiting_reason="Historical stale quota note",
            retry_at=1234567890.0,
        )
        summary = next(
            item for item in host.state(receipt.run_id).runs if item.id == receipt.run_id
        )
        assert summary.waiting_reason is None
    finally:
        host.close()


def test_replacing_undispatched_preparation_supersedes_its_attempt(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda request, *, timeout: answer("unused", 1))
    try:
        receipt = host.start(
            LiveStartRequest(
                request_id="mission-superseded-preparation",
                question="Retry a prepared model call",
                agent_ids=["agent-synthesis"],
            )
        )
        control = host._control(receipt.run_id, "agent-synthesis")
        config = host._configuration(control)
        old_call_id, old_attempt_id = host._model_ids(control, 1)
        old_call = host.configurations.start_call(
            f"mission-call:{old_call_id}",
            call_id=old_call_id,
            configuration_id=config["id"],
            mission_id=control["body"]["mission_id"],
            task_id=control["body"]["task_id"],
            attempt_id=old_attempt_id,
            loaded_skill_ids=list(config["body"]["skills"]),
            exposed_tool_ids=host._exposed_tools(control, config)[0],
            require_common_tools=False,
        )
        host._save(
            "agent_attempt",
            old_attempt_id,
            {
                "schema": "ora.mission_attempt.v1",
                "mission_id": control["body"]["mission_id"],
                "run_id": receipt.run_id,
                "task_id": control["body"]["task_id"],
                "agentId": control["body"]["actor_id"],
                "call_ref": ref(old_call),
                "configuration_ref": ref(config),
                "startedAt": "2026-10-09T00:00:00Z",
                "status": "running",
                "endedAt": None,
                "report_ref": None,
            },
            mission_id=control["body"]["mission_id"],
        )
        host._update_control(control, call_ref=ref(old_call), sequence=1)
        assert host._schedule_model(host._control(receipt.run_id, "agent-synthesis"))
        old_attempt = host.store.get("agent_attempt", old_attempt_id)["body"]
        next_call = host._control(receipt.run_id, "agent-synthesis")["body"]["call_ref"]
        assert old_attempt["status"] == "superseded"
        assert old_attempt["status_basis"] == "prepared_call_replaced_before_dispatch"
        assert old_attempt["successor_call_ref"] == next_call
        assert old_attempt["endedAt"] is not None
        assert host.store.get("agent_attempt", old_attempt_id, 1)["body"]["status"] == "running"
        assert host.store.records("model_dispatch_attempt") == []
    finally:
        host.close()


def test_dispatch_notification_sees_matching_control_for_model_and_tool(tmp_path, monkeypatch):
    calls = []

    def transport(request, *, timeout):
        calls.append(request)
        if len(calls) == 1:
            return tool("discover_capabilities", {}, 1)
        return answer("A bounded answer after tool discovery.", 2)

    host = service(tmp_path, monkeypatch, transport)
    original_notify = host.executor.notify
    observed_handlers = set()

    def inspect_notify(mission_id):
        for work in host.executor._work(mission_id):
            if work["body"]["phase"] != "pending":
                continue
            spec = host.executor._spec_from(work)
            matching = [
                control
                for control in host._controls(spec.run_id)
                if control["body"].get("work_id") == spec.work_id
                and control["body"].get("actor_id") == spec.actor_id
            ]
            assert len(matching) == 1
            assert matching[0]["body"]["phase"] == (
                "pending_model" if spec.kind == "model" else "pending_tool"
            )
            observed_handlers.add(spec.handler_id)
        original_notify(mission_id)

    monkeypatch.setattr(host.executor, "notify", inspect_notify)
    receipt = host.start(
        LiveStartRequest(
            request_id="mission-atomic-work-control",
            question="Answer after discovering available tools",
            agent_ids=["agent-synthesis"],
        )
    )
    drain(host, receipt.run_id)
    assert host._run(receipt.run_id)["body"]["status"] == "completed"
    assert observed_handlers == {"mission-model", "mission-tool"}


def test_requested_tool_exposure_survives_host_restart(tmp_path, monkeypatch):
    requests = []

    def transport(request, *, timeout):
        body = json.loads(request.data)
        requests.append(body)
        count = len(requests)
        if count == 1:
            return tool("discover_capabilities", {}, count)
        if count == 2:
            return tool("request_capability", {"tool_id": "write_note"}, count)
        if count == 3:
            return tool(
                "write_note",
                {
                    "note_id": "selected-tool-note",
                    "category": "note",
                    "text": "A saved agent-selected note",
                    "expected_version": 0,
                },
                count,
            )
        return tool(
            "publish_result",
            {
                "title": "Bounded result",
                "text": "The note was saved.",
                "citations": [],
                "limitations": ["No source supplied"],
            },
            count,
        )

    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(
        LiveStartRequest(
            request_id="mission-progressive-tools",
            question="Choose an extra tool",
            agent_ids=["agent-synthesis"],
        )
    )
    for _ in range(150):
        host._drive_run(host._run(receipt.run_id))
        control = host._control(receipt.run_id, "agent-synthesis")["body"]
        if control.get("requested_capabilities") == ["write_note"]:
            break
        time.sleep(0.01)
    assert control["phase"] == "ready_model"
    assert len(requests) == 2
    initial_names = {item["function"]["name"] for item in requests[0]["tools"]}
    assert "write_note" not in initial_names
    assert {
        "search_scholarly",
        "publish_result",
        "peer_request",
        "request_capability",
    } <= initial_names
    assert len(initial_names) < len(
        host._configuration(host._control(receipt.run_id, "agent-synthesis"))["body"][
            "permitted_tools"
        ]
    )
    messages = [
        json.loads(host.store.read_blob(item["body"]["blob"]["sha256"]))
        for item in host.store.records("context_tool_result")
    ]
    listed = json.loads(
        next(item["content"] for item in messages if item["name"] == "discover_capabilities")
    )
    assert listed["granted"]["write_note"] and "claim_work" in listed["not_handled_here"]
    assert len(host.store.records("mission_capability_selection")) == 1

    host.close()
    resumed = service(tmp_path, monkeypatch, transport)
    drain(resumed, receipt.run_id, limit=20)
    assert resumed.state(receipt.run_id).snapshot.mission.status == "completed"
    assert "write_note" in {item["function"]["name"] for item in requests[2]["tools"]}
    assert requests[2]["model"] == requests[0]["model"]
    assert requests[2]["reasoning_effort"] == requests[0]["reasoning_effort"] == "high"
    assert resumed.store.get("working_note", "selected-tool-note") is not None


def test_bad_tool_action_is_local_and_other_peer_can_finish(tmp_path, monkeypatch):
    lock = threading.Lock()
    counts = {"synthesis": 0, "critique": 0}
    requests = []

    def transport(request, *, timeout):
        body = json.loads(request.data)
        requests.append(body)
        instructions = body["messages"][0]["content"]
        assert instructions.startswith(("You are Sage,", "You are Mxra,"))
        role = "critique" if instructions.startswith("You are Mxra,") else "synthesis"
        with lock:
            counts[role] += 1
            count = counts[role]
        if role == "synthesis" and count == 1:
            return tool("inspect_hit", {"hit_id": "missing-hit"}, 1)
        return answer(f"{role} gives a bounded answer.", 10 + count)

    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(
        LiveStartRequest(
            request_id="mission-invalid-tool",
            question="Independent review",
            agent_ids=["agent-synthesis", "agent-critique"],
        )
    )
    drain(host, receipt.run_id)
    snapshot = host.state(receipt.run_id).snapshot
    assert snapshot.mission.status == "completed"
    assert len(snapshot.artifacts) == 2
    assert counts == {"synthesis": 2, "critique": 1}
    tool_result = host.store.records("context_tool_result")[0]
    message = json.loads(host.store.read_blob(tool_result["body"]["blob"]["sha256"]))
    invalid = json.loads(message["content"])
    assert invalid["status"] == "rejected"
    assert invalid["reason"] == "Tool is not actor-granted"
    assert "inspect_hit" not in {
        item["function"]["name"] for item in requests[0]["tools"]
    }


def test_search_inspect_and_cited_publish_use_checked_copy(tmp_path, monkeypatch):
    requests = []

    def model_transport(request, *, timeout):
        body = json.loads(request.data)
        requests.append(body)
        if len(requests) == 1:
            return tool(
                "search_scholarly",
                {"route": "europe_pmc.search", "query": "synthetic trial", "limit": 1},
                1,
            )
        prior = json.loads(
            next(item["content"] for item in reversed(body["messages"]) if item["role"] == "tool")
        )
        if len(requests) == 2:
            return tool("inspect_hit", {"hit_id": prior["hits"][0]["hit_id"]}, 2)
        assert prior["status"] == "inspected"
        source_id = prior["source_id"]
        span_id = prior["passages"][0]["span_id"]
        return tool(
            "publish_result",
            {
                "title": "Synthetic finding",
                "text": f"The abstract reports improvement {source_id}::{span_id}.",
                "citations": [{"source_id": source_id, "span_id": span_id}],
                "limitations": ["Synthetic fixture"],
            },
            3,
        )

    def discovery_transport(method, url, *, body=None, headers=None):
        packet = {
            "hitCount": 1,
            "nextCursorMark": "*",
            "resultList": {
                "result": [
                    {
                        "source": "MED",
                        "id": "123",
                        "pmid": "123",
                        "doi": "10.1234/synthetic",
                        "title": "Synthetic study",
                        "abstractText": "DOI: 10.1234/synthetic. Symptoms improved at week 8.",
                        "isOpenAccess": "Y",
                    }
                ]
            },
        }
        return HTTPResult(200, json.dumps(packet).encode(), {})

    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    store = StateStore(tmp_path)
    session = ModelSession(
        store, ROOT, adapter=GroqSelectedAdapter(ROOT, transport=model_transport)
    )
    host = MissionService(
        ROOT,
        tmp_path,
        model_session=session,
        discovery_transport=discovery_transport,
        execution_mode="offline_test",
        start_supervisor=False,
    )
    receipt = host.start(
        LiveStartRequest(
            request_id="mission-search",
            question="What did the study report?",
            agent_ids=["agent-synthesis"],
        )
    )
    drain(host, receipt.run_id)
    snapshot = host.state(receipt.run_id).snapshot
    assert snapshot.mission.status == "completed"
    assert len(requests) == 3
    assert "inspect_hit" not in {
        item["function"]["name"] for item in requests[0]["tools"]
    }
    assert "inspect_hit" in {
        item["function"]["name"] for item in requests[1]["tools"]
    }
    search_tool = next(
        item for item in requests[0]["tools"] if item["function"]["name"] == "search_scholarly"
    )
    assert "Choose route and limit" in search_tool["function"]["description"]
    assert "search routes use query" in search_tool["function"]["description"]
    assert len(snapshot.sources) == 1
    assert snapshot.artifacts[0].citations[0].source_id == snapshot.sources[0].id
    assert len(host.store.records("source_use")) == 1
    assert any("source_packets" in json.dumps(item) for item in requests[2]["messages"])


@pytest.mark.parametrize("selection_mode", ["append", "replace"])
def test_source_window_is_model_selected_and_citable(tmp_path, monkeypatch, selection_mode):
    calls = []
    first_span = None

    def model_transport(request, *, timeout):
        nonlocal first_span
        body = json.loads(request.data)
        calls.append(body)
        count = len(calls)
        if count == 1:
            return tool(
                "search_scholarly",
                {
                    "route": "europe_pmc.search",
                    "query": "synthetic study",
                    "limit": 1,
                },
                count,
            )
        prior = json.loads(
            next(item["content"] for item in reversed(body["messages"]) if item["role"] == "tool")
        )
        if count == 2:
            return tool("inspect_hit", {"hit_id": prior["hits"][0]["hit_id"]}, count)
        if count == 3:
            return tool(
                "read_source_window",
                {
                    "source_id": prior["source_id"],
                    "source_version": 1,
                    "block_offset": 0,
                    "char_offset": 0,
                    "char_limit": 20,
                },
                count,
            )
        if count == 4:
            first_span = prior["passages"][0]["span_id"]
            return tool(
                "read_source_window",
                {
                    "source_id": prior["source_id"],
                    "source_version": 1,
                    "block_offset": prior["next_cursor"]["block_offset"],
                    "char_offset": prior["next_cursor"]["char_offset"],
                    "char_limit": 20,
                    "selection_mode": selection_mode,
                },
                count,
            )
        return tool(
            "publish_result",
            {
                "title": "Window finding",
                "text": "A checked window was read.",
                "citations": [{"source_id": prior["source_id"], "span_id": first_span}],
                "limitations": ["Synthetic abstract"],
            },
            count,
        )

    def discovery_transport(method, url, *, body=None, headers=None):
        packet = {
            "hitCount": 1,
            "nextCursorMark": "*",
            "resultList": {
                "result": [
                    {
                        "source": "MED",
                        "id": "123",
                        "pmid": "123",
                        "doi": "10.1234/window",
                        "title": "Synthetic window study",
                        "abstractText": "DOI: 10.1234/window. A longer synthetic result to inspect in windows.",
                        "isOpenAccess": "Y",
                    }
                ]
            },
        }
        return HTTPResult(200, json.dumps(packet).encode(), {})

    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    store = StateStore(tmp_path)
    session = ModelSession(
        store, ROOT, adapter=GroqSelectedAdapter(ROOT, transport=model_transport)
    )
    host = MissionService(
        ROOT,
        tmp_path,
        model_session=session,
        discovery_transport=discovery_transport,
        execution_mode="offline_test",
        start_supervisor=False,
    )
    receipt = host.start(
        LiveStartRequest(
            request_id=f"mission-window-{selection_mode}",
            question="Read a selected window",
            agent_ids=["agent-synthesis"],
        )
    )
    drain(host, receipt.run_id, limit=30)
    assert host._run(receipt.run_id)["body"]["status"] == "completed"
    windows = sorted(
        host.store.records("source_read_window"),
        key=lambda item: (item["body"]["block_offset"], item["body"]["char_offset"]),
    )
    assert len(windows) == 2
    control = host._control(receipt.run_id, "agent-synthesis")["body"]
    copy_id = windows[0]["body"]["copy_ref"]["id"]
    selected = control["selected_passage_refs"][copy_id]
    if selection_mode == "append":
        assert all(item["body"]["passage_refs"][0] in selected for item in windows)
    else:
        assert selected == windows[1]["body"]["passage_refs"]
    latest_intent = sorted(
        store.records("model_request_intent"), key=lambda item: item["created_at"]
    )[-1]
    current_refs = latest_intent["body"]["source_decisions"][0]["passage_refs"]
    assert windows[1]["body"]["passage_refs"][0] in current_refs
    assert (windows[0]["body"]["passage_refs"][0] in current_refs) == (selection_mode == "append")
    report = host.store.records("report")[0]
    assert report["body"]["citation_input_provenance"][0]["input_status"] == (
        "exact_passage_in_producing_call"
        if selection_mode == "append"
        else "checked_passage_outside_producing_call"
    )
    assert (
        host.state(receipt.run_id).snapshot.artifacts[0].citations[0].span_id.startswith("window-")
    )


def test_large_supplied_source_recovers_with_exact_window(tmp_path, monkeypatch):
    calls = []
    source_ref = None

    def transport(request, *, timeout):
        body = json.loads(request.data)
        calls.append(body)
        if len(calls) == 1:
            return tool(
                "read_source_window",
                {
                    "source_id": source_ref["id"],
                    "source_version": source_ref["version"],
                    "block_offset": 0,
                    "char_offset": 0,
                    "char_limit": 1000,
                    "selection_mode": "replace",
                },
                1,
            )
        return tool(
            "publish_result",
            {
                "title": "Bounded reading",
                "text": "The selected window supports a qualified answer.",
                "citations": [],
                "limitations": ["Only one window was read"],
            },
            2,
        )

    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(
        LiveStartRequest(
            request_id="mission-large-supplied-source",
            question="Read a bounded part of the supplied document",
            agent_ids=["agent-synthesis"],
            evidence_text="Synthetic source passage with enough content to inspect. " * 1600,
        )
    )
    control = host._control(receipt.run_id, "agent-synthesis")
    source_ref = control["body"]["source_refs"][0]
    host._update_control(
        control,
        input_budget={"observed_limit_tokens": 17000, "target_fraction": 0.70},
    )
    drain(host, receipt.run_id, limit=24)
    assert host._run(receipt.run_id)["body"]["status"] == "completed", (
        host._control(receipt.run_id, "agent-synthesis")["body"]["waiting_reason"],
        len(calls),
        [
            item["body"].get("source_turn_mode")
            for item in host.store.records("mission_call_budget")
        ],
    )
    assert len(calls) == 2
    budgets = host.store.records("mission_call_budget")
    assert any(item["body"]["source_turn_mode"] == "source_free_recovery" for item in budgets)
    assert any(item["body"]["source_turn_mode"] == "all_allowed_sources_fit" for item in budgets)
    first_budget = next(
        item for item in budgets if item["body"]["source_turn_mode"] == "source_free_recovery"
    )
    first_call = host.store.get(**first_budget["body"]["call_ref"])
    assert first_call["body"]["input_refs"] == []
    assert "source_free_recovery" in json.dumps(calls[0]["messages"])
    assert "Synthetic source passage" in json.dumps(calls[1]["messages"])


def test_window_preview_pressure_still_prepares_exact_checked_source_input(
    tmp_path, monkeypatch
):
    requests = []
    source_ref = None

    def transport(request, *, timeout):
        body = json.loads(request.data)
        requests.append(body)
        if len(requests) == 1:
            return tool("read_source_window", {
                "source_id": source_ref["id"], "source_version": source_ref["version"],
                "block_offset": 0, "char_offset": 0, "char_limit": 1600,
                "selection_mode": "replace",
            }, 1)
        if len(requests) == 2:
            prior = json.loads(next(
                item["content"] for item in reversed(body["messages"])
                if item["role"] == "tool"
            ))
            assert prior["next_request"]["char_offset"] == 1600
            return tool("read_source_window", {
                **prior["next_request"], "char_limit": 800,
                "selection_mode": "append",
            }, 2)
        return answer("The remaining text has not yet been checked.", 3)

    prefix = "DOI: 10.1234/window. "
    source_text = prefix + "A" * (1600 - len(prefix)) + "B" * 800 + "C" * 261
    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="window-preview-pressure", question="Read the synthetic abstract",
            evidence_text=source_text, agent_ids=["agent-synthesis"],
        ))
        source_ref = host._control(run.run_id, "agent-synthesis")["body"]["source_refs"][0]
        for _ in range(400):
            host._drive_run(host._run(run.run_id))
            control = host._control(run.run_id, "agent-synthesis")
            if control["body"]["phase"] == "ready_model" and len(
                control["body"]["tool_turn_refs"]
            ) == 2:
                break
            time.sleep(.01)
        else:
            pytest.fail("Two exact source windows were not saved before continuation")

        host._update_control(
            control, input_budget={"observed_limit_tokens": 8700, "target_fraction": .70}
        )
        control = host._control(run.run_id, "agent-synthesis")
        config = host._configuration(control, [source_ref])
        exposed, _unavailable = host._exposed_tools(control, config)
        prior_call_id, prior_attempt_id = host._model_ids(
            control, control["body"]["sequence"] + 1
        )
        prior_call = host.configurations.start_call(
            f"mission-call:{prior_call_id}",
            call_id=prior_call_id, configuration_id=config["id"],
            mission_id=control["body"]["mission_id"],
            task_id=control["body"]["task_id"], attempt_id=prior_attempt_id,
            loaded_skill_ids=list(config["body"]["skills"]),
            exposed_tool_ids=exposed, require_common_tools=False,
        )
        previous_budget_ref = host._save(
            "mission_call_budget", prior_call_id,
            {"mission_id": control["body"]["mission_id"], "run_id": run.run_id,
             "call_ref": ref(prior_call), "estimator_version": "older_estimator",
             "packer_version": "ora.working_context.packer.v13"},
            mission_id=control["body"]["mission_id"],
        )
        host._update_control(
            control, phase="waiting", work_id=None,
            waiting_reason="ContextOverflow: prior preparation did not fit",
        )
        assert host._recover_preparation(host._control(run.run_id, "agent-synthesis"))
        control = host._control(run.run_id, "agent-synthesis")
        assert control["body"]["feedback"]["previous_budget_ref"] == previous_budget_ref
        repaired_control_ref = ref(control)
        assert host._schedule_model(control)
        prepared_call = host._control(run.run_id, "agent-synthesis")["body"]["call_ref"]
        assert prepared_call["id"] != prior_call_id
        intent = host.store.get(
            "model_request_intent",
            f"model-intent:{prepared_call['id']}:{prepared_call['version']}",
        )
        assert intent is not None
        request = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        assert "preparation_repair" not in json.dumps(request["messages"])
        assert host.store.get(**repaired_control_ref)["body"]["feedback"][
            "previous_budget_ref"
        ] == previous_budget_ref
        windows = sorted(
            host.store.records("source_read_window"),
            key=lambda item: item["body"]["char_offset"],
        )
        assert [item["body"]["char_offset"] for item in windows] == [0, 1600]
        second_passage = windows[1]["body"]["passage_refs"][0]
        assert second_passage in intent["body"]["source_decisions"][0]["passage_refs"]
        packets = json.loads(request["messages"][1]["content"])["source_packets"]
        assert len(packets) == 1 and "B" * 800 in json.dumps(packets)
        assert packets[0]["text_basis"] == "checked_passages"
        assert second_passage in packets[0]["passage_refs"]
        assert json.dumps(request["messages"]).count("B" * 800) == 1
        inclusion = host.store.get("context_inclusion", f"context:{intent['id']}")
        assert inclusion["body"]["source_refs"] == [source_ref]
        tool_messages = [item for item in request["messages"] if item["role"] == "tool"]
        if tool_messages:
            tool_result = json.loads(tool_messages[-1]["content"])
            assert tool_result["next_request"]["char_offset"] == 2400
            assert tool_result["status"] == "saved_window_text_in_selected_source_packet"
            assert tool_result["source_text_status"] == (
                "exact_checked_passages_in_this_model_input"
            )
            assert tool_result["original_source_partial"] is True
            assert tool_result["next_cursor_scope"] == "retained_extracted_text_only"
            assert tool_result["passages"][0]["passage_ref"] == second_passage
            assert "text_preview" not in json.dumps(tool_result["passages"])
            assert inclusion["body"]["tool_result_compactions"][0][
                "omitted_preview_characters"
            ] == 800
        else:
            # Under tighter pressure, the completed tool envelope is archived.
            # Its exact result stays fetchable; checked text and navigation must
            # remain in this request without pretending the whole source was read.
            index = json.loads(request["messages"][-1]["content"])
            navigation = index["last_checked_window"]
            assert navigation["window_ref"] == ref(windows[1])
            assert navigation["next_cursor"] == {"block_offset": 0, "char_offset": 2400}
            assert navigation["source_text_status"] == (
                "checked_window_passages_in_selected_source_packet"
            )
            coverage = index["source_retrieval"][0]["selected_input_scope"]
            assert coverage["basis"] == "checked_passages_this_call"
            assert coverage["status"] == "incomplete_extracted_text"
            assert coverage["covered_chars"] == 2400
            assert coverage["total_chars"] == len(source_text)
            assert coverage["first_omitted_cursor"] == navigation["next_cursor"]
            latest_response = control["body"]["tool_turn_refs"][-1]
            assert any(
                item["type"] == "completed_tool_turn" and item["fetch"] == latest_response
                for item in inclusion["body"]["omitted"]
            )
            recovered = host.context.read_omitted_record(
                mission_id=control["body"]["mission_id"],
                actor_id=control["body"]["actor_id"], task_id=control["body"]["task_id"],
                inclusion_ref=ref(inclusion), pointer=latest_response, offset=0, limit=2000,
            )
            assert recovered["next_offset"] is None
            observation = json.loads(recovered["text"])
            assert observation["tool_name"] == "read_source_window"
            assert observation["window"]["window_ref"] == ref(windows[1])
            assert observation["window"]["passage_refs"] == [second_passage]
            assert observation["window"]["next_cursor"] == navigation["next_cursor"]
            assert observation["window"]["returned_chars"] == 800
        assert host.store.get("mission_call_budget", prior_call_id)["body"][
            "packer_version"
        ] == "ora.working_context.packer.v13"
        prepared_budget = host.store.get(
            "mission_call_budget", prepared_call["id"]
        )["body"]
        assert prepared_budget["packer_version"] == PACKER_VERSION
        # The soft target may yield to an exact preview within the observed
        # ceiling, after reserving the prior response's completion tokens.
        admission_target = prepared_budget["preview_admission_target_tokens"]
        assert 8700 * .70 <= admission_target
        assert prepared_budget["exact_complete_request_estimate_tokens"] <= admission_target
        assert admission_target <= (
            prepared_budget["observed_account_limit"]
            - prepared_budget["prior_completion_reservation"]
        )
        assert prepared_budget["exact_complete_request_sha256"] == intent["body"][
            "request_sha256"
        ]
        call = host.store.get(**prepared_call)
        assert call["body"]["model_id"] == requests[0]["model"]
        assert call["body"]["reasoning_settings"]["reasoning_effort"] == "high"
    finally:
        host.close()


def test_three_checked_windows_over_target_recover_without_dropping_selection(
    tmp_path, monkeypatch
):
    requests = []
    source_ref = None

    def transport(request, *, timeout):
        body = json.loads(request.data)
        requests.append(body)
        if len(requests) == 1:
            args = {"source_id": source_ref["id"], "source_version": source_ref["version"],
                    "block_offset": 0, "char_offset": 0, "char_limit": 1600,
                    "selection_mode": "replace"}
        else:
            prior = json.loads(next(
                item["content"] for item in reversed(body["messages"])
                if item["role"] == "tool"
            ))
            args = {**prior["next_request"], "char_limit": 800,
                    "selection_mode": "append"}
        return tool("read_source_window", args, len(requests))

    prefix = "DOI: 10.1234/three-windows. "
    source_text = prefix + _dense_synthetic_rows(2662 - len(prefix))
    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="three-checked-windows-eight-thousand",
            question="Assess the complete synthetic abstract", evidence_text=source_text,
            agent_ids=["agent-synthesis"],
        ))
        source_ref = host._control(run.run_id, "agent-synthesis")["body"]["source_refs"][0]
        for _ in range(500):
            host._drive_run(host._run(run.run_id))
            control = host._control(run.run_id, "agent-synthesis")
            if control["body"]["phase"] == "ready_model" and len(
                control["body"]["tool_turn_refs"]
            ) == 3:
                break
            time.sleep(.01)
        else:
            pytest.fail("Three checked windows were not saved")
        host._update_control(
            control, input_budget={"observed_limit_tokens": 8000,
                                   "target_fraction": .70},
        )
        assert host._schedule_model(host._control(run.run_id, "agent-synthesis"))
        call_ref = host._control(run.run_id, "agent-synthesis")["body"]["call_ref"]
        intent = host.store.get(
            "model_request_intent", f"model-intent:{call_ref['id']}:{call_ref['version']}"
        )
        assert intent is not None
        budget = host.store.get("mission_call_budget", call_ref["id"])["body"]
        assert budget["source_turn_mode"] == "all_allowed_sources_fit"
        selected = host._control(run.run_id, "agent-synthesis")["body"][
            "selected_passage_refs"
        ][source_ref["id"]]
        assert len(selected) == 3
        assert intent["body"]["source_decisions"][0]["passage_refs"] == selected
        request = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        assert "source_packets" in json.dumps(request["messages"])
        index = json.loads(request["messages"][-1]["content"])
        assert index["source_retrieval"][0]["selected_input_scope"]["status"] == (
            "complete_extracted_text"
        )
        assert host._control(run.run_id, "agent-synthesis")["body"][
            "selected_passage_refs"
        ][source_ref["id"]] == selected
        assert budget["source_inventory_ref"] is None
        assert budget["exact_complete_request_estimate_tokens"] <= (
            budget["observed_account_limit"] - 6
        )
        assert budget["preview_admission_target_tokens"] > 5600
        assert len(requests) == 3  # Preparation did not trigger another provider call.
    finally:
        host.close()


@pytest.mark.parametrize(
    "boundary", ["unchanged", "revoked", "late_message", "post_commit_overflow",
                 "over_target", "just_over_ceiling", "hard_revoked"]
)
def test_checked_window_exact_soft_fit_and_boundary_drift(
    tmp_path, monkeypatch, boundary
):
    requests = []
    source_ref = None
    actor = "agent-synthesis"

    def transport(request, *, timeout):
        body = json.loads(request.data)
        requests.append(body)
        return tool("read_source_window", {
            "source_id": source_ref["id"], "source_version": source_ref["version"],
            "block_offset": 0, "char_offset": 0, "char_limit": 1600,
            "selection_mode": "replace",
        }, len(requests))

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="checked-window-exact-soft-fit",
            question="Check a bounded synthetic excerpt",
            evidence_text="DOI: 10.1234/soft-fit. " + (
                _dense_synthetic_rows(3000)
                if boundary in {"over_target", "hard_revoked"} else "A" * 3000
            ),
            agent_ids=[actor],
        ))
        source_ref = host._control(run.run_id, actor)["body"]["source_refs"][0]
        for _ in range(200):
            host._drive_run(host._run(run.run_id))
            control = host._control(run.run_id, actor)
            if control["body"]["phase"] == "ready_model" and control["body"]["tool_turn_refs"]:
                break
            time.sleep(.01)
        else:
            pytest.fail("Checked source window was not saved")
        # The saved complete request exceeds a 5,600-token synthetic target.
        # Separately exercise exact preview under a deliberately conservative
        # soft source reservation. The unchanged/commit cases need room for
        # both the automatic capability catalog and the current tool envelope.
        target = (5600 if boundary in {"over_target", "hard_revoked"} else
                  4900 if boundary == "just_over_ceiling" else
                  8500 if boundary == "unchanged" else
                  8000 if boundary == "post_commit_overflow" else
                  9000 if boundary == "late_message" else 7000)
        host._update_control(
            control, input_budget={"observed_limit_tokens": target * 10 // 7,
                                   "target_fraction": .70},
        )
        before = host._control(run.run_id, actor)
        candidate_call_id, candidate_attempt_id = host._model_ids(
            before, before["body"]["sequence"] + 1
        )
        if boundary not in {"over_target", "just_over_ceiling", "hard_revoked"}:
            import ora.mission.service as service_module

            original_plan = service_module.plan_source_turn

            def conservative_soft_reservation(store, control_ref, *, source_input_allowance_tokens):
                allowance = (
                    min(source_input_allowance_tokens, 400)
                    if source_input_allowance_tokens < target
                    else source_input_allowance_tokens
                )
                return original_plan(
                    store, control_ref, source_input_allowance_tokens=allowance
                )

            monkeypatch.setattr(
                service_module, "plan_source_turn", conservative_soft_reservation
            )
        if boundary in {"revoked", "hard_revoked", "late_message"}:
            original = host._preview_soft_source_plan
            changed = False

            def preview_then_change(*args, **kwargs):
                nonlocal changed
                fit = original(*args, **kwargs)
                if fit and not changed and (boundary != "hard_revoked" or
                                            kwargs.get("total_target", 0) > target):
                    mission_id = host._run(run.run_id)["body"]["mission_id"]
                    if boundary in {"revoked", "hard_revoked"}:
                        revoke_source(host, source_ref, mission_id)
                    else:
                        current = host._control(run.run_id, actor)
                        sent = host.mailbox.send_message(
                            "late-preview-message", mission_id=mission_id,
                            message_id="late-preview-message",
                            sender_id=current["body"]["actor_id"],
                            recipient_id=current["body"]["actor_id"],
                            correlation_id="late-preview-correlation",
                                payload={"notice": "Synthetic late evidence qualifier. " * 10},
                            task_ref=ref(host.store.get(
                                "board_proposal", current["body"]["task_id"]
                            )),
                        )
                        assert sent["status"] == "accepted"
                    changed = True
                return fit

            monkeypatch.setattr(host, "_preview_soft_source_plan", preview_then_change)
        if boundary == "post_commit_overflow":
            from ora.mission.context import ContextOverflow

            original_assemble = host.context.assemble
            changed = False

            def late_required_context(*args, **kwargs):
                nonlocal changed
                if not changed and kwargs.get("compact_selected_window") and not kwargs.get(
                    "preview_call"
                ):
                    changed = True
                    raise ContextOverflow("Synthetic new required context exceeded allowance")
                return original_assemble(*args, **kwargs)

            monkeypatch.setattr(host.context, "assemble", late_required_context)
        assert host._schedule_model(host._control(run.run_id, actor))
        call_ref = host._control(run.run_id, actor)["body"]["call_ref"]
        intent = host.store.get(
            "model_request_intent", f"model-intent:{call_ref['id']}:{call_ref['version']}"
        )
        assert intent is not None
        budget = host.store.get("mission_call_budget", call_ref["id"])["body"]
        request = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        if boundary == "just_over_ceiling":
            assert budget["exact_complete_request_estimate_tokens"] <= 6994
            assert budget["preview_admission_target_tokens"] <= 6994
            assert budget["observed_account_limit"] == 7000
            if intent["body"]["source_decisions"]:
                assert intent["body"]["source_decisions"][0]["copy_ref"] == source_ref
            else:
                assert budget["source_turn_mode"] == "source_free_recovery"
                assert budget["source_inventory_ref"] is not None
            return
        if boundary == "over_target":
            assert budget["source_turn_mode"] == "all_allowed_sources_fit"
            assert budget["preview_admission_basis"] == TOKENIZER_BASIS
            assert budget["exact_complete_request_estimate_tokens"] > target
            assert budget["exact_complete_request_estimate_tokens"] <= (
                budget["observed_account_limit"] - 6
            )
            assert budget["preview_admission_target_tokens"] > target
            assert intent["body"]["source_decisions"][0]["copy_ref"] == source_ref
            assert "source_packets" in json.dumps(request["messages"])
            assert len(requests) == 1
            return
        if boundary in {"revoked", "hard_revoked"}:
            assert changed
            assert intent["body"]["source_decisions"] == []
            assert "source_packets" not in json.dumps(request["messages"])
            assert budget["source_exclusion_refs"]
            assert len(requests) == 1
            return
        if boundary == "late_message":
            assert changed
            assert "late-preview-message" in json.dumps(request["messages"])
            assert budget["exact_complete_request_estimate_tokens"] <= target
            if intent["body"]["source_decisions"]:
                assert intent["body"]["source_decisions"][0]["copy_ref"] == source_ref
            else:
                assert budget["source_turn_mode"] == "source_free_recovery"
                assert budget["source_inventory_ref"] is not None
                assert "source_packets" not in json.dumps(request["messages"])
                assert source_ref in host._control(run.run_id, actor)["body"]["source_refs"]
            assert len(requests) == 1
            return
        if boundary == "post_commit_overflow":
            assert changed
            assert budget["source_turn_mode"] == "source_free_recovery"
            assert intent["body"]["source_decisions"] == []
            assert "source_packets" not in json.dumps(request["messages"])
            assert call_ref["id"] != candidate_call_id
            assert host.store.get("agent_call", candidate_call_id) is not None
            assert host.store.get("agent_attempt", candidate_attempt_id) is None
            assert host.store.get(
                "model_request_intent", f"model-intent:{candidate_call_id}:1"
            ) is None
            assert len(requests) == 1
            # Model a restart at the saved pre-dispatch boundary, before the
            # source-free fallback advanced control.sequence. The orphaned
            # preview call and budget must never be reused for a new allowance.
            current = host._control(run.run_id, actor)
            host._update_control(
                current, phase="ready_model", sequence=before["body"]["sequence"],
                work_id=None, call_ref=before["body"]["call_ref"],
                input_budget={"observed_limit_tokens": 17000,
                              "target_fraction": .70},
            )
            assert host._schedule_model(host._control(run.run_id, actor))
            retried_ref = host._control(run.run_id, actor)["body"]["call_ref"]
            assert retried_ref["id"] != candidate_call_id
            assert host.store.get(
                "model_request_intent", f"model-intent:{retried_ref['id']}:1"
            ) is not None
            return
        assert budget["source_turn_mode"] != "source_free_recovery"
        assert budget["preview_admission_basis"] == TOKENIZER_BASIS
        assert budget["exact_complete_request_sha256"] == intent["body"][
            "request_sha256"
        ]
        assert budget["exact_complete_request_estimate_tokens"] <= target
        selected = host._control(run.run_id, actor)["body"][
            "selected_passage_refs"
        ][source_ref["id"]]
        assert intent["body"]["source_decisions"][0]["passage_refs"] == selected
        packet = json.loads(request["messages"][1]["content"])["source_packets"][0]
        assert "A" * 100 in packet["text"]
        assert packet["passage_refs"] == selected
        tool_result = json.loads(next(
            item["content"] for item in request["messages"] if item["role"] == "tool"
        ))
        assert tool_result["status"] == "saved_window_text_in_selected_source_packet"
        assert tool_result["next_cursor"]["char_offset"] == 1600
        assert tool_result["window_ref"]["kind"] == "source_read_window"
        assert estimate_request_tokens(
            host.store.read_blob(intent["body"]["blob"]["sha256"]),
            basis=intent["body"]["request_token_estimate"]["basis"],
        ).tokens <= target
        assert len(requests) == 1
    finally:
        host.close()


def test_source_free_turn_does_not_replay_prior_source_window_text(tmp_path, monkeypatch):
    import ora.mission.service as module

    calls = []
    source_ref = None

    def transport(request, *, timeout):
        body = json.loads(request.data)
        calls.append(body)
        if len(calls) == 1:
            return tool("read_source_window", {
                "source_id": source_ref["id"], "source_version": source_ref["version"],
                "char_limit": 1000, "selection_mode": "replace",
            }, 1)
        return answer("Select a smaller reading window next.", 2)

    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(LiveStartRequest(
        request_id="source-window-inventory-boundary", question="Inspect the supplied text",
        agent_ids=["agent-synthesis"], evidence_text="Distinct retained source sentence.",
    ))
    source_ref = host._control(receipt.run_id, "agent-synthesis")["body"]["source_refs"][0]
    for _ in range(100):
        host._drive_run(host._run(receipt.run_id))
        control = host._control(receipt.run_id, "agent-synthesis")
        if control["body"]["phase"] == "ready_model" and control["body"]["tool_turn_refs"]:
            break
        time.sleep(.01)
    else:
        pytest.fail("First source-window result was not durably saved")
    original = module.plan_source_turn
    monkeypatch.setattr(module, "plan_source_turn", lambda store, control_ref, **kwargs:
                        original(store, control_ref, source_input_allowance_tokens=1))
    drain(host, receipt.run_id)
    assert len(calls) == 2
    second_input = json.dumps(calls[1]["messages"])
    assert "source_free_recovery" in second_input
    assert "Distinct retained source sentence." not in second_input
    assert "read_source_window" in second_input
    budgets = host.store.records("mission_call_budget")
    recovery = next(item["body"] for item in budgets
                    if item["body"]["source_turn_mode"] == "source_free_recovery")
    assert recovery["current_tool_turn_ref"] is None
    assert recovery["compacted_tool_turn_refs"] == control["body"]["tool_turn_refs"]


@pytest.mark.parametrize("revoke_before_next_call", [False, True])
@pytest.mark.parametrize("extra_copy", [False, True])
def test_source_free_inventory_result_reaches_next_call_only_with_current_use(
    tmp_path, monkeypatch, revoke_before_next_call, extra_copy
):
    requests = []

    def transport(request, *, timeout):
        body = json.loads(request.data)
        requests.append(body)
        if len(requests) == 1:
            inventory = next(
                json.loads(message["content"])["inventory"]
                for message in body["messages"]
                if message["role"] == "user"
                and "inventory" in json.loads(message["content"])
            )
            pointer = inventory["inventory_ref"]
            return tool(
                "read_source_inventory",
                {
                    "inventory_id": pointer["id"],
                    "inventory_version": pointer["version"],
                    "cursor": 0,
                    "limit": 5,
                },
                1,
            )
        return answer("An exact source window is needed before a source claim.", 2)

    host = service(tmp_path, monkeypatch, transport)
    try:
        receipt = host.start(LiveStartRequest(
            request_id=f"inventory-handoff-{revoke_before_next_call}-{extra_copy}",
            question="Inspect the retained source inventory",
            agent_ids=["agent-synthesis"],
            evidence_text="Synthetic source text. " * 4000,
        ))
        if extra_copy:
            two_supplied_sources(host, receipt.run_id)
        host._update_control(
            host._control(receipt.run_id, "agent-synthesis"),
            input_budget={"observed_limit_tokens": 8000, "target_fraction": 0.70},
        )
        for _ in range(200):
            host._drive_run(host._run(receipt.run_id))
            control = host._control(receipt.run_id, "agent-synthesis")
            if control["body"]["phase"] == "ready_model" and control["body"]["tool_turn_refs"]:
                break
            time.sleep(.01)
        else:
            pytest.fail("Inventory result was not saved before continuation")
        prior_response = control["body"]["tool_turn_refs"][-1]
        if revoke_before_next_call:
            revoke_source(
                host, control["body"]["source_refs"][0],
                host._run(receipt.run_id)["body"]["mission_id"],
            )
        assert host._schedule_model(host._control(receipt.run_id, "agent-synthesis"))
        second_call_id = host._control(receipt.run_id, "agent-synthesis")["body"][
            "call_ref"
        ]["id"]
        for _ in range(200):
            host._drive_run(host._run(receipt.run_id))
            if len(requests) >= 2:
                break
            time.sleep(.01)
        else:
            pytest.fail("Second model request was not dispatched")
        second = requests[1]
        tool_messages = [item for item in second["messages"] if item["role"] == "tool"]
        budgets = {
            item["body"]["call_ref"]["id"]: item["body"]
            for item in host.store.records("mission_call_budget")
        }
        if revoke_before_next_call:
            assert tool_messages == []
            assert budgets[second_call_id]["current_tool_turn_ref"] is None
            assert prior_response in budgets[second_call_id]["compacted_tool_turn_refs"]
        else:
            assert budgets[second_call_id]["source_turn_mode"] == "source_free_recovery"
            assert len(tool_messages) == 1
            page = json.loads(tool_messages[0]["content"])
            assert page["schema"] == "ora.source_inventory_page.v1"
            assert len(page["items"]) == (2 if extra_copy else 1)
            assert page["next_cursor"] is None
            assert budgets[second_call_id]["current_tool_turn_ref"] == prior_response
            assert any(
                item["role"] == "assistant"
                and item.get("tool_calls", [{}])[0].get("function", {}).get("name")
                == "read_source_inventory"
                for item in second["messages"]
            )
        assert "Synthetic source text." not in json.dumps(second["messages"])
    finally:
        host.close()


def test_any_starting_peer_can_close_scope_while_other_peer_continues(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda request, *, timeout: answer("unused", 1))
    receipt = host.start(
        LiveStartRequest(
            request_id="mission-independent-peer-scope",
            question="Compare two independently useful analyses",
            agent_ids=["agent-synthesis", "agent-critique"],
        )
    )
    sage = host._control(receipt.run_id, "agent-synthesis")
    mira = host._control(receipt.run_id, "agent-critique")
    run = host._run(receipt.run_id)
    assert not sage["body"]["scope_required"]
    assert not mira["body"]["scope_required"]
    assert host._maybe_complete(run) is False
    sent = host.mailbox.send_request(
        "synthetic-scope-continuing-request",
        mission_id=run["body"]["mission_id"],
        request_id="synthetic-scope-peer-request",
        message_id="synthetic-scope-peer-message",
        requester_id=sage["body"]["actor_id"],
        recipient_id=mira["body"]["actor_id"],
        question="Check a separate interpretation",
        expected_artifact="Independent reply",
        cause_ref=sage["body"]["proposal_ref"],
        payload={"question": "Check a separate interpretation"},
        task_ref=sage["body"]["proposal_ref"],
    )
    assert sent["status"] == "accepted"
    signal = host.work.record_scope_disposition(
        "synthetic-independent-scope-signal",
        run_ref=ref(run),
        actor_id=sage["body"]["actor_id"],
        source_control_ref=ref(sage),
        decisions=[],
        reason="The accepted analysis answers the requested scope; the second peer continues",
    )
    assert signal["status"] == "recorded"
    assert host._schedule_model(sage)
    asyncio.run(host.executor.run_ready(run["body"]["mission_id"]))
    assert host._advance(host._control(receipt.run_id, "agent-synthesis"))
    assert host._control(receipt.run_id, "agent-synthesis")["body"]["phase"] == "done"
    assert host._maybe_complete(host._run(receipt.run_id))
    current = host._run(receipt.run_id)["body"]
    assert current["status"] == "completed"
    assert (
        host.store.get("mail_request", "synthetic-scope-peer-request")["body"]["status"]
        == "unanswered"
    )
    assert host._control(receipt.run_id, "agent-critique")["body"]["scope_status"] == "continued"
    summary = next(item for item in host.state(receipt.run_id).runs if item.id == receipt.run_id)
    assert summary.continuing_task_ids == [mira["body"]["task_id"]]


def test_model_can_save_latency_expectation_without_a_deadline(tmp_path, monkeypatch):
    calls = []

    def transport(request, *, timeout):
        calls.append(json.loads(request.data))
        if len(calls) == 1:
            return tool("request_capability", {"tool_id": "estimate_latency"}, 1)
        if len(calls) == 2:
            return tool(
                "estimate_latency",
                {
                    "category": "medium",
                    "basis": "Several source checks may be useful",
                    "expected_version": 0,
                },
                2,
            )
        return tool(
            "publish_result",
            {
                "title": "Bounded assessment",
                "text": "A result was recorded after the rough estimate.",
                "citations": [],
                "limitations": ["No source supplied"],
            },
            3,
        )

    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(
        LiveStartRequest(
            request_id="mission-latency-choice",
            question="Provide a bounded assessment",
            agent_ids=["agent-synthesis"],
        )
    )
    drain(host, receipt.run_id)
    assert host._run(receipt.run_id)["body"]["status"] == "completed"
    expectations = host.store.records("mission_latency_expectation")
    assert len(expectations) == 1
    assert expectations[0]["body"]["category"] == "medium"
    assert len(calls) == 3


def test_model_navigates_exact_venue_lead_without_promoting_it_to_evidence(tmp_path, monkeypatch):
    import ora.mission.service as service_module

    calls = []
    selected_ref = None
    fetched_urls = []
    html = b"<html><head><title>Synthetic venue page</title></head><body><h1>Current venue links</h1><a href='/events'>Events</a></body></html>"

    def fake_fetch(url, *, max_bytes):
        fetched_urls.append(url)
        assert max_bytes >= len(html)
        return {
            "status": 200,
            "content": html,
            "content_sha256": hashlib.sha256(html).hexdigest(),
            "byte_length": len(html),
            "content_type": "text/html",
            "requested_url": url,
            "final_url": url,
            "redirect_chain": [],
            "access_basis": "public_https_unauthenticated",
            "authenticated": False,
            "retrieved_at": "2026-10-09T00:00:00Z",
        }

    def transport(request, *, timeout):
        nonlocal selected_ref
        body = json.loads(request.data)
        calls.append(body)
        count = len(calls)
        if count == 1:
            return tool("request_capability", {"tool_id": "list_venues"}, count)
        if count == 2:
            return tool("list_venues", {"limit": 1}, count)
        prior = json.loads(
            next(item["content"] for item in reversed(body["messages"]) if item["role"] == "tool")
        )
        if count == 3:
            selected_ref = prior["entries"][0]["catalog_ref"]
            return tool("request_capability", {"tool_id": "navigate_venue"}, count)
        if count == 4:
            return tool("navigate_venue", {"catalog_ref": selected_ref}, count)
        assert prior["evidence_status"] == "navigation_lead_only"
        assert prior["navigation_context"]["content_use"] == "navigation_lead_only"
        assert prior["navigation_context"]["resource_type"]
        assert prior["title"] == "Synthetic venue page"
        assert prior["links"][0]["text"] == "Events"
        return tool(
            "publish_result",
            {
                "title": "Venue lead reviewed",
                "text": "A venue page was navigated as a lead.",
                "citations": [],
                "limitations": ["No article source was inspected"],
            },
            count,
        )

    monkeypatch.setattr(service_module, "fetch_public_source", fake_fetch)
    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(
        LiveStartRequest(
            request_id="mission-venue-navigation",
            question="Inspect a venue lead",
            agent_ids=["agent-synthesis"],
        )
    )
    drain(host, receipt.run_id, limit=30)
    assert host._run(receipt.run_id)["body"]["status"] == "completed"
    assert len(fetched_urls) == 1
    assert len(host.store.records("venue_navigation")) == 1
    assert host.store.records("source_copy") == []


def test_explicit_peer_wait_resumes_for_answer_review(tmp_path, monkeypatch):
    release_mira = threading.Event()
    counts = {"Sage": 0, "Mxra": 0}
    lock = threading.Lock()
    host = None
    receipt = None
    reviewed = False

    def transport(request, *, timeout):
        nonlocal reviewed
        body = json.loads(request.data)
        role = "Sage" if "Sage" in body["messages"][0]["content"] else "Mxra"
        with lock:
            counts[role] += 1
            count = counts[role]
        if role == "Mxra":
            release_mira.wait(5)
            return tool(
                "publish_result",
                {
                    "title": "Peer response",
                    "text": "The comparator remains uncertain.",
                    "citations": [],
                    "limitations": ["Synthetic fixture"],
                },
                90,
            )
        if count == 1:
            return tool(
                "peer_request",
                {"target_instance_id": "agent-critique", "question": "Check the comparator"},
                1,
            )
        if count == 2:
            return tool("request_capability", {"tool_id": "wait_for_dependencies"}, 2)
        request_id = host.store.records("mail_request")[0]["id"]
        request_record = host.store.get("mail_request", request_id)
        if count == 3:
            target = host._control(receipt.run_id, "agent-critique")
            return tool(
                "wait_for_dependencies",
                {
                    "dependencies": [
                        {
                            "kind": "peer_request",
                            "request_id": request_record["id"],
                            "request_version": request_record["version"],
                            "target_control_id": target["id"],
                            "target_control_version": target["version"],
                        }
                    ],
                    "reason": "Await the peer's independent comparator check",
                },
                3,
            )
        if not request_record["body"].get("answer_refs"):
            target = host._control(receipt.run_id, "agent-critique")
            return tool(
                "wait_for_dependencies",
                {
                    "dependencies": [
                        {
                            "kind": "peer_request",
                            "request_id": request_record["id"],
                            "request_version": request_record["version"],
                            "target_control_id": target["id"],
                            "target_control_version": target["version"],
                        }
                    ],
                    "reason": "The peer has not answered yet",
                },
                count,
            )
        if not reviewed:
            reviewed = True
            return tool(
                "review_peer_answer",
                {
                    "request_id": request_record["id"],
                    "answer_id": request_record["body"]["answer_refs"][-1]["id"],
                    "disposition": "answered",
                    "reason": "The peer's check is sufficient",
                },
                count,
            )
        return tool(
            "publish_result",
            {
                "title": "Scoped synthesis",
                "text": "The comparator was reviewed.",
                "citations": [],
                "limitations": ["Synthetic fixture"],
            },
            count,
        )

    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(
        LiveStartRequest(
            request_id="mission-explicit-peer-wait",
            question="Review a comparator",
            agent_ids=["agent-synthesis", "agent-critique"],
        )
    )
    try:
        for _ in range(250):
            host._drive_run(host._run(receipt.run_id))
            sage = host._control(receipt.run_id, "agent-synthesis")
            if sage["body"]["phase"] == "waiting" and sage["body"].get("wait_for"):
                break
            time.sleep(0.01)
        else:
            pytest.fail("Sage did not save an exact dependency wait")
        assert len(host.store.records("mission_wait_decision")) == 1
        assert host._run(receipt.run_id)["body"]["status"] == "active"
    finally:
        release_mira.set()
    drain(host, receipt.run_id, limit=50)
    assert host.store.records("mail_request")[0]["body"]["status"] == "answered"
    assert host._run(receipt.run_id)["body"]["status"] == "completed"


def test_fetched_title_basis_requires_title_visible_in_actual_source_bytes():
    title = "A substantial independent clinical research article title"
    blocks, _ = extract_blocks(
        f"<html><body><h1>{title}</h1><p>Abstract findings.</p></body></html>".encode(),
        "text/html",
    )
    assert (
        _visible_title_identity(title, blocks)
        == "verified_source:visible_title_matches_selected_hit"
    )
    assert (
        _visible_title_identity("A different independent clinical research article title", blocks)
        is None
    )


def test_mislabeled_pdf_fetch_reaches_intake_byte_detection(tmp_path, monkeypatch):
    import ora.mission.service as service_module

    pdf_bytes = b"%PDF-1.4\nsynthetic-pdf"
    hit_id = None
    calls = []

    def model_transport(request, *, timeout):
        nonlocal hit_id
        body = json.loads(request.data)
        calls.append(body)
        count = len(calls)
        if count == 1:
            return tool(
                "search_scholarly",
                {
                    "route": "europe_pmc.search",
                    "query": "synthetic PDF",
                    "limit": 1,
                },
                count,
            )
        prior = json.loads(
            next(item["content"] for item in reversed(body["messages"]) if item["role"] == "tool")
        )
        if count == 2:
            hit_id = prior["hits"][0]["hit_id"]
            return tool("request_capability", {"tool_id": "fetch_source"}, count)
        if count == 3:
            return tool("fetch_source", {"hit_id": hit_id}, count)
        return tool(
            "publish_result",
            {
                "title": "Fetched PDF",
                "text": "The checked PDF reports a difference.",
                "citations": [
                    {"source_id": prior["source_id"], "span_id": prior["passages"][0]["span_id"]}
                ],
                "limitations": ["Synthetic source"],
            },
            count,
        )

    def discovery_transport(method, url, *, body=None, headers=None):
        packet = {
            "hitCount": 1,
            "nextCursorMark": "*",
            "resultList": {
                "result": [
                    {
                        "source": "MED",
                        "id": "123",
                        "pmid": "123",
                        "doi": "10.1234/pdf",
                        "title": "Synthetic PDF study",
                        "abstractText": "Published abstract available.",
                        "isOpenAccess": "Y",
                    }
                ]
            },
        }
        return HTTPResult(200, json.dumps(packet).encode(), {})

    def fake_fetch(url):
        return {
            "status": 200,
            "content": pdf_bytes,
            "content_sha256": hashlib.sha256(pdf_bytes).hexdigest(),
            "byte_length": len(pdf_bytes),
            "content_type": "application/octet-stream",
            "requested_url": url,
            "final_url": url,
            "redirect_chain": [],
            "access_basis": "public_https_unauthenticated",
            "authenticated": False,
            "retrieved_at": "2026-10-09T00:00:00Z",
        }

    def fake_pdf_run(*args, **kwargs):
        return subprocess.CompletedProcess(
            args[0], 0, b"DOI: 10.1234/pdf.\nResults\nThe difference was -2.5 points.\f", b""
        )

    monkeypatch.setattr(service_module, "fetch_public_source", fake_fetch)
    monkeypatch.setattr("ora.evidence.intake.subprocess.run", fake_pdf_run)
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    store = StateStore(tmp_path)
    session = ModelSession(
        store, ROOT, adapter=GroqSelectedAdapter(ROOT, transport=model_transport)
    )
    host = MissionService(
        ROOT,
        tmp_path,
        model_session=session,
        discovery_transport=discovery_transport,
        execution_mode="offline_test",
        start_supervisor=False,
    )
    receipt = host.start(
        LiveStartRequest(
            request_id="mission-mislabeled-pdf",
            question="Inspect the PDF",
            agent_ids=["agent-synthesis"],
        )
    )
    drain(host, receipt.run_id, limit=24)
    assert host._run(receipt.run_id)["body"]["status"] == "completed"
    quality = host.store.records("intake_quality")[0]["body"]
    assert quality["detected_content_type"] == "application/pdf"
    assert quality["returned_content_type"] == "application/octet-stream"
    assert quality["mime_mismatch"] is True
    assert host.state(receipt.run_id).snapshot.artifacts[0].citations


def test_revoked_copy_is_excluded_without_blocking_other_source(tmp_path, monkeypatch):
    requests = []

    def transport(request, *, timeout):
        requests.append(json.loads(request.data))
        return answer("The second permitted source remains usable.", len(requests))

    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(
        LiveStartRequest(
            request_id="mission-one-revoked",
            question="Use the permitted source?",
            agent_ids=["agent-synthesis"],
            evidence_text="Revoked private synthetic source text.",
        )
    )
    revoked_ref, allowed_ref = two_supplied_sources(host, receipt.run_id)
    mission_id = host._run(receipt.run_id)["body"]["mission_id"]
    revoke_source(host, revoked_ref, mission_id)
    drain(host, receipt.run_id)
    assert host._run(receipt.run_id)["body"]["status"] == "completed"
    assert len(requests) == 1
    assert "Revoked private synthetic source text." not in json.dumps(requests[0])
    packets = json.loads(requests[0]["messages"][1]["content"])["source_packets"]
    assert [item["id"] for item in packets] == [allowed_ref["id"]]
    excluded = host.store.records("mission_source_exclusion")
    assert len(excluded) == 1 and excluded[0]["body"]["copy_ref"] == revoked_ref
    assert excluded[0]["body"]["model_input_status"] == "restricted"
    assert host._control(receipt.run_id, "agent-synthesis")["body"]["source_refs"] == [
        revoked_ref,
        allowed_ref,
    ]


def test_revocation_after_preparation_requeues_without_provider_dispatch(tmp_path, monkeypatch):
    requests = []

    def transport(request, *, timeout):
        requests.append(json.loads(request.data))
        return answer("The remaining source supports a bounded answer.", len(requests))

    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(
        LiveStartRequest(
            request_id="mission-revoke-after-prepare",
            question="Continue after a policy change?",
            agent_ids=["agent-synthesis"],
            evidence_text="Revoked private synthetic source text.",
        )
    )
    revoked_ref, allowed_ref = two_supplied_sources(host, receipt.run_id)
    control = host._control(receipt.run_id, "agent-synthesis")
    assert host._schedule_model(control)
    prior_call = host._control(receipt.run_id, "agent-synthesis")["body"]["call_ref"]
    mission_id = host._run(receipt.run_id)["body"]["mission_id"]
    revoke_source(host, revoked_ref, mission_id)
    drain(host, receipt.run_id, limit=20)
    assert host._run(receipt.run_id)["body"]["status"] == "completed"
    assert len(requests) == 1
    prior_intent_id = f"model-intent:{prior_call['id']}:{prior_call['version']}"
    assert host.store.get("model_dispatch_attempt", f"model-dispatch:{prior_intent_id}") is None
    assert host.store.get("model_response_receipt", f"model-response:{prior_intent_id}") is None
    blocks = host.store.records("mission_source_block")
    assert len(blocks) == 1
    assert blocks[0]["body"]["excluded_source_refs"] == [revoked_ref]
    assert blocks[0]["body"]["external_effect"] == "not_dispatched"
    packets = json.loads(requests[0]["messages"][1]["content"])["source_packets"]
    assert [item["id"] for item in packets] == [allowed_ref["id"]]
    assert "Revoked private synthetic source text." not in json.dumps(requests[0])


def test_revocation_omits_old_tool_text_from_next_model_input(tmp_path, monkeypatch):
    requests = []

    def transport(request, *, timeout):
        requests.append(json.loads(request.data))
        return (
            tool("discover_capabilities", {}, 1)
            if len(requests) == 1
            else answer("The remaining source supports a partial answer.", 2)
        )

    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(
        LiveStartRequest(
            request_id="mission-revoke-tool-history",
            question="Continue with safe context?",
            agent_ids=["agent-synthesis"],
            evidence_text="Revoked private synthetic source text.",
        )
    )
    revoked_ref, allowed_ref = two_supplied_sources(host, receipt.run_id)
    for _ in range(30):
        host._drive_run(host._run(receipt.run_id))
        control = host._control(receipt.run_id, "agent-synthesis")
        if control["body"]["phase"] == "ready_tool":
            break
        time.sleep(0.01)
    assert control["body"]["phase"] == "ready_tool"
    response_ref = control["body"]["response_ref"]
    host.context.save_tool_result(
        "synthetic-source-bearing-tool-result",
        response_ref=response_ref,
        tool_call_id="tool-1",
        name="discover_capabilities",
        content="Revoked private synthetic source text.",
    )
    host._update_control(
        control,
        phase="ready_model",
        work_id=None,
        tool_call=None,
        tool_turn_refs=[response_ref],
    )
    revoke_source(host, revoked_ref, host._run(receipt.run_id)["body"]["mission_id"])
    drain(host, receipt.run_id)
    assert host._run(receipt.run_id)["body"]["status"] == "completed"
    assert len(requests) == 2
    assert "Revoked private synthetic source text." not in json.dumps(requests[1])
    packets = json.loads(requests[1]["messages"][1]["content"])["source_packets"]
    assert [item["id"] for item in packets] == [allowed_ref["id"]]
    assert not any(item["role"] == "tool" for item in requests[1]["messages"])
    latest = sorted(host.store.records("context_inclusion"), key=lambda item: item["created_at"])[
        -1
    ]
    assert any(
        item["type"] == "completed_tool_turn" and item["ref"] == response_ref
        for item in latest["body"]["omitted"]
    )


def test_late_peer_request_reopens_recipient_and_requester_reviews_answer(tmp_path, monkeypatch):
    lock = threading.Lock()
    counts = {"Sage": 0, "Mxra": 0}
    inspected = []

    def transport(request, *, timeout):
        body = json.loads(request.data)
        role = "Sage" if "Sage" in body["messages"][0]["content"] else "Mxra"
        with lock:
            counts[role] += 1
            index = counts[role]
        if role == "Sage" and index == 1:
            return tool(
                "peer_request",
                {
                    "target_instance_id": "agent-critique",
                    "question": "Check the provisional answer",
                },
                1,
            )
        if role == "Mxra" and index == 2:
            context = json.loads(body["messages"][-1]["content"])
            assert any(item["type"] == "request" for item in context["details"])
            return tool(
                "publish_result",
                {
                    "title": "Peer check",
                    "text": "A limitation remains.",
                    "citations": [],
                    "limitations": ["No source"],
                },
                2,
            )
        if role == "Sage" and index == 3:
            context = json.loads(body["messages"][-1]["content"])
            request_ref = context["request_dispositions"][0]["ref"]
            answer_ref = context["request_dispositions"][0]["answer_refs"][0]
            inspected.append((request_ref, answer_ref))
            return tool(
                "review_peer_answer",
                {
                    "request_id": request_ref["id"],
                    "answer_id": answer_ref["id"],
                    "disposition": "answered",
                    "reason": "Useful independent check",
                },
                3,
            )
        return answer(f"{role} bounded contribution {index}.", 10 + index)

    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(
        LiveStartRequest(
            request_id="mission-peer",
            question="Review this uncertain question",
            agent_ids=["agent-synthesis", "agent-critique"],
        )
    )
    drain(host, receipt.run_id, limit=24)
    request = host.store.records("mail_request")[0]
    assert request["body"]["status"] == "answered"
    assert len(host.store.records("mail_answer")) == 1
    assert inspected and inspected[0][0]["id"] == request["id"]
    assert counts == {"Sage": 4, "Mxra": 2}
    assert host.state(receipt.run_id).snapshot.mission.status == "completed"


def test_partly_answered_peer_request_gets_second_answer_and_review(tmp_path, monkeypatch):
    lock = threading.Lock()
    counts = {"Sage": 0, "Mxra": 0}

    def transport(request, *, timeout):
        body = json.loads(request.data)
        role = "Sage" if "Sage" in body["messages"][0]["content"] else "Mxra"
        with lock:
            counts[role] += 1
            index = counts[role]
        if role == "Sage" and index == 1:
            return tool(
                "peer_request",
                {
                    "target_instance_id": "agent-critique",
                    "question": "Check the comparator and follow-up",
                },
                1,
            )
        if role == "Mxra" and index in {2, 3}:
            return tool(
                "publish_result",
                {
                    "title": f"Peer answer {index}",
                    "text": "First response is partial." if index == 2 else "Comparator checked.",
                    "citations": [],
                    "limitations": ["Synthetic fixture"],
                },
                index,
            )
        if role == "Sage" and index in {3, 5}:
            context = json.loads(body["messages"][-1]["content"])
            request = context["request_dispositions"][0]
            return tool(
                "review_peer_answer",
                {
                    "request_id": request["ref"]["id"],
                    "answer_id": request["answer_refs"][-1]["id"],
                    "disposition": "partly_answered" if index == 3 else "answered",
                    "reason": "Comparator still needed" if index == 3 else "Comparator resolved",
                },
                10 + index,
            )
        return answer(f"{role} bounded contribution {index}.", 20 + index)

    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(
        LiveStartRequest(
            request_id="mission-partial-peer",
            question="Check a comparator",
            agent_ids=["agent-synthesis", "agent-critique"],
        )
    )
    drain(host, receipt.run_id, limit=42)
    request = host.store.records("mail_request")[0]
    assert request["body"]["status"] == "answered"
    assert len(request["body"]["answer_refs"]) == 2
    assert counts["Mxra"] >= 3 and counts["Sage"] >= 5
    assert host.state(receipt.run_id).snapshot.mission.status == "completed"


def test_continued_optional_request_wakes_completed_peer_without_reopening_root(
    tmp_path,
    monkeypatch,
):
    continued = False
    optional_calls = 0

    def transport(request, *, timeout):
        nonlocal optional_calls
        body = json.loads(request.data)
        if not continued:
            return answer("The requested contribution is complete.", 1)
        context = json.loads(body["messages"][-1]["content"])
        objective = context["task"]["objective"]
        if objective == "Additional methods check":
            optional_calls += 1
            if optional_calls == 1:
                return tool(
                    "peer_request",
                    {
                        "target_instance_id": "agent-synthesis",
                        "question": "Please compare the follow-up interpretation",
                    },
                    2,
                )
            dispositions = context.get("request_dispositions", [])
            if dispositions and dispositions[0]["status"] == "awaiting_review":
                pending = dispositions[0]
                return tool(
                    "review_peer_answer",
                    {
                        "request_id": pending["ref"]["id"],
                        "answer_id": pending["answer_refs"][-1]["id"],
                        "disposition": "answered",
                        "reason": "Comparison addressed",
                    },
                    3,
                )
            return answer("Optional methods work remains scoped.", 4)
        assert objective == "Please compare the follow-up interpretation"
        return answer("Sage answered the later optional request.", 5)

    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(
        LiveStartRequest(
            request_id="mission-post-scope-peer",
            question="Finish the requested scope",
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis", "agent-critique"],
        )
    )
    drain(host, receipt.run_id)
    assert host._run(receipt.run_id)["body"]["status"] == "completed"
    completed_run = host._run(receipt.run_id)
    completed_snapshot = host.state(receipt.run_id).snapshot.model_dump(mode="json")
    sage_root = host._control(receipt.run_id, "agent-synthesis")
    sage_report = sage_root["body"]["report_ref"]
    mira_root = host._control(receipt.run_id, "agent-critique")
    mission_id = host._run(receipt.run_id)["body"]["mission_id"]
    optional_proposal = host.board.propose(
        "synthetic-post-scope-proposal",
        mission_id=mission_id,
        proposal_id="post-scope-optional",
        actor_id=mira_root["body"]["actor_id"],
        objective="Additional methods check",
        expected_artifact="Scoped peer contribution",
        next_action="Choose an independent follow-up",
    )
    assert optional_proposal["status"] == "accepted"
    host._save(
        "mission_task_control",
        "post-scope-optional-control",
        {
            **mira_root["body"],
            "task_id": "post-scope-optional",
            "proposal_ref": optional_proposal["proposal_ref"],
            "phase": "ready_model",
            "scope_required": False,
            "scope_status": "continued",
            "control_type": "research_work",
            "sequence": 0,
            "call_ref": None,
            "work_id": None,
            "response_ref": None,
            "tool_call": None,
            "tool_turn_refs": [],
            "report_ref": None,
            "feedback": None,
            "waiting_reason": None,
        },
        mission_id=mission_id,
    )
    continued = True
    for _ in range(120):
        changed, wake = host._drive_run(host._run(receipt.run_id))
        requests = host.store.records("mail_request")
        if requests and requests[0]["body"]["status"] == "answered":
            break
        if not changed:
            if wake:
                time.sleep(min(0.01, max(0.001, wake - time.time())))
            else:
                break
    requests = host.store.records("mail_request")
    assert requests and requests[0]["body"]["status"] == "answered"
    assert host._run(receipt.run_id)["body"]["status"] == "completed"
    assert host._control(receipt.run_id, "agent-synthesis")["body"]["report_ref"] == sage_report
    assert host._control(receipt.run_id, "agent-synthesis")["body"]["phase"] == "done"
    response_controls = [
        item
        for item in host._controls(receipt.run_id)
        if item["body"].get("control_type") == "peer_response"
    ]
    assert len(response_controls) == 1
    assert response_controls[0]["body"]["scope_status"] == "continued"
    dispositions = host.store.records("mission_post_scope_disposition")
    assert len(dispositions) == 1
    saved = dispositions[0]["body"]
    assert saved["decision"] == "continue_under_saved_optional_scope"
    assert saved["completed_run_ref"] == ref(completed_run)
    assert saved["opening_event_sequence"] > completed_run["body"]["closure_event_through"]
    assert host._run(receipt.run_id) == completed_run
    from ora.state.inspection import load_snapshot
    original = load_snapshot(host.store, completed_snapshot["snapshot_id"])
    assert original["mission"]["status"] == "completed"
    assert not original["messages"]
    # A new request from the already closed root is recorded separately; it
    # cannot silently reactivate that original completed contribution.
    request = host.mailbox.send_request(
        "late-root-question", mission_id=mission_id, request_id="late-root-question",
        message_id="late-root-message", requester_id=sage_root["body"]["actor_id"],
        recipient_id=mira_root["body"]["actor_id"], question="A separate follow-up question",
        expected_artifact="New scoped work", cause_ref=sage_root["body"]["proposal_ref"],
        payload={"text": "A separate follow-up question"},
    )
    assert request["status"] == "accepted"
    assert host._activate_peer_obligations(completed_run)
    saved_late = [item for item in host.store.records("mission_post_scope_disposition")
                  if item["body"]["request_ref"]["id"] == "late-root-question"]
    assert saved_late[0]["body"]["decision"] == "followup_required"
    assert host._control(receipt.run_id, "agent-synthesis") == sage_root
    assert host._run(receipt.run_id) == completed_run


def test_restart_after_direct_board_acceptance_does_not_reclaim_completed_task(
    tmp_path,
    monkeypatch,
):
    invocations = []

    def transport(request, *, timeout):
        invocations.append(1)
        return answer("A bounded answer.", len(invocations))

    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(
        LiveStartRequest(
            request_id="mission-crash", question="What is known?", agent_ids=["agent-synthesis"]
        )
    )
    original = host._finish_attempt
    fired = []

    def crash_once(*args, **kwargs):
        if not fired:
            fired.append(True)
            raise RuntimeError("injected local interruption after board acceptance")
        return original(*args, **kwargs)

    monkeypatch.setattr(host, "_finish_attempt", crash_once)
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline and not fired:
        try:
            host._drive_run(host._run(receipt.run_id))
        except RuntimeError as exc:
            assert "injected local interruption" in str(exc)
            break
        time.sleep(0.01)
    assert fired
    assert host.store.records("report")
    assert host.store.records("board_proposal")[0]["body"]["status"] == "completed"
    host.close()
    resumed = MissionService(
        ROOT,
        tmp_path,
        model_session=host.session,
        execution_mode="offline_test",
        start_supervisor=False,
    )
    drain(resumed, receipt.run_id)
    assert resumed.state(receipt.run_id).snapshot.mission.status == "completed"
    assert len(invocations) == 1
    assert len(resumed.store.records("report")) == 1


@pytest.mark.parametrize("cutoff_kind,cutoff_count", [("text", 1), ("text", 2), ("tool", 1)])
def test_length_cutoff_resumes_saved_work_without_publishing_fragments(
    tmp_path, monkeypatch, cutoff_kind, cutoff_count,
):
    calls = []

    def transport(request, *, timeout):
        payload = json.loads(request.data)
        calls.append(payload)
        if len(calls) > 1:
            context = json.loads(payload["messages"][-1]["content"])
            recovery = context["partial_visible_output"]
            if cutoff_kind == "text":
                assert recovery["status"] == "available"
                assert recovery["text_preview"].startswith("Unfinished visible draft")
            else:
                assert recovery["status"] == "content_unavailable"
            assert "PRIVATE_CUTOFF_REASONING" not in json.dumps(payload)
            assert "unfinished_arguments" not in json.dumps(payload)
        if len(calls) <= cutoff_count:
            message = {
                "role": "assistant",
                "content": f"Unfinished visible draft {len(calls)}." if cutoff_kind == "text" else None,
                "reasoning": "PRIVATE_CUTOFF_REASONING",
            }
            if cutoff_kind == "tool":
                message["tool_calls"] = [{
                    "id": "cut-off-action", "type": "function",
                    "function": {"name": "publish_result", "arguments": '{"unfinished_arguments":'},
                }]
            return Response({
                "id": f"cutoff-{len(calls)}", "model": MODEL,
                "choices": [{"message": message, "finish_reason": "length"}],
                "usage": {"prompt_tokens": 5, "completion_tokens": 9, "total_tokens": 14},
            })
        return answer("A complete scoped contribution after recovery.", len(calls))

    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(LiveStartRequest(
        request_id="mission-output-cutoff", question="Produce the scoped contribution",
        agent_ids=["agent-synthesis"],
    ))
    for _ in range(160):
        host._drive_run(host._run(receipt.run_id))
        control = host._control(receipt.run_id, "agent-synthesis")["body"]
        if control["phase"] == "ready_model" and (control.get("feedback") or {}).get("status") == "partial":
            break
        time.sleep(.01)
    else:
        pytest.fail("Length cutoff was not saved for a new model decision")
    assert host.store.records("report") == []
    assert host.store.records("context_tool_result") == []
    assert host._run(receipt.run_id)["body"]["status"] == "active"
    host.close()
    resumed = service(tmp_path, monkeypatch, transport)
    drain(resumed, receipt.run_id, limit=20)
    assert resumed._run(receipt.run_id)["body"]["status"] == "completed"
    assert len(calls) == cutoff_count + 1
    assert all(item["model"] == MODEL and item["reasoning_effort"] == "high" for item in calls)
    assert len(resumed.store.records("report")) == 1
    responses = resumed.store.records("model_response_receipt")
    assert sum(item["body"]["status"] == "partial" for item in responses) == cutoff_count
    assert len({item["body"]["call_ref"]["id"] for item in responses}) == len(calls)
    assert all(item["body"]["usage"]["total_tokens"] == 14
               for item in responses if item["body"]["status"] == "partial")
    assert resumed.store.records("report")[0]["body"]["body"] == "A complete scoped contribution after recovery."
    resumed.close()


@pytest.mark.parametrize("legacy_wait", [False, True])
def test_completed_model_waits_for_exact_executor_claim_release_without_recalling_model(
    tmp_path, monkeypatch, legacy_wait,
):
    calls = []
    release_entered = threading.Event()
    permit_release = threading.Event()

    def transport(request, *, timeout):
        calls.append(json.loads(request.data))
        return answer("A complete synthetic contribution.", len(calls))

    host = service(tmp_path, monkeypatch, transport)
    original_release = host.executor._release_board

    def held_release(spec, fence):
        release_entered.set()
        if not permit_release.wait(5):
            raise AssertionError("Fixture did not release the exact executor claim")
        return original_release(spec, fence)

    monkeypatch.setattr(host.executor, "_release_board", held_release)
    try:
        run = host.start(LiveStartRequest(
            request_id=f"direct-claim-release-race-{legacy_wait}",
            question="Give a synthetic result",
            agent_ids=["agent-synthesis"],
        ))
        for _ in range(300):
            host._drive_run(host._run(run.run_id))
            control = host._control(run.run_id, "agent-synthesis")
            if release_entered.is_set() and control["body"].get("waiting_reason") == (
                "Accepted model result awaits current task claim release"
            ):
                break
            time.sleep(.01)
        else:
            pytest.fail("Completed model work did not wait for its executor claim")
        report_ref = control["body"]["report_ref"]
        assert report_ref is not None
        assert host.store.get("board_proposal", control["body"]["task_id"])["body"][
            "status"
        ] == "open"
        assert len(calls) == 1
        assert not any(
            item["id"].startswith("mission-direct-claim:")
            for item in host.store.records("board_rejection")
        )
        legacy_operation = f"mission-direct-claim:{report_ref['id']}"
        if legacy_wait:
            host.board._reject(
                legacy_operation, control["body"]["mission_id"], "conflict",
                "Synthetic prior claim rejection",
            )
            host._update_control(
                control, work_id=None,
                waiting_reason="Direct result needs current task claim review",
            )

        permit_release.set()
        drain(host, run.run_id, limit=20)
        assert host._run(run.run_id)["body"]["status"] == "completed"
        assert len(calls) == 1 and len(host.store.records("report")) == 1
        outcomes = host.store.records("board_outcome")
        direct = next(item for item in outcomes if report_ref in item["body"]["result_refs"])
        executor = next(item for item in outcomes if not item["body"]["completes_task"])
        assert direct["body"]["claim_generation"] > executor["body"]["claim_generation"]
        assert (host.store.get("board_rejection", legacy_operation) is not None) == legacy_wait
    finally:
        permit_release.set()
        host.close()


def test_direct_result_does_not_claim_task_revision_between_check_and_claim(
    tmp_path, monkeypatch,
):
    calls = []
    host = service(
        tmp_path, monkeypatch,
        lambda request, *, timeout: (
            calls.append(json.loads(request.data)) or answer("Old scope result.", len(calls))
        ),
    )
    original_complete = host._complete_direct
    revised = []

    def revise_then_claim(control, report_ref, expected_proposal_ref):
        if not revised:
            change = host.board.revise(
                "synthetic-intervening-revision",
                mission_id=control["body"]["mission_id"],
                proposal_id=control["body"]["task_id"],
                actor_id=control["body"]["actor_id"],
                expected_version=expected_proposal_ref["version"],
                reason="The requested scope changed after the model response",
                next_action="Review new synthetic evidence",
            )
            assert change["status"] == "accepted"
            revised.append(change["proposal_ref"])
        return original_complete(control, report_ref, expected_proposal_ref)

    monkeypatch.setattr(host, "_complete_direct", revise_then_claim)
    try:
        run = host.start(LiveStartRequest(
            request_id="direct-intervening-revision",
            question="Answer the original synthetic scope", agent_ids=["agent-synthesis"],
        ))
        for _ in range(300):
            host._drive_run(host._run(run.run_id))
            control = host._control(run.run_id, "agent-synthesis")
            if control["body"].get("waiting_reason") == (
                "Completed model result needs review of a revised task version"
            ):
                break
            time.sleep(.01)
        else:
            pytest.fail("Revised task was not fenced from the old complete result")
        assert len(calls) == 1 and len(revised) == 1
        assert ref(host.store.get("board_proposal", control["body"]["task_id"])) == revised[0]
        assert host.store.records("report") and not any(
            item["body"]["completes_task"] for item in host.store.records("board_outcome")
        )
        assert host._run(run.run_id)["body"]["status"] == "active"
    finally:
        host.close()


def test_429_wait_uses_fresh_model_call_and_intent(tmp_path, monkeypatch):
    invocations = []

    def transport(request, *, timeout):
        invocations.append(json.loads(request.data))
        if len(invocations) == 1:
            raise urllib.error.HTTPError(
                "https://example.invalid", 429, "rate limit", {"Retry-After": "0.01"}, BytesIO(b"")
            )
        return answer("Recovered after the saved retry time.", 2)

    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(
        LiveStartRequest(
            request_id="mission-429", question="Recover?", agent_ids=["agent-synthesis"]
        )
    )
    drain(host, receipt.run_id)
    calls = host.calls(receipt.run_id)
    assert host.state(receipt.run_id).snapshot.mission.status == "completed"
    assert len(invocations) == 2
    assert len({item["id"] for item in calls}) == 2
    assert len({item["trace"]["intentReference"]["id"] for item in calls}) == 2
    assert {item["status"] for item in calls} == {"failed", "complete"}


def test_429_without_provider_timing_waits_route_window_instead_of_busy_retry(
    tmp_path, monkeypatch
):
    invocations = []

    def transport(request, *, timeout):
        invocations.append(time.monotonic())
        raise urllib.error.HTTPError(
            "https://example.invalid", 429, "rate limit", {}, BytesIO(b"")
        )

    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(
        LiveStartRequest(
            request_id="mission-429-no-timing",
            question="Recover?",
            agent_ids=["agent-synthesis"],
        )
    )
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline and host.store.get("capacity_quota", "groq-selected") is None:
        host._drive_run(host._run(receipt.run_id))
        time.sleep(0.01)
    assert len(invocations) == 1
    quota = host.store.get("capacity_quota", "groq-selected")
    assert quota is not None
    assert quota["body"]["blocked_until"] - time.time() > 50
    assert host.capacity.policy.quotas["groq-selected"].window_seconds == 60


def test_restart_corrects_historical_8192_reservation_without_model_dispatch(
    tmp_path, monkeypatch
):
    def no_model_call(request, *, timeout):
        pytest.fail("Restart reservation recovery dispatched a model call")

    host = service(tmp_path, monkeypatch, no_model_call)
    receipt = host.start(
        LiveStartRequest(
            request_id="mission-historical-estimate",
            question="Assess scope",
            agent_ids=["agent-synthesis"],
        )
    )
    control = host._control(receipt.run_id, "agent-synthesis")
    assert host._schedule_model(control)
    work = host._work_for(host._control(receipt.run_id, "agent-synthesis"))
    spec = host.executor._spec_from(work)
    cap_id = host.executor._capacity_id(spec)
    host.capacity.enqueue(
        cap_id, task_id=spec.proposal_ref["id"], lane=spec.lane, kind="model",
        quota_scopes=("groq-selected",), estimated_tokens=8192,
        mission_id=spec.mission_id, run_id=spec.run_id,
        attempt_id=spec.attempt_id, call_id=spec.call_id,
        budget_version=spec.configuration_ref["id"],
    )

    def historical(tx):
        current = tx.get("capacity_operation", cap_id)
        body = dict(current["body"])
        body.pop("estimate_basis")
        tx.put("capacity_operation", cap_id, body, expected_version=current["version"])
        return {"saved": True}

    host.store.transact("fixture-historical-8192", {}, historical)
    host.close()
    resumed = MissionService(
        ROOT, tmp_path, model_session=host.session,
        execution_mode="offline_test", start_supervisor=False,
    )
    corrected = resumed.capacity.get(cap_id)
    assert corrected["estimated_tokens_original"] == 8192
    assert 0 < corrected["estimated_tokens"] < 8192
    assert corrected["attempts"] == []
    assert resumed.store.records("model_response_receipt") == []


def test_restart_repairs_saved_413_with_observed_input_allowance(tmp_path, monkeypatch):
    invocations = []
    request_sizes = []

    def transport(request, *, timeout):
        invocations.append(json.loads(request.data))
        request_sizes.append(len(request.data))
        if len(invocations) == 1:
            raise urllib.error.HTTPError(
                "https://example.invalid",
                413,
                "too large",
                {
                    "Retry-After": "0.01",
                    "x-ratelimit-limit-tokens": "8000",
                    "x-ratelimit-remaining-tokens": "495",
                    "x-ratelimit-reset-tokens": "1m",
                },
                BytesIO(b""),
            )
        return answer("A compact continuation answered the question.", 2)

    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(
        LiveStartRequest(
            request_id="mission-413", question="Repair input?",
            evidence_text="DOI: 10.1234/oversized-413. "
                          + "Synthetic report detail requiring source inspection. " * 700,
            agent_ids=["agent-synthesis"],
        )
    )
    control = host._control(receipt.run_id, "agent-synthesis")
    assert host._schedule_model(control)
    control = host._control(receipt.run_id, "agent-synthesis")
    work = host._work_for(control)
    capacity_id = host.executor._capacity_id(host.executor._spec_from(work))
    host.dispatch.poll(host._run(receipt.run_id)["body"]["mission_id"])
    deadline = time.monotonic() + 4
    while time.monotonic() < deadline:
        if (
            host.store.records("model_response_receipt")
            and (cap := host.capacity.get(capacity_id))
            and cap["phase"] == "failed"
        ):
            break
        time.sleep(0.01)
    assert host.store.records("model_response_receipt")
    assert host.capacity.get(capacity_id)["phase"] == "failed"
    host._update_control(
        control, phase="waiting", waiting_reason="Older host marked 413 as terminal"
    )
    host.close()
    resumed = MissionService(
        ROOT,
        tmp_path,
        model_session=host.session,
        execution_mode="offline_test",
        start_supervisor=False,
    )
    resumed._drive_run(resumed._run(receipt.run_id))
    repaired = resumed._control(receipt.run_id, "agent-synthesis")["body"]
    assert repaired["input_budget"]["observed_limit_tokens"] == 8000
    assert repaired["phase"] in {"retry_at", "ready_model"}
    if repaired["phase"] == "retry_at":
        time.sleep(0.03)
    # The rejected request retains an uncertain capacity charge. Advance the
    # offline quota clock past its window and the explicit token reset. Without
    # reset evidence, the saved 495-token remainder must not be treated as fresh
    # capacity. This test checks 413 reconstruction, not missing-reset recovery.
    resumed.store.clock = lambda: time.time() + 61
    drain(resumed, receipt.run_id)
    assert resumed.state(receipt.run_id).snapshot.mission.status == "completed"
    assert len(invocations) == 2
    assert request_sizes[1] < request_sizes[0]
    budgets = resumed.store.records("mission_call_budget")
    assert any(item["body"]["observed_account_limit"] == 8000 for item in budgets)
    assert (
        len({item["trace"]["intentReference"]["id"] for item in resumed.calls(receipt.run_id)}) == 2
    )
    second = resumed.start(
        LiveStartRequest(
            request_id="mission-413-next-task",
            question="Use saved account headroom?",
            agent_ids=["agent-synthesis"],
        )
    )
    drain(resumed, second.run_id)
    next_budget = next(
        item
        for item in resumed.store.records("mission_call_budget")
        if item["body"]["run_id"] == second.run_id
    )
    assert next_budget["body"]["observed_account_limit"] == 8000
    assert (
        next_budget["body"]["account_limit_evidence_ref"]
        == repaired["input_budget"]["response_ref"]
    )
    assert next_budget["body"]["model_context_limit"] == 131_072
    assert resumed.state(second.run_id).snapshot.mission.status == "completed"
    assert len(invocations) == 3


@pytest.mark.parametrize("with_limit_header", [True, False])
def test_413_soft_target_escalation_needs_a_smaller_exact_request(
    tmp_path, monkeypatch, with_limit_header
):
    sent = []

    def transport(request, *, timeout):
        sent.append(json.loads(request.data))
        headers = {"Retry-After": "0.01"}
        if with_limit_header:
            headers["x-ratelimit-limit-tokens"] = "8000"
        raise urllib.error.HTTPError(
            "https://example.invalid", 413, "too large",
            headers,
            BytesIO(b""),
        )

    host = service(tmp_path, monkeypatch, transport)
    try:
        receipt = host.start(LiveStartRequest(
            request_id="same-413-payload", question="Synthetic rate-bound request",
            agent_ids=["agent-synthesis"],
        ))
        assert host._schedule_model(host._control(receipt.run_id, "agent-synthesis"))
        control = host._control(receipt.run_id, "agent-synthesis")
        work = host._work_for(control)
        capacity_id = host.executor._capacity_id(host.executor._spec_from(work))
        host.dispatch.poll(host._run(receipt.run_id)["body"]["mission_id"])
        for _ in range(400):
            cap = host.capacity.get(capacity_id)
            if cap and cap["phase"] == "failed":
                break
            time.sleep(.005)
        else:
            pytest.fail("Synthetic 413 receipt was not saved")
        if not with_limit_header:
            assert host._repair_oversize(control, work, cap) is False
            assert host._control(receipt.run_id, "agent-synthesis")["body"].get(
                "input_budget"
            ) is None
            assert len(sent) == 1
            return
        assert host._repair_oversize(control, work, cap)
        repaired = host._control(receipt.run_id, "agent-synthesis")
        failed_intent = host.store.records("model_request_intent")[0]
        assert repaired["body"]["input_budget"]["request_bytes"] == (
            failed_intent["body"]["blob"]["size"]
        )
        # Force an above-soft retry while retaining the exact 413 receipt.
        host._update_control(
            repaired, phase="ready_model", retry_at=None,
            input_budget={**repaired["body"]["input_budget"], "target_fraction": .4},
        )
        with pytest.raises(ContextOverflow):
            host._schedule_model(host._control(receipt.run_id, "agent-synthesis"))
        assert len(sent) == 1
        assert len(host.store.records("model_request_intent")) == 1
    finally:
        host.close()


def test_prior_completion_at_observed_rate_limit_stays_local(tmp_path, monkeypatch):
    sent = []

    def transport(request, *, timeout):
        sent.append(json.loads(request.data))
        response = tool("discover_capabilities", {}, 1)
        response.payload["usage"] = {
            "prompt_tokens": 5, "completion_tokens": 8000, "total_tokens": 8005,
        }
        return response

    host = service(tmp_path, monkeypatch, transport)
    try:
        receipt = host.start(LiveStartRequest(
            request_id="no-rate-reservation", question="Inspect a synthetic capability",
            agent_ids=["agent-synthesis"],
        ))
        for _ in range(200):
            host._drive_run(host._run(receipt.run_id))
            control = host._control(receipt.run_id, "agent-synthesis")
            if control["body"]["phase"] == "ready_model" and control["body"]["tool_turn_refs"]:
                break
            time.sleep(.005)
        else:
            pytest.fail("Synthetic first tool turn was not saved")
        host._update_control(control, input_budget={
            "observed_limit_tokens": 8000, "target_fraction": .70,
        })
        with pytest.raises(ContextOverflow, match="request reservation"):
            host._schedule_model(host._control(receipt.run_id, "agent-synthesis"))
        diagnostics = [item for item in host.store.records("mission_host_diagnostic")
                       if item["body"].get("category") == "local_token_rate_reservation"]
        assert len(diagnostics) == 1
        assert diagnostics[0]["body"]["prior_completion_reservation"] == 8000
        assert len(sent) == 1
        assert len(host.store.records("model_request_intent")) == 1
        assert len(host.store.records("capacity_operation")) == 2  # Model and tool only.
    finally:
        host.close()


def test_restart_retries_old_context_overreserve_with_actual_tool_estimate(
    tmp_path,
    monkeypatch,
):
    invocations = []

    def transport(request, *, timeout):
        invocations.append(json.loads(request.data))
        return answer("The corrected prepared call ran once.", 1)

    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(
        LiveStartRequest(
            request_id="mission-estimator",
            question="Can the prepared call fit?",
            agent_ids=["agent-synthesis"],
        )
    )
    control = host._control(receipt.run_id, "agent-synthesis")
    body = control["body"]
    next_call_id, attempt_id = host._model_ids(control, 1)
    config = host._configuration(control)
    call = host.configurations.start_call(
        f"mission-call:{next_call_id}",
        call_id=next_call_id,
        configuration_id=config["id"],
        mission_id=body["mission_id"],
        task_id=body["task_id"],
        attempt_id=attempt_id,
        loaded_skill_ids=list(config["body"]["skills"]),
        exposed_tool_ids=sorted(config["body"]["permitted_tools"]),
    )
    host._save(
        "agent_attempt",
        attempt_id,
        {
            "schema": "ora.mission_attempt.v1",
            "mission_id": body["mission_id"],
            "run_id": receipt.run_id,
            "task_id": body["task_id"],
            "agentId": body["actor_id"],
            "call_ref": ref(call),
            "configuration_ref": ref(config),
            "startedAt": "2026-10-09T00:00:00Z",
            "status": "running",
            "endedAt": None,
            "report_ref": None,
        },
        mission_id=body["mission_id"],
        operation_id=f"mission-attempt:{attempt_id}",
    )
    host._save(
        "mission_call_budget",
        next_call_id,
        {
            "mission_id": body["mission_id"],
            "run_id": receipt.run_id,
            "context_allowance": 1,
            "estimate_basis": "old_full_manifest",
            "estimator_version": "source_working_set_v7",
            "packer_version": "ora.working_context.packer.v20",
        },
        mission_id=body["mission_id"],
        operation_id="old-budget-fixture",
    )
    host._update_control(
        control,
        phase="waiting",
        work_id=None,
        waiting_reason="ContextOverflow: this branch needs local repair",
        input_budget={"observed_limit_tokens": 8000, "target_fraction": 0.70},
    )
    resumed = MissionService(
        ROOT,
        tmp_path,
        model_session=host.session,
        execution_mode="offline_test",
        start_supervisor=False,
    )
    assert resumed._recover_preparation(resumed._control(receipt.run_id, "agent-synthesis"))
    drain(resumed, receipt.run_id)
    old = resumed.store.get("mission_call_budget", next_call_id)
    assert old["version"] == 1 and old["body"]["estimate_basis"] == "old_full_manifest"
    latest = [
        item for item in resumed.store.records("mission_call_budget") if item["id"] != next_call_id
    ]
    assert len(latest) == 1
    assert latest[0]["body"]["estimator_version"] == ESTIMATOR_VERSION
    assert len(latest[0]["body"]["exposed_tool_ids"]) < len(config["body"]["permitted_tools"])
    assert len(invocations) == 1
    assert resumed.state(receipt.run_id).snapshot.mission.status == "completed"


def test_old_context_repair_does_not_churn_when_preparation_still_fails(tmp_path, monkeypatch):
    from ora.mission.context import ContextOverflow

    host = service(tmp_path, monkeypatch, lambda request, *, timeout: answer("unused", 1))
    receipt = host.start(
        LiveStartRequest(
            request_id="mission-context-repair-no-churn",
            question="Keep the saved preparation while blocked",
            agent_ids=["agent-synthesis"],
        )
    )
    control = host._control(receipt.run_id, "agent-synthesis")
    call_id, _ = host._model_ids(control, 1)
    budget_ref = host._save(
        "mission_call_budget",
        call_id,
        {
            "mission_id": control["body"]["mission_id"],
            "run_id": receipt.run_id,
            "estimator_version": "older_estimator",
            "packer_version": "older_packer",
        },
        mission_id=control["body"]["mission_id"],
    )
    host._update_control(
        control,
        phase="waiting",
        work_id=None,
        waiting_reason="ContextOverflow: previous preparation could not fit",
    )

    def still_overflow(_control):
        raise ContextOverflow("Required context still cannot fit")

    monkeypatch.setattr(host, "_schedule_model", still_overflow)
    for _ in range(10):
        host._drive_run(host._run(receipt.run_id))
    current = host._control(receipt.run_id, "agent-synthesis")
    assert current["body"]["phase"] == "waiting"
    assert current["body"]["preparation_repair_attempted_for"] == ref(budget_ref)
    assert current["version"] == 4  # Initial, waiting, one repair, one failed retry.
    assert host.store.records("model_response_receipt") == []


def test_model_can_page_its_exact_context_omissions(tmp_path, monkeypatch):
    requests = []

    def transport(request, *, timeout):
        body = json.loads(request.data)
        requests.append(body)
        if len(requests) == 1:
            context = json.loads(body["messages"][-1]["content"])
            return tool(
                "read_context_page",
                {"inclusion_id": context["older_records"]["inclusion_id"], "cursor": 0, "limit": 5},
                1,
            )
        return answer("The paged context was available by exact receipt.", 2)

    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(
        LiveStartRequest(
            request_id="mission-context-page",
            question="Review saved context",
            agent_ids=["agent-synthesis"],
        )
    )
    drain(host, receipt.run_id)
    assert len(requests) == 2
    tool_result = host.store.records("context_tool_result")[0]
    message = json.loads(host.store.read_blob(tool_result["body"]["blob"]["sha256"]))
    result = json.loads(message["content"])
    assert result["schema"] == "ora.context_omission_page.v2"
    assert result["total"] == 0
    assert host.state(receipt.run_id).snapshot.mission.status == "completed"


def test_agent_optional_branch_and_explicit_scope_disposition(tmp_path, monkeypatch):
    host = None
    root_calls = 0

    def transport(request, *, timeout):
        nonlocal root_calls
        body = json.loads(request.data)
        if "new_peer_work" in json.dumps(body["messages"]):
            return answer("The alternative remains uncertain.", 90)
        root_calls += 1
        if root_calls == 1:
            return tool("request_capability", {"tool_id": "propose_work"}, root_calls)
        if root_calls == 2:
            return tool(
                "propose_work",
                {
                    "objective": "Check an alternative explanation",
                    "expected_artifact": "Independent appraisal",
                    "next_action": "Inspect contrary evidence",
                    "deferred": False,
                },
                root_calls,
            )
        if root_calls == 3:
            return tool("request_capability", {"tool_id": "propose_scope_completion"}, root_calls)
        if root_calls in {4, 5}:
            optional = host.work.open_optional_work(receipt.run_id)
            return tool(
                "propose_scope_completion",
                {
                    "reason": "The requested contribution can close while the optional branch continues",
                    "decisions": [
                        {
                            "control_id": item["control_ref"]["id"],
                            "control_version": item["control_ref"]["version"],
                            "disposition": "continue",
                        }
                        for item in optional
                    ],
                },
                root_calls,
            )
        return tool(
            "publish_result",
            {
                "title": "Requested result",
                "text": "Bounded answer",
                "citations": [],
                "limitations": ["No source was supplied"],
            },
            6,
        )

    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(
        LiveStartRequest(
            request_id="mission-optional-work",
            question="Assess alternatives",
            agent_ids=["agent-synthesis"],
            required_agent_ids=["agent-synthesis"],
        )
    )
    drain(host, receipt.run_id, limit=35)
    assert host._run(receipt.run_id)["body"]["status"] == "completed"
    assert host.store.records("mission_scope_disposition")
    controls = host._controls(receipt.run_id)
    assert len(controls) == 2
    assert any(item["body"].get("scope_required") is False for item in controls)
    assert host._run(receipt.run_id)["body"]["agent_ids"] == ["agent-synthesis"]


def test_saved_provider_tool_name_rejection_gets_fresh_feedback_call(tmp_path, monkeypatch):
    invocations = []

    def transport(request, *, timeout):
        body = json.loads(request.data)
        invocations.append(body)
        if len(invocations) == 1:
            error = {
                "error": {
                    "type": "invalid_request_error",
                    "code": "tool_use_failed",
                    "message": "attempted to call tool 'pubmed.pmid' which was not in request.tools",
                }
            }
            raise urllib.error.HTTPError(
                "https://example.invalid", 400, "bad tool", {}, BytesIO(json.dumps(error).encode())
            )
        return answer("A registered function was available; bounded answer.", 2)

    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(
        LiveStartRequest(
            request_id="mission-tool-repair",
            question="Find a publication",
            agent_ids=["agent-synthesis"],
        )
    )
    host._drive_run(host._run(receipt.run_id))
    control = host._control(receipt.run_id, "agent-synthesis")
    host._update_control(
        control, phase="waiting", waiting_reason="Older host marked 400 as terminal"
    )
    resumed = MissionService(
        ROOT,
        tmp_path,
        model_session=host.session,
        execution_mode="offline_test",
        start_supervisor=False,
    )
    drain(resumed, receipt.run_id)
    assert resumed.state(receipt.run_id).snapshot.mission.status == "completed"
    assert len(invocations) == 2
    assert "search_scholarly" in json.dumps(invocations[1]["messages"])
    assert {item["status"] for item in resumed.calls(receipt.run_id)} == {"failed", "complete"}


def test_generic_tool_use_400_does_not_claim_unregistered_route(tmp_path, monkeypatch):
    def transport(request, *, timeout):
        error = {
            "error": {
                "type": "invalid_request_error",
                "code": "tool_use_failed",
                "message": "Tool validation failed for an unspecified reason",
            }
        }
        raise urllib.error.HTTPError(
            "https://example.invalid", 400, "bad tool", {}, BytesIO(json.dumps(error).encode())
        )

    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(
        LiveStartRequest(
            request_id="mission-generic-tool-400",
            question="Find a publication",
            agent_ids=["agent-synthesis"],
        )
    )
    control = host._control(receipt.run_id, "agent-synthesis")
    assert host._schedule_model(control)
    work = host._work_for(host._control(receipt.run_id, "agent-synthesis"))
    spec = host.executor._spec_from(work)
    capacity_id = host.executor._capacity_id(spec)
    host.dispatch.poll(host._run(receipt.run_id)["body"]["mission_id"])
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        response = host.store.get(
            "model_response_receipt",
            f"model-response:model-intent:{spec.call_id}:{spec.call_version}",
        )
        cap = host.capacity.get(capacity_id)
        if response and cap and cap["phase"] == "failed":
            break
        time.sleep(0.01)
    assert response["body"]["error_kind"] == "http_error"
    assert host._repair_tool_rejection(control, work, cap) is False
    assert host._control(receipt.run_id, "agent-synthesis")["body"].get("feedback") is None


def test_saved_missing_required_tool_argument_400_restarts_only_its_branch(tmp_path, monkeypatch):
    requests = []
    message = (
        "Tool call validation failed: tool call validation failed: parameters for tool "
        "search_scholarly did not match schema: errors: [missing properties: 'limit']"
    )

    def transport(request, *, timeout):
        requests.append(json.loads(request.data))
        if len(requests) == 1:
            error = {"error": {"type": "invalid_request_error", "code": "tool_use_failed",
                               "message": message}}
            raise urllib.error.HTTPError(
                "https://example.invalid", 400, "bad tool", {}, BytesIO(json.dumps(error).encode())
            )
        return answer("Synthetic bounded answer after a fresh decision.", 2)

    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(LiveStartRequest(
        request_id="missing-required-argument", question="Assess a synthetic publication",
        agent_ids=["agent-synthesis"],
    ))
    control = host._control(receipt.run_id, "agent-synthesis")
    assert host._schedule_model(control)
    work = host._work_for(host._control(receipt.run_id, "agent-synthesis"))
    spec = host.executor._spec_from(work)
    host.dispatch.poll(host._run(receipt.run_id)["body"]["mission_id"])
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        failed = host.store.get(
            "model_response_receipt",
            f"model-response:model-intent:{spec.call_id}:{spec.call_version}",
        )
        cap = host.capacity.get(host.executor._capacity_id(spec))
        if failed and cap and cap["phase"] == "failed":
            break
        time.sleep(0.01)
    assert failed and failed["body"]["error_kind"] == "http_error"
    assert len(requests) == 1
    assert host.store.records("context_tool_result") == []
    host._update_control(
        host._control(receipt.run_id, "agent-synthesis"),
        phase="waiting", waiting_reason="Older host left saved provider 400 waiting",
    )
    resumed = MissionService(
        ROOT, tmp_path, model_session=host.session,
        execution_mode="offline_test", start_supervisor=False,
    )
    drain(resumed, receipt.run_id)
    assert resumed.state(receipt.run_id).snapshot.mission.status == "completed"
    assert len(requests) == 2
    assert "required limit was missing" in json.dumps(requests[1]["messages"])
    assert resumed.store.get(**ref(failed)) == failed
    calls = resumed.store.records("agent_call")
    assert len({item["id"] for item in calls}) == 2
    rejected_call = resumed.store.get("agent_call", spec.call_id)
    assert resumed.store.get("agent_attempt", rejected_call["body"]["attempt_id"])["body"]["status"] == "failed"
    assert resumed.store.records("context_tool_result") == []


def test_missing_required_tool_argument_needs_exact_saved_schema():
    from ora.mission.service import _captured_missing_required_tool_argument

    schema = {"type": "object", "properties": {"query": {"type": "string"}},
              "required": ["query"], "additionalProperties": False}
    tools = [{"type": "function", "function": {
        "name": "search_scholarly", "parameters": schema,
    }}]
    error = {"type": "invalid_request_error", "code": "tool_use_failed", "message": (
        "Tool call validation failed: tool call validation failed: parameters for tool "
        "search_scholarly did not match schema: errors: [missing properties: 'query']"
    )}
    assert _captured_missing_required_tool_argument(tools, error) == ("search_scholarly", "query")
    schema["required"] = []
    assert _captured_missing_required_tool_argument(tools, error) is None
    schema["required"] = ["query"]
    for altered in (
        {**error, "message": error["message"].replace("'query'", "'unknown'")},
        {**error, "message": error["message"].replace("search_scholarly", "other_tool")},
        {**error, "message": "Tool validation failed for an unspecified reason"},
        {**error, "code": "other_failure"},
    ):
        assert _captured_missing_required_tool_argument(tools, altered) is None


def test_saved_null_selected_id_400_recovers_with_fresh_intent(tmp_path, monkeypatch):
    import ora.models.groq as groq_module
    from ora.mission.service import TOOL_SCHEMAS

    requests = []
    message = (
        "Tool call validation error: parameters for tool search_scholarly do not match "
        "schema: selected_id expected integer or string but got null"
    )

    def transport(request, *, timeout):
        requests.append(json.loads(request.data))
        if len(requests) == 1:
            error = {"error": {"type": "invalid_request_error", "code": "tool_use_failed",
                               "message": message}}
            raise urllib.error.HTTPError(
                "https://example.invalid", 400, "bad tool", {}, BytesIO(json.dumps(error).encode())
            )
        return answer("A bounded response after the saved correction.", 2)

    selected = TOOL_SCHEMAS["search_scholarly"]["properties"]["selected_id"]
    new_types = selected["type"]
    monkeypatch.setitem(selected, "type", ["string", "integer"])
    original_classifier = groq_module._tool_failure_kind
    monkeypatch.setattr(groq_module, "_tool_failure_kind", lambda *_args: None)
    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(LiveStartRequest(
        request_id="saved-null-selected-id", question="Assess a synthetic publication",
        agent_ids=["agent-synthesis"],
    ))
    control = host._control(receipt.run_id, "agent-synthesis")
    assert host._schedule_model(control)
    work = host._work_for(host._control(receipt.run_id, "agent-synthesis"))
    spec = host.executor._spec_from(work)
    capacity_id = host.executor._capacity_id(spec)
    host.dispatch.poll(host._run(receipt.run_id)["body"]["mission_id"])
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        failed = host.store.get(
            "model_response_receipt",
            f"model-response:model-intent:{spec.call_id}:{spec.call_version}",
        )
        cap = host.capacity.get(capacity_id)
        if failed and cap and cap["phase"] == "failed":
            break
        time.sleep(0.01)
    assert failed and failed["body"]["error_kind"] == "http_error"
    assert len(requests) == 1
    assert host.store.records("context_tool_result") == []
    assert host.store.records("retrieval_hit") == []
    control = host._control(receipt.run_id, "agent-synthesis")
    host._update_control(control, phase="waiting", waiting_reason="Older host left 400 waiting")
    control = host._control(receipt.run_id, "agent-synthesis")
    assert host._repair_tool_rejection(control, work, cap)
    repaired = host._control(receipt.run_id, "agent-synthesis")
    assert repaired["body"]["phase"] == "ready_model"
    assert repaired["body"]["feedback"]["status"] == "provider_rejected_null_selected_id"
    assert repaired["body"]["work_id"] is None
    assert len(requests) == 1  # Repair did not replay the provider call.
    monkeypatch.setitem(selected, "type", new_types)
    monkeypatch.setattr(groq_module, "_tool_failure_kind", original_classifier)
    resumed = MissionService(
        ROOT, tmp_path, model_session=host.session,
        execution_mode="offline_test", start_supervisor=False,
    )
    drain(resumed, receipt.run_id)
    assert resumed.state(receipt.run_id).snapshot.mission.status == "completed"
    assert len(requests) == 2
    assert "Omit selected_id" in json.dumps(requests[1]["messages"])
    assert resumed.store.get(**ref(failed)) == failed
    responses = resumed.store.records("model_response_receipt")
    assert len(responses) == 2
    assert responses[0]["body"]["call_ref"] != responses[1]["body"]["call_ref"]
    assert resumed.store.records("context_tool_result") == []


def test_optional_null_catalog_fields_keep_required_values_strict():
    from ora.agents.capabilities import CapabilityError, validate_arguments
    from ora.mission.service import TOOL_SCHEMAS

    report = {"title": "Synthetic result", "text": "Bounded result", "citations": [],
              "limitations": []}
    validate_arguments(TOOL_SCHEMAS["publish_result"], {
        **report, "answer_request_id": None, "scope_completion": None,
    })
    validate_arguments(TOOL_SCHEMAS["revise_work"], {
        "proposal_id": "proposal", "proposal_version": 1, "reason": "Revised",
        "objective": None, "next_action": None,
    })
    validate_arguments(TOOL_SCHEMAS["respond_invitation"], {
        "decision": "declined", "counter": None,
    })
    validate_arguments(TOOL_SCHEMAS["search_scholarly"], {
        "route": "pubmed.search", "query": "synthetic", "limit": 1,
        "selected_id": None, "selected_url": None,
    })
    for name, arguments in (
        ("publish_result", {**report, "title": None}),
        ("search_scholarly", {"route": "pubmed.search", "query": "x", "limit": None}),
        ("revise_work", {"proposal_id": None, "proposal_version": 1, "reason": "x"}),
        ("respond_invitation", {"decision": None}),
    ):
        with pytest.raises(CapabilityError):
            validate_arguments(TOOL_SCHEMAS[name], arguments)


def test_captured_null_optional_field_requires_exact_old_schema():
    from ora.mission.service import _captured_optional_null_tool_error

    schema = {"type": "object", "properties": {"answer_request_id": {"type": "string"}},
              "required": [], "additionalProperties": False}
    tools = [{"type": "function", "function": {
        "name": "publish_result", "parameters": schema,
    }}]
    error = {"type": "invalid_request_error", "code": "tool_use_failed", "message": (
        "Tool call validation failed: tool call validation failed: parameters for tool "
        "publish_result did not match schema: errors: "
        "[`/answer_request_id`: expected string, but got null]"
    )}
    assert _captured_optional_null_tool_error(tools, error) == (
        "publish_result", "answer_request_id",
    )
    schema["required"] = ["answer_request_id"]
    assert _captured_optional_null_tool_error(tools, error) is None
    schema["required"] = []
    schema["properties"]["answer_request_id"]["type"] = ["string", "null"]
    assert _captured_optional_null_tool_error(tools, error) is None
    schema["properties"]["answer_request_id"]["type"] = "string"
    assert _captured_optional_null_tool_error(tools, {
        **error, "message": error["message"].replace("answer_request_id", "title"),
    }) is None


def test_saved_null_answer_request_400_recovers_without_tool_effect(tmp_path, monkeypatch):
    from ora.mission.service import TOOL_SCHEMAS

    requests = []
    message = (
        "Tool call validation failed: tool call validation failed: parameters for tool "
        "publish_result did not match schema: errors: "
        "[`/answer_request_id`: expected string, but got null]"
    )

    def transport(request, *, timeout):
        requests.append(json.loads(request.data))
        if len(requests) == 1:
            error = {"error": {"type": "invalid_request_error", "code": "tool_use_failed",
                               "message": message}}
            raise urllib.error.HTTPError(
                "https://example.invalid", 400, "bad tool", {}, BytesIO(json.dumps(error).encode())
            )
        return answer("A bounded response after the saved correction.", 2)

    field = TOOL_SCHEMAS["publish_result"]["properties"]["answer_request_id"]
    new_types = field["type"]
    monkeypatch.setitem(field, "type", "string")
    host = service(tmp_path, monkeypatch, transport)
    receipt = host.start(LiveStartRequest(
        request_id="saved-null-answer-request", question="Assess a synthetic publication",
        agent_ids=["agent-synthesis"],
    ))
    control = host._control(receipt.run_id, "agent-synthesis")
    assert host._schedule_model(control)
    work = host._work_for(host._control(receipt.run_id, "agent-synthesis"))
    spec = host.executor._spec_from(work)
    capacity_id = host.executor._capacity_id(spec)
    host.dispatch.poll(host._run(receipt.run_id)["body"]["mission_id"])
    deadline = time.monotonic() + 3
    while time.monotonic() < deadline:
        failed = host.store.get(
            "model_response_receipt",
            f"model-response:model-intent:{spec.call_id}:{spec.call_version}",
        )
        cap = host.capacity.get(capacity_id)
        if failed and cap and cap["phase"] == "failed":
            break
        time.sleep(0.01)
    assert failed and failed["body"]["error_kind"] == "http_error"
    assert len(requests) == 1
    assert host.store.records("report") == []
    assert host.store.records("context_tool_result") == []
    control = host._control(receipt.run_id, "agent-synthesis")
    host._update_control(control, phase="waiting", waiting_reason="Older host left 400 waiting")
    control = host._control(receipt.run_id, "agent-synthesis")
    assert host._repair_tool_rejection(control, work, cap)
    repaired = host._control(receipt.run_id, "agent-synthesis")
    assert repaired["body"]["phase"] == "ready_model"
    assert repaired["body"]["feedback"]["status"] == "provider_rejected_null_optional_argument"
    assert repaired["body"]["feedback"]["function_name"] == "publish_result"
    assert repaired["body"]["feedback"]["field"] == "answer_request_id"
    assert repaired["body"]["work_id"] is None
    assert len(requests) == 1
    monkeypatch.setitem(field, "type", new_types)
    resumed = MissionService(
        ROOT, tmp_path, model_session=host.session,
        execution_mode="offline_test", start_supervisor=False,
    )
    drain(resumed, receipt.run_id)
    assert resumed.state(receipt.run_id).snapshot.mission.status == "completed"
    assert len(requests) == 2
    assert "Omit optional answer_request_id" in json.dumps(requests[1]["messages"])
    assert resumed.store.get(**ref(failed)) == failed
    responses = resumed.store.records("model_response_receipt")
    assert len(responses) == 2
    assert responses[0]["body"]["call_ref"] != responses[1]["body"]["call_ref"]


def _search_spec(host: MissionService, run_id: str, *, suffix: str) -> WorkSpec:
    control = host._control(run_id, "agent-synthesis")
    body = control["body"]
    return WorkSpec(
        work_id=f"offline-tool-{suffix}", mission_id=body["mission_id"], run_id=run_id,
        proposal_ref=body["proposal_ref"], actor_id=body["actor_id"],
        configuration_ref={}, input_refs=(), handler_id="mission-tool", handler_version="1",
        kind="tool", lane="retrieval",
    )


def test_oversized_search_preserves_failure_and_offers_scoped_recovery(tmp_path, monkeypatch):
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    try:
        run = host.start(LiveStartRequest(
            request_id="oversized-search", question="Find relevant abstracts",
            agent_ids=["agent-synthesis"],
        ))
        monkeypatch.setattr("ora.mission.service.run_discovery_page", lambda *_a, **_kw: {
            "outcome": "oversized_response", "hits": [], "window": None,
        })
        result, outputs = host._search(_search_spec(host, run.run_id, suffix="oversized"), {
            "route": "europe_pmc.search", "query": "depression trial", "limit": 5,
        })
        assert result["outcome"] == "oversized_response"
        assert result["next_request"] is None and not outputs
        assert len(result["recovery_options"]) == 2
        assert host._control(run.run_id, "agent-synthesis")["body"]["phase"] == "ready_model"
    finally:
        host.close()


@pytest.mark.parametrize("outcome", ["provider_failure", "quota_wait", "uncertain_dispatch"])
def test_pubmed_partial_leg_gives_explicit_same_query_resume(tmp_path, monkeypatch, outcome):
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    try:
        run = host.start(LiveStartRequest(
            request_id="partial-leg-resume", question="Find trial abstracts",
            agent_ids=["agent-synthesis"],
        ))
        saved_ref = {"kind": "discovery_esearch_leg", "id": "saved-leg", "version": 1}
        requests = []

        def discovery(_store, request, **_kwargs):
            requests.append(request)
            return {"outcome": outcome, "hits": [], "esearch_ref": saved_ref,
                    "esearch_retrieved_at": "2026-01-01T12:00:00Z"}

        monkeypatch.setattr("ora.mission.service.run_discovery_page", discovery)
        result, _ = host._search(_search_spec(host, run.run_id, suffix="partial"), {
            "route": "pubmed.search", "query": "depression trial", "limit": 3,
            "offset": 0, "from_date": "2020-01-01",
        })
        assert result["next_request"] is None
        assert result["esearch_retrieved_at"] == "2026-01-01T12:00:00Z"
        assert result["resume_request"] == {
            "route": "pubmed.search", "query": "depression trial", "limit": 3,
            "offset": 0, "from_date": "2020-01-01", "resume_esearch_ref": saved_ref,
        }
        assert len(requests) == 1  # Returning an option never dispatches another request.
        host._search(_search_spec(host, run.run_id, suffix="selected-resume"),
                     result["resume_request"])
        assert requests[1]["resume_esearch_ref"] == saved_ref
        assert requests[1]["query"] == requests[0]["query"]
    finally:
        host.close()


@pytest.mark.parametrize("route,expected", [
    ("pubmed", "pubmed.search"),
    ("crossref", "crossref.search"),
])
def test_documented_search_alias_preserves_selected_route_and_query(
    tmp_path, monkeypatch, route, expected
):
    calls = []

    def discovery(method, url, *, body=None, headers=None):
        calls.append(url)
        if expected == "pubmed.search":
            return HTTPResult(200, json.dumps({"esearchresult": {"idlist": [], "count": "0"}}).encode(), {})
        return HTTPResult(200, json.dumps({"message": {"items": [], "total-results": 0}}).encode(), {})

    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    host.discovery_transport = discovery
    run = host.start(LiveStartRequest(
        request_id=f"alias-{route}", question="Synthetic query", agent_ids=["agent-synthesis"],
    ))
    result, _ = host._search(_search_spec(host, run.run_id, suffix=route), {
        "route": route, "query": "synthetic comparison", "limit": 2,
    })
    assert result["route"] == expected
    assert result["requested_route"] == route
    assert len(calls) == 1
    assert "synthetic" in calls[0]


@pytest.mark.parametrize("route", ["pubmed.pmid", "pubmed.abstract"])
@pytest.mark.parametrize("omit_query", [False, True])
def test_numeric_pmid_selected_id_is_in_prepared_schema_and_exact_fetch(
    tmp_path, monkeypatch, route, omit_query
):
    calls = []
    model_requests = []

    def model_transport(request, *, timeout):
        model_requests.append(json.loads(request.data))
        if len(model_requests) == 1:
            arguments = {"route": route, "selected_id": 12345, "limit": 1}
            if not omit_query:
                arguments["query"] = ""
            return tool("search_scholarly", arguments, 1)
        return answer("The exact synthetic PMID was retrieved.", 2)

    def discovery(method, url, *, body=None, headers=None):
        calls.append(url)
        if route == "pubmed.abstract":
            return HTTPResult(200, b"<PubmedArticleSet><PubmedArticle><MedlineCitation>"
                b"<PMID>12345</PMID><Article><ArticleTitle>Synthetic PMID</ArticleTitle>"
                b"<Abstract><AbstractText>This is a project-owned abstract fixture.</AbstractText></Abstract>"
                b"</Article></MedlineCitation></PubmedArticle></PubmedArticleSet>", {})
        return HTTPResult(200, json.dumps({
            "result": {"uids": ["12345"], "12345": {"uid": "12345", "title": "Synthetic PMID"}},
        }).encode(), {})

    host = service(tmp_path, monkeypatch, model_transport)
    host.discovery_transport = discovery
    run = host.start(LiveStartRequest(
        request_id=f"numeric-pmid-{omit_query}", question="Inspect exact PMID",
        agent_ids=["agent-synthesis"],
    ))
    control = host._control(run.run_id, "agent-synthesis")
    assert host._schedule_model(control)
    call = host.store.get(**host._control(run.run_id, "agent-synthesis")["body"]["call_ref"])
    intent = host.store.get("model_request_intent", f"model-intent:{call['id']}:{call['version']}")
    prepared = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
    search = next(item["function"] for item in prepared["tools"]
                  if item["function"]["name"] == "search_scholarly")
    assert search["parameters"]["properties"]["selected_id"]["type"] == ["string", "integer", "null"]
    assert "query" not in search["parameters"]["required"]
    assert prepared["reasoning_effort"] == "high"
    drain(host, run.run_id)
    message = json.loads(host.store.read_blob(
        host.store.records("context_tool_result")[0]["body"]["blob"]["sha256"]
    ))
    result = json.loads(message["content"])
    assert result["route"] == route and result["outcome"] == "success"
    assert len(calls) == 1 and "id=12345" in calls[0]


def test_search_without_query_is_rejected_locally_before_http(tmp_path, monkeypatch):
    from ora.agents.capabilities import validate_arguments
    from ora.mission.service import TOOL_SCHEMAS, LocalToolRejection

    calls = []
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    host.discovery_transport = lambda *_args, **_kwargs: calls.append(1)
    run = host.start(LiveStartRequest(
        request_id="missing-search-query", question="Search synthetic literature",
        agent_ids=["agent-synthesis"],
    ))
    arguments = {"route": "pubmed.search", "limit": 1}
    validate_arguments(TOOL_SCHEMAS["search_scholarly"], arguments)
    with pytest.raises(LocalToolRejection) as rejected:
        host._search(_search_spec(host, run.run_id, suffix="missing-query"), arguments)
    assert rejected.value.field == "query"
    assert "search terms" in rejected.value.next_action
    with pytest.raises(LocalToolRejection) as no_selection:
        host._search(_search_spec(host, run.run_id, suffix="missing-selection"), {
            "route": "pubmed.abstract", "limit": 1,
        })
    assert no_selection.value.field == "selected_id/selected_url"
    assert calls == []


def test_exact_doi_selected_id_fetches_without_query(tmp_path, monkeypatch):
    calls = []

    def discovery(method, url, *, body=None, headers=None):
        calls.append(url)
        return HTTPResult(200, json.dumps({"message": {
            "DOI": "10.1234/synthetic", "title": ["Synthetic trial"],
            "URL": "https://doi.org/10.1234/synthetic",
        }}).encode(), {})

    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    host.discovery_transport = discovery
    run = host.start(LiveStartRequest(
        request_id="doi-without-query", question="Inspect exact synthetic DOI",
        agent_ids=["agent-synthesis"],
    ))
    result, _ = host._search(_search_spec(host, run.run_id, suffix="exact-doi"), {
        "route": "crossref.doi", "selected_id": "10.1234/synthetic", "limit": 1,
    })
    assert result["outcome"] == "success"
    assert len(calls) == 1 and calls[0].endswith("/10.1234%2Fsynthetic")


@pytest.mark.parametrize("truncated", [False, True])
def test_exact_pubmed_abstract_intake_preserves_partial_original(tmp_path, monkeypatch, truncated):
    abstract = "Project-owned abstract fixture describing a trial comparison."
    if truncated:
        abstract += " (ABSTRACT TRUNCATED AT 250 WORDS)"
    xml = ("<PubmedArticleSet><PubmedArticle><MedlineCitation><PMID>12345</PMID><Article>"
           "<ArticleTitle>Synthetic PMID</ArticleTitle><Abstract><AbstractText>" + abstract
           + "</AbstractText></Abstract></Article></MedlineCitation></PubmedArticle></PubmedArticleSet>").encode()
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    host.discovery_transport = lambda *_args, **_kwargs: HTTPResult(200, xml, {})
    run = host.start(LiveStartRequest(request_id="abstract-copy-quality", question="Read the abstract",
                                     agent_ids=["agent-synthesis"]))
    spec = _search_spec(host, run.run_id, suffix="abstract-quality")
    found, _ = host._search(spec, {"route": "pubmed.abstract", "query": "", "selected_id": 12345, "limit": 1})
    result, refs = host._inspect_hit(spec, {"hit_id": found["hits"][0]["hit_id"]})
    assert result["status"] == "inspected" and refs
    quality = host.store.records("intake_quality")[0]["body"]
    assert quality["truncated"] is truncated
    assert quality["classification"] == ("partial" if truncated else "abstract")
    assert host.store.records("source_copy")[0]["body"]["inspection_state"] == "checked"
    assert host.store.records("publication")[0]["body"]["verified_identifiers"]["pmid"] == "12345"


@pytest.mark.parametrize("route", ["pubmed.pmid", "pubmed.abstract"])
def test_metadata_only_hit_offers_optional_identifier_search_example(tmp_path, monkeypatch, route):
    calls = []

    def discovery(method, url, *, body=None, headers=None):
        calls.append(url)
        if route == "pubmed.abstract":
            return HTTPResult(200, b"<PubmedArticleSet><PubmedArticle><MedlineCitation>"
                b"<PMID>12345</PMID><Article><ArticleTitle>Synthetic PMID</ArticleTitle>"
                b"</Article></MedlineCitation></PubmedArticle></PubmedArticleSet>", {})
        return HTTPResult(200, json.dumps({
            "result": {"uids": ["12345"], "12345": {"uid": "12345", "title": "Synthetic PMID"}},
        }).encode(), {})

    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    host.discovery_transport = discovery
    run = host.start(LiveStartRequest(
        request_id="metadata-search-example", question="Inspect a synthetic hit",
        agent_ids=["agent-synthesis"],
    ))
    spec = _search_spec(host, run.run_id, suffix="metadata-example")
    found, _ = host._search(spec, {
        "route": route, "query": "", "selected_id": 12345, "limit": 1,
    })
    result, refs = host._inspect_hit(spec, {"hit_id": found["hits"][0]["hit_id"]})
    assert result["status"] == "metadata_only" and refs == ()
    assert result["source_text_request_example"]["tool"] == "search_scholarly"
    expected = {
        "route": "pubmed.abstract", "selected_id": "12345", "query": "", "limit": 1,
    } if route == "pubmed.pmid" else {
        "route": "europe_pmc.search", "query": "EXT_ID:12345 AND SRC:MED", "limit": 1,
    }
    assert result["source_text_request_example"]["arguments"] == expected
    assert len(calls) == 1 and host.store.records("source_copy") == []


@pytest.mark.parametrize("offset", [0, 2, 100])
def test_europe_pmc_offset_rejects_locally_without_cursor_guess(
    tmp_path, monkeypatch, offset
):
    calls = []
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    host.discovery_transport = lambda *args, **kwargs: calls.append((args, kwargs))
    run = host.start(LiveStartRequest(
        request_id=f"epmc-offset-{offset}", question="Synthetic query",
        agent_ids=["agent-synthesis"],
    ))
    with pytest.raises(ValueError, match="offset.*Europe PMC"):
        host._search(_search_spec(host, run.run_id, suffix="offset"), {
            "route": "europe_pmc.search", "query": "synthetic", "limit": 1, "offset": offset,
        })
    assert calls == []


@pytest.mark.parametrize("requested", [2000, 3000])
def test_large_source_window_returns_bounded_exact_continuation(
    tmp_path, monkeypatch, requested
):
    calls = []
    source_ref = None

    def transport(request, *, timeout):
        body = json.loads(request.data)
        calls.append(body)
        if len(calls) == 1:
            return tool("read_source_window", {
                "source_id": source_ref["id"], "source_version": source_ref["version"],
                "block_offset": 0, "char_offset": 0, "char_limit": requested,
                "selection_mode": "replace",
            }, 1)
        if len(calls) == 2:
            prior = json.loads(next(item["content"] for item in reversed(body["messages"])
                                    if item["role"] == "tool"))
            return tool("read_source_window", prior["next_request"], 2)
        return answer("The exact bounded source was read.", 3)

    host = service(tmp_path, monkeypatch, transport)
    run = host.start(LiveStartRequest(
        request_id=f"large-window-{requested}", question="Read synthetic source",
        evidence_text="A synthetic source passage. " * 200,
        agent_ids=["agent-synthesis"],
    ))
    source_ref = host._control(run.run_id, "agent-synthesis")["body"]["source_refs"][0]
    drain(host, run.run_id)
    windows_by_offset = {}
    for item in host.store.records("context_tool_result"):
        message = json.loads(host.store.read_blob(item["body"]["blob"]["sha256"]))
        if message["name"] == "read_source_window":
            page = json.loads(message["content"])
            saved_window = host.store.get(**page["window_ref"])
            windows_by_offset[saved_window["body"]["char_offset"]] = page
    result = windows_by_offset[0]
    assert result["effective_char_limit"] == requested
    assert result["requested_char_limit"] == requested
    assert result["returned_chars"] == requested
    assert result["passages"][0]["locator"].endswith(f"chars=0:{requested}")
    assert result["next_request"] == {
        "source_id": source_ref["id"], "source_version": source_ref["version"],
        "block_offset": 0, "char_offset": requested, "char_limit": requested,
        "selection_mode": "replace",
    }
    assert f"chars={requested}:" in windows_by_offset[requested][
        "passages"
    ][0]["locator"]
    windows = sorted(host.store.records("source_read_window"),
                     key=lambda item: item["body"]["char_offset"])
    assert len(windows) == 2
    assert [item["body"]["char_offset"] for item in windows] == [0, requested]
    assert [item["body"]["char_limit"] for item in windows] == [requested, requested]
    assert all(item["body"]["copy_ref"] == source_ref for item in windows)
    spans = [host.store.get(**item["body"]["passage_refs"][0])["body"] for item in windows]
    assert spans[0]["locator"].endswith(f"chars=0:{requested}")
    assert f"chars={requested}:" in spans[1]["locator"]


@pytest.mark.parametrize("requested", [50, 100])
def test_context_page_large_limit_uses_existing_inclusion_and_reports_effective_size(
    tmp_path, monkeypatch, requested
):
    calls = []

    def transport(request, *, timeout):
        body = json.loads(request.data)
        calls.append(body)
        if len(calls) == 1:
            context = json.loads(body["messages"][-1]["content"])
            return tool("read_context_page", {
                "inclusion_id": context["older_records"]["inclusion_id"],
                "cursor": 0, "limit": requested,
            }, 1)
        return answer("The exact context page was returned.", 2)

    host = service(tmp_path, monkeypatch, transport)
    run = host.start(LiveStartRequest(
        request_id=f"context-large-{requested}", question="Review saved context",
        agent_ids=["agent-synthesis"],
    ))
    drain(host, run.run_id)
    message = json.loads(host.store.read_blob(
        host.store.records("context_tool_result")[0]["body"]["blob"]["sha256"]
    ))
    result = json.loads(message["content"])
    assert result["requested_limit"] == requested
    assert result["effective_limit"] == 20
    assert result["next_cursor"] is None


@pytest.mark.parametrize("policy_order", [(1, 2), (2, 1)])
def test_prepared_publish_schema_tracks_saved_participation_policy(
    tmp_path, monkeypatch, policy_order
):
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    for number, policy in enumerate(policy_order):
        run = host.start(LiveStartRequest(
            request_id=f"policy-{number}-{policy}", question="Synthetic scope",
            agent_ids=["agent-synthesis"],
        ))
        if policy == 1:
            current = host._run(run.run_id)
            host._save("mission_run", run.run_id,
                       {**current["body"], "participation_policy_version": 1},
                       mission_id=current["body"]["mission_id"])
        assert host._schedule_model(host._control(run.run_id, "agent-synthesis"))
        call = host.store.get(**host._control(run.run_id, "agent-synthesis")["body"]["call_ref"])
        intent = host.store.get("model_request_intent", f"model-intent:{call['id']}:{call['version']}")
        prepared = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        publish = next(item["function"] for item in prepared["tools"]
                       if item["function"]["name"] == "publish_result")
        assert ("scope_completion" in publish["parameters"]["properties"]) == (policy == 2)
        assert ("For this saved participation policy" in publish["description"]) == (policy == 1)
        assert prepared["reasoning_effort"] == "high"


@pytest.mark.parametrize("name,args,expected_field", [
    ("read_source_window", {"source_id": "outside-source", "source_version": 0,
                            "block_offset": 0, "char_offset": 0, "char_limit": 2000}, "arguments"),
    ("read_source_window", {"source_id": "outside-source", "source_version": 1,
                            "block_offset": 0, "char_offset": 0, "char_limit": 2000},
     "source_id/source_version"),
    ("fetch_source", {"hit_id": "missing-hit"}, "hit_id"),
    ("evidence_lookup", {"source_id": "outside-source"}, "source_id"),
])
def test_invalid_exact_reference_is_scoped_local_rejection(
    tmp_path, monkeypatch, name, args, expected_field
):
    model_calls = []
    discovery_calls = []

    def transport(request, *, timeout):
        model_calls.append(json.loads(request.data))
        count = len(model_calls)
        if count == 1 and name in {"fetch_source", "evidence_lookup"}:
            return tool("request_capability", {"tool_id": name}, count)
        if count == 1 or count == 2 and name in {"fetch_source", "evidence_lookup"}:
            return tool(name, args, count)
        return answer("The invalid reference was not used.", count)

    host = service(tmp_path, monkeypatch, transport)
    host.discovery_transport = lambda *a, **kw: discovery_calls.append((a, kw))
    run = host.start(LiveStartRequest(
        request_id=f"invalid-{name}-{expected_field}", question="Inspect exact input",
        evidence_text="A permitted synthetic source.", agent_ids=["agent-synthesis"],
    ))
    drain(host, run.run_id)
    messages = [
        json.loads(host.store.read_blob(item["body"]["blob"]["sha256"]))
        for item in host.store.records("context_tool_result")
    ]
    rejected = next(json.loads(item["content"]) for item in messages if item["name"] == name)
    assert rejected["status"] == "rejected"
    assert rejected["field"] == expected_field
    assert rejected["external_action"] == "none"
    assert rejected["next_action"]
    assert discovery_calls == []
    assert host.store.records("source_read_window") == []


@pytest.mark.parametrize("name", ["read_source_window", "evidence_lookup"])
def test_existing_foreign_source_is_not_read_or_disclosed(tmp_path, monkeypatch, name):
    model_calls = []
    outside_ref = None

    def transport(request, *, timeout):
        model_calls.append(json.loads(request.data))
        count = len(model_calls)
        if count == 1 and name == "evidence_lookup":
            return tool("request_capability", {"tool_id": name}, count)
        if count == 1 or count == 2 and name == "evidence_lookup":
            args = ({"source_id": outside_ref["id"]} if name == "evidence_lookup" else {
                "source_id": outside_ref["id"], "source_version": outside_ref["version"],
                "block_offset": 0, "char_offset": 0, "char_limit": 2000,
            })
            return tool(name, args, count)
        return answer("No foreign source was read.", count)

    host = service(tmp_path, monkeypatch, transport)
    first = host.start(LiveStartRequest(
        request_id=f"foreign-source-reader-{name}", question="Read assigned source",
        evidence_text="Assigned synthetic text.", agent_ids=["agent-synthesis"],
    ))
    foreign = host.start(LiveStartRequest(
        request_id=f"foreign-source-owner-{name}", question="Different mission",
        evidence_text="Foreign synthetic content that must remain scoped.",
        agent_ids=["agent-synthesis"],
    ))
    outside_ref = host._control(foreign.run_id, "agent-synthesis")["body"]["source_refs"][0]
    assert host.store.get(**outside_ref) is not None
    assert outside_ref not in host._control(first.run_id, "agent-synthesis")["body"]["source_refs"]
    drain(host, first.run_id)
    messages = [
        json.loads(host.store.read_blob(item["body"]["blob"]["sha256"]))
        for item in host.store.records("context_tool_result")
    ]
    rejected = next(json.loads(item["content"]) for item in messages if item["name"] == name)
    assert rejected["status"] == "rejected" and rejected["external_action"] == "none"
    assert outside_ref["id"] not in json.dumps(rejected)
    assert "Foreign synthetic content" not in json.dumps(rejected)
    assert host.store.records("source_read_window") == []


def test_existing_foreign_hit_cannot_dispatch_fetch(tmp_path, monkeypatch):
    model_calls = []
    hit_id = None
    fetch_calls = []

    def transport(request, *, timeout):
        model_calls.append(json.loads(request.data))
        count = len(model_calls)
        if count == 1:
            return tool("request_capability", {"tool_id": "fetch_source"}, count)
        if count == 2:
            return tool("fetch_source", {"hit_id": hit_id}, count)
        return answer("Foreign hit access was rejected.", count)

    def discovery(method, url, *, body=None, headers=None):
        return HTTPResult(200, json.dumps({
            "hitCount": 1, "nextCursorMark": "*",
            "resultList": {"result": [{
                "source": "MED", "id": "765", "pmid": "765",
                "title": "Foreign synthetic hit", "abstractText": "Foreign abstract.",
            }]},
        }).encode(), {})

    def forbidden_fetch(*args, **kwargs):
        fetch_calls.append((args, kwargs))
        raise AssertionError("foreign hit must not dispatch a public fetch")

    host = service(tmp_path, monkeypatch, transport)
    host.discovery_transport = discovery
    monkeypatch.setattr("ora.mission.service.fetch_public_source", forbidden_fetch)
    reader = host.start(LiveStartRequest(
        request_id="foreign-hit-reader", question="Inspect assigned hits",
        agent_ids=["agent-synthesis"],
    ))
    owner = host.start(LiveStartRequest(
        request_id="foreign-hit-owner", question="Different mission",
        agent_ids=["agent-synthesis"],
    ))
    discovered, _ = host._search(_search_spec(host, owner.run_id, suffix="foreign-hit"), {
        "route": "europe_pmc.search", "query": "synthetic", "limit": 1,
    })
    hit_id = discovered["hits"][0]["hit_id"]
    assert host.store.get("retrieval_hit", hit_id) is not None
    drain(host, reader.run_id)
    messages = [
        json.loads(host.store.read_blob(item["body"]["blob"]["sha256"]))
        for item in host.store.records("context_tool_result")
    ]
    rejected = next(json.loads(item["content"]) for item in messages
                    if item["name"] == "fetch_source")
    assert rejected["status"] == "rejected"
    assert rejected["field"] == "hit_id" and rejected["external_action"] == "none"
    assert hit_id not in json.dumps(rejected)
    assert "Foreign synthetic hit" not in json.dumps(rejected)
    assert fetch_calls == []


def test_europe_pmc_result_gives_exact_reusable_cursor_request(tmp_path, monkeypatch):
    calls = []

    def discovery(method, url, *, body=None, headers=None):
        calls.append(url)
        second = "cursorMark=opaque-next" in url
        return HTTPResult(200, json.dumps({
            "hitCount": 2, "nextCursorMark": "opaque-next" if not second else "opaque-next",
            "resultList": {"result": [{
                "source": "MED", "id": "456" if second else "123",
                "pmid": "456" if second else "123",
                "title": "Synthetic second page" if second else "Synthetic first page",
                "abstractText": "Synthetic second abstract." if second else "Synthetic first abstract.",
            }]},
        }).encode(), {})

    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: None)
    host.discovery_transport = discovery
    run = host.start(LiveStartRequest(
        request_id="epmc-next-request", question="Page a synthetic query",
        agent_ids=["agent-synthesis"],
    ))
    result, _ = host._search(_search_spec(host, run.run_id, suffix="cursor"), {
        "route": "europe_pmc.search", "query": "synthetic study", "limit": 1,
    })
    assert result["next_request"] == {
        "route": "europe_pmc.search", "query": "synthetic study",
        "limit": 1, "cursor": "opaque-next",
    }
    second, _ = host._search(_search_spec(host, run.run_id, suffix="cursor-next"),
                             result["next_request"])
    assert second["next_request"] is None
    assert len(calls) == 2 and "cursorMark=%2A" in calls[0]
    assert "cursorMark=opaque-next" in calls[1]
    assert result["hits"][0]["hit_id"] != second["hits"][0]["hit_id"]


def test_model_search_filters_reach_route_and_survive_cursor_continuation(
    tmp_path, monkeypatch
):
    from ora.mission.service import TOOL_SCHEMAS

    calls = []

    def discovery(method, url, *, body=None, headers=None):
        calls.append(url)
        packet = {"hitCount": 2, "nextCursorMark": "opaque-next", "resultList": {
            "result": [{"source": "MED", "id": str(len(calls)),
                        "pmid": str(len(calls)), "title": "Synthetic dated study",
                        "abstractText": "Synthetic abstract."}]
        }}
        return HTTPResult(200, json.dumps(packet).encode(), {})

    assert "from_date" in TOOL_SCHEMAS["search_scholarly"]["properties"]
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    host.discovery_transport = discovery
    run = host.start(LiveStartRequest(
        request_id="filtered-search", question="Synthetic dated query",
        agent_ids=["agent-synthesis"],
    ))
    request = {
        "route": "europe_pmc.search", "query": "synthetic depression trial", "limit": 1,
        "from_date": "2020-01-01", "until_date": "2025-12-31",
        "journal": "Synthetic Journal",
    }
    first, _ = host._search(_search_spec(host, run.run_id, suffix="filtered-1"), request)
    assert first["next_request"] == {**request, "cursor": "opaque-next"}
    host._search(_search_spec(host, run.run_id, suffix="filtered-2"), first["next_request"])
    assert len(calls) == 2
    assert all("FIRST_PDATE" in url and "JOURNAL" in url for url in calls)
    assert "cursorMark=opaque-next" in calls[1]
    with pytest.raises(ValueError, match="route_filters"):
        host._search(_search_spec(host, run.run_id, suffix="wrong-filter"), {
            **request, "route": "pubmed.search", "journal": "Synthetic Journal",
        })
    assert len(calls) == 2  # Unsupported fields are rejected before HTTP.


def test_exa_contents_selection_options_reach_one_call_and_saved_result(
    tmp_path, monkeypatch
):
    from ora.mission.service import TOOL_SCHEMAS

    calls = []

    def discovery(method, url, *, body=None, headers=None):
        calls.append((method, url, body))
        return HTTPResult(200, json.dumps({"results": [{
            "url": "https://example.org/synthetic", "text": "Synthetic article text",
        }]}).encode(), {})

    monkeypatch.setenv("EXA_API_KEY", "synthetic-exa-token")
    assert "selection_reason" in TOOL_SCHEMAS["search_scholarly"]["properties"]
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    host.discovery_transport = discovery
    run = host.start(LiveStartRequest(
        request_id="exa-selection", question="Inspect synthetic source",
        agent_ids=["agent-synthesis"],
    ))
    result, _ = host._search(_search_spec(host, run.run_id, suffix="exa-selection"), {
        "route": "exa.contents", "limit": 1,
        "selected_url": "https://example.org/synthetic",
        "max_characters": 4000, "max_age_hours": 24,
        "selection_reason": "Inspect this exact candidate",
    })
    assert len(calls) == 1
    assert calls[0][2]["urls"] == ["https://example.org/synthetic"]
    assert calls[0][2]["text"]["maxCharacters"] == 4000
    assert calls[0][2]["maxAgeHours"] == 24
    assert result["selection"]["reason_status"] == "explicit"
    assert result["selection"]["freshness_basis"] == "explicit_max_age_hours"
    assert len(result["hits"]) == 1


def test_legacy_policy_publishes_valid_contribution_without_inline_scope_upgrade(
    tmp_path, monkeypatch
):
    calls = []

    def transport(request, *, timeout):
        calls.append(json.loads(request.data))
        return tool("publish_result", {
            "title": "Legacy contribution", "text": "A bounded synthetic conclusion.",
            "citations": [], "limitations": ["No external evidence in this fixture"],
        }, len(calls))

    host = service(tmp_path, monkeypatch, transport)
    run = host.start(LiveStartRequest(
        request_id="legacy-valid-publication", question="Synthetic scope",
        agent_ids=["agent-synthesis"],
    ))
    current = host._run(run.run_id)
    host._save("mission_run", run.run_id,
               {**current["body"], "participation_policy_version": 1},
               mission_id=current["body"]["mission_id"])
    drain(host, run.run_id)
    assert host.store.records("report")[0]["body"]["body"] == "A bounded synthetic conclusion."
    assert host.state(run.run_id).snapshot.mission.status == "completed"
    publish = next(item["function"] for item in calls[0]["tools"]
                   if item["function"]["name"] == "publish_result")
    assert "scope_completion" not in publish["parameters"]["properties"]


@pytest.mark.parametrize("legacy_policy", [1, None])
def test_older_pinned_inline_completion_obeys_saved_legacy_policy(
    tmp_path, monkeypatch, legacy_policy
):
    calls = []

    def transport(request, *, timeout):
        calls.append(json.loads(request.data))
        return tool("publish_result", {
            "title": "Pinned contribution", "text": "A retained synthetic contribution.",
            "citations": [], "limitations": ["Synthetic fixture"],
            "scope_completion": {"reason": "Requested scope answered", "decisions": []},
        }, len(calls))

    host = service(tmp_path, monkeypatch, transport)
    run = host.start(LiveStartRequest(
        request_id="older-pinned-inline", question="Synthetic scope",
        agent_ids=["agent-synthesis"], required_agent_ids=["agent-synthesis"],
    ))
    current = host._run(run.run_id)
    saved_body = {**current["body"]}
    if legacy_policy is None:
        saved_body.pop("participation_policy_version")
    else:
        saved_body["participation_policy_version"] = legacy_policy
    host._save("mission_run", run.run_id, saved_body,
               mission_id=current["body"]["mission_id"])
    control = host._control(run.run_id, "agent-synthesis")
    assert control["body"]["scope_required"] is True
    actual_registry = host._tool_registry
    host._tool_registry = lambda _control: (host.catalog, host.configurations)
    try:
        assert host._schedule_model(control)
    finally:
        host._tool_registry = actual_registry
    call = host.store.get(**host._control(run.run_id, "agent-synthesis")["body"]["call_ref"])
    assert "scope_completion" in call["body"]["permitted_tools"]["publish_result"]["schema"]["properties"]
    drain(host, run.run_id)
    assert host.store.records("report")[0]["body"]["body"] == "A retained synthetic contribution."
    assert len(host.store.records("mission_scope_disposition")) == 1
    assert host._run(run.run_id)["body"].get("participation_policy_version") == legacy_policy
    assert host.state(run.run_id).snapshot.mission.status == "completed"
    artifacts = host.store.records("executor_artifact")
    assert any(item["body"]["payload"] and
               item["body"]["payload"].get("scope_completion_policy_version") == 1
               for item in artifacts)
    before = (len(host.store.records("report")),
              len(host.store.records("mission_scope_disposition")),
              len(host.store.records("model_request_intent")))
    intent_hash = host.store.get(
        "model_request_intent", f"model-intent:{call['id']}:{call['version']}"
    )["body"]["request_sha256"]
    host.close()
    resumed = MissionService(
        ROOT, tmp_path, model_session=host.session,
        execution_mode="offline_test", start_supervisor=False,
    )
    resumed._drive_run(resumed._run(run.run_id))
    assert (len(resumed.store.records("report")),
            len(resumed.store.records("mission_scope_disposition")),
            len(resumed.store.records("model_request_intent"))) == before
    assert resumed.store.get(
        "model_request_intent", f"model-intent:{call['id']}:{call['version']}"
    )["body"]["request_sha256"] == intent_hash


@pytest.mark.parametrize("case", ["incomplete", "stale", "optional_actor"])
def test_old_pinned_completion_cannot_skip_legacy_optional_coverage(
    tmp_path, monkeypatch, case
):
    selected_decisions = []
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: None)
    run = host.start(LiveStartRequest(
        request_id=f"pinned-legacy-{case}", question="Synthetic scope",
        agent_ids=["agent-synthesis"], required_agent_ids=["agent-synthesis"],
    ))
    saved = host._run(run.run_id)
    host._save("mission_run", run.run_id,
               {**saved["body"], "participation_policy_version": 1},
               mission_id=saved["body"]["mission_id"])
    control = host._control(run.run_id, "agent-synthesis")
    optional = host.work.propose(
        f"synthetic-optional-{case}", run_ref=ref(host._run(run.run_id)),
        actor_id=control["body"]["actor_id"], source_control_ref=ref(control),
        proposal_id=f"optional-{case}", objective="Check another explanation",
        expected_artifact="Optional appraisal", next_action="Inspect another source",
        branch_of_ref=control["body"]["proposal_ref"],
    )
    if case == "stale":
        old_optional_ref = optional["control_ref"]
        host._update_control(host.store.get(**old_optional_ref), waiting_reason="Updated version")
        selected_decisions.append({
            "control_id": old_optional_ref["id"],
            "control_version": old_optional_ref["version"], "disposition": "continue",
        })
    target_id = (optional["control_ref"]["id"] if case == "optional_actor" else control["id"])
    target = host.store.get("mission_task_control", target_id)
    actual_registry = host._tool_registry
    host._tool_registry = lambda _control: (host.catalog, host.configurations)
    try:
        assert host._schedule_model(target)
    finally:
        host._tool_registry = actual_registry
    prepared = host.store.get("mission_task_control", target_id)
    call = host.store.get(**prepared["body"]["call_ref"])
    assert "scope_completion" in call["body"]["permitted_tools"]["publish_result"]["schema"]["properties"]
    requested = {
        "id": "old-pinned-inline", "name": "publish_result",
        "arguments": {
            "title": "Rejected scoped contribution", "text": "A synthetic draft.",
            "citations": [], "limitations": ["Synthetic fixture"],
            "scope_completion": {
                "reason": "Requested scope answered", "decisions": selected_decisions,
            },
        },
    }
    host._update_control(prepared, phase="pending_tool", tool_call=requested)
    current_control = host.store.get("mission_task_control", target_id)
    work_id = host._tool_id(current_control, requested["id"])
    host._update_control(current_control, work_id=work_id)
    current_control = host.store.get("mission_task_control", target_id)
    body = current_control["body"]
    spec = WorkSpec(
        work_id=work_id,
        mission_id=body["mission_id"], run_id=run.run_id,
        proposal_ref=body["proposal_ref"], actor_id=body["actor_id"],
        configuration_ref=ref(host.store.get("agent_configuration", call["body"]["configuration_id"])),
        input_refs=(), handler_id="mission-tool", handler_version="1", kind="tool",
        lane="compute", call_id=call["id"], call_version=call["version"],
    )
    rejected = host._tool_work(spec).payload["result"]
    assert rejected["status"] == "rejected"
    if case == "optional_actor":
        assert rejected["field"] == "scope_completion"
        assert rejected["condition"] == "actor is not a requested participant"
        assert rejected["external_action"] == "none"
    else:
        assert "Open optional work changed" in rejected["reason"]
    assert host.store.records("mission_scope_disposition") == []
    assert all(item["body"]["body"] != "A synthetic draft."
               for item in host.store.records("report"))
    assert host.state(run.run_id).snapshot.mission.status == "active"


def test_operator_selects_one_primary_run_without_waking_historical_work(tmp_path, monkeypatch):
    from ora.credentials import GroqCredentialProfiles

    requests = []

    def transport(request, *, timeout):
        requests.append(json.loads(request.data))
        return answer("Synthetic scoped answer.", len(requests))

    host = service(tmp_path, monkeypatch, transport)
    config_root = tmp_path / "operator-config"
    config_path = config_root / ".local/groq-credential-profiles.json"
    config_path.parent.mkdir(parents=True)

    def select(runs, *, enabled=True):
        config_path.write_text(json.dumps({"version": 1, "profiles": {
            "primary": {"enabled": enabled, "allowed_run_ids": runs},
            "backup": {"enabled": False},
        }}))
        host.credential_profiles = GroqCredentialProfiles(config_root)

    try:
        old = host.start(LiveStartRequest(
            request_id="selective-old", question="Earlier fixture task",
            agent_ids=["agent-synthesis"], credential_profile="primary",
        ))
        assert host._schedule_model(host._control(old.run_id, "agent-synthesis"))
        queued = host._control(old.run_id, "agent-synthesis")["body"]
        old_run = host._run(old.run_id)
        select([], enabled=False)
        host._drive_run(old_run)
        held_phase = host._control(old.run_id, "agent-synthesis")["body"]["operator_credential_hold"]
        new = host.start(LiveStartRequest(
            request_id="selective-new", question="New fixture task",
            agent_ids=["agent-synthesis"], credential_profile="primary",
        ))
        backup = host.start(LiveStartRequest(
            request_id="selective-backup", question="Paused fixture task",
            agent_ids=["agent-synthesis"], credential_profile="backup",
        ))
        select([new.run_id])
        for _ in range(3):
            host._drive_run(old_run)
            host._drive_run(host._run(backup.run_id))
        held = host._control(old.run_id, "agent-synthesis")["body"]
        assert "enabled only for selected runs" in held["waiting_reason"]
        assert held["operator_credential_hold"] == held_phase
        assert held["call_ref"] == queued["call_ref"]
        assert requests == []
        drain(host, new.run_id)
        assert host._run(new.run_id)["body"]["status"] == "completed"
        assert len(requests) == 1
        assert host._run(old.run_id) == old_run
        assert host._control(old.run_id, "agent-synthesis")["body"]["phase"] == "waiting"
        assert not host.credential_profiles.is_run_enabled("backup", backup.run_id)
        select([old.run_id])
        with monkeypatch.context() as excluded:
            def unexpected_delivery(_mission_id):
                pytest.fail("Excluded completed run must not deliver late mail or activate obligations")

            excluded.setattr(host, "_deliver_pending", unexpected_delivery)
            assert host._drive_run(host._run(new.run_id)) == (False, None)
        drain(host, old.run_id)
        assert host._run(old.run_id)["body"]["status"] == "completed"
        assert len(requests) == 2
        assert host._control(old.run_id, "agent-synthesis")["body"]["call_ref"] == queued["call_ref"]
        assert host._run(old.run_id)["body"]["profile"] == old_run["body"]["profile"]
    finally:
        host.close()
