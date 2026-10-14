"""Check which saved sources a report depends on and whether its text may be reused.

Trace the report back to the model call, prepared request, included context and
cited passages that produced it. Return an access status or reject acceptance
when required links are missing or current source permissions prevent reuse.
These checks establish traceability and permitted use, not whether the report's
scientific conclusions are correct.
"""

from __future__ import annotations

import hashlib

from ora.agents.capabilities import content_hash
from ora.coordination.board import ref
from ora.models.session import SessionError, _current_context_dependency_use
from ora.state import StateError, StateStore


class ReportLineageError(StateError):
    """A report cannot prove the complete source ancestry of its text."""


class ReportSourceUnavailable(StateError):
    """A source behind a report no longer permits model-text use."""


def accepted_report(store: StateStore, control: dict, mission_id: str) -> dict | None:
    """Follow a completed task's exact board outcome to its accepted report."""
    body = control["body"]
    report_ref = body.get("report_ref")
    report = store.get(**report_ref) if (
        isinstance(report_ref, dict) and report_ref.get("kind") == "report"
    ) else None
    if (body.get("phase") != "done" or report is None
            or report["body"].get("mission_id") != mission_id):
        return None
    proposal = store.get("board_proposal", body["task_id"])
    if (proposal is None or proposal["body"].get("mission_id") != mission_id
            or proposal["body"].get("status") != "completed"):
        return None
    outcome_ref = proposal["body"].get("outcome_ref")
    if not isinstance(outcome_ref, dict) or outcome_ref.get("kind") != "board_outcome":
        return None
    outcome = store.get(**outcome_ref)
    if (outcome is None or outcome["body"].get("mission_id") != mission_id
            or (outcome["body"].get("proposal_ref") or {}).get("id") != body["task_id"]):
        return None
    for pointer in outcome["body"].get("result_refs", []):
        if pointer == ref(report):
            return report
        if pointer.get("kind") == "executor_artifact":
            artifact = store.get(**pointer)
            if (artifact and artifact["body"].get("mission_id") == mission_id
                    and ref(report) in artifact["body"].get("output_refs", [])):
                return report
    return None


def _record(reader, pointer: object, kind: str) -> dict:
    if (type(pointer) is not dict or set(pointer) != {"kind", "id", "version"}
            or pointer.get("kind") != kind
            or type(pointer.get("id")) is not str or not pointer["id"]
            or type(pointer.get("version")) is not int or pointer["version"] < 1):
        raise ReportLineageError(f"Exact {kind} reference is required")
    saved = reader.get(**pointer)
    if saved is None or ref(saved) != pointer:
        raise ReportLineageError(f"Exact {kind} record is unavailable")
    return saved


def _call_sources(reader, call: dict) -> list[dict]:
    inputs = call["body"].get("input_refs")
    if not isinstance(inputs, list):
        raise ReportLineageError("Producing call source inputs are invalid")
    sources = []
    for pointer in inputs:
        if not isinstance(pointer, dict):
            raise ReportLineageError("Producing call input is invalid")
        exact = {key: pointer[key] for key in ("kind", "id", "version") if key in pointer}
        copy = _record(reader, exact, "source_copy")
        if set(pointer) == {"kind", "id", "version", "sha256"}:
            if content_hash(copy["body"]) != pointer["sha256"]:
                raise ReportLineageError("Producing copy hash changed")
        elif set(pointer) != {"kind", "id", "version"}:
            raise ReportLineageError("Producing copy reference is invalid")
        sources.append(exact)
    return sources


def report_source_lineage(reader, report: dict) -> tuple[list[dict], dict]:
    """Derive direct, context, and cited copies from immutable producer records.

    The reader may be StateStore or Transaction. Missing producer evidence fails
    closed; a source-free report is established by a valid empty producing input.
    """
    if report.get("kind") != "report" or not isinstance(report.get("body"), dict):
        raise ReportLineageError("Exact report record is required")
    body = report["body"]
    attempt = _record(reader, body.get("producer_attempt_ref"), "agent_attempt")
    attempt_body = attempt["body"]
    call = _record(reader, attempt_body.get("call_ref"), "agent_call")
    call_ref = ref(call)
    call_body = call["body"]
    config = _record(reader, attempt_body.get("configuration_ref"), "agent_configuration")
    config_body = config["body"]
    config_hash = content_hash({
        key: value for key, value in config_body.items()
        if key != "configuration_sha256"
    })
    actor = reader.get("agent_instance", attempt_body.get("agentId"), 1)
    run = reader.get("mission_run", body.get("run_id"))
    if (
        attempt_body.get("mission_id") != body.get("mission_id")
        or attempt_body.get("run_id") != body.get("run_id")
        or call_body.get("mission_id") != body.get("mission_id")
        or call_body.get("task_id") != attempt_body.get("task_id")
        or call_body.get("instance_id") != attempt_body.get("agentId")
        or call_body.get("attempt_id") != attempt["id"]
        or call_body.get("configuration_id") != config["id"]
        or config_body.get("configuration_sha256") != config_hash
        or call_body.get("configuration_sha256") != config_hash
        or config_body.get("mission_id") != body.get("mission_id")
        or config_body.get("instance_id") != attempt_body.get("agentId")
        or any(call_body.get(key) != config_body.get(key) for key in (
            "provider", "endpoint", "model_id", "settings", "reasoning_settings",
            "instructions_sha256", "input_refs", "permitted_tools",
        ))
        or actor is None or actor["body"].get("mission_id") != body.get("mission_id")
        or actor["body"].get("run_id") != body.get("run_id")
        or run is None or run["body"].get("mission_id") != body.get("mission_id")
    ):
        raise ReportLineageError("Report producer actor, task, or run differs")

    intent_ref = {
        "kind": "model_request_intent",
        "id": f"model-intent:{call['id']}:{call['version']}",
        "version": 1,
    }
    intent = _record(reader, intent_ref, "model_request_intent")
    source_refs = intent["body"].get("source_refs")
    context_refs = intent["body"].get("context_source_dependency_refs")
    if (
        intent["body"].get("mission_id") != body.get("mission_id")
        or intent["body"].get("task_id") != attempt_body.get("task_id")
        or intent["body"].get("instance_id") != attempt_body.get("agentId")
        or intent["body"].get("attempt_id") != attempt["id"]
        or intent["body"].get("call_ref") != call_ref
        or intent["body"].get("configuration_ref") != ref(config)
        or intent["body"].get("configuration_sha256") != config_hash
        or not isinstance(source_refs, list)
        or not isinstance(context_refs, list)
        or source_refs != _call_sources(reader, call)
    ):
        raise ReportLineageError("Producing model intent differs from its call")
    inclusion = _record(reader, {
        "kind": "context_inclusion", "id": f"context:{intent['id']}", "version": 1,
    }, "context_inclusion")
    included = inclusion["body"]
    task_ref = included.get("task_ref")
    task = _record(reader, task_ref, "board_proposal")
    if (
        included.get("mission_id") != body.get("mission_id")
        or task["id"] != attempt_body.get("task_id")
        or task["body"].get("mission_id") != body.get("mission_id")
        or included.get("call_ref") != call_ref
        or included.get("intent_ref") != intent_ref
        or included.get("configuration_ref") != ref(config)
        or included.get("source_refs") != source_refs
        or included.get("context_source_dependency_refs") != context_refs
        or included.get("request_sha256") != intent["body"].get("request_sha256")
    ):
        raise ReportLineageError("Producing context inclusion differs from its intent")
    blob = intent["body"].get("blob")
    blob_reader = getattr(reader, "store", reader)
    if not isinstance(blob, dict) or type(blob.get("sha256")) is not str:
        raise ReportLineageError("Producing request blob is invalid")
    try:
        request_bytes = blob_reader.read_blob(blob["sha256"])
    except (StateError, ValueError) as exc:
        raise ReportLineageError("Producing request bytes are unavailable") from exc
    if hashlib.sha256(request_bytes).hexdigest() != intent["body"].get("request_sha256"):
        raise ReportLineageError("Producing request bytes changed")

    citations = body.get("citations", [])
    provenance = body.get("citation_input_provenance", [])
    if (not isinstance(citations, list) or not isinstance(provenance, list)
            or len(citations) != len(provenance)):
        raise ReportLineageError("Report citations lack exact source provenance")
    dependencies: dict[tuple[str, str, int], dict] = {}
    for pointer in (*source_refs, *context_refs):
        copy = _record(reader, pointer, "source_copy")
        dependencies[(copy["kind"], copy["id"], copy["version"])] = ref(copy)
    seen_citations = set()
    for citation, item in zip(citations, provenance, strict=True):
        if not isinstance(citation, dict) or not isinstance(item, dict):
            raise ReportLineageError("Report citation provenance is invalid")
        pair = citation.get("sourceId"), citation.get("spanId")
        if (pair in seen_citations or pair != (item.get("source_id"), item.get("span_id"))
                or item.get("producing_intent_ref") not in (None, intent_ref)):
            raise ReportLineageError("Report citation differs from its inspected source")
        seen_citations.add(pair)
        copy = _record(reader, item.get("copy_ref"), "source_copy")
        passage = _record(reader, item.get("passage_ref"), "source_passage")
        if (copy["id"] != pair[0] or passage["body"].get("copy_ref") != ref(copy)
                or passage["body"].get("span_id") != pair[1]
                or passage["body"].get("inspection_state") != "checked"
                or ref(copy) not in dependencies.values()):
            raise ReportLineageError("Report citation has a different checked passage")
        dependencies[(copy["kind"], copy["id"], copy["version"])] = ref(copy)
    return list(dependencies.values()), call_ref


def report_text_access(reader, report: dict) -> dict:
    """Withhold all model-authored report text on incomplete or revoked lineage."""
    body = report.get("body") if isinstance(report, dict) else None
    if (isinstance(report, dict) and report.get("kind") == "report"
            and isinstance(body, dict)
            and set(body) <= {
                "mission_id", "title", "body", "citations",
                "citation_input_provenance", "limitations",
            }
            and body.get("producer_attempt_ref") is None
            and body.get("run_id") is None
            and body.get("citations", []) == []
            and body.get("citation_input_provenance", []) == []):
        # This minimal pre-run legacy shape has no producing model turn or
        # dependency fields. Normal reports cannot erase ancestry into it.
        return {"status": "available", "source_dependency_refs": [],
                "unavailable_source_refs": [], "dependency_lineage_complete": True}
    try:
        dependencies, call_ref = report_source_lineage(reader, report)
    except (ReportLineageError, StateError, KeyError, TypeError, ValueError):
        return {"status": "data_unavailable_due_source_use",
                "source_dependency_refs": [], "unavailable_source_refs": [],
                "dependency_lineage_complete": False}
    unavailable = []
    for pointer in dependencies:
        try:
            _current_context_dependency_use(
                reader, pointer, report["body"]["mission_id"], call_ref,
            )
        except (SessionError, StateError, KeyError, TypeError, ValueError):
            unavailable.append(pointer)
    return {"status": "available" if not unavailable else "data_unavailable_due_source_use",
            "source_dependency_refs": dependencies,
            "unavailable_source_refs": unavailable,
            "dependency_lineage_complete": True}


def require_report_text_use(reader, report: dict) -> list[dict]:
    """Require valid ancestry and current permission inside an acceptance transaction."""
    dependencies, call_ref = report_source_lineage(reader, report)
    for pointer in dependencies:
        try:
            _current_context_dependency_use(
                reader, pointer, report["body"]["mission_id"], call_ref,
            )
        except (SessionError, StateError, KeyError, TypeError, ValueError) as exc:
            raise ReportSourceUnavailable(
                "Current source use no longer permits this report text"
            ) from exc
    return dependencies
