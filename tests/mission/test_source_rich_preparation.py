"""A complete checked source set may defer unused schemas without losing grants."""

from __future__ import annotations

import json

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.evidence import read_source_window
from ora.mission.source_context import plan_source_turn
from ora.mission.tool_catalog import TOOL_PURPOSES, UNHANDLED_COMMON_TOOLS
from ora.models.profiles import QWEN_MODEL
from tests.mission.test_selected_window_admission import _saved_tool_request
from tests.mission.test_service import service


def _three_source_host(tmp_path, monkeypatch, *, items=42, limit=6800):
    host = service(tmp_path, monkeypatch, lambda *_a, **_kw: (_ for _ in ()).throw(
        AssertionError("Source-rich preparation must not dispatch a model")))
    aliases = ("agent-synthesis", "agent-critique")
    texts = [
        f"SYNTHETIC_REPORT_{number}: " + " ".join(
            f"Group {index % 7} recorded comparison {index * (number + 13) % 997};"
            for index in range(items)
        ) for number in range(1, 4)
    ]
    run = host.start(LiveStartRequest(
        request_id="source-rich-complete-synthetic",
        question="Compare three synthetic reports and coordinate source-grounded review.",
        scope="Keep each cohort and time point distinct while weighing uncertainty.",
        evidence_text=texts[0], agent_ids=list(aliases), required_agent_ids=list(aliases),
        agent_models={alias: {"model": QWEN_MODEL, "reasoning_effort": "medium",
                              "max_completion_tokens": 960} for alias in aliases},
    ))
    saved_run = host._run(run.run_id)
    sources = list(saved_run["body"]["source_refs"])
    for number, value in enumerate(texts[1:], 2):
        sources.append(host._supplied_source({
            **saved_run, "id": f"{run.run_id}-synthetic-{number}",
            "body": {**saved_run["body"], "profile": {
                **saved_run["body"]["profile"], "evidenceText": value,
            }},
        }))
    host.store.transact("source-rich-link-three", {}, lambda tx: tx.put(
        "mission_run", run.run_id, {**saved_run["body"], "source_refs": sources},
        expected_version=saved_run["version"],
    ), mission_id=saved_run["body"]["mission_id"])
    for alias in aliases:
        control = host._control(run.run_id, alias)
        host._update_control(control, source_refs=sources, input_budget={
            "basis": "qualification_imposed_limit", "observed_limit_tokens": limit,
            "target_fraction": 1.0,
        })
    return host, run, sources, texts


def _prepared(host, run_id, alias):
    control = host._control(run_id, alias)
    assert host._schedule_model(control)
    prepared = host._control(run_id, alias)
    call = host.store.get(**prepared["body"]["call_ref"])
    intent = host.store.get("model_request_intent",
                            f"model-intent:{call['id']}:{call['version']}")
    raw = host.store.read_blob(intent["body"]["blob"]["sha256"])
    return prepared, call, intent, json.loads(raw), host.store.get(
        "mission_call_budget", call["id"])["body"]


def test_complete_three_source_request_keeps_text_and_requestable_reader(
    tmp_path, monkeypatch,
):
    host, run, sources, texts = _three_source_host(tmp_path, monkeypatch, limit=8000)
    try:
        for alias in ("agent-synthesis", "agent-critique"):
            _control, call, intent, packet, budget = _prepared(host, run.run_id, alias)
            packets = [source for message in packet["messages"]
                       if message["role"] == "user" and message["content"].startswith("{")
                       for source in json.loads(message["content"]).get("source_packets", [])]
            assert [item["id"] for item in packets] == [item["id"] for item in sources]
            assert all(text in item["text"] for text, item in zip(texts, packets, strict=True))
            assert [item["copy_sha256"] for item in packets] == [
                host.store.get(**source)["body"]["content_sha256"] for source in sources
            ]
            names = {item["function"]["name"] for item in packet["tools"]}
            assert {"discover_capabilities", "request_capability", "publish_result"} <= names
            assert names == set(call["body"]["exposed_tools"])
            assert "read_source_window" in call["body"]["permitted_tools"]
            availability = next(json.loads(message["content"]) for message in packet["messages"]
                                if message["role"] == "user"
                                and message["content"].startswith("{")
                                and json.loads(message["content"]).get("authority")
                                == "trusted_host_attached_tool_availability")
            assert set(availability["call_now_local_functions"]) == names
            assert "request_capability" in availability["request_first_rule"]
            instructions = packet["messages"][0]["content"]
            config = host.store.get("agent_configuration", call["body"]["configuration_id"])
            assert call["body"]["permitted_tools"] == config["body"]["permitted_tools"]
            assert all(name in instructions for name in call["body"]["permitted_tools"]
                       if name not in UNHANDLED_COMMON_TOOLS)
            current = next(json.loads(message["content"]) for message in packet["messages"]
                           if message["role"] == "user" and message["content"].startswith("{")
                           and json.loads(message["content"]).get("authority")
                           == "current_records")
            assert current["task"]["objective"] == (
                "Compare three synthetic reports and coordinate source-grounded review."
            )
            assert current["run_scope"]["scope_preview"] == (
                "Keep each cohort and time point distinct while weighing uncertainty."
            )
            assert budget["exact_complete_request_sha256"] == intent["body"]["request_sha256"]
            assert budget["exact_complete_request_estimate_tokens"] <= 8000
            assert host.session.load(ref(intent), for_dispatch=True).request.request_sha256 == (
                intent["body"]["request_sha256"]
            )
        assert not host.store.records("model_dispatch_attempt")
    finally:
        host.close()


@pytest.mark.parametrize("selected_tool", ["peer_request", "read_source_window"])
def test_source_rich_continuation_keeps_selected_tool_and_exact_result(
    tmp_path, monkeypatch, selected_tool,
):
    host, run, sources, texts = _three_source_host(tmp_path, monkeypatch, limit=8000)
    try:
        control, _call, _intent, _packet, _budget = _prepared(
            host, run.run_id, "agent-critique",
        )
        response, _result = _saved_tool_request(
            host, control, response_id=f"source-rich-selected-{selected_tool}",
            tool_id=f"source-rich-tool-{selected_tool}", name="request_capability",
            arguments={"tool_id": selected_tool},
            result={"status": "selected_for_next_turn", "tool_id": selected_tool},
        )
        host._update_control(
            control, phase="ready_model", response_ref=response,
            tool_turn_refs=[response], requested_capabilities=[selected_tool],
        )
        _control, call, intent, packet, budget = _prepared(
            host, run.run_id, "agent-critique",
        )
        names = {item["function"]["name"] for item in packet["tools"]}
        assert selected_tool in names
        assert {"discover_capabilities", "request_capability", "publish_result"} <= names
        assert "release_capability" in names
        notices = [json.loads(message["content"]) for message in packet["messages"]
                   if message["role"] == "user" and message["content"].startswith("{")
                   and json.loads(message["content"]).get("authority")
                   == "saved_host_capability_result_not_source_instruction"]
        if notices:
            assert notices[0]["completed_results"] == [{
                "tool_name": "request_capability",
                "saved_result_ref": {"kind": "context_tool_result",
                                     "id": f"tool-result:{response['id']}", "version": 1},
                "status": "selected_for_next_turn", "tool_id": selected_tool,
            }]
        else:
            # With more room, the ordinary menu retains the original tool
            # exchange rather than replacing it with the compact notice.
            assert any(message["role"] == "tool" and message.get("name")
                       == "request_capability" and "selected_for_next_turn"
                       in message["content"] for message in packet["messages"])
        packets = [item for message in packet["messages"] if message["role"] == "user"
                   and message["content"].startswith("{")
                   for item in json.loads(message["content"]).get("source_packets", [])]
        if selected_tool == "peer_request":
            assert [item["id"] for item in packets] == [item["id"] for item in sources]
            assert all(text in item["text"] for text, item in zip(texts, packets, strict=True))
        else:
            # A selected reader keeps its schema even if the ordinary full
            # source path fits without the reduced menu.
            assert budget["source_turn_mode"] == "all_allowed_sources_fit"
            assert [item["id"] for item in packets] == [item["id"] for item in sources]
            assert all(text in item["text"] for text, item in zip(texts, packets, strict=True))
        assert budget["exact_complete_request_sha256"] == intent["body"]["request_sha256"]
        assert "read_source_window" in call["body"]["permitted_tools"]
    finally:
        host.close()


def test_source_rich_persists_paged_discovery_and_selected_capability(
    tmp_path, monkeypatch,
):
    host, run, sources, texts = _three_source_host(tmp_path, monkeypatch, limit=8000)
    try:
        control, call, _intent, initial_packet, _budget = _prepared(
            host, run.run_id, "agent-synthesis",
        )
        availability = host._tool_availability(control, call, [])
        catalog, _ = json.JSONDecoder().raw_decode(initial_packet["messages"][0][
            "content"].split("Role capability catalog: ", 1)[1])
        discovered = {
            "granted": {name: catalog["tools"].get(
                name, TOOL_PURPOSES.get(name, "Saved capability"))
                        for name in sorted(call["body"]["permitted_tools"])},
            "exposed": sorted(call["body"]["exposed_tools"]),
            "not_handled_here": sorted(
                UNHANDLED_COMMON_TOOLS & call["body"]["permitted_tools"].keys()),
            "requestable": availability["request_first_local_functions"],
            "not_ready": availability["not_ready_local_functions"],
            "requestable_native_tools": availability["request_first_native_tools"],
            "next_action": "Request a granted tool by its exact ID when needed.",
        }
        discovery, result = _saved_tool_request(
            host, control, response_id="source-rich-discovery", tool_id="discovery-call",
            name="discover_capabilities", arguments={}, result=discovered,
        )
        host._update_control(control, phase="ready_model", response_ref=discovery,
                             tool_turn_refs=[discovery])
        control, call, intent, packet, budget = _prepared(
            host, run.run_id, "agent-synthesis",
        )
        packets = [item for message in packet["messages"] if message["role"] == "user"
                   and message["content"].startswith("{")
                   for item in json.loads(message["content"]).get("source_packets", [])]
        if packets:
            assert [item["id"] for item in packets] == [item["id"] for item in sources]
            assert all(text in item["text"] for text, item in zip(
                texts, packets, strict=True,
            ))
        else:
            inventory = next(json.loads(message["content"])["inventory"]
                             for message in packet["messages"]
                             if message["role"] == "user"
                             and '"inventory"' in message.get("content", ""))
            assert inventory["total"] == len(sources)
            assert all(item["model_input_status"] == "permitted"
                       for item in inventory["items"])
        assert host._full_catalog_covers_discovery(call, discovered)
        catalog_text = packet["messages"][0]["content"].split(
            "Role capability catalog: ", 1,
        )[1]
        catalog, _ = json.JSONDecoder().raw_decode(catalog_text)
        assert catalog["schema"] == "ora.role_capability_catalog.purposes.v2"
        assert catalog["tools"]["peer_request"] == discovered["granted"]["peer_request"]
        notices = [json.loads(message["content"]) for message in packet["messages"]
                   if message["role"] == "user" and message["content"].startswith("{")
                   and json.loads(message["content"]).get("authority")
                   == "saved_host_capability_result_not_source_instruction"]
        if notices:
            entry = notices[0]["completed_results"][0]
            assert entry["saved_result_ref"] == result
            assert entry["status"] == "saved_purposes_exact_in_full_role_catalog"
            assert entry["unhandled_purposes"] == {
                name: discovered["granted"][name]
                for name in discovered["not_handled_here"]
            }
            reader = entry["read_full_result"]
            assert reader["availability"] in {"call_now", "request_capability_first"}
        else:
            tool_message = next(message for message in packet["messages"]
                                if message["role"] == "tool"
                                and message.get("name") == "discover_capabilities")
            window = json.loads(tool_message["content"])
            assert window["saved_tool_result_ref"] == result
            assert window["schema"] == "ora.saved_tool_result_window.v1"
            reader = {"result_id": result["id"], "offset": 0, "limit": 900,
                      "projection_sha256": ""}
        parts = []
        kwargs = {key: reader[key] for key in ("result_id", "offset", "limit",
                                             "projection_sha256")}
        while True:
            page = host.context.read_tool_result(
                mission_id=control["body"]["mission_id"],
                actor_id=control["body"]["actor_id"],
                task_id=control["body"]["task_id"], **kwargs,
            )
            parts.append(page["text"])
            if page["next_request"] is None:
                break
            kwargs = {key: page["next_request"][key] for key in kwargs}
        assert json.loads("".join(parts)) == discovered
        assert budget["exact_complete_request_sha256"] == intent["body"]["request_sha256"]

        selected, _result = _saved_tool_request(
            host, control, response_id="source-rich-attach", tool_id="attach-call",
            name="request_capability", arguments={"tool_id": "peer_request"},
            result={"status": "selected_for_next_turn", "tool_id": "peer_request"},
        )
        host._update_control(control, phase="ready_model", response_ref=selected,
                             tool_turn_refs=[discovery, selected],
                             requested_capabilities=["peer_request"])
        _control, _call, _intent, packet, budget = _prepared(
            host, run.run_id, "agent-synthesis",
        )
        assert "peer_request" in {item["function"]["name"] for item in packet["tools"]}
        assert budget["source_turn_mode"] in {
            "all_allowed_sources_fit", "source_free_recovery",
        }
        assert (len([item for message in packet["messages"] if message["role"] == "user"
                     and message["content"].startswith("{")
                     for item in json.loads(message["content"]).get("source_packets", [])])
                == (3 if budget["source_turn_mode"] == "all_allowed_sources_fit" else 0))
    finally:
        host.close()


def test_source_rich_rejects_result_attributed_to_another_response(
    tmp_path, monkeypatch,
):
    host, run, _sources, _texts = _three_source_host(tmp_path, monkeypatch)
    try:
        control, _call, _intent, _packet, _budget = _prepared(
            host, run.run_id, "agent-synthesis",
        )
        actual_response, actual_result = _saved_tool_request(
            host, control, response_id="source-rich-real-selection",
            tool_id="same-call", name="request_capability",
            arguments={"tool_id": "peer_request"},
            result={"status": "selected_for_next_turn", "tool_id": "peer_request"},
        )
        actual_response_body = host.store.get(**actual_response)["body"]
        actual_result_body = host.store.get(**actual_result)["body"]

        def misattribute(tx):
            displayed = tx.put(
                "model_response_receipt", "source-rich-displayed-selection",
                actual_response_body,
            )
            tx.put("context_tool_result", f"tool-result:{displayed['id']}",
                   actual_result_body)
            return ref(displayed)

        displayed_ref = host.store.transact(
            "source-rich-misattributed-result", {}, misattribute,
            mission_id=control["body"]["mission_id"],
        )
        host._update_control(
            control, phase="ready_model", response_ref=displayed_ref,
            tool_turn_refs=[displayed_ref],
            requested_capabilities=["peer_request"],
        )
        assert host._source_rich_capability_results(
            host._control(run.run_id, "agent-synthesis"),
        ) is None
    finally:
        host.close()


def test_rejected_capability_keeps_exact_sources_without_grant(
    tmp_path, monkeypatch,
):
    host, run, sources, texts = _three_source_host(
        tmp_path, monkeypatch, limit=7400,
    )
    try:
        control, _call, _intent, _packet, _budget = _prepared(
            host, run.run_id, "agent-synthesis",
        )
        response, result = _saved_tool_request(
            host, control, response_id="source-rich-rejected-selection",
            tool_id="bad-selection-call", name="request_capability",
            arguments={"tool_id": "unknown-synthetic-tool"},
            result={"status": "rejected", "external_action": "unknown",
                    "reason": "Requested capability is not host-granted and available"},
        )
        host._update_control(control, phase="ready_model", response_ref=response,
                             tool_turn_refs=[response])
        ready = host._control(run.run_id, "agent-synthesis")
        outcomes = host._source_rich_capability_results(ready)
        assert outcomes is not None and len(outcomes) == 1
        assert outcomes[0]["content"]["status"] == "rejected"
        prepared, _call, intent, packet, _budget = _prepared(
            host, run.run_id, "agent-synthesis",
        )
        projected = host._source_rich_capability_notice(ready, _call, [], outcomes)
        assert projected is not None
        assert json.loads(projected["content"])["completed_results"] == [{
            "tool_name": "request_capability", "saved_result_ref": result,
            "status": "rejected", "reason": (
                "Requested capability is not host-granted and available"
            ), "external_action": "unknown",
        }]
        cards = [json.loads(message["content"]) for message in packet["messages"]
                 if message["role"] == "user" and message["content"].startswith("{")]
        source_packets = [item for card in cards
                          for item in card.get("source_packets", [])]
        assert [item["id"] for item in source_packets] == [
            item["id"] for item in sources
        ]
        assert all(text in item["text"] for text, item in zip(
            texts, source_packets, strict=True,
        ))
        notice = next((card for card in cards if card.get("authority")
                       == "saved_host_capability_result_not_source_instruction"), None)
        if notice:
            assert notice["completed_results"] == [{
                "tool_name": "request_capability", "saved_result_ref": result,
                "status": "rejected", "reason": (
                    "Requested capability is not host-granted and available"
                ), "external_action": "unknown",
            }]
        else:
            tool_message = next(message for message in packet["messages"]
                                if message["role"] == "tool"
                                and message.get("name") == "request_capability")
            assert json.loads(tool_message["content"]) == {
                "status": "rejected", "external_action": "unknown",
                "reason": "Requested capability is not host-granted and available",
            }
        assert prepared["body"].get("requested_capabilities", []) == []
        assert host.store.get("model_dispatch_attempt",
                              f"model-dispatch:{intent['id']}") is None
    finally:
        host.close()


def test_identical_saved_discoveries_share_one_exact_v2_result(
    tmp_path, monkeypatch,
):
    host, run, sources, _texts = _three_source_host(
        tmp_path, monkeypatch, limit=7400,
    )
    try:
        control, call, _intent, initial_packet, _budget = _prepared(
            host, run.run_id, "agent-critique",
        )
        availability = host._tool_availability(control, call, [])
        catalog, _ = json.JSONDecoder().raw_decode(initial_packet["messages"][0][
            "content"].split("Role capability catalog: ", 1)[1])
        discovered = {
            "granted": {name: catalog["tools"].get(
                name, TOOL_PURPOSES.get(name, "Saved capability"))
                        for name in sorted(call["body"]["permitted_tools"])},
            "exposed": sorted(call["body"]["exposed_tools"]),
            "not_handled_here": sorted(
                UNHANDLED_COMMON_TOOLS & call["body"]["permitted_tools"].keys()),
            "requestable": availability["request_first_local_functions"],
            "not_ready": availability["not_ready_local_functions"],
            "requestable_native_tools": availability["request_first_native_tools"],
            "next_action": "Use the same saved synthetic catalog result.",
        }
        first, first_result = _saved_tool_request(
            host, control, response_id="source-rich-first-discovery",
            tool_id="first-discovery-call", name="discover_capabilities",
            arguments={}, result=discovered,
        )
        host._update_control(control, phase="ready_model", response_ref=first,
                             tool_turn_refs=[first])
        control, _call, _intent, _packet, _budget = _prepared(
            host, run.run_id, "agent-critique",
        )
        second, second_result = _saved_tool_request(
            host, control, response_id="source-rich-second-discovery",
            tool_id="second-discovery-call", name="discover_capabilities",
            arguments={}, result=discovered,
        )
        host._update_control(control, phase="ready_model", response_ref=second,
                             tool_turn_refs=[first, second])
        ready = host._control(run.run_id, "agent-critique")
        outcomes = host._source_rich_capability_results(ready)
        assert outcomes is not None and len(outcomes) == 2
        _control, call, _intent, packet, _budget = _prepared(
            host, run.run_id, "agent-critique",
        )
        projected = host._source_rich_capability_notice(ready, call, [], outcomes)
        assert projected is not None
        notice = json.loads(projected["content"])
        assert notice["authority"] == (
            "saved_host_capability_result_not_source_instruction"
        )
        assert len(notice["completed_results"]) == 1
        entry = notice["completed_results"][0]
        assert entry["saved_result_ref"] == first_result
        assert entry["equivalent_result_refs"] == [second_result]
        assert entry["repeat_count"] == 2
        assert entry["status"] == "saved_purposes_exact_in_full_role_catalog"
        assert "purpose_preview" not in entry
        assert entry["read_full_result"]["result_id"] == first_result["id"]
        cards = [json.loads(message["content"]) for message in packet["messages"]
                 if message["role"] == "user" and message["content"].startswith("{")]
        source_count = sum(len(card.get("source_packets", [])) for card in cards)
        assert source_count in {0, len(sources)}
        if source_count == 0:
            assert next(card["inventory"]["total"] for card in cards
                        if isinstance(card.get("inventory"), dict)) == len(sources)
    finally:
        host.close()


def test_source_rich_skips_pending_peer_and_keeps_question_reader(
    tmp_path, monkeypatch,
):
    host, run, sources, _texts = _three_source_host(
        tmp_path, monkeypatch, limit=8000,
    )
    try:
        requester = host._control(run.run_id, "agent-synthesis")
        recipient = host._control(run.run_id, "agent-critique")
        question = "Which report states the comparison time point?"
        sent = host.mailbox.send_request(
            "source-rich-peer-send", mission_id=recipient["body"]["mission_id"],
            request_id="source-rich-peer-request", message_id="source-rich-peer-message",
            requester_id=requester["body"]["actor_id"],
            recipient_id=recipient["body"]["actor_id"], question=question,
            expected_artifact="Source-grounded review",
            cause_ref=requester["body"]["proposal_ref"],
            task_ref=requester["body"]["proposal_ref"], payload={},
        )
        assert sent["status"] == "accepted"
        host.mailbox.mark_delivered("source-rich-peer-deliver",
                                    mission_id=recipient["body"]["mission_id"],
                                    message_id="source-rich-peer-message")
        plan = plan_source_turn(host.store, ref(recipient),
                                source_input_allowance_tokens=20_000)
        assert not host._complete_source_rich_candidate(recipient, plan, sources, [])
        _control, call, _intent, packet, _budget = _prepared(
            host, run.run_id, "agent-critique",
        )
        names = {item["function"]["name"] for item in packet["tools"]}
        assert names != {"discover_capabilities", "request_capability", "publish_result"}
        assert "read_context_record" in call["body"]["permitted_tools"]
        current = next(json.loads(message["content"]) for message in packet["messages"]
                       if message["role"] == "user" and message["content"].startswith("{")
                       and json.loads(message["content"]).get("authority")
                       == "current_records")
        assert current["incoming_request"]["question"] == question
    finally:
        host.close()


@pytest.mark.parametrize("boundary", ["partial", "revoked", "active_skill"])
def test_source_rich_requires_whole_current_permitted_copies(
    tmp_path, monkeypatch, boundary,
):
    host, run, sources, texts = _three_source_host(tmp_path, monkeypatch)
    try:
        control = host._control(run.run_id, "agent-synthesis")
        if boundary == "partial":
            window = read_source_window(host.evidence, "source-rich-partial-read",
                                        sources[0], block_offset=0, block_limit=1,
                                        char_offset=0, char_limit=350)
            host._update_control(control, selected_passage_refs={
                sources[0]["id"]: window["passage_refs"],
            })
        elif boundary == "revoked":
            use_ref = host.context._source_status(host.store.get(**sources[0]))[
                "current_source_use_ref"
            ]

            def revoke(tx):
                use = tx.get(**use_ref)
                changed = tx.put(use["kind"], use["id"], {
                    **use["body"], "allowed_uses": [],
                    "policy_status": "requires_source_policy_resolution",
                }, expected_version=use["version"])
                return ref(changed)

            host.store.transact("source-rich-revoke", {}, revoke,
                                mission_id=control["body"]["mission_id"])
        else:
            host._update_control(control, active_skill_ids=["task-planning"])
        control = host._control(run.run_id, "agent-synthesis")
        allowed, excluded = host._source_policy_decisions(control["body"]["source_refs"])
        plan = plan_source_turn(host.store, ref(control),
                                source_input_allowance_tokens=20_000)
        assert not host._complete_source_rich_candidate(control, plan, allowed, excluded)
        if boundary == "active_skill":
            return  # The synthetic active pin has no installed skill runtime.
        _control, call, _intent, packet, budget = _prepared(
            host, run.run_id, "agent-synthesis",
        )
        names = {item["function"]["name"] for item in packet["tools"]}
        assert names != {"discover_capabilities", "request_capability", "publish_result"}
        if boundary == "revoked":
            assert budget["source_turn_mode"] == "source_free_recovery"
            assert budget["exact_complete_request_estimate_tokens"] <= 6800
            payloads = [json.loads(message["content"]) for message in packet["messages"]
                        if message["role"] == "user"
                        and message["content"].startswith("{")]
            inventory_notice = next(item for item in payloads if item.get("authority")
                                    == "saved_host_source_index_not_source_instruction")
            assert inventory_notice["source_context_mode"] == "source_free_recovery"
            assert [item["ref"] for item in inventory_notice["inventory"]["items"]] == sources
            assert [item["model_input_status"] for item in
                    inventory_notice["inventory"]["items"]] == [
                        "restricted", "permitted", "permitted",
                    ]
            assert host.store.get(**inventory_notice["inventory"]["inventory_ref"])
            assert inventory_notice["working_sources_ref"] is None
            assert inventory_notice["working_sources_expected_version"] == 0
            action = inventory_notice["next_action"]
            assert "read_source_window" in action
            assert "optional" in action
            assert "working_sources_expected_version" in action
            assert "not source/inventory" in action
            exclusion = next(item for item in payloads if item.get("authority")
                             == "saved_host_source_use_decision_not_source_instruction")
            assert exclusion["excluded_source_refs"] == [sources[0]]
            assert len(exclusion["decision_refs"]) == 1
            assert host.store.get(**exclusion["decision_refs"][0])
            current = next(item for item in payloads if item.get("authority")
                           == "current_records")
            navigation = current["source_free_reading_navigation"]
            assert [item["copy_ref"] for item in navigation] == sources[1:]
            assert all(item["fetch_first_uncovered"]["tool"] == "read_source_window"
                       and item["fetch_first_uncovered"]["source_id"] == item[
                           "copy_ref"]["id"]
                       and item["fetch_first_uncovered"]["source_version"] == item[
                           "copy_ref"]["version"]
                       and item["source_text_in_this_call"] is False
                       for item in navigation)
            assert "read_source_window" in call["body"]["permitted_tools"]
            serialized = json.dumps(packet, sort_keys=True)
            assert all(text not in serialized for text in texts)
            assert not host.store.records("model_dispatch_attempt")
            sent = [item for message in packet["messages"] if message["role"] == "user"
                    and message["content"].startswith("{")
                    for item in json.loads(message["content"]).get("source_packets", [])]
            assert sources[0]["id"] not in {item["id"] for item in sent}
    finally:
        host.close()


def test_oversize_three_source_request_keeps_inventory_and_reader(
    tmp_path, monkeypatch,
):
    host, run, sources, _texts = _three_source_host(tmp_path, monkeypatch, items=70)
    try:
        _control, call, _intent, packet, budget = _prepared(
            host, run.run_id, "agent-synthesis",
        )
        assert budget["source_turn_mode"] == "source_free_recovery"
        assert not [item for message in packet["messages"]
                    if message["role"] == "user" and message["content"].startswith("{")
                    for item in json.loads(message["content"]).get("source_packets", [])]
        assert "read_source_window" in {
            item["function"]["name"] for item in packet["tools"]
        }
        overview = next(json.loads(message["content"]) for message in packet["messages"]
                        if message["role"] == "user" and message["content"].startswith("{")
                        and json.loads(message["content"]).get("source_context_mode")
                        == "source_free_recovery")
        assert {row["ref"]["id"] for row in overview["inventory"]["items"]} == {
            pointer["id"] for pointer in sources
        }
        assert "read_source_window" in call["body"]["permitted_tools"]
    finally:
        host.close()
