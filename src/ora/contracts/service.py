"""Show how research commands can change saved state without losing their receipts.

This local SQLite example checks record versions, duplicate command IDs, artifact
references, and allowed state changes, then saves accepted events with receipts in
one transaction. It trusts fixture actors and is not a live authenticated host.
"""

import hashlib
import json
import sqlite3
from pathlib import Path

from pydantic import ValidationError

from .models import ArtifactRef, Command, Event, EvidencePacket, Receipt

TRANSITIONS = {
    "operation": {
        "running": {"running", "waiting", "completed", "failed", "uncertain", "cancelled"},
        "waiting": {"waiting", "running", "completed", "failed", "uncertain", "cancelled"},
        "uncertain": {"completed", "failed", "cancelled"},
        "completed": set(),
        "failed": set(),
        "cancelled": set(),
    },
    "mission": {
        "active": {"waiting", "completed", "cancelled"},
        "waiting": {"active", "completed", "cancelled"},
        "completed": set(),
        "cancelled": set(),
    },
    "branch": {
        "open": {"waiting", "completed", "cancelled"},
        "waiting": {"open", "completed", "cancelled"},
        "completed": {"open"},
        "cancelled": set(),
    },
    "task": {
        "queued": {"running", "waiting", "cancelled"},
        "running": {"waiting", "completed", "failed", "cancelled"},
        "waiting": {"queued", "running", "cancelled"},
        "failed": {"queued", "cancelled"},
        "completed": set(),
        "cancelled": set(),
    },
    "attempt": {
        "running": {"waiting", "completed", "failed", "uncertain", "cancelled"},
        "waiting": {"running", "completed", "failed", "uncertain", "cancelled"},
        "uncertain": {"completed", "failed", "cancelled"},
        "completed": set(),
        "failed": set(),
        "cancelled": set(),
    },
    "request": {
        "requested": {"delivered", "withdrawn"},
        "delivered": {"acknowledged", "answered", "withdrawn"},
        "acknowledged": {"answered", "withdrawn"},
        "answered": set(),
        "withdrawn": set(),
    },
    "invitation": {
        "offered": {"accepted", "declined", "countered", "withdrawn"},
        "countered": {"accepted", "declined", "withdrawn"},
        "accepted": set(),
        "declined": set(),
        "withdrawn": set(),
    },
    "report": {
        "draft": {"partial", "published", "withdrawn"},
        "partial": {"published", "superseded", "withdrawn"},
        "published": {"superseded", "withdrawn"},
        "superseded": set(),
        "withdrawn": set(),
    },
}


def canonical(value) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"))


class ContractService:
    """Single local board for executable examples, with independent entity versions."""

    def __init__(self, path: Path):
        self.db = sqlite3.connect(path, isolation_level=None, timeout=5)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS entities (
            mission TEXT, kind TEXT, id TEXT, state TEXT, version INTEGER,
            owner TEXT, data TEXT, PRIMARY KEY(mission, kind, id));
          CREATE TABLE IF NOT EXISTS receipts (
            id TEXT PRIMARY KEY, input_hash TEXT, body TEXT);
          CREATE TABLE IF NOT EXISTS events (
            mission TEXT, sequence INTEGER, body TEXT, PRIMARY KEY(mission,sequence));
          CREATE TABLE IF NOT EXISTS operations (
            mission TEXT, id TEXT, dedupe TEXT, attempt TEXT, owner TEXT,
            state TEXT, version INTEGER, request TEXT, result TEXT,
            PRIMARY KEY(mission,id), UNIQUE(mission,dedupe));
          CREATE TABLE IF NOT EXISTS subscriptions (
            id TEXT PRIMARY KEY, mission TEXT, cursor INTEGER);
          CREATE TABLE IF NOT EXISTS late_results (
            command_id TEXT PRIMARY KEY, body TEXT);
          CREATE TABLE IF NOT EXISTS fixture_artifacts (
            mission TEXT, id TEXT, version INTEGER, sha256 TEXT, bytes BLOB,
            kind TEXT, validation_record TEXT, PRIMARY KEY(mission,id,version));
          CREATE TABLE IF NOT EXISTS rejected_collisions (
            command_id TEXT, input_hash TEXT, PRIMARY KEY(command_id,input_hash));
        """)

    def close(self):
        self.db.close()

    def seed(
        self,
        mission: str,
        kind: str,
        identifier: str,
        state: str,
        owner: str = "peer-a",
        data: dict | None = None,
    ):
        """Fixture setup only; production creation must be an authenticated command."""
        if kind not in TRANSITIONS or state not in TRANSITIONS[kind]:
            raise ValueError("unknown entity state")
        self.db.execute(
            "INSERT INTO entities VALUES (?,?,?,?,?,?,?)",
            (mission, kind, identifier, state, 1, owner, canonical(data or {})),
        )

    def entity(self, mission: str, kind: str, identifier: str):
        row = self.db.execute(
            "SELECT * FROM entities WHERE mission=? AND kind=? AND id=?",
            (mission, kind, identifier),
        ).fetchone()
        return dict(row) if row else None

    def operation(self, mission: str, identifier: str):
        row = self.db.execute(
            "SELECT * FROM operations WHERE mission=? AND id=?", (mission, identifier)
        ).fetchone()
        return dict(row) if row else None

    def seed_artifact(
        self,
        mission: str,
        identifier: str,
        content: bytes,
        kind="published_evidence",
        validation_record="fixture-byte-check-v1",
    ):
        """Small immutable fixture BLOB; production file/store boundary is separate."""
        digest = hashlib.sha256(content).hexdigest()
        self.db.execute(
            "INSERT INTO fixture_artifacts VALUES (?,?,?,?,?,?,?)",
            (mission, identifier, 1, digest, content, kind, validation_record),
        )
        return ArtifactRef(id=identifier, version=1, sha256=digest)

    def _resolved(self, mission: str, reference: ArtifactRef):
        row = self.db.execute(
            "SELECT * FROM fixture_artifacts WHERE mission=? AND id=? AND version=?",
            (mission, reference.id, reference.version),
        ).fetchone()
        if (
            not row
            or row["sha256"] != reference.sha256
            or hashlib.sha256(row["bytes"]).hexdigest() != reference.sha256
        ):
            return None
        return row

    def validate_evidence(self, mission: str, packet: EvidencePacket):
        row = self._resolved(mission, packet.artifact)
        if (
            not row
            or row["validation_record"] != packet.validation_record
            or row["kind"] != packet.kind
        ):
            raise ValueError("unresolved evidence copy, kind, or validation record")
        return packet  # Byte lineage is established; semantic support remains a peer judgment.

    def _save_receipt(
        self,
        identifier: str,
        digest: str,
        status: str,
        code: str,
        message: str,
        version: int,
        events=(),
        repair=None,
    ):
        receipt = Receipt(
            command_id=identifier,
            status=status,
            code=code,
            message=message,
            version=version,
            event_ids=list(events),
            repair=repair,
        )
        self.db.execute(
            "INSERT INTO receipts VALUES (?,?,?)",
            (identifier, digest, receipt.model_dump_json(by_alias=True)),
        )
        return receipt

    def apply(self, raw: dict) -> Receipt:
        digest = hashlib.sha256(canonical(raw).encode()).hexdigest()
        identifier = raw.get("commandId", raw.get("command_id", f"invalid-{digest[:24]}"))
        if not isinstance(identifier, str) or not identifier:
            identifier = f"invalid-{digest[:24]}"
        self.db.execute("BEGIN IMMEDIATE")
        try:
            existing = self.db.execute(
                "SELECT * FROM receipts WHERE id=?", (identifier,)
            ).fetchone()
            if existing:
                if existing["input_hash"] != digest:
                    self.db.execute(
                        "INSERT OR IGNORE INTO rejected_collisions VALUES (?,?)",
                        (identifier, digest),
                    )
                    receipt = Receipt(
                        command_id=identifier,
                        status="rejected",
                        code="id_collision",
                        message="This command ID already names different content.",
                        version=0,
                        repair="Use a new command ID for a changed action.",
                    )
                else:
                    receipt = Receipt.model_validate_json(existing["body"])
                    receipt.status = "duplicate"
                self.db.execute("COMMIT")
                return receipt
            try:
                command = Command.model_validate(raw, strict=True)
            except ValidationError as error:
                locations = [
                    ".".join(map(str, e["loc"])) for e in error.errors(include_input=False)
                ]
                receipt = self._save_receipt(
                    identifier,
                    digest,
                    "rejected",
                    "invalid_contract",
                    "Invalid fields: " + ", ".join(locations),
                    0,
                    repair="Repair the named fields using contract 1.0.",
                )
            else:
                receipt = self._execute(command, digest)
            self.db.execute("COMMIT")
            return receipt
        except BaseException:
            self.db.execute("ROLLBACK")
            raise

    def _execute(self, command: Command, digest: str) -> Receipt:
        p = command.payload

        def reject(code, message, version=0):
            return self._save_receipt(
                command.command_id,
                digest,
                "rejected",
                code,
                message,
                version,
                repair="Refresh this entity, revise the action, and retry with a new ID.",
            )

        mission = self.entity(command.mission_id, "mission", command.mission_id)
        if not mission:
            return reject("unknown_mission", "No mission exists for this command.")
        if mission["state"] in {"completed", "cancelled"} and p.kind != "finish_operation":
            return reject(
                "terminal_mission",
                "Create explicitly authorized follow-up work for a terminal mission.",
            )

        references = list(command.artifact_versions)
        if p.kind in {"transition", "peer_message"}:
            references.extend(p.evidence)
        elif p.kind == "start_operation":
            references.extend(p.request.input_artifacts)
        elif p.kind == "finish_operation":
            references.extend(p.result.result_artifacts)
        if any(self._resolved(command.mission_id, ref) is None for ref in references):
            return reject(
                "unresolved_artifact",
                "An artifact ID/version/hash does not resolve to a retained, checked fixture copy.",
            )

        if p.kind in {"start_operation", "finish_operation"}:
            return self._operation_command(command, digest, reject, mission)

        if p.kind == "propose_task":
            if command.actor.kind not in {"peer", "user"}:
                return reject("scientific_authority", "Peers or the user propose scientific work.")
            if self.entity(command.mission_id, "task", p.task_id) or command.expected_version != 0:
                return reject(
                    "stale_version", "A new task requires a new ID and expectedVersion 0."
                )
            if p.task_id in p.dependency_ids:
                return reject(
                    "self_dependency", "A task cannot depend on itself; revise this branch."
                )
            for dependency in p.dependency_ids:
                if not self.entity(command.mission_id, "task", dependency):
                    return reject("unknown_dependency", f"Unknown dependency: {dependency}")
            kind, entity_id, state, version = "task", p.task_id, "queued", 1
            self.db.execute(
                "INSERT INTO entities VALUES (?,?,?,?,?,?,?)",
                (
                    command.mission_id,
                    kind,
                    entity_id,
                    state,
                    version,
                    command.actor.id,
                    p.model_dump_json(by_alias=True),
                ),
            )
            reason, evidence = p.rationale, []
        elif p.kind == "peer_message":
            if command.actor.kind not in {"peer", "user"}:
                return reject(
                    "scientific_authority", "A host cannot originate scientific peer advice."
                )
            if command.expected_version != 0:
                return reject("stale_version", "A new message requires expectedVersion 0.")
            kind = {"request": "request", "invitation": "invitation"}.get(p.message_type, "message")
            entity_id = p.message_id
            if self.entity(command.mission_id, kind, entity_id):
                return reject(
                    "existing_message", "Message already exists; replay its original command."
                )
            state = {"request": "requested", "invitation": "offered"}.get(p.message_type, "sent")
            version = 1
            self.db.execute(
                "INSERT INTO entities VALUES (?,?,?,?,?,?,?)",
                (
                    command.mission_id,
                    kind,
                    entity_id,
                    state,
                    version,
                    p.recipient_id,
                    p.model_dump_json(by_alias=True),
                ),
            )
            reason, evidence = p.body, p.evidence
        else:
            kind, entity_id, state = p.entity_kind, p.entity_id, p.target_state
            entity = self.entity(command.mission_id, kind, entity_id)
            if not entity:
                return reject("unknown_entity", "The requested entity does not exist.")
            version = entity["version"]
            if version != command.expected_version:
                return reject(
                    "stale_version", "Another accepted change advanced this entity.", version
                )
            if state not in TRANSITIONS[kind][entity["state"]]:
                return reject(
                    "invalid_transition",
                    f"Cannot move {kind} from {entity['state']} to {state}.",
                    version,
                )
            mechanical = (
                kind == "attempt"
                or (kind == "task" and state in {"running", "waiting", "failed", "queued"})
                or (kind == "request" and state == "delivered")
            )
            if command.actor.kind == "host" and not mechanical:
                return reject(
                    "scientific_authority",
                    "The host cannot make this scientific disposition.",
                    version,
                )
            if kind in {"invitation", "request"} and state in {
                "accepted",
                "declined",
                "countered",
                "acknowledged",
                "answered",
            }:
                if command.actor.id != entity["owner"]:
                    return reject(
                        "recipient_authority",
                        "Only the intended peer may answer this request.",
                        version,
                    )
            if kind == "request" and state == "answered" and not p.evidence:
                return reject(
                    "missing_answer",
                    "Link an answer artifact; acknowledgment is not an answer.",
                    version,
                )
            if kind == "mission" and state == "completed":
                authorized = json.loads(mission["data"]).get("terminalAuthorizationId")
                if not authorized or p.terminal_authorization_id != authorized:
                    return reject(
                        "missing_terminal_authorization",
                        "Reference the mission's existing completion policy.",
                        version,
                    )
                pending = self.db.execute(
                    "SELECT 1 FROM entities WHERE mission=? AND kind IN ('task','request') AND state NOT IN ('completed','cancelled','answered','withdrawn') LIMIT 1",
                    (command.mission_id,),
                ).fetchone()
                if pending:
                    return reject(
                        "pending_work",
                        "Resolve or explicitly disposition pending work before completion.",
                        version,
                    )
                unfinished = self.db.execute(
                    "SELECT 1 FROM operations WHERE mission=? AND state NOT IN ('completed','cancelled','failed') LIMIT 1",
                    (command.mission_id,),
                ).fetchone()
                if unfinished:
                    return reject(
                        "pending_operation",
                        "Reconcile unfinished or uncertain operations before positive completion.",
                        version,
                    )
            version += 1
            self.db.execute(
                "UPDATE entities SET state=?,version=? WHERE mission=? AND kind=? AND id=?",
                (state, version, command.mission_id, kind, entity_id),
            )
            reason, evidence = p.reason, p.evidence
        event = self._event(command, kind, entity_id, state, version, reason, evidence)
        return self._save_receipt(
            command.command_id,
            digest,
            "accepted",
            "committed",
            "The change and its replay event were committed together.",
            version,
            [event.id],
        )

    def _operation_command(self, command, digest, reject, mission):
        p = command.payload
        if p.kind == "start_operation":
            request = p.request
            if request.mission_id != command.mission_id or request.task_id != command.task_id:
                return reject("identity_mismatch", "Job mission/task IDs must match its command.")
            existing = self.db.execute(
                "SELECT * FROM operations WHERE mission=? AND (id=? OR dedupe=?)",
                (command.mission_id, request.operation_id, request.idempotency_key),
            ).fetchone()
            if existing:
                if existing["request"] != request.model_dump_json(by_alias=True):
                    return reject(
                        "operation_collision",
                        "This operation identity already names a different request.",
                    )
                return self._save_receipt(
                    command.command_id,
                    digest,
                    "duplicate",
                    "existing_operation",
                    "Reattach to the existing durable operation.",
                    existing["version"],
                )
            if command.expected_version != 0:
                return reject("stale_version", "A new operation requires expectedVersion 0.")
            pinned = json.loads(mission["data"]).get("modelConfigurationHash")
            selection = request.model_selection
            calculated = selection.calculate_hash(
                selection.provider, selection.model, selection.reasoning_effort, selection.settings
            )
            if calculated != selection.configuration_hash:
                return reject(
                    "invalid_model_hash",
                    "Model configuration hash does not match the actual provider/model/reasoning/settings.",
                )
            if pinned and pinned != request.model_selection.configuration_hash:
                return reject(
                    "model_configuration_changed",
                    "Use the mission's saved model configuration; no silent substitution.",
                )
            if not self.entity(command.mission_id, "task", request.task_id):
                return reject("unknown_task", "Attach the operation to an existing task.")
            self.db.execute(
                "INSERT INTO operations VALUES (?,?,?,?,?,?,?,?,NULL)",
                (
                    command.mission_id,
                    request.operation_id,
                    request.idempotency_key,
                    request.attempt_id,
                    request.owner_token,
                    "running",
                    1,
                    request.model_dump_json(by_alias=True),
                ),
            )
            event = self._event(
                command,
                "operation",
                request.operation_id,
                "running",
                1,
                "Operation accepted; caller may detach.",
                [],
            )
            return self._save_receipt(
                command.command_id,
                digest,
                "deferred",
                "operation_started",
                "Use the stable operation ID to observe progress or reattach.",
                1,
                [event.id],
            )
        result = p.result
        operation = self.operation(command.mission_id, result.operation_id)
        if not operation:
            return reject("unknown_operation", "No accepted operation matches this result.")
        if command.task_id != json.loads(operation["request"])["taskId"]:
            return reject(
                "identity_mismatch",
                "A result must retain its originating task ID.",
                operation["version"],
            )
        if result.owner_token != operation["owner"] or result.attempt_id != operation["attempt"]:
            return reject(
                "stale_owner",
                "An old attempt cannot write its replacement's operation.",
                operation["version"],
            )
        if operation["state"] in {"completed", "cancelled", "failed"} or mission["state"] in {
            "completed",
            "cancelled",
        }:
            self.db.execute(
                "INSERT INTO late_results VALUES (?,?)",
                (command.command_id, result.model_dump_json(by_alias=True)),
            )
            return self._save_receipt(
                command.command_id,
                digest,
                "accepted",
                "late_result_retained",
                "Retained for reconciliation; terminal state was not reopened.",
                operation["version"],
            )
        if command.expected_version != operation["version"]:
            return reject(
                "stale_version",
                "Reattach and use the current operation version.",
                operation["version"],
            )
        if result.status not in TRANSITIONS["operation"][operation["state"]]:
            return reject(
                "invalid_transition",
                "Reconcile an uncertain outcome before starting new execution.",
                operation["version"],
            )
        version = operation["version"] + 1
        self.db.execute(
            "UPDATE operations SET state=?,version=?,result=? WHERE mission=? AND id=?",
            (
                result.status,
                version,
                result.model_dump_json(by_alias=True),
                command.mission_id,
                result.operation_id,
            ),
        )
        event = self._event(
            command,
            "operation",
            result.operation_id,
            result.status,
            version,
            result.progress,
            result.result_artifacts,
        )
        return self._save_receipt(
            command.command_id,
            digest,
            "accepted",
            "operation_updated",
            "Operation outcome committed; scientific interpretation remains separate.",
            version,
            [event.id],
        )

    def _event(self, command, kind, identifier, state, version, reason, evidence):
        sequence = self.db.execute(
            "SELECT coalesce(max(sequence),0)+1 FROM events WHERE mission=?", (command.mission_id,)
        ).fetchone()[0]
        event = Event(
            id=f"{command.mission_id}:event-{sequence}",
            sequence=sequence,
            actor=command.actor,
            mission_id=command.mission_id,
            task_id=command.task_id,
            command_id=command.command_id,
            entity_kind=kind,
            entity_id=identifier,
            version=version,
            kind=command.payload.kind,
            state=state,
            reason=reason,
            evidence=evidence,
        )
        self.db.execute(
            "INSERT INTO events VALUES (?,?,?)",
            (command.mission_id, sequence, event.model_dump_json(by_alias=True)),
        )
        return event

    def events(self, mission: str, after_cursor: int = 0):
        rows = self.db.execute(
            "SELECT body FROM events WHERE mission=? AND sequence>? ORDER BY sequence",
            (mission, after_cursor),
        )
        return [Event.model_validate_json(row["body"]) for row in rows]

    def subscribe(self, identifier: str, mission: str, cursor: int = 0):
        self.db.execute(
            "INSERT INTO subscriptions VALUES (?,?,?) ON CONFLICT(id) DO NOTHING",
            (identifier, mission, cursor),
        )

    def catch_up(self, identifier: str):
        row = self.db.execute("SELECT * FROM subscriptions WHERE id=?", (identifier,)).fetchone()
        if not row:
            raise KeyError("unknown subscription")
        return self.events(row["mission"], row["cursor"])

    def acknowledge_cursor(self, identifier: str, cursor: int):
        subscription = self.db.execute(
            "SELECT mission FROM subscriptions WHERE id=?", (identifier,)
        ).fetchone()
        if not subscription:
            raise KeyError("unknown subscription")
        maximum = self.db.execute(
            "SELECT coalesce(max(sequence),0) FROM events WHERE mission=?",
            (subscription["mission"],),
        ).fetchone()[0]
        if cursor < 0 or cursor > maximum:
            raise ValueError("cursor must refer to persisted events")
        self.db.execute(
            "UPDATE subscriptions SET cursor=max(cursor,?) WHERE id=?", (cursor, identifier)
        )

    def pending_requests(self, mission: str):
        return [
            dict(row)
            for row in self.db.execute(
                "SELECT * FROM entities WHERE mission=? AND kind='request' AND state NOT IN ('answered','withdrawn')",
                (mission,),
            )
        ]

    def wait_summary(self, mission: str):
        pending = self.pending_requests(mission)
        ready = self.db.execute(
            "SELECT count(*) FROM entities WHERE mission=? AND kind='task' AND state IN ('queued','running')",
            (mission,),
        ).fetchone()[0]
        return {
            "readyWork": ready,
            "pendingRequestIds": [r["id"] for r in pending],
            "nextAction": "wake peers to revise requests or open an alternative evidence route"
            if pending and not ready
            else "continue eligible work",
            "missionCompleted": False,
        }
