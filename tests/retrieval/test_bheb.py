"""Offline checks for receipt-pinned BHEB leads and explicit source use."""

import hashlib
import json
import threading
import time
from pathlib import Path

import pytest

from ora.retrieval.bheb import BhebCorpusService, _digest
from ora.retrieval.providers import HTTPResult
from ora.state import StateStore

SCOPE = {"mission_id": "mission-1", "run_id": "run-1", "actor_id": "actor-1", "task_id": "task-1"}
MATCHED = "The fictional intervention had a synthetic near-end outcome."


def _write(path: Path, data: dict) -> None:
    path.write_text(json.dumps(data), encoding="utf-8")


def _fixture(
    tmp_path: Path,
    monkeypatch,
    *,
    source: bool = False,
    content_mode: str = "existing_abstract",
    full_text: bool = False,
):
    attrs = {
        "source_id": "synthetic-1",
        "source_version": "v1",
        "access_posture": "open_full_text" if full_text else "abstract_only",
        "source_type": "journal_article",
        "evidence_axis": "behavioral_health",
    }
    text = (
        "Synthetic self-authored introduction. " + ("Background filler. " * 120) + MATCHED + " End."
    )
    packet = (
        "# Synthetic evidence\n\n"
        "Source ID: synthetic-1\n"
        f"Content mode: {content_mode}\n\n"
        "## Abstract or reviewed summary\n\n"
        + text
        + "\n\nApplicability: Use only for the labeled evidence roles.\n"
    )
    if full_text:
        packet = (
            "# Synthetic full text\n\n## Methods\n\n"
            + text
            + "\n\n## References\n\nSynthetic reference.\n"
        )
    raw = packet.encode()
    source_hash = hashlib.sha256(raw).hexdigest()
    release = tmp_path / "release"
    release.mkdir()
    (release / "synthetic.md").write_bytes(raw)
    entry = {
        "source": {
            "source_id": "synthetic-1",
            "version": "v1",
            "title": "Synthetic evidence",
            "abstract": text,
            "authored_summary": "A separate summary that must never replace the abstract.",
            "access_posture": "open_full_text" if full_text else "abstract_only",
            "canonical_url": "https://example.org/synthetic",
            "retrieval_eligible": True,
            "lifecycle_state": "approved",
        },
        "upload_path": "synthetic.md",
        "upload_size_bytes": len(raw),
        "upload_checksum_sha256": source_hash,
        "upload_attributes": attrs,
        "retrieval_mode": "approved_full_text" if full_text else "abstract_packet",
        "content_mode": "approved_full_text_with_abstract_identity" if full_text else content_mode,
    }
    manifest = {"release_id": "synthetic-release", "status": "approved", "sources": [entry]}
    manifest_hash = _digest(manifest)
    _write(
        release / "release-manifest.json", {**manifest, "manifest_checksum_sha256": manifest_hash}
    )
    file = {
        "file_id": "file-synthetic",
        "vector_store_file_id": "vs-file-synthetic",
        "source_id": "synthetic-1",
        "status": "completed",
        "filename": "synthetic.md",
        "upload_checksum_sha256": source_hash,
        "size_bytes": len(raw),
        "attributes": attrs,
        "attributes_checksum_sha256": _digest(attrs),
        "chunking_strategy": {},
    }
    receipt = {
        "status": "completed",
        "manifest_parity_verified": True,
        "release_id": "synthetic-release",
        "release_manifest_checksum_sha256": manifest_hash,
        "vector_store_id": "vs_synthetic",
        "file_count": 1,
        "files": [file],
    }
    receipt_hash = _digest(receipt)
    receipt_path = tmp_path / "receipt.json"
    _write(receipt_path, {**receipt, "receipt_checksum_sha256": receipt_hash})
    config = {
        "publication_receipt_path": str(receipt_path),
        "vector_store_id": "vs_synthetic",
        "release_id": "synthetic-release",
        "release_manifest_checksum_sha256": manifest_hash,
    }
    if source:
        policy_path = tmp_path / "policy.json"
        _write(
            policy_path,
            {
                "release_id": "synthetic-release",
                "release_manifest_checksum_sha256": manifest_hash,
                "publication_receipt_checksum_sha256": receipt_hash,
                "revoked": False,
                "expires_at": "2099-01-01T00:00:00Z",
                "sources": {
                    "synthetic-1": {
                        "allow_storage": True,
                        "allow_model_input": True,
                        "revoked": False,
                        "source_version": "v1",
                        "upload_checksum_sha256": source_hash,
                        "rights_basis": "self-authored test fixture",
                        "declared_surface": "passage" if full_text else "abstract",
                    }
                },
            },
        )
        config.update(
            source_manifest_path=str(release / "release-manifest.json"),
            source_use_policy_path=str(policy_path),
        )
    config_path = tmp_path / "config.json"
    _write(config_path, config)
    monkeypatch.setenv("OSRA_OPENAI_API_KEY", "synthetic-test-key")
    return config_path, attrs, text


def _response(attrs, *, file_id="file-synthetic", status=200):
    body = {
        "data": [
            {
                "file_id": file_id,
                "filename": "synthetic.md",
                "attributes": attrs,
                "score": 0.7,
                "content": [{"type": "text", "text": MATCHED}],
            }
        ]
    }
    return HTTPResult(status, json.dumps(body).encode(), {})


def test_search_binds_receipt_and_replays_without_second_call(tmp_path, monkeypatch):
    config, attrs, _ = _fixture(tmp_path, monkeypatch)
    calls = []

    def transport(method, url, **kwargs):
        calls.append((method, url, kwargs["body"]))
        return _response(attrs)

    service = BhebCorpusService(config, StateStore(tmp_path / "state"), transport)
    first = service.search(
        SCOPE, "op-1", "fictional outcome", {"evidence_axis": "behavioral_health"}
    )
    assert first["status"] == "completed" and len(first["hits"]) == 1
    assert "excerpt" not in first["hits"][0]
    saved_hit = service.store.get("bheb_search_hit", first["hits"][0]["hit_id"])
    assert "excerpt" not in saved_hit["body"]
    assert calls[0][0] == "POST" and calls[0][2]["rewrite_query"] is False
    assert calls[0][2]["filters"] == {
        "type": "eq",
        "key": "evidence_axis",
        "value": "behavioral_health",
    }
    assert (
        service.search(SCOPE, "op-1", "fictional outcome", {"evidence_axis": "behavioral_health"})
        == first
    )
    assert len(calls) == 1
    with pytest.raises(ValueError, match="different inputs"):
        service.search(SCOPE, "op-1", "a different question")
    assert service.inspect(SCOPE, first["hits"][0]["hit_id"], "op-1")["status"] == "metadata_only"
    assert service.summary()["reason"] == "manifest_unavailable"


def test_unmatched_provider_file_cannot_become_a_lead(tmp_path, monkeypatch):
    config, attrs, _ = _fixture(tmp_path, monkeypatch)
    service = BhebCorpusService(
        config,
        StateStore(tmp_path / "state"),
        lambda *a, **k: _response(attrs, file_id="file-unknown"),
    )
    assert service.search(SCOPE, "op-2", "question")["hits"] == []


def test_uncertain_attempt_is_durable_and_never_replayed(tmp_path, monkeypatch):
    config, _, _ = _fixture(tmp_path, monkeypatch)
    calls = []

    def failing(*args, **kwargs):
        calls.append(1)
        raise TimeoutError("unknown outcome")

    service = BhebCorpusService(config, StateStore(tmp_path / "state"), failing)
    assert service.search(SCOPE, "op-3", "question")["status"] == "uncertain"
    assert service.search(SCOPE, "op-3", "question")["status"] == "uncertain"
    assert len(calls) == 1


def test_missing_config_or_key_is_harmless(tmp_path, monkeypatch):
    service = BhebCorpusService(tmp_path / "absent.json", StateStore(tmp_path / "state"))
    assert service.search(SCOPE, "op-4", "question")["status"] == "unavailable"
    config, _, _ = _fixture(tmp_path, monkeypatch)
    monkeypatch.delenv("OSRA_OPENAI_API_KEY")
    service = BhebCorpusService(config, StateStore(tmp_path / "state2"))
    assert service.summary()["enabled"] is False
    assert service.search(SCOPE, "op-4", "question")["reason"] == "credential_unavailable"


def test_store_id_cannot_change_fixed_transport_endpoint(tmp_path, monkeypatch):
    config, _, _ = _fixture(tmp_path, monkeypatch)
    data = json.loads(config.read_text())
    data["vector_store_id"] = "vs_synthetic/../../other"
    _write(config, data)
    calls = []
    service = BhebCorpusService(
        config, StateStore(tmp_path / "state"), lambda *a, **k: calls.append(1)
    )
    assert service.search(SCOPE, "invalid-store", "question")["reason"] == "invalid_config"
    assert not calls


def test_checked_synthetic_source_requires_exact_body_and_explicit_policy(tmp_path, monkeypatch):
    config, attrs, text = _fixture(tmp_path, monkeypatch, source=True)
    service = BhebCorpusService(
        config, StateStore(tmp_path / "state"), lambda *a, **k: _response(attrs)
    )
    hit_id = service.search(SCOPE, "op-5", "question")["hits"][0]["hit_id"]
    checked = service.inspect(SCOPE, hit_id, "op-5")
    assert checked["status"] == "source_available"
    assert checked["source_text"] == text and checked["rights_status"] == "permitted"
    assert checked["declared_surface"] == "abstract"
    assert checked["source_sha256"] == hashlib.sha256(text.encode()).hexdigest()
    assert checked["upload_sha256"] != checked["source_sha256"]
    assert checked["matched_text"] == MATCHED and checked["match_offset"] > 1900
    assert service.current_source_use("synthetic-1", "v1", checked["upload_sha256"])
    assert (
        service.inspect({**SCOPE, "actor_id": "other"}, hit_id, "op-5")["status"]
        == "source_unavailable"
    )
    source_path = tmp_path / "release" / "synthetic.md"
    source_path.write_text(text + " changed", encoding="utf-8")
    mismatch = service.inspect(SCOPE, hit_id, "op-5")
    assert mismatch["status"] == "metadata_only" and mismatch["reason"] == "body_hash_mismatch"
    assert not service.current_source_use("synthetic-1", "v1", checked["upload_sha256"])


def test_revocation_stops_source_inspection_and_reuse(tmp_path, monkeypatch):
    config, attrs, _ = _fixture(tmp_path, monkeypatch, source=True)
    service = BhebCorpusService(
        config, StateStore(tmp_path / "state"), lambda *a, **k: _response(attrs)
    )
    hit_id = service.search(SCOPE, "op-6", "question")["hits"][0]["hit_id"]
    checked = service.inspect(SCOPE, hit_id, "op-6")
    assert checked["status"] == "source_available"
    policy_path = tmp_path / "policy.json"
    policy = json.loads(policy_path.read_text())
    policy["revoked"] = True
    _write(policy_path, policy)
    revoked = service.inspect(SCOPE, hit_id, "op-6")
    assert revoked["status"] == "metadata_only" and revoked["reason"] == "policy_revoked"
    assert not service.current_source_use("synthetic-1", "v1", checked["upload_sha256"])


def test_verified_file_does_not_make_unmatched_excerpt_a_source_passage(tmp_path, monkeypatch):
    config, attrs, _ = _fixture(tmp_path, monkeypatch, source=True)
    response = _response(attrs)
    payload = json.loads(response.body)
    payload["data"][0]["content"][0]["text"] = (
        "Provider transformed text absent from the checked file."
    )
    service = BhebCorpusService(
        config,
        StateStore(tmp_path / "state"),
        lambda *a, **k: HTTPResult(200, json.dumps(payload).encode(), {}),
    )
    hit_id = service.search(SCOPE, "unmatched", "question")["hits"][0]["hit_id"]
    inspected = service.inspect(SCOPE, hit_id, "unmatched")
    assert inspected["status"] == "source_available"
    assert inspected["matched_text"] is None and inspected["match_offset"] is None

    payload["data"][0]["content"][0]["text"] = "Content mode: existing_abstract"
    metadata_service = BhebCorpusService(
        config,
        StateStore(tmp_path / "metadata_state"),
        lambda *a, **k: HTTPResult(200, json.dumps(payload).encode(), {}),
    )
    metadata_hit = metadata_service.search(SCOPE, "metadata-hit", "question")["hits"][0]["hit_id"]
    metadata_inspected = metadata_service.inspect(SCOPE, metadata_hit, "metadata-hit")
    assert metadata_inspected["status"] == "source_available"
    assert metadata_inspected["matched_text"] is None


def test_summary_packet_cannot_be_substituted_for_original_abstract(tmp_path, monkeypatch):
    config, attrs, _ = _fixture(
        tmp_path, monkeypatch, source=True, content_mode="executive_summary"
    )
    service = BhebCorpusService(
        config, StateStore(tmp_path / "state"), lambda *a, **k: _response(attrs)
    )
    hit_id = service.search(SCOPE, "summary", "question")["hits"][0]["hit_id"]
    assert service.inspect(SCOPE, hit_id, "summary")["status"] == "metadata_only"
    assert "excerpt" not in service.store.get("bheb_search_hit", hit_id)["body"]


def test_approved_full_text_markdown_requires_exact_upload_and_permission(tmp_path, monkeypatch):
    config, attrs, _ = _fixture(tmp_path, monkeypatch, source=True, full_text=True)
    service = BhebCorpusService(
        config, StateStore(tmp_path / "state"), lambda *a, **k: _response(attrs)
    )
    hit_id = service.search(SCOPE, "fulltext", "question")["hits"][0]["hit_id"]
    checked = service.inspect(SCOPE, hit_id, "fulltext")
    assert checked["status"] == "source_available"
    assert checked["declared_surface"] == "passage"
    assert checked["source_text"].startswith("# Synthetic full text")
    assert checked["upload_sha256"] == checked["source_sha256"]
    assert checked["matched_text"] == MATCHED and checked["match_offset"] > 1900


def test_concurrent_same_operation_dispatches_once(tmp_path, monkeypatch):
    config, attrs, _ = _fixture(tmp_path, monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def slow(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return _response(attrs)

    service = BhebCorpusService(config, StateStore(tmp_path / "state"), slow)
    results = []
    thread = threading.Thread(
        target=lambda: results.append(service.search(SCOPE, "same", "question"))
    )
    thread.start()
    assert entered.wait(2)
    second = service.search(SCOPE, "same", "question")
    release.set()
    thread.join(timeout=2)
    assert len(calls) == 1
    assert second["status"] == "uncertain"  # A saved dispatch is not a saved response.
    assert results[0]["status"] == "completed"


def test_identical_query_reuses_across_operation_ids_and_alias_can_inspect(tmp_path, monkeypatch):
    config, attrs, _ = _fixture(tmp_path, monkeypatch, source=True)
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        return _response(attrs)

    service = BhebCorpusService(config, StateStore(tmp_path / "state"), transport)
    first = service.search(SCOPE, "first", "same question", {"evidence_axis": "behavioral_health"})
    reused = service.search(
        SCOPE, "second", "same question", {"evidence_axis": "behavioral_health"}
    )
    assert first["status"] == reused["status"] == "completed"
    assert first["hits"] == reused["hits"] and reused["reuse_status"] == "reused"
    assert len(calls) == 1
    assert service.inspect(SCOPE, reused["hits"][0]["hit_id"], "second")["matched_text"] == MATCHED


def test_new_term_is_separate_and_refresh_is_explicit(tmp_path, monkeypatch):
    config, attrs, _ = _fixture(tmp_path, monkeypatch)
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        return _response(attrs)

    service = BhebCorpusService(config, StateStore(tmp_path / "state"), transport)
    service.search(SCOPE, "one", "measure")
    service.search(SCOPE, "two", "intervention")
    assert len(calls) == 2
    service.search({**SCOPE, "actor_id": "other"}, "other-actor", "measure")
    service.search(SCOPE, "filtered", "measure", {"evidence_axis": "behavioral_health"})
    service.search(SCOPE, "short", "measure", limit=2)
    assert len(calls) == 5
    refreshed = service.search(SCOPE, "three", "measure", refresh=True)
    assert refreshed["status"] == "completed" and refreshed["reuse_status"] == "refreshed"
    assert len(calls) == 6
    reused = service.search(SCOPE, "four", "measure")
    assert reused["reuse_status"] == "reused" and len(calls) == 6


def test_policy_change_invalidates_query_reuse_and_old_operation(tmp_path, monkeypatch):
    config, attrs, _ = _fixture(tmp_path, monkeypatch, source=True)
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        return _response(attrs)

    service = BhebCorpusService(config, StateStore(tmp_path / "state"), transport)
    service.search(SCOPE, "before", "same question")
    policy_path = tmp_path / "policy.json"
    policy = json.loads(policy_path.read_text())
    policy["sources"]["synthetic-1"]["allow_model_input"] = False
    _write(policy_path, policy)
    assert service.search(SCOPE, "before", "same question")["reason"] == "binding_changed"
    after = service.search(SCOPE, "after", "same question")
    assert after["status"] == "completed" and after["reuse_status"] == "fresh"
    assert len(calls) == 2


def test_uncertain_same_query_requires_explicit_refresh_with_new_operation(tmp_path, monkeypatch):
    config, attrs, _ = _fixture(tmp_path, monkeypatch)
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            raise TimeoutError("outcome unknown")
        return _response(attrs)

    service = BhebCorpusService(config, StateStore(tmp_path / "state"), transport)
    assert service.search(SCOPE, "uncertain-first", "same question")["status"] == "uncertain"
    same = service.search(SCOPE, "uncertain-second", "same question")
    assert same["status"] == "uncertain" and same["reuse_status"] == "reused"
    assert len(calls) == 1
    fresh = service.search(SCOPE, "explicit-refresh", "same question", refresh=True)
    assert fresh["status"] == "completed" and len(calls) == 2


def test_concurrent_identical_query_across_operations_dispatches_once(tmp_path, monkeypatch):
    config, attrs, _ = _fixture(tmp_path, monkeypatch)
    entered = threading.Event()
    release = threading.Event()
    calls = []

    def slow(*args, **kwargs):
        calls.append(1)
        entered.set()
        assert release.wait(2)
        return _response(attrs)

    service = BhebCorpusService(config, StateStore(tmp_path / "state"), slow)
    results = []
    thread = threading.Thread(
        target=lambda: results.append(service.search(SCOPE, "parallel-a", "same query"))
    )
    thread.start()
    assert entered.wait(2)
    alias = service.search(SCOPE, "parallel-b", "same query")
    release.set()
    thread.join(timeout=2)
    assert alias["status"] == "uncertain" and alias["reuse_status"] == "reused"
    assert len(calls) == 1 and results[0]["status"] == "completed"
    assert service.search(SCOPE, "parallel-b", "same query")["status"] == "completed"


def test_rate_limit_wait_is_durable_and_blocks_new_operation(tmp_path, monkeypatch):
    config, attrs, _ = _fixture(tmp_path, monkeypatch)
    calls = []

    def limited(*args, **kwargs):
        calls.append(1)
        return HTTPResult(429, b"{}", {"Retry-After": "30"})

    service = BhebCorpusService(config, StateStore(tmp_path / "state"), limited)
    first = service.search(SCOPE, "limited", "question")
    assert first["status"] == "waiting" and first["diagnostics"]["http_status"] == 429
    second = service.search(SCOPE, "later", "different question")
    assert second["status"] == "waiting" and second["retry_after_seconds"] > 0
    assert len(calls) == 1


def test_known_rate_wait_can_retry_new_operation_after_reset(tmp_path, monkeypatch):
    config, attrs, _ = _fixture(tmp_path, monkeypatch)
    now = [time.time()]
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        return (
            HTTPResult(429, b"{}", {"Retry-After": "30"}) if len(calls) == 1 else _response(attrs)
        )

    service = BhebCorpusService(
        config, StateStore(tmp_path / "state", clock=lambda: now[0]), transport
    )
    assert service.search(SCOPE, "wait-first", "question")["status"] == "waiting"
    assert service.search(SCOPE, "wait-alias", "question")["status"] == "waiting"
    assert len(calls) == 1
    now[0] += 31
    after = service.search(SCOPE, "wait-after", "question")
    assert after["status"] == "completed" and after["reuse_status"] == "fresh"
    assert len(calls) == 2


def test_malformed_sibling_and_clipping_are_visible(tmp_path, monkeypatch):
    config, attrs, _ = _fixture(tmp_path, monkeypatch)
    payload = {
        "data": [
            {
                "file_id": {"malformed": "identifier"},
                "filename": "wrong.md",
                "attributes": attrs,
                "content": [{"type": "text", "text": "invalid"}],
            },
            {
                "file_id": "file-synthetic",
                "filename": "synthetic.md",
                "attributes": attrs,
                "score": 0.4,
                "content": [{"type": "text", "text": "A" * 5000}],
            },
        ],
        "has_more": True,
        "next_page": "opaque-cursor",
    }
    service = BhebCorpusService(
        config,
        StateStore(tmp_path / "state"),
        lambda *a, **k: HTTPResult(200, json.dumps(payload).encode(), {}),
    )
    result = service.search(SCOPE, "mixed", "question")
    assert result["status"] == "completed" and len(result["hits"]) == 1
    assert result["diagnostics"]["skipped_count"] == 1
    assert result["diagnostics"]["malformed_count"] == 1
    assert result["diagnostics"]["clipped_count"] == 1
    assert result["diagnostics"]["has_more"] is True
    assert result["diagnostics"]["next_page_present"] is True


def test_all_unusable_rows_are_not_reported_as_empty(tmp_path, monkeypatch):
    config, attrs, _ = _fixture(tmp_path, monkeypatch)
    bad = {
        "data": [
            {
                "file_id": "unknown",
                "filename": "bad.md",
                "attributes": attrs,
                "content": [{"type": "text", "text": "candidate"}],
            }
        ],
        "has_more": False,
        "next_page": None,
    }
    service = BhebCorpusService(
        config,
        StateStore(tmp_path / "state"),
        lambda *a, **k: HTTPResult(200, json.dumps(bad).encode(), {}),
    )
    result = service.search(SCOPE, "all-bad", "question")
    assert result["status"] == "unusable_results"
    assert result["diagnostics"]["returned_count"] == 1
    assert result["diagnostics"]["skipped_count"] == 1


def test_release_binding_change_withholds_previous_hit(tmp_path, monkeypatch):
    config, attrs, _ = _fixture(tmp_path, monkeypatch, source=True)
    service = BhebCorpusService(
        config, StateStore(tmp_path / "state"), lambda *a, **k: _response(attrs)
    )
    hit_id = service.search(SCOPE, "bound", "question")["hits"][0]["hit_id"]
    assert service.inspect(SCOPE, hit_id, "bound")["status"] == "source_available"
    data = json.loads(config.read_text())
    data["release_manifest_checksum_sha256"] = "0" * 64
    _write(config, data)
    assert service.inspect(SCOPE, hit_id, "bound")["status"] == "source_unavailable"


def test_expired_permission_or_wrong_version_withholds_source(tmp_path, monkeypatch):
    config, attrs, _ = _fixture(tmp_path, monkeypatch, source=True)
    service = BhebCorpusService(
        config, StateStore(tmp_path / "state"), lambda *a, **k: _response(attrs)
    )
    hit_id = service.search(SCOPE, "policy", "question")["hits"][0]["hit_id"]
    policy_path = tmp_path / "policy.json"
    policy = json.loads(policy_path.read_text())
    policy["expires_at"] = "2000-01-01T00:00:00Z"
    _write(policy_path, policy)
    expired = service.inspect(SCOPE, hit_id, "policy")
    assert expired["status"] == "metadata_only" and expired["reason"] == "policy_expired"
    policy["expires_at"] = "2099-01-01T00:00:00Z"
    policy["sources"]["synthetic-1"]["source_version"] = "v2"
    _write(policy_path, policy)
    assert service.inspect(SCOPE, hit_id, "policy")["status"] == "metadata_only"


def test_storage_permission_is_separate_and_required(tmp_path, monkeypatch):
    config, attrs, _ = _fixture(tmp_path, monkeypatch, source=True)
    policy_path = tmp_path / "policy.json"
    policy = json.loads(policy_path.read_text())
    policy["sources"]["synthetic-1"]["allow_storage"] = False
    _write(policy_path, policy)
    service = BhebCorpusService(
        config, StateStore(tmp_path / "state"), lambda *a, **k: _response(attrs)
    )
    hit_id = service.search(SCOPE, "withheld", "question")["hits"][0]["hit_id"]
    assert service.inspect(SCOPE, hit_id, "withheld")["status"] == "metadata_only"
    assert "excerpt" not in service.store.get("bheb_search_hit", hit_id)["body"]


def test_missing_policy_reports_bounded_reason(tmp_path, monkeypatch):
    config, attrs, _ = _fixture(tmp_path, monkeypatch, source=True)
    (tmp_path / "policy.json").unlink()
    service = BhebCorpusService(
        config, StateStore(tmp_path / "state"), lambda *a, **k: _response(attrs)
    )
    hit_id = service.search(SCOPE, "no-policy", "question")["hits"][0]["hit_id"]
    assert service.summary()["reason"] == "policy_unavailable"
    inspected = service.inspect(SCOPE, hit_id, "no-policy")
    assert inspected["status"] == "metadata_only" and inspected["reason"] == "policy_unavailable"
