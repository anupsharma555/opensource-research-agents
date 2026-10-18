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
    assert result["run_usage"]["cache_read"]["legacy_uncertain_calls"] == 3
    assert result["local_prefix_reuse"]["status"] == "request_unavailable"
    assert result["local_prefix_reuse"]["estimated_repeated_message_tokens"] is None
    assert result["retrieval"]["http_attempt_count"] is None
    assert result["local_export"]["total"] == 0
    assert (state / "state.sqlite3").read_bytes() == before
    assert "private prompt" not in json.dumps(result)
    assert "private source body" not in json.dumps(result)


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
    assert "0.200s" in report and "exact saved message" in report
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
    assert "## Model turns" in output
    assert "unavailable coverage" in output
    assert "3 legacy captures without field-level diagnostics" in output
    assert "complete HTTP-attempt counts are unavailable" in output
    assert "Local exact-prefix reuse proxy" in output
    assert "not Groq's rendered prefix, cached tokens" in output
    assert "| 1 | captured | 100 | 10 | 2 | legacy uncertain | legacy uncertain |" in output
    assert "do-not-load" not in output
    assert "from-process" not in output


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
