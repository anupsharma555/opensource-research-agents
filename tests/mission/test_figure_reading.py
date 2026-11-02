"""Offline Qwen figure requests preserve source authority and exact dispatch state."""

from __future__ import annotations

import hashlib
import io
import json
import urllib.error
from pathlib import Path

import pytest

from ora.coordination.board import ref
from ora.evidence.document_ocr import ocr_public_document_page, render_public_document_page
from ora.mission.figure_reading import (
    IMAGE_INPUT_TOKENS,
    FigureDispatchUncertain,
    FigureReader,
    FigureReadingError,
    estimate_vision_tokens,
    read_figure_result,
)
from ora.state import StateStore
from tests.evidence.test_document_ocr import SCOPE, URL, saved_public_pdf, synthetic_pdf


class Reply:
    status = 200
    headers = {"x-ratelimit-remaining-tokens": "5000"}

    def __init__(self, payload: dict):
        self.raw = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def read(self, _limit):
        return self.raw


def _granted_page(store: StateStore, *, allow_vision: bool = True) -> tuple[dict, dict, dict]:
    document = saved_public_pdf(store, synthetic_pdf("Synthetic figure label"))
    source_hash = store.get(**document)["body"]["content_sha256"]

    def grant(tx):
        copy = tx.put("source_copy", "synthetic-figure-copy", {
            "mission_id": SCOPE["mission_id"], "document_ref": document,
            "content_sha256": source_hash, "inspection_state": "checked",
            "reference": URL,
        })
        use = tx.put("source_use", "synthetic-figure-use", {
            "copy_ref": ref(copy), "policy_status": "granted",
            "allowed_uses": ["model_input"],
        })
        tx.bind("source_copy.public_document", f"{document['id']}:{document['version']}",
                "source_copy", copy["id"])
        tx.bind("source_use.copy", f"{copy['id']}:{copy['version']}",
                "source_use", use["id"])
        tx.put("mission_run", SCOPE["run_id"], {
            "mission_id": SCOPE["mission_id"], "status": "active",
            "profile": {"executionMode": "offline_test", "allowVision": allow_vision,
                        "modelSettings": {"credential_profile": "primary",
                                          "quota_domain": "groq-selected"}},
        })
        return {"copy_ref": ref(copy), "use_ref": ref(use)}

    store.transact("grant-synthetic-figure", {}, grant, mission_id=SCOPE["mission_id"])
    rendered = render_public_document_page(
        store, document, **SCOPE, page_number=1, operation_id="render-figure-page",
    )
    assert rendered["model_input_status"] == "permitted"
    ocr = ocr_public_document_page(
        store, rendered["page_image_ref"], **SCOPE, operation_id="ocr-figure-page",
    )
    assert ocr["status"] == "ocr_unverified"
    return document, rendered["page_image_ref"], ocr["ocr_ref"]


def _reader(store: StateStore, transport, monkeypatch) -> FigureReader:
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-test-key")
    return FigureReader(store, Path(__file__).parents[2],
                        transport=transport, allow_test_transport=True)


def _prepare(reader: FigureReader, document: dict, page: dict, ocr: dict, *,
             operation: str = "figure-one") -> dict:
    return reader.prepare(
        operation_id=operation, scope=SCOPE, document_ref=document,
        page_image_ref=page, ocr_ref=ocr, question="What label is visible?",
        credential_profile="primary", quota_domain="groq-selected",
    )


def test_vision_opt_in_and_exact_saved_request_then_single_capture(tmp_path, monkeypatch):
    store = StateStore(tmp_path)
    document, page, ocr = _granted_page(store, allow_vision=False)
    calls = []

    def transport(request, *, timeout):
        calls.append(request)
        body = json.loads(request.data)
        assert body["model"] == "qwen/qwen3.8-27b"
        assert body["reasoning_effort"] == "none"
        assert body["messages"][0]["content"][1]["image_url"]["url"].startswith(
            "data:image/png;base64,"
        )
        assert "https://example.org" not in request.data.decode()
        return Reply({
            "model": "qwen/qwen3.8-27b",
            "choices": [{"message": {"content": "The label reads Synthetic figure label.",
                                      "reasoning": "PRIVATE_REASONING_NOT_TO_SAVE"}}],
            "usage": {"prompt_tokens": 2090, "completion_tokens": 20, "total_tokens": 2110,
                      "prompt_tokens_details": {"cached_tokens": 281, "other": "ignored"},
                      "completion_tokens_details": {"reasoning_tokens": 7},
                      "prompt_time": 0.2},
        })

    reader = _reader(store, transport, monkeypatch)
    with pytest.raises(FigureReadingError, match="not enabled"):
        _prepare(reader, document, page, ocr)
    assert calls == []

    run = store.get("mission_run", SCOPE["run_id"])
    store.transact("enable-synthetic-vision", {}, lambda tx: tx.put(
        "mission_run", run["id"], {
            **run["body"], "profile": {**run["body"]["profile"], "allowVision": True},
        }, expected_version=run["version"],
    ), mission_id=SCOPE["mission_id"])
    production_gate = FigureReader(store, Path(__file__).parents[2], transport=transport)
    with pytest.raises(FigureReadingError, match="Live figure reading"):
        _prepare(production_gate, document, page, ocr)
    assert calls == []
    estimate = estimate_vision_tokens("What label is visible?")
    assert estimate["input_tokens"] >= IMAGE_INPUT_TOKENS
    assert estimate["reserved_tokens"] == estimate["input_tokens"] + 1024
    intent = _prepare(reader, document, page, ocr)
    assert reader.operation_state("figure-one", scope=SCOPE) == {
        "intent_ref": intent, "dispatch_ref": None, "response_ref": None,
    }
    saved = store.get(**intent)
    assert saved["body"]["image_sha256"] == store.get(**page)["body"]["image_sha256"]
    assert hashlib.sha256(store.read_blob(saved["body"]["request_blob"]["sha256"])).hexdigest() == (
        saved["body"]["request_sha256"]
    )
    outcome = reader.invoke_capture(intent, allow_live_calls=False)
    assert outcome.status == "generated_unverified" and outcome.used_tokens == 2110
    state = reader.operation_state("figure-one", scope=SCOPE)
    assert state["intent_ref"] == intent
    assert state["dispatch_ref"] and state["response_ref"] == outcome.response_ref
    assert outcome.rate_headers == {"x-ratelimit-remaining-tokens": "5000"}
    assert reader.invoke_capture(intent, allow_live_calls=False) == outcome
    assert len(calls) == 1
    result = reader.read_result(outcome.response_ref, scope=SCOPE)
    assert result["interpretation"] == "The label reads Synthetic figure label."
    assert result["usage"] == {
        "prompt_tokens": 2090, "completion_tokens": 20, "total_tokens": 2110,
        "prompt_tokens_details": {"cached_tokens": 281},
        "completion_tokens_details": {"reasoning_tokens": 7},
        "prompt_time": 0.2,
    }
    assert result["evidence_status"] == "generated_unverified"
    assert result["ocr_ref"] == ocr and result["source_dependency_refs"]
    accepted_view = store.transact("read-figure-in-acceptance", {}, lambda tx:
        read_figure_result(tx, outcome.response_ref, scope=SCOPE),
        mission_id=SCOPE["mission_id"])
    assert accepted_view["source_dependency_refs"] == result["source_dependency_refs"]
    assert "PRIVATE_REASONING" not in str(store.get(**outcome.response_ref))
    receipt = store.get(**outcome.response_ref)
    assert receipt["body"]["usage"] == result["usage"]
    assert receipt["body"]["cache_observation"] == {
        "status": "reported_hit", "cached_tokens": 281,
    }
    forged = store.transact("forged-figure-receipt", {}, lambda tx: tx.put(
        "figure_response_receipt", receipt["id"], {
            **receipt["body"], "request_sha256": "0" * 64,
        }, expected_version=receipt["version"]), mission_id=SCOPE["mission_id"])
    with pytest.raises(FigureReadingError, match="request lineage"):
        read_figure_result(store, ref(forged), scope=SCOPE)
    selected_use = store.get("source_use", "synthetic-figure-use")
    store.transact("revoke-figure-after-response", {}, lambda tx: tx.put(
        "source_use", selected_use["id"], {
            **selected_use["body"], "allowed_uses": [], "policy_status": "revoked",
        }, expected_version=selected_use["version"]), mission_id=SCOPE["mission_id"])
    with pytest.raises(FigureReadingError, match="Current figure source use"):
        reader.read_result(outcome.response_ref, scope=SCOPE)


def test_revocation_blocks_dispatch_and_uncertain_attempt_never_resends(tmp_path, monkeypatch):
    store = StateStore(tmp_path)
    document, page, ocr = _granted_page(store)
    calls = []
    reader = _reader(store, lambda *_args, **_kwargs: calls.append(1), monkeypatch)
    intent = _prepare(reader, document, page, ocr)
    use = store.get("source_use", "synthetic-figure-use")
    store.transact("revoke-figure-use", {}, lambda tx: tx.put(
        "source_use", use["id"], {
            **use["body"], "allowed_uses": [], "policy_status": "revoked",
        }, expected_version=use["version"]), mission_id=SCOPE["mission_id"])
    with pytest.raises(FigureReadingError, match="Current document use"):
        reader.invoke_capture(intent, allow_live_calls=False)
    assert calls == [] and not store.records("figure_dispatch_attempt")

    # Restore a fresh permitted page in a separate state so the transport can
    # fail after the durable marker without concealing a source-use rejection.
    fresh = StateStore(tmp_path / "uncertain")
    document2, page2, ocr2 = _granted_page(fresh)
    attempts = []

    def lost(_request, *, timeout):
        attempts.append(1)
        raise TimeoutError("synthetic transport uncertainty")

    lost_reader = _reader(fresh, lost, monkeypatch)
    intent2 = _prepare(lost_reader, document2, page2, ocr2)
    with pytest.raises(FigureDispatchUncertain, match="unknown"):
        lost_reader.invoke_capture(intent2, allow_live_calls=False)
    uncertain_state = lost_reader.operation_state("figure-one", scope=SCOPE)
    assert uncertain_state["intent_ref"] == intent2
    assert uncertain_state["dispatch_ref"] and uncertain_state["response_ref"] is None
    with pytest.raises(FigureDispatchUncertain, match="already"):
        lost_reader.invoke_capture(intent2, allow_live_calls=False)
    assert attempts == [1]


def test_rate_limit_is_saved_without_retrying_same_vision_intent(tmp_path, monkeypatch):
    store = StateStore(tmp_path)
    document, page, ocr = _granted_page(store)
    calls = []

    def limited(_request, *, timeout):
        calls.append(1)
        raise urllib.error.HTTPError(
            "https://api.groq.com/openai/v1/chat/completions", 429, "synthetic limit",
            {"Retry-After": "3", "x-ratelimit-remaining-tokens": "0"}, None,
        )

    reader = _reader(store, limited, monkeypatch)
    intent = _prepare(reader, document, page, ocr)
    outcome = reader.invoke_capture(intent, allow_live_calls=False)
    assert outcome.status == "rate_limited" and outcome.used_tokens is None
    assert outcome.retry_after_seconds == 3
    assert outcome.rate_headers["x-ratelimit-remaining-tokens"] == "0"
    visible = reader.read_result(outcome.response_ref, scope=SCOPE)
    assert visible["usage"] is None
    assert store.get(**outcome.response_ref)["body"]["cache_observation"] == {
        "status": "unavailable",
    }
    assert "output_rate_limit_observation" not in store.get(**outcome.response_ref)["body"]
    assert visible["status"] == "rate_limited"
    assert visible["retry_after_seconds"] == 3
    assert visible["interpretation"] is None
    assert "later explicit figure request" in visible["next_action"]
    replay = store.transact("read-rate-limited-figure", "inspect", lambda tx: read_figure_result(
        tx, outcome.response_ref, scope=SCOPE,
    ))
    assert replay == visible
    assert reader.invoke_capture(intent, allow_live_calls=False) == outcome
    assert calls == [1]


def test_figure_output_minute_observation_is_numeric_only(tmp_path, monkeypatch):
    store = StateStore(tmp_path)
    document, page, ocr = _granted_page(store)
    calls = []
    private_error = {"error": {"type": "tokens", "code": "rate_limit_exceeded",
                 "message": ("Rate limit reached for model `qwen/qwen3.8-27b` "
                             "in organization PRIVATE_ORG on output tokens per minute "
                             "(OTPM): Limit 1000, Used 120, Requested 1024.")}}
    raw = json.dumps(private_error).encode()

    def limited(_request, *, timeout):
        calls.append(1)
        raise urllib.error.HTTPError(
            "https://api.groq.com/openai/v1/chat/completions", 429,
            "synthetic limit", {"Retry-After": "3"}, io.BytesIO(raw),
        )

    reader = _reader(store, limited, monkeypatch)
    intent = _prepare(reader, document, page, ocr)
    outcome = reader.invoke_capture(intent, allow_live_calls=False)
    receipt = store.get(**outcome.response_ref)["body"]
    assert receipt["output_rate_limit_observation"] == {
        "scope": "output_tokens_per_minute", "limit_tokens": 1000,
        "used_tokens": 120, "requested_tokens": 1024,
    }
    assert "PRIVATE_ORG" not in json.dumps(receipt)
    assert receipt["status"] == "rate_limited" and receipt["usage"] is None
    assert reader.invoke_capture(intent, allow_live_calls=False) == outcome
    assert calls == [1]


def test_figure_usage_must_reconcile_and_length_finish_is_visible(tmp_path, monkeypatch):
    store = StateStore(tmp_path)
    document, page, ocr = _granted_page(store)
    calls = []

    def transport(_request, *, timeout):
        calls.append(1)
        return Reply({
            "model": "qwen/qwen3.8-27b",
            "choices": [{"message": {"content": "Visible label"},
                         "finish_reason": "length"}],
            "usage": {"prompt_tokens": 2048, "completion_tokens": 10,
                      "total_tokens": 2058 if len(calls) == 2 else 2059},
        })

    reader = _reader(store, transport, monkeypatch)
    bad = _prepare(reader, document, page, ocr, operation="figure-bad-usage")
    with pytest.raises(FigureDispatchUncertain, match="could not be verified"):
        reader.invoke_capture(bad, allow_live_calls=False)
    assert reader.operation_state("figure-bad-usage", scope=SCOPE)["dispatch_ref"]
    assert reader.operation_state("figure-bad-usage", scope=SCOPE)["response_ref"] is None
    with pytest.raises(FigureDispatchUncertain, match="already"):
        reader.invoke_capture(bad, allow_live_calls=False)
    good = _prepare(reader, document, page, ocr, operation="figure-length")
    outcome = reader.invoke_capture(good, allow_live_calls=False)
    visible = reader.read_result(outcome.response_ref, scope=SCOPE)
    assert visible["usage"] == {
        "prompt_tokens": 2048, "completion_tokens": 10, "total_tokens": 2058,
    }
    assert store.get(**outcome.response_ref)["body"]["cache_observation"] == {
        "status": "not_reported", "field_presence": "details_absent",
    }
    assert visible["finish_reason"] == "length"
    assert visible["output_truncated"] is True
    assert visible["evidence_status"] == "generated_unverified"
    assert calls == [1, 1]


def test_figure_receipt_marks_malformed_optional_cache_without_losing_totals(tmp_path, monkeypatch):
    store = StateStore(tmp_path)
    document, page, ocr = _granted_page(store)

    def transport(_request, *, timeout):
        return Reply({
            "model": "qwen/qwen3.8-27b",
            "choices": [{"message": {"content": "Synthetic label"},
                         "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 2048, "completion_tokens": 10,
                      "total_tokens": 2058,
                      "prompt_tokens_details": {"cached_tokens": "unknown"}},
        })

    reader = _reader(store, transport, monkeypatch)
    intent = _prepare(reader, document, page, ocr, operation="figure-malformed-cache")
    outcome = reader.invoke_capture(intent, allow_live_calls=False)
    receipt = store.get(**outcome.response_ref)["body"]
    assert receipt["status"] == "generated_unverified"
    assert receipt["cache_observation"] == {
        "status": "malformed", "field_presence": "cached_tokens_invalid",
    }
    assert receipt["usage"] == {
        "prompt_tokens": 2048, "completion_tokens": 10, "total_tokens": 2058,
    }


def test_selected_page_ocr_change_blocks_vision_before_http(tmp_path, monkeypatch):
    store = StateStore(tmp_path)
    document, page, ocr = _granted_page(store)
    calls = []
    reader = _reader(store, lambda *_args, **_kwargs: calls.append(1), monkeypatch)
    intent = _prepare(reader, document, page, ocr)
    saved_ocr = store.get(**ocr)
    store.transact("restrict-selected-page-ocr", {}, lambda tx: tx.put(
        "public_document_page_ocr", saved_ocr["id"], {
            **saved_ocr["body"], "status": "ocr_restricted",
            "explicit_use_prohibition_detected": True,
        }, expected_version=saved_ocr["version"]), mission_id=SCOPE["mission_id"])
    with pytest.raises(FigureReadingError, match="Selected-page OCR"):
        reader.invoke_capture(intent, allow_live_calls=False)
    assert calls == [] and not store.records("figure_dispatch_attempt")
