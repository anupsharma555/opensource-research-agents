"""Restart and provider-failure behavior for the optional BHEB search route."""

from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from ora.retrieval.bheb import MAX_BODY_BYTES, BhebCorpusService, _digest
from ora.retrieval.providers import HTTPDispatchBlocked, HTTPResult
from ora.state import StateStore

SCOPE = {"mission_id": "mission", "run_id": "run", "actor_id": "actor", "task_id": "task"}


def _fixture(tmp_path: Path, monkeypatch) -> tuple[Path, dict]:
    attrs = {"source_id": "synthetic-1", "source_version": "v1", "access_posture": "abstract_only"}
    file = {
        "file_id": "file-synthetic",
        "vector_store_file_id": "vs-file-synthetic",
        "source_id": "synthetic-1",
        "status": "completed",
        "filename": "synthetic.md",
        "upload_checksum_sha256": "a" * 64,
        "size_bytes": 100,
        "attributes": attrs,
        "attributes_checksum_sha256": _digest(attrs),
        "chunking_strategy": {},
    }
    receipt = {
        "status": "completed",
        "manifest_parity_verified": True,
        "release_id": "synthetic-release",
        "release_manifest_checksum_sha256": "b" * 64,
        "vector_store_id": "vs_synthetic",
        "file_count": 1,
        "files": [file],
    }
    receipt_path = tmp_path / "receipt.json"
    receipt_path.write_text(json.dumps({**receipt, "receipt_checksum_sha256": _digest(receipt)}))
    config_path = tmp_path / "config.json"
    config_path.write_text(
        json.dumps(
            {
                "publication_receipt_path": str(receipt_path),
                "vector_store_id": "vs_synthetic",
                "release_id": "synthetic-release",
                "release_manifest_checksum_sha256": "b" * 64,
            }
        )
    )
    monkeypatch.setenv("OSRA_OPENAI_API_KEY", "synthetic-test-token")
    return config_path, attrs


def _success(attrs: dict) -> HTTPResult:
    return HTTPResult(
        200,
        json.dumps(
            {
                "data": [
                    {
                        "file_id": "file-synthetic",
                        "filename": "synthetic.md",
                        "attributes": attrs,
                        "score": 0.5,
                        "content": [{"type": "text", "text": "Synthetic candidate excerpt."}],
                    }
                ],
                "has_more": False,
                "next_page": None,
            }
        ).encode(),
        {},
    )


def _service(config: Path, root: Path, transport, *, clock=time.time, fault=None):
    return BhebCorpusService(config, StateStore(root, clock=clock, fault=fault), transport)


def test_completed_and_reused_results_survive_fresh_store_and_service(tmp_path, monkeypatch):
    config, attrs = _fixture(tmp_path, monkeypatch)
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        return _success(attrs)

    root = tmp_path / "state"
    first = _service(config, root, transport).search(SCOPE, "first", "same query")
    assert first["status"] == "completed" and len(first["hits"]) == 1
    restarted = _service(config, root, transport)
    assert restarted.search(SCOPE, "first", "same query")["status"] == "completed"
    reused = restarted.search(SCOPE, "second", "same query")
    assert reused["status"] == "completed" and reused["reuse_status"] == "reused"
    assert reused["hits"] == first["hits"] and len(calls) == 1
    assert (
        restarted.inspect(SCOPE, reused["hits"][0]["hit_id"], "second")["status"] == "metadata_only"
    )


def test_interrupted_dispatch_is_uncertain_after_restart_until_explicit_refresh(
    tmp_path, monkeypatch
):
    config, attrs = _fixture(tmp_path, monkeypatch)
    root = tmp_path / "state"
    calls = []

    def interrupted(*args, **kwargs):
        calls.append(1)
        raise KeyboardInterrupt("simulated process interruption after dispatch")

    with pytest.raises(KeyboardInterrupt):
        _service(config, root, interrupted).search(SCOPE, "interrupted", "question")
    restarted = _service(config, root, lambda *a, **k: calls.append(1) or _success(attrs))
    assert restarted.search(SCOPE, "interrupted", "question")["status"] == "uncertain"
    assert restarted.search(SCOPE, "alias", "question")["status"] == "uncertain"
    assert len(calls) == 1
    refreshed = restarted.search(SCOPE, "refresh", "question", refresh=True)
    assert refreshed["status"] == "completed" and len(calls) == 2


def test_result_persistence_failure_keeps_dispatch_uncertain_across_restart(tmp_path, monkeypatch):
    config, attrs = _fixture(tmp_path, monkeypatch)
    root = tmp_path / "state"
    commits = []

    def fail_result(point):
        if point == "before_metadata_commit":
            commits.append(1)
            if len(commits) == 2:
                raise OSError("simulated result commit failure")

    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        return _success(attrs)

    first = _service(config, root, transport, fault=fail_result).search(
        SCOPE, "failed-save", "question"
    )
    assert first["status"] == "uncertain" and first["reason"] == "result_persistence_uncertain"
    restarted = _service(config, root, transport)
    assert restarted.search(SCOPE, "failed-save", "question")["status"] == "uncertain"
    assert restarted.search(SCOPE, "alias", "question")["status"] == "uncertain"
    assert len(calls) == 1
    assert restarted.search(SCOPE, "explicit", "question", refresh=True)["status"] == "completed"
    assert len(calls) == 2


def test_rate_wait_and_retry_after_restart(tmp_path, monkeypatch):
    config, attrs = _fixture(tmp_path, monkeypatch)
    now = [time.time()]
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        return HTTPResult(429, b"{}", {"Retry-After": "30"}) if len(calls) == 1 else _success(attrs)

    root = tmp_path / "state"
    assert (
        _service(config, root, transport, clock=lambda: now[0]).search(SCOPE, "first", "question")[
            "status"
        ]
        == "waiting"
    )
    restarted = _service(config, root, transport, clock=lambda: now[0])
    assert restarted.search(SCOPE, "alias", "question")["status"] == "waiting"
    assert len(calls) == 1
    now[0] += 31
    assert restarted.search(SCOPE, "after-reset", "question")["status"] == "completed"
    assert len(calls) == 2


@pytest.mark.parametrize("retry_after", [7200, 86400])
def test_long_numeric_rate_wait_survives_restart_without_shortening(
    tmp_path, monkeypatch, retry_after
):
    config, attrs = _fixture(tmp_path, monkeypatch)
    now = [time.time()]
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        return (
            HTTPResult(429, b"{}", {"Retry-After": str(retry_after)})
            if len(calls) == 1
            else _success(attrs)
        )

    root = tmp_path / "state"
    first = _service(config, root, transport, clock=lambda: now[0]).search(
        SCOPE, "long-first", "question"
    )
    assert first["status"] == "waiting"
    assert first["diagnostics"]["retry_after_seconds"] == retry_after
    restarted = _service(config, root, transport, clock=lambda: now[0])
    now[0] += retry_after - 1
    assert restarted.search(SCOPE, "still-waiting", "question")["status"] == "waiting"
    assert restarted.search(SCOPE, "different-query", "other question")["status"] == "waiting"
    assert len(calls) == 1
    now[0] += 2
    assert restarted.search(SCOPE, "after-full-wait", "question")["status"] == "completed"
    assert len(calls) == 2


def test_extreme_numeric_rate_wait_fails_closed_without_overflow(tmp_path, monkeypatch):
    config, _ = _fixture(tmp_path, monkeypatch)
    now = [time.time()]
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        return HTTPResult(429, b"{}", {"Retry-After": "9" * 1000})

    root = tmp_path / "state"
    first = _service(config, root, transport, clock=lambda: now[0]).search(
        SCOPE, "huge-first", "question"
    )
    assert first["status"] == "waiting" and first["diagnostics"]["wait_indefinite"] is True
    restarted = _service(config, root, transport, clock=lambda: now[0])
    now[0] += 10**9
    later = restarted.search(SCOPE, "huge-later", "different question")
    assert later["status"] == "waiting" and later["wait_indefinite"] is True
    assert len(calls) == 1


def test_predispatch_guard_records_zero_attempt_and_replays_after_restart(tmp_path, monkeypatch):
    config, attrs = _fixture(tmp_path, monkeypatch)
    root = tmp_path / "state"
    calls = []

    def blocked(*args, **kwargs):
        calls.append(1)
        raise HTTPDispatchBlocked("retrieval_attempt_cap")

    first = _service(config, root, blocked).search(SCOPE, "blocked", "question")
    assert first["status"] == "not_dispatched" and first["reason"] == "retrieval_attempt_cap"
    assert first["diagnostics"]["attempt_count"] == 0
    restarted = _service(config, root, lambda *a, **k: calls.append(1) or _success(attrs))
    assert restarted.search(SCOPE, "blocked", "question")["status"] == "not_dispatched"
    assert restarted.search(SCOPE, "alias", "question")["status"] == "not_dispatched"
    assert len(calls) == 1
    assert (
        restarted.search(SCOPE, "authorized-refresh", "question", refresh=True)["status"]
        == "completed"
    )
    assert len(calls) == 2


@pytest.mark.parametrize("status", [401, 404])
def test_known_http_failure_is_durable_without_retry(tmp_path, monkeypatch, status):
    config, _ = _fixture(tmp_path, monkeypatch)
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        return HTTPResult(status, b'{"error":"synthetic"}', {})

    root = tmp_path / "state"
    first = _service(config, root, transport).search(SCOPE, "known-error", "question")
    assert first["status"] == "failed" and first["diagnostics"]["http_status"] == status
    assert first["reason"] == ("authentication_failed" if status == 401 else "store_unavailable")
    assert (
        _service(config, root, transport).search(SCOPE, "same-query", "question")["status"]
        == "failed"
    )
    assert len(calls) == 1


@pytest.mark.parametrize(
    "failure", ["timeout", "transport", "provider_503", "oversized", "malformed"]
)
def test_unknown_provider_or_response_failure_is_bounded_and_not_replayed(
    tmp_path, monkeypatch, failure
):
    config, _ = _fixture(tmp_path, monkeypatch)
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        if failure == "timeout":
            raise TimeoutError("synthetic timeout")
        if failure == "transport":
            raise ConnectionError("synthetic connection failure")
        if failure == "provider_503":
            return HTTPResult(503, b'{"error":"unavailable"}', {})
        if failure == "oversized":
            return HTTPResult(200, b"x" * (MAX_BODY_BYTES + 1), {})
        return HTTPResult(200, b'{"data": "invalid"}', {})

    root = tmp_path / "state"
    first = _service(config, root, transport).search(SCOPE, "uncertain", "question")
    assert first["status"] == "uncertain" and first["hits"] == []
    assert (
        first["reason"]
        == {
            "timeout": "provider_timeout",
            "transport": "transport_outcome_unknown",
            "provider_503": "provider_unavailable",
            "oversized": "oversized_response",
            "malformed": "malformed_response",
        }[failure]
    )
    assert (
        _service(config, root, transport).search(SCOPE, "alias", "question")["status"]
        == "uncertain"
    )
    assert len(calls) == 1
