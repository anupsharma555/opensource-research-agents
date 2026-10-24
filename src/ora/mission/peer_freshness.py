"""Detect scoped task and peer updates after an immutable model input was prepared."""

from __future__ import annotations

import json

from ora.state import StateError

from .user_input_context import new_user_input


def changed_task_message(reader, db, *, control: dict) -> dict | None:
    """Return one same-task message absent from an exact saved model input.

    A message's pending event commits its immutable payload. A later delivery
    event does not make text newly visible if the pending message was already
    included. This works with a read-only store or an acceptance transaction.
    """
    body = control["body"]
    call_ref = body.get("call_ref")
    if not isinstance(call_ref, dict):
        return None
    intent = reader.get(
        "model_request_intent",
        f"model-intent:{call_ref['id']}:{call_ref['version']}", 1,
    )
    inclusion = reader.get(
        "context_inclusion", f"context:{intent['id']}", 1,
    ) if intent else None
    exact_intent = ({key: intent[key] for key in ("kind", "id", "version")}
                    if intent else None)
    if intent is None or inclusion is None or any((
        intent["body"].get("mission_id") != body["mission_id"],
        intent["body"].get("task_id") != body["task_id"],
        intent["body"].get("instance_id") != body["actor_id"],
        intent["body"].get("call_ref") != call_ref,
        inclusion["body"].get("intent_ref") != exact_intent,
        inclusion["body"].get("mission_id") != body["mission_id"],
        (inclusion["body"].get("task_ref") or {}).get("id") != body["task_id"],
    )):
        raise StateError("Current model input lost exact task lineage")
    through = inclusion["body"].get("event_through")
    if type(through) is not int or through < 0:
        raise StateError("Current model input lacks an event watermark")
    user_change = new_user_input(
        reader, db, mission_id=body["mission_id"], run_id=body["run_id"],
        task_id=body["task_id"], actor_id=body["actor_id"], event_through=through,
        included_refs=inclusion["body"].get("included_refs", []),
    )
    if user_change is not None:
        return user_change["input_ref"]
    included = {
        (item.get("kind"), item.get("id"))
        for item in inclusion["body"].get("included_refs", [])
        if isinstance(item, dict)
    }
    rows = db.execute(
        "SELECT body FROM events WHERE mission_id=? AND sequence>? "
        "AND event_type='peer_message_pending' ORDER BY sequence",
        (body["mission_id"], through),
    )
    for row in rows:
        event = json.loads(row["body"])
        pointer = event.get("message_ref")
        message = reader.get(**pointer) if (
            isinstance(pointer, dict)
            and set(pointer) == {"kind", "id", "version"}
            and pointer.get("kind") == "mail_message"
        ) else None
        if (
            message is None
            or (message["kind"], message["id"]) in included
            or message["body"].get("mission_id") != body["mission_id"]
            or (message["body"].get("recipient_ref") or {}).get("id") != body["actor_id"]
            or (message["body"].get("task_ref") or {}).get("id") != body["task_id"]
        ):
            continue
        return {key: message[key] for key in ("kind", "id", "version")}
    return None


def changed_peer_input(reader, db, *, mission_id: str, run_id: str,
                       task_id: str, actor_id: str, event_through: int,
                       included_refs=()) -> dict | None:
    """Return one relevant new inbound request, answer or decline.

    ``reader`` and ``db`` may be a read-only snapshot or the model-dispatch
    transaction. The latter closes the race between a peer delivery and the
    provider marker. A newer event only matters for actors in this exact run.
    """
    if type(event_through) is not int or event_through < 0:
        raise StateError("Prepared peer-input event watermark is invalid")

    def same_run(peer_id: str) -> bool:
        if not isinstance(peer_id, str):
            return False
        actor = reader.get("agent_instance", peer_id, 1)
        return bool(
            actor and actor["body"].get("mission_id") == mission_id
            and actor["body"].get("run_id") == run_id
        )

    if not same_run(actor_id):
        raise StateError("Prepared peer-input actor is outside this run")
    user_change = new_user_input(
        reader, db, mission_id=mission_id, run_id=run_id, task_id=task_id,
        actor_id=actor_id, event_through=event_through, included_refs=included_refs,
    )
    if user_change is not None:
        return user_change
    rows = db.execute(
        "SELECT sequence,event_type,body FROM events "
        "WHERE mission_id=? AND sequence>? "
        "AND event_type IN ('peer_message_delivered','peer_answer_submitted',"
        "'peer_request_declined','research_peer_notice') "
        "ORDER BY sequence",
        (mission_id, event_through),
    )
    for row in rows:
        event = json.loads(row["body"])
        if row["event_type"] == "research_peer_notice":
            pointer = event.get("notice_ref")
            notice = reader.get(**pointer) if (
                type(pointer) is dict
                and set(pointer) == {"kind", "id", "version"}
                and pointer.get("kind") == "research_peer_notice"
                and isinstance(pointer.get("id"), str)
                and type(pointer.get("version")) is int
                and pointer["version"] > 0
            ) else None
            body = notice["body"] if notice else {}
            if (
                notice is None
                or event.get("recipient_id") != actor_id
                or body.get("mission_id") != mission_id
                or body.get("run_id") != run_id
                or body.get("recipient_actor_id") != actor_id
                or body.get("recipient_task_id") != task_id
                or not same_run(body.get("producer_actor_id"))
                or body.get("producer_actor_id") == actor_id
            ):
                continue
            return {
                "event_sequence": row["sequence"],
                "event_type": row["event_type"],
                "request_ref": {key: notice[key] for key in ("kind", "id", "version")},
            }
        if row["event_type"] == "peer_message_delivered":
            if event.get("recipient_id") != actor_id:
                continue
            message_ref = event.get("message_ref")
            message = reader.get(**message_ref) if isinstance(message_ref, dict) else None
            request_id = message["body"].get("request_id") if message else None
            request = reader.get("mail_request", request_id) if request_id else None
            if (
                not request or message["body"].get("mission_id") != mission_id
                or message["body"].get("recipient_ref", {}).get("id") != actor_id
                or request["body"].get("mission_id") != mission_id
                or request["body"].get("recipient_id") != actor_id
                or request["body"].get("requester_id") != message["body"].get(
                    "sender_ref", {}
                ).get("id")
                or request["body"].get("message_ref", {}).get("id") != message["id"]
                or not request["body"].get("delivered_at")
                or not same_run(request["body"].get("requester_id"))
            ):
                continue
        elif row["event_type"] == "peer_request_declined":
            request_ref = event.get("request_ref")
            request = reader.get(**request_ref) if (
                type(request_ref) is dict
                and set(request_ref) == {"kind", "id", "version"}
                and request_ref.get("kind") == "mail_request"
                and isinstance(request_ref.get("id"), str)
                and type(request_ref.get("version")) is int
                and request_ref["version"] > 0
            ) else None
            cause_ref = request["body"].get("cause_ref") if request else None
            cause = reader.get(**cause_ref) if (
                type(cause_ref) is dict
                and set(cause_ref) == {"kind", "id", "version"}
                and cause_ref.get("kind") == "board_proposal"
                and isinstance(cause_ref.get("id"), str)
                and type(cause_ref.get("version")) is int
                and cause_ref["version"] > 0
            ) else None
            if (
                request is None or cause is None
                or event.get("recipient_ids") != [actor_id]
                or request["body"].get("status") != "declined"
                or request["body"].get("mission_id") != mission_id
                or request["body"].get("requester_id") != actor_id
                or not same_run(request["body"].get("recipient_id"))
                or cause["body"].get("mission_id") != mission_id
                or cause["id"] != task_id
                or cause["body"].get("actor_ref", {}).get("id") != actor_id
            ):
                continue
        else:
            request_ref = event.get("request_ref")
            request = reader.get(**request_ref) if isinstance(request_ref, dict) else None
            answer_ref = event.get("answer_ref")
            answer = reader.get(**answer_ref) if isinstance(answer_ref, dict) else None
            if (
                not request or not answer
                or request["body"].get("mission_id") != mission_id
                or request["body"].get("requester_id") != actor_id
                or not same_run(request["body"].get("recipient_id"))
                or answer["body"].get("mission_id") != mission_id
                or answer["body"].get("request_id") != request["id"]
                or answer["body"].get("responder_id") != request["body"].get("recipient_id")
            ):
                continue
        return {
            "event_sequence": row["sequence"],
            "event_type": row["event_type"],
            "request_ref": {key: request[key] for key in ("kind", "id", "version")},
        }
    return None
