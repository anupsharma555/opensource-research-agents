"""Prepare saved research information for an agent's next model call.

Gather the current task, peer messages, notes and tool results into a working
view that fits the caller's input allowance, keeping omitted details retrievable.
Check source permissions and save a record linking the included information to
the exact prepared request. This records preparation, not provider delivery or
scientific use; the agent still chooses what to investigate.
"""

from __future__ import annotations

import hashlib
import json
import re
from copy import deepcopy
from dataclasses import dataclass
from typing import Callable, Iterable

from ora.agents.capabilities import content_hash
from ora.coordination.board import ref, require_ref
from ora.evidence.coverage import extracted_text_coverage
from ora.mission.browser_search import project_browser_search
from ora.mission.latency import read_due_review, read_review_delivery_response
from ora.mission.output_recovery import OutputRecoveryError, read_partial_output_window
from ora.mission.report_lineage import report_text_access
from ora.mission.shared_materials import (
    shared_material_entries,
    shared_material_entry,
    shared_material_view,
)
from ora.mission.tool_results import (
    MAX_RESULT_PAGE_CHARS,
    PAGEABLE_TOOLS,
    RESULT_PAGE_SCHEMA,
    ResultProjectionChanged,
    result_window,
)
from ora.models.groq import ModelPreflightError, _messages
from ora.models.session import _explicit_prohibition
from ora.state import StateError, StateStore, Transaction
from ora.state.source_lineage import inherited_public_source, scoped_note_source_input
from ora.state.store import canonical, identifier

PACKER_VERSION = "ora.working_context.packer.v44"


def _shared_material_overview(entry: dict, inclusion_id: str | None = None) -> dict:
    """A short pointer into the exact page, without report or source body."""
    visible = shared_material_view(entry)
    overview = {
        "type": visible["type"], "ref": visible["ref"],
        "by": visible.get("origin_display_name") or visible["origin_actor_id"],
        "status": visible["status"], "title": visible.get("title"),
        "read": "read_context_page",
    }
    if inclusion_id is not None and visible.get("reader_tool") == "read_context_record":
        overview.pop("read")
        overview["read_request"] = _shared_material_reader_view(entry, inclusion_id)["read_request"]
    return overview


def _shared_material_reader_view(entry: dict, inclusion_id: str) -> dict:
    """Keep the exact report-read arguments beside its discoverable reference."""
    visible = shared_material_view(entry)
    if visible.get("reader_tool") == "read_context_record":
        visible["read_request"] = {
            "tool": "read_context_record", "inclusion_id": inclusion_id,
            **visible["ref"], "offset": 0, "limit": MAX_CONTEXT_RECORD_WINDOW_CHARS,
        }
    return visible


def _omission_marker(count: int, fetch: dict, older_records: object) -> dict | None:
    if not count:
        return None
    marker = {"count": count}
    if fetch != older_records:
        marker["fetch"] = fetch
    return marker
MAX_CONTEXT_PAGE_CHARS = 1800
MAX_CONTEXT_PAGE_ITEMS = 20
MAX_CONTEXT_RECORD_WINDOW_CHARS = 2000
MAX_RECENT_SEARCH_RECAP = 4
MAX_SEARCH_RECAP_SCAN = 12
MAX_RECENT_INSPECTION_RECAP = 4
MAX_SCOPE_PREVIEW_CHARS = 1200
MAX_PARENT_PREVIEW_CHARS = 700
MAX_PARTIAL_OUTPUT_PREVIEW_CHARS = 600
MAX_SHARED_MATERIAL_PREVIEWS = 6


class ContextError(StateError):
    """One context preparation or binding needs a scoped repair."""


class ContextOverflow(ContextError):
    """The caller's explicit input allowance cannot fit required context."""

    def __init__(self, message: str, *, required_tokens: int | None = None):
        super().__init__(message)
        self.required_tokens = required_tokens


# Prepared context: the messages plus references needed to explain what was
# included, omitted and available at this point in the run.
@dataclass(frozen=True)
class ContextPack:
    call_ref: dict
    configuration_ref: dict
    task_ref: dict
    source_refs: tuple[dict, ...]
    messages: list[dict]
    included_refs: tuple[dict, ...]
    omitted: tuple[dict, ...]
    inbox_cursor: int
    event_through: int
    estimated_tokens: int | None
    tool_result_compactions: tuple[dict, ...] = ()
    run_ref: dict | None = None
    parent_run_ref: dict | None = None
    followup_target_ref: dict | None = None
    note_text_refs: tuple[dict, ...] = ()
    conversation_source_dependency_refs: tuple[dict, ...] = ()


def _sha(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def _heads(store: StateStore, kind: str) -> list[dict]:
    items, after = [], ""
    while True:
        page = store.records(kind, after_id=after)
        items.extend(page)
        if len(page) < 1000:
            return items
        after = page[-1]["id"]


def _exact(store: StateStore, pointer: dict, mission_id: str) -> dict:
    db = store._connect()
    try:
        return require_ref(Transaction(store, db), mission_id, pointer)
    finally:
        db.close()


def _context_source_ref(store: StateStore, pointer: dict, mission_id: str,
                        actor_id: str, task_id: str) -> dict:
    db = store._connect()
    try:
        tx = Transaction(store, db)
        actor = tx.get("agent_instance", actor_id, 1)
        run_id = actor["body"].get("run_id") if actor else None
        if pointer.get("kind") == "source_copy" and isinstance(run_id, str) and (
            inherited_public_source(
                tx, mission_id=mission_id, run_id=run_id, copy_ref=pointer,
                actor_id=actor_id, task_id=task_id,
            )
        ):
            return tx.get(**pointer)
        return require_ref(tx, mission_id, pointer)
    finally:
        db.close()


def _unique(records: Iterable[dict]) -> tuple[dict, ...]:
    seen, output = set(), []
    for item in records:
        pointer = ref(item)
        key = (pointer["kind"], pointer["id"], pointer["version"])
        if key not in seen:
            seen.add(key)
            output.append(pointer)
    return tuple(output)


def _newest(records: Iterable[dict]) -> list[dict]:
    return sorted(records, key=lambda item: (item["created_at"], item["id"]), reverse=True)


def _routine_own_executor_outcome(store: StateStore, outcome: dict, actor_id: str) -> bool:
    """Recognize this peer's executor receipt wrapper, not a scientific update."""
    body = outcome["body"]
    actor_ref = body.get("actor_ref")
    if (
        outcome["kind"] != "board_outcome"
        or body.get("completes_task") is not False
        or not isinstance(actor_ref, dict)
        or actor_ref.get("id") != actor_id
        or not outcome["id"].startswith("executor-outcome:")
    ):
        return False
    suffix = outcome["id"].removeprefix("executor-outcome:")
    pointers = body.get("result_refs")
    if (
        not isinstance(pointers, list)
        or len(pointers) != 3
        or [item.get("kind") for item in pointers if isinstance(item, dict)]
        != ["executor_artifact", "executor_receipt", "coordination_trace"]
        or pointers[0].get("id") != f"executor-artifact:{suffix}"
        or pointers[1].get("id") != f"executor-receipt:{suffix}"
    ):
        return False
    records = []
    for pointer in pointers:
        if set(pointer) != {"kind", "id", "version"}:
            return False
        saved = store.get(**pointer)
        if saved is None or ref(saved) != pointer:
            return False
        records.append(saved)
    artifact, receipt, trace = records
    art = artifact["body"]
    outputs = art.get("output_refs", [])
    if not isinstance(outputs, list) or any(
        not isinstance(pointer, dict)
        or pointer.get("kind")
        in {"report", "working_note", "mail_message", "mail_answer", "board_dissent"}
        for pointer in outputs
    ):
        return False
    return bool(
        art.get("mission_id") == body.get("mission_id")
        and art.get("actor_id") == actor_id
        and art.get("handler_id") in {"mission-model", "mission-tool"}
        and receipt["body"].get("mission_id") == body.get("mission_id")
        and receipt["body"].get("artifact_ref") == pointers[0]
        and receipt["body"].get("work_id") == art.get("work_id")
        and trace["body"].get("mission_id") == body.get("mission_id")
        and trace["body"].get("instance_id") == actor_id
        and trace["body"].get("proof") == "recorded_local_callback"
        and pointers[0] in trace["body"].get("output_refs", [])
    )


class WorkingContext:
    """Prepare current peer context and prove exact inclusion in a model intent."""

    def __init__(self, store: StateStore) -> None:
        self.store = store

    @staticmethod
    def _visible_report_page_items(page: object) -> list[dict]:
        if not isinstance(page, dict):
            return []
        items = (
            page.get("items") if page.get("schema") in {
                "ora.context_omission_page.v1", "ora.context_omission_page.v2",
            }
            else page.get("included_items")
            if page.get("schema") == "ora.context_page_tool_result.v1" else None
        )
        if not isinstance(items, list):
            return []
        return [
            item for item in items
            if isinstance(item, dict)
            and isinstance(item.get("ref"), dict)
            and item["ref"].get("kind") == "report"
            and isinstance(item.get("summary"), dict)
            and isinstance(item["summary"].get("title"), dict)
            and isinstance(item["summary"]["title"].get("text"), str)
            and bool(item["summary"]["title"]["text"])
        ]

    @staticmethod
    def _shared_page_items(page: object) -> list[dict]:
        if not isinstance(page, dict):
            return []
        items = (
            page.get("items") if page.get("schema") == "ora.context_omission_page.v2"
            else page.get("included_items")
            if page.get("schema") == "ora.context_page_tool_result.v1" else None
        )
        return [item for item in items or []
                if isinstance(item, dict) and item.get("type") == "shared_material"
                and isinstance(item.get("ref"), dict)]

    # Source access: saved material and notes must keep their exact source links
    # and current use status before their text can be reused in a model input.
    def _source_status(self, selected: dict) -> dict:
        """Show separate intake policy and current copy use without implying permission."""
        current = self.store.get("source_copy", selected["id"])
        selected_use = self.store.resolve(
            "source_use.copy", f"{selected['id']}:{selected['version']}"
        )
        current_use = (
            self.store.resolve("source_use.copy", f"{current['id']}:{current['version']}")
            if current
            else None
        )
        use = current_use["body"] if current_use else None
        valid_use = bool(use and use.get("copy_ref") == ref(current))
        selected_policy = selected_use["body"] if selected_use else None
        valid_selected = bool(selected_policy and selected_policy.get("copy_ref") == ref(selected))
        selected_status = (
            "permitted"
            if valid_selected
            and "model_input" in selected_policy.get("allowed_uses", [])
            and selected_policy.get("policy_status") == "granted"
            else "restricted"
            if valid_selected
            else "unknown"
        )
        current_status = (
            "permitted"
            if valid_use
            and "model_input" in use.get("allowed_uses", [])
            and use.get("policy_status") == "granted"
            else "restricted"
            if valid_use
            else "unknown"
        )
        if (
            _explicit_prohibition(selected["body"])
            or current
            and _explicit_prohibition(current["body"])
            or selected_status == "restricted"
        ):
            status = "restricted"
        else:
            status = selected_status
        return {
            "selected_ref": ref(selected),
            "current_ref": ref(current) if current else None,
            "selected_source_use_ref": ref(selected_use) if selected_use else None,
            "current_source_use_ref": ref(current_use) if current_use else None,
            "selected_model_input_status": selected_status,
            "current_model_input_status": current_status,
            "model_input_status": status,
            "current_allowed_uses": use.get("allowed_uses") if valid_use else None,
            "current_policy_status": use.get("policy_status") if valid_use else None,
            "fetch": ref(selected),
            "authorization": "host_must_recheck_source_use_and_copy_before_dispatch",
        }

    def require_current_source_use(self, source_refs: Iterable[dict]) -> list[dict]:
        """Reject a known copy or intake-policy revocation at this call boundary.

        Unknown policy remains explicit; the model bridge still checks the
        selected source decision and retained bytes.
        """
        statuses = []
        for pointer in source_refs:
            selected = self.store.get(**pointer)
            if selected is None or selected["kind"] != "source_copy":
                raise ContextError("Exact source copy is unavailable")
            status = self._source_status(selected)
            if status["model_input_status"] == "restricted":
                raise ContextError(
                    "Current or selected source-use policy restricts this model input"
                )
            statuses.append(status)
        return statuses

    def working_note_text_access(self, note: dict) -> dict:
        """Resolve only exact note input ancestry before exposing draft text.

        A note remains saved when one of its source inputs is no longer usable.
        Unsupported or broken ancestry is not treated as source-free text.
        """
        dependencies: dict[tuple[str, str, int], dict] = {}
        visiting: set[tuple[str, str, int]] = set()
        visited: set[tuple[str, str, int]] = set()
        complete = True
        scope = note["body"]
        actor_id = scope.get("actor_id")
        actor = self.store.get("agent_instance", actor_id, 1) if isinstance(actor_id, str) else None
        run_id = actor["body"].get("run_id") if (
            actor and actor["body"].get("mission_id") == scope.get("mission_id")
        ) else None

        def inherited_input(pointer: dict) -> bool:
            return isinstance(run_id, str) and scoped_note_source_input(
                self.store, mission_id=scope.get("mission_id"), run_id=run_id,
                actor_id=scope.get("actor_id"), task_id=scope.get("task_id"),
                pointer=pointer,
            ) is not None

        def visit(pointer: object) -> None:
            nonlocal complete
            if (
                not isinstance(pointer, dict)
                or set(pointer) != {"kind", "id", "version"}
                or not isinstance(pointer.get("kind"), str)
                or not isinstance(pointer.get("id"), str)
                or type(pointer.get("version")) is not int
                or pointer["version"] <= 0
            ):
                complete = False
                return
            key = (pointer["kind"], pointer["id"], pointer["version"])
            if key in visiting:
                complete = False
                return
            if key in visited:
                return
            visiting.add(key)
            record = self.store.get(**pointer)
            if record is None or ref(record) != pointer:
                complete = False
                visiting.remove(key)
                return
            if record["kind"] == "source_copy":
                if (record["body"].get("mission_id") not in (None, scope.get("mission_id"))
                        and not inherited_input(pointer)):
                    complete = False
                dependencies[key] = pointer
            elif record["kind"] in {"source_passage", "source_read_window"}:
                copy_ref = record["body"].get("copy_ref")
                copy = self.store.get(**copy_ref) if (
                    isinstance(copy_ref, dict)
                    and set(copy_ref) == {"kind", "id", "version"}
                    and copy_ref.get("kind") == "source_copy"
                    and type(copy_ref.get("id")) is str
                    and type(copy_ref.get("version")) is int
                    and copy_ref["version"] > 0
                ) else None
                if (copy is not None
                        and copy["body"].get("mission_id") not in (
                            None, scope.get("mission_id"),
                        ) and not inherited_input(pointer)):
                    complete = False
                visit(copy_ref)
            elif record["kind"] == "working_note":
                body = record["body"]
                if any(body.get(field) != scope.get(field)
                       for field in ("mission_id", "actor_id", "task_id")):
                    complete = False
                elif isinstance(body.get("input_refs"), list):
                    for item in body["input_refs"]:
                        visit(item)
                else:
                    complete = False
            else:
                complete = False
            visiting.remove(key)
            visited.add(key)

        inputs = scope.get("input_refs")
        if isinstance(inputs, list):
            for pointer in inputs:
                visit(pointer)
        else:
            complete = False
        unavailable = []
        for pointer in dependencies.values():
            copy = self.store.get(**pointer)
            if copy is None or self._source_status(copy)["model_input_status"] != "permitted":
                unavailable.append(pointer)
        return {
            "status": "available" if complete and not unavailable else "data_unavailable_due_source_use",
            "source_dependency_refs": list(dependencies.values()),
            "unavailable_source_refs": unavailable,
            "dependency_lineage_complete": complete,
        }

    def conversation_summary_access(
        self, summary: dict, *, mission_id: str, actor_id: str, task_id: str,
    ) -> dict:
        """Check current source use for one saved model-authored summary."""
        body = summary["body"]
        if (
            summary["kind"] != "context_summary"
            or body.get("mission_id") != mission_id
            or body.get("actor_id") != actor_id
            or body.get("task_id") != task_id
            or body.get("authority") != "model_authored_continuation_aid"
            or not isinstance(body.get("text"), str)
            or not isinstance(body.get("source_dependency_refs"), list)
        ):
            raise ContextError("Conversation summary is outside this peer's task")
        unavailable = []
        for pointer in body["source_dependency_refs"]:
            copy = self.store.get(**pointer) if (
                isinstance(pointer, dict)
                and set(pointer) == {"kind", "id", "version"}
                and pointer.get("kind") == "source_copy"
            ) else None
            if (copy is None or ref(copy) != pointer
                    or self._source_status(copy)["model_input_status"] != "permitted"):
                unavailable.append(pointer)
        return {
            "status": "available" if not unavailable else "data_unavailable_due_source_use",
            "source_dependency_refs": body["source_dependency_refs"],
            "unavailable_source_refs": unavailable,
        }

    def working_note_projection(self, note: dict) -> dict:
        """Return the exact record or a text-free current-use diagnostic."""
        access = self.working_note_text_access(note)
        if access["status"] == "available":
            return note
        body = note["body"]
        return {
            **note,
            "body": {
                key: body[key] for key in (
                    "schema", "mission_id", "actor_id", "task_id", "category",
                    "status", "input_refs", "updated_at",
                ) if key in body
            } | {
                **access,
                "next_action": "Inspect the exact source dependency and current use; revise this note from a permitted copy or continue without its text.",
            },
        }

    def _checked_note_tool_payload(self, payload: dict, result_ref: dict) -> dict:
        """Keep saved tool pairing while removing restricted note or summary text."""
        name = payload.get("name")
        if name not in {"read_note", "read_plan", "search_notes", "read_context_page", "read_context_record"}:
            return payload
        try:
            content = json.loads(payload["content"])
        except (KeyError, TypeError, ValueError):
            return {**payload, "content": canonical({
                "status": "saved_note_read_unavailable",
                "saved_tool_result_ref": result_ref,
                "next_action": "Reissue the exact note read after checking current source use.",
            })}
        if not isinstance(content, dict):
            return payload
        changed = False

        def exact_note(value: object) -> dict | None:
            if not isinstance(value, dict):
                return None
            pointer = {key: value.get(key) for key in ("kind", "id", "version")}
            if (pointer["kind"] != "working_note" or not isinstance(pointer["id"], str)
                    or type(pointer["version"]) is not int):
                return None
            note = self.store.get(**pointer)
            return note if note and ref(note) == pointer else None

        def summary_access(value: object) -> dict | None:
            if (not isinstance(value, dict)
                    or set(value) != {"kind", "id", "version"}
                    or value.get("kind") != "context_summary"):
                return None
            summary = self.store.get(**value)
            result = self.store.get(**result_ref)
            response_ref = result["body"].get("response_ref") if result else None
            response = self.store.get(**response_ref) if isinstance(response_ref, dict) else None
            call_ref = response["body"].get("call_ref") if response else None
            call = self.store.get(**call_ref) if isinstance(call_ref, dict) else None
            if summary is None or ref(summary) != value or result is None or call is None:
                return None
            try:
                return self.conversation_summary_access(
                    summary, mission_id=result["body"]["mission_id"],
                    actor_id=call["body"]["instance_id"],
                    task_id=call["body"]["task_id"],
                )
            except (ContextError, KeyError, TypeError):
                return None

        if name in {"read_note", "read_plan"} and isinstance(content.get("note"), dict):
            saved = exact_note(content["note"])
            if saved is None:
                content["note"] = {
                    "status": "exact_working_note_unavailable", "saved_tool_result_ref": result_ref,
                }
                changed = True
            else:
                projected = self.working_note_projection(saved)
                if "text" not in projected["body"]:
                    content["note"] = projected
                    if name == "read_plan":
                        content["status"] = "data_unavailable_due_source_use"
                    changed = True
        elif name == "search_notes" and isinstance(content.get("notes"), list):
            safe_notes = []
            for item in content["notes"]:
                saved = exact_note(item)
                projected = self.working_note_projection(saved) if saved else None
                if projected is None:
                    safe_notes.append({
                        "status": "exact_working_note_unavailable", "saved_tool_result_ref": result_ref,
                    })
                    changed = True
                elif "text" not in projected["body"]:
                    safe_notes.append(projected)
                    changed = True
                else:
                    safe_notes.append(item)
            if changed:
                content["notes"] = safe_notes
        elif name == "read_context_record":
            pointer = content.get("ref")
            if isinstance(pointer, dict) and pointer.get("kind") == "working_note":
                saved = self.store.get(**pointer) if set(pointer) == {"kind", "id", "version"} else None
                access = self.working_note_text_access(saved) if saved else None
                if access is None or access["status"] != "available":
                    content = {
                        "status": "data_unavailable_due_source_use",
                        "note_ref": pointer,
                        "saved_tool_result_ref": result_ref,
                        **(access or {"dependency_lineage_complete": False}),
                        "next_action": "Inspect current source use and reissue this exact note read after access is resolved.",
                    }
                    changed = True
            elif isinstance(pointer, dict) and pointer.get("kind") == "context_summary":
                access = summary_access(pointer)
                if access is None or access["status"] != "available":
                    content = {
                        "status": "data_unavailable_due_source_use",
                        "summary_ref": pointer,
                        "saved_tool_result_ref": result_ref,
                        **(access or {"source_dependency_refs": [],
                                       "unavailable_source_refs": []}),
                    }
                    changed = True
        elif name == "read_context_page" and isinstance(content.get("items"), list):
            for item in content["items"]:
                pointer = item.get("ref") if isinstance(item, dict) else None
                if not isinstance(pointer, dict):
                    continue
                if pointer.get("kind") == "context_summary":
                    access = summary_access(pointer)
                    if access is None or access["status"] != "available":
                        item["summary"] = {
                            "kind": "context_summary",
                            "status": "data_unavailable_due_source_use",
                            **(access or {"source_dependency_refs": [],
                                           "unavailable_source_refs": []}),
                        }
                        changed = True
                    continue
                if pointer.get("kind") != "working_note":
                    continue
                saved = self.store.get(**pointer) if set(pointer) == {"kind", "id", "version"} else None
                access = self.working_note_text_access(saved) if saved else None
                if access is None or access["status"] != "available":
                    item["summary"] = {
                        "kind": "working_note", "status": "data_unavailable_due_source_use",
                        **(access or {"dependency_lineage_complete": False}),
                    }
                    changed = True
        return {**payload, "content": canonical(content)} if changed else payload

    @staticmethod
    def _visible_note_refs(payload: dict) -> tuple[dict, ...]:
        try:
            content = json.loads(payload["content"])
        except (KeyError, TypeError, ValueError):
            return ()
        if not isinstance(content, dict):
            return ()
        name = payload.get("name")
        if content.get("schema") == RESULT_PAGE_SCHEMA:
            return tuple(content.get("note_text_refs", []))
        records = ([content.get("note")] if name in {"read_note", "read_plan"} else
                   content.get("notes", []) if name == "search_notes" else [])
        refs = []
        for note in records:
            if (isinstance(note, dict) and note.get("kind") == "working_note"
                    and isinstance(note.get("body"), dict)
                    and isinstance(note["body"].get("text"), str)):
                refs.append(ref(note))
        if name == "read_context_record" and isinstance(content.get("ref"), dict):
            if content["ref"].get("kind") == "working_note" and "text" in content:
                refs.append(content["ref"])
        if name == "read_context_page":
            items = content.get("included_items", content.get("items", []))
            for item in items if isinstance(items, list) else []:
                if (isinstance(item, dict) and isinstance(item.get("ref"), dict)
                        and item["ref"].get("kind") == "working_note"
                        and isinstance(item.get("summary"), dict)
                        and "text" in item["summary"]):
                    refs.append(item["ref"])
        return tuple(refs)

    def restricted_working_notes(self, note_refs: Iterable[dict]) -> list[dict]:
        """Return exact current-use diagnostics for note text in a model input."""
        restricted = []
        for pointer in note_refs:
            note = self.store.get(**pointer) if (
                isinstance(pointer, dict) and set(pointer) == {"kind", "id", "version"}
                and pointer.get("kind") == "working_note"
            ) else None
            access = self.working_note_text_access(note) if note else {
                "status": "data_unavailable_due_source_use",
                "source_dependency_refs": [], "unavailable_source_refs": [],
                "dependency_lineage_complete": False,
            }
            if access["status"] != "available":
                restricted.append({"note_ref": pointer, **access})
        return restricted

    def working_note_source_dependency_refs(self, note_refs: Iterable[dict]) -> tuple[dict, ...]:
        """Bind permitted exact copy dependencies for note text entering one call."""
        dependencies: dict[tuple[str, str, int], dict] = {}
        restricted = []
        for pointer in note_refs:
            note = self.store.get(**pointer) if (
                isinstance(pointer, dict) and set(pointer) == {"kind", "id", "version"}
                and pointer.get("kind") == "working_note"
            ) else None
            access = self.working_note_text_access(note) if note else {
                "status": "data_unavailable_due_source_use",
                "source_dependency_refs": [], "unavailable_source_refs": [],
                "dependency_lineage_complete": False,
            }
            if access["status"] != "available":
                restricted.append({"note_ref": pointer, **access})
            for copy_ref in access["source_dependency_refs"]:
                key = (copy_ref["kind"], copy_ref["id"], copy_ref["version"])
                dependencies[key] = copy_ref
        if restricted:
            raise ContextError("Working note source use changed: " + canonical(restricted))
        return tuple(dependencies.values())

    def context_source_dependency_refs(self, pack: ContextPack) -> tuple[dict, ...]:
        """Bind sources behind text actually retained in this complete context."""
        dependencies = {
            (item["kind"], item["id"], item["version"]): item
            for item in self.working_note_source_dependency_refs(pack.note_text_refs)
        }
        for pointer in pack.conversation_source_dependency_refs:
            copy = self.store.get(**pointer) if isinstance(pointer, dict) else None
            if copy is None or self._source_status(copy)["model_input_status"] != "permitted":
                raise ContextError("Compaction argument source use changed")
            dependencies[(pointer["kind"], pointer["id"], pointer["version"])] = pointer

        def add_report(access: dict) -> None:
            if access["status"] != "available":
                raise ContextError("Visible report source use changed during preparation")
            for pointer in access["source_dependency_refs"]:
                dependencies[(pointer["kind"], pointer["id"], pointer["version"])] = pointer

        try:
            index = json.loads(pack.messages[-1]["content"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ContextError("Assembled context index is invalid") from exc
        if not isinstance(index, dict):
            raise ContextError("Assembled context index is invalid")
        browser_view = index.get("browser_search")
        if isinstance(browser_view, dict) and browser_view.get("kind") == "browser_search_discovery":
            for pointer in self.response_source_dependencies(browser_view["response_receipt_ref"]):
                dependencies[(pointer["kind"], pointer["id"], pointer["version"])] = pointer
        for observation in index.get("recent_scholarly_searches", []):
            search = observation.get("search") if isinstance(observation, dict) else None
            if isinstance(search, dict) and search.get("status") == "saved_tool_observation_not_evidence_use":
                for pointer in self.response_source_dependencies(observation["response_ref"]):
                    dependencies[(pointer["kind"], pointer["id"], pointer["version"])] = pointer
        navigation = index.get("latest_search_navigation")
        if isinstance(navigation, dict) and navigation.get("first_hit_ids"):
            for pointer in self.response_source_dependencies(navigation["response_ref"]):
                dependencies[(pointer["kind"], pointer["id"], pointer["version"])] = pointer
        summary_view = index.get("summary")
        if isinstance(summary_view, dict) and summary_view.get("authority") == "model_authored_continuation_aid_not_verified_evidence":
            pointer = summary_view.get("ref")
            summary = self.store.get(**pointer) if isinstance(pointer, dict) else None
            if (summary is None or ref(summary) != pointer
                    or summary["body"].get("text") != summary_view.get("text")):
                raise ContextError("Assembled conversation summary changed")
            for source_ref in summary["body"].get("source_dependency_refs", []):
                copy = self.store.get(**source_ref) if isinstance(source_ref, dict) else None
                if copy is None or self._source_status(copy)["model_input_status"] != "permitted":
                    raise ContextError("Conversation summary source use changed")
                dependencies[(source_ref["kind"], source_ref["id"], source_ref["version"])] = source_ref
        target = index.get("followup_target")
        if (
            isinstance(target, dict)
            and ("target_title" in target or "target_body_preview" in target)
        ):
            report = self.store.get(**pack.followup_target_ref)
            inclusion = {"body": {
                "run_ref": pack.run_ref,
                "followup_target_ref": pack.followup_target_ref,
                "source_refs": list(pack.source_refs),
            }}
            if report is None or target.get("target_ref") != ref(report):
                raise ContextError("Visible parent report changed during preparation")
            add_report(self._followup_target_text_access(inclusion, report))
        shared = index.get("shared_materials")
        if isinstance(shared, dict):
            shared_config = self.store.get(**pack.configuration_ref)
            if shared_config is None:
                raise ContextError("Visible shared-material context lost its configuration")
            shared_scope = {
                "mission_id": shared_config["body"]["mission_id"],
                "actor_id": shared_config["body"]["instance_id"],
                "task_id": pack.task_ref["id"],
            }
            overview = shared.get("overview")
            if isinstance(overview, dict):
                pointer = overview.get("ref")
                current = shared_material_entry(
                    self.store, **shared_scope, pointer=pointer,
                )
                inclusion_id = (
                    f"context:model-intent:{pack.call_ref['id']}:{pack.call_ref['version']}"
                    if "read_request" in overview else None
                )
                if current is None or _shared_material_overview(current, inclusion_id) != overview:
                    raise ContextError("Visible shared-material overview changed")
                if (current["type"] == "shared_accepted_report"
                        and isinstance(current.get("title"), str)):
                    add_report(report_text_access(self.store, self.store.get(**pointer)))
                for source_ref in current.get("source_dependency_refs", []):
                    dependencies[(source_ref["kind"], source_ref["id"],
                                  source_ref["version"])] = source_ref
            for visible in shared.get("entries", []):
                pointer = visible.get("ref") if isinstance(visible, dict) else None
                current = shared_material_entry(
                    self.store, **shared_scope, pointer=pointer,
                )
                expected = (
                    _shared_material_reader_view(
                        current,
                        f"context:model-intent:{pack.call_ref['id']}:{pack.call_ref['version']}",
                    ) if current is not None and "read_request" in visible
                    else shared_material_view(current) if current is not None else None
                )
                # Older pinned requests retain their metadata-only view.
                if expected != visible:
                    raise ContextError("Visible shared-material metadata changed")
                if (visible.get("type") == "shared_accepted_report"
                        and isinstance(visible.get("title"), str)):
                    report = self.store.get(**pointer)
                    add_report(report_text_access(self.store, report))
                for source_ref in current.get("source_dependency_refs", []):
                    dependencies[(source_ref["kind"], source_ref["id"],
                                  source_ref["version"])] = source_ref

        config = self.store.get(**pack.configuration_ref)
        if config is None:
            raise ContextError("Prepared report context lost its configuration")
        for position, message in enumerate(pack.messages):
            if message.get("role") == "tool" and message.get("name") in {
                "calculate", "search_saved_passages", "read_browser_search_result",
            }:
                try:
                    result = json.loads(message["content"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise ContextError("Visible research tool result is invalid") from exc
                for pointer in result.get("source_dependency_refs", []):
                    copy = self.store.get(**pointer) if isinstance(pointer, dict) else None
                    if copy is None or self._source_status(copy)["model_input_status"] != "permitted":
                        raise ContextError("Visible research tool source use changed")
                    dependencies[(pointer["kind"], pointer["id"], pointer["version"])] = pointer
            if message.get("role") == "tool" and message.get("name") == "read_source_window":
                try:
                    window_result = json.loads(message["content"])
                except (KeyError, TypeError, ValueError) as exc:
                    raise ContextError("Visible source window result is invalid") from exc
                if isinstance(window_result, dict) and window_result.get("copy_ref"):
                    copy_ref = window_result["copy_ref"]
                    copy = self.store.get(**copy_ref) if isinstance(copy_ref, dict) else None
                    if copy is None or self._source_status(copy)["model_input_status"] != "permitted":
                        raise ContextError("Visible source window source use changed")
                    window_ref = window_result.get("window_ref")
                    if window_ref:
                        window = self.store.get(**window_ref)
                        if window is None or window["body"].get("copy_ref") != copy_ref:
                            raise ContextError("Visible source window lineage changed")
                    dependencies[(copy_ref["kind"], copy_ref["id"], copy_ref["version"])] = copy_ref
            if message.get("role") == "tool" and message.get("name") == "read_conversation_history":
                try:
                    page = json.loads(message["content"])
                    if isinstance(page, dict) and page.get("status") == "rejected":
                        continue  # A local rejection contains no archived text.
                    archive_ref = page["archive_ref"]
                except (KeyError, TypeError, ValueError) as exc:
                    raise ContextError("Visible conversation page is invalid") from exc
                archive = self.store.get(**archive_ref)
                if archive is None or ref(archive) != archive_ref:
                    raise ContextError("Visible conversation archive is unavailable")
                for source_ref in archive["body"].get("source_dependency_refs", []):
                    copy = self.store.get(**source_ref) if isinstance(source_ref, dict) else None
                    if copy is None or self._source_status(copy)["model_input_status"] != "permitted":
                        raise ContextError("Visible conversation page source use changed")
                    dependencies[(source_ref["kind"], source_ref["id"], source_ref["version"])] = source_ref
            if message.get("role") != "tool" or message.get("name") not in {
                "read_context_record", "read_context_page",
            }:
                continue
            try:
                window = json.loads(message["content"])
            except (KeyError, TypeError, ValueError):
                continue
            if message["name"] == "read_context_page":
                visible_items = self._visible_report_page_items(window)
                shared_items = self._shared_page_items(window)
                if not visible_items and not shared_items:
                    continue
                assistant = pack.messages[position - 1] if position else {}
                calls = assistant.get("tool_calls", []) if assistant.get("role") == "assistant" else []
                if len(calls) != 1 or calls[0].get("id") != message.get("tool_call_id"):
                    raise ContextError("Visible report page lost its exact tool call")
                try:
                    arguments = json.loads(calls[0]["function"]["arguments"])
                    inclusion_ref = {"kind": "context_inclusion",
                                     "id": arguments["inclusion_id"], "version": 1}
                    cursor, limit = arguments["cursor"], arguments["limit"]
                except (KeyError, TypeError, ValueError) as exc:
                    raise ContextError("Visible report page has invalid read arguments") from exc
                checked = self.read_omitted_page(
                    mission_id=config["body"]["mission_id"],
                    actor_id=config["body"]["instance_id"],
                    task_id=pack.task_ref["id"], inclusion_ref=inclusion_ref,
                    cursor=cursor, limit=limit,
                )
                if (
                    window.get("schema") == "ora.context_omission_page.v2"
                    and checked != window
                    or window.get("schema") == "ora.context_page_tool_result.v1"
                    and checked["items"][:len(window["included_items"])]
                    != window["included_items"]
                ):
                    raise ContextError("Visible report page changed during preparation")
                for item in shared_items:
                    pointer = item["ref"]
                    current = shared_material_entry(
                        self.store, mission_id=config["body"]["mission_id"],
                        actor_id=config["body"]["instance_id"],
                        task_id=pack.task_ref["id"], pointer=pointer,
                    )
                    summary = item.get("summary", {})
                    if current is None:
                        if summary.get("status") != "shared_material_unavailable":
                            raise ContextError("Visible shared page lost current authority")
                        continue
                    title = summary.get("title")
                    if (summary.get("status") != current["status"]
                            or isinstance(title, dict)
                            and title.get("text") != (current.get("title") or "")[:300]):
                        raise ContextError("Visible shared page metadata changed")
                    for source_ref in current.get("source_dependency_refs", []):
                        dependencies[(source_ref["kind"], source_ref["id"],
                                      source_ref["version"])] = source_ref
                inclusion = self.store.get(**inclusion_ref)
                if inclusion is None:
                    raise ContextError("Visible report page lost its source inclusion")
                for item in visible_items:
                    report = self.store.get(**item["ref"])
                    if report is None:
                        raise ContextError("Visible report page lost its exact report")
                    access = (
                        self._followup_target_text_access(inclusion, report)
                        if item.get("type") == "followup_target_detail"
                        else self._shared_report_text_access(inclusion, report)
                        if item.get("type") == "shared_material"
                        else self._same_mission_report_text_access(inclusion, report)
                    )
                    add_report(access)
                continue
            if (
                isinstance(window, dict)
                and window.get("schema") == "ora.context_record_window.v1"
                and isinstance(window.get("ref"), dict)
                and window["ref"].get("kind") == "retrieval_hit"
            ):
                assistant = pack.messages[position - 1] if position else {}
                calls = assistant.get("tool_calls", []) if assistant.get("role") == "assistant" else []
                if len(calls) != 1 or calls[0].get("id") != message.get("tool_call_id"):
                    raise ContextError("Visible shared hit window lost its exact tool call")
                arguments = json.loads(calls[0]["function"]["arguments"])
                pointer = {key: arguments[key] for key in ("kind", "id", "version")}
                checked = self.read_omitted_record(
                    mission_id=config["body"]["mission_id"],
                    actor_id=config["body"]["instance_id"],
                    task_id=pack.task_ref["id"],
                    inclusion_ref={"kind": "context_inclusion",
                                   "id": arguments["inclusion_id"], "version": 1},
                    pointer=pointer, offset=arguments["offset"], limit=arguments["limit"],
                )
                if checked != window:
                    raise ContextError("Visible shared hit window changed during preparation")
                current = shared_material_entry(
                    self.store, mission_id=config["body"]["mission_id"],
                    actor_id=config["body"]["instance_id"],
                    task_id=pack.task_ref["id"], pointer=pointer,
                )
                if current is None:
                    raise ContextError("Visible shared hit lost current authority")
                for source_ref in current.get("source_dependency_refs", []):
                    dependencies[(source_ref["kind"], source_ref["id"],
                                  source_ref["version"])] = source_ref
                continue
            if (
                isinstance(window, dict)
                and window.get("schema") == "ora.context_record_window.v1"
                and isinstance(window.get("ref"), dict)
                and window["ref"].get("kind") == "context_summary"
                and isinstance(window.get("text"), str)
            ):
                try:
                    projection = json.loads(window["text"])
                except (TypeError, ValueError):
                    projection = None
                if not isinstance(projection, dict) or not isinstance(projection.get("text"), str):
                    continue
                assistant = pack.messages[position - 1] if position else {}
                calls = assistant.get("tool_calls", []) if assistant.get("role") == "assistant" else []
                if len(calls) != 1 or calls[0].get("id") != message.get("tool_call_id"):
                    raise ContextError("Visible summary window lost its exact tool call")
                try:
                    arguments = json.loads(calls[0]["function"]["arguments"])
                    pointer = {key: arguments[key] for key in ("kind", "id", "version")}
                    inclusion_ref = {"kind": "context_inclusion",
                                     "id": arguments["inclusion_id"], "version": 1}
                except (KeyError, TypeError, ValueError) as exc:
                    raise ContextError("Visible summary window has invalid read arguments") from exc
                if pointer != window["ref"] or pointer["kind"] != "context_summary":
                    raise ContextError("Visible summary window differs from its exact request")
                checked = self.read_omitted_record(
                    mission_id=config["body"]["mission_id"],
                    actor_id=config["body"]["instance_id"],
                    task_id=pack.task_ref["id"], inclusion_ref=inclusion_ref,
                    pointer=pointer, offset=arguments["offset"], limit=arguments["limit"],
                )
                if checked != window:
                    raise ContextError("Visible summary window changed during preparation")
                summary = self.store.get(**pointer)
                access = self.conversation_summary_access(
                    summary, mission_id=config["body"]["mission_id"],
                    actor_id=config["body"]["instance_id"], task_id=pack.task_ref["id"],
                )
                if access["status"] != "available":
                    raise ContextError("Visible summary source use changed")
                for source_ref in access["source_dependency_refs"]:
                    dependencies[(source_ref["kind"], source_ref["id"], source_ref["version"])] = source_ref
                continue
            if (
                not isinstance(window, dict)
                or window.get("schema") != "ora.context_record_window.v1"
                or not isinstance(window.get("ref"), dict)
                or window["ref"].get("kind") != "report"
                or window.get("report_text_status") != "available"
                or not window.get("text")
            ):
                continue
            assistant = pack.messages[position - 1] if position else {}
            calls = assistant.get("tool_calls", []) if assistant.get("role") == "assistant" else []
            if len(calls) != 1 or calls[0].get("id") != message.get("tool_call_id"):
                raise ContextError("Visible report window lost its exact tool call")
            try:
                arguments = json.loads(calls[0]["function"]["arguments"])
                pointer = {key: arguments[key] for key in ("kind", "id", "version")}
                inclusion_ref = {"kind": "context_inclusion",
                                 "id": arguments["inclusion_id"], "version": 1}
            except (KeyError, TypeError, ValueError) as exc:
                raise ContextError("Visible report window has invalid read arguments") from exc
            if pointer != window["ref"] or pointer["kind"] != "report":
                raise ContextError("Visible report window differs from its exact request")
            checked = self.read_omitted_record(
                mission_id=config["body"]["mission_id"],
                actor_id=config["body"]["instance_id"],
                task_id=pack.task_ref["id"], inclusion_ref=inclusion_ref,
                pointer=pointer, offset=arguments["offset"], limit=arguments["limit"],
            )
            if checked != window:
                raise ContextError("Visible report window changed during preparation")
            inclusion = self.store.get(**inclusion_ref)
            report = self.store.get(**pointer)
            if inclusion is None or report is None:
                raise ContextError("Visible report window lost its source record")
            followup = any(
                item.get("type") == "followup_target_detail" and item.get("fetch") == pointer
                for item in inclusion["body"].get("omitted", [])
            )
            shared_report = any(
                item.get("type") == "shared_material" and item.get("fetch") == pointer
                for item in inclusion["body"].get("omitted", [])
            )
            add_report(
                self._followup_target_text_access(inclusion, report) if followup
                else self._shared_report_text_access(inclusion, report) if shared_report
                else self._same_mission_report_text_access(inclusion, report)
            )
        return tuple(dependencies.values())

    def prepared_working_note_restrictions(self, intent_ref: dict) -> list[dict]:
        """Recheck note text in exact retained request bytes before dispatch."""
        intent = self.store.get(**intent_ref)
        if intent is None or intent["kind"] != "model_request_intent":
            raise ContextError("Prepared model input is unavailable")
        request = json.loads(self.store.read_blob(intent["body"]["blob"]["sha256"]))
        refs = []
        for message in request.get("messages", []):
            if message.get("role") == "tool":
                refs.extend(self._visible_note_refs(message))
            elif message.get("role") == "user":
                try:
                    content = json.loads(message["content"])
                except (KeyError, TypeError, ValueError):
                    continue
                if not isinstance(content, dict):
                    continue
                for item in content.get("details", []):
                    if (isinstance(item, dict) and item.get("type") == "note"
                            and isinstance(item.get("body"), dict)
                            and isinstance(item["body"].get("text"), str)):
                        refs.append(item["ref"])
        unique = {(item["kind"], item["id"], item["version"]): item for item in refs}
        return self.restricted_working_notes(unique.values())

    # Saved tool results: recover only this peer's authorized task data, with
    # bounded pages for large results rather than repeating the tool action.
    def _saved_result_scope(self, result_id: str, *, mission_id: str,
                            actor_id: str, task_id: str) -> tuple[dict, dict]:
        """Resolve a saved result through its exact originating model request."""
        result = self.store.get("context_tool_result", result_id, 1)
        if result is None or result["body"].get("mission_id") != mission_id:
            raise ContextError("Saved tool result is unavailable in this mission")
        response = self.store.get(**result["body"]["response_ref"])
        call = self.store.get(**response["body"]["call_ref"]) if response else None
        if (response is None or response["kind"] != "model_response_receipt"
                or response["body"].get("status") != "tool_requested"
                or response["body"].get("mission_id") != mission_id
                or call is None or call["kind"] != "agent_call"
                or any(call["body"].get(key) != expected for key, expected in (
                    ("mission_id", mission_id), ("instance_id", actor_id), ("task_id", task_id),
                ))):
            raise ContextError("Saved tool result belongs to another peer task")
        payload = json.loads(self.store.read_blob(result["body"]["blob"]["sha256"]))
        raw = json.loads(self.store.read_blob(response["body"]["blob"]["sha256"]))
        calls = raw.get("tool_calls", [])
        if (len(calls) != 1 or payload.get("tool_call_id") != calls[0].get("id")
                or payload.get("name") != calls[0].get("function", {}).get("name")
                or _sha(payload) != result["body"].get("message_sha256")):
            raise ContextError("Saved result differs from its exact tool request")
        return result, payload

    def read_tool_result(self, *, mission_id: str, actor_id: str, task_id: str,
                         result_id: str, offset: int, limit: int,
                         projection_sha256: str) -> dict:
        """Read allowlisted result projections, never arbitrary records or blobs."""
        result, payload = self._saved_result_scope(
            result_id, mission_id=mission_id, actor_id=actor_id, task_id=task_id,
        )
        name = payload.get("name")
        if name not in PAGEABLE_TOOLS:
            raise ContextError("This saved tool result has no authorized pageable projection")
        try:
            content = json.loads(payload["content"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ContextError("Saved tool result is not structured JSON") from exc
        if not isinstance(content, dict):
            raise ContextError("Saved tool result has no authorized object projection")
        notes = []

        def note_projection(item: object) -> dict:
            if not isinstance(item, dict):
                raise ContextError("Saved note result is malformed")
            pointer = {key: item.get(key) for key in ("kind", "id", "version")}
            if (pointer["kind"] != "working_note" or not isinstance(pointer["id"], str)
                    or type(pointer["version"]) is not int):
                raise ContextError("Saved note result needs an exact working note")
            saved = self.store.get(**pointer)
            if saved is None or any(saved["body"].get(key) != expected for key, expected in (
                ("mission_id", mission_id), ("actor_id", actor_id), ("task_id", task_id),
            )):
                raise ContextError("Saved note result belongs to another peer task")
            checked = self.working_note_projection(saved)
            # Reauthorization cannot add text absent from the original read.
            if "text" not in item.get("body", {}):
                checked = {**checked, "body": {key: value for key, value in checked["body"].items()
                                              if key != "text"}}
            elif item["body"]["text"] != saved["body"].get("text"):
                raise ContextError("Saved note text differs from its exact record")
            fields = {
                "schema", "mission_id", "actor_id", "task_id", "task_ref", "category", "text",
                "input_refs", "status", "updated_at", "source_dependency_refs",
                "unavailable_source_refs", "dependency_lineage_complete", "next_action",
            }
            projected = {key: checked[key] for key in ("kind", "id", "version", "created_at")}
            projected["body"] = {key: value for key, value in checked["body"].items() if key in fields}
            notes.append(projected)
            return projected

        def pointer_projection(item: object, kind: str) -> dict:
            if (not isinstance(item, dict) or set(item) != {"kind", "id", "version"}
                    or item.get("kind") != kind or not isinstance(item.get("id"), str)
                    or type(item.get("version")) is not int or item["version"] < 1):
                raise ContextError("Saved board result has an invalid exact reference")
            return item

        def optional_projection(items: object) -> list[dict]:
            if not isinstance(items, list):
                raise ContextError("Saved optional work result needs a list")
            output = []
            for item in items:
                if not isinstance(item, dict):
                    raise ContextError("Saved optional work item is malformed")
                entry = {key: pointer_projection(item.get(key), kind) for key, kind in (
                    ("control_ref", "mission_task_control"), ("proposal_ref", "board_proposal"),
                )}
                for key in ("actor_id", "control_type", "scope_status", "phase"):
                    if not isinstance(item.get(key), str):
                        raise ContextError("Saved optional work metadata is malformed")
                    entry[key] = item[key]
                output.append(entry)
            return output

        if name == "read_plan":
            # Plan annotations contain only host status and exact references;
            # all agent-authored text lives in the permission-checked note.
            projection = dict(content)
            if isinstance(content.get("note"), dict):
                projection["note"] = note_projection(content["note"])
                if "text" not in projection["note"]["body"]:
                    projection["status"] = "data_unavailable_due_source_use"
        elif name == "read_note":
            projection = {"status": "found", "note": note_projection(content.get("note"))}
        elif name == "search_notes":
            if not isinstance(content.get("notes"), list):
                raise ContextError("Saved note search needs a list")
            projection = {"notes": [note_projection(item) for item in content["notes"]]}
        else:
            projection = {"optional_work": optional_projection(content.get("optional_work"))}
            if name == "board_read":
                if not isinstance(content.get("open_work"), list):
                    raise ContextError("Saved board result needs an open work list")
                projection["open_work"] = [pointer_projection(item, "board_proposal")
                                           for item in content["open_work"]]
        return result_window(
            projection, result_ref=ref(result),
            original_content_sha256=hashlib.sha256(payload["content"].encode()).hexdigest(),
            offset=offset, limit=limit, projection_sha256=projection_sha256, note_records=notes,
        )

    def response_source_dependencies(self, response_ref: dict, *, require_permitted: bool = True) -> tuple[dict, ...]:
        """Bind model-authored text and arguments to their exact producing input."""
        response = self.store.get(**response_ref)
        call = self.store.get(**response["body"]["call_ref"]) if response else None
        intent = self.store.get(
            "model_request_intent", f"model-intent:{call['id']}:{call['version']}"
        ) if call else None
        if intent is None or intent["body"].get("call_ref") != ref(call):
            raise ContextError("Model-authored text lost its producing input lineage")
        dependencies = {}
        for field in ("source_refs", "context_source_dependency_refs"):
            pointers = intent["body"].get(field)
            if not isinstance(pointers, list):
                raise ContextError("Model-authored text has incomplete source lineage")
            for pointer in pointers:
                copy = self.store.get(**pointer) if isinstance(pointer, dict) else None
                if copy is None or copy["kind"] != "source_copy":
                    raise ContextError("Model-authored text has an invalid source dependency")
                if require_permitted and self._source_status(copy)["model_input_status"] != "permitted":
                    raise ContextError("Model-authored text source use changed")
                dependencies[(pointer["kind"], pointer["id"], pointer["version"])] = pointer
        return tuple(dependencies.values())

    def browser_search_result(self, *, mission_id: str, actor_id: str, task_id: str,
                              response_id: str, offset: int = 0, limit: int = 1800,
                              content_sha256: str = "") -> dict:
        """Read discovery output only in its original task and source-use scope."""
        response = self.store.get("model_response_receipt", response_id, 1)
        call = self.store.get(**response["body"]["call_ref"]) if response else None
        if (call is None or any(call["body"].get(key) != value for key, value in (
                ("mission_id", mission_id), ("instance_id", actor_id), ("task_id", task_id)))):
            raise ContextError("Browser result is outside this peer's task")
        dependencies = self.response_source_dependencies(ref(response))
        result = project_browser_search(self.store, ref(response), offset=offset, limit=limit)
        if (offset and not content_sha256) or (content_sha256 and content_sha256 != result["content_sha256"]):
            raise ContextError("Browser result continuation needs its exact content hash")
        result["source_dependency_refs"] = list(dependencies)
        # Only exact URL leads retained by the host can be passed to fetch_source.
        saved_leads = self.store.get("browser_search_leads", f"browser-leads:{response_id}", 1)
        if saved_leads and any(saved_leads["body"].get(key) != value for key, value in (
                ("response_ref", ref(response)), ("mission_id", mission_id),
                ("task_id", task_id), ("actor_id", actor_id))):
            raise ContextError("Browser URL leads differ from their response scope")
        hit_refs = saved_leads["body"]["hit_refs"] if saved_leads else []
        result["hits"] = []
        for pointer in hit_refs:
            hit = self.store.get(**pointer)
            if hit is None or ref(hit) != pointer or self.browser_hit_response(
                    hit, mission_id=mission_id, actor_id=actor_id, task_id=task_id) != ref(response):
                raise ContextError("Browser URL lead lost its exact response provenance")
            result["hits"].append({
                "hit_id": hit["id"], "original_url": hit["body"]["source"]["original_url"],
                "access_surface": "metadata", "source_inspection": "not_performed",
            })
        return result

    def browser_hit_response(self, hit: dict, *, mission_id: str, actor_id: str, task_id: str) -> dict:
        """Resolve and reauthorize the response from which a URL lead came."""
        body = hit["body"]
        if any(body.get(key) != value for key, value in (
                ("mission_id", mission_id), ("agent_instance_id", actor_id),
                ("task_id", task_id), ("route", "groq.browser_search.url_leads"))):
            raise ContextError("Browser URL lead is outside this peer's task")
        delivery = self.store.get(**body["delivery_ref"])
        pointer = delivery["body"]["envelope"]["request"]["filters"].get("model_response_receipt_ref") if delivery else None
        if not isinstance(pointer, dict):
            raise ContextError("Browser URL lead lost its originating response")
        self.response_source_dependencies(pointer)
        return pointer

    def _tool_exchange(self, response_ref: dict, *, scope: dict | None = None,
                       active_skill_ids: set[str] | None = None) -> tuple[list[dict], dict | None]:
        response = self.store.get(**response_ref)
        if (
            response is None
            or response["kind"] != "model_response_receipt"
            or response["body"].get("status") != "tool_requested"
        ):
            raise ContextError("Tool continuation needs a saved model tool request")
        private = json.loads(self.store.read_blob(response["body"]["blob"]["sha256"]))
        origin_ref = response["body"].get("call_ref")
        origin_call = self.store.get(**origin_ref) if isinstance(origin_ref, dict) else None
        if scope and (origin_call is None or any(origin_call["body"].get(key) != scope[value]
                for key, value in (("mission_id", "mission_id"), ("instance_id", "actor_id"), ("task_id", "task_id")))):
            raise ContextError("Tool continuation is outside this peer's task")
        calls = private.get("tool_calls")
        if not isinstance(calls, list) or len(calls) != 1:
            raise ContextError("Selected adapter needs one saved sequential tool call")
        assistant = {"role": "assistant", "content": private.get("content"), "tool_calls": calls}
        # The saved restricted reasoning is not part of the tool-call/result
        # protocol. Retain its exact record for trusted diagnosis, but do not
        # repeat private reasoning in the next model request or make a missing
        # reasoning blob block an otherwise reconciled tool continuation.
        result = self.store.get("context_tool_result", f"tool-result:{response['id']}")
        if result is None or result["body"].get("response_ref") != response_ref:
            raise ContextError(
                f"Tool result for {calls[0]['id']} is pending; reconcile before continuation"
            )
        payload = json.loads(self.store.read_blob(result["body"]["blob"]["sha256"]))
        if payload.get("tool_call_id") != calls[0]["id"]:
            raise ContextError("Saved tool result no longer matches its call")
        if payload.get("name") == "read_plan" and assistant["content"] is not None:
            # A read's numeric arguments are safe to replay, but accompanying
            # model prose may derive from sources visible in its original turn.
            # Note projection alone cannot remove that separate assistant text.
            dependencies = self.response_source_dependencies(response_ref, require_permitted=False)
            if any(
                (copy := self.store.get(**pointer)) is None
                or self._source_status(copy)["model_input_status"] != "permitted"
                for pointer in dependencies
            ):
                assistant["content"] = None
        if (payload.get("name") in {
                "calculate", "search_saved_passages", "read_browser_search_result",
                "search_scholarly", "save_plan",
            }
                or origin_call and "browser_search" in origin_call["body"].get("exposed_tools", [])):
            dependencies = list(self.response_source_dependencies(response_ref, require_permitted=False))
            saved_content = json.loads(payload["content"])
            dependencies.extend(saved_content.get("source_dependency_refs", []))
            if any(
                not isinstance(pointer, dict)
                or (copy := self.store.get(**pointer)) is None
                or self._source_status(copy)["model_input_status"] != "permitted"
                for pointer in dependencies
            ):
                safe_call = {**calls[0], "function": {
                    **calls[0]["function"], "arguments": "{}",
                }}
                return [
                    {"role": "assistant", "content": None, "tool_calls": [safe_call]},
                    {"role": "tool", "tool_call_id": calls[0]["id"], "name": payload["name"],
                     "content": canonical({"status": "data_unavailable_due_source_use"})},
                ], {"status": "tool_argument_withheld_due_source_use", "saved_tool_result_ref": ref(result)}
        if payload.get("name") == "read_skill_resource" and active_skill_ids is not None:
            saved_content = json.loads(payload["content"])
            if (saved_content.get("schema") == "ora.skill_resource_page.v1"
                    and saved_content.get("skill_id") not in active_skill_ids):
                payload = {**payload, "content": canonical({"status": "skill_no_longer_active"})}
        if payload.get("name") == "read_source_window":
            try:
                window_result = json.loads(payload["content"])
            except (KeyError, TypeError, ValueError):
                window_result = None
            copy_ref = window_result.get("copy_ref") if isinstance(window_result, dict) else None
            if copy_ref:
                copy = self.store.get(**copy_ref) if isinstance(copy_ref, dict) else None
                if copy is None or self._source_status(copy)["model_input_status"] != "permitted":
                    payload = {**payload, "content": canonical({
                        "status": "data_unavailable_due_source_use",
                        "saved_tool_result_ref": ref(result),
                    })}
        if payload.get("name") == "inspect_hit":
            try:
                inspected = json.loads(payload["content"])
            except (KeyError, TypeError, ValueError):
                inspected = None
            if isinstance(inspected, dict) and inspected.get("status") == "inspected":
                copy_ref = {
                    "kind": "source_copy", "id": inspected.get("source_id"),
                    "version": inspected.get("source_version"),
                }
                copy = self.store.get(**copy_ref) if (
                    isinstance(copy_ref["id"], str)
                    and type(copy_ref["version"]) is int
                ) else None
                if copy is None or self._source_status(copy)["model_input_status"] != "permitted":
                    payload = {**payload, "content": canonical({
                        "status": "data_unavailable_due_source_use",
                        "saved_tool_result_ref": ref(result),
                    })}
        if payload.get("name") == "compact_conversation":
            saved_call = self.store.get(**response["body"]["call_ref"])
            intent = self.store.get(
                "model_request_intent",
                f"model-intent:{saved_call['id']}:{saved_call['version']}"
            ) if saved_call else None
            if intent is None or intent["body"].get("call_ref") != ref(saved_call):
                raise ContextError("Compaction replay input lineage is incomplete")
            dependencies = []
            for field in ("source_refs", "context_source_dependency_refs"):
                pointers = intent["body"].get(field)
                if not isinstance(pointers, list):
                    raise ContextError("Compaction replay source lineage is incomplete")
                dependencies.extend(pointers)
            if any(
                not isinstance(pointer, dict)
                or (copy := self.store.get(**pointer)) is None
                or self._source_status(copy)["model_input_status"] != "permitted"
                for pointer in dependencies
            ):
                safe_call = {**calls[0], "function": {
                    **calls[0]["function"],
                    "arguments": canonical({"summary": "Unavailable due to current source use"}),
                }}
                return [
                    {"role": "assistant", "content": None, "tool_calls": [safe_call]},
                    {"role": "tool", "tool_call_id": calls[0]["id"],
                     "name": "compact_conversation",
                     "content": canonical({"status": "data_unavailable_due_source_use"})},
                ], {"status": "compaction_argument_withheld_due_source_use",
                    "saved_tool_result_ref": ref(result)}
        original_content = payload.get("content")
        paging_rechecked = False
        if payload.get("name") == "read_conversation_history" and isinstance(original_content, str):
            from .conversation import read_history

            try:
                saved_page = json.loads(original_content)
            except ValueError:
                saved_page = None
            if isinstance(saved_page, dict) and saved_page.get("schema") == "ora.conversation_history_page.v1":
                saved_call = self.store.get(**response["body"]["call_ref"])
                if saved_call is None:
                    raise ContextError("Conversation page replay lost its originating call")
                arguments = json.loads(calls[0]["function"]["arguments"])
                scope_now = scope or {
                    "mission_id": response["body"]["mission_id"],
                    "actor_id": saved_call["body"]["instance_id"],
                    "task_id": saved_call["body"]["task_id"],
                }
                checked = read_history(self, **scope_now, **arguments)
                payload = {**payload, "content": canonical(checked)}
                paging_rechecked = True
        if payload.get("name") in PAGEABLE_TOOLS | {"read_tool_result"}:
            saved_call = self.store.get(**response["body"]["call_ref"])
            if saved_call is None:
                raise ContextError("Saved result replay needs its originating call")
            result_scope = scope or {
                "mission_id": response["body"]["mission_id"],
                "actor_id": saved_call["body"]["instance_id"],
                "task_id": saved_call["body"]["task_id"],
            }
            self._saved_result_scope(result["id"], **result_scope)
            if payload["name"] == "read_tool_result":
                arguments = json.loads(calls[0]["function"]["arguments"])
                # Rejected reads contain no authorized window to replay.
                saved_window = json.loads(original_content)
                if isinstance(saved_window, dict) and saved_window.get("schema") == RESULT_PAGE_SCHEMA:
                    try:
                        checked = self.read_tool_result(**result_scope, **arguments)
                    except ResultProjectionChanged:
                        checked = {
                            "schema": RESULT_PAGE_SCHEMA, "status": "projection_changed_restart_required",
                            "saved_tool_result_ref": saved_window["saved_tool_result_ref"],
                            "next_request": {"tool": "read_tool_result", **arguments,
                                             "offset": 0, "projection_sha256": ""},
                        }
                    payload = {**payload, "content": canonical(checked)}
                    paging_rechecked = True
        report_rechecked = False
        try:
            original_window = json.loads(original_content)
        except (TypeError, ValueError):
            original_window = None
        if (
            payload.get("name") == "read_context_record"
            and isinstance(original_window, dict)
            and original_window.get("schema") == "ora.context_record_window.v1"
            and isinstance(original_window.get("ref"), dict)
            and original_window["ref"].get("kind") in {"report", "retrieval_hit"}
        ):
            arguments = json.loads(calls[0]["function"]["arguments"])
            if arguments.get("kind") in {"report", "retrieval_hit"}:
                # A saved successful report window can outlive a source-use
                # permission. Recheck its exact original scope on every replay;
                # retain the original tool result as historical evidence.
                saved_call = self.store.get(**response["body"]["call_ref"])
                if saved_call is None:
                    raise ContextError("Report replay needs its exact originating call")
                read_arguments = dict(
                    mission_id=response["body"]["mission_id"],
                    actor_id=saved_call["body"]["instance_id"],
                    task_id=saved_call["body"]["task_id"],
                    inclusion_ref={"kind": "context_inclusion",
                                   "id": arguments["inclusion_id"], "version": 1},
                    pointer={key: arguments[key] for key in ("kind", "id", "version")},
                    limit=arguments["limit"],
                )
                original_inclusion = self.store.get(**read_arguments["inclusion_ref"])
                shared_window = bool(original_inclusion and any(
                    item.get("type") == "shared_material"
                    and item.get("fetch") == read_arguments["pointer"]
                    for item in original_inclusion["body"].get("omitted", [])
                ))
                try:
                    checked = self.read_omitted_record(**read_arguments, offset=0)
                    if (checked.get("report_text_status") == "available"
                            or arguments.get("kind") == "retrieval_hit") and arguments["offset"]:
                        checked = self.read_omitted_record(
                            **read_arguments, offset=arguments["offset"])
                except ContextError:
                    if not shared_window:
                        raise
                    checked = {
                        "status": "shared_material_unavailable",
                        "ref": read_arguments["pointer"],
                    }
                payload = {**payload, "content": canonical(checked)}
                report_rechecked = True
        if (
            payload.get("name") == "read_context_page"
            and (self._visible_report_page_items(original_window)
                 or self._shared_page_items(original_window))
        ):
            try:
                arguments = json.loads(calls[0]["function"]["arguments"])
            except (KeyError, TypeError, ValueError) as exc:
                raise ContextError("Saved report page has invalid read arguments") from exc
            if (not isinstance(arguments, dict)
                or set(arguments) != {"inclusion_id", "cursor", "limit"}
                or original_window.get("inclusion_ref") != {
                    "kind": "context_inclusion", "id": arguments["inclusion_id"],
                    "version": 1,
                }):
                raise ContextError("Saved report page differs from its exact tool request")
            saved_call = self.store.get(**response["body"]["call_ref"])
            if saved_call is None:
                raise ContextError("Report page replay needs its exact originating call")
            checked = self.read_omitted_page(
                mission_id=response["body"]["mission_id"],
                actor_id=saved_call["body"]["instance_id"],
                task_id=saved_call["body"]["task_id"],
                inclusion_ref=original_window["inclusion_ref"],
                cursor=arguments["cursor"], limit=arguments["limit"],
            )
            payload = {**payload, "content": canonical(checked)}
            report_rechecked = True
        payload = self._checked_note_tool_payload(payload, ref(result))
        note_continuation_rechecked = False
        if (
            payload.get("name") == "read_context_record"
            and payload.get("content") == original_content
            and isinstance(original_window, dict)
            and original_window.get("schema") == "ora.context_record_window.v1"
            and isinstance(original_window.get("ref"), dict)
            and original_window["ref"].get("kind") == "working_note"
        ):
            try:
                arguments = json.loads(calls[0]["function"]["arguments"])
                if (
                    not isinstance(arguments, dict)
                    or set(arguments) != {
                        "inclusion_id", "kind", "id", "version", "offset", "limit"
                    }
                    or type(arguments["inclusion_id"]) is not str
                    or type(arguments["id"]) is not str
                    or type(arguments["version"]) is not int
                    or type(arguments["offset"]) is not int
                    or type(arguments["limit"]) is not int
                    or original_window["ref"] != {
                        key: arguments[key] for key in ("kind", "id", "version")
                    }
                ):
                    raise ValueError("invalid saved note read")
            except (KeyError, TypeError, ValueError) as exc:
                raise ContextError("Saved working-note read has invalid arguments") from exc
            saved_call = self.store.get(**response["body"]["call_ref"])
            if saved_call is None:
                raise ContextError("Working-note replay needs its exact originating call")
            read_scope = scope or {
                "mission_id": response["body"]["mission_id"],
                "actor_id": saved_call["body"]["instance_id"],
                "task_id": saved_call["body"]["task_id"],
            }
            checked = self.read_omitted_record(
                **read_scope,
                inclusion_ref={"kind": "context_inclusion",
                               "id": arguments["inclusion_id"], "version": 1},
                pointer=original_window["ref"], offset=arguments["offset"],
                limit=arguments["limit"],
            )
            comparable = (
                checked if "next_request" in original_window else
                {key: value for key, value in checked.items() if key != "next_request"}
            )
            if original_window != comparable:
                raise ContextError("Saved working-note window differs from its current exact read")
            if checked != original_window:
                payload = {**payload, "content": canonical(checked)}
                note_continuation_rechecked = True
        if payload.get("name") in PAGEABLE_TOOLS and len(payload.get("content", "")) > MAX_RESULT_PAGE_CHARS:
            window = self.read_tool_result(
                **result_scope, result_id=result["id"], offset=0, limit=1000, projection_sha256="",
            )
            payload = {**payload, "content": canonical(window)}
            paging_rechecked = True
        compaction = ({
            "saved_tool_result_ref": ref(result),
            "original_content_sha256": hashlib.sha256(original_content.encode()).hexdigest(),
            "status": (
                "saved_result_window_rechecked" if paging_rechecked
                else "report_current_use_rechecked" if report_rechecked
                else "working_note_continuation_rechecked" if note_continuation_rechecked
                else "working_note_current_use_rechecked"
            ),
        } if isinstance(original_content, str) and payload.get("content") != original_content else None)
        if (
            payload.get("name") == "read_context_page"
            and len(payload.get("content", "")) > MAX_CONTEXT_PAGE_CHARS
        ):
            try:
                page = json.loads(payload["content"])
                items = page["items"]
                prior_cursor = page["cursor"]
                inclusion_id = page["inclusion_ref"]["id"]
                if (
                    not isinstance(items, list)
                    or type(prior_cursor) is not int
                    or not isinstance(inclusion_id, str)
                ):
                    raise ValueError("invalid saved page")
            except (KeyError, TypeError, ValueError) as exc:
                raise ContextError("Saved page result cannot be compacted safely") from exc
            compact = {
                "schema": "ora.context_page_tool_result.v1",
                "status": "saved_result_compacted_for_model_input",
                "saved_tool_result_ref": ref(result),
                "original_content_sha256": hashlib.sha256(payload["content"].encode()).hexdigest(),
                "inclusion_id": inclusion_id,
                "original_cursor": prior_cursor,
                "original_item_count": len(items),
                "included_items": [],
                "omitted_item_count": len(items),
                "next_cursor": prior_cursor,
            }
            for item in items:
                candidate = {
                    **compact,
                    "included_items": [*compact["included_items"], item],
                    "omitted_item_count": len(items) - len(compact["included_items"]) - 1,
                    "next_cursor": prior_cursor + len(compact["included_items"]) + 1,
                }
                if len(canonical(candidate)) > MAX_CONTEXT_PAGE_CHARS:
                    break
                compact = candidate
            included_count = len(compact["included_items"])
            if included_count == len(items):
                compact["next_cursor"] = page.get("next_cursor")
            payload = {**payload, "content": canonical(compact)}
            compaction = {
                **(compaction or {}),
                "saved_tool_result_ref": ref(result),
                "original_content_sha256": compact["original_content_sha256"],
                "original_item_count": len(items),
                "included_item_refs": [item["ref"] for item in items[:included_count]],
                "omitted_item_refs": [item["ref"] for item in items[included_count:]],
                "continuation_cursor": compact["next_cursor"],
                "inclusion_id": inclusion_id,
            }
        return [assistant, payload], compaction

    def _retained_context_window(
        self, response_ref: dict, *, scope: dict,
    ) -> tuple[list[dict], dict] | None:
        """Recheck one already selected report or working-note window.

        The exact assistant/result pair retains its original read coordinates.
        The ordinary replay path checks current source use and this peer's task;
        the caller decides whether this optional pair fits the input.
        """
        response = self.store.get(**response_ref)
        if response is None:
            raise ContextError("Saved context window response is unavailable")
        private = json.loads(self.store.read_blob(response["body"]["blob"]["sha256"]))
        calls = private.get("tool_calls") or []
        if len(calls) != 1 or calls[0].get("function", {}).get("name") != "read_context_record":
            return None
        try:
            args = json.loads(calls[0]["function"]["arguments"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ContextError("Saved report continuation has invalid read arguments") from exc
        if not isinstance(args, dict) or args.get("kind") not in {"report", "working_note"}:
            return None
        note_window = args["kind"] == "working_note"
        label = "working note" if note_window else "report"
        result = self.store.get("context_tool_result", f"tool-result:{response['id']}")
        if result is None:
            raise ContextError(f"Saved {label} window result is unavailable")
        payload = json.loads(self.store.read_blob(result["body"]["blob"]["sha256"]))
        try:
            window = json.loads(payload["content"])
        except (KeyError, TypeError, ValueError):
            return None  # A saved rejection is not a selected report window.
        if (not isinstance(window, dict)
                or window.get("schema") != "ora.context_record_window.v1"
                or not isinstance(window.get("ref"), dict)
                or window["ref"].get("kind") != args["kind"]
                or not note_window and window.get("report_text_status") != "available"):
            return None
        if (
            payload.get("tool_call_id") != calls[0]["id"]
            or payload.get("name") != "read_context_record"
            or window.get("ref") != {key: args.get(key) for key in ("kind", "id", "version")}
            or window.get("offset") != args.get("offset")
            or not isinstance(window.get("projection_sha256"), str)
            or not isinstance(window.get("text"), str)
            or window.get("returned_chars") != len(window["text"])
        ):
            raise ContextError(f"Saved {label} window differs from its exact tool request")
        exchange, _ = self._tool_exchange(response_ref, scope=scope)
        checked = json.loads(exchange[-1]["content"])
        if not isinstance(checked, dict):
            raise ContextError(f"Saved {label} window has invalid checked content")
        receipt = {
            "kind": "retained_working_note_window" if note_window else "retained_report_window",
            "response_ref": response_ref,
            "saved_tool_result_ref": ref(result),
            "note_ref" if note_window else "report_ref": window["ref"],
            "projection_sha256": window["projection_sha256"],
            "offset": window["offset"],
            "returned_chars": window["returned_chars"],
            "original_content_sha256": hashlib.sha256(payload["content"].encode()).hexdigest(),
            "status": "eligible_for_input",
        }
        if (checked.get("status") == "data_unavailable_due_source_use" if note_window
                else checked.get("report_text_status") != "available"):
            return [], {**receipt, "status": "omitted_due_source_use"}
        if any(checked.get(key) != window.get(key) for key in (
            "ref", "projection_sha256", "offset", "returned_chars", "text",
        )):
            raise ContextError(f"Saved {label} window changed before continuation")
        return exchange, receipt

    def _compact_selected_window_preview(
        self, response_ref: dict, payload: dict, *, call: dict, control: dict,
        selected_source_refs: tuple[dict, ...],
    ) -> tuple[dict, dict] | None:
        """Avoid repeating a checked window already selected as source input.

        The saved result remains exact. If the current call's source selection
        cannot independently supply every preview byte, keep the original tool
        result and let the ordinary context limit apply.
        """
        if payload.get("name") != "read_source_window":
            return None
        try:
            result = self.store.get("context_tool_result", f"tool-result:{response_ref['id']}")
            content = payload["content"]
            window_result = json.loads(content)
            copy_ref = window_result["copy_ref"]
            window_ref = window_result["window_ref"]
            passages = window_result["passages"]
            window_passages = window_result["passage_refs"]
            selected_passages = window_result["model_selection"]["passage_refs"]
            saved_copy = self.store.get(**copy_ref)
            saved_window = self.store.get(**window_ref)
            saved_quality = (
                self.store.get(**saved_window["body"]["quality_ref"])
                if saved_window and isinstance(saved_window["body"].get("quality_ref"), dict)
                else None
            )
            call_sources = [
                {key: item[key] for key in ("kind", "id", "version")}
                for item in call["body"].get("input_refs", [])
                if item.get("kind") == "source_copy"
            ]
            if (
                not result or result["body"].get("response_ref") != response_ref
                or not isinstance(content, str)
                or not isinstance(window_result, dict)
                or copy_ref not in selected_source_refs or copy_ref not in call_sources
                or saved_copy is None or saved_window is None
                or saved_copy["kind"] != "source_copy"
                or saved_window["kind"] != "source_read_window"
                or saved_window["body"].get("copy_ref") != copy_ref
                or saved_window["body"].get("passage_refs") != window_passages
                or saved_window["body"].get("quality_ref") != window_result.get("quality_ref")
                or saved_window["body"].get("source_use_ref") != window_result.get("source_use_ref")
                or saved_quality is None or saved_quality["kind"] != "intake_quality"
                or window_result.get("source_id", copy_ref["id"]) != copy_ref["id"]
                or not isinstance(passages, list) or not passages
                or not isinstance(selected_passages, list)
                or window_result.get("omissions") is not None
                and not isinstance(window_result["omissions"], list)
                or window_result.get("source_selection_omissions") is not None
                and not isinstance(window_result["source_selection_omissions"], list)
                or control["body"].get("selected_passage_refs", {}).get(copy_ref["id"])
                != selected_passages
                or any(pointer not in selected_passages for pointer in window_passages)
                or self._source_status(saved_copy)["model_input_status"] != "permitted"
            ):
                return None
            omitted_chars = 0
            for item in passages:
                pointer = item["passage_ref"]
                saved_passage = self.store.get(**pointer)
                preview = item["text_preview"]
                if (
                    not isinstance(preview, str) or not preview
                    or pointer not in window_passages
                    or saved_passage is None or saved_passage["kind"] != "source_passage"
                    or saved_passage["body"].get("copy_ref") != copy_ref
                    or saved_passage["body"].get("text") != preview
                    or saved_passage["body"].get("span_id") != item.get("span_id")
                    or saved_passage["body"].get("locator") != item.get("locator")
                ):
                    return None
                omitted_chars += len(preview)
            coverage = extracted_text_coverage(
                self.store, copy_ref, selected_passages, [window_ref]
            )
            if coverage["status"] == "unknown":
                return None
            remaining = window_result.get("remaining_in_block_chars")
            if remaining is None:
                remaining = next(
                    (item.get("characters") for item in window_result.get("omissions", [])
                     if isinstance(item, dict)
                     and item.get("reason") == "remaining_characters_in_block"),
                    None,
                )
            compacted = {
                "schema": "ora.selected_checked_window_tool_result.v1",
                "status": "saved_window_text_in_selected_source_packet",
                "saved_tool_result_ref": ref(result),
                "copy_ref": copy_ref,
                "source_id": copy_ref["id"],
                "source_version": copy_ref["version"],
                "window_ref": window_ref,
                "passages": [
                    {key: value for key, value in item.items() if key != "text_preview"}
                    for item in passages
                ],
                "model_selection_mode": window_result["model_selection"].get("mode"),
                "selected_passage_count": len(selected_passages),
                "returned_chars": window_result.get("returned_chars", omitted_chars),
                "next_cursor": window_result["next_cursor"],
                "next_request": window_result["next_request"],
                "remaining_in_block_chars": remaining,
                "total_blocks": window_result.get(
                    "total_blocks", saved_window["body"].get("total_blocks")
                ),
                "original_source_partial": coverage["original_source_partial"],
                "source_surface": coverage["source_surface"],
                "saved_omissions_count": (
                    len(window_result["omissions"])
                    if window_result.get("omissions") is not None else None
                ),
                "next_cursor_scope": "retained_extracted_text_only",
                "source_text_status": "exact_checked_passages_in_this_model_input",
            }
            if window_result.get("source_selection_omissions"):
                compacted["source_selection_omissions_count"] = len(
                    window_result["source_selection_omissions"]
                )
            if saved_copy["body"].get("mime_type") == "application/pdf":
                reviewed_table = saved_quality["body"].get("table_check")
                selected_pages = {
                    int(match.group(1)) for item in passages
                    if (match := re.match(r"page=(\d+)\b", item["locator"]))
                }
                current_page_check = (
                    reviewed_table if isinstance(reviewed_table, dict)
                    and reviewed_table.get("page") in selected_pages else None
                )
                expected_table_context = {
                    "rendered_original_check": current_page_check,
                    "reviewed_page_elsewhere": (
                        reviewed_table.get("page") if isinstance(reviewed_table, dict)
                        and current_page_check is None else None
                    ),
                    "caution": (
                        "PDF layout text alone does not validate a table cell; inspect "
                        "the rendered original page and its header, units, denominator and footnote."
                    ),
                }
                if window_result.get("table_context") != expected_table_context:
                    return None
                compacted["table_context"] = expected_table_context
            elif window_result.get("table_context") is not None:
                return None
            receipt = {
                "kind": "selected_checked_source_window_preview",
                "saved_tool_result_ref": ref(result),
                "original_content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                "copy_ref": copy_ref,
                "window_ref": window_ref,
                "passage_refs": window_passages,
                "omitted_preview_characters": omitted_chars,
                "omitted_item_refs": [],
            }
            return {**payload, "content": canonical(compacted)}, receipt
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            return None

    def _compact_selected_inspection_preview(
        self, response_ref: dict, payload: dict, *, call: dict, control: dict,
        selected_source_refs: tuple[dict, ...],
    ) -> tuple[dict, dict] | None:
        """Keep inspect_hit navigation while its exact passage enters a source packet."""
        if payload.get("name") != "inspect_hit":
            return None
        try:
            result = self.store.get("context_tool_result", f"tool-result:{response_ref['id']}")
            original = payload["content"]
            inspected = json.loads(original)
            preview = inspected["preview"]
            preview_text = preview["text"]
            copy_ref = {
                "kind": "source_copy", "id": inspected["source_id"],
                "version": inspected["source_version"],
            }
            copy = self.store.get(**copy_ref)
            call_sources = [
                {key: item[key] for key in ("kind", "id", "version")}
                for item in call["body"].get("input_refs", [])
                if isinstance(item, dict) and item.get("kind") == "source_copy"
            ]
            if (
                result is None or result["body"].get("response_ref") != response_ref
                or result["body"].get("effect_ref") != copy_ref
                or result["body"].get("message_sha256")
                != _sha(payload)
                or inspected.get("status") != "inspected"
                or not isinstance(preview_text, str) or not preview_text
                or copy_ref not in selected_source_refs or copy_ref not in call_sources
                or copy_ref not in control["body"].get("source_refs", [])
                or copy is None or copy["kind"] != "source_copy"
                or self._source_status(copy)["model_input_status"] != "permitted"
            ):
                return None
            selected = control["body"].get("selected_passage_refs", {}).get(copy_ref["id"])
            if selected is None:
                selected = [
                    ref(self.store.get("source_passage", passage_id))
                    for passage_id in copy["body"].get("passage_ids", [])
                ]
            if not isinstance(selected, list) or not selected:
                return None
            from ora.evidence import EvidenceError, EvidenceLibrary
            from ora.evidence.intake import checked_model_text

            try:
                checked = checked_model_text(EvidenceLibrary(self.store), copy_ref, selected)
            except EvidenceError:
                return None
            if (
                inspected.get("source_url") != copy["body"].get("reference")
                or inspected.get("classification") != checked["classification"]
            ):
                return None
            matched = [
                entry for entry in checked["passages"]
                if entry["text"] == preview_text
                and entry["locator"].startswith(f"{preview['locator']}; chars=")
                and any(
                    item.get("span_id") == self.store.get(**entry["passage_ref"])["body"]["span_id"]
                    and item.get("locator") == entry["locator"]
                    for item in inspected.get("passages", [])
                )
            ]
            if len(matched) != 1 or preview_text not in checked["text"]:
                return None
            projected = {
                **inspected,
                "preview": {
                    **{key: value for key, value in preview.items() if key != "text"},
                    "text_status": "exact_checked_passage_in_selected_source_packet",
                    "text_sha256": hashlib.sha256(preview_text.encode()).hexdigest(),
                },
                "saved_tool_result_ref": ref(result),
            }
            receipt = {
                "kind": "selected_checked_inspection_preview",
                "saved_tool_result_ref": ref(result),
                "original_content_sha256": hashlib.sha256(original.encode()).hexdigest(),
                "copy_ref": copy_ref,
                "passage_refs": [matched[0]["passage_ref"]],
                "omitted_preview_characters": len(preview_text),
                "omitted_item_refs": [],
            }
            return {**payload, "content": canonical(projected)}, receipt
        except (KeyError, TypeError, ValueError, AttributeError, json.JSONDecodeError):
            return None

    def checked_window_is_selected_input(
        self, response_ref: dict, *, call: dict, control: dict,
        selected_source_refs: tuple[dict, ...],
    ) -> bool:
        """Check whether an exact saved window is already in this call's packets."""
        try:
            exchange, _ = self._tool_exchange(response_ref)
            return self._compact_selected_window_preview(
                response_ref, exchange[1], call=call, control=control,
                selected_source_refs=selected_source_refs,
            ) is not None
        except (ContextError, KeyError, TypeError, ValueError):
            return False

    def _archived_tool_observation(self, response_ref: dict) -> dict:
        """Project an exact completed turn without its result text or reasoning."""
        response = self.store.get(**response_ref)
        result = self.store.get("context_tool_result", f"tool-result:{response['id']}")
        private = json.loads(self.store.read_blob(response["body"]["blob"]["sha256"]))
        calls = private.get("tool_calls") or []
        tool = calls[0] if len(calls) == 1 else {}
        function = tool.get("function") or {}
        name = function.get("name")
        observation = {
            "response_ref": response_ref,
            "tool_call_id": tool.get("id"),
            "tool_name": name,
            "result_ref": ref(result),
            "effect_ref": result["body"].get("effect_ref"),
        }
        if name == "read_source_window":
            try:
                result_message = json.loads(self.store.read_blob(result["body"]["blob"]["sha256"]))
                window = json.loads(result_message["content"])
                window_ref = window["window_ref"]
                copy_ref = window["copy_ref"]
                saved_window = self.store.get(**window_ref)
                if (
                    result_message.get("tool_call_id") != tool["id"]
                    or result_message.get("name") != name
                    or saved_window is None
                    or saved_window["body"].get("copy_ref") != copy_ref
                    or saved_window["body"].get("passage_refs") != window["passage_refs"]
                ):
                    raise ValueError("window result mismatch")
                returned_chars = 0
                for pointer in window["passage_refs"]:
                    passage = self.store.get(**pointer)
                    if (passage is None or passage["body"].get("copy_ref") != copy_ref
                            or not isinstance(passage["body"].get("text"), str)):
                        raise ValueError("window passage unavailable")
                    returned_chars += len(passage["body"]["text"])
                observation["window"] = {
                    "copy_ref": copy_ref,
                    "window_ref": window_ref,
                    "passage_refs": window["passage_refs"],
                    "returned_chars": returned_chars,
                    "next_cursor": window["next_cursor"],
                    "status": "saved_result_metadata_not_source_text",
                }
            except (KeyError, TypeError, ValueError):
                observation["window_status"] = "saved_result_has_no_safe_window_projection"
            return observation
        if name == "inspect_hit":
            try:
                arguments = json.loads(function["arguments"])
                result_message = json.loads(self.store.read_blob(result["body"]["blob"]["sha256"]))
                outcome = json.loads(result_message["content"])
                hit_id = arguments.get("hit_id")
                status = outcome.get("status")
                if (
                    not isinstance(arguments, dict)
                    or not isinstance(outcome, dict)
                    or not isinstance(hit_id, str)
                    or not hit_id
                    or result_message.get("tool_call_id") != tool["id"]
                    or result_message.get("name") != name
                    or outcome.get("hit_id", hit_id) != hit_id
                    or status not in {
                        "metadata_only", "source_unavailable",
                        "source_use_restricted_or_no_passage", "inspected",
                    }
                ):
                    raise ValueError("unexpected saved inspection result")
            except (KeyError, TypeError, ValueError, json.JSONDecodeError, AttributeError):
                observation["inspection_status"] = "saved_result_has_no_safe_inspection_projection"
                return observation
            inspection = {
                "hit_id": hit_id,
                "status": status,
                "result_ref": ref(result),
                "basis": "saved_tool_outcome_not_current_source_permission",
            }
            if status == "inspected":
                source_ref = result["body"].get("effect_ref")
                source = self.store.get(**source_ref) if (
                    isinstance(source_ref, dict)
                    and set(source_ref) == {"kind", "id", "version"}
                    and source_ref.get("kind") == "source_copy"
                ) else None
                if (
                    source is not None
                    and ref(source) == source_ref
                    and source["body"].get("mission_id") == response["body"].get("mission_id")
                    and outcome.get("source_id") == source_ref["id"]
                    and outcome.get("source_version") == source_ref["version"]
                ):
                    inspection["source_ref"] = source_ref
                    inspection["current_model_input_status"] = self._source_status(source)[
                        "model_input_status"
                    ]
                else:
                    inspection["current_model_input_status"] = "unavailable"
            observation["inspection"] = inspection
            return observation
        if name != "search_scholarly":
            return observation
        try:
            arguments = json.loads(function["arguments"])
            result_message = json.loads(self.store.read_blob(result["body"]["blob"]["sha256"]))
            if (
                result_message.get("tool_call_id") != tool["id"]
                or result_message.get("name") != name
            ):
                raise ValueError("tool result mismatch")
            outcome = json.loads(result_message["content"])
            hits = outcome.get("hits")
            if not isinstance(arguments, dict) or not isinstance(outcome, dict):
                raise ValueError("unexpected search result")
        except (KeyError, TypeError, ValueError, json.JSONDecodeError):
            observation["search_status"] = "saved_result_has_no_safe_search_projection"
            return observation
        try:
            dependencies = self.response_source_dependencies(
                response_ref, require_permitted=False,
            )
        except ContextError:
            dependencies = ()
            search_status = "source_lineage_unavailable"
            unavailable = []
        else:
            unavailable = [pointer for pointer in dependencies if (
                self._source_status(self.store.get(**pointer))["model_input_status"]
                != "permitted"
            )]
            search_status = (
                "data_unavailable_due_source_use" if unavailable else "available"
            )
        if search_status != "available":
            # Arguments were model-authored under an earlier source context.
            # Keep only operational counts and exact coordinates when that
            # context cannot be reauthorized; the original blob stays saved.
            observation["search"] = {
                "route": outcome.get("route") if isinstance(outcome.get("route"), str) else None,
                "hit_count": len(hits) if isinstance(hits, list) else None,
                "status": search_status,
                "source_dependency_refs": list(dependencies)[:4],
                "unavailable_source_refs": unavailable[:4],
            }
            return observation
        query = arguments.get("query")
        if not isinstance(query, str):
            query = ""
        hit_ids = [item.get("hit_id") for item in hits if isinstance(item, dict)] if isinstance(hits, list) else []
        observation["search"] = {
            "route": outcome.get("route") if isinstance(outcome.get("route"), str) else None,
            "query_preview": query[:160],
            "query_chars": len(query),
            "query_sha256": hashlib.sha256(query.encode()).hexdigest(),
            "selected_id": arguments.get("selected_id") if isinstance(arguments.get("selected_id"), (str, int)) else None,
            "outcome": outcome.get("outcome") if isinstance(outcome.get("outcome"), str) else None,
            "hit_count": len(hits) if isinstance(hits, list) else None,
            "first_hit_ids": [value for value in hit_ids[:3] if isinstance(value, str)],
            "status": "saved_tool_observation_not_evidence_use",
        }
        return observation

    def _saved_search_hit_ids(self, response_ref: dict, observation: dict) -> list[str]:
        """Read only exact, same-task lead IDs from a completed saved search result."""
        response = self.store.get(**response_ref)
        result_ref = observation.get("result_ref")
        result = self.store.get(**result_ref) if isinstance(result_ref, dict) else None
        search = observation.get("search")
        if (
            response is None or response["body"].get("status") != "tool_requested"
            or observation.get("tool_name") != "search_scholarly"
            or not isinstance(search, dict)
            or search.get("outcome") not in {"success", "empty"}
            or result is None or result["kind"] != "context_tool_result"
            or result["body"].get("response_ref") != response_ref
            or result["body"].get("mission_id") != response["body"].get("mission_id")
        ):
            raise ContextError("Exact completed search result is unavailable")
        try:
            message = json.loads(self.store.read_blob(result["body"]["blob"]["sha256"]))
            payload = json.loads(message["content"])
            hits = payload["hits"]
        except (KeyError, TypeError, ValueError, UnicodeDecodeError) as exc:
            raise ContextError("Exact completed search result is malformed") from exc
        if (
            not isinstance(message, dict) or not isinstance(payload, dict)
            or
            message.get("name") != "search_scholarly"
            or message.get("tool_call_id") != observation.get("tool_call_id")
            or payload.get("outcome") != search["outcome"]
            or not isinstance(hits, list) or len(hits) > 100
            or search.get("hit_count") != len(hits)
        ):
            raise ContextError("Exact completed search result does not match its saved turn")
        hit_ids = []
        for item in hits:
            hit_id = item.get("hit_id") if isinstance(item, dict) else None
            hit = self.store.get("retrieval_hit", hit_id, 1) if isinstance(hit_id, str) else None
            if (
                not hit_id or hit_id in hit_ids or hit is None
                or hit["body"].get("mission_id") != response["body"].get("mission_id")
                or hit["body"].get("task_id") != response["body"].get("task_id")
            ):
                raise ContextError("Saved search lead is outside this peer's exact task")
            hit_ids.append(hit_id)
        if hit_ids[:3] != search.get("first_hit_ids"):
            raise ContextError("Saved search leads differ from the archived preview")
        return hit_ids

    def _current_control(self, control_ref: dict, mission_id: str,
                         actor_id: str, task_id: str) -> dict:
        try:
            control = _exact(self.store, control_ref, mission_id)
        except StateError as exc:
            raise ContextError("Working context needs this peer's current task control") from exc
        if (
            control["kind"] != "mission_task_control"
            or self.store.get("mission_task_control", control["id"]) != control
            or control["body"].get("actor_id") != actor_id
            or control["body"].get("task_id") != task_id
        ):
            raise ContextError("Working context needs this peer's current task control")
        return control

    def _model_history(self, history: Iterable[dict], control: dict | None,
                       mission_id: str, actor_id: str, task_id: str) -> list[dict]:
        """Keep scientific feedback; skip one redundant host-only repair notice.

        The complete repair and exact prior budget ref remain in current task
        control. This notice only explains which local packer retried a failed
        preparation; it does not describe an observation or model response.
        """
        messages = list(history)
        if control is None:
            return messages
        feedback = control["body"].get("feedback")
        if (
            not isinstance(feedback, dict)
            or set(feedback) != {"preparation_repair", "previous_budget_ref"}
            or not isinstance(feedback["preparation_repair"], dict)
            or set(feedback["preparation_repair"]) != {"estimator", "packer"}
            or not all(isinstance(value, str) and value
                       for value in feedback["preparation_repair"].values())
            or control["body"].get("preparation_repair_attempted_for")
            != feedback["previous_budget_ref"]
        ):
            return messages
        pointer = feedback["previous_budget_ref"]
        if (
            not isinstance(pointer, dict)
            or set(pointer) != {"kind", "id", "version"}
            or pointer.get("kind") != "mission_call_budget"
            or type(pointer.get("version")) is not int
        ):
            return messages
        budget = self.store.get(**pointer)
        prior_call_ref = budget["body"].get("call_ref") if budget else None
        prior_call = self.store.get(**prior_call_ref) if (
            isinstance(prior_call_ref, dict)
            and set(prior_call_ref) == {"kind", "id", "version"}
            and prior_call_ref.get("kind") == "agent_call"
        ) else None
        if (
            budget is None or ref(budget) != pointer
            or budget["body"].get("mission_id") != mission_id
            or budget["body"].get("run_id") != control["body"].get("run_id")
            or prior_call is None
            or prior_call["body"].get("instance_id") != actor_id
            or prior_call["body"].get("task_id") != task_id
        ):
            return messages
        notice = canonical({
            "authority": "saved_host_observation_not_source_instruction",
            "host_feedback": feedback,
        })
        return [
            item for item in messages
            if not (isinstance(item, dict) and item.get("role") == "user"
                    and item.get("content") == notice)
        ]

    def _target_text_access(
        self, report: dict, snapshot: dict, selected_sources: Iterable[dict]
    ) -> dict:
        """Check report dependencies independently of source text selected for this call.

        A permitted source omitted to fit the current input does not revoke the
        derived report. Exact dependency records and current use policy still
        determine access; selection only describes what source text is present.
        """
        body = report["body"]
        dependencies: dict[tuple[str, str, int], dict] = {}
        complete = True
        producer_access = report_text_access(self.store, report)
        if not producer_access["dependency_lineage_complete"]:
            complete = False

        def add(pointer: object) -> None:
            nonlocal complete
            if isinstance(pointer, dict) and set(pointer) == {
                "kind", "id", "version", "sha256"
            }:
                exact = {key: pointer[key] for key in ("kind", "id", "version")}
                copy = self.store.get(**exact) if exact["kind"] == "source_copy" else None
                if copy is None or content_hash(copy["body"]) != pointer["sha256"]:
                    complete = False
                    return
                pointer = exact
            if (
                not isinstance(pointer, dict)
                or set(pointer) != {"kind", "id", "version"}
                or pointer.get("kind") != "source_copy"
                or not isinstance(pointer.get("id"), str)
                or type(pointer.get("version")) is not int
            ):
                complete = False
                return
            dependencies[(pointer["kind"], pointer["id"], pointer["version"])] = pointer

        provenance = body.get("citation_input_provenance", [])
        for pointer in producer_access["source_dependency_refs"]:
            add(pointer)
        if not isinstance(provenance, list):
            complete = False
        else:
            for item in provenance:
                add(item.get("copy_ref") if isinstance(item, dict) else None)
        citations = body.get("citations", [])
        if not isinstance(citations, list) or citations and len(provenance) < len(citations):
            complete = False
        producer_ref = body.get("producer_attempt_ref")
        if producer_ref is not None:
            attempt = self.store.get(**producer_ref) if (
                isinstance(producer_ref, dict)
                and set(producer_ref) == {"kind", "id", "version"}
                and producer_ref.get("kind") == "agent_attempt"
            ) else None
            call_ref = attempt["body"].get("call_ref") if attempt else None
            call = self.store.get(**call_ref) if (
                isinstance(call_ref, dict)
                and set(call_ref) == {"kind", "id", "version"}
                and call_ref.get("kind") == "agent_call"
            ) else None
            if (
                attempt is None or call is None
                or attempt["body"].get("mission_id") != body.get("mission_id")
                or call["body"].get("mission_id") != body.get("mission_id")
            ):
                complete = False
            else:
                for pointer in call["body"].get("input_refs", []):
                    if isinstance(pointer, dict) and pointer.get("kind") == "source_copy":
                        add(pointer)
        # A report can use earlier within-run notes or tool results beyond its
        # producing call and citations. Until exhaustive report dependencies are
        # recorded, every source displayed in the parent snapshot is a possible
        # derivation of its prose.
        indexed_sources = snapshot["body"].get("inspection", {}).get("sources", [])
        if not isinstance(indexed_sources, list):
            complete = False
        else:
            for item in indexed_sources:
                add(item.get("copyReference") if isinstance(item, dict) else None)

        selected = {
            (pointer.get("kind"), pointer.get("id"), pointer.get("version"))
            for pointer in selected_sources if isinstance(pointer, dict)
        }
        unavailable = []
        omitted = []
        for key, pointer in dependencies.items():
            if key not in selected:
                omitted.append(pointer)
            copy = self.store.get(**pointer)
            if copy is None or ref(copy) != pointer:
                unavailable.append(pointer)
                continue
            status = self._source_status(copy)["model_input_status"]
            if status == "permitted":
                continue
            encounter_ref = copy["body"].get("encounter")
            encounter = (
                self.store.get(**encounter_ref) if isinstance(encounter_ref, dict) else None
            )
            supplied_without_policy = (
                status == "unknown"
                and encounter is not None
                and encounter["body"].get("route") == "user_supplied"
                and copy["body"].get("rights_status") in {None, "permitted"}
                and self.store.resolve(
                    "source_use.copy", f"{pointer['id']}:{pointer['version']}"
                ) is None
            )
            if not supplied_without_policy:
                unavailable.append(pointer)
        for pointer in producer_access["unavailable_source_refs"]:
            if pointer not in unavailable:
                unavailable.append(pointer)
        return {
            "status": "available" if (
                complete and not unavailable and producer_access["status"] == "available"
            ) else "data_unavailable_due_source_use",
            "source_dependency_refs": list(dependencies.values()),
            "source_dependencies_omitted_from_input": omitted,
            "unavailable_source_refs": unavailable,
            "dependency_lineage_complete": complete,
        }

    def _followup_target_text_access(self, inclusion: dict, report: dict) -> dict:
        run_ref = inclusion["body"].get("run_ref")
        run = self.store.get(**run_ref) if isinstance(run_ref, dict) else None
        snapshot = (
            self.store.get("research_snapshot", run["body"].get("parent_snapshot_id"), 1)
            if run else None
        )
        if (
            run is None or snapshot is None
            or run["body"].get("target_ref") != ref(report)
            or ref(report) != inclusion["body"].get("followup_target_ref")
        ):
            raise ContextError("Exact parent follow-up target is unavailable")
        return self._target_text_access(
            report, snapshot, inclusion["body"].get("source_refs", [])
        )

    def _same_mission_report_text_access(self, inclusion: dict, report: dict) -> dict:
        """Check known run-wide dependencies of a locally linked report read."""
        mission_id = inclusion["body"].get("mission_id")
        run = self.store.get("mission_run", report["body"].get("run_id"))
        if report["body"].get("mission_id") != mission_id:
            raise ContextError("Linked report is unavailable in this mission")
        if run is None:
            # Older source-free reports can have no run lineage. They remain
            # readable only when neither the report nor this input names a
            # source dependency; otherwise expose an explicit absence status.
            if (
                not report["body"].get("citations")
                and not report["body"].get("citation_input_provenance")
                and not report["body"].get("producer_attempt_ref")
                and not inclusion["body"].get("source_refs")
            ):
                return self._target_text_access(
                    report, {"body": {"inspection": {"sources": []}}}, []
                )
            return {
                "status": "data_unavailable_due_source_use",
                "source_dependency_refs": [],
                "unavailable_source_refs": [],
                "dependency_lineage_complete": False,
            }
        if run["body"].get("mission_id") != mission_id:
            raise ContextError("Linked report run is unavailable in this mission")
        sources = list(run["body"].get("source_refs", []))
        for control in _heads(self.store, "mission_task_control"):
            if control["body"].get("run_id") == run["id"]:
                sources.extend(control["body"].get("source_refs", []))
        head = self.store.get("mission_snapshot_head", run["id"])
        snapshot_ref = head["body"].get("snapshot_ref") if head else None
        snapshot = self.store.get(**snapshot_ref) if isinstance(snapshot_ref, dict) else None
        if snapshot is not None and snapshot["body"].get("mission_id") == mission_id:
            sources.extend(
                item.get("copyReference") if isinstance(item, dict) else item
                for item in snapshot["body"].get("inspection", {}).get("sources", [])
            )
        provenance = report["body"].get("citation_input_provenance", [])
        if isinstance(provenance, list):
            sources.extend(
                item.get("copy_ref") for item in provenance if isinstance(item, dict)
            )
        producer_ref = report["body"].get("producer_attempt_ref")
        attempt = self.store.get(**producer_ref) if isinstance(producer_ref, dict) else None
        call_ref = attempt["body"].get("call_ref") if attempt else None
        call = self.store.get(**call_ref) if isinstance(call_ref, dict) else None
        if call is not None:
            sources.extend(
                item for item in call["body"].get("input_refs", [])
                if isinstance(item, dict) and item.get("kind") == "source_copy"
            )
        exact_sources = [
            {key: item[key] for key in ("kind", "id", "version")}
            for item in sources
            if isinstance(item, dict)
            and set(item) in (
                {"kind", "id", "version"}, {"kind", "id", "version", "sha256"}
            )
            and item.get("kind") == "source_copy"
        ]
        # Reuse the parent-report lineage and current-use check, conservatively
        # treating every source in the local run as a possible dependency.
        view = {"body": {"inspection": {"sources": [
            {"copyReference": item} for item in sources
        ]}}}
        return self._target_text_access(report, view, exact_sources)

    def _shared_report_text_access(self, inclusion: dict, report: dict) -> dict:
        """Use the accepted peer report's exact producing lineage, not all run copies."""
        saved = inclusion["body"]
        call = self.store.get(**saved["call_ref"])
        if call is None or shared_material_entry(
            self.store, mission_id=saved["mission_id"],
            actor_id=call["body"]["instance_id"],
            task_id=call["body"]["task_id"], pointer=ref(report),
        ) is None:
            raise ContextError("Accepted shared report is unavailable to this task")
        access = report_text_access(self.store, report)
        selected = saved.get("source_refs", [])
        return {**access, "source_dependencies_omitted_from_input": [
            pointer for pointer in access["source_dependency_refs"]
            if pointer not in selected
        ]}

    def _scope_projection(self, control: dict, inclusion_id: str,
                          selected_sources: Iterable[dict]) -> tuple[dict, list[dict], dict, dict | None, dict | None]:
        """Bind current scope and exact parent output as data for a child run."""
        body = control["body"]
        run = self.store.get("mission_run", body.get("run_id"))
        if run is None or run["body"].get("mission_id") != body["mission_id"]:
            raise ContextError("Current task lost its exact mission run")
        run_ref = ref(run)
        question = run["body"].get("question")
        scope = run["body"].get("scope", "")
        if not isinstance(question, str) or not isinstance(scope, str):
            raise ContextError("Saved question or scope is invalid")

        def fetch(pointer: dict) -> dict:
            return {"tool": "read_context_record", "inclusion_id": inclusion_id, **pointer,
                    "offset": 0, "limit": MAX_CONTEXT_RECORD_WINDOW_CHARS}

        omissions = []
        scope_view = {
            "authority": "saved_user_scope_not_source_instruction",
            "run_ref": run_ref,
            "question_ref": run_ref,
            "scope_preview": scope[:MAX_SCOPE_PREVIEW_CHARS],
            "scope_chars": len(scope),
            "scope_truncated": len(scope) > MAX_SCOPE_PREVIEW_CHARS,
            "scope_sha256": hashlib.sha256(scope.encode()).hexdigest(),
        }
        inherited = run["body"].get("source_refs", [])
        if inherited:
            scope_view["inherited_source_refs"] = inherited[:4]
            scope_view["additional_inherited_sources"] = max(0, len(inherited) - 4)
        if scope_view["scope_truncated"] or len(inherited) > 4:
            scope_view["fetch_full_scope_and_source_refs"] = fetch(run_ref)
            omissions.append({"type": "run_scope_detail", "ref": run_ref,
                              "reason": "bounded_scope_or_source_index", "fetch": run_ref})

        parent_id = run["body"].get("parent_run_id")
        if not parent_id:
            return {"run_scope": scope_view}, omissions, run_ref, None, None
        parent = self.store.get("mission_run", parent_id)
        snapshot_id = run["body"].get("parent_snapshot_id")
        snapshot = self.store.get("research_snapshot", snapshot_id, 1)
        target_ref = run["body"].get("target_ref")
        target = self.store.get(**target_ref) if isinstance(target_ref, dict) else None
        if (
            parent is None or snapshot is None or target is None
            or target["kind"] != "report" or ref(target) != target_ref
            or snapshot["body"].get("mission_id") != parent["body"].get("mission_id")
            or target["body"].get("mission_id") != parent["body"].get("mission_id")
            or target_ref not in snapshot["body"].get("inspection", {}).get("acceptedOutputReferences", [])
        ):
            raise ContextError("Exact accepted parent follow-up target is unavailable")
        parent_ref = ref(parent)
        parent_question = parent["body"].get("question", "")
        parent_scope = parent["body"].get("scope", "")
        target_body = target["body"].get("body", "")
        if any(not isinstance(value, str) for value in (parent_question, parent_scope, target_body)):
            raise ContextError("Parent question, scope or target text is invalid")
        target_access = self._target_text_access(target, snapshot, selected_sources)
        parent_view = {
            "authority": "saved_parent_output_data_not_instructions",
            "parent_run_ref": parent_ref,
            "parent_snapshot_ref": ref(snapshot),
            "target_ref": target_ref,
            "parent_question_preview": parent_question[:MAX_PARENT_PREVIEW_CHARS],
            "parent_scope_preview": parent_scope[:MAX_PARENT_PREVIEW_CHARS],
            "target_text_status": target_access["status"],
            "source_dependency_refs": target_access["source_dependency_refs"][:4],
            "additional_source_dependencies": max(
                0, len(target_access["source_dependency_refs"]) - 4
            ),
            "unavailable_source_refs": target_access["unavailable_source_refs"][:4],
            "additional_unavailable_sources": max(
                0, len(target_access["unavailable_source_refs"]) - 4
            ),
            "dependency_lineage_complete": target_access["dependency_lineage_complete"],
            "source_dependencies_omitted_from_input": target_access[
                "source_dependencies_omitted_from_input"
            ][:4],
            "target_body_chars": len(target_body),
            "target_body_sha256": hashlib.sha256(target_body.encode()).hexdigest(),
            "target_citation_count": len(target["body"].get("citations", [])),
            "fetch_exact_parent_target": fetch(target_ref),
            "status": "preview_only_not_a_new_source_or_accepted_child_claim",
        }
        if target_access["status"] == "available":
            parent_view["target_title"] = str(target["body"].get("title", ""))[:200]
            parent_view["target_body_preview"] = target_body[:MAX_PARENT_PREVIEW_CHARS]
        omissions.append({"type": "followup_target_detail", "ref": target_ref,
                          "reason": "bounded_parent_output_preview", "fetch": target_ref})
        if (len(parent_question) > MAX_PARENT_PREVIEW_CHARS
                or len(parent_scope) > MAX_PARENT_PREVIEW_CHARS):
            parent_view["fetch_full_parent_scope"] = fetch(parent_ref)
            omissions.append({"type": "parent_scope_detail", "ref": parent_ref,
                              "reason": "bounded_parent_scope_preview", "fetch": parent_ref})
        return {"run_scope": scope_view, "followup_target": parent_view}, omissions, run_ref, parent_ref, target_ref

    def _source_progress(
        self, control: dict, sources: list[dict], mission_id: str, *,
        default_supplied: bool = False,
    ) -> list[dict]:
        """Describe selected spans and saved windows, without claiming full coverage."""
        body = control["body"]
        available = body.get("source_refs", [])
        if any(ref(copy) not in available for copy in sources):
            raise ContextError("Selected source is not in the current task control")
        progress = []
        for copy in sources:
            pointer = ref(copy)
            selected = body.get("selected_passage_refs", {}).get(copy["id"])
            if selected is None:
                selected = []
                encounter_ref = copy["body"].get("encounter")
                encounter = (
                    self.store.get(**encounter_ref) if isinstance(encounter_ref, dict) else None
                )
                if encounter and encounter["body"].get("route") == "user_supplied":
                    if default_supplied:
                        from ora.evidence import EvidenceLibrary
                        from ora.evidence.intake import default_exact_submitted_passage

                        default = default_exact_submitted_passage(
                            EvidenceLibrary(self.store), pointer
                        )
                        if default:
                            selected.append(default)
                    passage_ids = []
                else:
                    passage_ids = copy["body"].get("passage_ids", [])
                for passage_id in passage_ids:
                    passage = self.store.get("source_passage", passage_id)
                    if passage is None:
                        raise ContextError("Source copy lost a saved selected passage")
                    selected.append(ref(passage))
            spans = []
            for passage_ref in selected:
                passage = _exact(self.store, passage_ref, mission_id)
                if passage["kind"] != "source_passage" or passage["body"].get("copy_ref") != pointer:
                    raise ContextError("Selected passage is not from its exact source copy")
                spans.append({"ref": passage_ref, "locator": passage["body"].get("locator")})
            latest = None
            for window_ref in reversed(body.get("read_window_refs", [])):
                window = self.store.get(**window_ref)
                if window is None or window["kind"] != "source_read_window":
                    raise ContextError("Exact saved source window is unavailable")
                if window["body"].get("copy_ref") == pointer:
                    latest = window
                    break
            coverage = extracted_text_coverage(
                self.store, pointer, selected, body.get("read_window_refs", [])
            )
            # Saved windows can cover the retained copy even when the current
            # model input contains only a later, explicitly selected excerpt.
            # Compute this view from selected checked passages alone; do not
            # count older windows as text supplied to this call.
            selected_coverage = (
                extracted_text_coverage(self.store, pointer, selected, [])
                if selected else None
            )
            selected_input = {
                "basis": "checked_passages_selected_for_this_call_not_model_comprehension",
                **(
                    {key: selected_coverage[key] for key in (
                        "status", "scope", "total_chars", "covered_chars",
                        "uncovered_chars", "next_unread_cursor",
                    )}
                    if selected_coverage else {
                        "status": "unknown",
                        "scope": "retained_extracted_text_only",
                        "total_chars": None,
                        "covered_chars": None,
                        "uncovered_chars": None,
                        "next_unread_cursor": None,
                        "reason": "raw_copy_input_has_no_checked_passage_selection",
                    }
                ),
            }
            item = {
                "copy_ref": pointer,
                "selected_passage_count": len(spans),
                "selected_spans": spans[:4],
                "additional_selected_passages": max(0, len(spans) - 4),
                "selected_input_coverage": selected_input,
                "latest_read_window_ref": ref(latest) if latest else None,
                "latest_window_next_cursor": latest["body"].get("next_cursor") if latest else None,
                "latest_window_total_blocks": latest["body"].get("total_blocks") if latest else None,
                "extracted_text_coverage": {
                    key: coverage[key] for key in (
                        "status", "scope", "original_source_partial", "source_surface",
                        "total_blocks", "total_chars", "covered_chars", "uncovered_chars",
                        "next_unread_cursor",
                    )
                },
                "status": "saved_windows_not_past_model_delivery_or_original_completeness",
            }
            if selected_input["status"] == "incomplete_extracted_text":
                item["whole_copy_absence_claim_guidance"] = (
                    "This call includes only selected checked passages. For a claim that "
                    "a detail is absent from the whole retained source, consult omitted "
                    "exact spans or source-linked saved notes; otherwise scope the claim "
                    "to the selected excerpt."
                )
            progress.append(item)
        return progress

    def _source_free_navigation_item(self, control: dict, item: dict) -> dict:
        """Project verified saved reading positions without source text or delivery claims."""
        coverage = item["extracted_text_coverage"]
        copy_ref = item["copy_ref"]
        matching_windows = [
            window for pointer in control["body"].get("read_window_refs", [])
            if (window := self.store.get(**pointer)) is not None
            and window["body"].get("copy_ref") == copy_ref
        ]
        coverage_basis = "checked_selected_passages_and_saved_windows"
        if coverage["status"] == "unknown" and matching_windows:
            # A supplied source can have an older passage without a mapped
            # character locator. Verify the exact host-saved windows alone;
            # do not let that unrelated passage suppress their real cursors.
            window_coverage = extracted_text_coverage(
                self.store, copy_ref, [], [ref(window) for window in matching_windows]
            )
            if window_coverage["status"] != "unknown":
                coverage = window_coverage
                coverage_basis = "verified_saved_windows_only"
        entry = {
            "copy_ref": copy_ref,
            "saved_coverage": {
                key: coverage[key]
                for key in (
                    "status", "scope", "original_source_partial",
                    "covered_chars", "total_chars",
                )
            },
            "latest_window_ref": (
                ref(matching_windows[-1])
                if coverage["status"] != "unknown" and matching_windows else None
            ),
            "latest_window_next_cursor": (
                matching_windows[-1]["body"].get("next_cursor")
                if coverage["status"] != "unknown" and matching_windows else None
            ),
            "source_text_in_this_call": False,
            "past_model_delivery": "unverified",
            "fetch_first_uncovered": (
                {
                    "tool": "read_source_window",
                    "source_id": copy_ref["id"],
                    "source_version": copy_ref["version"],
                    **coverage["next_unread_cursor"],
                    "char_limit": 1900,
                }
                if coverage["status"] == "incomplete_extracted_text"
                and coverage["next_unread_cursor"] is not None
                else None
            ),
        }
        entry["saved_coverage"]["basis"] = coverage_basis
        entry["saved_coverage"]["first_uncovered_cursor"] = coverage["next_unread_cursor"]
        if coverage["status"] != "unknown" and matching_windows:
            # This is an exact earlier reading option, not the coverage
            # frontier; first_uncovered_cursor above is that frontier.
            furthest = max(
                matching_windows,
                key=lambda window: (
                    window["body"]["block_offset"], window["body"]["char_offset"],
                ),
            )
            if ref(furthest) != entry["latest_window_ref"]:
                entry["furthest_start_saved_window"] = {
                    "window_ref": ref(furthest),
                    "start_cursor": {
                        "block_offset": furthest["body"]["block_offset"],
                        "char_offset": furthest["body"]["char_offset"],
                    },
                    "char_limit": furthest["body"]["char_limit"],
                    "next_cursor": furthest["body"]["next_cursor"],
                }
        return entry

    def _source_fit_feedback(
        self, control: dict, turns: tuple[dict, ...], reason: str | None,
    ) -> dict | None:
        """Describe a saved reading window without claiming its text was delivered."""
        body = control["body"]
        for pointer in reversed(turns):
            observation = self._archived_tool_observation(pointer)
            window_view = observation.get("window")
            if not isinstance(window_view, dict):
                continue
            window_ref = window_view["window_ref"]
            copy_ref = window_view["copy_ref"]
            window = self.store.get(**window_ref)
            copy = self.store.get(**copy_ref)
            if (
                copy_ref not in body.get("source_refs", [])
                or window_ref not in body.get("read_window_refs", [])
                or window is None or copy is None
                or (
                    copy["body"].get("mission_id") not in (None, body["mission_id"])
                    and not inherited_public_source(
                        self.store, mission_id=body["mission_id"],
                        run_id=body["run_id"], copy_ref=copy_ref,
                        actor_id=body["actor_id"], task_id=body["task_id"],
                        control=control,
                    )
                )
                or window["body"].get("copy_ref") != copy_ref
            ):
                continue
            saved = window["body"]
            permitted = self._source_status(copy)["model_input_status"] == "permitted"
            return {
                "result_ref": observation["result_ref"],
                "window_ref": window_ref,
                "copy_ref": copy_ref,
                "saved_read_succeeded": True,
                "source_text_in_this_call": False,
                "past_model_delivery": "not_established_by_saved_read",
                "status": (
                    ("saved_text_omitted_for_input_fit" if reason in {
                        "exact_source_text_exceeds_allowance",
                        "required_context_or_complete_request_exceeded_allowance",
                    } else "saved_text_not_selected_for_current_input") if permitted
                    else "saved_text_unavailable_under_current_source_policy"
                ),
                "requested_window": {
                    key: saved[key] for key in ("block_offset", "char_offset", "char_limit")
                },
                "returned_chars": window_view["returned_chars"],
                "latest_window_next_cursor": window_view["next_cursor"],
                "next_action": (
                    "Saved source text is absent from this input. "
                    "Choose a smaller read_source_window at the required offset with "
                    "selection_mode=replace; append keeps earlier selected text. "
                    "Use discover_capabilities/request_capability for source-linked notes "
                    "when needed."
                    if permitted else
                    "Current source policy prevents delivery; resolve source use or use "
                    "another permitted source. This is not a size-related denial."
                ),
            }
        return None

    def _partial_output_projection(
        self, control: dict, call: dict, sources: list[dict], inclusion_id: str,
        mission_id: str, task_id: str, actor_id: str,
    ) -> tuple[dict | None, dict | None]:
        """Offer only saved visible text from an exact length cutoff to its next turn."""
        feedback = control["body"].get("feedback")
        if not isinstance(feedback, dict) or feedback.get("status") not in {
            "partial", "output_length_partial",
        }:
            return None, None
        response_ref = feedback.get("previous_response_ref")
        if (
            not isinstance(response_ref, dict)
            or set(response_ref) != {"kind", "id", "version"}
            or response_ref.get("kind") != "model_response_receipt"
        ):
            return None, None
        pinned_sources = [
            {key: item[key] for key in ("kind", "id", "version")}
            for item in call["body"].get("input_refs", [])
            if isinstance(item, dict) and item.get("kind") == "source_copy"
        ]
        if pinned_sources != [ref(item) for item in sources]:
            result = {"status": "reissue_required", "reason": "current_source_selection_changed"}
        else:
            try:
                result = read_partial_output_window(
                    self.store, response_ref,
                    mission_id=mission_id, task_id=task_id, actor_id=actor_id,
                    current_call_source_refs=pinned_sources,
                    limit=MAX_PARTIAL_OUTPUT_PREVIEW_CHARS,
                )
            except OutputRecoveryError:
                result = {"status": "content_unavailable", "reason": "unqualified_saved_response"}
        view = {
            "authority": "saved_incomplete_visible_model_output_not_an_accepted_result",
            "response_ref": response_ref,
            "status": result["status"],
            "next_action": "Continue with a fresh complete action; partial tool arguments were not executed",
        }
        if result["status"] != "available":
            if isinstance(result.get("reason"), str):
                view["reason"] = result["reason"]
            return view, None
        view.update({
            "text_preview": result["text"],
            "preview_characters": len(result["text"]),
            "total_characters": result["total_characters"],
            "content_sha256": result["content_sha256"],
            "next_offset": result["next_offset"],
        })
        if result["next_offset"] is not None:
            view["next_request"] = {
                "tool": "read_context_record", "inclusion_id": inclusion_id,
                **response_ref, "offset": result["next_offset"],
                "limit": MAX_CONTEXT_RECORD_WINDOW_CHARS,
            }
        omission = {
            "type": "partial_visible_output", "ref": response_ref,
            "reason": "visible_draft_bounded_for_current_input", "fetch": response_ref,
        }
        return view, omission

    def save_tool_result(
        self,
        operation_id: str,
        *,
        response_ref: dict,
        tool_call_id: str,
        content: str,
        name: str,
        effect_ref: dict | None = None,
    ) -> dict:
        """Save a completed tool result once; external effect execution stays with host."""
        identifier(tool_call_id)
        if not isinstance(content, str) or not isinstance(name, str) or not name:
            raise ContextError("Tool result needs text and a function name")
        response = self.store.get(**response_ref)
        if response is None or response["kind"] != "model_response_receipt":
            raise ContextError("Saved model response is unavailable")
        raw = json.loads(self.store.read_blob(response["body"]["blob"]["sha256"]))
        calls = raw.get("tool_calls")
        if (
            response["body"].get("status") != "tool_requested"
            or not isinstance(calls, list)
            or len(calls) != 1
            or calls[0].get("id") != tool_call_id
            or calls[0].get("function", {}).get("name") != name
        ):
            raise ContextError("Tool result does not match the returned model call")
        message = {"role": "tool", "tool_call_id": tool_call_id, "name": name, "content": content}
        blob = self.store.put_blob(canonical(message).encode())
        mission_id = response["body"]["mission_id"]
        body = {
            "schema": "ora.context_tool_result.v1",
            "mission_id": mission_id,
            "response_ref": response_ref,
            "tool_call_id": tool_call_id,
            "effect_ref": effect_ref,
            "message_sha256": _sha(message),
            "blob": blob,
        }

        def save(tx: Transaction) -> dict:
            saved_response = require_ref(tx, mission_id, response_ref)
            if saved_response != response:
                raise ContextError("Tool response changed before result commit")
            effect = require_ref(tx, mission_id, effect_ref) if effect_ref else None
            record = tx.put("context_tool_result", f"tool-result:{response['id']}", body)
            tx.link(record, saved_response, "responds_to")
            if effect:
                tx.link(record, effect, "derived_from")
            return ref(record)

        return self.store.transact(operation_id, body, save, mission_id=mission_id)

    # Build one turn's working view from saved records. The model session adds
    # instructions and source packets, then handles the separate dispatch step.
    def assemble(
        self,
        call_ref: dict,
        task_ref: dict,
        *,
        source_refs: Iterable[dict] = (),
        evidence_refs: Iterable[dict] = (),
        contribution_refs: Iterable[dict] = (),
        note_refs: Iterable[dict] = (),
        summary_ref: dict | None = None,
        history: Iterable[dict] = (),
        tool_turn_refs: Iterable[dict] = (),
        archived_tool_turn_refs: Iterable[dict] = (),
        control_ref: dict | None = None,
        allowance: int | None = None,
        estimate_tokens: Callable[[str], int] | None = None,
        preview_call: dict | None = None,
        compact_selected_window: bool = False,
    ) -> ContextPack:
        """Assemble current state; size allowance applies to these messages only.

        The host must account separately for pinned instructions, exposed tools,
        selected source text, and output/reasoning reserve before setting allowance.
        Full records remain fetchable when their detail is omitted here.
        """
        if allowance is not None and (
            type(allowance) is not int or allowance <= 0 or estimate_tokens is None
        ):
            raise ContextError("Input allowance needs a positive size and token estimator")
        call = preview_call if preview_call is not None else self.store.get(**call_ref)
        if (
            call is None or call["kind"] != "agent_call" or ref(call) != call_ref
            or preview_call is not None and (
                call_ref["version"] != 1
                or self.store.get("agent_call", call_ref["id"]) is not None
            )
        ):
            raise ContextError("Exact saved or validated preview agent call is unavailable")
        mission_id = call["body"]["mission_id"]
        actor_id = call["body"]["instance_id"]
        task = _exact(self.store, task_ref, mission_id)
        if task["kind"] != "board_proposal" or task["id"] != call["body"]["task_id"]:
            raise ContextError("Call and current task are different")
        current_task = self.store.get("board_proposal", task["id"])
        if current_task is None:
            raise ContextError("Task is unavailable")
        config = self.store.get("agent_configuration", call["body"]["configuration_id"], 1)
        if (
            config is None
            or config["body"].get("mission_id") != mission_id
            or config["body"].get("instance_id") != actor_id
            or content_hash(
                {k: v for k, v in config["body"].items() if k != "configuration_sha256"}
            )
            != call["body"].get("configuration_sha256")
        ):
            raise ContextError("Pinned call configuration is unavailable or changed")
        requested_sources = list(source_refs) or [
            {key: item[key] for key in ("kind", "id", "version")}
            for item in call["body"].get("input_refs", [])
            if item.get("kind") == "source_copy"
        ]
        sources = [
            _context_source_ref(self.store, p, mission_id, actor_id, task["id"])
            for p in requested_sources
        ]
        if any(item["kind"] != "source_copy" for item in sources):
            raise ContextError("Source inputs need exact source-copy references")
        evidence = [_exact(self.store, p, mission_id) for p in evidence_refs]
        contributions = [_exact(self.store, p, mission_id) for p in contribution_refs]
        notes = [_exact(self.store, p, mission_id) for p in note_refs]
        if any(
            item["kind"] != "working_note"
            or item["body"].get("actor_id") != actor_id
            or item["body"].get("task_id") != task["id"]
            for item in notes
        ):
            raise ContextError("Only this peer's task notes may enter its context")
        notes = [self.working_note_projection(note) for note in _newest(notes)]
        summary = _exact(self.store, summary_ref, mission_id) if summary_ref else None
        if summary is not None:
            if summary["body"].get("authority") != "model_authored_continuation_aid":
                raise ContextError("Conversation summary source lineage is unavailable")
            access = self.conversation_summary_access(
                summary, mission_id=mission_id, actor_id=actor_id, task_id=task["id"],
            )
            if access["status"] != "available":
                raise ContextError("Conversation summary source use is unavailable")
        current_turns = tuple(tool_turn_refs)
        archived_turns = tuple(archived_tool_turn_refs)
        if any(pointer in current_turns for pointer in archived_turns):
            raise ContextError("Current and completed tool-turn references must be distinct")
        for pointer in archived_turns:
            response = _exact(self.store, pointer, mission_id)
            call_pointer = response["body"].get("call_ref")
            saved_call = self.store.get(**call_pointer) if isinstance(call_pointer, dict) else None
            result = self.store.get("context_tool_result", f"tool-result:{response['id']}")
            if (
                response["kind"] != "model_response_receipt"
                or response["body"].get("status") != "tool_requested"
                or response["body"].get("task_id") != task["id"]
                or saved_call is None
                or saved_call["body"].get("instance_id") != actor_id
                or result is None
                or result["body"].get("response_ref") != pointer
            ):
                raise ContextError("Completed tool-turn history has an unresolved exact result")
        # Coordination context: gather incoming messages, outstanding requests
        # and shared updates so the peer can respond to the saved state of work.
        cursor_record = self.store.get("mail_subscription", f"{mission_id}:{actor_id}")
        cursor = cursor_record["body"]["cursor"] if cursor_record else 0
        db = self.store._connect()
        try:
            event_through = db.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM events WHERE mission_id=?", (mission_id,)
            ).fetchone()[0]
        finally:
            db.close()
        messages = [
            item
            for item in _heads(self.store, "mail_message")
            if item["body"].get("mission_id") == mission_id
            and item["body"].get("recipient_ref", {}).get("id") == actor_id
            and (
                item["body"].get("task_ref") is None
                or item["body"]["task_ref"].get("id") == task["id"]
                or item["body"].get("request_id") is not None
            )
        ]
        requests, request_messages, answers = [], [], []
        for item in _heads(self.store, "mail_request"):
            body = item["body"]
            if body.get("mission_id") != mission_id or actor_id not in {
                body.get("recipient_id"),
                body.get("requester_id"),
            }:
                continue
            pointer = body.get("message_ref")
            original = self.store.get(**pointer) if isinstance(pointer, dict) else None
            if original is None or original["body"].get("mission_id") != mission_id:
                raise ContextError("Peer request lost its exact originating message")
            subject = original["body"].get("task_ref") or body.get("cause_ref") or {}
            if body.get("recipient_id") != actor_id and (
                subject.get("kind") != "board_proposal" or subject.get("id") != task["id"]
            ):
                continue
            requests.append(item)
            latest_message = self.store.get("mail_message", original["id"])
            if latest_message:
                request_messages.append(latest_message)
            for answer_ref in body.get("answer_refs", []):
                answer = _exact(self.store, answer_ref, mission_id)
                if (
                    answer["kind"] != "mail_answer"
                    or answer["body"].get("request_id") != item["id"]
                ):
                    raise ContextError("Peer request answer lineage is invalid")
                answers.append(answer)
        outgoing_request_messages = [
            item
            for item in request_messages
            if item["body"].get("sender_ref", {}).get("id") == actor_id
        ]
        messages = _newest(messages)
        requests = _newest(requests)
        answers = _newest(answers)
        outgoing_request_messages = _newest(outgoing_request_messages)
        shared = [
            item
            for kind in ("board_dissent", "board_outcome")
            for item in _heads(self.store, kind)
            if item["body"].get("mission_id") == mission_id
            and item["body"].get("proposal_ref", {}).get("id") == task["id"]
        ]
        for item in _heads(self.store, "peer_update_uptake"):
            if item["body"].get("mission_id") != mission_id:
                continue
            pointer = item["body"].get("update_ref")
            update = self.store.get(**pointer) if isinstance(pointer, dict) else None
            if update is None:
                continue
            subject = update["body"].get("task_ref") or update["body"].get("proposal_ref") or {}
            if (
                subject.get("id") == task["id"]
                or update["kind"] == "board_proposal"
                and update["id"] == task["id"]
            ):
                shared.append(item)
        shared = _newest(shared)
        routine_own_outcomes = [
            item for item in shared if _routine_own_executor_outcome(self.store, item, actor_id)
        ]
        routine_keys = {(item["id"], item["version"]) for item in routine_own_outcomes}
        shared = [item for item in shared if (item["id"], item["version"]) not in routine_keys]
        active = {"unanswered", "awaiting_review", "partial_awaiting_review", "partly_answered"}
        request_preview = sorted(
            requests,
            key=lambda item: (item["body"].get("status") in active, item["created_at"], item["id"]),
            reverse=True,
        )[:2]
        shared_preview = sorted(
            shared,
            key=lambda item: (
                item["kind"] == "board_dissent"
                or (
                    item["kind"] == "peer_update_uptake"
                    and item["body"].get("disposition") == "challenged"
                ),
                item["created_at"],
                item["id"],
            ),
            reverse=True,
        )[:2]
        stale = []
        if summary:
            for pointer in [
                *summary["body"].get("input_refs", []),
                *summary["body"].get("source_refs", []),
            ]:
                if isinstance(pointer, dict) and all(
                    k in pointer for k in ("kind", "id", "version")
                ):
                    head = self.store.get(pointer["kind"], pointer["id"])
                    if head is None or head["version"] != pointer["version"]:
                        stale.append(pointer)
        source_status = [self._source_status(item) for item in sources]
        inclusion_id = f"context:model-intent:{call['id']}:{call['version']}"
        page_fetch = {"tool": "read_context_page", "inclusion_id": inclusion_id, "cursor": 0}
        index = {
            "schema": "ora.working_context.v4",
            "authority": "current_records",
            "selected_model_id": call["body"].get("model_id"),
            "task_ref": ref(current_task),
            "task": {
                key: current_task["body"].get(key)
                for key in ("objective", "status", "next_action", "expected_artifact")
                if key in current_task["body"]
            },
            "record_counts": {
                "messages": len(messages),
                "requests": len(requests),
                "answers": len(answers),
                "shared_updates": len(shared) + len(routine_own_outcomes),
                "routine_own_outcomes_compacted": len(routine_own_outcomes),
                "evidence": len(evidence),
                "contributions": len(contributions),
                "notes": len(notes),
                "completed_tool_turns": len(archived_turns),
                "shared_materials": 0,
            },
            "older_records": page_fetch,
            "summary": (
                {
                    "ref": ref(summary),
                    "status": "stale" if stale else "current",
                    "stale_input_refs": stale,
                    "authority": (
                        "model_authored_continuation_aid_not_verified_evidence"
                        if summary["body"].get("authority") == "model_authored_continuation_aid"
                        else "optional_peer_aid"
                    ),
                    **({
                        "text": summary["body"]["text"],
                        "archive_ref": summary["body"]["archive_ref"],
                        "history_fetch": {"tool": "read_conversation_history",
                                          "archive_id": summary["body"]["archive_ref"]["id"],
                                          "cursor": 0, "limit": 1800,
                                          "projection_sha256": ""},
                    } if summary["body"].get("authority") == "model_authored_continuation_aid" else {}),
                }
                if summary
                else None
            ),
            "details": [],
            "omitted": None,
        }
        if summary is None:
            index.pop("summary")
        scope_omissions = []
        shared_materials = []
        control = None
        source_progress = []
        run_ref = parent_run_ref = followup_target_ref = None
        if control_ref is not None:
            control = self._current_control(control_ref, mission_id, actor_id, task["id"])
            (scope_view, scope_omissions, run_ref, parent_run_ref,
             followup_target_ref) = self._scope_projection(
                control, inclusion_id, (ref(item) for item in sources)
            )
            index.update(scope_view)
            browser_refs = control["body"].get("browser_search_response_refs", [])
            if browser_refs:
                try:
                    browser_result = self.browser_search_result(
                        mission_id=mission_id, actor_id=actor_id, task_id=task["id"],
                        response_id=browser_refs[-1]["id"], limit=600,
                    )
                except ContextError:
                    browser_result = {"status": "data_unavailable_due_source_use",
                                      "response_receipt_ref": browser_refs[-1]}
                index["browser_search"] = browser_result
                index["browser_search_response_refs"] = browser_refs[-4:]
            if sources:
                source_progress = self._source_progress(
                    control, sources, mission_id, default_supplied=True
                )
                index["source_progress"] = source_progress
            elif control["body"].get("source_refs"):
                permitted = []
                for pointer in control["body"]["source_refs"]:
                    copy = self.store.get(**pointer)
                    if copy is not None and self._source_status(copy)["model_input_status"] == "permitted":
                        permitted.append(copy)
                if permitted:
                    # Source-free recovery retains reading state, not source
                    # bytes. The next selected call must perform its own use
                    # check and carry its own exact passage packet.
                    retained_progress = self._source_progress(
                        control, permitted[:2], mission_id
                    )
                    index["retained_reading_progress"] = [
                        {
                            "copy_ref": item["copy_ref"],
                            "selected_passage_count": item["selected_passage_count"],
                            "latest_read_window_ref": item["latest_read_window_ref"],
                            "latest_window_next_cursor": item["latest_window_next_cursor"],
                            "extracted_text_coverage": item["extracted_text_coverage"],
                            "source_text_in_this_call": False,
                            "past_model_delivery": "unverified",
                        }
                        for item in retained_progress
                    ]
                    # Keep the exact saved reading cursor when the larger
                    # progress view is dropped under input pressure. This is
                    # navigation over retained checked text, never a claim
                    # that those bytes entered this model request.
                    index["source_free_reading_navigation"] = [
                        self._source_free_navigation_item(control, item)
                        for item in retained_progress
                    ]
                    if len(permitted) > 2:
                        index["additional_retained_reading_copies"] = len(permitted) - 2
            partial_view, partial_omission = self._partial_output_projection(
                control, call, sources, inclusion_id, mission_id, task["id"], actor_id
            )
            if partial_view is not None:
                index["partial_visible_output"] = partial_view
            if partial_omission is not None:
                scope_omissions.append(partial_omission)
            shared_materials = shared_material_entries(
                self.store, mission_id=mission_id, actor_id=actor_id, task_id=task["id"],
            )
            index["record_counts"]["shared_materials"] = len(shared_materials)
            scope_omissions.extend({
                "type": "shared_material", "ref": entry["ref"],
                "reason": "shared_material_metadata_only", "fetch": entry["ref"],
            } for entry in shared_materials)
        recent_observations = [
            self._archived_tool_observation(pointer)
            for pointer in archived_turns[-MAX_SEARCH_RECAP_SCAN:]
        ]
        if archived_turns and control and sources:
            latest = archived_turns[-1]
            if self.checked_window_is_selected_input(
                latest, call=call, control=control,
                selected_source_refs=tuple(ref(item) for item in sources),
            ):
                observation = self._archived_tool_observation(latest)
                if observation.get("window"):
                    index["last_checked_window"] = {
                        "response_ref": latest,
                        "result_ref": observation["result_ref"],
                        **observation["window"],
                        "text_status": "exact_checked_passage_in_selected_source_packet",
                    }
        inspection_by_hit = {
            inspection["hit_id"]: inspection
            for observation in recent_observations
            if (inspection := observation.get("inspection"))
        }
        recent_searches = [
            observation for observation in recent_observations if observation.get("search")
        ]
        if recent_searches:
            index["recent_scholarly_searches"] = recent_searches[-MAX_RECENT_SEARCH_RECAP:]
            for observation in index["recent_scholarly_searches"]:
                known = [
                    inspection_by_hit[hit_id]
                    for hit_id in observation["search"].get("first_hit_ids", [])
                    if hit_id in inspection_by_hit
                ]
                if known:
                    observation["search"]["known_hit_inspections"] = known
        recent_inspections = []
        seen_hits = set()
        for observation in reversed(recent_observations):
            inspection = observation.get("inspection")
            if inspection and inspection["hit_id"] not in seen_hits:
                recent_inspections.append(inspection)
                seen_hits.add(inspection["hit_id"])
                if len(recent_inspections) == MAX_RECENT_INSPECTION_RECAP:
                    break
        if recent_inspections:
            index["recent_hit_inspections"] = list(reversed(recent_inspections))
        if messages:
            index["message_refs"] = [ref(item) for item in messages[:2]]
        if outgoing_request_messages:
            index["outgoing_request_message_refs"] = list(_unique(outgoing_request_messages[:2]))
        if requests:
            index["request_dispositions"] = [
                {
                    "ref": ref(item),
                    "status": item["body"].get("status"),
                    "answer_refs": item["body"].get("answer_refs", []),
                    "accepted_answer_ref": item["body"].get("accepted_answer_ref"),
                    "replacement_request_ref": item["body"].get("replacement_request_ref"),
                }
                for item in request_preview
            ]
            if control:
                for item, disposition in zip(request_preview, index["request_dispositions"]):
                    request_body = item["body"]
                    if (request_body.get("requester_id") != actor_id
                            or request_body.get("status") != "unanswered"):
                        continue
                    recipients = [
                        candidate for candidate in _heads(self.store, "mission_task_control")
                        if candidate["body"].get("mission_id") == mission_id
                        and candidate["body"].get("run_id") == control["body"].get("run_id")
                        and candidate["body"].get("actor_id") == request_body.get("recipient_id")
                    ]
                    if len(recipients) == 1:
                        disposition["recipient_control_ref"] = ref(recipients[0])
                        disposition["next_action"] = (
                            "Continue independent work, or request wait_for_dependencies "
                            "and wait on this exact request and recipient control. "
                            "Use review_peer_answer after an answer arrives."
                        )
        if answers:
            index["request_answer_refs"] = [ref(item) for item in answers[:2]]
        if shared_preview:
            urgent_preview = [
                item
                for item in shared_preview
                if item["kind"] == "board_dissent"
                or item["kind"] == "peer_update_uptake"
                and item["body"].get("disposition") == "challenged"
            ]
            if urgent_preview:
                index["shared_update_refs"] = [ref(item) for item in urgent_preview]
        if routine_own_outcomes:
            index["routine_own_outcomes"] = {
                "count": len(routine_own_outcomes),
                "recent_refs": [ref(item) for item in routine_own_outcomes[:2]],
                "fetch": page_fetch,
            }
        if sources:
            index["source_refs"] = [ref(item) for item in sources]
            source_retrieval = []
            for item in source_status:
                view = {
                    "ref": item["selected_ref"],
                    "status": item["model_input_status"],
                    "current_ref": item["current_ref"],
                    "current_source_use_ref": item["current_source_use_ref"],
                    "selected_model_input_status": item["selected_model_input_status"],
                    "current_model_input_status": item["current_model_input_status"],
                }
                progress = next(
                    (entry for entry in source_progress
                     if entry["copy_ref"] == item["selected_ref"]), None
                )
                if progress:
                    selected_input = progress["selected_input_coverage"]
                    view["selected_input_scope"] = {
                        "basis": "checked_passages_in_this_call_not_saved_windows",
                        "status": selected_input["status"],
                        "covered_chars": selected_input["covered_chars"],
                        "total_chars": selected_input["total_chars"],
                        "first_omitted_cursor": selected_input["next_unread_cursor"],
                        "saved_read_status": progress["extracted_text_coverage"]["status"],
                    }
                    if selected_input["status"] == "incomplete_extracted_text":
                        view["absence_claim_scope"] = (
                            "For whole-source absence, consult omitted exact spans or "
                            "source-linked notes; otherwise scope to this excerpt."
                        )
                source_retrieval.append(view)
            index["source_retrieval"] = source_retrieval
        if evidence:
            index["evidence_refs"] = [ref(item) for item in evidence[:2]]
        if contributions:
            index["contribution_refs"] = [ref(item) for item in contributions[:2]]
        if notes:
            index["note_refs"] = [ref(item) for item in notes[:2]]
        past = self._model_history(history, control, mission_id, actor_id, task["id"])
        if any(
            not isinstance(item, dict)
            or item.get("role") in {"tool"}
            or "reasoning" in item
            or "tool_calls" in item
            for item in past
        ):
            raise ContextError("Tool history must come from exact saved response/result records")
        source_fit_recovery = False
        source_recovery_reason = None
        if not sources and control:
            for message in past:
                try:
                    marker = json.loads(message.get("content", ""))
                except (TypeError, ValueError):
                    continue
                if isinstance(marker, dict) and marker.get("source_context_mode") == "source_free_recovery":
                    source_fit_recovery = True
                    source_recovery_reason = marker.get("reason")
                    break
        delivery_feedback = (
            self._source_fit_feedback(control, (*archived_turns, *current_turns), source_recovery_reason)
            if source_fit_recovery else None
        )
        if delivery_feedback:
            prior_navigation = next((
                item for item in index.get("source_free_reading_navigation", [])
                if item.get("copy_ref") == delivery_feedback["copy_ref"]
            ), None)
            if prior_navigation:
                delivery_feedback["first_uncovered_cursor"] = prior_navigation[
                    "saved_coverage"
                ]["first_uncovered_cursor"]
                delivery_feedback["coverage_basis"] = "saved_reading_not_model_delivery"
            for position, message in enumerate(past):
                try:
                    marker = json.loads(message.get("content", ""))
                except (TypeError, ValueError):
                    continue
                if isinstance(marker, dict) and marker.get("source_context_mode") == "source_free_recovery":
                    # The exact-window feedback replaces these generic host
                    # instructions. Preserve the source-free boundary, reason,
                    # inventory and its fetch coordinates.
                    marker.pop("next_action", None)
                    marker.pop("complete_request_preview", None)
                    past[position] = {**message, "content": canonical(marker)}
            # Replace duplicated reading reminders for this copy with the
            # actionable delivery boundary; other retained sources stay visible.
            for key in ("retained_reading_progress", "source_free_reading_navigation"):
                remaining = [
                    item for item in index.get(key, [])
                    if item.get("copy_ref") != delivery_feedback["copy_ref"]
                ]
                if remaining:
                    index[key] = remaining
                else:
                    index.pop(key, None)
            index["source_read_delivery"] = delivery_feedback
        tool_compactions = []
        tool_positions = []
        conversation_dependencies = []
        for pointer in current_turns:
            exchange, compaction = self._tool_exchange(pointer, scope={
                "mission_id": mission_id, "actor_id": actor_id, "task_id": task["id"],
            }, active_skill_ids=set(config["body"].get("skills", {})))
            tool_positions.append((pointer, len(past)))
            past.extend(exchange)
            function = exchange[0].get("tool_calls", [{}])[0].get("function", {})
            origin_response = self.store.get(**pointer)
            origin_call = self.store.get(**origin_response["body"]["call_ref"])
            if ((function.get("name") in {
                    "compact_conversation", "calculate", "search_saved_passages",
                    "read_browser_search_result", "search_scholarly", "save_plan",
                }
                 or function.get("name") == "read_plan" and exchange[0].get("content") is not None
                 or "browser_search" in origin_call["body"].get("exposed_tools", []))
                    and (compaction or {}).get("status") not in {
                        "compaction_argument_withheld_due_source_use", "tool_argument_withheld_due_source_use",
                    }):
                response = self.store.get(**pointer)
                origin = self.store.get(**response["body"]["call_ref"])
                intent = self.store.get("model_request_intent", f"model-intent:{origin['id']}:{origin['version']}")
                if intent is None:
                    raise ContextError("Compaction argument intent is unavailable")
                for field in ("source_refs", "context_source_dependency_refs"):
                    pointers = intent["body"].get(field)
                    if not isinstance(pointers, list):
                        raise ContextError("Compaction argument lineage is incomplete")
                    conversation_dependencies.extend(pointers)
            if compaction:
                tool_compactions.append(compaction)
            if delivery_feedback and exchange[-1].get("name") == "read_source_window":
                result = self.store.get("context_tool_result", f"tool-result:{pointer['id']}")
                if result and ref(result) == delivery_feedback["result_ref"]:
                    original = past[-1]["content"]
                    past[-1] = {**past[-1], "content": canonical({
                        "schema": "ora.source_window_delivery.v1", **delivery_feedback,
                    })}
                    tool_compactions.append({
                        "kind": "source_window_not_delivered_for_input_fit",
                        "saved_tool_result_ref": ref(result),
                        "original_content_sha256": hashlib.sha256(original.encode()).hexdigest(),
                        "copy_ref": delivery_feedback["copy_ref"],
                        "window_ref": delivery_feedback["window_ref"],
                        "omitted_item_refs": [],
                    })
                    index["source_read_delivery"] = {
                        "result_ref": ref(result),
                        "status": delivery_feedback["status"],
                        "details_in_current_tool_result": True,
                    }
        if current_turns and control:
            pointer, position = tool_positions[-1]
            compacted = self._compact_selected_window_preview(
                pointer, past[position + 1], call=call, control=control,
                selected_source_refs=tuple(ref(item) for item in sources),
            )
            if compacted is None:
                compacted = self._compact_selected_inspection_preview(
                    pointer, past[position + 1], call=call, control=control,
                    selected_source_refs=tuple(ref(item) for item in sources),
                )
            if compacted is None and compact_selected_window:
                raise ContextError("Selected checked window changed before compact input")
            if compacted is not None:
                past[position + 1], compaction = compacted
                tool_compactions.append(compaction)
        try:
            _messages([*past, {"role": "user", "content": canonical(index)}])
        except ModelPreflightError as exc:
            raise ContextError("History has an incomplete or invalid tool exchange") from exc

        included = [call, config, current_task, *sources]
        urgent = [("request", item) for item in requests if item["body"].get("status") in active]
        urgent += [("message", item) for item in messages]
        urgent += [("request_answer", item) for item in answers]
        latency_review = read_due_review(self.store, mission_id, actor_id, task["id"])
        if (latency_review is not None
                and read_review_delivery_response(self.store, latency_review) is None):
            urgent.append(("latency_review", latency_review))
        urgent += [
            ("shared_update", item)
            for item in shared
            if item["kind"] == "board_dissent"
            or (
                item["kind"] == "peer_update_uptake"
                and item["body"].get("disposition") == "challenged"
            )
        ]
        # Recency and unresolved state are mechanical packing hints. They do not
        # rank scientific claims; every omitted detail keeps its exact fetch ref.
        urgent.sort(key=lambda pair: (pair[1]["created_at"], pair[1]["id"]), reverse=True)
        candidates = urgent
        candidates += [
            ("request", item) for item in requests if item["body"].get("status") not in active
        ]
        candidates += [("outgoing_request_message", item) for item in outgoing_request_messages]
        candidates += [
            ("shared_update", item)
            for item in shared
            if item["kind"] != "board_dissent"
            and not (
                item["kind"] == "peer_update_uptake"
                and item["body"].get("disposition") == "challenged"
            )
        ]
        candidates += [("evidence", item) for item in evidence]
        candidates += [("contribution", item) for item in contributions]
        candidates += [("note", item) for item in notes]
        if summary:
            candidates.append(("summary", summary))
        packing_details = False
        preincluded: set[int] = set()
        remaining_positions = set(range(len(candidates)))

        # Input budget: count this working view and its retrieval pointers.
        # The caller reserves space for instructions, tools and source text.
        def measure(value: dict, *, remaining_count: int | None = None) -> int | None:
            if estimate_tokens is None:
                return None
            if allowance is not None and not packing_details:
                # Reserve the exact omission-navigation envelope before deciding
                # whether optional context needs compaction. Scope/tool omissions
                # can grow during compaction, so measure their current count.
                count = ((len(remaining_positions) if remaining_count is None else remaining_count)
                         + len(routine_own_outcomes)
                         + len(archived_turns) + len(scope_omissions)
                         + sum(len(item.get("omitted_item_refs", []))
                               for item in tool_compactions))
                value = {**value, "omitted": _omission_marker(
                    count, page_fetch, value.get("older_records")
                )}
            amount = estimate_tokens(
                canonical([*past, {"role": "user", "content": canonical(value)}])
            )
            if type(amount) is not int or amount < 0:
                raise ContextError("Token estimator must return a nonnegative integer")
            return amount

        def compact_reference_index() -> int | None:
            minimum = measure(index)
            if (
                minimum is not None
                and minimum > allowance
                and index.get("routine_own_outcomes", {}).get("recent_refs")
            ):
                # These are already in the exact pageable omission list. In a
                # tight turn the count and fetch path carry the same history.
                index["routine_own_outcomes"].pop("recent_refs")
                minimum = measure(index)
            if (
                minimum is not None
                and minimum > allowance
                and index.get("source_refs")
                == [item["ref"] for item in index.get("source_retrieval", [])]
            ):
                # Every exact source pointer remains in source_retrieval.
                index.pop("source_refs")
                minimum = measure(index)
            if minimum is not None and minimum > allowance and isinstance(
                index.get("shared_materials"), dict
            ):
                # Count and exact page remain in record_counts/older_records.
                index.pop("shared_materials")
                minimum = measure(index)
            if minimum is not None and minimum > allowance:
                # Zero counts carry no extra history; remaining counts and the
                # exact older-records fetch path still describe all omissions.
                index["record_counts"] = {
                    key: count for key, count in index["record_counts"].items() if count
                }
                minimum = measure(index)
            return minimum

        if allowance is not None:
            # A small record can be cheaper than the final omission envelope.
            # Apply that existing optimization before the early required-index
            # decision too, especially for long exact inclusion identifiers.
            for position, (label, item) in enumerate(candidates):
                detail = {"type": label, "ref": ref(item), "body": item["body"]}
                proposed = {**index, "details": [*index["details"], detail]}
                remaining = remaining_positions - {position}
                if measure(proposed, remaining_count=len(remaining)) < measure(index):
                    index["details"].append(detail)
                    remaining_positions = remaining
                    included.append(item)
                    preincluded.add(position)
        if shared_materials:
            for anchor in (inclusion_id, None):
                overview = _shared_material_overview(shared_materials[0], anchor)
                with_overview = {**index, "shared_materials": {
                    "count": len(shared_materials), "overview": overview,
                    "fetch": page_fetch,
                }}
                if allowance is None or measure(with_overview) <= allowance:
                    index = with_overview
                    break
                if routine_own_outcomes:
                    # Prefer a direct reader over duplicate routine refs. If
                    # it cannot fit, preserve the smaller page-based pointer.
                    without_routine = {key: value for key, value in with_overview.items()
                                       if key != "routine_own_outcomes"}
                    if measure(without_routine) <= allowance:
                        index = without_routine
                        break
        required = measure(index)
        browser_budget_hits = []
        if (allowance is not None and required is not None and required > allowance
                and (browser := index.get("browser_search"))
                and browser.get("kind") == "browser_search_discovery"):
            browser_budget_hits = browser.get("hits", [])
            index["browser_search"] = {
                "response_receipt_ref": browser["response_receipt_ref"],
                "status": "discovery_text_omitted_for_budget",
                "next_request": {"tool": "read_browser_search_result",
                                 "response_id": browser["response_receipt_ref"]["id"],
                                 "offset": 0, "limit": 1800, "content_sha256": ""},
            }
            required = measure(index)
        if (
            allowance is not None and required is not None and required > allowance
            and (partial := index.get("partial_visible_output"))
            and "text_preview" in partial
        ):
            partial.pop("text_preview")
            partial["preview_characters"] = 0
            partial["next_offset"] = 0
            partial["next_request"] = {
                "tool": "read_context_record", "inclusion_id": inclusion_id,
                **partial["response_ref"], "offset": 0,
                "limit": MAX_CONTEXT_RECORD_WINDOW_CHARS,
            }
            partial["preview_omitted_for_budget"] = True
            required = measure(index)
        if allowance is not None and required is not None and required > allowance:
            # These are derived reminders; exact source packets and completed
            # tool-turn receipts remain available if the turn is tight.
            for key in (
                "recent_scholarly_searches", "recent_hit_inspections", "source_progress",
                "retained_reading_progress", "additional_retained_reading_copies",
            ):
                if key == "recent_scholarly_searches":
                    searches = index.get(key) or []
                    latest = searches[-1] if searches else None
                    search = latest.get("search") if isinstance(latest, dict) else None
                    if (
                        latest is not None
                        and archived_turns
                        and latest.get("response_ref") == archived_turns[-1]
                        and isinstance(search, dict)
                        and search.get("status") == "saved_tool_observation_not_evidence_use"
                    ):
                        # The just-completed lookup remains navigable after its
                        # full tool reply is archived. These are lead IDs and a
                        # saved result pointer, never source evidence or text.
                        index["latest_search_navigation"] = {
                            "response_ref": latest["response_ref"],
                            "result_ref": latest["result_ref"],
                            "route": search["route"],
                            "outcome": search["outcome"],
                            "hit_count": search["hit_count"],
                            "first_hit_ids": search["first_hit_ids"],
                            "status": "saved_search_leads_not_checked_source_text",
                        }
                        if (
                            type(search["hit_count"]) is int
                            and search["hit_count"] > len(search["first_hit_ids"])
                        ):
                            index["latest_search_navigation"]["remaining_hit_ids_fetch"] = {
                                "tool": "read_context_record", "inclusion_id": inclusion_id,
                                **latest["response_ref"], "offset": 0,
                                "limit": MAX_CONTEXT_RECORD_WINDOW_CHARS,
                            }
                index.pop(key, None)
                required = measure(index)
                if required is not None and required <= allowance:
                    break
        if allowance is not None and required is not None and required > allowance:
            last_window = index.get("last_checked_window")
            if (
                isinstance(last_window, dict)
                and last_window.get("text_status")
                == "exact_checked_passage_in_selected_source_packet"
            ):
                # The admission check above already matched this archived
                # result to the exact checked passage packet in this call.
                # Keep its saved navigation, not duplicate copy/passage refs.
                index["last_checked_window"] = {
                    key: last_window[key]
                    for key in (
                        "response_ref", "result_ref", "window_ref", "next_cursor",
                    )
                }
                index["last_checked_window"]["source_text_status"] = (
                    "checked_window_passages_in_selected_source_packet"
                )
                required = measure(index)
        if allowance is not None and required is not None and required > allowance:
            target = index.get("followup_target")
            if isinstance(target, dict) and isinstance(target.get("target_body_preview"), str):
                preview = target["target_body_preview"]
                if len(preview) > 220 and target.get("fetch_exact_parent_target"):
                    target["target_body_preview"] = preview[:220]
                    target["preview_truncated_for_budget"] = True
                    required = measure(index)
        if allowance is not None and required is not None and required > allowance:
            target = index.get("followup_target")
            if isinstance(target, dict) and parent_run_ref is not None:
                # The exact parent run and target remain pageable. Keep their
                # identity, use boundary and a small permitted target preview
                # while omitting repeated parent question/scope prose.
                index["followup_target"] = {
                    key: value for key, value in target.items()
                    if key not in {"parent_question_preview", "parent_scope_preview",
                                   "target_body_sha256", "additional_source_dependencies",
                                   "additional_unavailable_sources"}
                }
                index["followup_target"]["parent_scope_omitted_for_budget"] = True
                index["followup_target"]["fetch_full_parent_scope"] = {
                    "tool": "read_context_record", "inclusion_id": inclusion_id,
                    **parent_run_ref, "offset": 0, "limit": MAX_CONTEXT_RECORD_WINDOW_CHARS,
                }
                if not any(item.get("type") == "parent_scope_detail" for item in scope_omissions):
                    scope_omissions.append({
                        "type": "parent_scope_detail", "ref": parent_run_ref,
                        "reason": "parent_scope_compacted_for_input", "fetch": parent_run_ref,
                    })
                if index.get("run_scope", {}).get("question_ref") == index.get(
                    "run_scope", {}
                ).get("run_ref"):
                    index["run_scope"].pop("question_ref")
                required = measure(index)
        if allowance is not None and required is not None and required > allowance:
            # Exact source pointers remain in source_retrieval. Routine own
            # outcomes remain in record_counts and the pageable omission list.
            if index.get("source_refs") == [
                item["ref"] for item in index.get("source_retrieval", [])
            ]:
                index.pop("source_refs")
            if routine_own_outcomes:
                index.pop("routine_own_outcomes", None)
            if isinstance(index.get("run_scope"), dict) and index["run_scope"].get("run_ref"):
                index["run_scope"].pop("scope_sha256", None)
            required = measure(index)
            target = index.get("followup_target")
            if (required is not None and required > allowance
                    and isinstance(target, dict)
                    and isinstance(target.get("target_body_preview"), str)):
                if len(target["target_body_preview"]) > 160:
                    target["target_body_preview"] = target["target_body_preview"][:160]
                    target["preview_truncated_for_budget"] = True
                    required = measure(index)
        if allowance is not None and required is not None and required > allowance:
            # Equal selected/current source views repeat the same copy and
            # permission state. Keep the exact copy and current use references;
            # retain the full form whenever either identity or status diverges.
            compact_sources = []
            for item in index.get("source_retrieval", []):
                if (
                    item["ref"] == item["current_ref"]
                    and item["status"]
                    == item["selected_model_input_status"]
                    == item["current_model_input_status"]
                ):
                    compact_sources.append(
                        {
                            "ref": item["ref"],
                            "current_source_use_ref": item["current_source_use_ref"],
                            "status": item["status"],
                            "selected_and_current_equal": True,
                            **({"selected_input_scope": item["selected_input_scope"]}
                               if "selected_input_scope" in item else {}),
                            **({"absence_claim_scope": item["absence_claim_scope"]}
                               if "absence_claim_scope" in item else {}),
                        }
                    )
                else:
                    compact_sources.append(item)
            if compact_sources != index.get("source_retrieval", []):
                index["source_retrieval"] = compact_sources
                required = measure(index)
        if allowance is not None and required is not None and required > allowance:
            # The field name already scopes this to checked passages in this
            # call. Retain the first omitted cursor and saved-read distinction
            # while shortening repeated prose, only under pressure.
            for item in index.get("source_retrieval", []):
                scope = item.get("selected_input_scope")
                if isinstance(scope, dict) and scope.get("basis") == (
                    "checked_passages_in_this_call_not_saved_windows"
                ):
                    scope["basis"] = "checked_passages_this_call"
                if item.get("absence_claim_scope") == (
                    "For whole-source absence, consult omitted exact spans or "
                    "source-linked notes; otherwise scope to this excerpt."
                ):
                    item["absence_claim_scope"] = (
                        "Whole-source absence unverified from this selected excerpt."
                    )
            required = measure(index)
        if (
            allowance is not None
            and required is not None
            and required > allowance
            and index.get("routine_own_outcomes", {}).get("recent_refs")
        ):
            # The recent pointers are already in the exact pageable omission
            # list. Apply the same lossless compaction before the required-index
            # check, not only after building the optional-detail index.
            index["routine_own_outcomes"].pop("recent_refs")
            required = measure(index)
        if allowance is not None and required is not None and required > allowance and control:
            # The checked passage is already a separate pinned model input.
            # Keep the exact saved tool result and all navigation references,
            # but omit duplicate preview bytes only when this selection proves
            # they will enter the same call through the source bridge.
            for pointer, position in reversed(tool_positions):
                compacted = self._compact_selected_window_preview(
                    pointer, past[position + 1], call=call, control=control,
                    selected_source_refs=tuple(ref(item) for item in sources),
                )
                if compacted is None:
                    continue
                past[position + 1], compaction = compacted
                tool_compactions.append(compaction)
                required = measure(index)
                if required is not None and required <= allowance:
                    break
        if allowance is not None and required is not None and required > allowance and delivery_feedback:
            target = index.get("followup_target")
            if isinstance(target, dict) and target.get("fetch_exact_parent_target"):
                # Actionable delivery feedback takes precedence over a repeated
                # parent preview. The report and parent scope remain exact and
                # pageable; the current task and user constraints stay intact.
                index["followup_target"] = {
                    key: target[key] for key in (
                        "target_ref", "parent_run_ref", "target_text_status",
                        "dependency_lineage_complete", "unavailable_source_refs",
                        "fetch_exact_parent_target", "fetch_full_parent_scope",
                    ) if key in target
                }
                index["followup_target"]["preview_omitted_for_source_delivery_feedback"] = True
                required = measure(index)
        if allowance is not None and required is not None and required > allowance:
            # Empty request fields repeat no peer state. Keep every request's
            # exact ref and status; populated answer/disposition refs remain.
            for disposition in index.get("request_dispositions", []):
                for key in ("answer_refs", "accepted_answer_ref", "replacement_request_ref"):
                    if not disposition.get(key):
                        disposition.pop(key, None)
            # The first inventory page is saved and can be fetched by this
            # task. Under pressure, avoid repeating its host-owned first-page
            # items alongside the exact saved-window delivery feedback.
            if delivery_feedback and control and "read_source_inventory" in call["body"].get("exposed_tools", []):
                for position, message in enumerate(past):
                    try:
                        marker = json.loads(message.get("content", ""))
                    except (TypeError, ValueError):
                        continue
                    if not isinstance(marker, dict) or marker.get("source_context_mode") != "source_free_recovery":
                        continue
                    inventory = marker.get("inventory")
                    pointer = inventory.get("inventory_ref") if isinstance(inventory, dict) else None
                    if (not isinstance(pointer, dict)
                            or set(pointer) != {"kind", "id", "version"}
                            or pointer["kind"] != "mission_source_inventory"
                            or "items" not in inventory):
                        continue
                    saved_inventory = self.store.get(**pointer)
                    if saved_inventory is None or any(
                        saved_inventory["body"].get(key) != control["body"].get(key)
                        for key in ("mission_id", "run_id", "actor_id", "task_id", "source_refs")
                    ):
                        continue
                    marker["inventory"] = {
                        "inventory_ref": pointer,
                        "total": saved_inventory["body"]["count"],
                        "fetch_tool": "read_source_inventory",
                    }
                    marker["inventory_first_page_deferred_for_input_fit"] = True
                    past[position] = {**message, "content": canonical(marker)}
            target = index.get("followup_target")
            if (
                isinstance(target, dict)
                and target.get("fetch_exact_parent_target")
                and target.get("fetch_full_parent_scope")
                and any(item.get("type") == "followup_target_detail"
                        and item.get("fetch") == target.get("target_ref")
                        for item in scope_omissions)
                and any(item.get("type") == "parent_scope_detail"
                        and item.get("fetch") == target.get("parent_run_ref")
                        for item in scope_omissions)
            ):
                # Both exact records are already in the pageable omission
                # index. Retain their refs and one existing fetch coordinate;
                # a selected-source turn still keeps its permitted preview.
                target.pop("fetch_exact_parent_target")
                target.pop("fetch_full_parent_scope")
                target["parent_details_page"] = "older_records"
            required = measure(index)
        if allowance is not None and required is not None and required > allowance:
            last_window = index.get("last_checked_window")
            if (
                isinstance(last_window, dict)
                and last_window.get("source_text_status")
                == "checked_window_passages_in_selected_source_packet"
                and archived_turns
                and last_window.get("response_ref") == archived_turns[-1]
                and last_window.get("result_ref")
                == self._archived_tool_observation(archived_turns[-1]).get("result_ref")
                and control
                and self.checked_window_is_selected_input(
                    archived_turns[-1], call=call, control=control,
                    selected_source_refs=tuple(ref(item) for item in sources),
                )
            ):
                # The completed turn is in the exact older-records omission
                # list, and this selected packet carries its checked text.
                # Keep window/cursor/coverage state; page only repeated
                # response and result identities through that existing list.
                last_window.pop("response_ref")
                last_window.pop("result_ref")
                last_window["read_event_page"] = "older_records"
                required = measure(index)
        if allowance is not None and required is not None and required > allowance and current_turns:
            # A just-requested bounded record window has priority over repeated
            # navigation and parent previews. The source inventory and exact
            # parent records remain fetchable; keep the tool's scientific text.
            latest = past[tool_positions[-1][1] + 1]
            if latest.get("name") == "read_context_record" and sources:
                try:
                    current_window = json.loads(latest.get("content", ""))
                except (TypeError, ValueError):
                    current_window = None
                target = index.get("followup_target")
                if (
                    isinstance(current_window, dict)
                    and current_window.get("schema") == "ora.context_record_window.v1"
                    and current_window.get("report_text_status") == "available"
                    and isinstance(current_window.get("text"), str)
                    and isinstance(target, dict)
                    and target.get("target_ref") == current_window.get("ref")
                    and target.get("parent_details_page") == "older_records"
                ):
                    # The exact current report window is already in this input.
                    # Page its historical display metadata, while keeping source
                    # lineage and any preview text outside the delivered window.
                    compact_target = {
                        key: target[key] for key in (
                            "authority", "target_ref", "parent_run_ref",
                            "dependency_lineage_complete", "source_dependency_refs",
                        ) if key in target
                    }
                    preview = target.get("target_body_preview")
                    if (isinstance(preview, str)
                            and canonical(preview)[1:-1] not in current_window["text"]):
                        compact_target.update({
                            key: target[key] for key in (
                                "target_body_preview", "target_body_chars",
                                "preview_truncated_for_budget",
                            ) if key in target
                        })
                    compact_target.update({
                        "target_window_in_current_tool_result": True,
                        "parent_details_page": "older_records",
                    })
                    index["followup_target"] = compact_target
                    last_window = index.get("last_checked_window")
                    if (
                        isinstance(last_window, dict)
                        and last_window.get("source_text_status")
                        == "checked_window_passages_in_selected_source_packet"
                    ):
                        # source_retrieval already states this call's checked
                        # coverage; retain the saved window and continuation.
                        last_window.pop("source_text_status")
                    required = measure(index)
            if latest.get("name") == "read_context_record" and not sources:
                def decoded(content: object) -> dict:
                    try:
                        value = json.loads(content)
                    except (TypeError, ValueError):
                        return {}
                    return value if isinstance(value, dict) else {}

                inventory_ref = next(
                    (
                        message.get("inventory", {}).get("inventory_ref")
                        for message in (
                            decoded(item.get("content"))
                            for item in past if item.get("role") == "user"
                        )
                        if isinstance(message, dict)
                        and message.get("source_context_mode") == "source_free_recovery"
                        and isinstance(message.get("inventory"), dict)
                    ), None,
                )
                if inventory_ref and index.get("source_free_reading_navigation"):
                    index["source_free_reading_navigation"] = {
                        "status": "saved_reading_available_in_current_inventory",
                        "inventory_ref": inventory_ref,
                        "source_text_in_this_call": False,
                    }
                    required = measure(index)
                target = index.get("followup_target")
                target_compacted = False
                if (
                    required is not None and required > allowance
                    and isinstance(target, dict)
                    and target.get("target_ref") == decoded(latest.get("content")).get("ref")
                ):
                    index["followup_target"] = {
                        key: target[key] for key in (
                            "target_ref", "parent_run_ref", "dependency_lineage_complete",
                            "source_dependency_refs",
                        ) if key in target
                    }
                    index["followup_target"].update({
                        "target_window_in_current_tool_result": True,
                        "raw_source_text_in_this_call": False,
                    })
                    target_compacted = True
                    parent_ref = target.get("parent_run_ref")
                    if (
                        isinstance(parent_ref, dict)
                        and not any(item.get("type") == "parent_scope_detail"
                                    for item in scope_omissions)
                    ):
                        scope_omissions.append({
                            "type": "parent_scope_detail", "ref": parent_ref,
                            "reason": "parent_scope_deferred_for_requested_record_window",
                            "fetch": parent_ref,
                        })
                    required = measure(index)
                scope = index.get("run_scope")
                if (
                    target_compacted
                    and isinstance(scope, dict)
                    and isinstance(scope.get("run_ref"), dict)
                ):
                    scope_ref = scope["run_ref"]
                    index["run_scope"] = {
                        "authority": scope["authority"],
                        "run_ref": scope_ref,
                        "scope_chars": scope["scope_chars"],
                        "fetch_full_scope_and_source_refs": {
                            "tool": "read_context_record", "inclusion_id": inclusion_id,
                            **scope_ref, "offset": 0,
                            "limit": MAX_CONTEXT_RECORD_WINDOW_CHARS,
                        },
                    }
                    if not any(item.get("type") == "run_scope_detail" for item in scope_omissions):
                        scope_omissions.append({
                            "type": "run_scope_detail", "ref": scope_ref,
                            "reason": "scope_deferred_for_requested_record_window",
                            "fetch": scope_ref,
                        })
                    required = measure(index)
        if allowance is not None and required is not None and required > allowance:
            last_window = index.get("last_checked_window")
            retrieval = index.get("source_retrieval")
            target = index.get("followup_target")
            if (
                archived_turns
                and sources
                and len(sources) == 1
                and control
                and isinstance(last_window, dict)
                and last_window.get("source_text_status")
                == "checked_window_passages_in_selected_source_packet"
                and last_window.get("read_event_page") == "older_records"
                and isinstance(retrieval, list)
                and len(retrieval) == 1
                and retrieval[0].get("selected_and_current_equal") is True
                and retrieval[0].get("status") == "permitted"
                and retrieval[0].get("ref") == ref(sources[0])
                and isinstance(retrieval[0].get("selected_input_scope"), dict)
                and isinstance(target, dict)
                and target.get("parent_details_page") == "older_records"
                and self.checked_window_is_selected_input(
                    archived_turns[-1], call=call, control=control,
                    selected_source_refs=tuple(ref(item) for item in sources),
                )
            ):
                # The selected packet carries this checked text and passage
                # identity. The older-records page retains the exact read and
                # parent report; keep the cursor, use decision, excerpt scope,
                # target preview and source dependencies in this tight turn.
                view = retrieval[0]
                scope = view["selected_input_scope"]
                index["source_retrieval"] = [{
                    "ref": view["ref"],
                    "current_source_use_ref": view["current_source_use_ref"],
                    "status": view["status"],
                    "selected_input_scope": {
                        key: scope[key] for key in (
                            "status", "covered_chars", "total_chars",
                            "first_omitted_cursor", "saved_read_status",
                        ) if key in scope
                    },
                    "absence_claim_scope": "Whole-source absence unverified from this selected excerpt.",
                }]
                index["last_checked_window"] = {
                    "window_ref": last_window["window_ref"],
                    "next_cursor": last_window["next_cursor"],
                    "read_event_page": "older_records",
                }
                index["followup_target"] = {
                    key: target[key] for key in (
                        "target_ref", "parent_run_ref", "target_body_preview",
                        "source_dependency_refs", "dependency_lineage_complete", "status",
                    ) if key in target
                }
                if target.get("unavailable_source_refs"):
                    index["followup_target"]["unavailable_source_refs"] = (
                        target["unavailable_source_refs"]
                    )
                index["followup_target"]["parent_details_page"] = "older_records"
                run_scope = index.get("run_scope")
                if isinstance(run_scope, dict):
                    run_scope.pop("authority", None)  # Host index and exact run ref retain scope authority.
                    for key, value in (
                        ("additional_inherited_sources", 0),
                        ("scope_chars", 0), ("scope_preview", ""),
                        ("scope_truncated", False),
                    ):
                        if run_scope.get(key) == value:
                            run_scope.pop(key)
                required = measure(index)
        if allowance is not None and required is not None and required > allowance:
            required = compact_reference_index()
        if allowance is not None and required is not None and required > allowance:
            raise ContextOverflow(
                f"Required task, configuration and reference index needs {required} input tokens; allowance is {allowance}. Fetch smaller exact detail or repair this turn",
                required_tokens=required,
            )
        omissions = [
            {"type": label, "ref": ref(item), "reason": "input_allowance", "fetch": ref(item)}
            for label, item in candidates
        ]
        routine_omissions = [
            {
                "type": "shared_update",
                "ref": ref(item),
                "reason": "routine_own_executor_outcome_compacted",
                "fetch": ref(item),
            }
            for item in routine_own_outcomes
        ]
        history_omissions = [
            {
                "type": "completed_tool_turn",
                "ref": pointer,
                "reason": "completed_history_compacted",
                "fetch": pointer,
            }
            for pointer in archived_turns
        ]
        tool_page_omissions = [
            {
                "type": "tool_page_item",
                "ref": pointer,
                "reason": "saved_tool_page_compacted_for_input",
                "fetch": pointer,
                "source_tool_result_ref": compaction["saved_tool_result_ref"],
            }
            for compaction in tool_compactions
            for pointer in compaction.get("omitted_item_refs", [])
        ]
        packing_details = True

        def omission_view(positions: set[int]) -> dict | None:
            count = (
                len(positions)
                + len(routine_omissions)
                + len(history_omissions)
                + len(tool_page_omissions)
                + len(scope_omissions)
            )
            return _omission_marker(count, page_fetch, index.get("older_records"))

        if allowance is not None:
            index["omitted"] = omission_view(remaining_positions)
            # Some tiny details are shorter than their omission marker. Include
            # them before judging whether the exact index itself can fit.
            for position, (label, item) in enumerate(candidates):
                if position in preincluded:
                    continue
                detail = {"type": label, "ref": ref(item), "body": item["body"]}
                remaining = remaining_positions - {position}
                proposed = {
                    **index,
                    "details": [*index["details"], detail],
                    "omitted": omission_view(remaining),
                }
                if measure(proposed) < measure(index):
                    index["details"].append(detail)
                    remaining_positions = remaining
                    index["omitted"] = omission_view(remaining_positions)
                    included.append(item)
                    preincluded.add(position)
            minimum = compact_reference_index()
            if minimum is not None and minimum > allowance:
                raise ContextOverflow(
                    "Exact reference and omission index exceeds this turn's input allowance"
                )
        # A selected report or working-note window is exact working input.
        # Retain earlier checked reads when they fit, before optional board
        # metadata. Replay rechecks the original scope and current source use.
        base_past = list(past)
        insertion = tool_positions[0][1] if tool_positions else len(base_past)
        retained_exchanges: dict[int, list[dict]] = {}
        selected_windows = set()
        used_tool_call_ids = {
            call["id"] for message in base_past for call in message.get("tool_calls", [])
        }
        for message in base_past:
            if message.get("role") != "tool" or message.get("name") != "read_context_record":
                continue
            try:
                window = json.loads(message["content"])
            except (KeyError, TypeError, ValueError):
                continue
            if (isinstance(window, dict)
                    and window.get("schema") == "ora.context_record_window.v1"
                    and (window.get("report_text_status") == "available"
                         or isinstance(window.get("ref"), dict)
                         and window["ref"].get("kind") == "working_note"
                         and isinstance(window.get("text"), str)
                         and window.get("returned_chars") == len(window["text"]))):
                selected_windows.add(canonical({key: window.get(key) for key in (
                    "ref", "projection_sha256", "offset", "returned_chars",
                )}))
        for position in reversed(range(len(archived_turns))):
            pointer = archived_turns[position]
            continuation = self._retained_context_window(pointer, scope={
                "mission_id": mission_id, "actor_id": actor_id, "task_id": task["id"],
            })
            if continuation is None:
                continue
            exchange, receipt = continuation
            omission = next(item for item in history_omissions if item["ref"] == pointer)
            note_window = receipt["kind"] == "retained_working_note_window"
            reason_prefix = "working_note_window" if note_window else "report_window"
            identity = canonical({
                "ref": receipt["note_ref" if note_window else "report_ref"],
                **{key: receipt[key] for key in ("projection_sha256", "offset", "returned_chars")},
            })
            if not exchange:
                omission["reason"] = reason_prefix + "_source_use_unavailable"
            elif identity in selected_windows:
                receipt["status"] = "same_exact_window_already_in_input"
            elif exchange[0]["tool_calls"][0]["id"] in used_tool_call_ids:
                # IDs need only have been unique in their original requests.
                # Do not rename saved calls or block current work when an old
                # optional exchange cannot share the current protocol history.
                omission["reason"] = reason_prefix + "_tool_call_id_conflict"
                receipt["status"] = "omitted_due_tool_call_id_conflict"
            else:
                trial = {**retained_exchanges, position: exchange}
                past = [*base_past[:insertion],
                        *(message for order in sorted(trial) for message in trial[order]),
                        *base_past[insertion:]]
                history_omissions.remove(omission)
                proposed = {**index, "omitted": omission_view(remaining_positions)}
                size = measure(proposed)
                if allowance is not None and size is not None and size > allowance:
                    # Prefer a previously read exact window over repeat catalog
                    # metadata and empty counters. Keep the same checked scope,
                    # source use and omission navigation; roll back a trial that
                    # still cannot fit without disturbing other selected text.
                    original_index = index
                    index = deepcopy(proposed)
                    size = compact_reference_index()
                    proposed = index
                    index = original_index
                if allowance is None or size is not None and size <= allowance:
                    retained_exchanges = trial
                    selected_windows.add(identity)
                    used_tool_call_ids.add(exchange[0]["tool_calls"][0]["id"])
                    index = proposed
                    included.extend((self.store.get(**pointer),
                                     self.store.get(**receipt["saved_tool_result_ref"])))
                    receipt["status"] = "included_exact_window"
                else:
                    history_omissions.append(omission)
                    omission["reason"] = reason_prefix + "_omitted_for_input_allowance"
                    receipt["status"] = "omitted_for_input_allowance"
                    past = [*base_past[:insertion],
                            *(message for order in sorted(retained_exchanges)
                              for message in retained_exchanges[order]),
                            *base_past[insertion:]]
            tool_compactions.append(receipt)
        history_omissions.sort(key=lambda item: archived_turns.index(item["ref"]))
        if shared_materials and "shared_materials" not in index:
            # A tight turn may first shed routine context and duplicate tool
            # history. Retry the smaller peer pointer against the resulting
            # measured input, before spending space on optional details.
            for anchor in (inclusion_id, None):
                overview = {**index, "shared_materials": {
                    "count": len(shared_materials),
                    "overview": _shared_material_overview(shared_materials[0], anchor),
                    "fetch": page_fetch,
                }}
                size = measure(overview)
                if allowance is None or size is not None and size <= allowance:
                    index = overview
                    break
        # Every pointer remains in the exact omission page. Show a few safe
        # host-checked metadata entries only if the complete context still fits;
        # neither report prose nor source bytes enter as optional candidates.
        for entry in shared_materials[:MAX_SHARED_MATERIAL_PREVIEWS]:
            proposed = {**index, "shared_materials": {
                "count": len(shared_materials),
                "entries": [*index.get("shared_materials", {}).get("entries", []),
                            _shared_material_reader_view(entry, inclusion_id)],
                "older_records": page_fetch,
            }}
            size = measure(proposed)
            if allowance is None or size is not None and size <= allowance:
                index = proposed
        # Keep actionable discovery pointers ahead of optional board detail.
        # These were verified against the exact response and current task above;
        # their linked pages remain uninspected. All omitted leads stay readable.
        for hit in browser_budget_hits:
            view = index["browser_search"]
            proposed = {**index, "browser_search": {
                **view,
                "source_inspection": "not_performed", "access_surface": "metadata",
                "available_hit_count": len(browser_budget_hits),
                "hits": [*view.get("hits", []), {
                    "hit_id": hit["hit_id"], "original_url": hit["original_url"],
                }],
            }}
            size = measure(proposed)
            if allowance is None or size is not None and size <= allowance:
                index = proposed
            else:
                break
        # Add optional detail only when it fits; the remaining records stay
        # saved and appear in the omission list with a way to retrieve them.
        for position, (label, item) in enumerate(candidates):
            if position in preincluded:
                continue
            detail = {"type": label, "ref": ref(item), "body": item["body"]}
            if allowance is None:
                index["details"].append(detail)
                included.append(item)
                remaining_positions.remove(position)
                continue
            remaining = remaining_positions - {position}
            proposed = {
                **index,
                "details": [*index["details"], detail],
                "omitted": omission_view(remaining),
            }
            size = measure(proposed)
            if size is not None and size <= allowance:
                index["details"].append(detail)
                remaining_positions = remaining
                index["omitted"] = omission_view(remaining_positions)
                included.append(item)
        order = {
            (label, item["kind"], item["id"], item["version"]): position
            for position, (label, item) in enumerate(candidates)
        }
        index["details"].sort(
            key=lambda entry: order[
                (entry["type"], entry["ref"]["kind"], entry["ref"]["id"], entry["ref"]["version"])
            ]
        )
        index["omitted"] = omission_view(remaining_positions)
        try:
            _messages([*past, {"role": "user", "content": canonical(index)}])
        except ModelPreflightError as exc:
            raise ContextError("Retained report history has an invalid tool exchange") from exc
        final_size = measure(index)
        if allowance is not None and final_size is not None and final_size > allowance:
            raise ContextOverflow("Omission index itself exceeds the caller's input allowance")
        visible_note_refs = [
            ref(item) for item in included
            if item["kind"] == "working_note" and isinstance(item["body"].get("text"), str)
        ]
        visible_note_refs.extend(
            pointer
            for message in past if message.get("role") == "tool"
            for pointer in self._visible_note_refs(message)
        )
        return ContextPack(
            call_ref,
            ref(config),
            ref(current_task),
            _unique(sources),
            [*past, {"role": "user", "content": canonical(index)}],
            _unique(included),
            tuple(omissions[position] for position in sorted(remaining_positions))
            + tuple(routine_omissions)
            + tuple(history_omissions)
            + tuple(tool_page_omissions)
            + tuple(scope_omissions),
            cursor,
            event_through,
            final_size,
            tuple(tool_compactions),
            run_ref,
            parent_run_ref,
            followup_target_ref,
            tuple({(item["kind"], item["id"], item["version"]): item
                   for item in visible_note_refs}.values()),
            tuple({(item["kind"], item["id"], item["version"]): item
                   for item in conversation_dependencies}.values()),
        )

    # Omitted context: list or read saved details in bounded pages, checking the
    # original peer and task before returning any supported record text.
    def read_omitted_page(
        self,
        *,
        mission_id: str,
        actor_id: str,
        task_id: str,
        inclusion_ref: dict,
        cursor: int,
        limit: int,
    ) -> dict:
        """Page exact omissions for the named peer without exposing source bytes.

        The host supplies mission, actor and task from the claimed work. The
        model can choose only the saved inclusion and page cursor.
        """
        if type(cursor) is not int or cursor < 0:
            raise ContextError("Context page cursor must be a nonnegative integer")
        if type(limit) is not int or limit <= 0:
            raise ContextError("Context page limit must be a positive integer")
        if (
            type(inclusion_ref) is not dict
            or set(inclusion_ref) != {"kind", "id", "version"}
            or inclusion_ref["kind"] != "context_inclusion"
        ):
            raise ContextError("An exact context inclusion reference is required")
        inclusion = self.store.get(**inclusion_ref)
        if inclusion is None or inclusion["body"].get("mission_id") != mission_id:
            raise ContextError("Context inclusion is unavailable in this mission")
        saved = inclusion["body"]
        call = self.store.get(**saved["call_ref"])
        config = self.store.get(**saved["configuration_ref"])
        task = self.store.get(**saved["task_ref"])
        if (
            call is None
            or config is None
            or task is None
            or call["body"].get("mission_id") != mission_id
            or call["body"].get("instance_id") != actor_id
            or call["body"].get("task_id") != task_id
            or task["id"] != task_id
            or task["body"].get("mission_id") != mission_id
            or call["body"].get("configuration_id") != config["id"]
            or call["body"].get("configuration_sha256")
            != config["body"].get("configuration_sha256")
        ):
            raise ContextError("Context inclusion belongs to another peer or task")
        omissions = saved.get("omitted")
        if not isinstance(omissions, list) or cursor > len(omissions):
            raise ContextError("Saved omission page is unavailable")
        effective_limit = min(limit, MAX_CONTEXT_PAGE_ITEMS)
        stop = min(cursor + effective_limit, len(omissions))
        items = []

        def page(items: list[dict]) -> dict:
            end = cursor + len(items)
            return {
                "schema": "ora.context_omission_page.v2",
                "inclusion_ref": inclusion_ref,
                "cursor": cursor,
                "requested_limit": limit,
                "effective_limit": effective_limit,
                "returned_count": len(items),
                "next_cursor": end if end < len(omissions) else None,
                "total": len(omissions),
                "items": items,
            }

        def preview(value: object) -> dict | None:
            if not isinstance(value, str):
                return None
            return {"text": value[:300], "total_chars": len(value), "truncated": len(value) > 300}

        def authorized_followup(pointer: dict, omission_type: str) -> bool:
            run_ref = saved.get("run_ref")
            run = self.store.get(**run_ref) if isinstance(run_ref, dict) else None
            if run is None or run["body"].get("mission_id") != mission_id:
                return False
            if omission_type == "run_scope_detail":
                return pointer == run_ref
            parent_ref = saved.get("parent_run_ref")
            parent = self.store.get(**parent_ref) if isinstance(parent_ref, dict) else None
            if parent is None or run["body"].get("parent_run_id") != parent["id"]:
                return False
            if omission_type == "parent_scope_detail":
                return pointer == parent_ref
            if omission_type != "followup_target_detail":
                return False
            snapshot = self.store.get(
                "research_snapshot", run["body"].get("parent_snapshot_id"), 1
            )
            return bool(
                pointer == saved.get("followup_target_ref") == run["body"].get("target_ref")
                and snapshot
                and snapshot["body"].get("mission_id") == parent["body"].get("mission_id")
                and pointer in snapshot["body"].get("inspection", {}).get("acceptedOutputReferences", [])
                and record["body"].get("mission_id") == parent["body"].get("mission_id")
            )

        for omission in omissions[cursor:stop]:
            pointer = omission.get("fetch")
            record = self.store.get(**pointer) if isinstance(pointer, dict) else None
            if record is None or ref(record) != pointer:
                raise ContextError("An exact omitted record is unavailable")
            body = record["body"]
            kind = record["kind"]
            partial_probe = None
            shared_entry = None
            if omission.get("type") == "shared_material":
                shared_entry = shared_material_entry(
                    self.store, mission_id=mission_id, actor_id=actor_id,
                    task_id=task_id, pointer=pointer,
                )
                # The exact inclusion already fixed this pointer. If current
                # authority later disappears, keep the page cursor usable and
                # show only a generic unavailable marker for this saved slot.
                permitted = kind in {"retrieval_hit", "report"}
            elif kind in {"mail_message", "mail_request"}:
                permitted = (
                    actor_id
                    in {
                        body.get("sender_ref", {}).get("id"),
                        body.get("recipient_ref", {}).get("id"),
                    }
                    if kind == "mail_message"
                    else actor_id in {body.get("requester_id"), body.get("recipient_id")}
                )
            elif kind == "mail_answer":
                request = self.store.get("mail_request", body.get("request_id"))
                permitted = bool(
                    request
                    and actor_id
                    in {request["body"].get("requester_id"), request["body"].get("recipient_id")}
                )
            elif kind == "working_note":
                permitted = body.get("actor_id") == actor_id and body.get("task_id") == task_id
            elif kind == "context_summary":
                permitted = body.get("actor_id") == actor_id and body.get("task_id") == task_id
            elif kind == "mission_host_diagnostic" and omission.get("type") == "latency_review":
                permitted = (
                    body.get("category") == "latency_review_due"
                    and body.get("actor_id") == actor_id
                    and body.get("task_id") == task_id
                )
            elif kind == "model_response_receipt" and omission.get("type") == "completed_tool_turn":
                call_pointer = body.get("call_ref")
                saved_call = (
                    self.store.get(**call_pointer) if isinstance(call_pointer, dict) else None
                )
                result = self.store.get("context_tool_result", f"tool-result:{record['id']}")
                permitted = bool(
                    body.get("task_id") == task_id
                    and saved_call
                    and saved_call["body"].get("instance_id") == actor_id
                    and result
                    and result["body"].get("response_ref") == pointer
                )
            elif kind == "model_response_receipt" and omission.get("type") == "partial_visible_output":
                try:
                    partial_probe = read_partial_output_window(
                        self.store, pointer,
                        mission_id=mission_id, task_id=task_id, actor_id=actor_id,
                        current_call_source_refs=saved.get("source_refs", []), limit=1,
                    )
                    permitted = True
                except OutputRecoveryError:
                    permitted = False
            elif kind in {"mission_run", "report"} and omission.get("type") in {
                "run_scope_detail", "parent_scope_detail", "followup_target_detail"
            }:
                permitted = authorized_followup(pointer, omission["type"])
            elif kind in {"board_dissent", "board_outcome"}:
                permitted = body.get("proposal_ref", {}).get("id") == task_id
            elif kind == "peer_update_uptake":
                update_ref = body.get("update_ref")
                update = self.store.get(**update_ref) if isinstance(update_ref, dict) else None
                subject = (
                    (update["body"].get("task_ref") or update["body"].get("proposal_ref") or {})
                    if update
                    else {}
                )
                permitted = bool(
                    update
                    and (
                        subject.get("id") == task_id
                        or update["kind"] == "board_proposal"
                        and update["id"] == task_id
                    )
                )
            else:
                permitted = kind in {
                    "source_copy",
                    "source_passage",
                    "retrieval_hit",
                    "publication",
                    "study_family",
                    "source_encounter",
                }
            foreign_followup = kind in {"mission_run", "report"} and omission.get("type") in {
                "parent_scope_detail", "followup_target_detail"
            }
            if not permitted or not foreign_followup and body.get("mission_id") not in {None, mission_id}:
                raise ContextError("Omitted record is outside this peer's scope")
            summary: dict = {"kind": kind}
            if omission.get("type") == "shared_material":
                if shared_entry is None:
                    summary["status"] = "shared_material_unavailable"
                else:
                    shared_view = _shared_material_reader_view(shared_entry, inclusion["id"])
                    summary.update({key: value for key, value in shared_view.items()
                                    if key != "ref"})
                    if isinstance(summary.get("title"), str):
                        summary["title"] = preview(summary["title"])
            elif kind == "mail_request":
                summary.update(
                    status=body.get("status"),
                    question=preview(body.get("question")),
                    answer_count=len(body.get("answer_refs", [])),
                    recent_answer_refs=body.get("answer_refs", [])[-2:],
                )
            elif kind == "mail_message":
                summary.update(
                    request_id=body.get("request_id"),
                    sender_ref=body.get("sender_ref"),
                    delivered=bool(body.get("delivered_at")),
                )
            elif kind == "mail_answer":
                summary.update(artifact_ref=body.get("artifact_ref"), partial=body.get("partial"))
            elif kind == "mission_host_diagnostic" and omission.get("type") == "latency_review":
                summary.update(
                    category=body["category"], message=preview(body.get("message")),
                    recorded_at=body.get("recorded_at"),
                    expectation_ref=body.get("expectation_ref"),
                )
            elif kind == "board_dissent":
                summary.update(
                    proposal_ref=body.get("proposal_ref"), critique=preview(body.get("critique"))
                )
            elif kind == "board_outcome":
                summary.update(
                    proposal_ref=body.get("proposal_ref"),
                    result_count=len(body.get("result_refs", [])),
                    recent_result_refs=body.get("result_refs", [])[-2:],
                    completes_task=body.get("completes_task"),
                )
            elif kind == "peer_update_uptake":
                summary.update(
                    update_ref=body.get("update_ref"),
                    disposition=body.get("disposition"),
                    statement=preview(body.get("statement")),
                )
            elif kind == "working_note":
                projected = self.working_note_projection(record)["body"]
                summary.update(category=projected.get("category"))
                if "text" in projected:
                    summary["text"] = preview(projected["text"])
                else:
                    summary.update({key: projected[key] for key in (
                        "status", "source_dependency_refs", "unavailable_source_refs",
                        "dependency_lineage_complete", "next_action",
                    )})
            elif kind == "context_summary":
                try:
                    access = self.conversation_summary_access(
                        record, mission_id=mission_id, actor_id=actor_id, task_id=task_id,
                    )
                except ContextError:
                    access = {"status": "source_lineage_unavailable",
                              "source_dependency_refs": [], "unavailable_source_refs": []}
                summary.update(status=access["status"],
                               source_dependency_refs=access["source_dependency_refs"][:4],
                               unavailable_source_refs=access["unavailable_source_refs"][:4])
            elif kind == "model_response_receipt":
                if omission.get("type") == "partial_visible_output":
                    summary.update(
                        status=partial_probe["status"],
                        response_ref=pointer,
                        authority="saved_incomplete_visible_output_not_an_accepted_result",
                    )
                    if partial_probe["status"] == "available":
                        summary["total_characters"] = partial_probe["total_characters"]
                        summary["content_sha256"] = partial_probe["content_sha256"]
                else:
                    observation = self._archived_tool_observation(pointer)
                    summary.update(status="completed", **{
                        key: value for key, value in observation.items()
                        if key in {"tool_call_id", "tool_name", "result_ref", "effect_ref"}
                    })
                    if search := observation.get("search"):
                        summary["search"] = {
                            key: search[key]
                            for key in (
                                "route", "query_preview", "query_sha256", "outcome",
                                "hit_count", "status", "source_dependency_refs",
                                "unavailable_source_refs",
                            )
                            if key in search
                        }
                    if inspection := observation.get("inspection"):
                        summary["inspection"] = inspection
            elif kind == "mission_run":
                summary.update(question=preview(body.get("question")),
                               scope_chars=len(body.get("scope", "")))
            elif kind == "report":
                access = (
                    self._followup_target_text_access(inclusion, record)
                    if omission.get("type") == "followup_target_detail"
                    else self._same_mission_report_text_access(inclusion, record)
                )
                summary.update(
                    target_text_status=access["status"],
                    source_dependency_refs=access["source_dependency_refs"][:4],
                    additional_source_dependencies=max(
                        0, len(access["source_dependency_refs"]) - 4
                    ),
                )
                if access["status"] == "available":
                    summary["title"] = preview(body.get("title"))
                summary["body_chars"] = len(body.get("body", ""))
            else:
                summary["status"] = "exact_reference_only_use_authorized_source_reader"
            item = {"type": omission["type"], "ref": pointer, "summary": summary}
            items.append(item)
            tentative = page(items)
            if len(canonical(tentative)) > MAX_CONTEXT_PAGE_CHARS:
                items.pop()
                if not items:
                    # Exact ref remains fetchable; a single oversized summary
                    # cannot consume the model's whole current tool turn.
                    item["summary"] = {"kind": kind, "status": "summary_deferred_by_page_budget"}
                    items.append(item)
                    tentative["items"] = items
                    if len(canonical(tentative)) > MAX_CONTEXT_PAGE_CHARS:
                        raise ContextOverflow(
                            "One exact page reference exceeds the reply allowance"
                        )
                break
        return page(items)

    def read_omitted_record(
        self,
        *,
        mission_id: str,
        actor_id: str,
        task_id: str,
        inclusion_ref: dict,
        pointer: dict,
        offset: int,
        limit: int,
    ) -> dict:
        """Read a bounded safe projection of one exact omitted non-source record."""
        if type(pointer) is not dict or set(pointer) != {"kind", "id", "version"}:
            raise ContextError("An exact omitted record reference is required")
        if type(offset) is not int or offset < 0:
            raise ContextError("Context record offset must be a nonnegative integer")
        if type(limit) is not int or limit <= 0:
            raise ContextError("Context record limit must be a positive integer")
        inclusion = self.store.get(**inclusion_ref)
        if inclusion is None or inclusion["kind"] != "context_inclusion":
            raise ContextError(
                "Context inclusion is unavailable. Copy inclusion_id from read_request "
                "or older_records in the context index; put the target record ID in id."
            )
        omitted = inclusion["body"].get("omitted", [])
        position = next((n for n, item in enumerate(omitted) if item.get("fetch") == pointer), None)
        linked_report = False
        if position is None and pointer["kind"] == "report":
            for number, item in enumerate(omitted):
                anchor_ref = item.get("fetch")
                anchor = self.store.get(**anchor_ref) if isinstance(anchor_ref, dict) else None
                if anchor is None:
                    continue
                anchor_body = anchor["body"]
                if (
                    anchor["kind"] == "board_outcome"
                    and pointer in anchor_body.get("result_refs", [])
                ) or (
                    anchor["kind"] == "mail_answer" and pointer == anchor_body.get("artifact_ref")
                ):
                    position, linked_report = number, True
                    break
        if position is None:
            raise ContextError("Record was not omitted from this exact model input")
        self.read_omitted_page(
            mission_id=mission_id,
            actor_id=actor_id,
            task_id=task_id,
            inclusion_ref=inclusion_ref,
            cursor=position,
            limit=1,
        )
        record = self.store.get(**pointer)
        if record is None or (
            linked_report
            and (record["kind"] != "report" or record["body"].get("mission_id") != mission_id)
        ):
            raise ContextError("Linked report is unavailable in this mission")
        body = record["body"]
        kind = record["kind"]
        if omitted[position].get("type") == "shared_material" and shared_material_entry(
            self.store, mission_id=mission_id, actor_id=actor_id,
            task_id=task_id, pointer=pointer,
        ) is None:
            raise ContextError("Shared material is no longer available to this task")
        if kind == "model_response_receipt" and omitted[position].get("type") == "partial_visible_output":
            try:
                result = read_partial_output_window(
                    self.store, pointer,
                    mission_id=mission_id, task_id=task_id, actor_id=actor_id,
                    current_call_source_refs=inclusion["body"].get("source_refs", []),
                    offset=offset, limit=min(limit, MAX_CONTEXT_RECORD_WINDOW_CHARS),
                )
            except OutputRecoveryError as exc:
                raise ContextError("Saved partial output cannot be read for this peer") from exc
            return {
                "schema": "ora.partial_visible_output_window.v1",
                "requested_limit": limit,
                "effective_limit": min(limit, MAX_CONTEXT_RECORD_WINDOW_CHARS),
                **result,
            }
        fields = {
            "board_dissent": ("proposal_ref", "critique", "evidence_refs"),
            "board_outcome": ("proposal_ref", "result_refs", "completes_task"),
            "peer_update_uptake": ("update_ref", "disposition", "statement", "evidence_refs"),
            "mail_request": (
                "question",
                "status",
                "answer_refs",
                "accepted_answer_ref",
                "expected_artifact",
                "cause_ref",
                "message_ref",
            ),
            "mail_answer": ("artifact_ref", "partial", "request_id"),
            "working_note": ("category", "text", "input_refs"),
            "context_summary": ("text", "input_refs", "source_refs"),
            "mission_host_diagnostic": (
                "category", "expectation_ref", "control_ref", "recorded_at",
                "review_after_seconds", "elapsed_since_expectation_seconds",
                "phase", "waiting_reason", "wait_kind", "capacity_wait_until",
                "last_progress_at", "message",
            ),
            "mission_run": ("question", "scope", "source_refs", "parent_run_id",
                            "parent_snapshot_id", "target_ref"),
            "report": ("title", "body", "citations", "limitations", "producer_attempt_ref"),
        }.get(kind)
        if kind == "report":
            access = (
                self._followup_target_text_access(inclusion, record)
                if omitted[position].get("type") == "followup_target_detail"
                else self._shared_report_text_access(inclusion, record)
                if omitted[position].get("type") == "shared_material"
                else self._same_mission_report_text_access(inclusion, record)
            )
            projection = (
                {key: body[key] for key in fields if key in body}
                if access["status"] == "available"
                else {"status": access["status"], "report_ref": pointer,
                      "source_dependency_refs": access["source_dependency_refs"],
                      "unavailable_source_refs": access["unavailable_source_refs"],
                      "dependency_lineage_complete": access["dependency_lineage_complete"],
                      "next_action": "Inspect current source use and lineage; reissue this exact read after access is resolved."}
            )
        elif kind == "retrieval_hit" and omitted[position].get("type") == "shared_material":
            # A page-listed hit is a lead. Source text still requires the
            # ordinary inspect_hit path and its current-use/intake checks.
            projection = shared_material_entry(
                self.store, mission_id=mission_id, actor_id=actor_id,
                task_id=task_id, pointer=pointer,
            )
            if projection is None:
                raise ContextError("Shared hit is no longer available to this task")
            projection = shared_material_view(projection)
        elif kind == "model_response_receipt" and not linked_report:
            projection = self._archived_tool_observation(pointer)
            if projection.get("search", {}).get("outcome") in {"success", "empty"}:
                hit_ids = self._saved_search_hit_ids(pointer, projection)
                projection["search"]["saved_hit_ids"] = hit_ids
                projection["search"]["saved_hit_count"] = len(hit_ids)
                projection["search"]["saved_hit_status"] = (
                    "verified_same_task_leads_not_checked_source_text"
                )
        elif kind == "context_summary":
            try:
                access = self.conversation_summary_access(
                    record, mission_id=mission_id, actor_id=actor_id, task_id=task_id,
                )
            except ContextError:
                access = {"status": "source_lineage_unavailable",
                          "source_dependency_refs": [], "unavailable_source_refs": []}
            projection = (
                {key: body[key] for key in fields if key in body}
                if access["status"] == "available"
                else {"status": access["status"], "summary_ref": pointer,
                      "source_dependency_refs": access["source_dependency_refs"],
                      "unavailable_source_refs": access["unavailable_source_refs"]}
            )
        elif fields is not None:
            if kind == "working_note":
                body = self.working_note_projection(record)["body"]
            projection = {key: body[key] for key in fields if key in body}
            if kind == "working_note" and "text" not in body:
                projection.update({key: body[key] for key in (
                    "status", "source_dependency_refs", "unavailable_source_refs",
                    "dependency_lineage_complete", "next_action",
                )})
        else:
            raise ContextError(
                "Raw source, peer payload or private record is unavailable through this tool"
            )
        encoded = canonical(projection)
        if offset > len(encoded):
            raise ContextError("Text cursor exceeds the exact record projection")
        effective_limit = min(limit, MAX_CONTEXT_RECORD_WINDOW_CHARS)
        end = min(offset + effective_limit, len(encoded))
        return {
            "schema": "ora.context_record_window.v1",
            "ref": pointer,
            "projection_sha256": hashlib.sha256(encoded.encode()).hexdigest(),
            "offset": offset,
            "requested_limit": limit,
            "effective_limit": effective_limit,
            "returned_chars": end - offset,
            "next_offset": end if end < len(encoded) else None,
            **({"next_request": {"tool": "read_context_record",
                                 "inclusion_id": inclusion["id"], **pointer,
                                 "offset": end, "limit": limit}}
               if end < len(encoded) else {}),
            "total_chars": len(encoded),
            "text": encoded[offset:end],
            **({
                "report_text_status": access["status"],
                "source_dependencies_omitted_from_input": access.get(
                    "source_dependencies_omitted_from_input", []
                ),
            } if kind == "report" else {}),
        }

    # Preparation receipt: compare the working view with the exact saved request
    # before recording inclusion. Provider execution needs separate evidence.
    def save_inclusion(self, operation_id: str, pack: ContextPack, intent_ref: dict) -> dict:
        """Bind prepared context to actual saved request bytes, without claiming uptake."""
        intent = self.store.get(**intent_ref)
        if intent is None or intent["kind"] != "model_request_intent":
            raise ContextError("Saved model request intent is unavailable")
        body = intent["body"]
        if (
            body.get("call_ref") != pack.call_ref
            or body.get("configuration_ref") != pack.configuration_ref
            or tuple(body.get("source_refs", [])) != pack.source_refs
        ):
            raise ContextError("Prepared model call differs from context selection")
        self.require_current_source_use(pack.source_refs)
        actual = json.loads(self.store.read_blob(body["blob"]["sha256"]))
        if actual.get("messages", [])[-len(pack.messages) :] != pack.messages:
            raise ContextError("Prepared model messages differ from assembled context")
        dependency_refs = self.context_source_dependency_refs(pack)
        if tuple(body.get("context_source_dependency_refs", [])) != dependency_refs:
            raise ContextError("Prepared source dependencies differ from assembled context")
        context_index = json.loads(pack.messages[-1]["content"])
        if context_index.get("older_records", {}).get("inclusion_id") != f"context:{intent['id']}":
            raise ContextError("Context page reference differs from saved intent")
        prefix = actual["messages"][: -len(pack.messages)]
        config = self.store.get(**pack.configuration_ref)
        if (
            config is None
            or not prefix
            or prefix[0] != {"role": "developer", "content": config["body"].get("instructions")}
        ):
            raise ContextError("Prepared model instructions differ from pinned configuration")
        if pack.source_refs:
            if len(prefix) != 2 or prefix[1].get("role") != "user":
                raise ContextError("Prepared source packets are missing")
            try:
                packets = json.loads(prefix[1]["content"])["source_packets"]
            except (TypeError, ValueError, KeyError) as exc:
                raise ContextError("Prepared source packets are malformed") from exc
            if not isinstance(packets, list) or [
                (item.get("id"), item.get("version")) for item in packets
            ] != [(item["id"], item["version"]) for item in pack.source_refs]:
                raise ContextError("Prepared source packets differ from exact copy selection")
            for item in packets:
                if "text" in item and hashlib.sha256(item["text"].encode()).hexdigest() != item.get(
                    "sha256"
                ):
                    raise ContextError("Prepared source text differs from its checksum")
            progress = context_index.get("source_progress")
            if progress:
                decisions = body.get("source_decisions", [])
                for item in progress:
                    selected = next(
                        (
                            decision.get("passage_refs", []) for decision in decisions
                            if decision.get("copy_ref") == item.get("copy_ref")
                        ),
                        None,
                    )
                    if (
                        not isinstance(selected, list)
                        or len(selected) != item.get("selected_passage_count")
                        or selected[:4] != [span.get("ref") for span in item.get("selected_spans", [])]
                    ):
                        raise ContextError("Source progress differs from prepared passage selection")
        elif len(prefix) != 1:
            raise ContextError("Prepared model input has unaccounted source material")
        if _sha(actual) != body.get("request_sha256"):
            raise ContextError("Prepared request hash differs from retained bytes")
        receipt = {
            "schema": "ora.context_inclusion.v1",
            "packer_version": PACKER_VERSION,
            "mission_id": body["mission_id"],
            "call_ref": pack.call_ref,
            "intent_ref": intent_ref,
            "configuration_ref": pack.configuration_ref,
            "task_ref": pack.task_ref,
            "source_refs": list(pack.source_refs),
            "run_ref": pack.run_ref,
            "parent_run_ref": pack.parent_run_ref,
            "followup_target_ref": pack.followup_target_ref,
            "included_refs": list(pack.included_refs),
            "omitted": list(pack.omitted),
            "inbox_cursor": pack.inbox_cursor,
            "event_through": pack.event_through,
            "message_sha256": _sha(pack.messages),
            "request_sha256": body["request_sha256"],
            "estimated_context_tokens": pack.estimated_tokens,
            "tool_result_compactions": list(pack.tool_result_compactions),
            "note_text_refs": list(pack.note_text_refs),
            "context_source_dependency_refs": list(dependency_refs),
            "returned_reasoning_policy": "saved_restricted_not_replayed",
            "status": "prepared_input_not_scientific_uptake",
        }

        def save(tx: Transaction) -> dict:
            saved_intent = tx.get(**intent_ref)
            if saved_intent != intent:
                raise ContextError("Model request changed before inclusion receipt")
            record = tx.put("context_inclusion", f"context:{intent['id']}", receipt)
            tx.link(record, saved_intent, "included_in")
            if pack.run_ref:
                tx.link(record, require_ref(tx, body["mission_id"], pack.run_ref), "about_run")
            actor_id = body.get("instance_id")
            actor = (
                tx.get("agent_instance", actor_id, 1)
                if isinstance(actor_id, str) else None
            )
            run_id = actor["body"].get("run_id") if actor else None
            for pointer in pack.included_refs:
                source = (
                    tx.get(**pointer)
                    if pointer.get("kind") == "source_copy"
                    and isinstance(run_id, str)
                    and inherited_public_source(
                        tx, mission_id=body["mission_id"], run_id=run_id,
                        copy_ref=pointer, actor_id=actor_id,
                        task_id=body.get("task_id"),
                    )
                    else require_ref(tx, body["mission_id"], pointer)
                )
                tx.link(record, source, "input")
            return ref(record)

        return self.store.transact(operation_id, receipt, save, mission_id=body["mission_id"])
