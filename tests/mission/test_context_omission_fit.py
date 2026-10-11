"""Exact omission navigation participates in packing before optional detail compaction."""

import json
import math

from ora.coordination.board import ref
from ora.evidence import read_source_window
from ora.mission.context import ContextOverflow, WorkingContext, _omission_marker
from ora.state.store import canonical
from tests.evidence.test_coverage import checked_copy
from tests.mission.test_context import compact_checked_window_fixture
from tests.models.test_session import _preview_task, select, setup


def test_omission_marker_cannot_make_more_room_fail_after_smaller_context_fits(tmp_path):
    text = 'DOI: 10.1234/coverage. ' + 'A' * 1000
    library, saved = checked_copy(tmp_path, text.encode(), selected=[text[:100]])
    window = read_source_window(library, 'window', saved['copy_ref'],
                                block_offset=0, char_offset=0, char_limit=500)
    compact_checked_window_fixture(library, saved, window)
    store = library.store
    original_call = store.get('agent_call', 'call')
    call = store.transact('next-call', {}, lambda tx: ref(tx.put(
        'agent_call', 'next-call-' + 'c' * 192, original_call['body'],
    )), mission_id='m')
    task = ref(store.get('board_proposal', 'task'))
    control = ref(store.get('mission_task_control', 'compact-window-control'))
    response = ref(store.get('model_response_receipt', 'compact-window-response'))
    dissent = store.transact('dissent', {}, lambda tx: ref(tx.put('board_dissent', 'dissent', {
        'mission_id': 'm', 'proposal_ref': task, 'critique': 'Check the selected evidence. ' * 100,
    })), mission_id='m')
    context = WorkingContext(store)
    kwargs = dict(source_refs=[saved['copy_ref']], control_ref=control,
                  archived_tool_turn_refs=[response], estimate_tokens=len)
    measurements = []
    def measure(value):
        measurements.append(value)
        return len(value)
    context.assemble(call, task, **{**kwargs, 'estimate_tokens': measure})
    # This admits the unreserved index but not the final required omission link.
    boundary = len(measurements[0]) + 10
    for allowance in [boundary - 100, boundary, boundary + 50]:
        pack = context.assemble(call, task, allowance=allowance, **kwargs)
        projected = json.loads(pack.messages[-1]['content'])
        assert pack.estimated_tokens <= allowance
        assert projected['task_ref'] == task
        assert projected['omitted']['count'] == len(pack.omitted)
        assert 'fetch' not in projected['omitted']
        assert projected['older_records']['tool'] == 'read_context_page'
        assert dissent in [item['ref'] for item in pack.omitted]
        assert response in [item['ref'] for item in pack.omitted]
        assert saved['copy_ref'] in pack.source_refs
        assert projected['last_checked_window'].get('text_status') == (
            'exact_checked_passage_in_selected_source_packet'
        ) or projected['last_checked_window'].get('source_text_status') == (
            'checked_window_passages_in_selected_source_packet'
        )


def test_tiny_detail_replaces_long_omission_envelope_before_early_size_failure(tmp_path):
    from ora.state import StateStore
    from tests.mission.test_context import fixture

    store = StateStore(tmp_path)
    original_call, task = fixture(store)
    call = store.transact('long-call', {}, lambda tx: ref(tx.put(
        'agent_call', 'long-call-' + 'c' * 192, store.get(**original_call)['body'],
    )), mission_id='m')
    tiny = store.transact('tiny', {}, lambda tx: ref(tx.put(
        'source_passage', 'tiny', {},
    )), mission_id='m')
    context = WorkingContext(store)
    full = context.assemble(call, task, evidence_refs=[tiny], estimate_tokens=len)
    packed = context.assemble(call, task, evidence_refs=[tiny],
                              allowance=full.estimated_tokens, estimate_tokens=len)
    assert tiny in packed.included_refs
    assert packed.omitted == ()
    assert packed.estimated_tokens <= full.estimated_tokens


def test_distinct_omission_fetch_is_preserved():
    page = {"tool": "read_context_page", "inclusion_id": "context:next", "cursor": 0}
    older = {"tool": "read_context_page", "inclusion_id": "context:prior", "cursor": 0}
    assert _omission_marker(2, page, older) == {"count": 2, "fetch": page}
    assert _omission_marker(2, page, page) == {"count": 2}


def test_complete_near_limit_preview_keeps_source_and_current_tool_result(tmp_path):
    store, _, bridge, call, _, source, _ = setup(tmp_path)
    _preview_task(store)
    task = ref(store.get("board_proposal", "task"))
    private = store.put_blob(canonical({
        "content": None,
        "tool_calls": [{
            "id": "discover-1", "type": "function", "function": {
                "name": "evidence_lookup", "arguments": canonical({"source_id": source["id"]}),
            },
        }],
    }).encode())

    def save(tx):
        return ref(tx.put("model_response_receipt", "current-tool-response", {
            "mission_id": "mission", "task_id": task["id"], "call_ref": ref(call),
            "status": "tool_requested", "blob": private,
        }))

    response = store.transact("current-tool-response", {}, save, mission_id="mission")
    context = WorkingContext(store)
    context.save_tool_result(
        "current-tool-result", response_ref=response, tool_call_id="discover-1",
        name="evidence_lookup", content=canonical({"status": "ready", "source_id": source["id"]}),
    )
    # Force an omission so the two page coordinates would otherwise be repeated.
    store.transact("large-critique", {}, lambda tx: ref(tx.put("board_dissent", "large-critique", {
        "mission_id": "mission", "proposal_ref": task, "actor_id": call["body"]["instance_id"],
        "critique": "Review the synthetic source. " * 200,
    })), mission_id="mission")
    def estimate(value):
        return math.ceil(len(value) / 4 * 1.25)
    full = context.assemble(ref(call), task, source_refs=[source],
                            tool_turn_refs=[response], estimate_tokens=estimate)
    for allowance in range(1, full.estimated_tokens + 1):
        try:
            pack = context.assemble(
                ref(call), task, source_refs=[source], tool_turn_refs=[response],
                allowance=allowance, estimate_tokens=estimate,
            )
            if pack.omitted:
                break
        except ContextOverflow:
            continue
    else:
        raise AssertionError("Expected a minimum source and tool context with an omission")

    current = json.loads(pack.messages[-1]["content"])
    assert current["omitted"]["count"] == len(pack.omitted)
    assert "fetch" not in current["omitted"]
    assert current["older_records"]["tool"] == "read_context_page"
    assert pack.messages[-2]["role"] == "tool"
    prepared = bridge.preview_request(
        call=call, phase="collect", messages=pack.messages, sources=select(source),
    )
    request = prepared.body
    source_packets = [
        item for message in request["messages"]
        if isinstance(message.get("content"), str) and message["content"].startswith("{")
        for item in json.loads(message["content"]).get("source_packets", [])
    ]
    assert len(source_packets) == 1
    assert source_packets[0]["id"] == source["id"]
    assert "Synthetic abstract reports" in source_packets[0]["text"]
    tool_messages = [m for m in request["messages"] if m.get("role") == "tool"]
    assert len(tool_messages) == 1
    assert json.loads(tool_messages[0]["content"])["status"] == "ready"
    accepted_estimate = estimate(canonical(request))
    duplicated = json.loads(canonical(request))
    index = json.loads(duplicated["messages"][-1]["content"])
    index["omitted"]["fetch"] = index["older_records"]
    duplicated["messages"][-1]["content"] = canonical(index)
    previous_estimate = estimate(canonical(duplicated))
    assert accepted_estimate < previous_estimate
    hard_target = previous_estimate - 1
    assert accepted_estimate <= hard_target
    saved = bridge.prepare(
        call_id=call["id"], call_version=call["version"], phase="collect",
        messages=pack.messages, sources=select(source),
    )
    inclusion = context.save_inclusion("near-limit-inclusion", pack, saved.intent_ref)
    assert store.get(**inclusion)["body"]["omitted"]
