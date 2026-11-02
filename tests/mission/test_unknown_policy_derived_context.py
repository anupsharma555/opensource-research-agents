"""An unresolved source-use decision blocks saved derived text and dispatch."""

import pytest

from ora.coordination.board import ref
from ora.mission.context import WorkingContext
from ora.mission.memory import WorkingNotes
from ora.models import SessionError
from ora.state import StateStore
from ora.state.store import canonical
from tests.mission.test_report_dispatch_dependency import _prepared_report
from tests.mission.test_tool_result_paging import save_result


def test_unknown_policy_withholds_cached_report_and_tool_result_before_dispatch(
    tmp_path, monkeypatch,
) -> None:
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-token")
    store, bridge, intent_ref, source_ref, captured = _prepared_report(
        tmp_path, source_linked=True,
    )
    intent = store.get(**intent_ref)
    call_ref = intent["body"]["call_ref"]
    task = store.get("board_proposal", "report-task")
    control = store.get("mission_task_control", "report-child-control")
    protected = "SYNTHETIC_REPORT_TEXT_FOR_FOLLOWUP"
    cached = "SYNTHETIC_CACHED_SOURCE_RESULT"
    safe = "UNRELATED_SOURCE_FREE_NOTE"
    context = WorkingContext(store)
    response_ref, _ = save_result(
        store, call_ref, "calculate",
        {"value": cached, "source_dependency_refs": [source_ref]},
        suffix="unknown-policy-cache",
    )
    before = context.assemble(
        call_ref, ref(task), control_ref=ref(control), tool_turn_refs=[response_ref],
    )
    assert protected in canonical(before.messages)
    assert cached in canonical(before.messages)
    assert bridge.load(intent_ref, for_dispatch=True)

    note_ref = WorkingNotes(store).write(
        "unknown-policy-independent-note", mission_id="mission",
        actor_id=store.get(**call_ref)["body"]["instance_id"], task_ref=ref(task),
        note_id="unknown-policy-independent-note", category="note", text=safe,
    )

    def make_unknown(tx):
        use = tx.get("source_use", "report-use")
        return ref(tx.put("source_use", use["id"], {
            **use["body"], "allowed_uses": ["model_input"],
            "policy_status": "requires_source_policy_resolution",
        }, expected_version=use["version"]))

    store.transact("unknown-cached-source-policy", {}, make_unknown,
                   mission_id="mission")
    reopened = WorkingContext(StateStore(tmp_path))
    exchange, _ = reopened._tool_exchange(response_ref)
    assert cached not in canonical(exchange)
    assert "data_unavailable_due_source_use" in canonical(exchange)
    current = reopened.assemble(
        call_ref, ref(task), control_ref=ref(control), note_refs=[note_ref],
        tool_turn_refs=[response_ref],
    )
    serialized = canonical(current.messages)
    assert protected not in serialized and cached not in serialized
    assert safe in serialized
    assert source_ref not in reopened.context_source_dependency_refs(current)
    with pytest.raises(SessionError, match="current model-use grant"):
        bridge.invoke_capture(intent_ref)
    assert captured == []
    assert store.records("model_dispatch_attempt") == []
    assert store.get(**source_ref) is not None
