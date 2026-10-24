"""The trace CLI stays local by default and pages explicit operations safely."""

from __future__ import annotations

import hashlib
import json

import pytest

from ora.observability.export import _change_status
from ora.observability.export import export_pending as real_export_pending
from ora.state import StateStore
from ora.state.store import canonical
from scripts import trace_run


def _ref(record: dict) -> dict:
    return {key: record[key] for key in ("kind", "id", "version")}


def _seed(store: StateStore, *, run_id: str = "run-one", calls: int = 3) -> None:
    mission_id = "mission-one"

    def save(tx):
        tx.put("mission_run", run_id, {
            "mission_id": mission_id, "status": "completed", "question": "private question",
        })
        actor = tx.put("agent_instance", f"actor-{run_id}", {
            "mission_id": mission_id, "run_id": run_id,
        })
        for index in range(calls):
            suffix = f"{run_id}-{index}"
            call = tx.put("agent_call", f"call-{suffix}", {
                "mission_id": mission_id, "task_id": f"task-{run_id}",
                "attempt_id": f"attempt-{suffix}", "instance_id": actor["id"],
            })
            request = canonical({"messages": [{"role": "system", "content": "private prompt"}],
                                 "tools": []}).encode()
            blob = tx.store.put_blob(request)
            digest = hashlib.sha256(request).hexdigest()
            intent = tx.put("model_request_intent", f"model-intent:{call['id']}:1", {
                "mission_id": mission_id, "call_ref": _ref(call),
                "request_sha256": digest, "blob": blob,
            })
            tx.put("model_dispatch_attempt", f"model-dispatch:{intent['id']}", {
                "mission_id": mission_id, "intent_ref": _ref(intent),
                "request_sha256": digest,
            })
            tx.put("model_response_receipt", f"model-response:{intent['id']}", {
                "mission_id": mission_id, "task_id": f"task-{run_id}",
                "attempt_id": f"attempt-{suffix}", "call_ref": _ref(call),
                "intent_ref": _ref(intent), "request_sha256": digest,
                "status": "complete", "usage": {
                    "prompt_tokens": 100, "completion_tokens": 10, "total_tokens": 110,
                    "completion_tokens_details": {"reasoning_tokens": 2},
                },
            })
            tx.put("coordination_trace", f"trace-{suffix}", {
                "mission_id": mission_id, "run_id": run_id,
                "instance_id": actor["id"], "task_id": f"task-{run_id}",
                "attempt_id": f"attempt-{suffix}", "category": "model_call",
                "status": "recorded", "summary": "private source body",
            })
        return {"saved": True}

    store.transact(f"seed-{run_id}", {}, save, mission_id=mission_id)


def _args(state, *extra):
    return ["--state-dir", str(state), "--run-id", "run-one", *extra]


def test_default_is_read_only_no_env_or_export_and_missing_cache_is_unknown(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    store = StateStore(state)
    _seed(store)
    before = (state / "state.sqlite3").read_bytes()
    monkeypatch.setattr(trace_run, "_export_env", lambda: (_ for _ in ()).throw(AssertionError("env loaded")))
    monkeypatch.setattr(trace_run, "project_run", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("projected")))
    monkeypatch.setattr(trace_run, "export_pending", lambda *_a, **_k: (_ for _ in ()).throw(AssertionError("sent")))

    assert trace_run.main(_args(state)) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["action"] == "inspect"
    assert result["model"]["usage"]["input"]["known_tokens"] == 300
    assert result["model"]["usage"]["reasoning_output_subset"]["known_tokens"] == 6
    assert result["model"]["usage"]["cache_read_input_subset"]["known_tokens"] is None
    assert result["model"]["usage"]["cache_fraction_reported_subset"]["value"] is None
    assert result["run_usage"]["cache_read"]["tokens"] is None
    assert result["run_usage"]["cache_fraction"]["coverage_status"] == "unavailable"
    assert result["run_usage"]["cache_fraction_confirmed"]["status"] == "unknown"
    assert result["run_usage"]["cache_fraction_confirmed"]["value"] is None
    assert result["run_usage"]["cache_read"]["legacy_uncertain_calls"] == 3
    assert result["local_prefix_reuse"]["status"] == "request_unavailable"
    assert result["local_prefix_reuse"]["estimated_repeated_message_tokens"] is None
    assert result["retrieval"]["http_attempt_count"] is None
    assert result["local_export"]["total"] == 0
    assert (state / "state.sqlite3").read_bytes() == before
    assert "private prompt" not in json.dumps(result)
    assert "private source body" not in json.dumps(result)
    result["run_usage"].pop("cache_fraction_confirmed")
    assert ("Model cache fraction: **unknown**, based on 0/3 saved request records "
            "with both fields" in trace_run._markdown(result))


def test_local_trace_shows_confirmed_cache_floor_and_field_presence(tmp_path, capsys):
    store = StateStore(tmp_path)
    _seed(store)
    first, second = store.records("model_response_receipt")[:2]
    for record, observation, details in (
        (first, {"status": "reported_hit", "cached_tokens": 10}, {"cached_tokens": 10}),
        (second, {"status": "not_reported", "field_presence": "details_absent"}, None),
    ):
        usage = {**record["body"]["usage"]}
        if details is not None:
            usage["prompt_tokens_details"] = details
        store.transact(f"cache-shape-{record['id']}", {}, lambda tx: tx.put(
            record["kind"], record["id"], {
                **record["body"], "usage": usage, "cache_observation": observation,
            }, expected_version=record["version"],
        ))
    assert trace_run.main(_args(tmp_path, "--format", "markdown")) == 0
    output = capsys.readouterr().out
    assert ("Confirmed cached share of all reported input: **3.3%** "
            "(10 of 300 tokens; 1/3 input-reporting calls with cache detail; "
            "confirmed minimum; omitted cache detail remains unknown)" in output)
    assert "Saved cache-field presence: details absent 1" in output
    assert "Model cache fraction:" not in output
    assert "not additional or failed provider HTTP attempts" in output


def test_cache_summary_excludes_preparations_and_failed_usage_from_input_denominator(
    tmp_path, capsys,
):
    store = StateStore(tmp_path)
    _seed(store, calls=7)

    def add_unsent_and_failed(tx):
        for index in range(4):
            call = tx.put("agent_call", f"call-extra-{index}", {
                "mission_id": "mission-one", "task_id": "task-run-one",
                "attempt_id": f"attempt-extra-{index}", "instance_id": "actor-run-one",
            })
            intent = tx.put("model_request_intent", f"model-intent:{call['id']}:1", {
                "mission_id": "mission-one", "call_ref": _ref(call),
                "request_sha256": f"{index + 1:064x}",
            })
            if index == 3:
                tx.put("model_dispatch_attempt", f"model-dispatch:{intent['id']}", {
                    "mission_id": "mission-one", "intent_ref": _ref(intent),
                    "request_sha256": f"{index + 1:064x}",
                })
                tx.put("model_response_receipt", f"model-response:{intent['id']}", {
                    "mission_id": "mission-one", "task_id": "task-run-one",
                    "attempt_id": f"attempt-extra-{index}", "call_ref": _ref(call),
                    "intent_ref": _ref(intent), "request_sha256": f"{index + 1:064x}",
                    "status": "failed", "http_status": 413, "usage": {},
                    "cache_observation": {"status": "unavailable"},
                })
        return {"saved": True}

    store.transact("add-unsent-and-failed", {}, add_unsent_and_failed,
                   mission_id="mission-one")
    assert trace_run.main(_args(tmp_path, "--format", "markdown")) == 0
    output = capsys.readouterr().out
    assert "Confirmed cached share of all reported input: **unknown**; 0/7 input-reporting calls" in output
    assert ("Cache record context: 11 saved request records; "
            "3 preparation-only and 1 other record without reported input" in output)
    assert "Cache-field states across 11 saved request records" in output
    assert "0/11 input-reporting calls" not in output
    assert "Cached-input share on reporting calls" not in output
    assert "Model cache fraction:" not in output
    assert "7/11 saved request records with both fields" in output


def test_local_trace_shows_admission_demand_separately_from_unknown_usage(tmp_path, capsys):
    store = StateStore(tmp_path)
    _seed(store, calls=1)
    receipt = store.records("model_response_receipt")[0]
    store.transact("add-failed-measurement", {}, lambda tx: tx.put(
        receipt["kind"], receipt["id"], {**receipt["body"], "usage": {}, "status": "failed",
            "token_measurement": {
                "schema": "ora.model.token_measurement.v1",
                "local_input_estimate": 7400, "provider_admission_tokens": 8200,
                "provider_admission_limit_tokens": 8000, "reported_input_tokens": None,
            }}, expected_version=receipt["version"],
    ))
    store.transact("unmatched-call", {}, lambda tx: tx.put("agent_call", "unmatched", {
        "mission_id": "mission-one", "task_id": "task-run-one",
        "instance_id": "actor-run-one",
    }))
    before = (tmp_path / "state.sqlite3").read_bytes()
    assert trace_run.main(_args(tmp_path, "--format", "markdown")) == 0
    output = capsys.readouterr().out
    assert "Token measurement fidelity" in output
    fidelity = output.split("## Token measurement fidelity")[1].split("## Context and integrity")[0]
    assert "| 1 | 7400 | 8200 | unknown |" in fidelity
    assert "| 2 |" not in fidelity
    assert "manifest_without_intent" in output.split("## Preparation and unmatched call records")[1]
    assert "Admission demand is not billed usage" in output
    assert (tmp_path / "state.sqlite3").read_bytes() == before


def test_tool_report_and_capped_projection_resume_without_network(tmp_path, monkeypatch, capsys):
    from tests.observability.test_tool_activity import (
        _followup_messages,
        _result,
        _turn,
        _work,
    )
    from tests.observability.test_tool_activity import (
        _seed as seed_tools,
    )

    state = tmp_path / "state"
    store = StateStore(state)
    seed_tools(store)
    call, response, tool = _turn(store, "first", "save_plan")
    _work(store, "first", call, tool)
    _, message = _result(store, response, tool, {"status": "saved", "private": "LOCAL_ONLY"})
    _turn(store, "second", "read_plan", messages=_followup_messages(tool, message))
    monkeypatch.setattr(trace_run, "_export_env", lambda: (_ for _ in ()).throw(AssertionError("env loaded")))
    args = ["--state-dir", str(state), "--run-id", "run-a"]
    assert trace_run.main([*args, "--format", "markdown"]) == 0
    report = capsys.readouterr().out
    assert "Tool activity by model loop" in report
    assert "save_plan | executed / completed" in report
    assert "0.200s" in report and "captured with exact result" in report
    assert "Loop 2: captured with exact result | — |" in report
    assert "LOCAL_ONLY" not in report
    options = ["--project", "--max-spans", "4", "--page-size", "1", "--max-pages", "1"]
    for _ in range(10):
        assert trace_run.main([*args, *options]) == 0
        result = json.loads(capsys.readouterr().out)
        operation = result["operation"]
        assert operation["created"] <= 4
        if operation["complete"]:
            break
        options = ["--project", "--max-spans", "4", "--page-size", "1", "--max-pages", "1"]
        for stream in ("receipt", "figure-receipt", "trace", "tool"):
            cursor = operation[f"next_after_{stream.replace('-', '_')}_id"]
            options.extend([f"--after-{stream}-id", cursor])
    assert operation["complete"]
    assert result["local_export"]["sent"] == 0
    spans = store.records("observability_export")
    tools = [row for row in spans if row["body"]["attributes"].get("gen_ai.tool.name")]
    assert len(result["tool_activity"]["steps"]) == 2
    assert len(tools) == 1  # The second selection has no settled execution/delivery yet.


def test_projection_pages_with_cap_and_restart_deduplicates(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    _seed(StateStore(state))
    monkeypatch.setattr(trace_run, "_export_env", lambda: (_ for _ in ()).throw(AssertionError("env loaded")))

    assert trace_run.main(_args(state, "--project", "--max-spans", "4", "--page-size", "1")) == 0
    first = json.loads(capsys.readouterr().out)
    assert first["operation"]["created"] <= 4
    assert first["operation"]["complete"] is False
    assert first["local_export"]["total"] <= 4

    assert trace_run.main(_args(state, "--project", "--page-size", "1")) == 0
    complete = json.loads(capsys.readouterr().out)
    assert complete["operation"]["complete"] is True
    assert complete["local_export"]["total"] == 7  # six sources and one run anchor
    assert complete["local_export"]["pending"] == 7

    assert trace_run.main(_args(state, "--project", "--page-size", "1")) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["operation"]["created"] == 0
    assert repeated["local_export"]["total"] == 7


def test_projection_page_cap_exposes_continuation_cursors(tmp_path, capsys):
    state = tmp_path / "state"
    _seed(StateStore(state))
    options = ["--project", "--page-size", "1", "--max-pages", "1"]
    seen = []
    for _ in range(5):
        assert trace_run.main(_args(state, *options)) == 0
        result = json.loads(capsys.readouterr().out)
        seen.append(result["local_export"]["total"])
        if result["operation"]["complete"]:
            break
        options = ["--project", "--page-size", "1", "--max-pages", "1",
                   "--after-receipt-id", result["operation"]["next_after_receipt_id"],
                   "--after-trace-id", result["operation"]["next_after_trace_id"]]
    assert seen == sorted(seen)
    assert result["operation"]["complete"] is True
    assert result["local_export"]["total"] == 7


def test_explicit_export_uses_only_scoped_config_and_does_not_resend(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    store = StateStore(state)
    _seed(store)
    _seed(store, run_id="run-two", calls=1)
    assert trace_run.main(_args(state, "--project")) == 0
    capsys.readouterr()
    assert trace_run.main(["--state-dir", str(state), "--run-id", "run-two", "--project"]) == 0
    capsys.readouterr()
    monkeypatch.setattr(trace_run, "_export_env", lambda: {
        "OSRA_TRACE_EXPORT_ENABLED": "true",
        "OSRA_TRACE_OTLP_TRACES_ENDPOINT": "https://api.smith.langchain.com/otel/v1/traces",
        "OSRA_TRACE_LANGSMITH_PROJECT": "synthetic-project",
        "LANGSMITH_API_KEY": "synthetic-key",
    })
    seen = []

    def safe_export(store, config, **kwargs):
        assert kwargs["mission_id"] == "mission-one"
        assert kwargs["run_id"] == "run-one"
        return real_export_pending(
            store, config, sender=lambda _config, body: seen.append(body["span_id"]) or True,
            **kwargs,
        )

    monkeypatch.setattr(trace_run, "export_pending", safe_export)
    assert trace_run.main(_args(state, "--export", "--page-size", "2")) == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported["operation"]["sent"] == 7
    assert exported["local_export"]["sent"] == 7
    assert len(seen) == 7
    assert trace_run._export_counts(StateStore(state, read_only=True), "mission-one", "run-two")["pending"] == 3
    assert trace_run.main(_args(state, "--export", "--page-size", "2")) == 0
    repeated = json.loads(capsys.readouterr().out)
    assert repeated["operation"]["sent"] == 0
    assert len(seen) == 7


def test_scoped_dotenv_only_and_markdown_has_unknown_cache(tmp_path, monkeypatch, capsys):
    (tmp_path / ".env").write_text(
        "UNRELATED_SECRET=do-not-load\n"
        "OSRA_TRACE_EXPORT_ENABLED=true\n"
        "OSRA_TRACE_OTLP_TRACES_ENDPOINT='https://api.smith.langchain.com/otel/v1/traces'\n"
        "OSRA_TRACE_LANGSMITH_PROJECT=synthetic-project\n"
        "LANGSMITH_API_KEY=from-file\n"
    )
    monkeypatch.setattr(trace_run, "ROOT", tmp_path)
    monkeypatch.setenv("LANGSMITH_API_KEY", "from-process")
    loaded = trace_run._export_env()
    assert loaded == {
        "OSRA_TRACE_EXPORT_ENABLED": "true",
        "OSRA_TRACE_OTLP_TRACES_ENDPOINT": "https://api.smith.langchain.com/otel/v1/traces",
        "OSRA_TRACE_LANGSMITH_PROJECT": "synthetic-project",
        "LANGSMITH_API_KEY": "from-process",
    }
    state = tmp_path / "state"
    _seed(StateStore(state))
    assert trace_run.main(_args(state, "--format", "markdown")) == 0
    output = capsys.readouterr().out
    assert "Cached input subset | unknown" in output
    assert "## Model requests with dispatch or response evidence" in output
    assert "Confirmed cached share of all reported input: **unknown**; 0/3 input-reporting calls" in output
    assert "Cache-field states across 3 saved request records" in output
    assert "3 legacy captures without field-level diagnostics" in output
    assert "complete HTTP-attempt counts are unavailable" in output
    assert "Local exact-prefix reuse proxy" in output
    assert "not Groq's rendered prefix, cached tokens" in output
    assert "| 1 | captured | 100 | 10 | 2 | legacy uncertain | legacy uncertain |" in output
    assert "do-not-load" not in output
    assert "from-process" not in output


def test_local_reuse_report_measures_cutoff_input_and_preserves_store(tmp_path, monkeypatch, capsys):
    from tests.observability.test_metrics import _seed_reuse_run

    store = StateStore(tmp_path)
    _seed_reuse_run(store, response_status="partial")
    before = store.records("observability_context_snapshot")
    monkeypatch.setattr(trace_run, "_export_env", lambda: pytest.fail("read-only report read credentials"))
    monkeypatch.setattr(trace_run, "_project", lambda *_args, **_kwargs: pytest.fail("projection"))
    monkeypatch.setattr(trace_run, "_export", lambda *_args, **_kwargs: pytest.fail("export"))
    args = ["--state-dir", str(tmp_path), "--run-id", "run-a"]
    assert trace_run.main(args) == 0
    payload = json.loads(capsys.readouterr().out)
    reuse = payload["local_prefix_reuse"]
    assert reuse["structural_measured_calls"] == 3
    assert reuse["structural_measured_followup_pairs"] == 1
    assert trace_run.main([*args, "--format", "markdown"]) == 0
    output = capsys.readouterr().out
    assert f"{100 * reuse['local_structural_input_reuse_fraction']:.1f}%" in output
    assert "## Local input reuse across steps" in output
    assert "| 2 | 1 | measured |" in output
    assert "baseline no predecessor" in output
    assert "3/3 request records measured" in output
    assert "Shared synthetic guidance" not in output
    assert StateStore(tmp_path, read_only=True).records("observability_context_snapshot") == before


def test_markdown_separates_unmatched_manifests_from_captured_requests(tmp_path, capsys):
    state = tmp_path / "state"
    store = StateStore(state)
    _seed(store)

    def save_unmatched(tx):
        for index in range(2):
            tx.put("agent_call", f"unmatched-{index}", {
                "mission_id": "mission-one", "task_id": "task-run-one",
                "instance_id": "actor-run-one",
            })
        return {"saved": True}

    store.transact("unmatched-manifests", {}, save_unmatched, mission_id="mission-one")
    before = (state / "state.sqlite3").read_bytes()
    assert trace_run.main(_args(state)) == 0
    metrics = json.loads(capsys.readouterr().out)
    assert len(metrics["loops"]) == 5  # Preserve the existing JSON diagnostic view.
    assert metrics["run_activity"]["model_dispatch_recorded_count"] == 3
    assert metrics["run_activity"]["model_captured_response_count"] == 3
    assert metrics["model"]["usage"]["input"]["known_tokens"] == 300
    assert metrics["model"]["usage"]["output"]["known_tokens"] == 30

    assert trace_run.main(_args(state, "--format", "markdown")) == 0
    report = capsys.readouterr().out
    requests, diagnostics = report.split("## Preparation and unmatched call records")
    assert requests.count("| captured | 100 | 10 |") == 3
    assert "manifest_without_intent" not in requests
    assert diagnostics.count("| manifest_without_intent |") == 2
    assert "A call manifest without an intent is not evidence of a prepared request" in diagnostics
    assert (state / "state.sqlite3").read_bytes() == before


def test_markdown_keeps_prepared_only_and_later_recovery_distinct(tmp_path, capsys):
    from tests.observability.test_tool_activity import (
        _followup_messages,
        _result,
        _turn,
    )
    from tests.observability.test_tool_activity import (
        _seed as seed_tools,
    )

    state = tmp_path / "state"
    store = StateStore(state)
    seed_tools(store)
    _, response, tool = _turn(store, "first", "save_plan")
    _, message = _result(store, response, tool, {"status": "saved"})
    _turn(store, "second", "calculate", messages=[{"role": "user", "content": "continue"}],
          response_status="complete")
    _turn(store, "third", "calculate", messages=_followup_messages(tool, message),
          response_status="complete")
    request = canonical({"messages": [], "tools": []}).encode()
    blob = store.put_blob(request)
    digest = hashlib.sha256(request).hexdigest()

    def save_prepared(tx):
        call = tx.put("agent_call", "prepared-call", {
            "mission_id": "mission-a", "instance_id": "actor-a", "task_id": "task-a",
        })
        tx.put("model_request_intent", "prepared-intent", {
            "mission_id": "mission-a", "call_ref": _ref(call),
            "request_sha256": digest, "blob": blob,
        })
        return {"saved": True}

    store.transact("prepared-only", {}, save_prepared, mission_id="mission-a")
    assert trace_run.main(["--state-dir", str(state), "--run-id", "run-a",
                           "--format", "markdown"]) == 0
    report = capsys.readouterr().out
    requests, diagnostics = report.split("## Preparation and unmatched call records")
    assert "| prepared_only |" not in requests
    assert "| 1 | prepared_only | Request intent saved; no verified dispatch or response |" in diagnostics
    assert "Loop 2: captured without result | captured later; exact saved message |" in report


def test_missing_optional_packages_fail_before_claiming_spans(tmp_path, monkeypatch, capsys):
    state = tmp_path / "state"
    _seed(StateStore(state))
    assert trace_run.main(_args(state, "--project")) == 0
    capsys.readouterr()
    monkeypatch.setattr(trace_run, "_export_env", lambda: {
        "OSRA_TRACE_EXPORT_ENABLED": "true",
        "OSRA_TRACE_OTLP_TRACES_ENDPOINT": "https://api.smith.langchain.com/otel/v1/traces",
        "OSRA_TRACE_LANGSMITH_PROJECT": "synthetic-project",
        "LANGSMITH_API_KEY": "synthetic-key",
    })
    monkeypatch.setattr(trace_run.importlib, "import_module", lambda _name: (_ for _ in ()).throw(ImportError()))
    monkeypatch.setattr(trace_run, "export_pending", lambda *_a, **_k: pytest.fail("claimed a span"))
    with pytest.raises(SystemExit) as stopped:
        trace_run.main(_args(state, "--export"))
    assert stopped.value.code == 2
    assert "setup --observability" in capsys.readouterr().err
    assert trace_run._export_counts(StateStore(state, read_only=True), "mission-one", "run-one")["pending"] == 7


def test_uncertain_export_errors_are_grouped_without_raw_details(tmp_path, capsys):
    state = tmp_path / "state"
    store = StateStore(state)
    _seed(store)
    assert trace_run.main(_args(state, "--project")) == 0
    capsys.readouterr()
    exports = store.records("observability_export")
    _change_status(store, exports[0], "uncertain", "http_rejected",
                   error_code="http_rejected", http_status=401)
    _change_status(store, exports[1], "uncertain", "acknowledgement_unknown")
    _change_status(store, exports[2], "uncertain", "private exception text")

    assert trace_run.main(_args(state)) == 0
    result = json.loads(capsys.readouterr().out)
    errors = result["local_export_errors"]
    assert errors == {
        "uncertain_total": 3,
        "by_error_code": {"http_rejected": 1},
        "by_http_status": {"401": 1},
        "other_http_status_count": 0,
        "legacy_without_error_code": {"acknowledgement_unknown": 1, "unclassified": 1},
    }
    assert "private exception text" not in json.dumps(result)
    assert trace_run.main(_args(state, "--format", "markdown")) == 0
    assert "http_rejected" in capsys.readouterr().out
