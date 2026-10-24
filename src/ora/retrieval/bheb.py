"""Optional, receipt-bound BHEB vector-store search for private research leads.

Search results are metadata leads. A provider snippet is never an inspected source
copy and this adapter does not grant permission to put it in a model context.
"""

from __future__ import annotations

import hashlib
import json
import math
import os
import re
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from ora.state import StateStore

from .providers import HTTPDispatchBlocked, HTTPResult

KIND = "bheb_search"
HIT_KIND = "bheb_search_hit"
WAIT_KIND = "bheb_search_wait"
QUERY_KIND = "bheb_search_query"
MAX_QUERY = 1000
MAX_RESULTS = 10
MAX_BODY_BYTES = 512 * 1024
MAX_EXCERPT = 4000
MAX_SOURCE_BYTES = 256 * 1024
FILTER_KEYS = frozenset(
    {
        "access_posture",
        "condition",
        "evidence_axis",
        "evidence_role",
        "source_type",
        "publication_year",
    }
)
_IDENTIFIER = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,159}$")
_HEX = re.compile(r"^[0-9a-f]{64}$")
_STORE_ID = re.compile(r"^vs_[A-Za-z0-9_-]{1,120}$")


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, msg, headers, newurl):
        return None


def bheb_http_transport(
    method: str, url: str, *, body: dict, headers: dict, timeout: float
) -> HTTPResult:
    """Send one fixed-host request without forwarding a bearer token on redirects."""
    if method != "POST" or not re.fullmatch(
        r"https://api\.openai\.com/v1/vector_stores/vs_[A-Za-z0-9_-]{1,120}/search", url
    ):
        raise ValueError("BHEB transport only permits the vector search endpoint")
    request = urllib.request.Request(
        url, data=json.dumps(body).encode(), headers=headers, method="POST"
    )
    opener = urllib.request.build_opener(_NoRedirect)
    try:
        with opener.open(request, timeout=timeout) as response:
            return HTTPResult(
                response.status, response.read(MAX_BODY_BYTES + 1), dict(response.headers)
            )
    except urllib.error.HTTPError as error:
        return HTTPResult(error.code, error.read(MAX_BODY_BYTES + 1), dict(error.headers))


def _digest(value: Any) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


def _scope(scope: dict) -> dict:
    if not isinstance(scope, dict) or any(
        not isinstance(scope.get(k), str) or not _IDENTIFIER.fullmatch(scope[k])
        for k in ("mission_id", "run_id", "actor_id", "task_id")
    ):
        raise ValueError("BHEB search needs exact mission, run, actor, and task identities")
    return {k: scope[k] for k in ("mission_id", "run_id", "actor_id", "task_id")}


def _read_config(path: Path) -> tuple[dict | None, str]:
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None, "config_unavailable"
    if not isinstance(raw, dict):
        return None, "invalid_config"
    for key in (
        "publication_receipt_path",
        "vector_store_id",
        "release_id",
        "release_manifest_checksum_sha256",
    ):
        if not isinstance(raw.get(key), str) or not raw[key].strip():
            return None, "invalid_config"
    if not _HEX.fullmatch(raw["release_manifest_checksum_sha256"]):
        return None, "invalid_config"
    if not _STORE_ID.fullmatch(raw["vector_store_id"]):
        return None, "invalid_config"
    return raw, "configured"


def _read_receipt(config: dict) -> tuple[dict | None, dict[str, dict], str]:
    try:
        receipt = json.loads(Path(config["publication_receipt_path"]).read_text(encoding="utf-8"))
        saved_checksum = receipt.pop("receipt_checksum_sha256")
        if not isinstance(saved_checksum, str) or _digest(receipt) != saved_checksum:
            return None, {}, "invalid_receipt"
        if (
            receipt.get("status") != "completed"
            or receipt.get("manifest_parity_verified") is not True
            or receipt.get("release_id") != config["release_id"]
            or receipt.get("release_manifest_checksum_sha256")
            != config["release_manifest_checksum_sha256"]
            or receipt.get("vector_store_id") != config["vector_store_id"]
        ):
            return None, {}, "invalid_receipt"
        files = receipt.get("files")
        if not isinstance(files, list) or not files or len(files) != receipt.get("file_count"):
            return None, {}, "invalid_receipt"
        by_file: dict[str, dict] = {}
        source_ids: set[str] = set()
        vector_file_ids: set[str] = set()
        for item in files:
            if (
                not isinstance(item, dict)
                or item.get("status") != "completed"
                or not isinstance(item.get("attributes"), dict)
            ):
                return None, {}, "invalid_receipt"
            file_id, source_id = item.get("file_id"), item.get("source_id")
            vector_file_id = item.get("vector_store_file_id")
            if (
                not isinstance(file_id, str)
                or not file_id
                or not isinstance(source_id, str)
                or not source_id
                or file_id in by_file
                or source_id in source_ids
                or not isinstance(vector_file_id, str)
                or not vector_file_id
                or vector_file_id in vector_file_ids
                or not isinstance(item.get("filename"), str)
                or not item["filename"]
                or not isinstance(item.get("upload_checksum_sha256"), str)
                or not _HEX.fullmatch(item["upload_checksum_sha256"])
                or type(item.get("size_bytes")) is not int
                or item["size_bytes"] <= 0
            ):
                return None, {}, "invalid_receipt"
            if item["attributes"].get("source_id") != source_id or item.get(
                "attributes_checksum_sha256"
            ) != _digest(item["attributes"]):
                return None, {}, "invalid_receipt"
            by_file[file_id] = item
            source_ids.add(source_id)
            vector_file_ids.add(vector_file_id)
        receipt["receipt_checksum_sha256"] = saved_checksum
        return receipt, by_file, "configured"
    except (OSError, ValueError, KeyError, TypeError):
        return None, {}, "receipt_unavailable"


class BhebCorpusService:
    """Persist one bounded external search attempt with deterministic replay."""

    def __init__(
        self,
        config_path: Path,
        store: StateStore,
        transport: Callable[..., HTTPResult] | None = None,
        credential: str | None = None,
    ):
        self.config_path = Path(config_path)
        self.store = store
        self.transport = transport or bheb_http_transport
        self.credential = credential

    def _credential(self) -> str:
        return (
            self.credential if self.credential is not None else os.getenv("OSRA_OPENAI_API_KEY", "")
        )

    def _binding(self) -> tuple[dict | None, dict | None, dict[str, dict], str]:
        config, reason = _read_config(self.config_path)
        if config is None:
            return None, None, {}, reason
        receipt, files, reason = _read_receipt(config)
        return config, receipt, files, reason

    @staticmethod
    def _binding_digest(config: dict, receipt: dict) -> str:
        policy_path = config.get("source_use_policy_path")
        try:
            policy_bytes = Path(policy_path).read_bytes() if isinstance(policy_path, str) else b""
            policy_digest = hashlib.sha256(policy_bytes).hexdigest() if policy_bytes else ""
            policy_state = "absent"
            if policy_bytes:
                policy = json.loads(policy_bytes)
                expiration = datetime.fromisoformat(
                    str(policy["expires_at"]).replace("Z", "+00:00")
                )
                policy_state = (
                    "revoked"
                    if policy.get("revoked") is True
                    else "expired"
                    if expiration.tzinfo is None or expiration <= datetime.now(timezone.utc)
                    else "active"
                )
        except (OSError, ValueError, KeyError, TypeError, AttributeError):
            policy_digest = "unavailable"
            policy_state = "unavailable"
        return _digest(
            {
                "release": receipt["release_manifest_checksum_sha256"],
                "receipt": receipt["receipt_checksum_sha256"],
                "policy": policy_digest,
                "policy_state": policy_state,
                "manifest": config.get("source_manifest_path", ""),
                "manifest_available": (
                    Path(config["source_manifest_path"]).is_file()
                    if isinstance(config.get("source_manifest_path"), str)
                    else False
                ),
            }
        )

    @staticmethod
    def _unavailable_reason(config: dict, receipt: dict, source_id: str | None = None) -> str:
        """Return bounded readiness detail without exposing a private path or body."""
        manifest_path = config.get("source_manifest_path")
        if not isinstance(manifest_path, str) or not Path(manifest_path).is_file():
            return "manifest_unavailable"
        policy_path = config.get("source_use_policy_path")
        if not isinstance(policy_path, str) or not Path(policy_path).is_file():
            return "policy_unavailable"
        try:
            policy = json.loads(Path(policy_path).read_text(encoding="utf-8"))
            if not isinstance(policy, dict):
                return "policy_invalid"
            if policy.get("revoked") is True:
                return "policy_revoked"
            expires_at = policy.get("expires_at")
            if not isinstance(expires_at, str):
                return "policy_invalid"
            expiry = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if expiry.tzinfo is None or expiry <= datetime.now(timezone.utc):
                return "policy_expired"
            if (
                policy.get("release_id") != receipt["release_id"]
                or policy.get("release_manifest_checksum_sha256")
                != receipt["release_manifest_checksum_sha256"]
                or policy.get("publication_receipt_checksum_sha256")
                != receipt["receipt_checksum_sha256"]
            ):
                return "policy_binding_mismatch"
            if source_id is not None:
                permission = policy.get("sources", {}).get(source_id)
                if not isinstance(permission, dict):
                    return "source_permission_unavailable"
                if permission.get("revoked") is True:
                    return "source_permission_revoked"
                if (
                    permission.get("allow_storage") is not True
                    or permission.get("allow_model_input") is not True
                ):
                    return "source_permission_unavailable"
            manifest = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
            if (
                not isinstance(manifest, dict)
                or manifest.get("manifest_checksum_sha256")
                != receipt["release_manifest_checksum_sha256"]
            ):
                return "manifest_binding_mismatch"
            if source_id is not None:
                entries = [
                    x
                    for x in manifest.get("sources", [])
                    if isinstance(x, dict)
                    and isinstance(x.get("source"), dict)
                    and x["source"].get("source_id") == source_id
                ]
                if len(entries) == 1:
                    root = Path(manifest_path).parent.resolve()
                    upload = (root / str(entries[0].get("upload_path", ""))).resolve()
                    if not upload.is_relative_to(root):
                        return "manifest_invalid"
                    if not upload.is_file():
                        return "source_body_unavailable"
                    if upload.stat().st_size > MAX_SOURCE_BYTES:
                        return "source_body_unavailable"
                    if hashlib.sha256(upload.read_bytes()).hexdigest() != entries[0].get(
                        "upload_checksum_sha256"
                    ):
                        return "body_hash_mismatch"
        except (OSError, ValueError, TypeError, AttributeError):
            return "source_verification_unavailable"
        return "source_verification_unavailable"

    def current_source_use(self, source_id: str, source_version: str, source_sha256: str) -> bool:
        """Recheck body and current permission before reusing a cached BHEB copy."""
        if not all(isinstance(v, str) and v for v in (source_id, source_version, source_sha256)):
            return False
        config, receipt, files, _ = self._binding()
        if config is None or receipt is None:
            return False
        matched = [
            f
            for f in files.values()
            if f["source_id"] == source_id
            and f["attributes"].get("source_version") == source_version
            and f.get("upload_checksum_sha256") == source_sha256
        ]
        if len(matched) != 1:
            return False
        checked = self._checked_source(
            config,
            receipt,
            files,
            {
                "source_id": source_id,
                "source_version": source_version,
                "file_id": matched[0]["file_id"],
            },
        )
        return checked is not None and checked["upload_sha256"] == source_sha256

    def summary(self) -> dict:
        config, receipt, files, reason = self._binding()
        if receipt is None:
            return {"enabled": False, "coverage": "unavailable", "reason": reason}
        policy_path = config.get("source_use_policy_path")
        checked_source_available = False
        if isinstance(config.get("source_manifest_path"), str) and isinstance(policy_path, str):
            try:
                permitted_ids = set(
                    json.loads(Path(policy_path).read_text(encoding="utf-8")).get("sources", {})
                )
            except (OSError, ValueError, TypeError, AttributeError):
                permitted_ids = set()
            for file in files.values():
                if (
                    file["source_id"] in permitted_ids
                    and self._checked_source(
                        config,
                        receipt,
                        files,
                        {
                            "source_id": file["source_id"],
                            "source_version": file["attributes"].get("source_version", ""),
                            "file_id": file["file_id"],
                        },
                    )
                    is not None
                ):
                    checked_source_available = True
                    break
        access_posture_counts: dict[str, int] = {}
        for file in files.values():
            posture = str(file["attributes"].get("access_posture", "unspecified"))
            access_posture_counts[posture] = access_posture_counts.get(posture, 0) + 1
        filter_coverage = {
            key: sum(
                1
                for file in files.values()
                if file["attributes"].get(key) not in (None, "", "unspecified")
            )
            for key in (
                "condition",
                "population",
                "evaluation_stage",
                "study_type",
                "publication_year",
            )
        }
        return {
            "enabled": bool(self._credential()),
            "coverage": "source_available" if checked_source_available else "metadata_only",
            "source_count": receipt["file_count"],
            "checked_source_available": checked_source_available,
            "access_posture_counts": access_posture_counts,
            "filter_coverage_counts": filter_coverage,
            "config_digest": self._binding_digest(config, receipt),
            "reason": (
                "credential_unavailable"
                if not self._credential()
                else self._unavailable_reason(config, receipt)
                if not checked_source_available
                else "checked_source_permission_available"
            ),
        }

    def search(
        self,
        scope: dict,
        operation_id: str,
        query: str,
        filters: dict | None = None,
        limit: int = 6,
        *,
        refresh: bool = False,
    ) -> dict:
        owner = _scope(scope)
        if not isinstance(operation_id, str) or not _IDENTIFIER.fullmatch(operation_id):
            raise ValueError("Invalid BHEB operation ID")
        if not isinstance(query, str) or not query.strip() or len(query) > MAX_QUERY:
            raise ValueError("BHEB query must contain 1 to 1000 characters")
        if type(limit) is not int or not 1 <= limit <= MAX_RESULTS:
            raise ValueError("BHEB result limit must be 1 to 10")
        if type(refresh) is not bool:
            raise ValueError("BHEB refresh must be a boolean")
        filters = filters or {}
        if (
            not isinstance(filters, dict)
            or set(filters) - FILTER_KEYS
            or any(
                (type(v) is not int or not 1900 <= v <= 2100)
                if k == "publication_year"
                else (not isinstance(v, str) or not v or len(v) > 120)
                for k, v in filters.items()
            )
        ):
            raise ValueError("BHEB filters must be bounded supported string values")
        config, receipt, files, reason = self._binding()
        if receipt is None:
            return {
                "status": "unavailable",
                "reason": reason,
                "operation_id": operation_id,
                "hits": [],
            }
        if not self._credential():
            return {
                "status": "unavailable",
                "reason": "credential_unavailable",
                "operation_id": operation_id,
                "hits": [],
            }
        request = {
            "scope": owner,
            "query": query.strip(),
            "filters": filters,
            "limit": limit,
            "refresh": refresh,
            "release_id": receipt["release_id"],
            "receipt_checksum_sha256": receipt["receipt_checksum_sha256"],
            "binding_digest": self._binding_digest(config, receipt),
        }
        fingerprint = _digest(request)
        intent_fingerprint = _digest(
            {key: request[key] for key in ("scope", "query", "filters", "limit", "refresh")}
        )
        query_key = "bheb-query:" + _digest(
            {
                key: request[key]
                for key in (
                    "scope",
                    "query",
                    "filters",
                    "limit",
                    "release_id",
                    "receipt_checksum_sha256",
                    "binding_digest",
                )
            }
        )
        record_id = "bheb-search:" + operation_id
        prior = self.store.get(KIND, record_id)
        if prior:
            if prior["body"].get("intent_fingerprint") != intent_fingerprint:
                raise ValueError("BHEB operation ID was already used with different inputs")
            if prior["body"].get("fingerprint") != fingerprint:
                return {
                    "status": "unavailable",
                    "reason": "binding_changed",
                    "operation_id": operation_id,
                    "hits": [],
                }
            return self._view(record_id, prior["body"])
        wait_id = "bheb-wait:" + receipt["receipt_checksum_sha256"]
        marker = {
            "scope": owner,
            "fingerprint": fingerprint,
            "intent_fingerprint": intent_fingerprint,
            "binding_digest": request["binding_digest"],
            "query_key": query_key,
            "reuse_status": "refreshed" if refresh else "fresh",
            "phase": "dispatched",
            "attempt_count": 1,
            "release_id": receipt["release_id"],
            "receipt_checksum_sha256": receipt["receipt_checksum_sha256"],
            "hit_ids": [],
        }

        def claim(tx):
            existing = tx.get(KIND, record_id)
            if existing:
                if existing["body"].get("intent_fingerprint") != intent_fingerprint:
                    raise ValueError("BHEB operation ID was already used with different inputs")
                if existing["body"].get("fingerprint") != fingerprint:
                    return {"created": False, "binding_changed": True}
                return {"created": False, "body": existing["body"]}
            family = tx.get(QUERY_KIND, query_key)
            wait = tx.get(WAIT_KIND, wait_id)
            origin = tx.get(KIND, family["body"]["origin_search_id"]) if family else None
            known_wait_expired = bool(
                origin
                and origin["body"].get("phase") == "waiting"
                and wait
                and wait["body"].get("indefinite") is not True
                and wait["body"].get("until_epoch", 0) <= self.store.clock()
            )
            if family and not refresh and not known_wait_expired:
                alias = {
                    **marker,
                    "phase": "reused",
                    "reuse_status": "reused",
                    "origin_search_id": family["body"]["origin_search_id"],
                }
                tx.put(KIND, record_id, alias)
                return {"created": False, "body": alias}
            if wait and (
                wait["body"].get("indefinite") is True
                or wait["body"].get("until_epoch", 0) > self.store.clock()
            ):
                return {
                    "created": False,
                    "waiting": True,
                    "waiting_until": wait["body"].get("until_epoch"),
                    "wait_indefinite": wait["body"].get("indefinite") is True,
                }
            tx.put(KIND, record_id, marker)
            tx.put(
                QUERY_KIND,
                query_key,
                {
                    "origin_search_id": record_id,
                    "binding_digest": request["binding_digest"],
                    "scope": owner,
                    "intent_fingerprint": intent_fingerprint,
                },
                expected_version=family["version"] if family else 0,
            )
            return {"created": True, "body": marker}

        admitted = self.store.transact(
            record_id + ":claim:" + uuid.uuid4().hex, request, claim, mission_id=owner["mission_id"]
        )
        if not admitted["created"]:
            if admitted.get("binding_changed"):
                return {
                    "status": "unavailable",
                    "reason": "binding_changed",
                    "operation_id": operation_id,
                    "hits": [],
                }
            if admitted.get("waiting"):
                return {
                    "status": "waiting",
                    "reason": "provider_rate_limit",
                    "operation_id": operation_id,
                    "retry_after_seconds": (
                        None
                        if admitted.get("wait_indefinite")
                        else max(1, math.ceil(admitted["waiting_until"] - self.store.clock()))
                    ),
                    "wait_indefinite": bool(admitted.get("wait_indefinite")),
                    "hits": [],
                }
            return self._view(record_id, admitted["body"])
        args: dict[str, Any] = {
            "query": query.strip(),
            "max_num_results": limit,
            "rewrite_query": False,
        }
        clauses = [{"type": "eq", "key": k, "value": v} for k, v in sorted(filters.items())]
        if clauses:
            args["filters"] = (
                clauses[0] if len(clauses) == 1 else {"type": "and", "filters": clauses}
            )
        started = time.monotonic()
        diagnostic: dict[str, Any] = {
            "http_status": None,
            "response_sha256": None,
            "elapsed_ms": None,
            "usage": "unknown",
            "returned_count": None,
            "skipped_count": None,
            "malformed_count": None,
            "clipped_count": None,
            "has_more": None,
            "next_page": None,
            "failure_condition": None,
        }
        stage = "transport"
        try:
            result = self.transport(
                "POST",
                "https://api.openai.com/v1/vector_stores/" + config["vector_store_id"] + "/search",
                body=args,
                headers={
                    "Authorization": "Bearer " + self._credential(),
                    "Content-Type": "application/json",
                },
                timeout=8,
            )
            stage = "response"
            if not isinstance(result, HTTPResult) or not isinstance(result.body, bytes):
                raise ValueError("invalid provider response object")
            diagnostic["http_status"] = result.status
            if len(result.body) > MAX_BODY_BYTES:
                raise OverflowError("provider response exceeded local byte cap")
            diagnostic["response_sha256"] = hashlib.sha256(result.body).hexdigest()
            if result.status != 200:
                phase = (
                    "waiting"
                    if result.status == 429
                    else "failed"
                    if 400 <= result.status < 500
                    else "uncertain"
                )
                hits: list[dict] = []
                diagnostic["failure_condition"] = (
                    "authentication_failed"
                    if result.status == 401
                    else "store_unavailable"
                    if result.status == 404
                    else "provider_rate_limit"
                    if result.status == 429
                    else "provider_rejected"
                    if 400 <= result.status < 500
                    else "provider_unavailable"
                )
                if phase == "waiting":
                    header = {str(k).lower(): v for k, v in result.headers.items()}.get(
                        "retry-after", "60"
                    )
                    numeric = str(header).strip()
                    if re.fullmatch(r"[0-9]+", numeric):
                        if len(numeric) > 15:
                            diagnostic["wait_indefinite"] = True
                            diagnostic["retry_after_seconds"] = None
                        else:
                            diagnostic["retry_after_seconds"] = max(1, int(numeric))
                    else:
                        diagnostic["retry_after_seconds"] = 60
            else:
                stage = "parsing"
                payload = json.loads(result.body)
                if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
                    raise ValueError("invalid provider result shape")
                rows = payload["data"]
                diagnostic["returned_count"] = len(rows)
                diagnostic["has_more"] = (
                    payload.get("has_more") if isinstance(payload.get("has_more"), bool) else None
                )
                diagnostic["next_page"] = (
                    payload.get("next_page") if isinstance(payload.get("next_page"), str) else None
                )
                hits, skipped, malformed, clipped = self._hits(
                    rows[:limit], files, filters, record_id, config, receipt
                )
                diagnostic["skipped_count"] = skipped + max(0, len(rows) - limit)
                diagnostic["malformed_count"] = malformed
                diagnostic["clipped_count"] = clipped
                phase = "completed" if hits or not rows else "unusable_results"
        except HTTPDispatchBlocked as error:
            phase, hits = "not_dispatched", []
            diagnostic["failure_condition"] = (
                error.reason
                if error.reason
                in {
                    "retrieval_attempt_cap",
                    "budget_lock_busy",
                    "budget_identity_mismatch",
                    "unresolved_attempt",
                    "endpoint_not_https",
                    "endpoint_invalid",
                    "model_endpoint_mismatch",
                    "model_request_mismatch",
                    "model_request_invalid",
                    "token_cap",
                }
                else "local_admission_blocked"
            )
        except TimeoutError:
            phase, hits = "uncertain", []
            diagnostic["failure_condition"] = "provider_timeout"
        except OverflowError:
            phase, hits = "uncertain", []
            diagnostic["failure_condition"] = "oversized_response"
        except Exception:
            phase, hits = "uncertain", []
            diagnostic["failure_condition"] = (
                "malformed_response"
                if stage in {"response", "parsing"}
                else "transport_outcome_unknown"
            )
        diagnostic["elapsed_ms"] = round((time.monotonic() - started) * 1000, 3)
        result_body = {
            **marker,
            **diagnostic,
            "phase": phase,
            "attempt_count": 0 if phase == "not_dispatched" else 1,
            "hit_ids": [h["hit_id"] for h in hits],
        }

        def save(tx):
            for hit in hits:
                tx.put(HIT_KIND, hit["hit_id"], hit)
            tx.put(KIND, record_id, result_body, expected_version=1)
            if phase == "waiting":
                current_wait = tx.get(WAIT_KIND, wait_id)
                tx.put(
                    WAIT_KIND,
                    wait_id,
                    {
                        "until_epoch": (
                            0
                            if diagnostic.get("wait_indefinite")
                            else self.store.clock() + diagnostic["retry_after_seconds"]
                        ),
                        "indefinite": diagnostic.get("wait_indefinite") is True,
                        "http_status": 429,
                        "source_operation_id": operation_id,
                    },
                    expected_version=current_wait["version"] if current_wait else 0,
                )
            return result_body

        try:
            self.store.transact(
                record_id + ":result", result_body, save, mission_id=owner["mission_id"]
            )
        except Exception:
            return {
                "status": "uncertain",
                "reason": "result_persistence_uncertain",
                "operation_id": operation_id,
                "hits": [],
            }
        return self._view(record_id, result_body)

    def _hits(
        self,
        rows: list,
        files: dict[str, dict],
        filters: dict,
        record_id: str,
        config: dict,
        receipt: dict,
    ) -> tuple[list[dict], int, int, int]:
        hits = []
        skipped = 0
        malformed = 0
        clipped = 0
        for index, row in enumerate(rows):
            if not isinstance(row, dict):
                skipped += 1
                malformed += 1
                continue
            file_id = row.get("file_id")
            if not isinstance(file_id, str):
                skipped += 1
                malformed += 1
                continue
            file = files.get(file_id)
            if (
                not file
                or row.get("filename") != file.get("filename")
                or row.get("attributes") != file["attributes"]
            ):
                skipped += 1
                continue
            attrs = file["attributes"]
            if any(attrs.get(k) != v for k, v in filters.items()):
                skipped += 1
                continue
            content = row.get("content")
            if not isinstance(content, list):
                skipped += 1
                malformed += 1
                continue
            whole_excerpt = "\n".join(
                part["text"]
                for part in content
                if isinstance(part, dict)
                and part.get("type") == "text"
                and isinstance(part.get("text"), str)
            )
            excerpt = whole_excerpt[:MAX_EXCERPT]
            if not excerpt.strip():
                skipped += 1
                malformed += 1
                continue
            clipped += len(whole_excerpt) > MAX_EXCERPT
            hit_id = "bheb-hit:" + _digest([record_id, index, row["file_id"], excerpt])[:32]
            hit = {
                "hit_id": hit_id,
                "search_id": record_id,
                "source_id": file["source_id"],
                "source_version": attrs.get("source_version", ""),
                "content_label": "unverified_search_excerpt",
                "access_posture": attrs.get("access_posture", ""),
                "score": (
                    row["score"]
                    if type(row.get("score")) in (int, float) and math.isfinite(row["score"])
                    else None
                ),
                "excerpt_original_chars": len(whole_excerpt),
                "excerpt_clipped": len(whole_excerpt) > MAX_EXCERPT,
                "excerpt_sha256": hashlib.sha256(excerpt.encode()).hexdigest(),
                "file_id": file["file_id"],
            }
            if self._checked_source(config, receipt, files, hit) is not None:
                hit["excerpt"] = excerpt
            hits.append(hit)
        return hits, skipped, malformed, clipped

    def _view(self, record_id: str, body: dict) -> dict:
        origin_id = body.get("origin_search_id")
        effective = body
        if isinstance(origin_id, str):
            origin = self.store.get(KIND, origin_id)
            effective = origin["body"] if origin else {"phase": "dispatched", "hit_ids": []}
        hits = []
        for hit_id in effective["hit_ids"]:
            hit = self.store.get(HIT_KIND, hit_id)
            if hit:
                item = hit["body"]
                hits.append(
                    {
                        k: item[k]
                        for k in (
                            "hit_id",
                            "source_id",
                            "source_version",
                            "content_label",
                            "access_posture",
                            "score",
                            "excerpt_clipped",
                            "excerpt_original_chars",
                        )
                    }
                )
        return {
            "status": effective["phase"] if effective["phase"] != "dispatched" else "uncertain",
            "reason": (
                "dispatch_outcome_unknown"
                if effective["phase"] == "dispatched"
                else effective.get("failure_condition")
            ),
            "operation_id": record_id.removeprefix("bheb-search:"),
            "reuse_status": body.get("reuse_status", "fresh"),
            "coverage": "metadata_only",
            "hits": hits,
            "diagnostics": {
                key: effective.get(key)
                for key in (
                    "http_status",
                    "response_sha256",
                    "elapsed_ms",
                    "usage",
                    "returned_count",
                    "skipped_count",
                    "malformed_count",
                    "clipped_count",
                    "has_more",
                    "retry_after_seconds",
                    "wait_indefinite",
                    "attempt_count",
                    "failure_condition",
                )
            }
            | {
                "next_page_present": isinstance(effective.get("next_page"), str)
                and bool(effective["next_page"])
            },
        }

    def inspect(self, scope: dict, hit_id: str, operation_id: str) -> dict:
        owner = _scope(scope)
        if not isinstance(hit_id, str) or not _IDENTIFIER.fullmatch(hit_id):
            raise ValueError("Invalid BHEB hit ID")
        if not isinstance(operation_id, str) or not _IDENTIFIER.fullmatch(operation_id):
            raise ValueError("Invalid BHEB operation ID")
        config, receipt, files, reason = self._binding()
        if receipt is None:
            return {"status": "source_unavailable", "reason": reason, "hit_id": hit_id}
        hit = self.store.get(HIT_KIND, hit_id)
        if hit is None:
            return {"status": "source_unavailable", "reason": "unknown_hit", "hit_id": hit_id}
        search = self.store.get(KIND, "bheb-search:" + operation_id)
        effective_id = (
            (search["body"].get("origin_search_id") or "bheb-search:" + operation_id)
            if search
            else ""
        )
        effective = self.store.get(KIND, effective_id) if effective_id else None
        if (
            search is None
            or search["body"].get("scope") != owner
            or effective is None
            or hit["body"].get("search_id") != effective_id
            or hit_id not in effective["body"].get("hit_ids", [])
            or search["body"].get("receipt_checksum_sha256") != receipt["receipt_checksum_sha256"]
        ):
            return {
                "status": "source_unavailable",
                "reason": "scope_or_binding_mismatch",
                "hit_id": hit_id,
            }
        item = hit["body"]
        checked = self._checked_source(config, receipt, files, item)
        if checked is not None:
            matched_text = None
            match_offset = None
            excerpt = item.get("excerpt")
            if (
                isinstance(excerpt, str)
                and excerpt
                and item.get("excerpt_clipped") is False
                and checked["source_text"].count(excerpt) == 1
            ):
                matched_text = excerpt
                match_offset = checked["source_text"].index(excerpt)
            return {
                "status": "source_available",
                "hit_id": hit_id,
                "hit_ref": {"kind": HIT_KIND, "id": hit_id, "version": hit["version"]},
                "matched_text": matched_text,
                "match_offset": match_offset,
                **checked,
            }
        return {
            "status": "metadata_only",
            "reason": self._unavailable_reason(config, receipt, item["source_id"]),
            "hit_id": hit_id,
            "source_id": item["source_id"],
            "source_version": item["source_version"],
            "content_label": item["content_label"],
            "excerpt_sha256": item["excerpt_sha256"],
        }

    def _checked_source(
        self, config: dict, receipt: dict, files: dict[str, dict], hit: dict
    ) -> dict | None:
        """Read exact immutable UTF-8 bytes only under a separate affirmative policy."""
        manifest_name = config.get("source_manifest_path")
        policy_name = config.get("source_use_policy_path")
        if not isinstance(manifest_name, str) or not isinstance(policy_name, str):
            return None
        try:
            manifest_path = Path(manifest_name).expanduser().resolve(strict=True)
            root = manifest_path.parent
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest_checksum = manifest.pop("manifest_checksum_sha256")
            if (
                manifest_checksum != receipt["release_manifest_checksum_sha256"]
                or _digest(manifest) != manifest_checksum
                or manifest.get("release_id") != receipt["release_id"]
                or manifest.get("status") != "approved"
            ):
                return None
            policy = json.loads(Path(policy_name).read_text(encoding="utf-8"))
            if (
                policy.get("release_manifest_checksum_sha256") != manifest_checksum
                or policy.get("publication_receipt_checksum_sha256")
                != receipt["receipt_checksum_sha256"]
                or policy.get("release_id") != receipt["release_id"]
                or policy.get("revoked") is not False
            ):
                return None
            expires_at = policy.get("expires_at")
            if not isinstance(expires_at, str):
                return None
            expiration = datetime.fromisoformat(expires_at.replace("Z", "+00:00"))
            if expiration.tzinfo is None or expiration <= datetime.now(timezone.utc):
                return None
            permissions = policy.get("sources", {}).get(hit["source_id"])
            if (
                not isinstance(permissions, dict)
                or permissions.get("allow_storage") is not True
                or permissions.get("allow_model_input") is not True
                or permissions.get("revoked") is not False
                or permissions.get("source_version") != hit["source_version"]
                or permissions.get("upload_checksum_sha256")
                != files[hit["file_id"]].get("upload_checksum_sha256")
                or not isinstance(permissions.get("rights_basis"), str)
                or not permissions["rights_basis"].strip()
                or permissions.get("declared_surface") not in {"abstract", "passage", "full_text"}
            ):
                return None
            entries = [
                entry
                for entry in manifest.get("sources", [])
                if isinstance(entry, dict)
                and isinstance(entry.get("source"), dict)
                and entry["source"].get("source_id") == hit["source_id"]
            ]
            if len(entries) != 1:
                return None
            entry = entries[0]
            source = entry["source"]
            file = files[hit["file_id"]]
            if (
                source.get("version") != hit["source_version"]
                or entry.get("upload_checksum_sha256") != file.get("upload_checksum_sha256")
                or entry.get("upload_attributes") != file.get("attributes")
                or source.get("retrieval_eligible") is not True
                or source.get("lifecycle_state") != "approved"
            ):
                return None
            mode = entry.get("content_mode")
            retrieval_mode = entry.get("retrieval_mode")
            if retrieval_mode == "abstract_packet":
                if (
                    mode not in {"existing_abstract", "extracted_abstract"}
                    or permissions["declared_surface"] != "abstract"
                    or source.get("access_posture") != "abstract_only"
                    or not str(source.get("abstract", "")).strip()
                ):
                    return None
            elif retrieval_mode == "approved_full_text":
                if (
                    permissions["declared_surface"] not in {"passage", "full_text"}
                    or source.get("access_posture") != "open_full_text"
                ):
                    return None
            else:
                return None
            relative = Path(entry["upload_path"])
            path = (root / relative).resolve(strict=True)
            if (
                not path.is_relative_to(root)
                or not path.is_file()
                or path.stat().st_size > MAX_SOURCE_BYTES
                or path.stat().st_size != entry.get("upload_size_bytes")
            ):
                return None
            body = path.read_bytes()
            body_hash = hashlib.sha256(body).hexdigest()
            if body_hash != entry["upload_checksum_sha256"]:
                return None
            upload_text = body.decode("utf-8")
            if not upload_text.strip():
                return None
            if retrieval_mode == "abstract_packet":
                heading = "## Abstract or reviewed summary\n\n"
                footer = "\n\nApplicability:"
                if (
                    upload_text.count(heading) != 1
                    or upload_text.count(footer) != 1
                    or upload_text.count(f"Content mode: {mode}\n") != 1
                ):
                    return None
                section = upload_text.split(heading, 1)[1].split(footer, 1)[0].strip()
                if section != source["abstract"].strip() or upload_text.count(section) != 1:
                    return None
                source_text = section
            else:
                source_text = upload_text
            logical_hash = hashlib.sha256(source_text.encode("utf-8")).hexdigest()
            return {
                "source_text": source_text,
                "source_sha256": logical_hash,
                "upload_sha256": body_hash,
                "source_version": source["version"],
                "source_id": hit["source_id"],
                "source_url": source.get("canonical_url") or "bheb-source:" + hit["source_id"],
                "title": source.get("title", ""),
                "doi": source.get("doi", ""),
                "pmid": source.get("pmid", ""),
                "retrieved_at": self.store.now(),
                "rights_status": "permitted",
                "rights_basis": permissions["rights_basis"],
                "declared_surface": permissions["declared_surface"],
                "release_manifest_checksum_sha256": manifest_checksum,
                "publication_receipt_checksum_sha256": receipt["receipt_checksum_sha256"],
                "provider_file_id": file["file_id"],
            }
        except (OSError, ValueError, KeyError, TypeError, UnicodeError):
            return None
