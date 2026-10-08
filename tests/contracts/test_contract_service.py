"""Check how the local research board handles commands and interrupted work.

The tests submit repeated, stale, malformed, and delayed actions, then inspect
saved receipts, peer requests, and event replay. Fixture actors are trusted here;
these cases do not prove a live service can authenticate callers.
"""

import copy

import pytest

from ora.contracts.models import Command, ModelSelection, OperationResult, ResearchSnapshot
from ora.contracts.replay import ReplayProjection
from ora.contracts.service import ContractService

HASH = "a" * 64
MODEL_HASH = ModelSelection.calculate_hash("fixture", "scripted-peer", "not_applicable")


@pytest.fixture
def board(tmp_path):
    service = ContractService(tmp_path / "board.sqlite")
    service.seed(
        "mission-a",
        "mission",
        "mission-a",
        "active",
        data={
            "terminalAuthorizationId": "completion-policy-v1",
            "modelConfigurationHash": MODEL_HASH,
        },
    )
    yield service
    service.close()


def command(identifier, payload, version=0, actor="peer-a", kind="peer", task=None):
    return {
        "schemaVersion": "1.0",
        "commandId": identifier,
        "actor": {"id": actor, "kind": kind},
        "missionId": "mission-a",
        "taskId": task,
        "expectedVersion": version,
        "payload": payload,
    }


def propose(identifier="propose-a", task="task-a"):
    return command(
        identifier,
        {
            "kind": "propose_task",
            "taskId": task,
            "title": "Find complementary evidence",
            "rationale": "The abstract leaves methods unresolved.",
            "latency": {
                "category": "long",
                "reason": "Iterative searches across sources",
                "nextAction": "Search the registry and repository",
            },
        },
    )


def transition(
    identifier,
    entity,
    target,
    version,
    *,
    entity_kind="task",
    actor="peer-a",
    kind="peer",
    evidence=None,
    authorization=None,
):
    return command(
        identifier,
        {
            "kind": "transition",
            "entityKind": entity_kind,
            "entityId": entity,
            "targetState": target,
            "reason": "Fixture decision with linked evidence.",
            "evidence": evidence or [],
            "terminalAuthorizationId": authorization,
        },
        version,
        actor,
        kind,
    )


def job():
    return {
        "jobId": "job-a",
        "missionId": "mission-a",
        "taskId": "task-a",
        "attemptId": "attempt-a",
        "operationId": "operation-a",
        "originatingModelCallId": "call-a",
        "toolCallId": "tool-call-a",
        "idempotencyKey": "effect-a",
        "ownerToken": "owner-a",
        "inputArtifacts": [],
        "expectedOutputs": ["source record"],
        "intendedEffects": ["retrieve source"],
        "permittedResources": ["fixture source"],
        "modelSelection": {
            "provider": "fixture",
            "model": "scripted-peer",
            "reasoningEffort": "not_applicable",
            "configurationHash": MODEL_HASH,
        },
        "requestTimeoutSeconds": 30,
        "leaseExpiresAt": "2030-01-01T00:05:00Z",
        "missionAuthorizationId": "mission-authorization-a",
    }


def result(status="completed", owner="owner-a"):
    return {
        "operationId": "operation-a",
        "attemptId": "attempt-a",
        "ownerToken": owner,
        "status": status,
        "resultArtifacts": [],
        "progress": "Fixture completed",
    }


def test_duplicate_stale_and_malformed_are_local(board):
    raw = propose()
    assert board.apply(raw).status == "accepted"
    assert board.apply(raw).status == "duplicate"
    collision = copy.deepcopy(raw)
    collision["payload"]["title"] = "Different work"
    assert board.apply(collision).code == "id_collision"
    assert board.apply(transition("stale", "task-a", "running", 0)).code == "stale_version"
    invalid = propose("invalid", "task-b")
    invalid["payload"]["latency"]["category"] = "deadline_30_seconds"
    assert board.apply(invalid).code == "invalid_contract"
    assert board.apply(propose("valid-sibling", "task-c")).status == "accepted"
    assert len(board.events("mission-a")) == 2


def test_host_cannot_make_scientific_choices(board):
    raw = propose()
    raw["actor"] = {"id": "mechanical-host", "kind": "host"}
    assert board.apply(raw).code == "scientific_authority"
    board.apply(propose("peer-proposal"))
    assert (
        board.apply(transition("claim", "task-a", "running", 1, kind="host")).status == "accepted"
    )
    assert (
        board.apply(transition("host-done", "task-a", "completed", 2, kind="host")).code
        == "scientific_authority"
    )
    assert board.apply(transition("peer-done", "task-a", "completed", 2)).status == "accepted"


def test_peer_delivery_acknowledgment_and_answer_are_distinct(board):
    payload = {
        "kind": "peer_message",
        "messageId": "request-a",
        "recipientId": "peer-b",
        "messageType": "request",
        "body": "Check the outcome definition",
        "requestId": "evidence-request-a",
    }
    assert board.apply(command("ask", payload)).status == "accepted"
    assert (
        board.apply(
            transition("delivery", "request-a", "delivered", 1, entity_kind="request", kind="host")
        ).status
        == "accepted"
    )
    assert (
        board.apply(
            transition("ack", "request-a", "acknowledged", 2, entity_kind="request", actor="peer-b")
        ).status
        == "accepted"
    )
    assert len(board.pending_requests("mission-a")) == 1
    assert (
        board.apply(
            transition(
                "empty-answer", "request-a", "answered", 3, entity_kind="request", actor="peer-b"
            )
        ).code
        == "missing_answer"
    )
    answer = board.seed_artifact(
        "mission-a", "answer-a", b"Synthetic answer with a source locator."
    ).model_dump(by_alias=True)
    assert (
        board.apply(
            transition(
                "wrong-peer", "request-a", "answered", 3, entity_kind="request", evidence=[answer]
            )
        ).code
        == "recipient_authority"
    )
    assert (
        board.apply(
            transition(
                "answer",
                "request-a",
                "answered",
                3,
                entity_kind="request",
                actor="peer-b",
                evidence=[answer],
            )
        ).status
        == "accepted"
    )
    assert board.pending_requests("mission-a") == []


def test_partial_report_failed_attempt_and_circular_wait_do_not_close_mission(board):
    board.seed("mission-a", "report", "report-a", "draft")
    board.seed("mission-a", "attempt", "attempt-a", "running")
    board.apply(transition("partial", "report-a", "partial", 1, entity_kind="report"))
    board.apply(transition("failed", "attempt-a", "failed", 1, entity_kind="attempt", kind="host"))
    for sender, recipient in [("peer-a", "peer-b"), ("peer-b", "peer-a")]:
        board.apply(
            command(
                "ask-" + sender,
                {
                    "kind": "peer_message",
                    "messageId": "request-" + sender,
                    "recipientId": recipient,
                    "messageType": "request",
                    "body": "Need the peer's evidence",
                },
                actor=sender,
            )
        )
    waiting = board.wait_summary("mission-a")
    assert waiting["readyWork"] == 0 and len(waiting["pendingRequestIds"]) == 2
    assert "alternative evidence" in waiting["nextAction"]
    assert board.entity("mission-a", "mission", "mission-a")["state"] == "active"
    assert (
        board.apply(
            transition(
                "premature",
                "mission-a",
                "completed",
                1,
                entity_kind="mission",
                authorization="completion-policy-v1",
            )
        ).code
        == "pending_work"
    )
    assert board.apply(propose("alternative", "task-alternative")).status == "accepted"


def test_branch_reopen_and_invitation_counter_do_not_rewrite_finished_work(board):
    board.seed("mission-a", "branch", "branch-a", "completed")
    assert (
        board.apply(transition("late-evidence", "branch-a", "open", 1, entity_kind="branch")).status
        == "accepted"
    )
    board.seed("mission-a", "invitation", "invite-a", "offered", owner="peer-b")
    assert (
        board.apply(
            transition(
                "counter", "invite-a", "countered", 1, entity_kind="invitation", actor="peer-b"
            )
        ).status
        == "accepted"
    )
    assert (
        board.apply(
            transition(
                "accept", "invite-a", "accepted", 2, entity_kind="invitation", actor="peer-b"
            )
        ).status
        == "accepted"
    )


def test_missed_duplicate_reordered_events_and_persisted_subscription(board):
    board.apply(propose())
    board.apply(transition("running", "task-a", "running", 1))
    board.apply(transition("waiting", "task-a", "waiting", 2))
    events = board.events("mission-a")
    projection = ReplayProjection()
    projection.receive(events[2])
    assert projection.missing_sequence == 1
    projection.receive(events[0])
    projection.receive(events[0])
    projection.receive(events[1])
    assert projection.cursor == 3
    assert projection.entities[("task", "task-a")] == {"state": "waiting", "version": 3}
    board.subscribe("desktop", "mission-a")
    assert len(board.catch_up("desktop")) == 3
    board.acknowledge_cursor("desktop", 2)
    assert [e.sequence for e in board.catch_up("desktop")] == [3]
    board.acknowledge_cursor("desktop", 1)
    assert [e.sequence for e in board.catch_up("desktop")] == [3]


def test_operation_restarts_reattaches_and_accepts_only_one_effect(tmp_path):
    path = tmp_path / "durable.sqlite"
    board = ContractService(path)
    board.seed(
        "mission-a", "mission", "mission-a", "active", data={"modelConfigurationHash": MODEL_HASH}
    )
    board.apply(propose())
    start = command("start", {"kind": "start_operation", "request": job()}, task="task-a")
    assert board.apply(start).status == "deferred"
    board.close()  # Caller/process stopped; the operation identity is durable.
    board = ContractService(path)
    try:
        assert board.apply(start).status == "duplicate"
        assert board.operation("mission-a", "operation-a")["state"] == "running"
        assert (
            board.apply(
                command("reattach", {"kind": "start_operation", "request": job()}, task="task-a")
            ).code
            == "existing_operation"
        )
        stale = command(
            "stale-owner",
            {"kind": "finish_operation", "result": result(owner="old-owner")},
            1,
            task="task-a",
        )
        assert board.apply(stale).code == "stale_owner"
        finish = command(
            "finish", {"kind": "finish_operation", "result": result()}, 1, task="task-a"
        )
        assert board.apply(finish).status == "accepted"
        assert board.apply(finish).status == "duplicate"
        assert len(board.events("mission-a")) == 3
        assert (
            board.apply(
                command("late", {"kind": "finish_operation", "result": result()}, 2, task="task-a")
            ).code
            == "late_result_retained"
        )
        assert board.operation("mission-a", "operation-a")["version"] == 2
    finally:
        board.close()


def test_quota_wait_uncertain_result_and_model_pin(board):
    board.apply(propose())
    wrong = job()
    wrong["modelSelection"]["model"] = "different-model"
    wrong["modelSelection"]["configurationHash"] = ModelSelection.calculate_hash(
        "fixture", "different-model", "not_applicable"
    )
    assert (
        board.apply(
            command("wrong-model", {"kind": "start_operation", "request": wrong}, task="task-a")
        ).code
        == "model_configuration_changed"
    )
    board.apply(command("start", {"kind": "start_operation", "request": job()}, task="task-a"))
    waiting = result("waiting")
    waiting["nextCheck"] = {
        "kind": "quota",
        "referenceId": "quota-a",
        "nextCheckAt": "2030-01-01T01:00:00Z",
        "wakeEvent": "quota available",
        "alternativeAction": "continue another provider",
    }
    assert (
        board.apply(
            command("wait", {"kind": "finish_operation", "result": waiting}, 1, task="task-a")
        ).status
        == "accepted"
    )
    uncertain = result("uncertain")
    uncertain["uncertaintySource"] = "Connection ended before acknowledgment"
    assert (
        board.apply(
            command(
                "uncertain", {"kind": "finish_operation", "result": uncertain}, 2, task="task-a"
            )
        ).status
        == "accepted"
    )
    assert board.apply(propose("sibling", "task-b")).status == "accepted"
    with pytest.raises(ValueError, match="missing evidence"):
        OperationResult.model_validate(result("uncertain"))


def test_incompatible_client_has_actionable_diagnostic(board):
    raw = propose()
    raw["schemaVersion"] = "0.9"
    receipt = board.apply(raw)
    assert receipt.code == "invalid_contract" and "schemaVersion" in receipt.message
    assert "1.0" in receipt.repair
    assert board.apply(propose("current-client", "task-b")).status == "accepted"


def test_unsupported_client_does_not_erase_pending_work_across_restart(tmp_path):
    path = tmp_path / "compatibility-fixture.sqlite"
    board = ContractService(path)
    board.seed("mission-a", "mission", "mission-a", "active")
    assert board.apply(propose()).status == "accepted"
    request = command(
        "ask-peer",
        {
            "kind": "peer_message",
            "messageId": "request-pending",
            "recipientId": "peer-b",
            "messageType": "request",
            "body": "Check the synthetic source version",
        },
        task="task-a",
    )
    assert board.apply(request).status == "accepted"
    older_client = propose("unsupported-client", "task-incompatible")
    older_client["schemaVersion"] = "0.9"  # Deliberately unsupported fixture value; no v0.9 release.
    rejected = board.apply(older_client)
    assert rejected.code == "invalid_contract" and "schemaVersion" in rejected.message
    assert "1.0" in rejected.repair
    assert board.entity("mission-a", "task", "task-incompatible") is None
    assert len(board.events("mission-a")) == 2
    board.close()

    board = ContractService(path)
    try:
        repeated = board.apply(older_client)
        assert repeated.status == "duplicate" and repeated.code == "invalid_contract"
        assert board.entity("mission-a", "mission", "mission-a")["state"] == "active"
        assert board.entity("mission-a", "task", "task-a")["state"] == "queued"
        assert [item["id"] for item in board.pending_requests("mission-a")] == ["request-pending"]
        assert board.apply(propose("current-sibling", "task-b")).status == "accepted"
        assert board.entity("mission-a", "task", "task-b")["state"] == "queued"
        assert len(board.events("mission-a")) == 3
    finally:
        board.close()


def test_current_ui_sample_uses_canonical_read_contract():
    from pathlib import Path

    path = Path("ui/public/sample-run.json")
    if not path.exists():
        pytest.skip("UI sample has not been generated in this development slice")
    ResearchSnapshot.model_validate_json(path.read_text(), strict=True)


def test_valid_command_round_trip():
    validated = Command.model_validate(propose(), strict=True)
    assert Command.model_validate_json(validated.model_dump_json(by_alias=True)) == validated


def test_other_missions_cannot_create_replay_gaps(board):
    board.seed("mission-b", "mission", "mission-b", "active")
    board.apply(propose())
    other = propose("other-mission", "task-other")
    other["missionId"] = "mission-b"
    board.apply(other)
    board.apply(transition("advance-a", "task-a", "running", 1))
    projection = ReplayProjection()
    for event in board.events("mission-a"):
        projection.receive(event)
    assert projection.cursor == 2 and projection.missing_sequence is None
    with pytest.raises(ValueError, match="one mission"):
        projection.receive(board.events("mission-b")[0])


def test_model_name_cannot_hide_behind_an_unchanged_hash(board):
    board.apply(propose())
    changed = job()
    changed["modelSelection"]["model"] = "silently-replaced-model"
    assert (
        board.apply(
            command("spoof", {"kind": "start_operation", "request": changed}, task="task-a")
        ).code
        == "invalid_model_hash"
    )
    assert board.operation("mission-a", "operation-a") is None


def test_operation_identity_uncertainty_and_completion_are_not_erased(board):
    board.apply(propose())
    board.apply(command("start", {"kind": "start_operation", "request": job()}, task="task-a"))
    assert (
        board.apply(
            command(
                "misattributed",
                {"kind": "finish_operation", "result": result()},
                1,
                task="another-task",
            )
        ).code
        == "identity_mismatch"
    )
    board.apply(transition("run", "task-a", "running", 1))
    board.apply(transition("done", "task-a", "completed", 2))
    assert (
        board.apply(
            transition(
                "close",
                "mission-a",
                "completed",
                1,
                entity_kind="mission",
                authorization="completion-policy-v1",
            )
        ).code
        == "pending_operation"
    )
    uncertain = result("uncertain")
    uncertain["uncertaintySource"] = "Receipt missing after transport loss"
    board.apply(
        command("uncertain", {"kind": "finish_operation", "result": uncertain}, 1, task="task-a")
    )
    assert (
        board.apply(
            command(
                "regress",
                {"kind": "finish_operation", "result": result("running")},
                2,
                task="task-a",
            )
        ).code
        == "invalid_transition"
    )
    assert board.operation("mission-a", "operation-a")["state"] == "uncertain"
    assert (
        board.apply(
            command(
                "reconciled", {"kind": "finish_operation", "result": result()}, 2, task="task-a"
            )
        ).status
        == "accepted"
    )


def test_unresolved_artifact_cannot_answer_a_request(board):
    raw = propose()
    raw["artifactVersions"] = [{"id": "invented", "version": 1, "sha256": HASH}]
    assert board.apply(raw).code == "unresolved_artifact"
    assert board.apply(propose("healthy", "task-b")).status == "accepted"


def test_replay_failure_does_not_poison_the_event_id(board):
    board.apply(propose())
    board.apply(transition("run", "task-a", "running", 1))
    first, second = board.events("mission-a")
    bad = second.model_copy(update={"version": 1})
    projection = ReplayProjection()
    projection.receive(first)
    with pytest.raises(ValueError, match="version did not advance"):
        projection.receive(bad)
    assert projection.cursor == 1 and bad.id not in projection.seen
    projection.receive(second)
    assert projection.cursor == 2
