"""Execute a bounded set of model-authored peer actions from one saved turn.

Each slot keeps its own durable operation identity. Reports are saved first, but
reply delivery and final publication remain separate host acceptance decisions.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from typing import Any

from ora.agents.capabilities import CapabilityError, validate_arguments
from ora.coordination.board import ref
from ora.coordination.executor import WorkResult, WorkSpec
from ora.mission.report_lineage import report_text_access, require_report_text_use
from ora.retrieval.redact import redact_text
from ora.state import StateError
from ora.state.store import canonical

SLOTS = ("report", "review", "followup_request")


def _key(prefix: str, *parts: Any) -> str:
    return prefix + hashlib.sha256(canonical(parts).encode()).hexdigest()[:32]


def _subspec(spec: WorkSpec, slot: str) -> WorkSpec:
    return replace(spec, work_id=f"{spec.work_id}:peer-turn:{slot}")


def _rejection(exc: Exception) -> dict:
    reason, _ = redact_text(str(exc)[:300])
    result = {"status": "rejected", "reason": reason, "external_action": "none"}
    if isinstance(exc, CapabilityError):
        result["field"] = "arguments"
    elif hasattr(exc, "field"):
        result.update(
            field=exc.field,
            condition=exc.condition,
            valid_form=exc.valid_form,
            next_action=exc.next_action,
        )
    return result


def _current_request(service, spec: WorkSpec, args: dict, *, recipient: bool) -> dict:
    request = service.store.get("mail_request", args["request_id"])
    if (request is None or request["version"] != args["request_version"]
            or request["body"].get("mission_id") != spec.mission_id
            or request["body"].get("recipient_id" if recipient else "requester_id")
            != spec.actor_id):
        raise StateError("Peer request identity or version changed")
    return request


def _report(service, spec: WorkSpec, control: dict, args: dict, *, reply: bool) -> dict:
    report_id = _key("report-", spec.run_id, spec.actor_id, spec.work_id)
    if service.store.get("report", report_id) is None:
        if service._new_task_message_after_input(control) is not None:
            raise StateError("A new task message arrived after this model input")
        if reply:
            _current_request(service, spec, args, recipient=True)
            service._validate_answer_request_id(control, args["request_id"])
        else:
            pending = service._unresolved_peer_actions(control)
            if pending:
                raise StateError("Unresolved peer actions need review or scope disposition")
    report = service._report(spec, args)
    return {"status": "report_saved", "report_ref": report}


def execute_peer_turn(service, spec: WorkSpec, control: dict,
                      descriptor: dict, args: dict, tool_call_id: str) -> WorkResult:
    """Run independent slots once; a revision requires this turn's saved review."""
    if (type(args) is not dict or not args or set(args) - set(SLOTS)
            or not any(args.get(slot) is not None for slot in SLOTS)):
        raise CapabilityError("peer_turn needs one or more named actions")
    schemas = descriptor["schema"].get("properties", {})
    if set(schemas) != set(SLOTS) or descriptor.get("version") != "1":
        raise CapabilityError("peer_turn has an unsupported saved contract")
    results: dict[str, dict] = {}
    refs: list[dict] = []
    report = args.get("report")
    report_after_review = isinstance(report, dict) and report.get("kind") == "revision"
    order = (("review", "followup_request", "report") if report_after_review else SLOTS)
    for slot in order:
        value = args.get(slot)
        if value is None:
            continue
        try:
            validate_arguments(schemas[slot], value)
            if slot == "report":
                reply = value["kind"] == "reply"
                if reply and ("request_id" not in value or "request_version" not in value):
                    raise CapabilityError("report.kind=reply needs request_id and request_version")
                if not reply and ("request_id" in value or "request_version" in value):
                    raise CapabilityError("report.kind=revision omits reply request identity")
                if not reply and results.get("review", {}).get("status") != "review_saved":
                    result = {"status": "blocked_dependency", "depends_on": "review"}
                else:
                    result = _report(service, spec, control, value, reply=reply)
            elif slot == "review":
                sub = _subspec(spec, slot)
                reviewed = service.mailbox.review_answer(
                    f"mission-peer-review:{sub.work_id}",
                    mission_id=spec.mission_id, request_id=value["request_id"],
                    reviewer_id=spec.actor_id, answer_id=value["answer_id"],
                    disposition=value["disposition"], reason=value["reason"],
                    expected_request_version=value["request_version"],
                    expected_answer_version=value["answer_version"],
                )
                if reviewed["status"] != "accepted":
                    raise StateError(reviewed["reason"])
                result = {"status": "review_saved", "request_ref": reviewed["request_ref"],
                          "disposition": value["disposition"]}
                refs.append(reviewed["request_ref"])
            elif slot == "followup_request":
                sub = _subspec(spec, slot)
                result, saved_refs = service._peer_request(sub, value)
                refs.extend(saved_refs)
            results[slot] = result
        except (CapabilityError, StateError, ValueError, KeyError, TypeError) as exc:
            results[slot] = _rejection(exc)
    return WorkResult(
        disposition="continue_task",
        payload={"tool_call_id": tool_call_id, "name": "peer_turn",
                 "result": {"status": "peer_turn_recorded", "operations": results}},
        output_refs=tuple(refs),
    )


def link_peer_reply(service, control: dict, report_ref: dict, request_id: str,
                    request_version: int) -> dict:
    """Publish a saved reply only with current report use and exact incoming scope."""
    body = control["body"]
    answer_id = _key("peer-answer-", request_id, report_ref)
    request = {"mission_id": body["mission_id"], "request_id": request_id,
               "request_version": request_version, "answer_id": answer_id,
               "responder_id": body["actor_id"], "artifact_ref": report_ref}

    def save(tx):
        if service._new_task_message_after_input(control, reader=tx, db=tx.db):
            raise StateError("A new task message arrived before peer reply delivery")
        incoming = tx.get("mail_request", request_id)
        if (incoming is None or incoming["version"] != request_version
                or incoming["body"].get("mission_id") != body["mission_id"]
                or incoming["body"].get("recipient_id") != body["actor_id"]):
            raise StateError("Peer request identity or version changed")
        report = tx.get(**report_ref)
        call = tx.get(**body["call_ref"])
        if (report is None or report["kind"] != "report" or ref(report) != report_ref
                or report["body"].get("mission_id") != body["mission_id"]
                or report["body"].get("run_id") != body["run_id"]
                or call is None
                or report["body"].get("producer_attempt_ref") != {
                    "kind": "agent_attempt", "id": call["body"]["attempt_id"],
                    "version": 1,
                }):
            raise StateError("Peer reply report has different lineage")
        require_report_text_use(tx, report)
        return service.mailbox.record_answer_tx(
            tx, mission_id=body["mission_id"], request_id=request_id,
            answer_id=answer_id, responder_id=body["actor_id"],
            artifact_ref=report_ref,
        )

    saved = service.store.transact(
        f"mission-peer-turn-answer:{answer_id}", request, save,
        mission_id=body["mission_id"],
    )
    service._wake.set()
    return {"status": "reply_linked", "report_ref": report_ref,
            "answer_ref": saved["answer_ref"], "request_ref": saved["request_ref"]}


def advance_peer_turn(service, control: dict, work: dict) -> bool:
    """Accept only this bundle's authorized effects, then continue or finish."""
    body = control["body"]
    artifact = service.store.get(**work["body"]["result_ref"])
    payload = artifact["body"].get("payload") or {}
    result = payload.get("result") or {}
    operations = dict(result.get("operations") or {})
    selected_args = body["tool_call"].get("arguments")
    args = selected_args if type(selected_args) is dict else {}
    report_args = args.get("report")
    report_kind = report_args.get("kind") if type(report_args) is dict else None
    existing = service.store.get(
        "context_tool_result", f"tool-result:{body['response_ref']['id']}",
    )
    if existing is not None:
        if existing["body"].get("response_ref") != body["response_ref"]:
            raise StateError("Saved peer turn result has different response lineage")
        saved_message = service.store.read_blob(existing["body"]["blob"]["sha256"])
        saved_content = json.loads(saved_message)["content"]
        saved_result = json.loads(saved_content)
        if (saved_result.get("status") != "peer_turn_recorded"
                or not isinstance(saved_result.get("operations"), dict)):
            raise StateError("Saved peer turn result has different identity")
        operations = saved_result["operations"]
    selected_report = operations.get("report") or {}
    if (existing is None and report_kind == "revision"
            and selected_report.get("status") == "report_saved"):
        report_ref = selected_report["report_ref"]
        failed_followup = (operations.get("followup_request") or {}).get(
            "status") == "rejected"
        newer = service._new_task_message_after_input(control)
        if not failed_followup and newer is None and service._complete_direct(
            control, report_ref, service.executor._spec_from(work).proposal_ref,
        ):
            service._finish_attempt(control, status="completed", report_ref=report_ref)
            service._update_control(
                control, phase="done", work_id=None, tool_operation_ref=None,
                report_ref=report_ref, waiting_reason=None, retry_at=None,
                wait_for=[], wait_decision_ref=None, last_progress_at=service.store.now(),
            )
            return True
        if not failed_followup:
            report = service.store.get(**report_ref)
            access = report_text_access(service.store, report) if report else {
                "status": "unavailable", "dependency_lineage_complete": False,
            }
            if newer is None and access["status"] == "available":
                reason = "Saved report awaits current task claim release"
                if (body.get("phase") == "waiting"
                        and body.get("waiting_reason") == reason):
                    return False
                service._update_control(control, phase="waiting", waiting_reason=reason)
                return True
            if newer is None and not access["dependency_lineage_complete"]:
                reason = "Saved report lineage needs local repair"
                if (body.get("phase") == "waiting"
                        and body.get("waiting_reason") == reason):
                    return False
                service._update_control(control, phase="waiting", waiting_reason=reason)
                return True
        operations["report"] = {
            **selected_report, "status": "saved_not_final",
            "reason": ("independent_followup_request_rejected" if failed_followup else
                       "new_task_message_after_model_input" if newer else
                       "current_report_or_claim_unavailable"),
            **({"message_ref": newer} if newer else {}),
        }
    if (existing is None and report_kind == "reply"
            and selected_report.get("status") == "report_saved"):
        selected = args["report"]
        try:
            operations["report"] = link_peer_reply(
                service, control, selected_report["report_ref"], selected["request_id"],
                selected["request_version"],
            )
        except (StateError, ValueError, KeyError, TypeError) as exc:
            operations["report"] = {**_rejection(exc),
                                    "status": "saved_not_linked",
                                    "report_ref": selected_report["report_ref"]}
    result = {"status": "peer_turn_recorded", "operations": operations}
    result_ref = service.context.save_tool_result(
        f"mission-tool-result:{body['response_ref']['id']}",
        response_ref=body["response_ref"], tool_call_id=body["tool_call"]["id"],
        name="peer_turn", content=canonical(result),
        effect_ref=next(iter(artifact["body"].get("output_refs", [])), None),
    )
    service._finish_attempt(control, status="completed")
    current = service.store.get("mission_task_control", control["id"])
    if current["body"]["phase"] in {"done", "deferred", "cancelled"}:
        return True
    peer_actions = [dict(item) for item in current["body"].get(
        "peer_action_obligations", [],
    )]
    followup = operations.get("followup_request") or {}
    if followup.get("status") == "rejected" and not any(
        item.get("tool_result_ref") == result_ref for item in peer_actions
    ):
        followup_args = args.get("followup_request")
        peer_actions.append({
            "tool_result_ref": result_ref, "response_ref": body["response_ref"],
            "target": (followup_args.get("target_instance_id")
                       if type(followup_args) is dict else None),
            "after_control_version": current["version"], "request_ref": None,
        })
    service._update_control(
        current, phase="ready_model", work_id=None, tool_call=None,
        tool_operation_ref=None, tool_turn_refs=[*body["tool_turn_refs"],
                                                  body["response_ref"]],
        peer_action_obligations=peer_actions, waiting_reason=None, retry_at=None,
        wait_for=[], wait_decision_ref=None, feedback=None,
        last_progress_at=service.store.now(),
    )
    return True
