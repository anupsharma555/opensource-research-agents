"""Create sample research records that should either pass or fail validation.

The cases cover commands, events, evidence, and delayed job results so Python and
JavaScript can check the same allowed record shapes. They are synthetic examples,
not observations from a live mission.
"""

import copy
import json
from pathlib import Path

from .models import ModelSelection


def fixture_cases():
    actor = {"id": "peer-synthetic", "kind": "peer"}
    artifact = {"id": "synthetic-artifact", "version": 1, "sha256": "a" * 64}
    command = {
        "schemaVersion": "1.0",
        "commandId": "synthetic-command",
        "actor": actor,
        "missionId": "synthetic-mission",
        "expectedVersion": 0,
        "payload": {
            "kind": "propose_task",
            "taskId": "synthetic-task",
            "title": "Check complementary evidence",
            "rationale": "Resolve an open methods detail.",
            "latency": {
                "category": "long",
                "reason": "Iterative retrieval",
                "nextAction": "Search another route",
            },
        },
    }
    event = {
        "schemaVersion": "1.0",
        "id": "synthetic-event",
        "sequence": 1,
        "actor": actor,
        "missionId": "synthetic-mission",
        "commandId": "synthetic-command",
        "entityKind": "task",
        "entityId": "synthetic-task",
        "version": 1,
        "kind": "propose_task",
        "state": "queued",
        "reason": "Peer proposal accepted",
    }
    operation = {
        "operationId": "synthetic-operation",
        "attemptId": "synthetic-attempt",
        "ownerToken": "synthetic-owner",
        "status": "completed",
        "resultArtifacts": [artifact],
        "progress": "Saved a fixture result",
    }
    evidence = {
        "id": "synthetic-packet",
        "version": 1,
        "kind": "published_evidence",
        "artifact": artifact,
        "locators": ["abstract:results:1"],
        "validationRecord": "fixture-check-v1",
        "accessSurface": "abstract",
        "summary": "The abstract explicitly reports these counts.",
    }
    selection = {
        "provider": "fixture",
        "model": "scripted-peer",
        "reasoningEffort": "not_applicable",
        "configurationHash": ModelSelection.calculate_hash(
            "fixture", "scripted-peer", "not_applicable"
        ),
    }
    job = {
        "jobId": "synthetic-job",
        "missionId": "synthetic-mission",
        "taskId": "synthetic-task",
        "attemptId": "synthetic-attempt",
        "operationId": "synthetic-operation",
        "originatingModelCallId": "synthetic-call",
        "toolCallId": "synthetic-tool-call",
        "idempotencyKey": "synthetic-effect",
        "ownerToken": "synthetic-owner",
        "inputArtifacts": [artifact],
        "expectedOutputs": ["source copy"],
        "intendedEffects": ["read source"],
        "permittedResources": ["synthetic fixture"],
        "modelSelection": selection,
        "requestTimeoutSeconds": 30,
        "leaseExpiresAt": "2030-01-01T01:00:00Z",
        "missionAuthorizationId": "synthetic-authorization",
    }
    cases = []

    def add(name, schema, value, accepted=True):
        cases.append(
            {"name": name, "schema": schema, "accepted": accepted, "value": copy.deepcopy(value)}
        )

    add("peer-proposal", "command", command)
    changed = copy.deepcopy(command)
    changed["schemaVersion"] = "0.9"
    add("incompatible-client", "command", changed, False)
    changed = copy.deepcopy(command)
    changed["expectedVersion"] = -1
    add("negative-version", "command", changed, False)
    changed = copy.deepcopy(command)
    changed["payload"]["kind"] = "plan_everything"
    add("unknown-action", "command", changed, False)
    changed = copy.deepcopy(command)
    changed["unrecognized"] = "must be explicit"
    add("unknown-field", "command", changed, False)
    add("accepted-event", "event", event)
    changed = copy.deepcopy(event)
    changed["sequence"] = 0
    add("invalid-event-order", "event", changed, False)
    add("immediate-result", "operation", operation)
    delayed = copy.deepcopy(operation)
    delayed["status"] = "waiting"
    delayed["nextCheck"] = {
        "kind": "quota",
        "referenceId": "synthetic-quota",
        "wakeEvent": "quota available",
        "alternativeAction": "Continue a different eligible source route",
    }
    add("deferred-result", "operation", delayed)
    changed = copy.deepcopy(delayed)
    del changed["nextCheck"]
    add("wait-without-wake", "operation", changed, False)
    changed = copy.deepcopy(delayed)
    changed["nextCheck"] = None
    add("wait-with-null-wake", "operation", changed, False)
    uncertain = copy.deepcopy(operation)
    uncertain["status"] = "uncertain"
    uncertain["uncertaintySource"] = "Remote acknowledgment was not received"
    add("uncertain-effect", "operation", uncertain)
    changed = copy.deepcopy(uncertain)
    del changed["uncertaintySource"]
    add("uncertain-without-source", "operation", changed, False)
    add("abstract-evidence", "evidence", evidence)
    checked = copy.deepcopy(evidence)
    checked["kind"] = "checked_result"
    checked["accessSurface"] = "experiment_output"
    add("checked-result", "evidence", checked)
    changed = copy.deepcopy(evidence)
    changed["locators"] = []
    add("evidence-without-locator", "evidence", changed, False)
    changed = copy.deepcopy(evidence)
    changed["artifact"]["sha256"] = "not-a-content-hash"
    add("invalid-copy-hash", "evidence", changed, False)
    add("declared-job", "job", job)
    changed = copy.deepcopy(job)
    del changed["originatingModelCallId"]
    add("job-without-origin", "job", changed, False)
    add(
        "cursor-query",
        "query",
        {
            "queryId": "query-a",
            "actor": actor,
            "missionId": "synthetic-mission",
            "kind": "events",
            "afterCursor": 3,
        },
    )
    return cases


def generate_examples(root: Path, check=False):
    path = root / "contracts" / "examples.json"
    expected = json.dumps(fixture_cases(), indent=2, sort_keys=True) + "\n"
    if check:
        return path.exists() and path.read_text() == expected
    path.parent.mkdir(exist_ok=True)
    path.write_text(expected)
    return True
