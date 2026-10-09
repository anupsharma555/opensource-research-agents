"""Explain accepted state events without exposing prompts or private payloads.

Each step describes only the event and exact saved versions that can be read
back. A prepared call is not a model invocation; a delivered message is not a
peer answer. Free-text source bodies, queries, tool arguments, credentials and
private model reasoning never enter this projection.
"""

from __future__ import annotations

import re
from typing import Any, get_args

from ora.contracts.models import ResearchStep, RetrievalEnvelope

_REF_KEYS = ("kind", "id", "version")
_MESSAGE_ACTIONS = {
    "requested": ("Ask a peer for help", "A peer request was recorded; no reply is recorded yet."),
    "delivered": (
        "Record peer delivery",
        "The message reached its destination; no reply is implied.",
    ),
    "acknowledged": (
        "Acknowledge peer work",
        "The recipient acknowledged the request; no substantive reply is implied.",
    ),
    "fulfilled": ("Record a peer reply", "A substantive peer reply was recorded."),
}
_RETRIEVAL_OUTCOMES = set(get_args(RetrievalEnvelope.model_fields["outcome"].annotation))
_TOOL_OUTCOMES = {"completed", "rejected", "failed"}
_SECRET = re.compile(
    r"(?i)\b(?:bearer|api[-_]?key|authorization|token)\s*[:=]?\s*[A-Za-z0-9._-]{6,}"
)
_EMAIL = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_URL = re.compile(r"https?://\S+", re.I)
_COORDINATION_ACTIONS = {
    "proposal_created": ("Propose research work", "A peer saved a question, expected contribution and next action."),
    "proposal_revised": ("Revise proposed work", "A peer saved a new work version; older claims cannot publish against it."),
    "proposal_claimed": ("Claim an eligible contribution", "A peer acquired a time-limited claim to this exact work version."),
    "proposal_dissent": ("Preserve an alternative interpretation", "A peer recorded disagreement without deleting the existing interpretation."),
    "proposal_result_accepted": ("Accept a peer contribution", "The contribution and its related local records committed together."),
    "proposals_reopened": ("Reopen affected research work", "Changed evidence reopened its dependent work for peer review."),
    "invitation_offered": ("Invite a peer to contribute", "An invitation was saved; acceptance and execution are separate."),
    "invitation_decided": ("Record an invitation response", "The invited peer's response was saved for the exact proposal version."),
    "peer_message_pending": ("Queue a peer message", "A message and its delivery intent were saved together."),
    "peer_message_delivered": ("Deliver a peer message", "Delivery was recorded; this does not establish that the requested work is answered."),
    "peer_message_acknowledged": ("Acknowledge a peer message", "The recipient acknowledged the message; a scientific response is still separate."),
    "peer_request_opened": ("Ask a peer for evidence or review", "A request names the question, expected contribution and exact supporting records."),
    "peer_answer_submitted": ("Submit a peer answer", "A saved contribution was offered for review; the requester has not necessarily accepted it."),
    "peer_request_reviewed": ("Review a peer answer", "The requester or authorized replacement recorded the answer's usefulness and next action."),
    "peer_request_declined": ("Record a declined request", "The request remains visible for an alternative route; other work can continue."),
    "peer_request_redirected": ("Request another route", "The original request remains linked to a new peer request."),
    "executor_work_submitted": ("Save ready-to-run work", "The host saved exact inputs, configuration and callback version; execution is separate."),
}


def _safe_text(value: Any, limit: int = 180) -> str | None:
    """Use brief reader-facing text, not arbitrary raw event or source fields."""
    if not isinstance(value, str):
        return None
    text = " ".join(value.split())
    text = _URL.sub("[link]", text)
    text = _EMAIL.sub("[address]", text)
    text = _SECRET.sub("[credential]", text)
    if not text:
        return None
    return text[:limit] + ("…" if len(text) > limit else "")


def _safe_label(value: Any) -> str | None:
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,79}", value):
        return None
    return value


def _ref(store: Any, value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    try:
        pointer = {key: value[key] for key in _REF_KEYS}
    except KeyError:
        return None
    if (
        not isinstance(pointer["kind"], str)
        or not isinstance(pointer["id"], str)
        or type(pointer["version"]) is not int
        or pointer["version"] < 1
    ):
        return None
    return pointer if store.get(**pointer) is not None else None


def _refs(store: Any, values: Any) -> list[dict[str, Any]]:
    if not isinstance(values, list):
        return []
    result = []
    for value in values:
        pointer = _ref(store, value)
        if pointer and pointer not in result:
            result.append(pointer)
    return result


def _record(store: Any, pointer: Any) -> dict[str, Any] | None:
    exact = _ref(store, pointer)
    return store.get(**exact) if exact else None


def _actor_name(store: Any, actor_id: Any) -> str | None:
    if not isinstance(actor_id, str) or not actor_id:
        return None
    actor = store.get("agent_instance", actor_id, version=1)
    return _safe_text(actor["body"].get("display_name"), 80) if actor else None


def explain_event(store: Any, event: dict[str, Any]) -> dict[str, Any]:
    """Return a bounded explanation of one accepted event and its exact refs."""
    body = event["body"] if isinstance(event.get("body"), dict) else {}
    kind = event["type"]
    action, summary, outcome = "Record a state change", "The host saved a state event.", "recorded"
    inputs: list[dict[str, Any]] = []
    outputs: list[dict[str, Any]] = []
    actor_id: str | None = None
    reason: str | None = None
    next_action: str | None = None
    proof = "saved local state; no model execution inferred"

    if kind == "coordination_trace_recorded":
        trace = _record(store, body.get("trace_ref"))
        if trace and trace["body"].get("mission_id") == event["mission_id"]:
            observed = trace["body"]
            actor_id = observed.get("instance_id")
            action = _safe_text(observed.get("action"), 240) or "Record execution activity"
            summary = _safe_text(observed.get("summary"), 1200) or "The recorded activity has no saved explanation."
            outcome = _safe_label(observed.get("status")) or "recorded"
            inputs = _refs(store, observed.get("input_refs"))
            outputs = _refs(store, [*observed.get("output_refs", []), body.get("trace_ref")])
            reason = _safe_text(observed.get("reason_summary"), 1200)
            next_action = _safe_text(observed.get("next_action"), 600)
            proof = _safe_text(observed.get("proof"), 240) or "saved host observation"
        else:
            action, summary, outcome = "Inspect a missing trace", "The exact trace record is unavailable; no outcome can be reconstructed.", "unavailable"

    elif kind in _COORDINATION_ACTIONS:
        action, summary = _COORDINATION_ACTIONS[kind]
        actor_id = body.get("actor_id") or body.get("sender_id") or body.get("responder_id")
        values = []
        for key, value in body.items():
            if key.endswith("_ref"):
                values.append(value)
            elif key.endswith("_refs") and isinstance(value, list):
                values.extend(value)
        outputs = _refs(store, values)
        # Look only at the referenced record's actor identity, never its free-text payload.
        for pointer in outputs:
            saved = store.get(**pointer)["body"]
            actor_id = actor_id or saved.get("actor_id") or saved.get("sender_id") or saved.get("requester_id")
            for field in ("actor_ref", "sender_ref", "owner_ref"):
                candidate = saved.get(field)
                if isinstance(candidate, dict) and candidate.get("kind") == "agent_instance":
                    actor_id = actor_id or candidate.get("id")
        proof = "saved coordination state; scientific usefulness requires separate review"

    elif kind == "agent_registered":
        actor_id = body.get("instance_id")
        actor = store.get("agent_instance", actor_id, version=1) if actor_id else None
        action, summary, outcome = (
            "Register a named research agent",
            "A permanent instance identity and readable name were saved; this does not establish competence.",
            "registered",
        )
        if actor:
            outputs = [_ref(store, actor)]
            predecessor = actor["body"].get("predecessor_id")
            if predecessor:
                inputs = _refs(store, [{"kind": "agent_instance", "id": predecessor, "version": 1}])

    elif kind == "agent_configuration_saved":
        config = store.get("agent_configuration", body.get("configuration_id"), version=1)
        action, summary, outcome = (
            "Save an agent configuration",
            "The provider, model, settings, instructions and capability choices were pinned for later work.",
            "saved",
        )
        if config:
            actor_id = config["body"].get("instance_id")
            model_id = _safe_label(config["body"].get("model_id"))
            if model_id:
                summary = (
                    f"Pinned model {model_id} and its settings for later work; no call was made."
                )
            outputs = [_ref(store, config)]
            inputs = _refs(
                store,
                [
                    {"kind": "agent_instance", "id": actor_id, "version": 1},
                    {
                        "kind": "agent_configuration",
                        "id": config["body"].get("previous_configuration_id"),
                        "version": 1,
                    },
                ],
            )
            reason = _safe_text(config["body"].get("change_reason"))

    elif kind in {"agent_call_prepared", "agent_tool_exposed"}:
        version = body.get("manifest_version")
        call = store.get("agent_call", body.get("call_id"), version=version)
        if kind == "agent_call_prepared":
            action, summary, outcome = (
                "Prepare an agent call",
                "The call's configuration and exact inputs were saved; no model invocation is implied.",
                "prepared",
            )
        else:
            action, summary, outcome = (
                "Expose a permitted tool",
                "A new tool view was saved for a prepared call; this does not mean the tool ran.",
                "exposed",
            )
        proof = "saved call manifest; no model or tool execution inferred"
        if call:
            actor_id = call["body"].get("instance_id")
            outputs = [_ref(store, call)]
            if kind == "agent_call_prepared":
                inputs = _refs(
                    store,
                    [
                        {
                            "kind": "agent_configuration",
                            "id": call["body"].get("configuration_id"),
                            "version": 1,
                        },
                        *call["body"].get("input_refs", []),
                    ],
                )
            elif call["version"] > 1:
                inputs = _refs(
                    store,
                    [{"kind": "agent_call", "id": call["id"], "version": call["version"] - 1}],
                )
                tool = _safe_label(body.get("tool_id"))
                if tool:
                    summary = (
                        f"The permitted {tool} tool was added to this call view; it has not run."
                    )

    elif kind == "retrieval_delivery_recorded":
        delivery = _record(store, body.get("delivery_ref"))
        attempt = _record(store, body.get("attempt_ref"))
        accepted = _refs(store, body.get("accepted_hit_refs"))
        reused = _refs(store, body.get("reused_hit_refs"))
        action, outcome = (
            "Record a retrieval delivery",
            (body.get("outcome") if body.get("outcome") in _RETRIEVAL_OUTCOMES else "recorded"),
        )
        new_count, reused_count = len(accepted), len(reused)
        request = attempt["body"].get("request", {}) if attempt else {}
        provider = _safe_label(request.get("provider"))
        route = _safe_label(request.get("route"))
        route_name = f" from {provider}/{route}" if provider and route else ""
        summary = (
            f"Saved a {outcome.replace('_', ' ')} retrieval delivery{route_name} with {new_count} new "
            f"{'hit' if new_count == 1 else 'hits'} and {reused_count} reused "
            f"{'hit' if reused_count == 1 else 'hits'}; relevance is not established by this receipt."
        )
        outputs = _refs(store, [body.get("delivery_ref"), body.get("attempt_ref"), *accepted])
        inputs = reused
        if attempt:
            actor_id = request.get("agent_instance_id")
        if delivery:
            proof = "saved adapter delivery; source quality not established"

    elif kind == "source_review_required":
        action, outcome = "Flag source-dependent work for review", "review_required"
        change = (
            body.get("kind")
            if body.get("kind") in {"correction", "retraction"}
            else "source change"
        )
        affected = _refs(store, body.get("review_refs"))
        summary = f"A {change} notice flagged {len(affected)} dependent version(s) for review."
        inputs = _refs(store, [body.get("affected_copy_ref"), body.get("notice_copy_ref")])
        outputs = affected
        next_action = "Review flagged versions before relying on their affected source passages."

    elif kind in {"capability_result", "skill_result"}:
        call = _record(store, body.get("call_ref"))
        receipt = _record(store, body.get("result_ref"))
        if call and call["id"] != body.get("call_id"):
            call = None
        if receipt and receipt["body"].get("call_id") != body.get("call_id"):
            receipt = None
        actor_id = call["body"].get("instance_id") if call else None
        result_kind = "tool" if kind == "capability_result" else "skill helper"
        outcome = body.get("status") if body.get("status") in _TOOL_OUTCOMES else "recorded"
        action = f"Record a {result_kind} result"
        label = _safe_label(body.get("tool_id" if kind == "capability_result" else "skill_id"))
        subject = f"{label} {result_kind}" if label else result_kind
        summary = f"The {subject} returned {outcome}; its content is kept in a separate receipt."
        inputs = _refs(store, [body.get("call_ref")]) if call else []
        outputs = _refs(store, [body.get("result_ref")]) if receipt else []
        proof = (
            "saved local invocation receipt; no model call inferred"
            if receipt
            else "saved invocation event; exact result receipt not named by this event"
        )

    elif kind == "snapshot_accepted":
        action, summary, outcome = (
            "Accept a research view for review",
            "The host saved a review view and its exact accepted outputs.",
            "accepted",
        )
        inputs = _refs(store, body.get("outputs"))
        outputs = _refs(store, [body.get("snapshot")])

    elif kind == "recorded_workflow_step":
        action, summary, outcome = (
            "Record an offline integration step",
            "A local recorded-data workflow step was saved; no model or provider call was made.",
            "recorded",
        )
        proof = "offline_test"

    elif kind.endswith("_saved"):
        record = _record(store, body.get("record"))
        if record:
            data = record["body"]
            outputs = [_ref(store, record)]
            # Links can be appended to a record after this event. Only explicit
            # event-time refs (or refs inside the immutable body) describe its
            # historical inputs.
            inputs = _refs(store, body.get("input_refs"))
            proof = "offline_test" if data.get("proof_kind") == "offline_test" else proof
            record_kind = record["kind"]
            if record_kind == "message":
                actor_id = data.get("fromAgentId")
                status = data.get("status")
                action, summary = _MESSAGE_ACTIONS.get(
                    status, ("Record peer communication", "A peer message state was saved.")
                )
                outcome = status if status in _MESSAGE_ACTIONS else "recorded"
                detail = _safe_text(data.get("summary"), 140)
                if detail and status in {"requested", "fulfilled"}:
                    summary += f" Summary: {detail}"
                next_action = _safe_text(data.get("nextAction"))
            elif record_kind == "attempt":
                actor_id = data.get("agentId")
                action, outcome = (
                    "Record an agent attempt",
                    _safe_label(data.get("status")) or "recorded",
                )
                summary = _safe_text(data.get("resultSummary")) or "An attempt state was saved."
                next_action = _safe_text(data.get("nextAction"))
            elif record_kind == "task":
                action, outcome = (
                    "Update research work",
                    _safe_label(data.get("status")) or "recorded",
                )
                title = _safe_text(data.get("title"), 140)
                summary = f"The task {title} is {outcome}." if title else f"A task is {outcome}."
                reason = _safe_text(data.get("latencyBasis"), 140)
                next_action = _safe_text(data.get("nextAction"))
            elif record_kind == "claim":
                action, outcome = (
                    "Save an evidence-linked finding",
                    "revised" if record["version"] > 1 else "saved",
                )
                finding = _safe_text(data.get("text"), 140)
                summary = (
                    f"Saved finding: {finding}"
                    if finding
                    else f"Saved a finding with {len(inputs)} recorded input version(s)."
                )
            elif record_kind == "report":
                action, outcome = (
                    "Save a research report",
                    "revised" if record["version"] > 1 else "saved",
                )
                producer = _record(store, data.get("producer_attempt_ref"))
                if producer:
                    actor_id = producer["body"].get("agentId")
                    inputs = _refs(store, [*inputs, data["producer_attempt_ref"]])
                title = _safe_text(data.get("title"), 140)
                summary = (
                    f"Saved report {title} with {len(inputs)} recorded input version(s)."
                    if title
                    else f"Saved a report with {len(inputs)} recorded input version(s)."
                )

    return ResearchStep(
        id=event["id"],
        sequence=event["sequence"],
        recorded_at=event["created_at"],
        actor_name=_actor_name(store, actor_id),
        action=action,
        summary=summary,
        outcome=outcome,
        input_references=inputs,
        output_references=outputs,
        reason_summary=reason,
        next_action=next_action,
        proof=proof,
    ).model_dump(mode="json", by_alias=True)
