"""Saved dual-peer requests remain honest in local reuse reports."""

from __future__ import annotations

import hashlib
import json

from ora.observability import build_context_metrics
from ora.state import StateStore
from ora.state.store import canonical
from scripts import trace_run
from scripts.report_api_runs import local_reuse_text
from tests.observability.test_metrics import _put, _ref, _seed_reuse_run


def _add_call(store: StateStore, name: str, actor: str, *, tools: list,
              response_status: str | None = "complete", readable: bool = True) -> None:
    call = _put(store, "agent_call", name, {
        "mission_id": "mission-a", "instance_id": actor, "task_id": "task-a",
        "attempt_id": f"attempt-{name}", "provider": "groq",
        "model_id": "openai/gpt-oss-120b",
    })
    raw = canonical({
        "model": "openai/gpt-oss-120b", "tools": tools,
        "messages": [{"role": "developer", "content": "Shared synthetic guidance"},
                     {"role": "user", "content": "Synthetic follow-up"}],
    }).encode()
    digest = hashlib.sha256(raw).hexdigest()
    intent = _put(store, "model_request_intent", f"intent-{name}", {
        "mission_id": "mission-a", "call_ref": _ref(call),
        "task_id": "task-a", "instance_id": actor,
        "attempt_id": f"attempt-{name}",
        "credential_profile": "synthetic_profile", "request_sha256": digest,
        "blob": store.put_blob(raw) if readable else None,
    })
    if response_status is None:
        return  # Prepared request, never dispatched.
    marker = _put(store, "model_dispatch_attempt", f"model-dispatch:{intent['id']}", {
        "mission_id": "mission-a", "intent_ref": _ref(intent),
        "call_ref": _ref(call), "credential_profile": "synthetic_profile",
        "request_sha256": digest, "status": "attempted_external_effect",
    })
    _put(store, "model_response_receipt", f"model-response:{intent['id']}", {
        "mission_id": "mission-a", "intent_ref": _ref(intent),
        "call_ref": _ref(call), "task_id": "task-a",
        "attempt_id": f"attempt-{name}", "request_sha256": digest,
        "status": response_status, "credential_profile": "synthetic_profile",
        "cache_observation": {"status": "not_reported"},
    })
    store.transact(f"capture-{name}", {}, lambda tx: tx.put(
        "model_dispatch_attempt", marker["id"],
        {**marker["body"], "status": "captured"}, expected_version=1,
    ))


def test_dual_peer_reuse_trace_is_weighted_and_preserves_coverage(tmp_path, capsys,
                                                                  monkeypatch):
    store = StateStore(tmp_path)
    _seed_reuse_run(store)
    original_tools = [{"type": "function", "function": {
        "name": "synthetic_tool", "description": "Synthetic example",
        "parameters": {"type": "object", "properties": {}},
    }}]
    extra_tool = {"type": "function", "function": {
        "name": "other_tool", "parameters": {"type": "object"},
    }}
    _add_call(store, "call-a-rejected", "actor-a", tools=[*original_tools, extra_tool],
              response_status="provider_rejected")
    _add_call(store, "call-b-followup", "actor-b", tools=original_tools)
    _add_call(store, "call-a-unreadable", "actor-a", tools=original_tools,
              readable=False)
    _add_call(store, "call-a-prepared", "actor-a", tools=original_tools,
              response_status=None)
    _put(store, "agent_call", "call-b-rejected-before-preparation", {
        "mission_id": "mission-a", "instance_id": "actor-b", "task_id": "task-a",
    })

    before = (tmp_path / "state.sqlite3").read_bytes()
    metrics = build_context_metrics(StateStore(tmp_path, read_only=True), "run-a")
    reuse = metrics["local_prefix_reuse"]
    rows = {row["call_id"]: row for row in reuse["structural_input_calls"]}
    assert len(rows) == 6  # Only dispatched calls with receipts become candidates.
    assert reuse["captured_terminal_model_calls"] == 6
    assert reuse["eligible_requests"] == 5
    assert reuse["request_unavailable_calls"] == 1
    assert reuse["structural_coverage_status"] == "partial"
    assert reuse["structural_measured_calls"] == 5
    assert reuse["structural_measured_followup_pairs"] == 3
    assert rows["call-0"]["status"] == rows["call-2"]["status"] == "baseline_no_predecessor"
    assert rows["call-a-rejected"]["status"] == "measured"
    assert rows["call-a-rejected"]["prior_call_id"] == "call-1"
    assert rows["call-a-rejected"]["unchanged_whole_schema_tokens"] == 0
    assert rows["call-a-rejected"]["tool_definition_overlap_status"] == "measured_changed_schema"
    assert rows["call-a-rejected"]["exact_shared_tool_definition_count"] == 1
    assert rows["call-b-followup"]["prior_call_id"] == "call-2"
    assert rows["call-b-followup"]["unchanged_whole_schema_tokens"] > 0
    assert rows["call-a-unreadable"]["status"] == "unavailable"
    assert rows["call-a-rejected"]["scope"]["actor_id"] == "actor-a"
    assert rows["call-b-followup"]["scope"]["actor_id"] == "actor-b"
    assert {row["scope"]["task_id"] for row in rows.values()} == {"task-a"}
    assert "call-a-prepared" not in rows
    assert "call-b-rejected-before-preparation" not in rows
    measured = [row for row in rows.values() if row["status"] in {
        "baseline_no_predecessor", "measured",
    }]
    assert reuse["local_structural_input_tokens"] == sum(
        row["local_structural_input_tokens"] for row in measured)
    assert reuse["repeated_local_structural_input_tokens"] == sum(
        row["repeated_local_structural_input_tokens"] for row in measured)
    assert reuse["local_structural_input_reuse_fraction"] == (
        reuse["repeated_local_structural_input_tokens"]
        / reuse["local_structural_input_tokens"])
    assert reuse["local_structural_input_reuse_fraction"] != (
        sum(row["repeated_input_fraction"] for row in measured) / len(measured))
    assert metrics["run_usage"]["cache_fraction"]["value"] is None

    monkeypatch.setattr(trace_run, "_export_env", lambda: (_ for _ in ()).throw(
        AssertionError("trace inspection read export settings")))
    args = ["--state-dir", str(tmp_path), "--run-id", "run-a"]
    assert trace_run.main(args) == 0
    output = json.loads(capsys.readouterr().out)
    assert output["local_prefix_reuse"] == reuse
    comparison_text = local_reuse_text({"local_prefix_reuse": reuse})
    assert f"Local input reuse: {100 * reuse['local_structural_input_reuse_fraction']:.1f}%" in comparison_text
    assert "5/6 request records" in comparison_text
    assert "not a provider cache hit" in comparison_text
    assert trace_run.main([*args, "--format", "markdown"]) == 0
    markdown = capsys.readouterr().out
    assert f"{100 * reuse['local_structural_input_reuse_fraction']:.1f}%" in markdown
    assert "5/6 request records measured, 1 unavailable" in markdown
    assert "| 1 | — | baseline no predecessor |" in markdown
    assert "| 3 | — | baseline no predecessor |" in markdown
    assert "| 4 | 2 | measured |" in markdown
    assert "| 5 | 3 | measured |" in markdown
    assert "| 6 | — | unavailable: invalid request |" in markdown
    assert "Unchanged definitions within changed tool lists" in markdown
    assert "provider cache hits or billing savings" in markdown
    assert "Synthetic follow-up" not in markdown
    assert (tmp_path / "state.sqlite3").read_bytes() == before
