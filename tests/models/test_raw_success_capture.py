"""Opted-in diagnostics retain exact successful HTTP bytes without exporting them."""

from __future__ import annotations

import hashlib
import io
import json
import urllib.error
from dataclasses import replace

import pytest

from ora.live.provider import MAX_RESPONSE_BYTES
from ora.models import ModelSession, SessionError
from ora.observability.export import EXPORT_KIND, project_run
from tests.models.test_session import ROOT, URL, response, select, setup

RAW_ONLY = "PRIVATE_RAW_RESPONSE_MARKER"


def _raw_response() -> bytes:
    payload = json.loads(response())
    payload["raw_only_marker"] = RAW_ONLY
    return json.dumps(payload, separators=(",", ":")).encode()


def _prepared(tmp_path, *, raw: bytes | None = None, opted_in: bool = False):
    store, _registry, bridge, call, _config, copy_ref, captured = setup(
        tmp_path, raw=raw,
    )
    session = ModelSession(
        store, ROOT, adapter=bridge.adapter, capture_raw_success=opted_in,
    )
    prepared = session.prepare(
        call_id=call["id"], call_version=1, phase="collect",
        messages=[{"role": "user", "content": "Review synthetic evidence"}],
        sources=select(copy_ref),
    )
    return store, session, prepared, captured


def test_opt_in_saves_exact_raw_bytes_and_preserves_normalized_output(tmp_path):
    raw = _raw_response()
    store, session, prepared, sent = _prepared(tmp_path / "opted", raw=raw, opted_in=True)
    receipt_ref = session.invoke_capture(prepared.intent_ref)
    receipt = store.get(**receipt_ref)
    body = receipt["body"]
    assert body["status"] == "tool_requested"
    assert body["raw_success_capture_requested"] is True
    restricted = store.get(
        "restricted_provider_response_capture",
        f"provider-response-capture:{prepared.intent_ref['id']}", 1,
    )
    assert restricted["body"]["status"] == "captured"
    assert restricted["body"]["access"] == "restricted_diagnostics"
    assert restricted["body"]["origin"] == "provider_http_success"
    assert restricted["body"]["intent_ref"] == prepared.intent_ref
    assert restricted["body"]["receipt_ref"] == receipt_ref
    assert restricted["body"]["request_sha256"] == prepared.request.request_sha256
    assert restricted["body"]["response_sha256"] == body["response_sha256"]
    assert restricted["body"]["response_bytes"] == len(raw)
    assert restricted["body"]["response_sha256"] == hashlib.sha256(raw).hexdigest()
    assert store.read_blob(restricted["body"]["blob"]["sha256"]) == raw
    normalized = store.read_blob(body["blob"]["sha256"])
    assert RAW_ONLY.encode() not in normalized
    assert RAW_ONLY not in json.dumps(body)
    assert body["usage"]["total_tokens"] == 30
    assert session.invoke_capture(prepared.intent_ref) == receipt_ref
    assert len(sent) == 1
    assert len(store.records("restricted_provider_response_capture")) == 1

    default_store, default_session, default_prepared, _ = _prepared(
        tmp_path / "default", raw=raw,
    )
    default_ref = default_session.invoke_capture(default_prepared.intent_ref)
    default_receipt = default_store.get(**default_ref)["body"]
    assert "raw_success_capture_requested" not in default_receipt
    assert default_receipt.get("raw_success_capture_requested", False) is False
    assert default_store.records("restricted_provider_response_capture") == []
    assert default_store.read_blob(default_receipt["blob"]["sha256"]) == normalized
    assert default_receipt["usage"] == body["usage"]
    opted_afterward = ModelSession(
        default_store, ROOT, adapter=default_session.adapter, capture_raw_success=True,
    )
    assert opted_afterward.invoke_capture(default_prepared.intent_ref) == default_ref
    assert default_store.records("restricted_provider_response_capture") == []

    store.transact("seed-raw-capture-export-run", {}, lambda tx: tx.put(
        "mission_run", "run", {"mission_id": "mission", "run_id": "run", "status": "active"},
    ), mission_id="mission")
    assert project_run(store, "mission", run_id="run")["created"] >= 1
    exported = json.dumps([item["body"] for item in store.records(EXPORT_KIND)])
    assert RAW_ONLY not in exported
    assert "restricted_provider_response_capture" not in exported
    assert restricted["id"] not in exported


def test_raw_capture_requires_opt_in_and_exact_hash(tmp_path):
    raw = _raw_response()
    store, session, prepared, _ = _prepared(tmp_path, raw=raw, opted_in=True)
    outcome = session.adapter.decode(
        prepared.request, json.loads(raw),
        response_sha256=hashlib.sha256(raw).hexdigest(),
    )
    with pytest.raises(SessionError, match="Raw provider response"):
        session.capture(prepared.intent_ref, outcome, raw_success_response=b"wrong")
    with pytest.raises(SessionError, match="Raw provider response"):
        session.capture(prepared.intent_ref, replace(outcome, http_status=401),
                        raw_success_response=raw)
    oversized = b"x" * (MAX_RESPONSE_BYTES + 1)
    with pytest.raises(SessionError, match="Raw provider response"):
        session.capture(prepared.intent_ref, replace(
            outcome, response_sha256=hashlib.sha256(oversized).hexdigest(),
        ), raw_success_response=oversized)
    assert store.records("restricted_provider_response_capture") == []
    receipt_ref = session.capture(prepared.intent_ref, outcome, raw_success_response=raw)
    assert session.capture(prepared.intent_ref, outcome,
                           raw_success_response=raw) == receipt_ref
    assert len(store.records("restricted_provider_response_capture")) == 1

    absent_store, absent_session, absent_prepared, _ = _prepared(
        tmp_path / "absent", raw=raw, opted_in=True,
    )
    absent_outcome = absent_session.adapter.decode(absent_prepared.request, json.loads(raw))
    receipt_ref = absent_session.capture(absent_prepared.intent_ref, absent_outcome,
                                         raw_success_response=None)
    receipt = absent_store.get(**receipt_ref)
    assert receipt["body"]["raw_success_capture_requested"] is True
    absent = absent_store.get(
        "restricted_provider_response_capture",
        f"provider-response-capture:{absent_prepared.intent_ref['id']}", 1,
    )
    assert absent["body"]["status"] == "unavailable"
    assert absent["body"]["blob"] is None

    off_store, off_session, off_prepared, _ = _prepared(
        tmp_path / "off", raw=raw,
    )
    off_outcome = off_session.adapter.decode(off_prepared.request, json.loads(raw))
    with pytest.raises(SessionError, match="Raw provider response"):
        off_session.capture(off_prepared.intent_ref, off_outcome,
                            raw_success_response=raw)
    assert off_store.records("model_response_receipt") == []


def test_http_error_has_no_raw_success_blob(tmp_path):
    raw = json.dumps({"error": {"type": "authentication_error",
                                "message": "Synthetic authorization failure"}}).encode()
    store, session, prepared, _ = _prepared(tmp_path, opted_in=True)

    def rejected(_request, timeout):
        raise urllib.error.HTTPError(URL, 401, "denied", {}, io.BytesIO(raw))

    session.adapter.transport = rejected
    receipt = store.get(**session.invoke_capture(prepared.intent_ref))["body"]
    assert receipt["status"] == "failed"
    assert receipt["http_status"] == 401
    assert receipt["raw_success_capture_requested"] is True
    absent = store.get(
        "restricted_provider_response_capture",
        f"provider-response-capture:{prepared.intent_ref['id']}", 1,
    )
    assert absent["body"]["status"] == "unavailable"
    assert absent["body"]["blob"] is None


@pytest.mark.parametrize("failure", ["blob", "status_record"])
def test_optional_storage_failure_keeps_known_normalized_result(
    tmp_path, monkeypatch, caplog, failure,
):
    raw = _raw_response()
    store, session, prepared, sent = _prepared(tmp_path, raw=raw, opted_in=True)
    if failure == "blob":
        original_put = store.put_blob

        def fail_raw_blob(content):
            if content == raw:
                raise OSError("synthetic private blob failure")
            return original_put(content)

        monkeypatch.setattr(store, "put_blob", fail_raw_blob)
    else:
        original_transact = store.transact

        def fail_capture_record(operation_id, *args, **kwargs):
            if operation_id.startswith("raw-response-capture:"):
                raise OSError("synthetic private record failure")
            return original_transact(operation_id, *args, **kwargs)

        monkeypatch.setattr(store, "transact", fail_capture_record)

    receipt_ref = session.invoke_capture(prepared.intent_ref)
    receipt = store.get(**receipt_ref)["body"]
    assert receipt["status"] == "tool_requested"
    assert receipt["usage"]["total_tokens"] == 30
    assert receipt["raw_success_capture_requested"] is True
    assert RAW_ONLY.encode() not in store.read_blob(receipt["blob"]["sha256"])
    marker = store.get("model_dispatch_attempt",
                       f"model-dispatch:{prepared.intent_ref['id']}")
    assert marker["body"]["status"] == "captured"
    assert marker["body"]["receipt_ref"] == receipt_ref
    capture = store.get(
        "restricted_provider_response_capture",
        f"provider-response-capture:{prepared.intent_ref['id']}", 1,
    )
    if failure == "blob":
        assert capture["body"]["status"] == "storage_failed"
        assert capture["body"]["diagnostic_code"] == "raw_blob_write_failed"
        assert capture["body"]["blob"] is None
    else:
        assert capture is None
    assert session.invoke_capture(prepared.intent_ref) == receipt_ref
    reopened = ModelSession(store, ROOT, adapter=session.adapter, capture_raw_success=True)
    assert reopened.invoke_capture(prepared.intent_ref) == receipt_ref
    assert len(sent) == 1
    assert RAW_ONLY not in caplog.text

    store.transact("seed-raw-failure-export-run", {}, lambda tx: tx.put(
        "mission_run", "run", {"mission_id": "mission", "run_id": "run", "status": "active"},
    ), mission_id="mission")
    project_run(store, "mission", run_id="run")
    exported = json.dumps([item["body"] for item in store.records(EXPORT_KIND)])
    assert RAW_ONLY not in exported
    assert "restricted_provider_response_capture" not in exported
