"""A report inherits source use from the exact model-visible working note."""

from __future__ import annotations

import asyncio
import json

import pytest

from ora.contracts.models import LiveFollowupRequest, LiveStartRequest
from ora.coordination.board import ref
from ora.mission import projection
from ora.mission.report_lineage import report_text_access
from tests.mission.test_service import answer, service, tool

NOTE_TEXT = "A synthetic working-note finding with a distinct source ancestor."


def _run_note_report(tmp_path, monkeypatch, *, direct: bool,
                     revoke_during_model: bool, revoke_after_save: bool = False,
                     source_linked: bool = True):
    calls = []
    host = None
    source = None
    mission_id = None

    def revoke():
        def change(tx):
            previous = tx.get(**source["use"])
            return ref(tx.put("source_use", previous["id"], {
                **previous["body"], "policy_status": "revoked", "allowed_uses": [],
            }, expected_version=previous["version"]))

        return host.store.transact(
            "report-source-revocation", {}, change, mission_id=mission_id,
        )

    def transport(request, *, timeout):
        calls.append(json.loads(request.data))
        assert NOTE_TEXT in request.data.decode()
        if revoke_during_model:
            revoke()
        if direct:
            return answer("Provisional summary: " + NOTE_TEXT, len(calls))
        return tool("publish_result", {
            "title": "Provisional note review", "text": NOTE_TEXT,
            "citations": [], "limitations": ["Synthetic note only."],
        }, len(calls))

    host = service(tmp_path, monkeypatch, transport)
    host.credential_profiles.is_run_enabled = lambda _profile, _run: True
    host.session.adapter.credential_profiles.is_enabled = lambda _profile: True
    run = host.start(LiveStartRequest(
        request_id="source-linked-report", question="Review the synthetic note provisionally.",
        agent_ids=["agent-synthesis"],
    ))
    control = host._control(run.run_id, "agent-synthesis")
    mission_id = control["body"]["mission_id"]
    blob = host.store.put_blob(NOTE_TEXT.encode("utf-8")) if source_linked else None

    def save_source(tx):
        publication = tx.put("publication", "report-source-publication", {
            "title": "Synthetic local note source",
        })
        encounter = tx.put("source_encounter", "report-source-encounter", {
            "publication": ref(publication), "route": "user_supplied",
            "status": "accepted",
        })
        copy = tx.put("source_copy", "report-source-copy", {
            "mission_id": mission_id, "rights_status": "permitted",
            "publication": ref(publication), "encounter": ref(encounter),
            "mime_type": "text/plain", "blob": blob,
            "content_sha256": blob["sha256"], "retain_bytes": True,
            "inspection_state": "checked", "validation_record": "synthetic-local-check",
            "passage_ids": [],
        })
        use = tx.put("source_use", "report-source-use", {
            "copy_ref": ref(copy), "allowed_uses": ["model_input"],
            "policy_status": "granted",
        })
        tx.bind("source_use.copy", "report-source-copy:1", "source_use", use["id"])
        return {"copy": ref(copy), "use": ref(use)}

    if source_linked:
        source = host.store.transact("report-source-setup", {}, save_source,
                                     mission_id=mission_id)
    host.notes.write(
        "report-source-note", mission_id=mission_id,
        actor_id=control["body"]["actor_id"],
        task_ref=control["body"]["proposal_ref"],
        note_id="report-source-note", category="hypothesis", text=NOTE_TEXT,
        input_refs=[source["copy"]] if source else [],
    )
    if revoke_after_save:
        original_save = host._save_report

        def save_then_revoke(*args, **kwargs):
            saved = original_save(*args, **kwargs)
            revoke()
            return saved

        host._save_report = save_then_revoke
    assert host._schedule_model(host._control(run.run_id, "agent-synthesis"))
    asyncio.run(host.executor.run_ready(mission_id))
    assert host._advance(host._control(run.run_id, "agent-synthesis"))
    if not direct:
        assert host._schedule_tool(host._control(run.run_id, "agent-synthesis"))
        asyncio.run(host.executor.run_ready(mission_id))
        assert host._advance(host._control(run.run_id, "agent-synthesis"))
    return host, run, source, revoke, calls


@pytest.mark.parametrize("direct", [False, True])
def test_revoked_during_model_cannot_publish_note_derived_report(
    tmp_path, monkeypatch, direct,
) -> None:
    host, run, source, _, calls = _run_note_report(
        tmp_path, monkeypatch, direct=direct, revoke_during_model=True,
    )
    try:
        assert len(calls) == 1
        assert not host.store.records("report")
        assert not any(
            item["body"].get("completes_task")
            for item in host.store.records("board_outcome")
        )
        responses = host.store.records("model_response_receipt")
        assert len(responses) == 1
        assert responses[0]["body"]["usage"] == {
            "prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11,
        }
        assert len(host.store.records("model_dispatch_attempt")) == 1
        assert host._control(run.run_id, "agent-synthesis")["body"]["phase"] != "done"
        assert host.store.get("source_use", source["use"]["id"])["body"]["policy_status"] == "revoked"
    finally:
        host.close()


@pytest.mark.parametrize("direct", [False, True])
def test_report_save_to_accept_rechecks_current_source_use(
    tmp_path, monkeypatch, direct,
) -> None:
    host, run, source, _, calls = _run_note_report(
        tmp_path, monkeypatch, direct=direct, revoke_during_model=False,
        revoke_after_save=True,
    )
    try:
        assert len(calls) == 1
        report = host.store.records("report")[0]
        assert report_text_access(host.store, report)["status"] != "available"
        assert not any(
            ref(report) in item["body"].get("result_refs", [])
            for item in host.store.records("board_outcome")
        )
        responses = host.store.records("model_response_receipt")
        assert len(responses) == 1
        assert responses[0]["body"]["usage"] == {
            "prompt_tokens": 5, "completion_tokens": 6, "total_tokens": 11,
        }
        assert len(host.store.records("model_dispatch_attempt")) == 1
        assert host._control(run.run_id, "agent-synthesis")["body"]["phase"] != "done"
    finally:
        host.close()


def test_source_free_working_note_can_publish_without_citations(tmp_path, monkeypatch) -> None:
    host, run, _, _, calls = _run_note_report(
        tmp_path, monkeypatch, direct=False, revoke_during_model=False,
        source_linked=False,
    )
    try:
        report = host.store.records("report")[0]
        assert report_text_access(host.store, report)["status"] == "available"
        assert report_text_access(host.store, report)["source_dependency_refs"] == []
        assert report["body"]["citations"] == []
        assert host.state(run.run_id).snapshot.artifacts[0].body == NOTE_TEXT
        assert len(calls) == 1
    finally:
        host.close()


def test_report_lineage_rejects_forged_producer_and_keeps_exact_older_copy(
    tmp_path, monkeypatch,
) -> None:
    host, run, source, _, _ = _run_note_report(
        tmp_path, monkeypatch, direct=False, revoke_during_model=False,
    )
    try:
        report = host.store.records("report")[0]
        forged = {
            **report,
            "body": {
                **report["body"],
                "producer_attempt_ref": {
                    "kind": "agent_attempt", "id": "another-task-attempt", "version": 1,
                },
            },
        }
        assert report_text_access(host.store, forged)["status"] != "available"
        assert not report_text_access(host.store, forged)["dependency_lineage_complete"]

        unchecked = host.store.transact(
            "report-unchecked-passage", {},
            lambda tx: ref(tx.put("source_passage", "report-unchecked-passage", {
                "copy_ref": source["copy"], "span_id": "unchecked",
                "inspection_state": "provider_asserted", "text": NOTE_TEXT,
            })),
            mission_id=host._run(run.run_id)["body"]["mission_id"],
        )
        forged_citation = {
            **report,
            "body": {
                **report["body"],
                "citations": [{"sourceId": source["copy"]["id"], "spanId": "unchecked"}],
                "citation_input_provenance": [{
                    "source_id": source["copy"]["id"], "span_id": "unchecked",
                    "copy_ref": source["copy"], "passage_ref": unchecked,
                }],
            },
        }
        assert report_text_access(host.store, forged_citation)["status"] != "available"

        def newer(tx):
            original = tx.get(**source["copy"])
            copy = tx.put("source_copy", original["id"], {
                **original["body"], "source_version": "synthetic-v2",
            }, expected_version=original["version"])
            use = tx.put("source_use", "report-source-use-v2", {
                "copy_ref": ref(copy), "allowed_uses": ["model_input"],
                "policy_status": "granted",
            })
            tx.bind("source_use.copy", f"{copy['id']}:2", "source_use", use["id"])
            return {"copy": ref(copy), "use": ref(use)}

        newer_copy = host.store.transact(
            "report-source-newer-copy", {}, newer,
            mission_id=host._run(run.run_id)["body"]["mission_id"],
        )["copy"]
        access = report_text_access(host.store, report)
        assert access["status"] == "available"
        assert access["source_dependency_refs"] == [source["copy"]]
        assert newer_copy not in access["source_dependency_refs"]
    finally:
        host.close()


def test_snapshot_retries_policy_change_between_fingerprint_and_commit(
    tmp_path, monkeypatch,
) -> None:
    host, run, source, revoke, _ = _run_note_report(
        tmp_path, monkeypatch, direct=False, revoke_during_model=False,
    )
    try:
        before = host.state(run.run_id).snapshot.snapshot_id
        revoke()
        count = len(host.store.records("research_snapshot"))
        original_build = projection.build_snapshot

        def grant_during_build(*args, **kwargs):
            def grant(tx):
                previous = tx.get("source_use", source["use"]["id"])
                return ref(tx.put("source_use", previous["id"], {
                    **previous["body"], "policy_status": "granted",
                    "allowed_uses": ["model_input"],
                }, expected_version=previous["version"]))

            host.store.transact("report-source-race-grant", {}, grant,
                                mission_id=host._run(run.run_id)["body"]["mission_id"])
            return original_build(*args, **kwargs)

        monkeypatch.setattr(projection, "build_snapshot", grant_during_build)
        current = host.state(run.run_id)
        assert current.snapshot.snapshot_id != before
        assert current.snapshot.artifacts[0].body == NOTE_TEXT
        assert len(host.store.records("research_snapshot")) == count + 1
        assert host.store.get("mission_snapshot_head", run.run_id)["body"]["snapshot_ref"]["id"] == current.snapshot.snapshot_id
    finally:
        host.close()


def test_provisional_direct_preview_is_not_sent_outside_context_source_checks(
    tmp_path, monkeypatch,
) -> None:
    host, run, _, revoke, _ = _run_note_report(
        tmp_path, monkeypatch, direct=True, revoke_during_model=False,
    )
    try:
        report = host.store.records("report")[0]
        control = host._control(run.run_id, "agent-synthesis")
        saved_feedback = {
            "status": "direct_answer_provisional_pending_peer_action",
            "candidate_ref": ref(report), "candidate_preview": NOTE_TEXT,
            "candidate_text_truncated": False,
            "next_action": "Read the exact saved report if needed.",
        }
        host._update_control(control, feedback=saved_feedback)
        control = host._control(run.run_id, "agent-synthesis")
        assert NOTE_TEXT not in json.dumps(host._model_base_history(control, []))
        assert host._control(run.run_id, "agent-synthesis")["body"]["feedback"] == saved_feedback
        revoke()
        history = json.dumps(host._model_base_history(control, []))
        assert NOTE_TEXT not in history
        assert "data_unavailable_due_source_use" in history
    finally:
        host.close()


@pytest.mark.parametrize("refresh_fails", [False, True])
def test_accepted_report_is_withheld_after_revoke_in_current_and_comparison_views(
    tmp_path, monkeypatch, refresh_fails,
) -> None:
    host, run, source, revoke, calls = _run_note_report(
        tmp_path, monkeypatch, direct=False, revoke_during_model=False,
    )
    try:
        report = host.store.records("report")[0]
        assert report_text_access(host.store, report)["source_dependency_refs"] == [source["copy"]]
        before = host.state(run.run_id)
        assert NOTE_TEXT in before.snapshot.artifacts[0].body
        before_id = before.snapshot.snapshot_id
        snapshot_count = len(host.store.records("research_snapshot"))
        assert host.state(run.run_id).snapshot.snapshot_id == before_id
        assert len(host.store.records("research_snapshot")) == snapshot_count

        revoke()
        original_refresh = host._refresh_snapshot
        if refresh_fails:
            from ora.state import StateError

            def failed_refresh(_run_id):
                raise StateError("Injected unavailable derived view")

            monkeypatch.setattr(host, "_refresh_snapshot", failed_refresh)
        after = host.state(run.run_id)
        assert (after.snapshot.snapshot_id == before_id) is refresh_fails
        assert NOTE_TEXT not in json.dumps(after.snapshot.model_dump(mode="json"))
        assert after.snapshot.artifacts[0].body.startswith("Report text unavailable")
        assert report_text_access(host.store, report)["status"] != "available"
        assert host.store.get("research_snapshot", before_id) is not None
        if refresh_fails:
            assert len(host.store.records("mission_host_diagnostic")) == 1
            assert host.state(run.run_id).snapshot.snapshot_id == before_id
            assert len(host.store.records("mission_host_diagnostic")) == 1
            monkeypatch.setattr(host, "_refresh_snapshot", original_refresh)

        follow = host.followup(LiveFollowupRequest(
            request_id="report-source-followup", parent_run_id=run.run_id,
            parent_snapshot_id=before_id, target_id=report["id"],
            target_version=report["version"], question="Review the prior report.",
            agent_ids=["agent-synthesis"],
        ))
        child = host.state(follow.run_id)
        assert NOTE_TEXT not in json.dumps(child.comparison_snapshot.model_dump(mode="json"))
        assert child.comparison_snapshot.artifacts[0].body.startswith("Report text unavailable")
        assert len(calls) == 1
    finally:
        host.close()
