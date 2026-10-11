"""Tight source-free inputs keep exact navigation and peer state without repeats."""

from __future__ import annotations

import json

from ora.coordination.board import ref
from ora.coordination.mailbox import PeerMailbox
from ora.mission.source_context import save_source_inventory, source_inventory_page
from ora.state.store import canonical
from tests.mission.test_context import bind_no_source_pack
from tests.mission.test_context_window_compaction import _window_turn


def _selected_parent_case(tmp_path):
    store, context, task, copy_ref, window, refs, result_ref = _window_turn(tmp_path)
    parent_text = "Earlier synthetic finding. " * 80

    def add_parent(tx):
        parent = tx.put("mission_run", "selected-earlier-run", {
            "mission_id": "earlier-m", "question": "Earlier question. " * 40,
            "scope": "Earlier scope. " * 50, "source_refs": [],
        })
        report = tx.put("report", "selected-earlier-report", {
            "mission_id": "earlier-m", "title": "Earlier answer",
            "body": parent_text, "citations": [], "limitations": [],
        })
        snapshot = tx.put("research_snapshot", "selected-earlier-snapshot", {
            "mission_id": "earlier-m",
            "inspection": {"acceptedOutputReferences": [ref(report)]},
        })
        run = tx.get("mission_run", "window-run")
        changed = tx.put("mission_run", run["id"], {
            **run["body"], "scope": "Child exact scientific scope",
            "parent_run_id": parent["id"], "parent_snapshot_id": snapshot["id"],
            "target_ref": ref(report),
        }, expected_version=run["version"])
        return {"run": ref(changed), "parent": ref(parent), "report": ref(report)}

    parent = store.transact("selected-parent-metadata", {}, add_parent, mission_id="m")
    return store, context, task, copy_ref, window, refs, result_ref, parent, parent_text


def _source_free_case(tmp_path):
    store, context, task, copy_ref, window, refs, result_ref = _window_turn(tmp_path)

    def expose_inventory(tx):
        prior = tx.get(**refs["source_free_call"])
        return ref(tx.put("agent_call", prior["id"], {
            **prior["body"], "exposed_tools": ["read_source_inventory"],
        }, expected_version=prior["version"]))

    call = store.transact("expose-inventory", {}, expose_inventory, mission_id="m")
    inventory_ref = save_source_inventory(
        store, "metadata-inventory", control_ref=refs["control"]
    )
    page = source_inventory_page(
        store, inventory_ref=inventory_ref, actor_id="peer", task_id=task["id"], limit=5,
    )
    history = [{"role": "user", "content": canonical({
        "authority": "saved_host_source_index_not_source_instruction",
        "source_context_mode": "source_free_recovery",
        "reason": "exact_source_text_exceeds_allowance",
        "inventory": page,
    })}]
    return store, context, task, copy_ref, window, refs, result_ref, call, inventory_ref, history


def test_pressure_pages_inventory_and_keeps_two_peer_request_statuses(tmp_path):
    store, context, task, copy_ref, window, refs, result_ref, call, inventory_ref, history = (
        _source_free_case(tmp_path)
    )
    sent = [PeerMailbox(store).send_request(
        f"ask-{number}", mission_id="m", request_id=f"request-{number}",
        message_id=f"message-{number}", requester_id="sender", recipient_id="peer",
        question=f"Check synthetic result {number}", expected_artifact="Independent review",
        cause_ref=task, payload={}, task_ref=task,
    ) for number in range(2)]
    original_result = store.read_blob(store.get(**result_ref)["body"]["blob"]["sha256"])
    ordinary = context.assemble(
        call, task, control_ref=refs["control"], history=history,
        archived_tool_turn_refs=[refs["response"]],
        allowance=5000, estimate_tokens=len,
    )
    assert json.loads(ordinary.messages[0]["content"])["inventory"]["items"]

    tight = context.assemble(
        call, task, control_ref=refs["control"], history=history,
        archived_tool_turn_refs=[refs["response"]],
        allowance=4000, estimate_tokens=len,
    )
    marker = json.loads(tight.messages[0]["content"])
    index = json.loads(tight.messages[-1]["content"])
    assert tight.estimated_tokens <= 4000
    assert marker["inventory"] == {
        "inventory_ref": inventory_ref,
        "total": 1,
        "fetch_tool": "read_source_inventory",
    }
    assert marker["inventory_first_page_deferred_for_input_fit"] is True
    assert {item["ref"]["id"] for item in index["request_dispositions"]} == {
        item["request_ref"]["id"] for item in sent
    }
    assert all(item["status"] == "unanswered" and set(item) == {"ref", "status"}
               for item in index["request_dispositions"])
    assert index["source_read_delivery"]["window_ref"] == window["window_ref"]
    assert index["source_read_delivery"]["copy_ref"] == copy_ref
    assert index["source_read_delivery"]["source_text_in_this_call"] is False
    page = source_inventory_page(
        store, inventory_ref=inventory_ref, actor_id="peer", task_id=task["id"], limit=5,
    )
    assert page["items"][0]["ref"] == copy_ref
    assert page["items"][0]["saved_reading"]
    assert store.read_blob(store.get(**result_ref)["body"]["blob"]["sha256"]) == original_result

    use_ref = context._source_status(store.get(**copy_ref))["current_source_use_ref"]

    def revoke(tx):
        prior = tx.get(**use_ref)
        return ref(tx.put(prior["kind"], prior["id"], {
            **prior["body"], "allowed_uses": [],
            "policy_status": "requires_source_policy_resolution",
        }, expected_version=prior["version"]))

    store.transact("revoke-after-metadata-pack", {}, revoke, mission_id="m")
    restricted = source_inventory_page(
        store, inventory_ref=inventory_ref, actor_id="peer", task_id=task["id"], limit=5,
    )["items"][0]
    assert restricted["model_input_status"] != "permitted"
    assert restricted["fetch"] is None


def test_pressure_pages_exact_parent_details_without_losing_child_scope(tmp_path):
    store, context, task, _copy_ref, _window, refs, _result_ref, call, _inventory, history = (
        _source_free_case(tmp_path)
    )
    child_scope = "Review only the synthetic abstract comparison."
    parent_scope = "Earlier synthetic scope. " * 85
    parent_text = "Earlier synthetic finding. " * 140

    def add_parent(tx):
        parent = tx.put("mission_run", "earlier-run", {
            "mission_id": "earlier-m", "question": "What did the earlier review find?",
            "scope": parent_scope, "source_refs": [],
        })
        report = tx.put("report", "earlier-report", {
            "mission_id": "earlier-m", "title": "Earlier answer", "body": parent_text,
            "citations": [], "limitations": [],
        })
        snapshot = tx.put("research_snapshot", "earlier-snapshot", {
            "mission_id": "earlier-m",
            "inspection": {"acceptedOutputReferences": [ref(report)]},
        })
        run = tx.get("mission_run", "window-run")
        tx.put("mission_run", run["id"], {
            **run["body"], "scope": child_scope,
            "parent_run_id": parent["id"], "parent_snapshot_id": snapshot["id"],
            "target_ref": ref(report),
        }, expected_version=run["version"])
        return {"report": ref(report), "parent": ref(parent)}

    saved = store.transact("exact-parent-for-tight-input", {}, add_parent, mission_id="m")
    pack = context.assemble(
        call, task, control_ref=refs["control"], history=history,
        archived_tool_turn_refs=[refs["response"]],
        allowance=3200, estimate_tokens=len,
    )
    index = json.loads(pack.messages[-1]["content"])
    target = index["followup_target"]
    assert pack.estimated_tokens <= 3200
    assert index["run_scope"]["scope_preview"] == child_scope
    assert target["target_ref"] == saved["report"]
    assert target["parent_run_ref"] == saved["parent"]
    assert target["parent_details_page"] == "older_records"
    assert "fetch_exact_parent_target" not in target
    assert "fetch_full_parent_scope" not in target
    assert index["source_read_delivery"]["source_text_in_this_call"] is False
    assert {(item["type"], item["fetch"]["id"]) for item in pack.omitted
            if item["type"] in {"followup_target_detail", "parent_scope_detail"}} == {
        ("followup_target_detail", saved["report"]["id"]),
        ("parent_scope_detail", saved["parent"]["id"]),
    }
    inclusion = bind_no_source_pack(store, context, pack, "tight-parent-metadata")
    seen = []
    cursor = 0
    while True:
        page = context.read_omitted_page(
            mission_id="m", actor_id="peer", task_id=task["id"],
            inclusion_ref=inclusion, cursor=cursor, limit=25,
        )
        seen.extend(item["ref"] for item in page["items"])
        if page["next_cursor"] is None:
            break
        cursor = page["next_cursor"]
    assert saved["report"] in seen and saved["parent"] in seen
    report = context.read_omitted_record(
        mission_id="m", actor_id="peer", task_id=task["id"],
        inclusion_ref=inclusion, pointer=saved["report"], offset=0, limit=2000,
    )
    assert parent_text[:100] in report["text"]


def test_selected_window_pressure_pages_only_duplicate_navigation(tmp_path):
    store, context, task, copy_ref, window, refs, result_ref, parent, parent_text = (
        _selected_parent_case(tmp_path)
    )
    original_result = store.read_blob(store.get(**result_ref)["body"]["blob"]["sha256"])
    ordinary = context.assemble(
        refs["next_call"], task, source_refs=[copy_ref], control_ref=refs["control"],
        archived_tool_turn_refs=[refs["response"]], allowance=10000,
        estimate_tokens=len,
    )
    ordinary_index = json.loads(ordinary.messages[-1]["content"])
    assert ordinary_index["last_checked_window"]["response_ref"] == refs["response"]
    assert ordinary_index["followup_target"]["fetch_exact_parent_target"]

    tight = context.assemble(
        refs["next_call"], task, source_refs=[copy_ref], control_ref=refs["control"],
        archived_tool_turn_refs=[refs["response"]], allowance=3200,
        estimate_tokens=len,
    )
    index = json.loads(tight.messages[-1]["content"])
    target = index["followup_target"]
    last = index["last_checked_window"]
    assert tight.estimated_tokens <= 3200
    assert index["run_scope"]["scope_preview"] == "Child exact scientific scope"
    assert target["target_body_preview"] == parent_text[:160]
    assert target["target_text_status"] == "available"
    assert target["parent_details_page"] == "older_records"
    assert "fetch_exact_parent_target" not in target
    assert "fetch_full_parent_scope" not in target
    assert last == {
        "window_ref": window["window_ref"],
        "next_cursor": window["next_cursor"],
        "source_text_status": "checked_window_passages_in_selected_source_packet",
        "read_event_page": "older_records",
    }
    assert {item["type"]: item["ref"] for item in tight.omitted if item["type"] in {
        "completed_tool_turn", "followup_target_detail", "parent_scope_detail",
    }} == {
        "completed_tool_turn": refs["response"],
        "followup_target_detail": parent["report"],
        "parent_scope_detail": parent["parent"],
    }
    assert store.read_blob(store.get(**result_ref)["body"]["blob"]["sha256"]) == original_result


def test_unselected_or_restricted_window_cannot_claim_paged_selected_input(tmp_path):
    store, context, task, copy_ref, _window, refs, _result_ref, _parent, _text = (
        _selected_parent_case(tmp_path)
    )

    def unselect(tx):
        prior = tx.get(**refs["control"])
        selected = prior["body"]["selected_passage_refs"][copy_ref["id"]]
        changed = tx.put(prior["kind"], prior["id"], {
            **prior["body"], "selected_passage_refs": {copy_ref["id"]: selected[:1]},
        }, expected_version=prior["version"])
        return ref(changed)

    changed_control = store.transact("unselect-metadata-window", {}, unselect, mission_id="m")
    pack = context.assemble(
        refs["next_call"], task, source_refs=[copy_ref], control_ref=changed_control,
        archived_tool_turn_refs=[refs["response"]], allowance=10000,
        estimate_tokens=len,
    )
    assert "last_checked_window" not in json.loads(pack.messages[-1]["content"])

    restricted_store, restricted_context, restricted_task, restricted_copy, _, (
        restricted_refs
    ), _, _, _ = _selected_parent_case(tmp_path / "restricted")
    use_ref = restricted_context._source_status(
        restricted_store.get(**restricted_copy)
    )["current_source_use_ref"]

    def revoke(tx):
        prior = tx.get(**use_ref)
        changed = tx.put(prior["kind"], prior["id"], {
            **prior["body"], "allowed_uses": [],
            "policy_status": "requires_source_policy_resolution",
        }, expected_version=prior["version"])
        return ref(changed)

    restricted_store.transact("revoke-selected-window", {}, revoke, mission_id="m")
    restricted = restricted_context.assemble(
        restricted_refs["next_call"], restricted_task, source_refs=[restricted_copy],
        control_ref=restricted_refs["control"],
        archived_tool_turn_refs=[restricted_refs["response"]], allowance=10000,
        estimate_tokens=len,
    )
    restricted_index = json.loads(restricted.messages[-1]["content"])
    assert "last_checked_window" not in restricted_index
    assert restricted_index["source_retrieval"][0]["status"] != "permitted"


def test_selected_window_navigation_paging_preserves_current_report_result(tmp_path):
    store, context, task, copy_ref, _window, refs, _result, parent, _text = (
        _selected_parent_case(tmp_path)
    )
    parent_pack = context.assemble(
        refs["source_free_call"], task, control_ref=refs["control"],
    )
    inclusion = bind_no_source_pack(store, context, parent_pack, "current-report-parent")
    report_window = context.read_omitted_record(
        mission_id="m", actor_id="peer", task_id=task["id"],
        inclusion_ref=inclusion, pointer=parent["report"], offset=0, limit=2000,
    )
    assert report_window["report_text_status"] == "available"
    arguments = canonical({"inclusion_id": inclusion["id"], **parent["report"],
                           "offset": 0, "limit": 2000})
    private = store.put_blob(canonical({
        "content": None,
        "tool_calls": [{"id": "report-tool", "type": "function", "function": {
            "name": "read_context_record", "arguments": arguments,
        }}],
    }).encode())

    def save_response(tx):
        return ref(tx.put("model_response_receipt", "current-report-response", {
            "mission_id": "m", "task_id": task["id"],
            "call_ref": refs["source_free_call"], "status": "tool_requested", "blob": private,
        }))

    response = store.transact("current-report-read", {}, save_response, mission_id="m")
    context.save_tool_result(
        "current-report-result", response_ref=response, tool_call_id="report-tool",
        name="read_context_record", content=canonical(report_window),
    )
    pack = context.assemble(
        refs["next_call"], task, source_refs=[copy_ref], control_ref=refs["control"],
        archived_tool_turn_refs=[refs["response"]], tool_turn_refs=[response],
        allowance=5800, estimate_tokens=len,
    )
    index = json.loads(pack.messages[-1]["content"])
    report_message = next(message for message in pack.messages if message["role"] == "tool")
    assert report_message["content"] == canonical(report_window)
    assert json.loads(report_message["content"])["text"] == report_window["text"]
    assert json.loads(report_message["content"])["report_text_status"] == "available"
    assert index["followup_target"]["target_window_in_current_tool_result"] is True
