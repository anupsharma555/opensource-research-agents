"""Opt-in runtime observation stays local-first, scoped and restart-safe."""

from __future__ import annotations

import threading

from ora.observability.evaluation import record_evaluation_observation
from ora.observability.export import EXPORT_KIND, ExportConfig
from ora.observability.runtime import (
    BINDING_KIND,
    DIAGNOSTIC_KIND,
    SNAPSHOT_KIND,
    RuntimeObserver,
)
from ora.state import StateError, StateStore

ENDPOINT = "https://api.smith.langchain.com/otel/v1/traces"


def _config(project: str = "synthetic-project") -> ExportConfig:
    return ExportConfig(enabled=True, endpoint=ENDPOINT,
                        api_key="synthetic-key", project=project).validated()


def _put(store: StateStore, kind: str, id: str, body: dict) -> dict:
    return store.transact(
        f"runtime-fixture:{kind}:{id}", {}, lambda tx: tx.put(kind, id, body),
        mission_id=body.get("mission_id", ""),
    )


def _ref(record: dict) -> dict:
    return {key: record[key] for key in ("kind", "id", "version")}


def _run_and_actor(store: StateStore, run_id: str, mission_id: str) -> None:
    _put(store, "mission_run", run_id, {"mission_id": mission_id, "run_id": run_id})
    _put(store, "agent_instance", f"actor:{run_id}", {
        "mission_id": mission_id, "run_id": run_id,
    })


def _trace(store: StateStore, id: str, run_id: str, mission_id: str) -> None:
    _put(store, "coordination_trace", id, {
        "mission_id": mission_id, "run_id": run_id,
        "instance_id": f"actor:{run_id}", "task_id": "task-a",
        "attempt_id": "attempt-a", "category": "tool_result",
        "status": "recorded", "summary": "PRIVATE_SOURCE_PAYLOAD",
    })


def _figure_receipt(store: StateStore, id: str, run_id: str, mission_id: str) -> None:
    intent = _put(store, "figure_request_intent", f"figure-intent:{id}", {
        "mission_id": mission_id, "run_id": run_id, "task_id": "task-a",
        "actor_id": f"actor:{run_id}",
        "request_sha256": "b" * 64,
    })
    _put(store, "figure_response_receipt", f"figure-response:{intent['id']}", {
        "mission_id": mission_id, "run_id": run_id, "task_id": "task-a",
        "actor_id": f"actor:{run_id}", "intent_ref": _ref(intent),
        "request_sha256": "b" * 64, "status": "generated_unverified",
        "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
    })


def _retrieval_attempt(store: StateStore, id: str, run_id: str,
                       mission_id: str) -> None:
    body = {
        "schema": "ora.retrieval.attempt.v1",
        "request": {"mission_id": mission_id,
                    "agent_instance_id": f"actor:{run_id}"},
        "outcome": "success", "delivery_refs": [],
    }
    store.transact(
        f"runtime-retrieval:{id}", {},
        lambda tx: tx.put("retrieval_attempt", id, body), mission_id=mission_id,
    )


def test_disabled_observer_has_no_worker_or_binding(tmp_path):
    store = StateStore(tmp_path)
    observer = RuntimeObserver(store, ExportConfig(), start_worker=True)
    _run_and_actor(store, "run-a", "mission-a")
    assert observer.register("run-a", newly_created=True) is False
    assert observer.wake("run-a") is False
    assert observer.process_once() == {
        "projected": 0, "sent": 0, "uncertain": 0, "snapshots": 0,
    }
    assert observer.status()["worker_alive"] is False
    assert store.records(BINDING_KIND) == []
    assert observer.close()


def test_local_reuse_snapshot_persists_across_restart_without_prompt_content(tmp_path):
    from tests.observability.test_metrics import _seed_reuse_run

    store = StateStore(tmp_path)
    observer = RuntimeObserver(store, _config(), sender=lambda *_: True, start_worker=False)
    _seed_reuse_run(store, response_status="partial")
    assert observer.register("run-a", newly_created=True)
    observer.process_once()
    snapshot = store.get(SNAPSHOT_KIND, "observability-context:run-a")
    reuse = snapshot["body"]["metrics"]["local_prefix_reuse"]
    assert reuse["structural_measured_calls"] == 3
    assert reuse["structural_measured_followup_pairs"] == 1
    assert 0 < reuse["local_structural_input_reuse_fraction"] < 1
    assert len(reuse["structural_input_calls"]) == 3
    assert "Shared synthetic guidance" not in str(snapshot)
    assert observer.close()
    reopened = RuntimeObserver(StateStore(tmp_path), _config(),
                               sender=lambda *_: True, start_worker=False)
    reopened.process_once()
    assert reopened.store.get(SNAPSHOT_KIND, "observability-context:run-a") == snapshot
    assert reopened.close()


def test_tool_projection_refreshes_on_results_and_later_model_receipts(tmp_path):
    from tests.observability.test_tool_activity import (
        _followup_messages,
        _result,
        _seed,
        _turn,
        _work,
    )

    store = StateStore(tmp_path)
    sent = []
    observer = RuntimeObserver(
        store, _config(), sender=lambda _config, body: sent.append(body) or True,
        start_worker=False, page_size=1,
    )
    _seed(store)
    call, response, tool = _turn(store, "first", "save_plan")
    assert observer.register("run-a", newly_created=True)
    for _ in range(12):
        observer.process_once()

    _work(store, "first", call, tool)
    _, message = _result(store, response, tool, {"status": "saved"})
    _turn(store, "second", "read_plan", messages=_followup_messages(tool, message))
    for _ in range(20):
        observer.process_once()
    tool_spans = [body for body in sent if body["attributes"].get("gen_ai.tool.name") == "save_plan"]
    assert tool_spans
    latest = tool_spans[-1]["attributes"]
    assert latest["osra.tool.execution_status"] == "executed"
    assert latest["osra.tool.delivery_status"] == "captured_later"
    assert latest["osra.tool.active_seconds"] == 0.2
    assert latest["osra.observed.wall_seconds"] == 0.25
    assert not any(key.startswith("gen_ai.usage.") for key in latest)
    before = len(sent)
    for _ in range(3):
        observer.process_once()
    assert len(sent) == before
    assert observer.status()["last_error_code"] is None
    assert observer.close()


def test_new_run_binding_exports_only_registered_run_and_survives_reopen(tmp_path):
    store = StateStore(tmp_path)
    sent = []
    observer = RuntimeObserver(store, _config(), sender=lambda _config, body: (
        sent.append(body["run_id"]) or True
    ), start_worker=False)
    _run_and_actor(store, "run-a", "mission-a")
    _trace(store, "trace-a", "run-a", "mission-a")
    _run_and_actor(store, "run-other", "mission-other")
    _trace(store, "trace-other", "run-other", "mission-other")
    assert observer.register("run-a", newly_created=True)
    assert not observer.register("run-other", newly_created=False)
    for _ in range(15):
        observer.process_once()
    assert sent and set(sent) == {"run-a"}
    assert store.records(EXPORT_KIND)
    assert any(item["body"]["name"] == "osra.run.summary"
               for item in store.records(EXPORT_KIND))
    assert all(item["body"]["run_id"] == "run-a" for item in store.records(EXPORT_KIND))
    binding = store.records(BINDING_KIND)[0]
    assert "synthetic-key" not in str(binding)
    assert ENDPOINT not in str(binding)
    assert "synthetic-project" not in str(binding)
    snapshot = store.get(SNAPSHOT_KIND, "observability-context:run-a")
    assert snapshot is not None and "PRIVATE_SOURCE_PAYLOAD" not in str(snapshot)
    assert observer.close()

    reopened = RuntimeObserver(StateStore(tmp_path), _config(), sender=lambda *_: (
        sent.append("unexpected-resend") or True
    ), start_worker=False)
    assert reopened.status()["registered_runs"] == 1
    for _ in range(5):
        reopened.process_once()
    assert "unexpected-resend" not in sent
    assert reopened.close()
    other_destination = RuntimeObserver(StateStore(tmp_path), _config("different-project"),
                                        start_worker=False)
    assert other_destination.status()["registered_runs"] == 0
    assert other_destination.close()


def test_historical_unbound_run_cannot_be_enrolled_as_new(tmp_path):
    store = StateStore(tmp_path)
    _run_and_actor(store, "run-old", "mission-old")
    observer = RuntimeObserver(store, _config(), start_worker=False)
    assert not observer.register("run-old", newly_created=True)
    assert not observer.register("run-old", newly_created=False)
    _run_and_actor(store, "run-new", "mission-new")
    assert observer.register("run-new", newly_created=True)
    assert observer.status()["registered_runs"] == 1
    assert observer.close() is False  # No worker or sender was started to drain it.


def test_reopen_never_resends_an_uncertain_span(tmp_path):
    store = StateStore(tmp_path)
    attempts = []

    def lost_ack(_config, body):
        attempts.append(body["span_id"])
        raise TimeoutError("synthetic acknowledgement lost")

    observer = RuntimeObserver(store, _config(), sender=lost_ack, start_worker=False)
    _run_and_actor(store, "run-a", "mission-a")
    _trace(store, "trace-a", "run-a", "mission-a")
    assert observer.register("run-a", newly_created=True)
    for _ in range(12):
        observer.process_once()
    assert attempts
    assert all(item["body"]["status"] == "uncertain"
               for item in store.records(EXPORT_KIND))
    assert observer.close()

    reopened = RuntimeObserver(StateStore(tmp_path), _config(),
                               sender=lambda *_: attempts.append("resent") or True,
                               start_worker=False)
    for _ in range(8):
        reopened.process_once()
    assert "resent" not in attempts
    assert reopened.close()


def test_final_receipt_just_before_close_is_projected_and_sent(tmp_path):
    store = StateStore(tmp_path)
    sent = []
    observer = RuntimeObserver(store, _config(),
                               sender=lambda _config, body: sent.append(body["name"]) or True,
                               start_worker=False)
    _run_and_actor(store, "run-a", "mission-a")
    _trace(store, "trace-a", "run-a", "mission-a")
    assert observer.register("run-a", newly_created=True)
    for _ in range(5):
        observer.process_once()
    earlier = len(sent)

    call = _put(store, "agent_call", "final-call", {
        "mission_id": "mission-a", "instance_id": "actor:run-a",
        "task_id": "task-a", "attempt_id": "final-attempt",
        "provider": "synthetic_provider", "model_id": "fixture-model",
    })
    intent = _put(store, "model_request_intent", "model-intent:final-call:1", {
        "mission_id": "mission-a", "call_ref": _ref(call),
        "request_sha256": "a" * 64,
    })
    _put(store, "model_response_receipt", f"model-response:{intent['id']}", {
        "mission_id": "mission-a", "task_id": "task-a", "attempt_id": "final-attempt",
        "call_ref": _ref(call), "intent_ref": _ref(intent),
        "request_sha256": "a" * 64, "status": "complete",
        "usage": {"prompt_tokens": 20, "completion_tokens": 3, "total_tokens": 23,
                  "prompt_tokens_details": {"cached_tokens": 5}},
    })
    assert observer.close(timeout_seconds=5)
    assert len(sent) > earlier and "osra.model.call" in sent[earlier:]
    assert all(item["body"]["status"] == "sent" for item in store.records(EXPORT_KIND))
    snapshot = store.get(SNAPSHOT_KIND, "observability-context:run-a")
    assert snapshot["body"]["metrics"]["model"]["usage"]["cache_read"]["tokens"] == 5


def test_close_creates_final_summary_before_its_bounded_drain(tmp_path):
    store = StateStore(tmp_path)
    sent = []
    observer = RuntimeObserver(store, _config(),
                               sender=lambda _config, body: sent.append(body["name"]) or True,
                               start_worker=False)
    _run_and_actor(store, "run-a", "mission-a")
    assert observer.register("run-a", newly_created=True)
    assert observer.close(timeout_seconds=5)
    assert "osra.run.summary" in sent
    assert all(item["body"]["status"] == "sent" for item in store.records(EXPORT_KIND))


def test_export_tick_skips_sent_rows_and_caps_actual_sends(tmp_path):
    store = StateStore(tmp_path)
    sent = []
    observer = RuntimeObserver(store, _config(),
                               sender=lambda _config, body: sent.append(body["name"]) or True,
                               page_size=12, start_worker=False)
    _run_and_actor(store, "run-a", "mission-a")
    _trace(store, "trace-a", "run-a", "mission-a")

    def old_exports(tx):
        for index in range(8):
            tx.put(EXPORT_KIND, f"a-already-sent-{index}", {
                "schema": "ora.observability_export.v1", "mission_id": "mission-a",
                "run_id": "run-a", "status": "sent",
            })
        return {"saved": True}

    store.transact("seed-sent-export-pages", {}, old_exports, mission_id="mission-a")
    assert observer.register("run-a", newly_created=True)
    first = observer.process_once()
    assert first["sent"] == 2 and len(sent) == 2
    assert observer._progress["run-a"].export_cursor.startswith("otel-export")
    assert observer.close()


def test_paged_scan_revisits_lexically_earlier_late_record(tmp_path):
    store = StateStore(tmp_path)
    observer = RuntimeObserver(store, _config(), sender=lambda *_: True,
                               page_size=1, max_pages_per_wake=1, start_worker=False)
    _run_and_actor(store, "run-a", "mission-a")
    _trace(store, "z-trace", "run-a", "mission-a")
    assert observer.register("run-a", newly_created=True)
    observer.process_once()
    _trace(store, "a-trace", "run-a", "mission-a")
    for _ in range(8):
        observer.process_once()
    assert any(item["body"].get("source_ref", {}).get("id") == "a-trace"
               for item in store.records(EXPORT_KIND) if item["body"].get("source_ref"))
    assert observer.close()


def test_figure_only_pages_and_revisits_earlier_receipt(tmp_path):
    store = StateStore(tmp_path)
    sent = []
    observer = RuntimeObserver(store, _config(),
                               sender=lambda _config, body: sent.append(body["name"]) or True,
                               page_size=1, max_pages_per_wake=1, start_worker=False)
    _run_and_actor(store, "run-a", "mission-a")
    _figure_receipt(store, "y", "run-a", "mission-a")
    _figure_receipt(store, "z", "run-a", "mission-a")
    assert observer.register("run-a", newly_created=True)
    observer.process_once()
    assert sum(item["body"]["name"] == "osra.figure.call"
               for item in store.records(EXPORT_KIND)) == 1
    for _ in range(8):
        observer.process_once()
    assert any(item["body"].get("source_ref", {}).get("id") ==
               "figure-response:figure-intent:z"
               for item in store.records(EXPORT_KIND) if item["body"].get("source_ref"))
    assert observer._progress["run-a"].project_active is False

    _figure_receipt(store, "a", "run-a", "mission-a")
    for _ in range(8):
        observer.process_once()
    figure_refs = {
        item["body"].get("source_ref", {}).get("id")
        for item in store.records(EXPORT_KIND)
        if (item["body"].get("source_ref") or {}).get("kind") == "figure_response_receipt"
    }
    assert figure_refs == {"figure-response:figure-intent:y",
                           "figure-response:figure-intent:z",
                           "figure-response:figure-intent:a"}
    assert sent.count("osra.figure.call") == 3
    snapshot = store.get(SNAPSHOT_KIND, "observability-context:run-a")
    assert snapshot["body"]["metrics"]["figure"]["usage"]["prompt_tokens"]["tokens"] == 150
    assert observer.close()


def test_retrieval_only_change_refreshes_snapshot_and_summary(tmp_path):
    store = StateStore(tmp_path)
    observer = RuntimeObserver(store, _config(), sender=lambda *_: True,
                               start_worker=False)
    _run_and_actor(store, "run-a", "mission-a")
    assert observer.register("run-a", newly_created=True)
    for _ in range(4):
        observer.process_once()
    initial = store.get(SNAPSHOT_KIND, "observability-context:run-a")
    initial_signal = observer._signal("mission-a", receipts_only=True)
    _retrieval_attempt(store, "attempt-other", "run-other", "mission-other")
    assert observer._signal("mission-a", receipts_only=True) == initial_signal

    _retrieval_attempt(store, "attempt-a", "run-a", "mission-a")
    assert observer._signal("mission-a", receipts_only=True) > initial_signal
    for _ in range(4):
        observer.process_once()
    updated = store.get(SNAPSHOT_KIND, "observability-context:run-a")
    assert updated["version"] > initial["version"]
    assert updated["body"]["metrics"]["retrieval"]["recorded_attempt_count"] == 1
    assert updated["body"]["metrics"]["retrieval"]["outcome_counts"] == {"success": 1}
    summaries = [item for item in store.records(EXPORT_KIND)
                 if item["body"]["name"] == "osra.run.summary"]
    assert any(item["body"]["attributes"].get(
        "osra.summary.retrieval_recorded_attempt_count") == 1
               for item in summaries)
    before_delivery = observer._signal("mission-a", receipts_only=True)
    store.transact(
        "runtime-retrieval-delivery:a", {},
        lambda tx: tx.put("retrieval_delivery", "delivery-a", {
            "envelope": {"request": {"missionId": "mission-a"}},
        }), mission_id="mission-a",
    )
    delivery_signal = observer._signal("mission-a", receipts_only=True)
    assert delivery_signal > before_delivery
    observer.process_once()
    assert observer._progress["run-a"].last_receipt_signal == delivery_signal
    assert observer.close()


def test_evaluation_stop_after_last_model_call_reaches_final_summary(tmp_path):
    store = StateStore(tmp_path)
    sent = []
    observer = RuntimeObserver(store, _config(),
                               sender=lambda _config, body: sent.append(body["name"]) or True,
                               start_worker=False)
    _run_and_actor(store, "run-a", "mission-a")
    assert observer.register("run-a", newly_created=True)
    for _ in range(4):
        observer.process_once()
    original = store.get(SNAPSHOT_KIND, "observability-context:run-a")
    ledger = {"schema": "ora.agent_validation_budget.v1",
              "model_attempts": [{"state": "observed"}],
              "retrieval_attempts": [{"state": "observed"}]}
    record_evaluation_observation(store, "run-a", ledger, "model_attempt_cap", 12.5)
    assert observer.close(timeout_seconds=5)
    updated = store.get(SNAPSHOT_KIND, "observability-context:run-a")
    assert updated["version"] > original["version"]
    assert updated["body"]["metrics"]["evaluation"]["outcome"] == "model_attempt_cap"
    summaries = [item for item in store.records(EXPORT_KIND)
                 if item["body"]["name"] == "osra.run.final_report"]
    assert len(summaries) == 1
    final = summaries[0]["body"]
    assert final["attributes"]["osra.summary.evaluation_outcome"] == "model_attempt_cap"
    assert final["attributes"]["osra.observed.wall_seconds"] == 12.5
    assert final["parent_span_id"] is None and final["status"] == "sent"
    assert sent.count("osra.run.summary") >= 1
    assert sent.count("osra.run.final_report") == 1


def test_local_snapshot_keeps_reported_zero_separate_from_unknown(tmp_path):
    store = StateStore(tmp_path)
    observer = RuntimeObserver(store, _config(), sender=lambda *_: True,
                               start_worker=False)
    _run_and_actor(store, "run-a", "mission-a")
    for number in (1, 2):
        call = _put(store, "agent_call", f"call-{number}", {
            "mission_id": "mission-a", "instance_id": "actor:run-a",
            "task_id": "task-a", "attempt_id": f"attempt-{number}",
            "provider": "synthetic_provider", "model_id": "fixture-model",
        })
        intent = _put(store, "model_request_intent", f"model-intent:call-{number}:1", {
            "mission_id": "mission-a", "call_ref": _ref(call),
            "request_sha256": "a" * 64,
        })
        _put(store, "model_dispatch_attempt", f"model-dispatch:{intent['id']}", {
            "mission_id": "mission-a", "intent_ref": _ref(intent),
            "request_sha256": "a" * 64,
        })
        if number == 1:
            _put(store, "model_response_receipt", f"model-response:{intent['id']}", {
                "mission_id": "mission-a", "task_id": "task-a",
                "attempt_id": "attempt-1", "call_ref": _ref(call),
                "intent_ref": _ref(intent), "request_sha256": "a" * 64,
                "status": "complete", "usage": {
                    "prompt_tokens": 100, "completion_tokens": 10,
                    "total_tokens": 110,
                    "prompt_tokens_details": {"cached_tokens": 0},
                },
            })
    assert observer.register("run-a", newly_created=True)
    observer.process_once()
    snapshot = store.get(SNAPSHOT_KIND, "observability-context:run-a")
    usage = snapshot["body"]["metrics"]["model"]["usage"]
    assert usage["cache_read"]["reported_calls"] == 1
    assert usage["cache_read"]["unknown_calls"] == 1
    assert usage["cache_fraction"]["value"] == 0
    assert usage["cache_fraction"]["covered_calls"] == 1
    assert "calls" not in snapshot["body"]["metrics"]
    assert observer.close()


def test_worker_error_is_safe_local_diagnostic(tmp_path, monkeypatch):
    store = StateStore(tmp_path)
    observer = RuntimeObserver(store, _config(), start_worker=False)
    _run_and_actor(store, "run-a", "mission-a")
    assert observer.register("run-a", newly_created=True)

    def fail_projection(*_args, **_kwargs):
        raise StateError("PRIVATE_FAILURE_DETAIL")

    monkeypatch.setattr("ora.observability.runtime.project_run", fail_projection)
    observer.process_once()
    diagnostics = store.records(DIAGNOSTIC_KIND)
    assert len(diagnostics) == 1
    assert diagnostics[0]["body"]["code"] == "observer_tick_failed"
    assert "PRIVATE_FAILURE_DETAIL" not in str(diagnostics)
    assert observer.close() is False  # The failed projection remains locally pending.


def test_transient_snapshot_failure_retries_without_new_receipt(tmp_path, monkeypatch):
    store = StateStore(tmp_path)
    observer = RuntimeObserver(store, _config(), sender=lambda *_: True,
                               start_worker=False)
    _run_and_actor(store, "run-a", "mission-a")
    assert observer.register("run-a", newly_created=True)
    original = observer._snapshot
    failed = [False]

    def fail_once(run_id, mission_id):
        if not failed[0]:
            failed[0] = True
            raise StateError("synthetic transient snapshot failure")
        return original(run_id, mission_id)

    monkeypatch.setattr(observer, "_snapshot", fail_once)
    observer.process_once()
    assert observer._progress["run-a"].last_receipt_signal == -1
    observer.process_once()
    assert observer._progress["run-a"].last_receipt_signal >= 0
    assert store.get(SNAPSHOT_KIND, "observability-context:run-a") is not None
    assert observer.close()


def test_bounded_close_reports_inflight_send_and_keeps_local_metrics(tmp_path):
    store = StateStore(tmp_path)
    started, release = threading.Event(), threading.Event()

    def blocked_sender(_config, _body):
        started.set()
        assert release.wait(3)
        return True

    observer = RuntimeObserver(store, _config(), sender=blocked_sender,
                               poll_interval=0.1, start_worker=True)
    _run_and_actor(store, "run-a", "mission-a")
    _trace(store, "trace-a", "run-a", "mission-a")
    assert observer.register("run-a", newly_created=True)
    try:
        assert started.wait(2)
        assert observer.close(timeout_seconds=0.1) is False
        assert observer.status()["worker_alive"] is True
        assert observer.status()["worker_pending_after_close"] is True
        assert store.get(SNAPSHOT_KIND, "observability-context:run-a") is not None
    finally:
        release.set()
        observer._thread.join(3)
    assert observer.status()["worker_alive"] is False
    assert observer.status()["worker_pending_after_close"] is False
