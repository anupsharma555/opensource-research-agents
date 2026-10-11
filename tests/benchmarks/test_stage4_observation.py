"""Keep qualification evidence exact and ongoing test consumption explicit."""

import json

import pytest

from scripts import qualify_stage4_mission as qualification
from scripts.qualify_stage4_mission import _has_accepted_report


def test_first_output_accepts_scientific_artifact_types_and_multiple_contributions():
    snapshot = {
        "mission": {"reportArtifactId": None},
        "artifacts": [{"id": "report-a", "version": 2, "kind": "research_contribution"}],
        "inspection": {"acceptedOutputReferences": [
            {"kind": "report", "id": "report-a", "version": 2},
            {"kind": "report", "id": "report-b", "version": 1},
        ]},
    }
    assert _has_accepted_report(snapshot)
    snapshot["artifacts"][0]["version"] = 3
    assert not _has_accepted_report(snapshot)


def test_unaccepted_or_wrong_kind_output_is_not_counted_as_first_report():
    snapshot = {
        "artifacts": [{"id": "report-a", "version": 1, "kind": "report"}],
        "inspection": {"acceptedOutputReferences": [
            {"kind": "board_outcome", "id": "report-a", "version": 1},
        ]},
    }
    assert not _has_accepted_report(snapshot)
    assert not _has_accepted_report({"artifacts": snapshot["artifacts"]})


@pytest.mark.parametrize("initial_active,overlap,observed_status,new_other_work,expected_starts", [
    (True, False, "waiting", False, 0),
    (False, False, "waiting", False, 1),
    (False, True, "waiting", False, 2),
    ("continuing", False, "waiting", False, 0),
    (False, False, "completed", True, 1),
])
def test_qualification_does_not_silently_accumulate_unfinished_tests(
    tmp_path, monkeypatch, initial_active, overlap, observed_status, new_other_work, expected_starts,
):
    suite = tmp_path / "benchmarks/stage4"
    suite.mkdir(parents=True)
    (suite / "cases.json").write_text(json.dumps({"version": "test", "cases": [{
        "id": "lookup", "difficulty": "simple", "roles": ["reader"],
        "question": "Read the supplied example", "scope": "test fixture",
    }]}))
    monkeypatch.setattr(qualification, "ROOT", tmp_path)
    monkeypatch.setattr(qualification, "connect_url", lambda _: None)
    monkeypatch.setattr(qualification, "_connection", lambda _: {"hostBuildId": None})
    monkeypatch.setattr(qualification.subprocess, "check_output", lambda *_a, **_kw: "test-head")
    state = {"agents": [{"id": "reader-1", "roleTemplateId": "reader", "availability": "available"}],
             "model": "unchanged-test-model", "runs": (
                 [{"id": "existing-work", "status": "waiting"}] if initial_active else [])}
    if initial_active == "continuing":
        state["runs"][0].update(status="completed", continuingTaskIds=["optional-peer-work"])
    starts = []

    def request(_connection, route, body=None):
        if route == "/api/live/state":
            return state
        assert route == "/api/live/start"  # No cancellation, restart or hidden model route.
        starts.append(body)
        return {"runId": f"test-{len(starts)}"}

    def observe(_connection, run_id, *_args):
        state["runs"].append({"id": run_id, "status": observed_status})
        if new_other_work:
            state["runs"].append({"id": "other-work", "status": "active"})
        return {"status": observed_status, "state": state}

    monkeypatch.setattr(qualification, "_request", request)
    monkeypatch.setattr(qualification, "_observe", observe)
    arguments = ["qualify", "--case", "lookup", "--repeat", "2", "--allow-live"]
    if overlap:
        arguments.append("--allow-overlap")
    monkeypatch.setattr(qualification.sys, "argv", arguments)
    assert qualification.main() == (0 if overlap else 2)
    assert len(starts) == expected_starts
    receipt = next((tmp_path / ".local/stage4/mission-qualifications").glob("*/qualification-status.json"))
    status = json.loads(receipt.read_text())
    assert status["ongoing_consumption_possible"] is True
    assert status["missions_cancelled_by_harness"] is False
    assert status["additional_test_submissions_deferred"] is not overlap


def test_selected_credential_profile_follows_start_and_exact_followup(tmp_path, monkeypatch):
    suite = tmp_path / "benchmarks/stage4"
    suite.mkdir(parents=True)
    (suite / "cases.json").write_text(json.dumps({"version": "test", "cases": [{
        "id": "lookup", "difficulty": "simple", "roles": ["reader"],
        "question": "Read the example", "scope": "offline", "followup": "Review it",
    }]}))
    monkeypatch.setattr(qualification, "ROOT", tmp_path)
    monkeypatch.setattr(qualification, "connect_url", lambda _: None)
    monkeypatch.setattr(qualification, "_connection", lambda _: {"hostBuildId": None})
    monkeypatch.setattr(qualification.subprocess, "check_output", lambda *_a, **_kw: "test-head")
    state = {"agents": [{"id": "reader-1", "roleTemplateId": "reader", "availability": "available"}],
             "model": "unchanged-test-model", "runs": []}
    submitted = []

    def request(_connection, route, body=None):
        if route == "/api/live/state":
            return state
        assert route in {"/api/live/start", "/api/live/followups"}
        submitted.append(body)
        return {"runId": f"run-{len(submitted)}"}

    snapshot = {"snapshotId": "saved-parent", "mission": {"reportArtifactId": "report-1"},
                "artifacts": [{"id": "report-1", "version": 2}]}
    monkeypatch.setattr(qualification, "_request", request)
    monkeypatch.setattr(qualification, "_observe", lambda *_a: {
        "status": "completed", "state": {**state, "snapshot": snapshot},
    })
    monkeypatch.setattr(qualification.sys, "argv", [
        "qualify", "--case", "lookup", "--allow-live", "--credential-profile", "backup",
    ])
    assert qualification.main() == 0
    assert len(submitted) == 2
    assert all(command["credentialProfile"] == "backup" for command in submitted)
    assert submitted[1]["targetVersion"] == 2
    manifest = next((tmp_path / ".local/stage4/mission-qualifications").glob("*/manifest.json"))
    assert json.loads(manifest.read_text())["credential_profile"] == "backup"
