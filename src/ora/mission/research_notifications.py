"""Durable, metadata-only notices for substantive peer research changes.

The activity digest supplies details on a later model turn. A notice only
identifies a changed saved artifact and its intended peer task; it does not
copy search text, source content, or a peer's interpretation into a message.
"""

from __future__ import annotations

import hashlib
from typing import Any

from ora.coordination.board import ref
from ora.state import StateError
from ora.state.store import canonical

from .research_activity import _copy_current

KIND = "research_peer_notice"
EVENT = "research_peer_notice"
SUBJECT_KINDS = {
    "checked_evidence": "source_copy",
    "new_gap": "working_note",
    "responsibility_change": "board_proposal",
}
INACTIVE = frozenset({"done", "deferred", "cancelled", "closed"})


def _id(*parts: object) -> str:
    return "research-notice:" + hashlib.sha256(canonical(parts).encode()).hexdigest()[:32]


def record_research_notice(host: Any, spec: Any, *, kind: str, subject_ref: dict) -> dict:
    """Coalesce one semantic change per active recipient task in this run.

    Call only after the substantive source, plan, or responsibility change has
    committed. Repeating the exact subject is idempotent across host restarts.
    """
    expected = SUBJECT_KINDS.get(kind)
    if (
        expected is None
        or not isinstance(subject_ref, dict)
        or set(subject_ref) != {"kind", "id", "version"}
        or subject_ref.get("kind") != expected
    ):
        raise StateError("Research notice needs a supported exact subject reference")
    producer_task = spec.proposal_ref["id"]
    inputs = {
        "mission_id": spec.mission_id,
        "run_id": spec.run_id,
        "actor_id": spec.actor_id,
        "task_id": producer_task,
        "kind": kind,
        "subject_ref": subject_ref,
    }

    def save(tx):
        # Control IDs are host-defined, so use the exact run's saved controls.
        controls = host._controls(spec.run_id)
        own = [
            item
            for item in controls
            if item["body"].get("mission_id") == spec.mission_id
            and item["body"].get("run_id") == spec.run_id
            and item["body"].get("actor_id") == spec.actor_id
            and item["body"].get("task_id") == producer_task
        ]
        if not own or all(item["body"].get("phase") in INACTIVE for item in own):
            raise StateError("Research notice producer task is not active")
        subject = tx.get(**subject_ref)
        if subject is None or tx.get(expected, subject_ref["id"]) != subject:
            raise StateError("Research notice subject is unavailable or superseded")
        if kind == "checked_evidence" and (
            subject["body"].get("mission_id") not in (None, spec.mission_id)
            or not any(subject_ref in item["body"].get("source_refs", []) for item in own)
            or not _copy_current(host, tx, subject_ref)
        ):
            raise StateError("Research notice source is not currently permitted")
        if kind == "responsibility_change" and (
            subject["id"] != producer_task or subject["body"].get("mission_id") != spec.mission_id
        ):
            raise StateError("Research notice responsibility is outside this task")
        if kind == "new_gap" and any(
            subject["body"].get(key) != value
            for key, value in (
                ("mission_id", spec.mission_id),
                ("actor_id", spec.actor_id),
                ("task_id", producer_task),
            )
        ):
            raise StateError("Research notice gap is outside this task")
        if (kind == "new_gap" and subject["body"].get("run_id",
                subject["body"].get("planning", {}).get("run_id")) != spec.run_id):
            raise StateError("Research notice gap is outside this run")
        notices = []
        for control in controls:
            body = control["body"]
            if (
                body.get("mission_id") != spec.mission_id
                or body.get("run_id") != spec.run_id
                or body.get("actor_id") == spec.actor_id
                or body.get("phase") in INACTIVE
            ):
                continue
            recipient = tx.get("mission_task_control", control["id"])
            if recipient != control:
                continue
            notice_id = _id(spec.mission_id, spec.run_id, producer_task, control["id"], kind)
            old = tx.get(KIND, notice_id)
            if old is not None and old["body"]["subject_ref"] == subject_ref:
                continue
            saved = tx.put(
                KIND,
                notice_id,
                {
                    "schema": "ora.mission.research_peer_notice.v1",
                    "mission_id": spec.mission_id,
                    "run_id": spec.run_id,
                    "producer_actor_id": spec.actor_id,
                    "producer_task_id": producer_task,
                    "recipient_actor_id": body["actor_id"],
                    "recipient_task_id": body["task_id"],
                    "kind": kind,
                    "subject_ref": subject_ref,
                    "updated_at": host.store.now(),
                },
                expected_version=old["version"] if old else 0,
            )
            tx.emit(
                spec.mission_id,
                EVENT,
                {"notice_ref": ref(saved), "recipient_id": body["actor_id"]},
            )
            notices.append(ref(saved))
        return {"status": "recorded" if notices else "unchanged", "notice_refs": notices}

    result = host.store.transact(_id("record", inputs), inputs, save, mission_id=spec.mission_id)
    if result["notice_refs"]:
        host.executor.notify(spec.mission_id)
    return result
