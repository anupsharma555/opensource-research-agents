"""Measure the shortest observable peer-answer review route through real packets.

The injected chooser reads each serialized request and its callable schemas. It
is a mechanical diagnostic, not a claim about natural model tool selection.
"""

from __future__ import annotations

import json
import socket
import time

import pytest

from ora.contracts.models import LiveStartRequest
from ora.models.token_estimate import estimate_request_tokens
from ora.state import StateStore
from tests.mission.test_service import serialized_peer_name, service, tool


def _drive_until(host, run_id: str, predicate) -> None:
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        host._drive_run(host._run(run_id))
        if predicate():
            return
        time.sleep(.005)
    raise AssertionError("Offline peer review did not reach its saved boundary")


def _last_result(packet: dict) -> tuple[str, dict]:
    for message in reversed(packet["messages"]):
        if message.get("role") == "tool":
            return message["name"], json.loads(message["content"])
    return "", {}


def _visible_report_windows(packet: dict, report_ref: dict) -> list[dict]:
    """Use only windows actually present in this serialized model request."""
    windows = []
    for message in packet["messages"]:
        if message.get("role") != "tool" or message.get("name") != "read_context_record":
            continue
        result = json.loads(message["content"])
        if result.get("ref") == report_ref:
            windows.append(result)
        nested = result.get("related_report_window")
        if isinstance(nested, dict) and nested.get("ref") == report_ref:
            windows.append(nested)
    return windows


def _complete_visible_report(windows: list[dict]) -> str | None:
    """Return complete canonical projection only if every page is still visible."""
    by_offset = {item["offset"]: item for item in windows
                 if item.get("report_text_status") == "available"}
    offset, parts = 0, []
    while offset in by_offset:
        item = by_offset[offset]
        parts.append(item["text"])
        next_offset = item["next_offset"]
        if next_offset is None:
            return "".join(parts)
        assert next_offset > offset
        offset = next_offset
    return None


@pytest.mark.parametrize(
    ("case", "body_chars", "input_limit", "guided_pointer"),
    [("inline", 120, None, False),
     ("paged", 2100, None, False),
     ("pressured_pair", 120, 5500, False),
     ("pointer_guided_pair", 1300, None, True)],
)
def test_peer_answer_review_mechanical_path(
    tmp_path, monkeypatch, case: str, body_chars: int, input_limit: int | None,
    guided_pointer: bool,
) -> None:
    """A real saved answer is reviewed using only text and tools in sent packets."""
    def no_network(*_args, **_kwargs):
        raise AssertionError("Offline efficiency check must not open a socket")

    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket.socket, "connect_ex", no_network)
    report_body = "A bounded synthetic assessment. " + "x" * body_chars
    request_id = "efficiency-peer-request"
    question = "Does the synthetic assessment address this bounded question?"
    host = None
    run_id = None
    choice_log: list[str] = []
    input_tokens: list[int] = []
    callable_tools: list[list[str]] = []
    read_seen_in_packets: list[str] = []
    transport_failures: list[str] = []
    seen_answer = False

    def choose(request, *, timeout):
        nonlocal seen_answer
        packet = json.loads(request.data)
        role = serialized_peer_name(packet)
        if role == "Mxra":
            assert not seen_answer
            names = {item["function"]["name"] for item in packet["tools"]}
            assert "publish_result" in names
            return tool("publish_result", {
                "title": "Synthetic independent assessment", "text": report_body,
                "citations": [], "limitations": ["Synthetic example only"],
                "answer_request_id": request_id,
            }, 1)

        assert role == "Sage"
        seen_answer = True
        assert host.store.get("mail_request", request_id)["body"]["answer_refs"]
        input_tokens.append(estimate_request_tokens(request.data).tokens)
        assert len(input_tokens) <= 8, choice_log
        names = {item["function"]["name"] for item in packet["tools"]}
        callable_tools.append(sorted(names))
        answer_ref = host.store.get("mail_request", request_id)["body"]["answer_refs"][0]
        report_ref = host.store.get(**answer_ref)["body"]["artifact_ref"]
        index = json.loads(packet["messages"][-1]["content"])
        last_name, last_result = _last_result(packet)
        windows = _visible_report_windows(packet, report_ref)
        cue = next((item for item in index.get("peer_answer_review_cues", [])
                    if item["answer_ref"] == answer_ref), None)
        if cue is not None and windows and windows[0]["offset"] == 0:
            visible_text = _complete_visible_report(windows)
            if visible_text is not None:
                assert cue["report_delivery"] == "complete_in_input"
                assert "read_request" not in cue
            else:
                assert cue["read_request"]["offset"] == windows[-1]["next_offset"]
        if last_name == "read_context_record" and last_result.get("ref") == report_ref:
            assert last_result["report_text_status"] == "available"
            read_seen_in_packets.append(last_result["text"])
        complete = _complete_visible_report(windows)
        if complete is not None:
            assert json.loads(complete)["body"] == report_body
        cards = index.get("peer_answer_previews", [])
        card = next((item for item in cards if item["answer_ref"] == answer_ref), None)
        inline_complete = card is not None and card.get("report", {}).get("body") == report_body
        if guided_pointer and not choice_log:
            cue = next((item for item in index.get("peer_answer_review_cues", [])
                        if item["answer_ref"] == answer_ref), None)
            route = cue or card
            assert route is not None, "Guided pointer case requires exact answer route"
            read_request = {
                "tool": "read_context_record",
                "inclusion_id": index["older_records"]["inclusion_id"],
                **answer_ref, "offset": 0, "limit": 2000,
            }
        elif complete is not None or inline_complete:
            read_request = None
        elif windows:
            # Continue the latest visible page, or reread from the beginning
            # if packing dropped a prior page. Never use hidden Python state.
            latest = windows[-1]
            if latest["next_offset"] is None:
                raise AssertionError("Final report page is visible but prior pages were omitted")
            read_request = dict(latest["next_request"])
        else:
            cues = index.get("peer_answer_review_cues", [])
            cue = next((item for item in cues if item["answer_ref"] == answer_ref), None)
            if cue is not None:
                read_request = dict(cue["read_request"])
                assert {key: read_request[key] for key in (
                    "kind", "id", "version",
                )} == report_ref
            else:
                older = index.get("older_records", {})
                assert answer_ref in index.get("request_answer_refs", [])
                assert older.get("tool") == "read_context_page"
                assert isinstance(older.get("inclusion_id"), str)
                read_request = {
                    "tool": "read_context_record",
                    "inclusion_id": older["inclusion_id"],
                    **answer_ref, "offset": 0, "limit": 2000,
                }

        if (guided_pointer and last_name == "read_context_record"
                and last_result.get("ref") == answer_ref and complete is None):
            # The current packet's answer-pointer result is navigation data.
            # Recover its exact report route from the visible cue/card.
            cue = next((item for item in index.get("peer_answer_review_cues", [])
                        if item["answer_ref"] == answer_ref), None)
            route = cue or card
            assert route is not None
            read_request = dict(route.get("read_request") or {
                "tool": "read_context_record",
                "inclusion_id": index["older_records"]["inclusion_id"],
                **route["report_ref"], "offset": 0, "limit": 2000,
            })

        if read_request is not None:
            desired = read_request.pop("tool")
            if desired not in names:
                assert "request_capability" in names
                choice_log.append("request_reader")
                return tool("request_capability", {"tool_id": desired}, 100 + len(choice_log))
            choice_log.append("read_answer_pointer" if read_request["kind"] == "mail_answer"
                              else "read_report")
            return tool(desired, read_request, 100 + len(choice_log))

        if "review_peer_answer" not in names:
            assert "request_capability" in names
            choice_log.append("request_review")
            return tool("request_capability", {
                "tool_id": "review_peer_answer",
            }, 100 + len(choice_log))
        assert complete is not None or inline_complete, (
            "Review requires the whole report projection in the current sent model input"
        )
        visible_review_routes = [
            item for key in ("peer_answer_review_cues", "peer_answer_previews")
            for item in index.get(key, [])
        ]
        for message in packet["messages"]:
            if message.get("role") == "tool" and message.get("name") == "read_context_record":
                result = json.loads(message["content"])
                if isinstance(result.get("peer_review"), dict):
                    visible_review_routes.append(result["peer_review"])
        assert any(item.get("request_ref", {}).get("id") == request_id
                   and item.get("answer_ref") == answer_ref
                   for item in visible_review_routes), (
            "Review IDs must be visible in the current serialized request"
        )
        assert any(item.get("question") == question
                   for item in visible_review_routes), (
            "The exact peer question must be visible in the current serialized request"
        )
        choice_log.append("review")
        return tool("review_peer_answer", {
            "request_id": request_id, "answer_id": answer_ref["id"],
            "disposition": "answered",
            "reason": "The synthetic peer assessment directly addresses the bounded question.",
        }, 100 + len(choice_log))

    def transport(request, *, timeout):
        try:
            return choose(request, timeout=timeout)
        except Exception as exc:
            transport_failures.append(f"{type(exc).__name__}: {exc}")
            raise

    prepared = tmp_path / "prepared"
    checkpoint = tmp_path / "answered-checkpoint"
    host = service(prepared, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id=f"mechanical-review-{case}",
            question="Obtain one synthetic independent assessment and review it.",
            agent_ids=["agent-synthesis", "agent-critique"],
            required_agent_ids=["agent-synthesis", "agent-critique"],
        ))
        run_id = run.run_id
        requester = host._control(run_id, "agent-synthesis")
        responder = host._control(run_id, "agent-critique")
        host._update_control(
            requester, phase="retry_at", retry_at=time.time() + 86400,
            waiting_reason="Fixture waits for answer",
        )
        sent = host.mailbox.send_request(
            "mechanical-review-request", mission_id=requester["body"]["mission_id"],
            request_id=request_id, message_id="mechanical-review-message",
            requester_id=requester["body"]["actor_id"],
            recipient_id=responder["body"]["actor_id"], question=question,
            expected_artifact="A synthetic assessment",
            cause_ref=requester["body"]["proposal_ref"],
            payload={"question": question}, task_ref=requester["body"]["proposal_ref"],
        )
        assert sent["status"] == "accepted"
        host.mailbox.mark_delivered(
            "mechanical-review-delivery",
            mission_id=requester["body"]["mission_id"],
            message_id="mechanical-review-message",
        )
        _drive_until(host, run_id, lambda: bool(host.store.get("mail_request", request_id)[
            "body"]["answer_refs"]))
        assert not seen_answer
        assert not transport_failures, transport_failures
        saved = host.store.get("mail_request", request_id)
        answer_ref = saved["body"]["answer_refs"][0]
        report_ref = host.store.get(**answer_ref)["body"]["artifact_ref"]
        source_refs = host._control(run_id, "agent-synthesis")["body"]["source_refs"]
        host.store.backup(checkpoint)
    finally:
        host.close()

    labels = (["current", "counterfactual"] if case.endswith("_pair") else ["current"])
    outcomes = []
    for label in labels:
        target = tmp_path / label
        StateStore.restore(checkpoint, target)
        choice_log.clear()
        input_tokens.clear()
        callable_tools.clear()
        read_seen_in_packets.clear()
        transport_failures.clear()
        seen_answer = False
        host = service(target, monkeypatch, transport)
        try:
            assert host.store.get("mail_request", request_id)["body"]["answer_refs"] == [answer_ref]
            assert host.store.get(**answer_ref)["body"]["artifact_ref"] == report_ref
            assert host._control(run_id, "agent-synthesis")["body"]["source_refs"] == source_refs
            if label == "counterfactual" and case == "pressured_pair":
                # Disable only the new pending-review attachment rule.
                monkeypatch.setattr(host, "_pending_answer_tools", lambda *_a, **_kw: set())
            if label == "counterfactual" and case == "pointer_guided_pair":
                original_reader = host.context.read_omitted_record

                def without_related_report(*args, **kwargs):
                    if kwargs.get("pointer", {}).get("kind") != "mail_answer":
                        return original_reader(*args, **kwargs)
                    material = host.context._peer_answer_material
                    host.context._peer_answer_material = lambda *_a, **_kw: None
                    try:
                        return original_reader(*args, **kwargs)
                    finally:
                        host.context._peer_answer_material = material

                monkeypatch.setattr(host.context, "read_omitted_record", without_related_report)
            changes = {"phase": "ready_model", "waiting_reason": None, "retry_at": None}
            if input_limit is not None:
                changes["input_budget"] = {
                    "observed_limit_tokens": input_limit, "target_fraction": 0.90,
                }
            host._update_control(host._control(run_id, "agent-synthesis"), **changes)
            _drive_until(host, run_id, lambda: host.store.get("mail_request", request_id)[
                "body"].get("accepted_answer_ref") is not None)
            saved_request = host.store.get("mail_request", request_id)
            assert saved_request["body"]["accepted_answer_ref"] == answer_ref
            assert not transport_failures, transport_failures
            assert len(input_tokens) == len(choice_log), (
                "Each sent requester request must yield one selected tool choice",
                input_tokens, choice_log,
            )
            assert choice_log[-1] == "review"
            assert choice_log.count("request_reader") <= 1
            assert choice_log.count("request_review") <= 1
            assert len(read_seen_in_packets) == choice_log.count("read_report")
            assert "review_peer_answer" in callable_tools[-1]
            outcome = {
                "case": case, "branch": label,
                "requester_model_turns": len(input_tokens), "choices": list(choice_log),
                "requester_input_tokens": sum(input_tokens),
                "per_turn_input_tokens": list(input_tokens),
                "report_pages_seen": len(read_seen_in_packets),
                "review_callable_per_turn": ["review_peer_answer" in tools
                                             for tools in callable_tools],
                "reader_callable_per_turn": ["read_context_record" in tools
                                             for tools in callable_tools],
            }
            print(json.dumps(outcome, sort_keys=True))
            outcomes.append(outcome)
        finally:
            host.close()
    if case == "pressured_pair":
        current, counterfactual = outcomes
        assert current["per_turn_input_tokens"][0] <= counterfactual[
            "per_turn_input_tokens"
        ][0]
        assert "request_review" not in current["choices"]
        assert "request_review" in counterfactual["choices"]
        assert current["requester_model_turns"] + 1 <= counterfactual[
            "requester_model_turns"
        ]
        assert current["requester_input_tokens"] < counterfactual[
            "requester_input_tokens"
        ]
    if case == "pointer_guided_pair":
        current, counterfactual = outcomes
        assert current["per_turn_input_tokens"][0] == counterfactual[
            "per_turn_input_tokens"
        ][0]
        assert current["choices"][0] == counterfactual["choices"][0] == (
            "read_answer_pointer"
        )
        assert "read_report" not in current["choices"]
        assert "read_report" in counterfactual["choices"]
        assert current["requester_model_turns"] + 1 <= counterfactual[
            "requester_model_turns"
        ]
        assert current["requester_input_tokens"] < counterfactual[
            "requester_input_tokens"
        ]
    (tmp_path / "measurement.json").write_text(json.dumps({
        "case": case,
        "basis": "offline_scripted_policy_current_serialized_input_only",
        "paired_same_checkpoint": len(outcomes) == 2,
        "provider_calls": 0,
        "outcomes": outcomes,
    }, indent=2) + "\n")
