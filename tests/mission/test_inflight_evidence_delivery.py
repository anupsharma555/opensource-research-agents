"""A permitted late evidence lead reaches the next real model request after inspection."""

from __future__ import annotations

import hashlib
import json
import socket
import threading
import time

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.retrieval.receipts import record_delivery
from tests.mission.test_service import service, tool

OLD_TEXT = (
    "Synthetic draft comparison reported benefit in 12 of 20 records, "
    "but its audit status was still pending."
)
NEW_TEXT = (
    "Synthetic later audit found benefit in only 3 of 20 records. "
    "The earlier count included records from a different group, so the draft comparison "
    "needs correction before any broader conclusion."
)
QUESTION = "Compare the supplied draft with any later audit evidence and state the limitation."
SCOPE = "Use only saved synthetic evidence and keep conflicting counts separate."


def _drive_until(host, run_id: str, predicate) -> None:
    for _ in range(500):
        host._drive_run(host._run(run_id))
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError("Late evidence case did not reach the saved boundary")


def _last_tool(request: dict, name: str) -> dict:
    message = next(
        item for item in reversed(request["messages"])
        if item.get("role") == "tool" and item.get("name") == name
    )
    return json.loads(message["content"])


def _saved_tool_names(host) -> list[str]:
    return [
        json.loads(host.store.read_blob(item["body"]["blob"]["sha256"]))["name"]
        for item in host.store.records("context_tool_result")
    ]


@pytest.mark.parametrize("reopen_before_next_turn", [False, True])
def test_permitted_late_evidence_enters_next_serialized_input_after_inspection(
    tmp_path, monkeypatch, reopen_before_next_turn,
) -> None:
    network_attempts = []

    def no_network(*_args, **_kwargs):
        network_attempts.append("socket connection")
        raise AssertionError("Offline late evidence case attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    started = threading.Event()
    release = threading.Event()
    requests: list[dict] = []
    saved: dict = {}
    active_host = None

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        requests.append(packet)
        step = len(requests)
        if step == 1:
            assert QUESTION in json.dumps(packet["messages"])
            assert SCOPE in json.dumps(packet["messages"])
            assert OLD_TEXT in json.dumps(packet["messages"])
            assert NEW_TEXT not in json.dumps(packet["messages"])
            started.set()
            assert release.wait(5), "Offline first call was not released"
            return tool("discover_capabilities", {}, step)

        control = active_host._control(saved["run_id"], "agent-synthesis")
        call_ref = control["body"]["call_ref"]
        intent = active_host.store.get(
            "model_request_intent", f"model-intent:{call_ref['id']}:{call_ref['version']}"
        )
        assert json.loads(active_host.store.read_blob(intent["body"]["blob"]["sha256"])) == packet
        assert QUESTION in json.dumps(packet["messages"])
        assert SCOPE in json.dumps(packet["messages"])
        if step == 2:
            assert NEW_TEXT not in json.dumps(packet["messages"])
            assert saved["message_ref"]["id"] in json.dumps(packet["messages"])
            assert saved["hit_ref"]["id"] in json.dumps(packet["messages"])
            index = json.loads(packet["messages"][-1]["content"])
            delivered = active_host.store.get("mail_message", saved["message_ref"]["id"])
            assert delivered["body"]["delivered_at"] is not None
            assert ref(delivered) in index["message_refs"]
            discovered = _last_tool(packet, "discover_capabilities")
            assert "granted" in discovered and "next_action" in discovered
            first_call = active_host.store.get(**saved["first_call_ref"])
            assert active_host.store.get(
                "agent_attempt", first_call["body"]["attempt_id"]
            )["body"]["status"] == "completed"
            detail = next(
                item for item in index["details"]
                if item["type"] == "message" and item["ref"] == ref(delivered)
            )
            assert detail["body"]["payload"] == saved["notification"]
            assert detail["body"]["evidence_refs"] == [saved["hit_ref"]]
            assert detail["body"]["sender_ref"]["id"] == saved["actor_id"]
            assert detail["body"]["recipient_ref"]["id"] == saved["actor_id"]
            assert detail["body"]["task_ref"] == saved["task_ref"]
            assert detail["body"]["correlation_id"] == "synthetic-later-audit"
            assert "inspect_hit" in {item["function"]["name"] for item in packet["tools"]}
            return tool("inspect_hit", {"hit_id": saved["hit_ref"]["id"]}, step)
        if step == 3:
            inspected = _last_tool(packet, "inspect_hit")
            assert inspected["status"] == "inspected"
            assert inspected["preview_scope"] == "complete_retained_extracted_text"
            assert inspected["preview"]["text_status"] == (
                "exact_checked_passage_in_selected_source_packet"
            )
            assert inspected["preview"]["text_sha256"] == hashlib.sha256(
                NEW_TEXT.encode()
            ).hexdigest()
            saved["new_copy_ref"] = {
                "kind": "source_copy", "id": inspected["source_id"],
                "version": inspected["source_version"],
            }
            saved["new_span_id"] = inspected["passages"][0]["span_id"]
            assert saved["new_copy_ref"] in control["body"]["source_refs"]
            assert NEW_TEXT in json.dumps(packet["messages"])
            source_packets = json.loads(packet["messages"][1]["content"])["source_packets"]
            assert any(
                item["id"] == saved["new_copy_ref"]["id"]
                and item["version"] == saved["new_copy_ref"]["version"]
                for item in source_packets
            )
            assert "read_source_window" in {
                item["function"]["name"] for item in packet["tools"]
            }
            return tool("read_source_window", {
                "source_id": inspected["source_id"],
                "source_version": inspected["source_version"],
                "block_offset": 0, "char_offset": 0, "char_limit": len(NEW_TEXT),
                "selection_mode": "replace",
            }, step)
        assert step == 4
        window = _last_tool(packet, "read_source_window")
        assert window["copy_ref"] == saved["new_copy_ref"]
        assert window["source_text_status"] == "exact_checked_passages_in_this_model_input"
        assert active_host.store.get(**window["passages"][0]["passage_ref"])["body"][
            "text"
        ] == NEW_TEXT
        saved["new_span_id"] = window["passages"][0]["span_id"]
        assert NEW_TEXT in json.dumps(packet["messages"])
        assert saved["first_request_bytes"] == active_host.store.read_blob(
            saved["first_intent"]["body"]["blob"]["sha256"]
        )
        return tool("publish_result", {
            "title": "Synthetic audit comparison",
            "text": (
                "The supplied draft reported 12 of 20, while the later synthetic audit "
                "reported 3 of 20 and identified a group-mix error. The conflict needs "
                "independent validation before broader use."
            ),
            "citations": [
                saved["old_citation"],
                {"source_id": saved["new_copy_ref"]["id"],
                 "span_id": saved["new_span_id"]},
            ],
            "limitations": ["Synthetic fixtures do not validate a real study"],
        }, step)

    host = service(tmp_path, monkeypatch, transport)
    active_host = host
    run = host.start(LiveStartRequest(
        request_id=f"late-evidence-{'reopen' if reopen_before_next_turn else 'continuous'}",
        question=QUESTION, scope=SCOPE, evidence_text=OLD_TEXT,
        agent_ids=["agent-synthesis"],
    ))
    saved["run_id"] = run.run_id
    initial_boundary_complete = False
    try:
        _drive_until(host, run.run_id, started.is_set)
        control = host._control(run.run_id, "agent-synthesis")
        mission_id = host._run(run.run_id)["body"]["mission_id"]
        actor_id = control["body"]["actor_id"]
        saved["actor_id"] = actor_id
        task = host.store.get("board_proposal", control["body"]["task_id"])
        saved["task_ref"] = ref(task)
        old_ref = control["body"]["source_refs"][0]
        old_copy = host.store.get(**old_ref)
        old_passage = host.store.get("source_passage", old_copy["body"]["passage_ids"][0])
        saved["old_citation"] = {
            "source_id": old_ref["id"], "span_id": old_passage["body"]["span_id"],
        }
        first_call = control["body"]["call_ref"]
        first_intent = host.store.get(
            "model_request_intent", f"model-intent:{first_call['id']}:{first_call['version']}"
        )
        first_bytes = host.store.read_blob(first_intent["body"]["blob"]["sha256"])
        saved["first_intent"] = first_intent
        saved["first_call_ref"] = first_call
        saved["first_request_bytes"] = first_bytes
        assert json.loads(first_bytes) == requests[0]

        # Retain a synthetic later lead through the normal offline delivery
        # normalizer. This records a candidate, not an inspected passage.
        delivery_request = {
            "mission_id": mission_id, "task_id": task["id"],
            "agent_instance_id": actor_id,
            "request_id": "synthetic-later-audit", "attempt_id": "synthetic-later-audit-1",
            "route": "synthetic-saved-index", "provider": "synthetic-fixture",
            "operation": "search", "requested_at": "2026-01-02T00:00:00Z",
            "requested_window": {"offset": 0, "limit": 1},
            "query": "synthetic later audit",
        }
        delivery = record_delivery(
            host.store, delivery_request,
            {"status": "ok", "count": 1, "offset": 0, "items": [{
                "id": "synthetic-audit-item", "url": "https://example.org/synthetic-audit",
                "title": "Synthetic later audit", "content": NEW_TEXT,
                "surface": "abstract", "origin": "original_source",
                "rights_status": "permitted", "access_status": "available",
            }]},
            delivery_id="synthetic-later-audit-delivery", adapter="count_offset",
            retrieved_at="2026-01-02T00:01:00Z",
        )
        saved["hit_ref"] = delivery["accepted_hit_refs"][0]
        hit_body = host.store.get(**saved["hit_ref"])["body"]
        assert hit_body["mission_id"] == mission_id
        assert hit_body["task_id"] == task["id"]
        assert hit_body["agent_instance_id"] == actor_id
        assert hit_body["route"] == "synthetic-saved-index"
        assert hit_body["provider"] == "synthetic-fixture"
        assert hit_body["source"]["content"] == NEW_TEXT
        assert hit_body["source"]["content_sha256"] == hashlib.sha256(
            NEW_TEXT.encode()
        ).hexdigest()
        notification = {
            "type": "evidence_update", "status": "new_candidate_for_inspection",
            "note": "A later synthetic audit may revise the draft count.",
        }
        saved["notification"] = notification
        message_args = dict(
            mission_id=mission_id, message_id="synthetic-later-audit-notice",
            sender_id=actor_id, recipient_id=actor_id,
            correlation_id="synthetic-later-audit", payload=notification,
            task_ref=ref(task), evidence_refs=[saved["hit_ref"]],
        )
        sent = host.mailbox.send_message("synthetic-later-audit-send", **message_args)
        assert host.mailbox.send_message("synthetic-later-audit-send", **message_args) == sent
        saved["message_ref"] = sent["message_ref"]
        assert host.store.get(**saved["message_ref"])["body"]["evidence_refs"] == [
            saved["hit_ref"]
        ]
        assert host.store.read_blob(first_intent["body"]["blob"]["sha256"]) == first_bytes
        assert NEW_TEXT.encode() not in first_bytes
        assert saved["message_ref"]["id"].encode() not in first_bytes
        assert control["body"]["source_refs"] == [old_ref]
        assert len(host.store.records("retrieval_hit")) == 1
        assert len(host.store.records("mail_message")) == 1
        assert _saved_tool_names(host).count("discover_capabilities") == 0

        release.set()
        _drive_until(host, run.run_id, lambda: (
            host._control(run.run_id, "agent-synthesis")["body"]["phase"] == "ready_model"
            and bool(host._control(run.run_id, "agent-synthesis")["body"].get("tool_turn_refs"))
        ))
        assert len(requests) == 1
        assert _saved_tool_names(host).count("discover_capabilities") == 1
        original_profile = host._run(run.run_id)["body"]["profile"]
        original_config = host._configuration(host._control(run.run_id, "agent-synthesis"))[
            "body"
        ]
        initial_boundary_complete = True
    finally:
        release.set()
        if reopen_before_next_turn or not initial_boundary_complete:
            host.close()

    if reopen_before_next_turn:
        active_host = None
        host = service(tmp_path, monkeypatch, transport)
        active_host = host
    try:
        current = host._control(run.run_id, "agent-synthesis")
        assert current["body"]["source_refs"] == [old_ref]
        assert current["body"]["actor_id"] == actor_id
        assert host._run(run.run_id)["body"]["question"] == QUESTION
        assert host._run(run.run_id)["body"]["scope"] == SCOPE
        assert host._run(run.run_id)["body"]["profile"] == original_profile
        assert host._configuration(current)["body"] == original_config
        _drive_until(host, run.run_id, lambda: (
            host._run(run.run_id)["body"]["status"] == "completed"
        ))
        assert len(requests) == 4
        assert len(host.store.records("mail_message")) == 1
        assert len(host.store.records("retrieval_hit")) == 1
        assert len(host.store.records("source_copy")) == 2
        assert len(host.store.records("source_read_window")) == 1
        assert _saved_tool_names(host).count("discover_capabilities") == 1
        assert _saved_tool_names(host).count("inspect_hit") == 1
        assert _saved_tool_names(host).count("read_source_window") == 1
        assert host.store.read_blob(first_intent["body"]["blob"]["sha256"]) == first_bytes
        assert host.store.get(**saved["message_ref"])["body"]["payload"] == notification
        assert host.store.get(**saved["new_copy_ref"])["body"]["inspection_state"] == "checked"
        use = host.store.resolve(
            "source_use.copy",
            f"{saved['new_copy_ref']['id']}:{saved['new_copy_ref']['version']}",
        )
        assert use is not None
        assert host.store.get(**ref(use))["body"]["policy_status"] == "granted"
        assert "model_input" in host.store.get(**ref(use))["body"]["allowed_uses"]
        assert host.store.get(**ref(use))["body"]["source_sha256"] == hashlib.sha256(
            NEW_TEXT.encode()
        ).hexdigest()
        final = host.store.get(**host._control(run.run_id, "agent-synthesis")["body"][
            "report_ref"
        ])
        assert "reported 3 of 20" in final["body"]["body"]
        assert final["body"]["citations"] == [
            {"sourceId": saved["old_citation"]["source_id"],
             "spanId": saved["old_citation"]["span_id"]},
            {"sourceId": saved["new_copy_ref"]["id"],
             "spanId": saved["new_span_id"]},
        ]
        assert [
            entry["input_status"] for entry in final["body"]["citation_input_provenance"]
        ] == ["exact_passage_in_producing_call"] * 2
        assert host.store.get("board_proposal", task["id"])["body"]["status"] == "completed"
        assert network_attempts == []
    finally:
        host.close()


def test_future_atlas_user_text_adapter_fixture_delivers_data_after_inflight_call(
    tmp_path, monkeypatch,
) -> None:
    """Simulate a future adapter through agent mailbox; no Atlas/user API exists here."""
    user_text = (
        "Synthetic user-pasted note says the later count may be 3 of 20; "
        "the source document and method have not been supplied."
    )
    blocked = threading.Event()
    release = threading.Event()
    network_attempts = []
    packets = []
    saved = {}

    def no_network(*_args, **_kwargs):
        network_attempts.append("socket connection")
        raise AssertionError("Offline user-text fixture attempted a network connection")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        if len(packets) == 1:
            assert user_text not in json.dumps(packet["messages"])
            blocked.set()
            assert release.wait(5), "Offline first call was not released"
            return tool("discover_capabilities", {}, 1)
        assert len(packets) == 2
        assert _last_tool(packet, "discover_capabilities")["granted"]
        index = json.loads(packet["messages"][-1]["content"])
        delivered = host.store.get("mail_message", saved["message_ref"]["id"])
        detail = next(
            item for item in index["details"]
            if item["type"] == "message" and item["ref"] == ref(delivered)
        )
        assert detail["body"]["payload"] == saved["payload"]
        assert detail["body"]["payload"]["text"] == user_text
        assert detail["body"]["task_ref"] == saved["task_ref"]
        assert detail["body"]["recipient_ref"]["id"] == saved["actor_id"]
        assert detail["body"]["sender_ref"]["id"] == saved["actor_id"]
        assert detail["body"]["payload"]["origin"] == "future_atlas_user_adapter_fixture"
        assert user_text in json.dumps(packet["messages"])
        assert user_text not in packet["messages"][0]["content"]
        source_packets = json.loads(packet["messages"][1]["content"])["source_packets"]
        assert [(item["id"], item["version"]) for item in source_packets] == [
            (saved["old_ref"]["id"], saved["old_ref"]["version"])
        ]
        assert user_text not in json.dumps(source_packets)
        return tool("publish_result", {
            "title": "Synthetic draft with unverified pasted note",
            "text": (
                "The supplied draft reported 12 of 20. A later user-pasted note "
                "suggests 3 of 20, but its source and method remain unverified."
            ),
            "citations": [saved["old_citation"]],
            "limitations": ["Later pasted text has no checked source copy"],
        }, 2)

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="future-atlas-user-text-fixture", question=QUESTION,
            scope=SCOPE, evidence_text=OLD_TEXT, agent_ids=["agent-synthesis"],
        ))
        _drive_until(host, run.run_id, blocked.is_set)
        control = host._control(run.run_id, "agent-synthesis")
        actor_id = control["body"]["actor_id"]
        saved["actor_id"] = actor_id
        task = host.store.get("board_proposal", control["body"]["task_id"])
        saved["task_ref"] = ref(task)
        old_copy = host.store.get(**control["body"]["source_refs"][0])
        saved["old_ref"] = ref(old_copy)
        passage = host.store.get("source_passage", old_copy["body"]["passage_ids"][0])
        saved["old_citation"] = {
            "source_id": old_copy["id"], "span_id": passage["body"]["span_id"],
        }
        first_call = control["body"]["call_ref"]
        first_intent = host.store.get(
            "model_request_intent", f"model-intent:{first_call['id']}:{first_call['version']}"
        )
        first_bytes = host.store.read_blob(first_intent["body"]["blob"]["sha256"])
        profile = host._run(run.run_id)["body"]["profile"]
        config = host._configuration(control)["body"]
        # The actor sender is only a fixture bridge. This is not authenticated
        # Atlas ingress, and the pasted text is not a checked source copy.
        payload = {
            "type": "simulated_user_submitted_evidence",
            "origin": "future_atlas_user_adapter_fixture",
            "actual_atlas_ingress": False,
            "submitted_at": "2026-01-02T00:02:00Z",
            "text_status": "unverified_user_paste",
            "text": user_text,
        }
        saved["payload"] = payload
        args = dict(
            mission_id=control["body"]["mission_id"],
            message_id="synthetic-user-paste-notice", sender_id=actor_id,
            recipient_id=actor_id, correlation_id="future-atlas-user-text-fixture",
            payload=payload, task_ref=ref(task), evidence_refs=[],
        )
        sent = host.mailbox.send_message("synthetic-user-paste-send", **args)
        assert host.mailbox.send_message("synthetic-user-paste-send", **args) == sent
        saved["message_ref"] = sent["message_ref"]
        assert host.store.read_blob(first_intent["body"]["blob"]["sha256"]) == first_bytes
        assert user_text.encode() not in first_bytes
        assert host._control(run.run_id, "agent-synthesis")["body"]["source_refs"] == [
            ref(old_copy)
        ]
        release.set()
        _drive_until(host, run.run_id, lambda: (
            host._run(run.run_id)["body"]["status"] == "completed"
        ))
        assert len(packets) == 2
        assert len(host.store.records("mail_message")) == 1
        assert len(host.store.records("source_copy")) == 1
        assert host.store.read_blob(first_intent["body"]["blob"]["sha256"]) == first_bytes
        assert host._run(run.run_id)["body"]["profile"] == profile
        assert host._configuration(host._control(run.run_id, "agent-synthesis"))[
            "body"
        ] == config
        report = host.store.get(**host._control(run.run_id, "agent-synthesis")["body"][
            "report_ref"
        ])
        assert report["body"]["citations"] == [{
            "sourceId": saved["old_citation"]["source_id"],
            "spanId": saved["old_citation"]["span_id"],
        }]
        assert "source and method remain unverified" in report["body"]["body"]
        assert network_attempts == []
    finally:
        release.set()
        host.close()
