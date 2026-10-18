"""Metadata-only export projection, explicit dispatch, and restart boundaries."""

from __future__ import annotations

from datetime import datetime, timezone

import pytest

from ora.observability.evaluation import record_evaluation_observation
from ora.observability.export import (
    EXPORT_KIND,
    ExportConfig,
    _change_status,
    export_pending,
    project_diagnostic_summary,
    project_run,
    project_run_summary,
    project_tool_activity,
    render_summary_output,
)
from ora.state.store import StateConflict, StateError, StateStore

ENDPOINT = "https://api.smith.langchain.com/otel/v1/traces"


def _seed(store: StateStore, *, cache_tokens: int | None = 60,
          failed: bool = False, usage_reported: bool = True,
          with_budget: bool = True) -> None:
    def write(tx):
        tx.put("mission_run", "run-1", {"mission_id": "mission-1", "run_id": "run-1"})
        tx.put("agent_instance", "actor-1", {"mission_id": "mission-1", "run_id": "run-1"})
        call = tx.put("agent_call", "call-1", {
            "mission_id": "mission-1", "task_id": "task-1", "attempt_id": "attempt-1",
            "instance_id": "actor-1", "provider": "synthetic_provider",
            "model_id": "fixture-model",
        })
        intent = tx.put("model_request_intent", "intent-1", {"mission_id": "mission-1"})
        intent_ref = {key: intent[key] for key in ("kind", "id", "version")}
        if with_budget:
            tx.put("mission_call_budget", "call-1", {
                "mission_id": "mission-1", "request_stage_token_estimate": {
                    "intent_ref": intent_ref, "request_sha256": "a" * 64,
                    "final_input_tokens": 100,
                    "stages": [
                        {"kind": "pinned_instructions", "marginal_tokens": 40},
                        {"kind": "source_packet", "marginal_tokens": 25},
                        {"kind": "source_packet", "marginal_tokens": 20},
                        {"kind": "working_context", "marginal_tokens": 15},
                    ],
                },
            })
        usage = {"prompt_tokens": 110, "completion_tokens": 12, "total_tokens": 122}
        if cache_tokens is not None:
            usage["prompt_tokens_details"] = {"cached_tokens": cache_tokens}
        tx.put("model_response_receipt", "response-1", {
            "mission_id": "mission-1", "task_id": "task-1", "attempt_id": "attempt-1",
            "call_ref": {key: call[key] for key in ("kind", "id", "version")},
            "intent_ref": intent_ref, "request_sha256": "a" * 64,
            "status": "failed" if failed else "completed", "returned_model": "test-model",
            "usage": usage if usage_reported else None,
            "timings": {"wall_seconds": 2.25},
            "http_status": 413 if failed else 200,
            "private_prompt": "This must never be projected",
        })
        tx.put("coordination_trace", "trace-1", {
            "mission_id": "mission-1", "run_id": "run-1", "task_id": "task-1",
            "attempt_id": "attempt-1", "instance_id": "actor-1",
            "category": "model_call", "status": "recorded",
            "summary": "Private scientific content must remain local",
        })
        return {"saved": True}

    store.transact("seed-export-test", {}, write, mission_id="mission-1")


def _config() -> ExportConfig:
    return ExportConfig(enabled=True, endpoint=ENDPOINT,
                        api_key="private-test-key", project="private-project").validated()


def test_projection_is_bounded_metadata_and_never_sends(tmp_path):
    store = StateStore(tmp_path)
    _seed(store)
    projected = project_run(store, "mission-1", run_id="run-1")
    assert projected == {"created": 3, "scanned": 2,
                         "next_after_receipt_id": None, "next_after_trace_id": None,
                         "next_after_figure_receipt_id": None}
    assert project_run(store, "mission-1", run_id="run-1")["created"] == 0
    records = store.records(EXPORT_KIND)
    model = next(item for item in records if item["body"]["name"] == "osra.model.call")
    assert model["body"]["attributes"]["gen_ai.usage.input_tokens"] == 110
    assert model["body"]["attributes"]["gen_ai.usage.total_tokens"] == 122
    assert model["body"]["attributes"]["gen_ai.usage.cache_read.input_tokens"] == 60
    assert model["body"]["attributes"]["gen_ai.system"] == "synthetic_provider"
    assert model["body"]["attributes"]["gen_ai.request.model"] == "fixture-model"
    assert model["body"]["attributes"]["gen_ai.response.model"] == "test-model"
    assert model["body"]["attributes"]["osra.usage.cache_read.status"] == "reported"
    assert model["body"]["attributes"]["osra.usage.reasoning.status"] == "not_reported"
    assert model["body"]["attributes"]["osra.estimated.stage.source_packet_tokens"] == 45
    assert all(item["body"]["status"] == "pending" for item in records)
    assert all(item["body"]["trace_id"] == model["body"]["trace_id"] for item in records)
    assert all(item["body"]["parent_span_id"] is not None
               for item in records if not item["body"]["attributes"].get("osra.projection.anchor"))
    saved = str(records)
    for private in ("This must never", "Private scientific", "private-test-key", ENDPOINT,
                    "private-project"):
        assert private not in saved
    assert export_pending(store, ExportConfig(), sender=lambda *_: pytest.fail("sent"))["status"] == "disabled"


def test_missing_cache_is_unknown_not_zero_and_error_remains_observed(tmp_path):
    store = StateStore(tmp_path)
    _seed(store, cache_tokens=None, failed=True)
    project_run(store, "mission-1")
    model = next(item for item in store.records(EXPORT_KIND)
                 if item["body"]["name"] == "osra.model.call")
    attributes = model["body"]["attributes"]
    assert "gen_ai.usage.cache_read.input_tokens" not in attributes
    assert attributes["osra.usage.cache_read.status"] == "legacy_uncertain"
    assert attributes["osra.outcome.status"] == "failed"
    assert attributes["osra.http_status"] == 413
    assert attributes["osra.observed.wall_seconds"] == 2.25

    reported_zero = StateStore(tmp_path / "reported-zero")
    _seed(reported_zero, cache_tokens=0)
    project_run(reported_zero, "mission-1")
    zero_model = next(item for item in reported_zero.records(EXPORT_KIND)
                      if item["body"]["name"] == "osra.model.call")
    assert zero_model["body"]["attributes"]["gen_ai.usage.cache_read.input_tokens"] == 0
    assert zero_model["body"]["attributes"]["osra.usage.cache_read.status"] == "reported"


def test_missing_budget_and_uncaptured_usage_do_not_fabricate_metrics(tmp_path):
    store = StateStore(tmp_path)
    _seed(store, failed=True, usage_reported=False, with_budget=False)
    assert project_run(store, "mission-1", run_id="run-1")["created"] == 3
    model = next(item for item in store.records(EXPORT_KIND)
                 if item["body"]["name"] == "osra.model.call")
    attributes = model["body"]["attributes"]
    assert attributes["osra.usage.input_tokens.status"] == "unknown"
    assert attributes["osra.usage.cache_read.status"] == "legacy_uncertain"
    assert "gen_ai.usage.input_tokens" not in attributes
    assert "osra.estimated.input_tokens" not in attributes


@pytest.mark.parametrize("observation,amount,status", [
    ({"status": "reported_hit", "cached_tokens": 60}, 60, "reported"),
    ({"status": "reported_zero", "cached_tokens": 0}, 0, "reported"),
    ({"status": "not_reported"}, None, "not_reported"),
    ({"status": "malformed"}, None, "invalid"),
    ({"status": "unavailable"}, None, "unknown"),
])
def test_cache_boundary_status_survives_safe_export(tmp_path, observation, amount, status):
    store = StateStore(tmp_path)
    _seed(store, cache_tokens=amount)
    receipt = store.get("model_response_receipt", "response-1")
    store.transact("cache-boundary", {}, lambda tx: tx.put(
        receipt["kind"], receipt["id"], {**receipt["body"], "cache_observation": observation},
        expected_version=receipt["version"]), mission_id="mission-1")
    project_run(store, "mission-1", run_id="run-1")
    sent = []
    result = export_pending(store, _config(), mission_id="mission-1", run_id="run-1",
                            sender=lambda _config, body: sent.append(body) or True)
    assert result["uncertain"] == 0
    attributes = next(item["attributes"] for item in sent if item["name"] == "osra.model.call")
    assert attributes["osra.cache.observation"] == observation["status"]
    assert attributes["osra.usage.cache_read.status"] == status
    assert attributes.get("gen_ai.usage.cache_read.input_tokens") == amount
    assert "private_prompt" not in str(sent)


def test_projection_and_send_are_scoped_to_one_exact_run(tmp_path):
    store = StateStore(tmp_path)
    _seed(store)

    def second_run(tx):
        tx.put("mission_run", "run-2", {"mission_id": "mission-1", "run_id": "run-2"})
        tx.put("agent_instance", "actor-2", {"mission_id": "mission-1", "run_id": "run-2"})
        call = tx.put("agent_call", "call-2", {
            "mission_id": "mission-1", "task_id": "task-2", "attempt_id": "attempt-2",
            "instance_id": "actor-2", "provider": "another_provider", "model_id": "model-two",
        })
        tx.put("model_response_receipt", "response-2", {
            "mission_id": "mission-1", "task_id": "task-2", "attempt_id": "attempt-2",
            "call_ref": {key: call[key] for key in ("kind", "id", "version")},
            "request_sha256": "b" * 64, "status": "completed",
            "usage": {"prompt_tokens": 9, "completion_tokens": 2, "total_tokens": 11},
        })
        tx.put("coordination_trace", "trace-2", {
            "mission_id": "mission-1", "run_id": "run-2", "instance_id": "actor-2",
            "task_id": "task-2", "attempt_id": "attempt-2",
            "category": "model_call", "status": "recorded",
        })
        tx.put("coordination_trace", "forged-cross-run-trace", {
            "mission_id": "mission-1", "run_id": "run-1", "instance_id": "actor-2",
            "task_id": "task-2", "attempt_id": "attempt-2",
            "category": "model_call", "status": "recorded",
        })
        return {"saved": True}

    store.transact("seed-second-export-run", {}, second_run, mission_id="mission-1")
    assert project_run(store, "mission-1", run_id="run-1")["created"] == 3
    assert project_run(store, "mission-1", run_id="run-2")["created"] == 3
    assert all(item["body"]["source_ref"] != {
        "kind": "coordination_trace", "id": "forged-cross-run-trace", "version": 1,
    } for item in store.records(EXPORT_KIND))
    sent = []
    result = export_pending(store, _config(), mission_id="mission-1", run_id="run-1",
                            sender=lambda _config, body: sent.append(body["run_id"]) or True)
    assert result["sent"] == 3 and sent == ["run-1"] * 3
    assert all(item["body"]["status"] == "pending" for item in store.records(EXPORT_KIND)
               if item["body"]["run_id"] == "run-2")
    with pytest.raises(StateError, match="does not belong"):
        project_run(store, "mission-other", run_id="run-1")
    with pytest.raises(StateError, match="does not belong"):
        export_pending(store, _config(), mission_id="mission-other", run_id="run-1",
                       sender=lambda *_: pytest.fail("cross-run send"))


def test_explicit_export_and_restart_do_not_resend_sent_or_uncertain(tmp_path):
    store = StateStore(tmp_path)
    _seed(store)
    project_run(store, "mission-1")
    seen = []

    def acknowledged(config, body):
        seen.append((config.project, body["span_id"]))
        return True

    result = export_pending(store, _config(), sender=acknowledged)
    assert result["sent"] == 3
    assert len(seen) == 3
    assert export_pending(StateStore(tmp_path), _config(), sender=acknowledged)["sent"] == 0
    assert len(seen) == 3

    second = StateStore(tmp_path / "second")
    _seed(second)
    project_run(second, "mission-1")

    def lost_ack(_config, _body):
        raise TimeoutError("simulated lost acknowledgement")

    result = export_pending(second, _config(), sender=lost_ack)
    assert result["uncertain"] == 3
    assert all(row["body"]["status"] == "uncertain" for row in second.records(EXPORT_KIND))
    assert export_pending(StateStore(tmp_path / "second"), _config(),
                          sender=lambda *_: pytest.fail("resent uncertain"))["sent"] == 0


def test_interrupted_send_reconciles_to_uncertain_without_network(tmp_path):
    clock = [datetime(2026, 10, 17, tzinfo=timezone.utc).timestamp()]
    store = StateStore(tmp_path, clock=lambda: clock[0])
    _seed(store)
    project_run(store, "mission-1")
    pending = next(item for item in store.records(EXPORT_KIND)
                   if item["body"]["name"] == "osra.model.call")
    _change_status(store, pending, "sending", None)
    clock[0] += 120
    result = export_pending(store, _config(), mission_id="mission-1",
                            sender=lambda *_: True)
    assert result["uncertain"] == 1
    assert store.get(EXPORT_KIND, pending["id"])["body"]["status"] == "uncertain"


def test_stale_competing_sender_cannot_replay_a_successful_claim(tmp_path):
    store = StateStore(tmp_path)
    _seed(store)
    project_run(store, "mission-1")
    pending = next(item for item in store.records(EXPORT_KIND)
                   if item["body"]["name"] == "osra.model.call")
    claimed = _change_status(store, pending, "sending", None)
    assert claimed["version"] == pending["version"] + 1
    with pytest.raises(StateConflict):
        _change_status(store, pending, "sending", None)


@pytest.mark.parametrize("status,error_code", [
    (200, None), (302, "redirect_rejected"), (429, "http_rejected"),
])
def test_real_otel_exporter_uses_one_exact_nonredirecting_request(
    tmp_path, monkeypatch, status, error_code,
):
    requests = pytest.importorskip("requests")
    pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    store = StateStore(tmp_path)
    _seed(store)
    project_run(store, "mission-1", run_id="run-1")
    projected = store.records(EXPORT_KIND, limit=1)[0]
    seen = []
    monkeypatch.setenv("OTEL_EXPORTER_OTLP_HEADERS", "secret-header=SENTINEL_PRIVATE")
    monkeypatch.setenv("OTEL_RESOURCE_ATTRIBUTES", "private.note=SENTINEL_PRIVATE")

    def fake_request(self, method, url, **kwargs):
        seen.append({"method": method, "url": url, "headers": kwargs["headers"],
                     "redirects": kwargs["allow_redirects"], "trust_env": self.trust_env,
                     "data": kwargs["data"]})
        response = requests.Response()
        response.status_code = status
        response.reason = "synthetic"
        response._content = b""
        if status == 302:
            response.headers["Location"] = "https://unreviewed.example/collect"
        return response

    monkeypatch.setattr(requests.Session, "request", fake_request)
    result = export_pending(store, _config(), mission_id="mission-1", run_id="run-1", limit=1)
    saved = store.get(EXPORT_KIND, projected["id"])["body"]
    assert result["sent"] == (1 if error_code is None else 0)
    assert result["uncertain"] == (0 if error_code is None else 1)
    assert saved["status"] == ("sent" if error_code is None else "uncertain")
    assert saved.get("error_code") == error_code
    assert saved.get("http_status") == (status if error_code else None)
    assert len(seen) == 1
    assert seen[0]["method"] == "POST" and seen[0]["url"] == ENDPOINT
    assert seen[0]["redirects"] is False and seen[0]["trust_env"] is False
    assert seen[0]["headers"] == {
        "Content-Type": "application/x-protobuf",
        "x-api-key": "private-test-key", "Langsmith-Project": "private-project",
    }
    assert b"SENTINEL_PRIVATE" not in seen[0]["data"]
    assert b"This must never" not in seen[0]["data"]


@pytest.mark.parametrize("failure,error_code", [
    ("connection", "connection_lost"), ("timeout", "timeout"),
])
def test_lost_ack_or_timeout_does_not_retry_http(tmp_path, monkeypatch, failure, error_code):
    requests = pytest.importorskip("requests")
    pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    store = StateStore(tmp_path)
    _seed(store)
    project_run(store, "mission-1", run_id="run-1")
    projected = store.records(EXPORT_KIND, limit=1)[0]
    seen = []

    def lost_ack(_self, method, url, **_kwargs):
        seen.append((method, url))
        if failure == "timeout":
            raise requests.Timeout("synthetic timeout")
        raise requests.ConnectionError("synthetic acknowledgement lost")

    monkeypatch.setattr(requests.Session, "request", lost_ack)
    result = export_pending(store, _config(), mission_id="mission-1", run_id="run-1", limit=1)
    saved = store.get(EXPORT_KIND, projected["id"])["body"]
    assert result["uncertain"] == 1
    assert saved["status"] == "uncertain" and saved["error_code"] == error_code
    assert "http_status" not in saved
    assert "synthetic acknowledgement lost" not in str(saved)
    assert "synthetic timeout" not in str(saved)
    assert seen == [("POST", ENDPOINT)]


def test_sdk_configuration_failure_is_local_and_sanitized(tmp_path, monkeypatch):
    module = pytest.importorskip("opentelemetry.exporter.otlp.proto.http.trace_exporter")
    store = StateStore(tmp_path)
    _seed(store)
    project_run(store, "mission-1", run_id="run-1")
    projected = store.records(EXPORT_KIND, limit=1)[0]

    def bad_configuration(_self, **_kwargs):
        raise ValueError("SYNTHETIC_PRIVATE_CONFIGURATION")

    monkeypatch.setattr(module.OTLPSpanExporter, "__init__", bad_configuration)
    result = export_pending(store, _config(), mission_id="mission-1", run_id="run-1", limit=1)
    saved = store.get(EXPORT_KIND, projected["id"])["body"]
    assert result["uncertain"] == 1
    assert saved["error_code"] == saved["reason"] == "configuration_error"
    assert "SYNTHETIC_PRIVATE_CONFIGURATION" not in str(saved)


def test_boolean_sender_false_retains_unknown_acknowledgement_contract(tmp_path):
    store = StateStore(tmp_path)
    _seed(store)
    project_run(store, "mission-1", run_id="run-1")
    projected = store.records(EXPORT_KIND, limit=1)[0]
    result = export_pending(store, _config(), mission_id="mission-1", run_id="run-1",
                            limit=1, sender=lambda *_: False)
    saved = store.get(EXPORT_KIND, projected["id"])["body"]
    assert result["uncertain"] == 1 and saved["reason"] == "acknowledgement_unknown"
    assert "error_code" not in saved and "http_status" not in saved


def test_unreviewed_saved_attribute_is_never_sent(tmp_path):
    store = StateStore(tmp_path)
    _seed(store)
    project_run(store, "mission-1", run_id="run-1")
    projected = next(item for item in store.records(EXPORT_KIND)
                     if item["body"]["name"] == "osra.model.call")

    def corrupt(tx):
        tx.put(EXPORT_KIND, projected["id"], {
            **projected["body"],
            "attributes": {**projected["body"]["attributes"],
                           "osra.prompt": "PRIVATE_SOURCE_TEXT"},
        }, expected_version=projected["version"])
        return {"saved": True}

    store.transact("synthetic-invalid-export", {}, corrupt, mission_id="mission-1")
    sent = []
    result = export_pending(store, _config(), mission_id="mission-1", run_id="run-1",
                            sender=lambda _config, body: sent.append(body["name"]) or True)
    assert result["uncertain"] == 1
    assert result["sent"] == 2 and "osra.model.call" not in sent
    assert store.get(EXPORT_KIND, projected["id"])["body"]["reason"] == "invalid_projection"


def test_forged_readable_anchor_cannot_export_other_input_text(tmp_path):
    store = StateStore(tmp_path)
    _seed(store)
    project_run(store, "mission-1", run_id="run-1")
    anchor = next(item for item in store.records(EXPORT_KIND)
                  if item["body"]["attributes"].get("osra.projection.anchor"))
    forged_label = "A different private question"
    forged = {**anchor["body"], "name": anchor["body"]["name"].replace(
        "Research task", forged_label,
    ), "attributes": {**anchor["body"]["attributes"], "osra.run.label": forged_label}}
    store.transact("forge-anchor-label", {}, lambda tx: {
        "ref": tx.put(EXPORT_KIND, anchor["id"], forged,
                      expected_version=anchor["version"])["id"]
    }, mission_id="mission-1")
    sent = []
    result = export_pending(store, _config(), mission_id="mission-1", run_id="run-1",
                            sender=lambda _config, body: sent.append(body) or True)
    assert result["uncertain"] == 1
    assert all("different private" not in str(body) for body in sent)
    assert store.get(EXPORT_KIND, anchor["id"])["body"]["reason"] == "invalid_projection"


@pytest.mark.parametrize(("month", "expected"), [
    (1, "Jan 17 01:15 PM EST"),
    (10, "Oct 17 02:15 PM EDT"),
])
def test_root_name_uses_saved_eastern_time_and_retains_utc(tmp_path, month, expected):
    instant = datetime(2026, month, 17, 18, 15, tzinfo=timezone.utc)
    store = StateStore(tmp_path, clock=instant.timestamp)
    _seed(store)
    project_run(store, "mission-1", run_id="run-1")
    anchor = next(item for item in store.records(EXPORT_KIND)
                  if item["body"]["attributes"].get("osra.projection.anchor"))
    assert expected in anchor["body"]["name"]
    assert anchor["body"]["attributes"]["osra.run.started_utc"] == (
        f"2026-{month:02d}-17T18:15:00Z"
    )


def test_run_summary_is_weighted_versioned_and_readable_without_private_input(tmp_path):
    store = StateStore(tmp_path)
    question = ("Assess treatment outcomes using https://private.example/report?token=hidden "
                "and api_key=supersecret for a long confidential instruction that stays local")

    def seed(tx):
        tx.put("mission_run", "run-summary", {
            "mission_id": "mission-summary", "question": question, "status": "active",
        })
        tx.put("agent_instance", "actor-summary", {
            "mission_id": "mission-summary", "run_id": "run-summary",
        })
        call = tx.put("agent_call", "call-summary", {
            "mission_id": "mission-summary", "instance_id": "actor-summary",
            "task_id": "task-summary", "attempt_id": "attempt-summary",
        })
        call_ref = {key: call[key] for key in ("kind", "id", "version")}
        intent = tx.put("model_request_intent", "model-intent:call-summary:1", {
            "mission_id": "mission-summary", "call_ref": call_ref,
            "request_sha256": "a" * 64,
        })
        intent_ref = {key: intent[key] for key in ("kind", "id", "version")}
        tx.put("model_dispatch_attempt", f"model-dispatch:{intent['id']}", {
            "mission_id": "mission-summary", "intent_ref": intent_ref,
            "request_sha256": "a" * 64,
        })
        tx.put("model_response_receipt", f"model-response:{intent['id']}", {
            "mission_id": "mission-summary", "task_id": "task-summary",
            "attempt_id": "attempt-summary", "call_ref": call_ref,
            "intent_ref": intent_ref, "request_sha256": "a" * 64,
            "status": "complete", "usage": {
                "prompt_tokens": 100, "completion_tokens": 20,
                "total_tokens": 120,
                "prompt_tokens_details": {"cached_tokens": 10},
                "completion_tokens_details": {"reasoning_tokens": 5},
            },
        })
        figure = tx.put("figure_request_intent", "figure-intent-summary", {
            "mission_id": "mission-summary", "run_id": "run-summary",
            "task_id": "task-summary", "request_sha256": "b" * 64,
        })
        figure_ref = {key: figure[key] for key in ("kind", "id", "version")}
        tx.put("figure_dispatch_attempt", f"figure-dispatch:{figure['id']}", {
            "mission_id": "mission-summary", "intent_ref": figure_ref,
            "request_sha256": "b" * 64,
        })
        tx.put("figure_response_receipt", f"figure-response:{figure['id']}", {
            "mission_id": "mission-summary", "intent_ref": figure_ref,
            "request_sha256": "b" * 64, "status": "generated_unverified",
            "usage": {"prompt_tokens": 50, "completion_tokens": 10, "total_tokens": 60},
        })
        return {"saved": True}

    store.transact("seed-summary", {}, seed, mission_id="mission-summary")
    first = project_run_summary(store, "mission-summary", "run-summary")
    assert first["created"] == 2
    assert project_run_summary(store, "mission-summary", "run-summary")["created"] == 0
    records = store.records(EXPORT_KIND)
    anchor = next(item for item in records
                  if item["body"]["attributes"].get("osra.projection.anchor"))
    summary = store.get(**first["summary_ref"])
    assert anchor["body"]["name"].startswith("OSRA · Assess treatment outcomes using [link]")
    assert anchor["body"]["name"].split(" · ")[-2].endswith(("EDT", "EST"))
    assert anchor["body"]["attributes"]["osra.run.started_utc"].endswith("Z")
    assert anchor["body"]["name"].endswith(anchor["body"]["attributes"]["osra.run.short_ref"])
    assert len(anchor["body"]["attributes"]["osra.run.label"]) <= 72
    saved = str(records)
    assert "private.example" not in saved and "supersecret" not in saved
    assert "confidential instruction that stays local" not in saved
    values = summary["body"]["attributes"]
    assert values["osra.summary.input_tokens"] == 150
    assert values["osra.summary.output_tokens"] == 30
    assert values["osra.summary.reasoning_output_tokens"] == 5
    assert values["osra.summary.input_percent"] == round(150 / 180 * 100, 3)
    assert values["osra.summary.output_percent"] == round(30 / 180 * 100, 3)
    assert values["osra.summary.cache_read_percent"] == 10.0
    assert values["osra.summary.cache_covered_calls"] == 1
    assert values["osra.summary.cache_total_calls"] == 2
    assert values["osra.summary.cache_coverage_status"] == "partial"
    assert values["osra.summary.cache_hit_calls"] == 1
    assert values["osra.summary.cache_miss_calls"] == 0
    assert values["osra.summary.cache_input_coverage_percent"] == round(100 / 150 * 100, 3)
    assert values["osra.summary.retrieval_recorded_attempt_count"] == 0
    assert "osra.summary.retrieval_http_attempt_count" not in values
    assert values["osra.summary.reasoning_output_percent"] == 25.0
    assert ("Cache: 10.0% of input cached on reporting records (10 tokens); "
            "1/2 saved request records with paired detail (partial coverage"
            in values["output.value"])
    assert "1/2 captured responses had paired cache and input detail" in values["output.value"]
    assert "Whole-run cached-input percentage unknown" in values["output.value"]
    assert "Provider tokens (reported subset): 150 input, 30 output" in values["output.value"]
    assert values["output.value"] == render_summary_output(values)
    assert values["osra.summary.call_count"] == 2
    assert values["osra.summary.call_count_basis"] == "saved_request_intents"
    assert values["osra.summary.cache_coverage_basis"] == "saved_request_intents"
    assert values["osra.summary.model_call_count"] == 1
    assert values["osra.summary.model_dispatch_recorded_count"] == 1
    assert values["osra.summary.model_captured_response_count"] == 1
    assert values["osra.summary.prepared_only_count"] == 0
    assert not any(key.startswith("gen_ai.usage.") for key in values)
    assert summary["body"]["parent_span_id"] == anchor["body"]["span_id"]

    response = store.get("model_response_receipt", "model-response:model-intent:call-summary:1")
    store.transact("revise-summary-usage", {}, lambda tx: tx.put(
        "model_response_receipt", response["id"], {
            **response["body"], "usage": {
                **response["body"]["usage"], "prompt_tokens": 110,
                "prompt_tokens_details": {"cached_tokens": 20},
            },
        }, expected_version=response["version"]), mission_id="mission-summary")
    second = project_run_summary(store, "mission-summary", "run-summary")
    assert second["created"] == 1 and second["digest"] != first["digest"]
    assert store.get(**first["summary_ref"])["body"]["status"] == "pending"
    assert store.get(**second["summary_ref"])["body"]["attributes"][
        "osra.summary.cache_read_percent"
    ] == round(20 / 110 * 100, 3)
    sent = []
    result = export_pending(store, _config(), mission_id="mission-summary",
                            run_id="run-summary",
                            sender=lambda _config, body: sent.append(body) or True)
    assert result["sent"] == 3
    assert sum(body["name"] == "osra.run.summary" for body in sent) == 2
    assert any(body["name"].startswith("OSRA · Assess treatment outcomes") for body in sent)
    assert "private.example" not in str(sent) and "supersecret" not in str(sent)
    assert all(not any(key.startswith("gen_ai.usage.") for key in body["attributes"])
               for body in sent)


def _cache_summary(store: StateStore, observation: dict, cached_tokens: int | None) -> dict:
    def seed(tx):
        tx.put("mission_run", "run-cache", {
            "mission_id": "mission-cache", "status": "active",
        })
        tx.put("agent_instance", "actor-cache", {
            "mission_id": "mission-cache", "run_id": "run-cache",
        })
        call = tx.put("agent_call", "call-cache", {
            "mission_id": "mission-cache", "instance_id": "actor-cache",
            "task_id": "task-cache", "attempt_id": "attempt-cache",
        })
        call_ref = {key: call[key] for key in ("kind", "id", "version")}
        intent = tx.put("model_request_intent", "model-intent:call-cache:1", {
            "mission_id": "mission-cache", "call_ref": call_ref,
            "request_sha256": "a" * 64,
        })
        intent_ref = {key: intent[key] for key in ("kind", "id", "version")}
        usage = {"prompt_tokens": 100, "completion_tokens": 20, "total_tokens": 120}
        if cached_tokens is not None:
            usage["prompt_tokens_details"] = {"cached_tokens": cached_tokens}
        tx.put("model_response_receipt", f"model-response:{intent['id']}", {
            "mission_id": "mission-cache", "task_id": "task-cache",
            "attempt_id": "attempt-cache", "call_ref": call_ref,
            "intent_ref": intent_ref, "request_sha256": "a" * 64,
            "status": "complete", "usage": usage,
            "cache_observation": observation,
        })
        return {"saved": True}

    store.transact("seed-cache-summary", {}, seed, mission_id="mission-cache")
    projected = project_run_summary(store, "mission-cache", "run-cache")
    return store.get(**projected["summary_ref"])


@pytest.mark.parametrize(("observation", "cached_tokens", "expected"), [
    ({"status": "not_reported"}, None,
     "Cache: Not reported by Groq (0/1 saved request records); cached-input percentage unknown."),
    ({"status": "reported_zero", "cached_tokens": 0}, 0,
     "Cache: 0.0% cached input (0 tokens); 1/1 saved request records with paired detail "
     "(complete coverage; 0 reported hit, 1 reported zero)."),
    ({"status": "reported_hit", "cached_tokens": 30}, 30,
     "Cache: 30.0% cached input (30 tokens); 1/1 saved request records with paired detail "
     "(complete coverage; 1 reported hit, 0 reported zero)."),
])
def test_summary_output_cache_reporting_is_prominent_and_precise(
    tmp_path, observation, cached_tokens, expected,
):
    store = StateStore(tmp_path)
    summary = _cache_summary(store, observation, cached_tokens)
    output = summary["body"]["attributes"]["output.value"]
    assert expected in output
    assert len(output) < 1600


def test_partial_cache_output_separates_saved_markers_from_actual_http_attempts():
    attributes = {
        "osra.summary.status": "active",
        "osra.summary.evaluation_outcome": "token_cap",
        "osra.summary.evaluation_model_http_attempts": 7,
        "osra.summary.evaluation_retrieval_http_attempts": 2,
        "osra.summary.cache_covered_calls": 1,
        "osra.summary.cache_total_calls": 8,
        "osra.summary.cache_read_percent": 31.3,
        "osra.summary.cache_read_tokens": 1792,
        "osra.summary.cache_hit_calls": 1,
        "osra.summary.cache_miss_calls": 0,
        "osra.summary.cache_not_reported_calls": 6,
        "osra.summary.cache_unknown_calls": 1,
        "osra.summary.captured_call_count": 7,
        "osra.summary.model_dispatch_recorded_count": 8,
        "osra.summary.model_captured_response_count": 7,
        "osra.summary.retrieval_recorded_attempt_count": 1,
        "osra.summary.error_or_unresolved_count": 1,
    }
    output = render_summary_output(attributes)
    assert "Actual HTTP attempts (validation ledger): 7 model, 2 retrieval" in output
    assert "1/8 saved request records with paired detail" in output
    assert "1/7 captured responses had paired cache and input detail" in output
    assert "31.3% of input cached on reporting records" in output
    assert "Whole-run cached-input percentage unknown" in output
    assert "8 pre-provider model dispatch markers, 7 captured responses" in output
    assert "1 model/figure request record with errors or unresolved status" in output
    assert "Markers do not establish HTTP attempts" in output
    assert "8 model HTTP attempts" not in output


def test_summary_output_reports_controller_stop_separately_from_active_mission(tmp_path):
    store = StateStore(tmp_path)
    _cache_summary(store, {"status": "not_reported"}, None)
    ledger = {"schema": "ora.agent_validation_budget.v1",
              "model_attempts": [{"state": "observed"}],
              "retrieval_attempts": [{"state": "observed"}]}
    record_evaluation_observation(store, "run-cache", ledger, "model_attempt_cap", 12.5)
    updated = project_run_summary(store, "mission-cache", "run-cache")
    output = store.get(**updated["summary_ref"])["body"]["attributes"]["output.value"]
    assert "Mission status: active." in output
    assert "Evaluation controller: model attempt cap; observation stopped." in output
    assert "Actual HTTP attempts (validation ledger): 1 model, 1 retrieval." in output
    assert "PRIVATE" not in output


def test_saved_private_summary_output_is_rejected_before_sender(tmp_path):
    store = StateStore(tmp_path)
    summary = _cache_summary(store, {"status": "not_reported"}, None)
    store.transact("inject-private-output", {}, lambda tx: tx.put(
        EXPORT_KIND, summary["id"], {
            **summary["body"], "attributes": {
                **summary["body"]["attributes"],
                "output.value": "PRIVATE_SOURCE_PAYLOAD",
            },
        }, expected_version=summary["version"]), mission_id="mission-cache")
    sent = []
    export_pending(store, _config(), mission_id="mission-cache", run_id="run-cache",
                   sender=lambda _config, body: sent.append(body) or True)
    assert all(body["name"] != "osra.run.summary" for body in sent)
    assert store.get(EXPORT_KIND, summary["id"])["body"]["reason"] == "invalid_projection"
    assert "PRIVATE_SOURCE_PAYLOAD" not in str(sent)


def _diagnostic_report(*, pair: bool = False, missing_usage: bool = False) -> dict:
    attempts = 2 if pair else 1
    captured = 0 if missing_usage else 1
    known = 0 if missing_usage else 1

    def field(tokens, *, unknown=0, not_reported=0, reported=None):
        reported = known if reported is None else reported
        return {"reported_tokens": tokens if reported else None,
                "reported_attempts": reported, "unknown_attempts": unknown,
                "not_reported_attempts": not_reported,
                "invalid_attempts": 0, "legacy_uncertain_attempts": 0}

    return {
        "schema": "ora.local_run_report.v1", "kind": "api_diagnostic",
        "report_id": "0123456789abcdef" if not pair else "fedcba9876543210",
        "task_label": "Synthetic exact-repeat check" if pair else "Cache wire probe",
        "started_at_utc": "2026-10-17T18:00:00+00:00",
        "profile_label": "PRIVATE_PROFILE_UNUSED", "model_label": "fixture-model",
        "outcome_status": "incomplete_pair" if pair else "single_response_observed",
        "mission_status": None,
        "counts": {"model_http_attempts": attempts,
                   "model_captured_responses": captured,
                   "model_usage_unknown_attempts": attempts - captured,
                   "retrieval_http_attempts": 0,
                   "retrieval_recorded_attempts": None},
        "usage": {
            "input": field(100, unknown=attempts - captured),
            "output": field(20, unknown=attempts - captured),
            "reasoning": field(5, unknown=attempts - captured),
            "cache_read": field(None, unknown=attempts - captured,
                                not_reported=captured, reported=0),
            "cache_fraction_reported_subset": {
                "value": None, "coverage_status": "unavailable",
                "covered_calls": 0, "total_calls": attempts,
                "eligible_input_coverage_fraction": None,
                "reported_hit_calls": 0, "reported_miss_calls": 0,
            },
            "input_fraction_reported_subset": 100 / 120 if known else None,
            "output_fraction_reported_subset": 20 / 120 if known else None,
            "reasoning_fraction_reported_subset": 5 / 20 if known else None,
        },
        "elapsed_seconds": 2.5,
        "timing": {
            "observed_window_seconds": 2.5,
            "observed_window_basis": "ledger_first_start_to_last_attempt_end",
            "first_ledger_started_at_utc": "2026-10-17T18:00:00+00:00",
            "measured_http_seconds_sum": 1.0,
            "between_model_attempt_gap_seconds_sum": 1.5 if pair else 0.0,
        },
        "attempts": [
            {"index": index,
             "input_tokens": 100 if known and index == attempts else None,
             "output_tokens": 20 if known and index == attempts else None,
             "private_payload": "PRIVATE_SOURCE_PAYLOAD"}
            for index in range(1, attempts + 1)
        ],
        "evidence_basis": "recognized_direct_result_and_exact_http_ledger",
    }


def test_standalone_diagnostic_summary_is_scoped_readable_and_idempotent(tmp_path):
    store = StateStore(tmp_path)
    report = _diagnostic_report()
    first = project_diagnostic_summary(store, report)
    second = project_diagnostic_summary(store, report)
    assert first["created"] == 1 and second["created"] == 0
    assert first["export_ref"] == second["export_ref"]
    assert store.records("mission_run") == []
    saved = store.get(**first["export_ref"])["body"]
    assert saved["name"] == "osra.diagnostic.summary"
    assert saved["parent_span_id"] is None
    assert saved["attributes"]["langsmith.trace.name"].startswith(
        "OSRA diagnostic · Cache wire probe · Oct 17"
    )
    assert saved["attributes"]["output.value"].startswith(
        "Diagnostic: Cache wire probe.\nOutcome: single response observed; "
        "no research mission completion claimed."
    )
    assert "Not reported by Groq (0/1 HTTP attempts)" in saved["attributes"]["output.value"]
    assert not any(key.startswith("gen_ai.usage.") for key in saved["attributes"])
    assert "PRIVATE" not in str(saved)
    sent = []
    result = export_pending(store, _config(), mission_id=first["mission_id"],
                            sender=lambda _config, body: sent.append(body) or True)
    assert result["sent"] == 1 and len(sent) == 1
    assert project_diagnostic_summary(store, report)["created"] == 0
    assert export_pending(store, _config(), mission_id=first["mission_id"],
                          sender=lambda *_: pytest.fail("resent"))["sent"] == 0


def test_incomplete_diagnostic_pair_explains_missing_cache_and_usage(tmp_path):
    store = StateStore(tmp_path)
    projected = project_diagnostic_summary(store, _diagnostic_report(pair=True))
    saved = store.get(**projected["export_ref"])["body"]
    output = saved["attributes"]["output.value"]
    assert "Outcome: incomplete pair; no research mission completion claimed." in output
    assert ("Not reported by Groq on 1 response; 1 attempt unavailable "
            "(0/2 HTTP attempts covered); cached-input percentage unknown.") in output
    assert "2 model, 0 retrieval; 1 captured model response" in output
    assert saved["attributes"]["osra.summary.diagnostic_model_usage_unknown_attempts"] == 1
    assert saved["attributes"]["osra.summary.total_tokens"] == 120
    assert saved["attributes"]["osra.summary.token_mix_covered_calls"] == 1

    unsupported = _diagnostic_report(pair=True, missing_usage=True)
    with pytest.raises(StateError, match="recognized direct API case"):
        project_diagnostic_summary(StateStore(tmp_path / "unsupported"), unsupported)


def test_diagnostic_projection_rejects_private_output_and_unreviewed_label(tmp_path):
    store = StateStore(tmp_path)
    report = _diagnostic_report()
    bad = {**report, "task_label": "PRIVATE_TASK_LABEL"}
    with pytest.raises(StateError):
        project_diagnostic_summary(store, bad)
    projected = project_diagnostic_summary(store, report)
    saved = store.get(**projected["export_ref"])
    store.transact("inject-diagnostic-output", {}, lambda tx: tx.put(
        EXPORT_KIND, saved["id"], {
            **saved["body"], "attributes": {
                **saved["body"]["attributes"], "output.value": "PRIVATE_SOURCE_PAYLOAD",
            },
        }, expected_version=saved["version"]), mission_id=projected["mission_id"])
    sent = []
    export_pending(store, _config(), mission_id=projected["mission_id"],
                   sender=lambda _config, body: sent.append(body) or True)
    assert sent == []
    assert store.get(EXPORT_KIND, saved["id"])["body"]["reason"] == "invalid_projection"


@pytest.mark.parametrize("values", [
    {"OSRA_TRACE_EXPORT_ENABLED": "yes"},
    {"OSRA_TRACE_EXPORT_ENABLED": "true", "LANGSMITH_API_KEY": "k",
     "OSRA_TRACE_LANGSMITH_PROJECT": "p"},
    {"OSRA_TRACE_EXPORT_ENABLED": "true", "LANGSMITH_API_KEY": "k",
     "OSRA_TRACE_LANGSMITH_PROJECT": "p",
     "OSRA_TRACE_OTLP_TRACES_ENDPOINT": "http://api.smith.langchain.com/otel/v1/traces"},
    {"OSRA_TRACE_EXPORT_ENABLED": "true", "LANGSMITH_API_KEY": "k",
     "OSRA_TRACE_LANGSMITH_PROJECT": "p",
     "OSRA_TRACE_OTLP_TRACES_ENDPOINT": "https://elsewhere.example/otel/v1/traces"},
])
def test_export_configuration_rejects_implicit_or_unreviewed_destinations(values):
    with pytest.raises(StateError):
        ExportConfig.from_env(values)


def test_local_reuse_is_separate_from_provider_cache_and_private_payload(tmp_path):
    store = StateStore(tmp_path)
    report = _diagnostic_report()
    report['local_prefix_reuse'] = {
        'basis': 'local_o200k_canonical_request_structure.v1', 'status': 'measured',
        'eligible_requests': 2, 'eligible_pairs': 1, 'measured_pairs': 1,
        'stable_tool_schema_pairs': 0, 'estimated_repeated_message_tokens': 75,
        'estimated_current_message_tokens': 100,
        'estimated_message_prefix_token_fraction': 0.75,
        'private_prompt': 'PRIVATE_SOURCE_PAYLOAD',
    }
    projected = project_diagnostic_summary(store, report)
    body = store.get(**projected['export_ref'])['body']
    attrs = body['attributes']
    assert '75.0% of compared follow-up message' in attrs['output.value']
    assert 'not provider cache hits' in attrs['output.value']
    assert 'Not reported by Groq' in attrs['output.value']
    assert 'osra.summary.cache_read_tokens' not in attrs
    assert not any(key.startswith('gen_ai.usage.') for key in attrs)
    assert 'PRIVATE_SOURCE_PAYLOAD' not in str(body)
    assert export_pending(store, _config(), mission_id=projected['mission_id'],
                          sender=lambda *_: True)['sent'] == 1


def test_changed_diagnostic_measurement_creates_new_summary_without_replay(tmp_path):
    store = StateStore(tmp_path)
    report = _diagnostic_report()
    first = project_diagnostic_summary(store, report)
    assert export_pending(store, _config(), mission_id=first['mission_id'],
                          sender=lambda *_: True)['sent'] == 1
    original = store.get(first['export_ref']['kind'], first['export_ref']['id'])
    report['local_prefix_reuse'] = {
        'basis': 'local_o200k_canonical_request_structure.v1',
        'status': 'no_eligible_pair', 'eligible_requests': 1,
        'eligible_pairs': 0, 'measured_pairs': 0,
    }
    second = project_diagnostic_summary(store, report)
    assert first['export_ref']['id'] != second['export_ref']['id']
    assert store.get(original['kind'], original['id']) == original
    sent = []
    assert export_pending(store, _config(), mission_id=first['mission_id'],
                          sender=lambda _config, body: sent.append(body) or True)['sent'] == 1
    assert len(sent) == 1
    assert project_diagnostic_summary(store, report)['created'] == 0
    assert export_pending(store, _config(), mission_id=first['mission_id'],
                          sender=lambda *_: pytest.fail('resend'))['sent'] == 0


def test_outbound_local_overlap_requires_consistent_counts_and_fraction():
    from ora.observability.export import _valid_local_reuse_attributes

    values = {'osra.local_reuse.status': 'measured',
              'osra.local_reuse.eligible_requests': 3,
              'osra.local_reuse.eligible_pairs': 2,
              'osra.local_reuse.measured_pairs': 2,
              'osra.local_reuse.stable_tool_schema_pairs': 1,
              'osra.local_reuse.estimated_repeated_message_tokens': 75,
              'osra.local_reuse.estimated_current_message_tokens': 100,
              'osra.local_reuse.message_prefix_percent': 75.0}
    assert _valid_local_reuse_attributes(values)
    for key, invalid in [('measured_pairs', 3), ('stable_tool_schema_pairs', 3),
                         ('estimated_repeated_message_tokens', 101),
                         ('message_prefix_percent', 50.0)]:
        assert not _valid_local_reuse_attributes({**values, 'osra.local_reuse.' + key: invalid})


def test_named_tool_projection_is_measured_private_and_restart_safe(tmp_path):
    from tests.observability.test_tool_activity import (
        _followup_messages,
        _result,
        _turn,
        _work,
    )
    from tests.observability.test_tool_activity import (
        _seed as seed_tool,
    )

    store = StateStore(tmp_path)
    seed_tool(store)
    call, response, selected = _turn(store, "first", "request_capability")
    _work(store, "first", call, selected)
    _, message = _result(store, response, selected, {
        "status": "selected_for_next_turn", "tool_id": "save_plan",
        "private_source": "PRIVATE_SOURCE_CONTENT",
    })
    _turn(store, "second", "save_plan", messages=_followup_messages(selected, message))

    assert project_tool_activity(store, "mission-a", "run-a") == {
        "created": 2, "scanned": 2, "next_after_id": None,
    }
    assert project_tool_activity(store, "mission-a", "run-a")["created"] == 0
    tool = next(row for row in store.records(EXPORT_KIND)
                if row["body"]["name"].startswith("Loop "))
    body = tool["body"]
    attributes = body["attributes"]
    assert body["name"] == "Loop 1 · request_capability"
    assert attributes["langsmith.span.kind"] == "tool"
    assert attributes["openinference.span.kind"] == "TOOL"
    assert attributes["gen_ai.tool.name"] == "request_capability"
    assert attributes["osra.tool.category"] == "capability_selection"
    assert attributes["osra.tool.execution_status"] == "executed"
    assert attributes["osra.tool.delivery_status"] == "captured_later"
    assert attributes["osra.tool.delivery_form"] == "exact_saved_message"
    assert attributes["osra.tool.active_seconds"] == 0.2
    assert attributes["osra.observed.wall_seconds"] == 0.25
    assert attributes["osra.tool.interval_basis"] == "single_capacity_attempt_wall_interval"
    assert not any(key.startswith("gen_ai.usage.") for key in attributes)
    assert "PRIVATE_SOURCE_CONTENT" not in str(body)

    sent = []
    first = export_pending(store, _config(), mission_id="mission-a", run_id="run-a",
                           sender=lambda _config, span: sent.append(span) or True)
    assert first["sent"] == 2 and first["uncertain"] == 0
    assert any(span["name"] == "Loop 1 · request_capability" for span in sent)

    ledger = {"schema": "ora.agent_validation_budget.v1",
              "model_attempts": [], "retrieval_attempts": []}
    record_evaluation_observation(store, "run-a", ledger, "model_attempt_cap", 1.0)
    assert project_tool_activity(store, "mission-a", "run-a")["created"] == 1
    pending = next(row for row in store.records(EXPORT_KIND)
                   if row["body"]["name"] == "Loop 2 · save_plan")
    assert pending["body"]["attributes"]["osra.tool.execution_status"] == "not_executed"
    assert pending["body"]["attributes"]["osra.tool.active_duration_status"] == "unavailable"
    assert pending["body"]["attributes"]["osra.tool.interval_status"] == "unavailable"
    assert "execution interval unavailable" in pending["body"]["attributes"]["output.value"]
    summary = project_run_summary(store, "mission-a", "run-a")
    summary_attributes = store.get(**summary["summary_ref"])["body"]["attributes"]
    assert summary_attributes["osra.tools.selected_count"] == 2
    assert summary_attributes["osra.tools.capability_selection_count"] == 1
    assert summary_attributes["osra.tools.operation_count"] == 1
    assert summary_attributes["osra.tools.delivered_count"] == 1
    assert "Tools: 2 selected" in summary_attributes["output.value"]
    assert not any(key.startswith("gen_ai.usage.") for key in summary_attributes)
    restarted = StateStore(tmp_path)
    assert project_tool_activity(restarted, "mission-a", "run-a")["created"] == 0
    followup = export_pending(restarted, _config(), mission_id="mission-a", run_id="run-a",
                              sender=lambda _config, span: sent.append(span) or True)
    assert followup["sent"] == 2 and followup["uncertain"] == 0
    assert sum(span["name"] == "Loop 2 · save_plan" for span in sent) == 1
    assert export_pending(restarted, _config(), mission_id="mission-a", run_id="run-a",
                          sender=lambda *_: pytest.fail("resent tool"))["sent"] == 0


def test_named_tool_export_rejects_private_fields_and_wrong_scope(tmp_path):
    from tests.observability.test_tool_activity import (
        _followup_messages,
        _result,
        _turn,
    )
    from tests.observability.test_tool_activity import (
        _seed as seed_tool,
    )

    store = StateStore(tmp_path)
    seed_tool(store)
    _, response, tool = _turn(store, "first", "read_source_window")
    _, message = _result(store, response, tool, {"status": "completed"})
    _turn(store, "second", "save_plan", messages=_followup_messages(tool, message))
    with pytest.raises(StateError, match="does not belong"):
        project_tool_activity(store, "other-mission", "run-a")
    project_tool_activity(store, "mission-a", "run-a")
    saved = next(row for row in store.records(EXPORT_KIND)
                 if row["body"]["name"].startswith("Loop "))
    store.transact("inject-private-tool-data", {}, lambda tx: tx.put(
        EXPORT_KIND, saved["id"], {
            **saved["body"], "attributes": {
                **saved["body"]["attributes"],
                "gen_ai.usage.input_tokens": 999,
                "private_prompt": "PRIVATE_SOURCE_CONTENT",
            },
        }, expected_version=saved["version"]), mission_id="mission-a")
    sent = []
    result = export_pending(store, _config(), mission_id="mission-a", run_id="run-a",
                            sender=lambda _config, body: sent.append(body) or True)
    assert not any(body["name"].startswith("Loop ") for body in sent)
    assert result["uncertain"] == 1
    assert store.get(EXPORT_KIND, saved["id"])["body"]["reason"] == "invalid_projection"


@pytest.mark.parametrize("first_acknowledged", [True, False])
def test_tool_delivery_change_refreshes_unsent_but_never_replays_uncertain(
        tmp_path, first_acknowledged):
    from tests.observability.test_tool_activity import (
        _followup_messages,
        _result,
        _turn,
    )
    from tests.observability.test_tool_activity import (
        _seed as seed_tool,
    )

    store = StateStore(tmp_path)
    seed_tool(store)
    _, response, tool = _turn(store, "first", "read_source_window")
    _, message = _result(store, response, tool, {"status": "completed"})
    record_evaluation_observation(store, "run-a", {
        "schema": "ora.agent_validation_budget.v1",
        "model_attempts": [], "retrieval_attempts": [],
    }, "model_attempt_cap", 1.0)
    assert project_tool_activity(store, "mission-a", "run-a")["created"] == 2
    original = next(row for row in store.records(EXPORT_KIND)
                    if row["body"]["name"].startswith("Loop "))
    assert original["body"]["attributes"]["osra.tool.delivery_status"] == (
        "not_delivered_after_observation"
    )
    if not first_acknowledged:
        export_pending(store, _config(), mission_id="mission-a", run_id="run-a",
                       sender=lambda _config, body: not body["name"].startswith("Loop "))
        assert store.get(EXPORT_KIND, original["id"])["body"]["status"] == "uncertain"

    _turn(store, "second", "save_plan", messages=_followup_messages(tool, message))
    if first_acknowledged:
        sent = []
        result = export_pending(store, _config(), mission_id="mission-a", run_id="run-a",
                                sender=lambda _config, body: sent.append(body) or True)
        assert result["uncertain"] == 0 and result["sent"] == 2
        tool_span = next(body for body in sent if body["name"].startswith("Loop "))
        assert tool_span["attributes"]["osra.tool.next_delivery_status"] == (
            "captured_with_exact_result"
        )
        assert tool_span["attributes"]["osra.tool.delivery_status"] == "captured_later"
        assert "tool-message status: captured with exact result" in (
            tool_span["attributes"]["output.value"]
        )
        assert store.get(EXPORT_KIND, original["id"])["version"] > original["version"]
        assert project_tool_activity(store, "mission-a", "run-a")["created"] == 1
        assert len([row for row in store.records(EXPORT_KIND)
                    if row["body"]["attributes"].get("osra.tool.step_id")
                    == original["body"]["attributes"]["osra.tool.step_id"]]) == 1
    else:
        assert project_tool_activity(store, "mission-a", "run-a")["created"] == 1
        assert len([row for row in store.records(EXPORT_KIND)
                    if row["body"]["attributes"].get("osra.tool.step_id")
                    == original["body"]["attributes"]["osra.tool.step_id"]]) == 1
        sent = []
        assert export_pending(store, _config(), mission_id="mission-a", run_id="run-a",
                              sender=lambda _config, body: (
                                  pytest.fail("replayed uncertain tool")
                                  if body["attributes"].get("osra.tool.step_id")
                                  == original["body"]["attributes"]["osra.tool.step_id"]
                                  else sent.append(body) or True))["sent"] == 1
        assert sent[0]["name"] == "Loop 2 · save_plan"


def test_sent_tool_delivery_update_is_append_only_and_batch_reuses_activity(
        tmp_path, monkeypatch):
    import ora.observability.export as exporter
    from tests.observability.test_tool_activity import (
        _followup_messages,
        _result,
        _turn,
    )
    from tests.observability.test_tool_activity import (
        _seed as seed_tool,
    )

    store = StateStore(tmp_path)
    seed_tool(store)
    _, response, tool = _turn(store, "first", "read_source_window")
    _, message = _result(store, response, tool, {"status": "completed"})
    record_evaluation_observation(store, "run-a", {
        "schema": "ora.agent_validation_budget.v1",
        "model_attempts": [], "retrieval_attempts": [],
    }, "model_attempt_cap", 1.0)
    assert project_tool_activity(store, "mission-a", "run-a")["created"] == 2
    original = next(row for row in store.records(EXPORT_KIND)
                    if row["body"]["name"].startswith("Loop "))
    assert export_pending(store, _config(), mission_id="mission-a", run_id="run-a",
                          sender=lambda *_: True)["sent"] == 2

    _turn(store, "second", "save_plan", messages=_followup_messages(tool, message))
    assert project_tool_activity(store, "mission-a", "run-a")["created"] == 2
    assert store.get(EXPORT_KIND, original["id"], original["version"]) == original
    assert store.get(EXPORT_KIND, original["id"])["body"]["status"] == "sent"
    updated = [row for row in store.records(EXPORT_KIND)
               if row["body"]["attributes"].get("osra.tool.step_id")
               == original["body"]["attributes"]["osra.tool.step_id"]]
    assert len(updated) == 2
    assert sorted(row["body"]["attributes"]["osra.tool.projection_revision"]
                  for row in updated) == [1, 2]

    original_builder = exporter.build_tool_activity
    calls = []

    def counted(*args):
        calls.append(args[1])
        return original_builder(*args)

    monkeypatch.setattr(exporter, "build_tool_activity", counted)
    sent = []
    result = export_pending(store, _config(), mission_id="mission-a", run_id="run-a",
                            sender=lambda _config, body: sent.append(body) or True)
    assert result["sent"] == 2 and result["uncertain"] == 0
    assert calls == ["run-a"]  # One exact activity snapshot for this export batch.
    assert sorted(span["name"] for span in sent) == [
        "Loop 1 · read_source_window", "Loop 2 · save_plan",
    ]


def test_tool_activity_cache_reuses_pages_and_invalidates_on_new_source_record(
        tmp_path, monkeypatch):
    import ora.observability.export as exporter
    from tests.observability.test_tool_activity import _seed as seed_tool
    from tests.observability.test_tool_activity import _turn

    store = StateStore(tmp_path)
    seed_tool(store, status="completed")
    for index in range(12):
        _turn(store, f"loop-{index}", "save_plan")
    built = []
    original_builder = exporter.build_tool_activity

    def counted(*args):
        built.append(args[1])
        return original_builder(*args)

    monkeypatch.setattr(exporter, "build_tool_activity", counted)
    cache = exporter.ToolActivityCache()
    cursor = ""
    scanned = 0
    while True:
        page = project_tool_activity(store, "mission-a", "run-a", limit=1,
                                     after_id=cursor, activity_cache=cache)
        scanned += page["scanned"]
        cursor = page["next_after_id"]
        if cursor is None:
            break
    assert scanned == 12
    assert built == ["run-a"]
    assert project_run_summary(store, "mission-a", "run-a",
                               activity_cache=cache)["created"] >= 1
    assert built == ["run-a"]  # Export records do not change the source high-water mark.

    _turn(store, "new-loop", "save_plan")
    project_tool_activity(store, "mission-a", "run-a", limit=1,
                          activity_cache=cache)
    assert built == ["run-a", "run-a"]
    with pytest.raises(StateError, match="does not belong"):
        cache.get(store, "another-mission", "run-a")
    with pytest.raises(StateError, match="cache"):
        project_tool_activity(store, "mission-a", "run-a", activity_cache={})
