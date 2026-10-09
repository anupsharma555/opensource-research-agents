"""Durable identities for scientific peers, lanes, and temporary specialists.

Names help readers find an agent. Immutable instance IDs, rather than names or
model settings, identify the actor that produced a record.
"""

from __future__ import annotations

import unicodedata
import uuid
from pathlib import Path
from typing import Any

from ora.skills import load_registry


class AgentIdentityError(ValueError):
    """An identity request does not satisfy the agent naming contract."""


def normalized_name(name: str) -> str:
    if not isinstance(name, str):
        raise AgentIdentityError("Display name must be text")
    display = " ".join(unicodedata.normalize("NFKC", name).split())
    if (
        not display
        or len(display) > 80
        or any(unicodedata.category(ch).startswith("C") for ch in display)
    ):
        raise AgentIdentityError("Display name must be 1–80 visible characters")
    return display.casefold()


class AgentRegistry:
    """Register actors through the shared state store's atomic alias binding."""

    KIND = "agent_instance"
    GLOBAL_CONTEXT_KINDS = frozenset({
        "publication", "study_family", "source_encounter", "source_copy", "source_passage",
    })

    def __init__(self, store: Any, project_root: Path) -> None:
        self.store = store
        self.role_ids = frozenset(load_registry(project_root, set()).roles)

    @staticmethod
    def _check_context(
        mission_id: str,
        run_id: str,
        role_id: str,
        kind: str,
        lane_id: str | None,
        role_ids: frozenset[str],
    ) -> None:
        if not mission_id or not run_id:
            raise AgentIdentityError("Mission and run IDs are required and remain distinct")
        if kind not in {"role", "lane", "temporary"}:
            raise AgentIdentityError("Unknown agent instance kind")
        if kind == "temporary":
            if role_id != "temporary_specialist" or lane_id is not None:
                raise AgentIdentityError("A temporary specialist needs its own role label")
        elif role_id not in role_ids:
            raise AgentIdentityError(f"Unknown nine-role responsibility: {role_id}")
        if kind == "lane" and (role_id != "literature_acquisition" or not lane_id):
            raise AgentIdentityError("A collection lane needs the acquisition role and a lane ID")
        if kind == "role" and lane_id is not None:
            raise AgentIdentityError("Only lane workers have a lane ID")

    def register(
        self,
        operation_id: str,
        *,
        mission_id: str,
        run_id: str,
        role_id: str,
        display_name: str,
        kind: str = "role",
        lane_id: str | None = None,
        predecessor_id: str | None = None,
    ) -> dict[str, Any]:
        """Create one permanent ID; identical operation retries return the same ID.

        Names are unique for the mission's history. A replacement chooses a new
        visible name, leaving older references and names attached to their owner.
        """
        if not operation_id:
            raise AgentIdentityError("Operation ID is required")
        self._check_context(mission_id, run_id, role_id, kind, lane_id, self.role_ids)
        name_key = normalized_name(display_name)
        display = " ".join(unicodedata.normalize("NFKC", display_name).split())
        request = {
            "mission_id": mission_id,
            "run_id": run_id,
            "role_id": role_id,
            "display_name": display,
            "kind": kind,
            "lane_id": lane_id,
            "predecessor_id": predecessor_id,
        }

        def create(tx: Any) -> dict[str, Any]:
            predecessor = None
            if predecessor_id is not None:
                predecessor = tx.get(self.KIND, predecessor_id)
                if predecessor is None or predecessor["body"]["mission_id"] != mission_id:
                    raise AgentIdentityError("Replacement predecessor is absent from this mission")
                if predecessor["body"]["role_id"] != role_id:
                    raise AgentIdentityError("Replacement must preserve the role identity")
                if predecessor["body"]["kind"] != kind or predecessor["body"]["lane_id"] != lane_id:
                    raise AgentIdentityError("Replacement must preserve its instance kind and lane")
            instance_id = f"agent-{uuid.uuid4().hex}"
            record = tx.put(self.KIND, instance_id, request, expected_version=0)
            tx.bind(f"agent-name:{mission_id}", name_key, self.KIND, instance_id)
            if predecessor is not None:
                tx.link(record, predecessor, "replaces")
            tx.emit(
                mission_id,
                "agent_registered",
                {"instance_id": instance_id, "predecessor_id": predecessor_id},
            )
            return record

        return self.store.transact(operation_id, request, create, mission_id=mission_id)

    def by_name(self, mission_id: str, display_name: str) -> dict[str, Any] | None:
        return self.store.resolve(f"agent-name:{mission_id}", normalized_name(display_name))

    def get(self, instance_id: str) -> dict[str, Any] | None:
        return self.store.get(self.KIND, instance_id)

    def record_handoff(
        self,
        operation_id: str,
        *,
        mission_id: str,
        successor_id: str,
        task_ref: dict[str, Any],
        context_refs: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Record what a replacement can inspect, without assigning or completing work."""
        if not operation_id or not mission_id or not successor_id:
            raise AgentIdentityError("Handoff needs an operation, mission and successor")
        if context_refs is not None and not isinstance(context_refs, list):
            raise AgentIdentityError("Handoff context references must be a list")
        refs = list(context_refs or [])
        all_refs = [task_ref, *refs]
        for pointer in all_refs:
            if (
                type(pointer) is not dict
                or set(pointer) != {"kind", "id", "version"}
                or not isinstance(pointer["kind"], str) or not pointer["kind"]
                or not isinstance(pointer["id"], str) or not pointer["id"]
                or type(pointer["version"]) is not int or pointer["version"] < 1
            ):
                raise AgentIdentityError("Handoff needs exact record references")
        if task_ref["kind"] != "task":
            raise AgentIdentityError("Handoff work reference must name a task")
        request = {
            "mission_id": mission_id,
            "successor_id": successor_id,
            "task_ref": task_ref,
            "context_refs": refs,
        }

        def save(tx: Any) -> dict[str, Any]:
            successor = tx.get(self.KIND, successor_id, version=1)
            if successor is None or successor["body"]["mission_id"] != mission_id:
                raise AgentIdentityError("Replacement is unavailable in this mission")
            predecessor_id = successor["body"].get("predecessor_id")
            predecessor = tx.get(self.KIND, predecessor_id, version=1) if predecessor_id else None
            if predecessor is None:
                raise AgentIdentityError("Replacement has no saved predecessor")
            targets = []
            for pointer in all_refs:
                target = tx.get(pointer["kind"], pointer["id"], version=pointer["version"])
                if target is None:
                    raise AgentIdentityError("Handoff reference is unavailable")
                owner = target["body"].get("mission_id")
                if owner != mission_id and not (
                    owner is None and pointer["kind"] in self.GLOBAL_CONTEXT_KINDS
                ):
                    raise AgentIdentityError("Handoff reference belongs to another mission")
                targets.append(target)
            body = {
                "schema": "ora.agent_handoff.v1",
                "mission_id": mission_id,
                "predecessor_ref": {"kind": self.KIND, "id": predecessor_id, "version": 1},
                "successor_ref": {"kind": self.KIND, "id": successor_id, "version": 1},
                "task_ref": task_ref,
                "context_refs": refs,
                "task_control_changed": False,
            }
            handoff = tx.put(
                "agent_handoff", f"handoff-{uuid.uuid4().hex}", body, expected_version=0,
            )
            tx.link(handoff, predecessor, "from_instance")
            tx.link(handoff, successor, "to_instance")
            tx.link(handoff, targets[0], "about_task")
            for target in targets[1:]:
                tx.link(handoff, target, "context")
            tx.emit(mission_id, "agent_handoff_recorded", {
                "handoff_ref": {key: handoff[key] for key in ("kind", "id", "version")},
                "task_ref": task_ref,
            })
            return handoff

        return self.store.transact(operation_id, request, save, mission_id=mission_id)

    def record_activity(
        self,
        operation_id: str,
        *,
        mission_id: str,
        instance_id: str,
        target_kind: str,
        target_id: str,
        target_version: int,
        task_id: str,
        attempt_id: str,
        call_id: str | None = None,
    ) -> dict[str, Any]:
        """Link an exact message/tool/skill/claim/artifact version to its actor."""
        if not all(
            (operation_id, mission_id, instance_id, target_kind, target_id, task_id, attempt_id)
        ):
            raise AgentIdentityError(
                "An activity needs operation, actor, target, task and attempt IDs"
            )
        if not isinstance(target_version, int) or target_version < 1:
            raise AgentIdentityError("Activity target needs an exact positive version")
        request = {
            "mission_id": mission_id,
            "instance_id": instance_id,
            "target_kind": target_kind,
            "target_id": target_id,
            "target_version": target_version,
            "task_id": task_id,
            "attempt_id": attempt_id,
            "call_id": call_id,
        }

        def link(tx: Any) -> dict[str, Any]:
            actor = tx.get(self.KIND, instance_id, version=1)
            target = tx.get(target_kind, target_id, version=target_version)
            if actor is None or actor["body"]["mission_id"] != mission_id or target is None:
                raise AgentIdentityError("Activity actor or exact target is unavailable")
            target_body = target["body"]
            if target_body.get("mission_id") != mission_id:
                raise AgentIdentityError("Activity target has no matching mission ownership")
            expected_actor = {"kind": self.KIND, "id": instance_id, "version": 1}
            declared_actor = target_body.get("actor_ref")
            if declared_actor is not None and declared_actor != expected_actor:
                raise AgentIdentityError("Activity target names another actor")
            if target_body.get("agent_instance_id", instance_id) != instance_id:
                raise AgentIdentityError("Activity target names another actor")
            target_call_id = target_body.get("call_id")
            if call_id is not None and target_call_id is None:
                raise AgentIdentityError("Activity target has no matching call lineage")
            if target_call_id is not None:
                manifest_version = target_body.get("manifest_version")
                if type(manifest_version) is not int or manifest_version < 1:
                    raise AgentIdentityError("Activity needs an exact originating call version")
                originating_call = tx.get("agent_call", target_call_id, version=manifest_version)
                if originating_call is None or any(
                    (
                        originating_call["body"].get("mission_id") != mission_id,
                        originating_call["body"].get("instance_id") != instance_id,
                        originating_call["body"].get("task_id") != task_id,
                        originating_call["body"].get("attempt_id") != attempt_id,
                        call_id is not None and call_id != target_call_id,
                    )
                ):
                    raise AgentIdentityError(
                        "Activity call lineage does not match the actor and work"
                    )
            elif (
                target_body.get("task_id") != task_id
                or target_body.get("attempt_id") != attempt_id
                or (declared_actor is None and target_body.get("agent_instance_id") != instance_id)
            ):
                raise AgentIdentityError(
                    "Activity needs explicit actor, task and attempt ownership"
                )
            if (
                target_body.get("task_id", task_id) != task_id
                or target_body.get("attempt_id", attempt_id) != attempt_id
            ):
                raise AgentIdentityError("Activity target work context conflicts with its call")
            activity_body = {**request, "verified_call_id": target_call_id}
            activity = tx.put(
                "agent_activity", f"activity-{uuid.uuid4().hex}", activity_body, expected_version=0
            )
            tx.link(activity, actor, "performed_by")
            tx.link(activity, target, "describes")
            return activity

        return self.store.transact(operation_id, request, link, mission_id=mission_id)
