"""A verified provider-rate hint stays usable across missions in one local store."""

from __future__ import annotations

import json

import ora.models.session as session_module
from ora.contracts.models import LiveFollowupRequest, LiveStartRequest
from ora.mission import MissionService
from ora.models import GroqSelectedAdapter
from tests.mission.test_cross_run_provider_limits import _project
from tests.mission.test_service import answer, drain


def _adapter(monkeypatch, observed_limit, packets, *, header_calls=(1,)):
    def transport(request, *, timeout):
        packets.append(json.loads(request.data))
        response = answer("A provisional synthetic result with no scientific claim.", len(packets))
        response.headers = ({"x-ratelimit-limit-tokens": str(observed_limit)}
                            if len(packets) in header_calls else {})
        return response

    monkeypatch.setattr(
        session_module, "GroqSelectedAdapter",
        lambda root: GroqSelectedAdapter(root, transport=transport),
    )


def _complete_one(host, request_id):
    run = host.start(LiveStartRequest(
        request_id=request_id,
        question="Interpret the supplied synthetic field reading with its limitation.",
        evidence_text=("Project-authored synthetic record: A 6.4 mg/L, B 6.7 mg/L; "
                       "a possible +0.4 mg/L high bias at B remains unresolved."),
        agent_ids=["agent-synthesis"],
    ))
    drain(host, run.run_id)
    assert host._run(run.run_id)["body"]["status"] == "completed"
    return run


def _followup(host, parent, request_id, *, credential_profile=None):
    snapshot = host.state(parent.run_id).snapshot
    artifact = snapshot.artifacts[0]
    return host.followup(LiveFollowupRequest(
        request_id=request_id, parent_run_id=parent.run_id,
        parent_snapshot_id=snapshot.snapshot_id,
        target_id=artifact.id, target_version=artifact.version,
        question="Audit that provisional interpretation against the original field record.",
        agent_ids=["agent-synthesis"], credential_profile=credential_profile,
    ))


def test_followup_reuses_verified_hint_already_imported_by_parent(tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    packets = []
    _adapter(monkeypatch, 8000, packets)
    donor = MissionService(project, tmp_path / "donor", execution_mode="live",
                           start_supervisor=False)
    try:
        _complete_one(donor, "synthetic-limit-donor")
    finally:
        donor.close()
    assert len(packets) == 1

    parent_host = MissionService(project, tmp_path / "shared", execution_mode="live",
                                 start_supervisor=False)
    try:
        parent = _complete_one(parent_host, "synthetic-parent")
        assert len(packets) == 2
        imported = parent_host.store.records("provider_limit_observation")
        assert len(imported) == 1
        assert imported[0]["body"]["observed_limit_tokens"] == 8000

        child = _followup(parent_host, parent, "synthetic-child")
        control = parent_host._control(child.run_id, "agent-synthesis")
        budget_hint = parent_host._effective_input_budget(control)
        assert budget_hint["observed_limit_tokens"] == 8000
        assert budget_hint["response_ref"]["id"] == imported[0]["id"]
        assert parent_host._schedule_model(control)
        prepared = parent_host._control(child.run_id, "agent-synthesis")
        call = parent_host.store.get(**prepared["body"]["call_ref"])
        budget = parent_host.store.get("mission_call_budget", call["id"])["body"]
        assert budget["observed_account_limit"] == 8000
        assert budget["account_limit_evidence_ref"] == budget_hint["response_ref"]
        assert (budget["exact_complete_request_estimate_tokens"]
                + budget["prior_completion_reservation"]
                + budget["rate_input_headroom_tokens"] <= 8000)
        assert len(parent_host.store.records("provider_limit_observation")) == 1
        assert len(packets) == 2  # Preparing the child does not dispatch it.

        newer_packets = []
        _adapter(monkeypatch, 9000, newer_packets)
        newer_donor = MissionService(project, tmp_path / "newer-donor",
                                     execution_mode="live", start_supervisor=False)
        try:
            _complete_one(newer_donor, "synthetic-newer-donor")
        finally:
            newer_donor.close()
        assert len(newer_packets) == 1
        changed = _followup(parent_host, parent, "synthetic-changed-child")
        changed_hint = parent_host._effective_input_budget(
            parent_host._control(changed.run_id, "agent-synthesis"))
        assert changed_hint["observed_limit_tokens"] == 9000
        assert changed_hint["response_ref"] != budget_hint["response_ref"]
        assert len(parent_host.store.records("provider_limit_observation")) == 2
    finally:
        parent_host.close()


def test_mismatched_profile_does_not_reuse_primary_hint(tmp_path, monkeypatch):
    project = _project(tmp_path)
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    monkeypatch.setenv("GROQ_API_KEY_BACKUP", "synthetic-backup-token")
    packets = []
    _adapter(monkeypatch, 8000, packets)
    donor = MissionService(project, tmp_path / "donor", execution_mode="live",
                           start_supervisor=False)
    try:
        _complete_one(donor, "synthetic-limit-donor")
    finally:
        donor.close()

    host = MissionService(project, tmp_path / "shared", execution_mode="live",
                          start_supervisor=False)
    try:
        backup = host.start(LiveStartRequest(
            request_id="synthetic-backup", question="Interpret the synthetic record.",
            evidence_text="Synthetic record with an unresolved calibration concern.",
            agent_ids=["agent-synthesis"], credential_profile="backup",
        ))
        control = host._control(backup.run_id, "agent-synthesis")
        assert host._effective_input_budget(control).get("observed_limit_tokens") is None
        assert not host.store.records("provider_limit_observation")
        assert len(packets) == 1
    finally:
        host.close()
