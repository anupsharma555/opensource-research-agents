"""Read-only BHEB release readiness and candidate planning.

This checks local published-release evidence without contacting OpenAI or
modifying the BHEB source archive. Candidate comparisons are relative to one
verified release, not the authoritative Zotero catalog.
"""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from .bheb import BhebCorpusService, _digest, _read_config, _read_receipt

_HEX = re.compile(r"^[0-9a-f]{64}$")
_MAX_CANDIDATES = 1000
_MAX_FILE_BYTES = 256 * 1024


def _load_json(path: Path) -> dict | None:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError, UnicodeError):
        return None
    return value if isinstance(value, dict) else None


def _manifest(config: dict, receipt: dict) -> tuple[dict | None, str]:
    name = config.get("source_manifest_path")
    if not isinstance(name, str) or not name:
        return None, "manifest_not_configured"
    manifest = _load_json(Path(name))
    if manifest is None:
        return None, "manifest_unavailable"
    saved_hash = manifest.pop("manifest_checksum_sha256", None)
    if (not isinstance(saved_hash, str) or not _HEX.fullmatch(saved_hash)
            or _digest(manifest) != saved_hash
            or saved_hash != receipt["release_manifest_checksum_sha256"]
            or manifest.get("release_id") != receipt["release_id"]
            or manifest.get("status") != "approved"
            or not isinstance(manifest.get("sources"), list)):
        return None, "manifest_mismatch"
    manifest["manifest_checksum_sha256"] = saved_hash
    return manifest, "verified"


def _policy(config: dict, receipt: dict) -> tuple[dict | None, str]:
    name = config.get("source_use_policy_path")
    if not isinstance(name, str) or not name:
        return None, "policy_not_configured"
    policy = _load_json(Path(name))
    if policy is None:
        return None, "policy_unavailable"
    if (policy.get("release_id") != receipt["release_id"]
            or policy.get("release_manifest_checksum_sha256")
            != receipt["release_manifest_checksum_sha256"]
            or policy.get("publication_receipt_checksum_sha256")
            != receipt["receipt_checksum_sha256"]
            or not isinstance(policy.get("sources"), dict)):
        return None, "policy_binding_mismatch"
    if policy.get("revoked") is not False:
        return None, "policy_revoked"
    try:
        expires = datetime.fromisoformat(policy["expires_at"].replace("Z", "+00:00"))
    except (KeyError, TypeError, ValueError):
        return None, "policy_expiry_invalid"
    if expires.tzinfo is None:
        return None, "policy_expiry_invalid"
    if expires <= datetime.now(timezone.utc):
        return None, "policy_expired"
    return policy, "verified"


def _sources(manifest: dict, receipt_files: dict[str, dict], policy: dict | None,
             manifest_path: Path) -> dict[str, Any]:
    by_source = {file["source_id"]: file for file in receipt_files.values()}
    seen: set[str] = set()
    result: dict[str, Any] = {}
    for entry in manifest["sources"]:
        if not isinstance(entry, dict) or not isinstance(entry.get("source"), dict):
            return {"status": "manifest_entry_invalid", "sources": {}}
        source = entry["source"]
        source_id = source.get("source_id")
        if not isinstance(source_id, str) or not source_id or source_id in seen:
            return {"status": "manifest_entry_invalid", "sources": {}}
        file = by_source.get(source_id)
        if (source_id in seen
                or file is None or source.get("version") != file["attributes"].get("source_version")
                or entry.get("upload_checksum_sha256") != file.get("upload_checksum_sha256")
                or entry.get("upload_attributes") != file.get("attributes")):
            return {"status": "manifest_receipt_mismatch", "sources": {}}
        seen.add(source_id)
        relative = entry.get("upload_path")
        body_status = "body_missing"
        if isinstance(relative, str) and relative and not Path(relative).is_absolute():
            try:
                root = manifest_path.parent.resolve(strict=True)
                body_path = (root / relative).resolve(strict=True)
                if body_path.is_relative_to(root) and body_path.is_file():
                    size = body_path.stat().st_size
                    if size <= _MAX_FILE_BYTES and size == entry.get("upload_size_bytes"):
                        digest = hashlib.sha256(body_path.read_bytes()).hexdigest()
                        body_status = (
                            "body_verified" if digest == entry["upload_checksum_sha256"]
                            else "body_hash_mismatch"
                        )
                    else:
                        body_status = "body_size_mismatch"
            except OSError:
                body_status = "body_missing"
        permissions = (policy or {}).get("sources", {}).get(source_id)
        permission_status = "policy_unavailable"
        if isinstance(permissions, dict):
            permission_status = "policy_denied"
            if (permissions.get("allow_storage") is True
                    and permissions.get("allow_model_input") is True
                    and permissions.get("revoked") is False
                    and permissions.get("source_version") == source["version"]
                    and permissions.get("upload_checksum_sha256")
                    == entry["upload_checksum_sha256"]
                    and isinstance(permissions.get("rights_basis"), str)
                    and permissions["rights_basis"].strip()
                    and permissions.get("declared_surface")
                    in {"abstract", "passage", "full_text"}):
                permission_status = "policy_verified"
        result[source_id] = {
            "source": source, "entry": entry, "file": file,
            "body_status": body_status, "permission_status": permission_status,
        }
    if seen != set(by_source):
        return {"status": "manifest_receipt_mismatch", "sources": {}}
    return {"status": "verified", "sources": result}


def inspect_readiness(config_path: Path) -> dict:
    """Inspect one selected BHEB release without network or filesystem writes."""
    config, config_state = _read_config(Path(config_path))
    if config is None:
        return {
            "status": "unavailable", "reason_codes": [config_state],
            "receipt_verified": False, "manifest_verified": False,
            "source_policy_verified": False, "source_use_ready": False,
            "all_sources_ready": False,
            "counts": {},
        }
    receipt, files, receipt_state = _read_receipt(config)
    if receipt is None:
        return {
            "status": "unavailable", "reason_codes": [receipt_state],
            "receipt_verified": False, "manifest_verified": False,
            "source_policy_verified": False, "source_use_ready": False,
            "all_sources_ready": False,
            "counts": {},
        }
    manifest, manifest_state = _manifest(config, receipt)
    policy, policy_state = _policy(config, receipt)
    reason_codes = []
    if manifest is None:
        reason_codes.append(manifest_state)
    if policy is None:
        reason_codes.append(policy_state)
    source_result = (
        _sources(manifest, files, policy, Path(config["source_manifest_path"]))
        if manifest is not None else {"status": "manifest_unavailable", "sources": {}}
    )
    if manifest is not None and source_result["status"] != "verified":
        reason_codes.append(source_result["status"])
    sources = source_result["sources"]
    counts = {
        "receipt_files": len(files),
        "manifest_sources": len(sources) if source_result["status"] == "verified" else 0,
        "body_verified": sum(x["body_status"] == "body_verified" for x in sources.values()),
        "body_missing": sum(x["body_status"] == "body_missing" for x in sources.values()),
        "body_mismatch": sum(x["body_status"] in {"body_hash_mismatch", "body_size_mismatch"}
                             for x in sources.values()),
        "policy_verified_sources": sum(x["permission_status"] == "policy_verified"
                                       for x in sources.values()),
    }
    class _ReadOnlyClock:
        @staticmethod
        def now() -> str:
            return datetime.now(timezone.utc).isoformat()

    adapter = BhebCorpusService(Path(config_path), _ReadOnlyClock())
    counts["source_validated"] = sum(
        adapter._checked_source(config, receipt, files, {
            "source_id": source_id,
            "source_version": item["source"].get("version"),
            "file_id": item["file"]["file_id"],
        }) is not None
        for source_id, item in sources.items()
        if item["body_status"] == "body_verified"
        and item["permission_status"] == "policy_verified"
    )
    if counts["body_missing"]:
        reason_codes.append("source_body_missing")
    if counts["body_mismatch"]:
        reason_codes.append("source_body_mismatch")
    eligible_for_validation = sum(
        item["body_status"] == "body_verified"
        and item["permission_status"] == "policy_verified"
        for item in sources.values()
    )
    if counts["source_validated"] < eligible_for_validation:
        reason_codes.append("source_structure_unverified")
    some_ready = bool(counts["source_validated"] > 0 and manifest is not None
                      and policy is not None and source_result["status"] == "verified")
    all_ready = some_ready and counts["source_validated"] == counts["receipt_files"]
    return {
        "status": "source_use_ready" if all_ready else "partially_ready" if some_ready
        else "metadata_only",
        "reason_codes": reason_codes,
        "receipt_verified": True,
        "manifest_verified": manifest is not None and source_result["status"] == "verified",
        "source_policy_verified": policy is not None,
        "source_use_ready": some_ready,
        "all_sources_ready": all_ready,
        "counts": counts,
    }


def public_summary(report: dict) -> dict:
    """Allow only aggregate status and reason codes into ordinary stdout."""
    known_status = {"unavailable", "metadata_only", "partially_ready", "source_use_ready"}
    known_reasons = {
        "config_unavailable", "invalid_config", "receipt_unavailable", "invalid_receipt",
        "manifest_not_configured", "manifest_unavailable", "manifest_mismatch",
        "manifest_entry_invalid", "manifest_receipt_mismatch", "policy_not_configured",
        "policy_unavailable", "policy_binding_mismatch", "policy_revoked",
        "policy_expiry_invalid", "policy_expired", "source_body_missing",
        "source_body_mismatch", "source_structure_unverified",
    }
    safe = {
        "status": report["status"] if report.get("status") in known_status else "unavailable",
        "reason_codes": [value for value in report.get("reason_codes", [])
                         if value in known_reasons],
    }
    for key in ("receipt_verified", "manifest_verified", "source_policy_verified",
                "source_use_ready", "all_sources_ready"):
        safe[key] = report.get(key) is True
    count_keys = {"receipt_files", "manifest_sources", "body_verified", "body_missing",
                  "body_mismatch", "policy_verified_sources", "source_validated"}
    safe["counts"] = {
        key: value for key, value in report.get("counts", {}).items()
        if key in count_keys and type(value) is int and value >= 0
    }
    if "candidate_counts" in report:
        safe["candidate_counts"] = {
            key: value for key, value in report["candidate_counts"].items()
            if key in {"added_candidate", "duplicate", "version_changed", "unknown"}
            and type(value) is int and value >= 0
        }
    return safe


def _doi(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    clean = value.strip().casefold()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if clean.startswith(prefix):
            clean = clean[len(prefix):]
    return clean if len(clean) <= 250 and re.fullmatch(r"10\.\d{4,9}/\S+", clean) else None


def _url(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    parsed = urlsplit(value.strip())
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        return None
    return urlunsplit((parsed.scheme.lower(), parsed.netloc.lower(),
                       parsed.path.rstrip("/") or "/", parsed.query, ""))


def _title(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    normalized = " ".join(value.casefold().split())
    return normalized if normalized and len(normalized) <= 500 else None


def _candidate_rows(path: Path) -> list[dict]:
    document = _load_json(path)
    rows = document.get("candidates") if document is not None else None
    if not isinstance(rows, list) or len(rows) > _MAX_CANDIDATES:
        raise ValueError("Candidate file needs a bounded candidates list")
    if any(not isinstance(row, dict) for row in rows):
        raise ValueError("Every candidate must be a metadata object")
    allowed = {"title", "doi", "canonical_url", "source_id", "source_version",
               "upload_checksum_sha256"}
    for row in rows:
        if set(row) - allowed or any(not isinstance(value, str) or len(value) > 500
                                     for value in row.values()):
            raise ValueError("Candidate metadata has unsupported or oversized fields")
        digest = row.get("upload_checksum_sha256")
        if digest is not None and not _HEX.fullmatch(digest):
            raise ValueError("Candidate upload checksum must be SHA-256")
    return rows


def plan_candidates(config_path: Path, candidates_path: Path) -> dict:
    """Classify metadata against the selected release; never adopt or publish it."""
    readiness = inspect_readiness(config_path)
    rows = _candidate_rows(candidates_path)
    config, _ = _read_config(Path(config_path))
    receipt, files, _ = _read_receipt(config) if config is not None else (None, {}, "")
    manifest, _ = _manifest(config, receipt) if receipt is not None else (None, "")
    verified_sources = (
        _sources(manifest, files, None, Path(config["source_manifest_path"]))
        if manifest is not None else {"status": "unavailable", "sources": {}}
    )
    sources = verified_sources["sources"] if verified_sources["status"] == "verified" else None
    decisions = []
    for index, candidate in enumerate(rows):
        classification, reason = "unknown", "selected_release_not_verified"
        if sources is not None:
            by_id = [sid for sid in sources if candidate.get("source_id") == sid]
            candidate_doi, candidate_url = _doi(candidate.get("doi")), _url(candidate.get("canonical_url"))
            by_doi = [sid for sid, item in sources.items()
                      if candidate_doi and _doi(item["source"].get("doi")) == candidate_doi]
            by_url = [sid for sid, item in sources.items()
                      if candidate_url and _url(item["source"].get("canonical_url")) == candidate_url]
            matched = set([*by_id, *by_doi, *by_url])
            if len(matched) > 1:
                reason = "conflicting_exact_identifiers"
            elif matched:
                source_id = next(iter(matched))
                existing = sources[source_id]
                existing_doi = _doi(existing["source"].get("doi"))
                existing_url = _url(existing["source"].get("canonical_url"))
                contradictory = (
                    ("source_id" in candidate and candidate["source_id"] != source_id)
                    or (candidate_doi is not None and existing_doi is not None
                        and candidate_doi != existing_doi)
                    or (candidate_url is not None and existing_url is not None
                        and candidate_url != existing_url)
                )
                unverified = (
                    ("doi" in candidate and (candidate_doi is None or existing_doi is None))
                    or ("canonical_url" in candidate
                        and (candidate_url is None or existing_url is None))
                )
                if contradictory:
                    reason = "conflicting_exact_identifiers"
                elif unverified:
                    # A source's missing DOI/URL is not proof that a newly supplied
                    # identifier belongs to it. BHEB's authoritative catalog
                    # reconciliation must decide this before adoption.
                    reason = "unverified_supplied_identifier"
                else:
                    changed_version = bool(
                        candidate.get("source_version")
                        and candidate["source_version"] != existing["source"].get("version")
                    )
                    changed_upload = bool(
                        candidate.get("upload_checksum_sha256")
                        and candidate["upload_checksum_sha256"]
                        != existing["entry"]["upload_checksum_sha256"]
                    )
                    classification = "version_changed" if changed_version or changed_upload else "duplicate"
                    reason = "exact_identity_changed_version_or_upload" if classification == "version_changed" else "exact_identity_in_selected_release"
            elif (_title(candidate.get("title")) is not None
                  and any(_title(candidate.get("title")) == _title(item["source"].get("title"))
                          for item in sources.values())):
                reason = "title_only_possible_duplicate"
            elif candidate_doi or candidate_url:
                classification, reason = "added_candidate", "identity_absent_from_selected_release"
            else:
                reason = "missing_exact_identity"
        decisions.append({"index": index, "classification": classification,
                          "reason": reason, "candidate": candidate})
    counts = {name: sum(item["classification"] == name for item in decisions)
              for name in ("added_candidate", "duplicate", "version_changed", "unknown")}
    return {
        **readiness, "candidate_counts": counts, "candidates": decisions,
        "comparison_scope": "selected_verified_release_only",
        "handoff": [
            "In Behavioral-Health-Evidence-Builder, reconcile candidates against the current Zotero export and rights manifest with manage_rag_corpus.py reconcile-acquisition-batch.",
            "Use manage_rag_corpus.py capture-import-dry-run and check-acquisition-readiness; require a new-only batch with zero updates and errors.",
            "Complete metadata, scientific-domain, clinical, and rights review; finalize the reviewed catalog before selecting a new release.",
            "Use plan_expanded_release.py with the finalized catalog and baseline release; publish a new immutable release/store only through BHEB's own preflight and publication gates.",
        ],
        "handoff_required_inputs": [
            "current Zotero library export", "source batch manifest",
            "separate source rights manifest", "source import batch",
            "new-only checksummed dry-run receipt", "finalized reviewed catalog directory",
            "immutable baseline release directory", "new release ID and approved reviewer",
        ],
        "handoff_commands": [
            "PYTHONPATH=backend/src python3 backend/scripts/manage_rag_corpus.py reconcile-acquisition-batch --library-export <current-export> --source-batch-manifest <batch-manifest> --source-rights-manifest <rights-manifest> --source-import-batch <import-batch> --output-directory <new-only-directory> --output-batch-id <new-id>",
            "PYTHONPATH=backend/src python3 backend/scripts/manage_rag_corpus.py capture-import-dry-run --batch-id <new-id> --import-batch <import-batch> --rights-manifest <rights-manifest> --import-report <import-report> --errors-report <errors-report> --output <dry-run-receipt>",
            "PYTHONPATH=backend/src python3 backend/scripts/manage_rag_corpus.py check-acquisition-readiness --library-export <current-export> --batch-manifest <batch-manifest> --rights-manifest <rights-manifest> --import-batch <import-batch> --dry-run-receipt <dry-run-receipt> --output <readiness-receipt>",
            "PYTHONPATH=backend/src python3 backend/scripts/plan_expanded_release.py --reviewed-catalog-directory <finalized-catalog> --baseline-release <baseline-release> --release-id <new-release-id> --reviewer <approved-reviewer> --spec-output <private-spec-output> --comparison-output <private-comparison-output>",
            "PYTHONPATH=backend/src python3 backend/scripts/manage_rag_corpus.py preflight-publication --manifest <new-release-manifest> --output <private-preflight-output>",
        ],
        "adoption_authority": "none",
        "external_api_calls": 0,
        "archive_writes": 0,
    }
