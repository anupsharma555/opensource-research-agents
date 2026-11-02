"""A fitting one-source turn can ask a peer without displacing checked evidence."""

from __future__ import annotations

import json

from ora.coordination.board import ref
from ora.evidence import read_source_window
from ora.models.token_estimate import estimate_request_tokens
from tests.mission.test_selected_window_admission import _saved_tool_request
from tests.mission.test_source_rich_preparation import _three_source_host


def _start(tmp_path, monkeypatch):
    host, run, sources, texts = _three_source_host(
        tmp_path, monkeypatch, items=35, limit=6900,
    )
    control = host._control(run.run_id, "agent-critique")
    host._update_control(control, input_budget={
        "basis": "qualification_imposed_limit", "observed_limit_tokens": 6900,
        "target_fraction": 0.9,
    })
    assert host._schedule_model(host._control(run.run_id, "agent-critique"))
    return host, run, sources, texts


def _read(host, run, source, number):
    control = host._control(run.run_id, "agent-critique")
    window = read_source_window(
        host.evidence, f"synthetic-peer-fit-read-{number}", source,
        block_offset=0, block_limit=1, char_offset=0, char_limit=5000,
    )
    window.update(
        requested_char_limit=5000, effective_char_limit=5000,
        returned_chars=sum(len(item["text_preview"]) for item in window["passages"]),
        remaining_in_block_chars=0, next_request=None,
        model_selection={
            "mode": "replace", "passage_refs": window["passage_refs"],
            "prior_passage_refs": control["body"]["selected_passage_refs"].get(
                source["id"], [],
            ),
        },
    )
    window["passages"] = [
        {**item, "span_id": host.store.get(**item["passage_ref"])["body"]["span_id"]}
        for item in window["passages"]
    ]
    response, _ = _saved_tool_request(
        host, control, response_id=f"synthetic-peer-fit-response-{number}",
        tool_id=f"synthetic-peer-fit-tool-{number}", name="read_source_window",
        arguments={
            "source_id": source["id"], "source_version": source["version"],
            "block_offset": 0, "char_offset": 0, "char_limit": 5000,
            "selection_mode": "replace",
        }, result=window,
    )
    host._update_control(
        control, phase="ready_model", response_ref=response,
        tool_turn_refs=[*control["body"]["tool_turn_refs"], response],
        read_window_refs=[*control["body"]["read_window_refs"], window["window_ref"]],
        selected_passage_refs={**control["body"]["selected_passage_refs"],
                               source["id"]: window["passage_refs"]},
    )


def _next(host, run):
    assert host._schedule_model(host._control(run.run_id, "agent-critique"))
    control = host._control(run.run_id, "agent-critique")
    call = host.store.get(**control["body"]["call_ref"])
    intent = host.store.get(
        "model_request_intent", f"model-intent:{call['id']}:{call['version']}",
    )
    raw = host.store.read_blob(intent["body"]["blob"]["sha256"])
    packet = json.loads(raw)
    budget = host.store.get("mission_call_budget", call["id"])["body"]
    cards = [json.loads(message["content"]) for message in packet["messages"]
             if message["role"] == "user" and message["content"].startswith("{")]
    packets = [item for card in cards for item in card.get("source_packets", [])]
    return call, intent, raw, packet, budget, cards, packets


def test_one_source_attaches_peer_only_after_exact_qwen_fit(tmp_path, monkeypatch):
    host, run, sources, texts = _start(tmp_path, monkeypatch)
    try:
        _read(host, run, sources[0], 1)
        call, intent, raw, packet, budget, cards, packets = _next(host, run)
        names = {item["function"]["name"] for item in packet["tools"]}
        assert "peer_request" in names
        peer_tool = next(item["function"] for item in packet["tools"]
                         if item["function"]["name"] == "peer_request")
        assert "complete focused question" in peer_tool["description"]
        assert {"request_capability", "publish_result", "read_source_window"} <= names
        assert [item["id"] for item in packets] == [sources[0]["id"]]
        assert texts[0] in packets[0]["text"]
        current = next(item for item in cards if item.get("authority") == "current_records")
        assert current["task"]["objective"] == (
            "Compare three synthetic reports and coordinate source-grounded review."
        )
        assert len(current["run_scope"]["inherited_source_refs"]) == 3
        availability = next(item for item in cards if item.get("authority")
                            == "trusted_host_attached_tool_availability")
        assert availability["peer_routes"]["ask_registered"] == "peer_request"
        assert budget["exact_complete_request_sha256"] == intent["body"][
            "request_sha256"
        ]
        assert budget["exact_complete_request_estimate_tokens"] == (
            estimate_request_tokens(raw).tokens
        ) <= 6900
        assert names == set(call["body"]["exposed_tools"])
        assert host.session.load(ref(intent), for_dispatch=True).request.request_sha256 == (
            intent["body"]["request_sha256"]
        )
        assert not host.store.records("model_dispatch_attempt")
    finally:
        host.close()


def test_two_sources_keep_text_and_requestable_peer_route(tmp_path, monkeypatch):
    host, run, sources, texts = _start(tmp_path, monkeypatch)
    try:
        _read(host, run, sources[0], 1)
        _next(host, run)
        _read(host, run, sources[1], 2)
        _call, intent, raw, packet, budget, cards, packets = _next(host, run)
        assert {item["function"]["name"] for item in packet["tools"]} == {
            "discover_capabilities", "request_capability", "publish_result",
            "read_source_window",
        }
        assert {item["id"] for item in packets} == {sources[0]["id"], sources[1]["id"]}
        assert all(any(text in item["text"] for item in packets) for text in texts[:2])
        assert len(next(item for item in cards if item.get("authority")
                        == "current_records")["run_scope"]["inherited_source_refs"]) == 3
        availability = next(item for item in cards if item.get("authority")
                            == "trusted_host_attached_tool_availability")
        assert availability["peer_routes"]["ask_registered"] == (
            "request_capability(tool_id=peer_request)"
        )
        assert budget["exact_complete_request_sha256"] == intent["body"][
            "request_sha256"
        ]
        assert estimate_request_tokens(raw).tokens <= 6900
        assert not host.store.records("model_dispatch_attempt")
    finally:
        host.close()


def test_no_registered_peer_does_not_attach_optional_schema(
    tmp_path, monkeypatch,
):
    host, run, sources, _texts = _start(tmp_path, monkeypatch)
    try:
        _read(host, run, sources[0], 1)
        monkeypatch.setattr(host, "_registered_peer_targets", lambda *_args: [])
        _call, _intent, _raw, packet, _budget, _cards, packets = _next(host, run)
        assert len(packets) == 1
        assert "peer_request" not in {
            item["function"]["name"] for item in packet["tools"]
        }
    finally:
        host.close()


def test_released_peer_is_not_reattached_by_optional_path(tmp_path, monkeypatch):
    host, run, sources, _texts = _start(tmp_path, monkeypatch)
    try:
        _read(host, run, sources[0], 1)
        control = host._control(run.run_id, "agent-critique")
        config = host._configuration(control, [sources[0]])
        exposed, _ = host._exposed_tools(
            control, config, defer_core_tools=True,
            fitting_peer_request=True, released_for_next_turn="peer_request",
        )
        assert "peer_request" not in exposed
    finally:
        host.close()


def test_pending_question_keeps_exact_reply_route_without_optional_peer(
    tmp_path, monkeypatch,
):
    host, run, sources, _texts = _start(tmp_path, monkeypatch)
    try:
        _read(host, run, sources[0], 1)
        sender = host._control(run.run_id, "agent-synthesis")
        recipient = host._control(run.run_id, "agent-critique")
        question = "Check the synthetic first report comparison."
        sent = host.mailbox.send_request(
            "synthetic-peer-fit-send", mission_id=sender["body"]["mission_id"],
            request_id="synthetic-peer-fit-request",
            message_id="synthetic-peer-fit-message",
            requester_id=sender["body"]["actor_id"],
            recipient_id=recipient["body"]["actor_id"], question=question,
            expected_artifact="A source-bounded answer",
            cause_ref=sender["body"]["proposal_ref"], payload={},
            task_ref=sender["body"]["proposal_ref"],
        )
        assert sent["status"] == "accepted"
        assert host.mailbox.mark_delivered(
            "synthetic-peer-fit-deliver", mission_id=sender["body"]["mission_id"],
            message_id="synthetic-peer-fit-message",
        )["status"] == "accepted"
        _call, intent, raw, packet, budget, cards, packets = _next(host, run)
        names = {item["function"]["name"] for item in packet["tools"]}
        assert "peer_request" not in names
        assert {"publish_result", "read_source_window"} <= names
        assert len(packets) == 1
        incoming = next(item for item in cards if item.get("authority")
                        == "current_records")["incoming_request"]
        assert incoming["question"] == question
        assert incoming["saved_question_has_no_hidden_suffix"] is True
        assert incoming["reply"] == {
            "tool": "publish_result", "answer_request_id": "synthetic-peer-fit-request",
        }
        assert budget["exact_complete_request_sha256"] == intent["body"][
            "request_sha256"
        ]
        assert estimate_request_tokens(raw).tokens <= 6900
    finally:
        host.close()


def test_revoked_selected_source_is_not_reintroduced_for_peer_fit(
    tmp_path, monkeypatch,
):
    host, run, sources, texts = _start(tmp_path, monkeypatch)
    try:
        _read(host, run, sources[0], 1)
        control = host._control(run.run_id, "agent-critique")
        use_ref = host.context._source_status(host.store.get(**sources[0]))[
            "current_source_use_ref"
        ]
        prior = host.store.get(**use_ref)
        host.store.transact(
            "synthetic-peer-fit-revoke", {},
            lambda tx: ref(tx.put(prior["kind"], prior["id"], {
                **prior["body"], "allowed_uses": [],
                "policy_status": "requires_source_policy_resolution",
            }, expected_version=prior["version"])),
            mission_id=control["body"]["mission_id"],
        )
        _call, _intent, raw, packet, _budget, _cards, packets = _next(host, run)
        assert not packets
        assert texts[0] not in raw.decode()
        assert "peer_request" not in {
            item["function"]["name"] for item in packet["tools"]
        }
    finally:
        host.close()
