"""Read-only BHEB readiness and selected-release candidate comparison."""

import json

import pytest

from ora.retrieval.bheb import _digest
from ora.retrieval.bheb_maintenance import (
    inspect_readiness,
    plan_candidates,
    public_summary,
)
from tests.retrieval.test_bheb import _fixture


def _candidates(path, rows):
    path.write_text(json.dumps({"candidates": rows}), encoding="utf-8")
    return path


def _rewrite_release(tmp_path, mutate):
    manifest_path = tmp_path / "release" / "release-manifest.json"
    receipt_path = tmp_path / "receipt.json"
    config_path = tmp_path / "config.json"
    policy_path = tmp_path / "policy.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
    config = json.loads(config_path.read_text(encoding="utf-8"))
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    manifest.pop("manifest_checksum_sha256")
    receipt.pop("receipt_checksum_sha256")
    mutate(manifest, receipt, policy)
    manifest_hash = _digest(manifest)
    receipt["release_manifest_checksum_sha256"] = manifest_hash
    receipt_hash = _digest(receipt)
    config["release_manifest_checksum_sha256"] = manifest_hash
    policy["release_manifest_checksum_sha256"] = manifest_hash
    policy["publication_receipt_checksum_sha256"] = receipt_hash
    manifest_path.write_text(json.dumps({**manifest, "manifest_checksum_sha256": manifest_hash}), encoding="utf-8")
    receipt_path.write_text(json.dumps({**receipt, "receipt_checksum_sha256": receipt_hash}), encoding="utf-8")
    config_path.write_text(json.dumps(config), encoding="utf-8")
    policy_path.write_text(json.dumps(policy), encoding="utf-8")


def _add_second_source(manifest, receipt, _policy):
    attrs = {**receipt["files"][0]["attributes"], "source_id": "synthetic-2"}
    manifest["sources"].append({
        "source": {"source_id": "synthetic-2", "version": "v1",
                   "title": "Second synthetic source", "doi": "10.0000/second",
                   "canonical_url": "https://example.org/article?id=2",
                   "retrieval_eligible": True, "lifecycle_state": "approved"},
        "upload_path": "missing.md", "upload_size_bytes": 1,
        "upload_checksum_sha256": "0" * 64, "upload_attributes": attrs,
    })
    receipt["files"].append({
        "file_id": "file-synthetic-2", "vector_store_file_id": "vs-file-synthetic-2",
        "source_id": "synthetic-2", "status": "completed", "filename": "missing.md",
        "upload_checksum_sha256": "0" * 64, "size_bytes": 1,
        "attributes": attrs, "attributes_checksum_sha256": _digest(attrs),
        "chunking_strategy": {},
    })
    receipt["file_count"] = 2


def test_missing_archive_and_policy_remain_metadata_only_and_novelty_unknown(
    tmp_path, monkeypatch,
):
    config, _, _ = _fixture(tmp_path, monkeypatch, source=False)
    report = inspect_readiness(config)
    assert report["receipt_verified"] is True
    assert report["manifest_verified"] is False
    assert report["source_use_ready"] is False
    assert set(report["reason_codes"]) == {
        "manifest_not_configured", "policy_not_configured",
    }
    candidates = _candidates(tmp_path / "candidates.json", [
        {"title": "Synthetic evidence", "canonical_url": "https://example.org/synthetic"},
    ])
    planned = plan_candidates(config, candidates)
    assert planned["candidate_counts"] == {
        "added_candidate": 0, "duplicate": 0, "version_changed": 0, "unknown": 1,
    }
    assert planned["candidates"][0]["reason"] == "selected_release_not_verified"
    assert "synthetic" not in json.dumps(public_summary(planned))
    assert str(tmp_path) not in json.dumps(public_summary(planned))


def test_verified_synthetic_release_policy_and_changed_body(tmp_path, monkeypatch):
    config, _, _ = _fixture(tmp_path, monkeypatch, source=True)
    before = {str(path.relative_to(tmp_path)): path.read_bytes()
              for path in tmp_path.rglob("*") if path.is_file()}
    report = inspect_readiness(config)
    assert report["status"] == "source_use_ready"
    assert report["counts"] == {
        "receipt_files": 1, "manifest_sources": 1, "body_verified": 1,
        "body_missing": 0, "body_mismatch": 0, "policy_verified_sources": 1,
        "source_validated": 1,
    }
    after = {str(path.relative_to(tmp_path)): path.read_bytes()
             for path in tmp_path.rglob("*") if path.is_file()}
    assert after == before

    body_path = tmp_path / "release" / "synthetic.md"
    body_path.write_bytes(body_path.read_bytes() + b" changed")
    changed = inspect_readiness(config)
    assert changed["source_use_ready"] is False
    assert changed["counts"]["body_mismatch"] == 1
    assert "source_body_mismatch" in changed["reason_codes"]


def test_candidate_identity_classes_are_scoped_to_verified_release(tmp_path, monkeypatch):
    config, _, _ = _fixture(tmp_path, monkeypatch, source=True)
    candidates = _candidates(tmp_path / "candidates.json", [
        {"title": "Synthetic evidence", "canonical_url": "https://example.org/synthetic"},
        {"title": "Synthetic evidence", "source_id": "synthetic-1", "source_version": "v2"},
        {"title": "Unlisted candidate", "doi": "10.0000/new-synthetic"},
        {"title": "Synthetic evidence"},
        {"title": "Unidentified candidate"},
    ])
    report = plan_candidates(config, candidates)
    assert [item["classification"] for item in report["candidates"]] == [
        "duplicate", "version_changed", "added_candidate", "unknown", "unknown",
    ]
    assert report["candidate_counts"] == {
        "added_candidate": 1, "duplicate": 1, "version_changed": 1, "unknown": 2,
    }
    assert report["comparison_scope"] == "selected_verified_release_only"
    assert report["adoption_authority"] == "none"
    assert report["external_api_calls"] == 0 and report["archive_writes"] == 0
    assert "reconcile-acquisition-batch" in report["handoff"][0]


def test_partially_ready_release_and_conflicting_identifiers(tmp_path, monkeypatch):
    config, _, _ = _fixture(tmp_path, monkeypatch, source=True)
    _rewrite_release(tmp_path, _add_second_source)
    report = inspect_readiness(config)
    assert report["status"] == "partially_ready"
    assert report["source_use_ready"] is True
    assert report["all_sources_ready"] is False
    assert report["counts"]["source_validated"] == 1
    assert report["counts"]["body_missing"] == 1
    candidates = _candidates(tmp_path / "candidates.json", [
        {"source_id": "synthetic-1", "doi": "10.0000/second"},
    ])
    plan = plan_candidates(config, candidates)
    assert plan["candidates"][0]["classification"] == "unknown"
    assert plan["candidates"][0]["reason"] == "conflicting_exact_identifiers"


def test_mixed_missing_body_and_unsupported_surface_report_both_reasons(
    tmp_path, monkeypatch,
):
    config, _, _ = _fixture(tmp_path, monkeypatch, source=True)

    def mutate(manifest, receipt, policy):
        _add_second_source(manifest, receipt, policy)
        policy["sources"]["synthetic-1"]["declared_surface"] = "passage"

    _rewrite_release(tmp_path, mutate)
    report = inspect_readiness(config)
    assert report["counts"]["body_missing"] == 1
    assert report["counts"]["body_verified"] == 1
    assert report["counts"]["source_validated"] == 0
    assert {"source_body_missing", "source_structure_unverified"} <= set(
        report["reason_codes"]
    )


def test_query_url_identity_is_not_collapsed(tmp_path, monkeypatch):
    config, _, _ = _fixture(tmp_path, monkeypatch, source=True)
    _rewrite_release(
        tmp_path,
        lambda manifest, _receipt, _policy: manifest["sources"][0]["source"].update(
            canonical_url="https://example.org/article?id=1"
        ),
    )
    candidates = _candidates(tmp_path / "candidates.json", [
        {"canonical_url": "https://example.org/article?id=1"},
        {"canonical_url": "https://example.org/article?id=2"},
    ])
    plan = plan_candidates(config, candidates)
    assert [row["classification"] for row in plan["candidates"]] == [
        "duplicate", "added_candidate",
    ]


def test_joined_exact_identifiers_must_agree_before_duplicate_or_version_change(
    tmp_path, monkeypatch,
):
    config, _, _ = _fixture(tmp_path, monkeypatch, source=True)
    _rewrite_release(
        tmp_path,
        lambda manifest, _receipt, _policy: manifest["sources"][0]["source"].update(
            doi="10.0000/owned"
        ),
    )
    candidates = _candidates(tmp_path / "candidates.json", [
        {"source_id": "synthetic-1", "doi": "10.0000/other"},
        {"source_id": "synthetic-1", "canonical_url": "https://example.org/other"},
        {"source_id": "synthetic-1", "doi": "10.0000/owned",
         "canonical_url": "https://example.org/synthetic"},
        {"source_id": "synthetic-1", "doi": "10.0000/owned",
         "canonical_url": "https://example.org/synthetic", "source_version": "v2"},
        {"doi": "10.0000/owned", "canonical_url": "https://example.org/other"},
    ])
    plan = plan_candidates(config, candidates)
    assert [(row["classification"], row["reason"]) for row in plan["candidates"]] == [
        ("unknown", "conflicting_exact_identifiers"),
        ("unknown", "conflicting_exact_identifiers"),
        ("duplicate", "exact_identity_in_selected_release"),
        ("version_changed", "exact_identity_changed_version_or_upload"),
        ("unknown", "conflicting_exact_identifiers"),
    ]


def test_new_identifier_on_source_with_missing_identifier_stays_uncertain(
    tmp_path, monkeypatch,
):
    config, _, _ = _fixture(tmp_path, monkeypatch, source=True)
    candidates = _candidates(tmp_path / "candidates.json", [
        {"source_id": "synthetic-1", "doi": "10.0000/proposed"},
    ])
    result = plan_candidates(config, candidates)["candidates"][0]
    assert (result["classification"], result["reason"]) == (
        "unknown", "unverified_supplied_identifier",
    )


def test_missing_titles_do_not_match_each_other(tmp_path, monkeypatch):
    config, _, _ = _fixture(tmp_path, monkeypatch, source=True)
    _rewrite_release(
        tmp_path,
        lambda manifest, _receipt, _policy: manifest["sources"][0]["source"].pop(
            "title", None
        ),
    )
    candidates = _candidates(tmp_path / "candidates.json", [{}])
    result = plan_candidates(config, candidates)["candidates"][0]
    assert (result["classification"], result["reason"]) == (
        "unknown", "missing_exact_identity",
    )


def test_verified_hashes_do_not_override_packet_surface_mismatch(tmp_path, monkeypatch):
    config, _, _ = _fixture(tmp_path, monkeypatch, source=True)
    policy_path = tmp_path / "policy.json"
    policy = json.loads(policy_path.read_text(encoding="utf-8"))
    policy["sources"]["synthetic-1"]["declared_surface"] = "passage"
    policy_path.write_text(json.dumps(policy), encoding="utf-8")
    report = inspect_readiness(config)
    assert report["counts"]["body_verified"] == 1
    assert report["counts"]["policy_verified_sources"] == 1
    assert report["counts"]["source_validated"] == 0
    assert "source_structure_unverified" in report["reason_codes"]
    assert report["source_use_ready"] is False


def test_candidate_file_rejects_content_and_invalid_checksum(tmp_path, monkeypatch):
    config, _, _ = _fixture(tmp_path, monkeypatch, source=True)
    bad = _candidates(tmp_path / "bad.json", [
        {"title": "Synthetic", "article_text": "should not enter this planner"},
    ])
    with pytest.raises(ValueError, match="unsupported"):
        plan_candidates(config, bad)
    bad = _candidates(tmp_path / "bad.json", [
        {"title": "Synthetic", "upload_checksum_sha256": "not-a-hash"},
    ])
    with pytest.raises(ValueError, match="SHA-256"):
        plan_candidates(config, bad)


def test_public_summary_drops_unrecognized_private_fields():
    safe = public_summary({
        "status": "secret-path", "reason_codes": ["policy_expired", "private-url"],
        "counts": {"receipt_files": 2, "private-source-id": 1},
        "candidate_counts": {"duplicate": 1, "private-title": 1},
        "candidate": {"doi": "10.0000/private"},
    })
    assert safe == {
        "status": "unavailable", "reason_codes": ["policy_expired"],
        "receipt_verified": False, "manifest_verified": False,
        "source_policy_verified": False, "source_use_ready": False,
        "all_sources_ready": False,
        "counts": {"receipt_files": 2}, "candidate_counts": {"duplicate": 1},
    }
