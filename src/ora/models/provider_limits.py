"""Private, account-scoped provider token-limit hints across isolated local stores.

Only a verified response header and its exact saved provenance are retained.
This cache supplies packing advice, never an admission decision or quota balance.
Offline fixtures must not be shared by callers unless explicitly selected.
"""

from __future__ import annotations

import hashlib
import json
import math
import shutil
import sqlite3
import tempfile
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator

from ora.state.store import StateStore

from .token_measurement import input_limit_observation

SCOPE_KEYS = ("credential_profile", "quota_domain", "endpoint", "model")
TTL_SECONDS = 24 * 60 * 60
FUTURE_SKEW_SECONDS = 300


def _reference(value: object, kind: str) -> bool:
    return (type(value) is dict and set(value) == {"kind", "id", "version"}
            and value["kind"] == kind and isinstance(value["id"], str)
            and bool(value["id"]) and type(value["version"]) is int
            and value["version"] > 0)


def _scope(value: object) -> dict[str, str] | None:
    if (type(value) is not dict or set(value) != set(SCOPE_KEYS)
            or any(not isinstance(value[key], str) or not value[key]
                   or len(value[key]) > 512 or any(ord(char) < 32 for char in value[key])
                   for key in SCOPE_KEYS)):
        return None
    return {key: value[key] for key in SCOPE_KEYS}


def _verified(store: StateStore, receipt_ref: dict, *, now: float) -> dict | None:
    """Recheck receipt, call, intent and request bytes before exporting a hint."""
    if not _reference(receipt_ref, "model_response_receipt"):
        return None
    receipt = store.get(**receipt_ref)
    if receipt is None:
        return None
    body = receipt["body"]
    headers = body.get("rate_headers") or {}
    text_limit = headers.get("x-ratelimit-limit-tokens") if isinstance(headers, dict) else None
    if not isinstance(text_limit, str) or not text_limit.isdecimal():
        return None
    limit = int(text_limit)
    if limit <= 0:
        return None
    try:
        observed_at = datetime.fromisoformat(receipt["created_at"].replace("Z", "+00:00")).timestamp()
    except (KeyError, TypeError, ValueError, OverflowError):
        return None
    if (not math.isfinite(observed_at) or observed_at > now + FUTURE_SKEW_SECONDS
            or observed_at + TTL_SECONDS <= now):
        return None
    intent_ref = body.get("intent_ref")
    call_ref = body.get("call_ref")
    if not _reference(intent_ref, "model_request_intent") or not _reference(call_ref, "agent_call"):
        return None
    intent = store.get(**intent_ref)
    call = store.get(**call_ref)
    if intent is None or call is None or intent["body"].get("call_ref") != call_ref:
        return None
    lineage = (body, intent["body"], call["body"])
    if any(
        not isinstance(lineage[0].get(key), str) or not lineage[0][key]
        or any(item.get(key) != lineage[0][key] for item in lineage[1:])
        for key in ("mission_id", "task_id", "attempt_id")
    ) or not _reference(body.get("configuration_ref"), "agent_configuration") \
            or body["configuration_ref"] != intent["body"].get("configuration_ref"):
        return None
    status, http_status = body.get("status"), body.get("http_status")
    if not (
        status in {"complete", "tool_requested", "partial"} and http_status in {None, 200}
        or status == "failed" and http_status in {413, 429}
    ):
        return None
    request_sha = body.get("request_sha256")
    blob = intent["body"].get("blob")
    if (not isinstance(request_sha, str) or len(request_sha) != 64
            or intent["body"].get("request_sha256") != request_sha
            or not isinstance(blob, dict) or blob.get("sha256") != request_sha
            or type(blob.get("size")) is not int):
        return None
    try:
        request_bytes = store.read_blob(request_sha)
    except (OSError, ValueError):
        return None
    if len(request_bytes) != blob["size"] or hashlib.sha256(request_bytes).hexdigest() != request_sha:
        return None
    scope = _scope({
        "credential_profile": body.get("credential_profile"),
        "quota_domain": body.get("quota_domain"),
        "endpoint": call["body"].get("endpoint"),
        "model": call["body"].get("model_id"),
    })
    if (scope is None or intent["body"].get("credential_profile") != scope["credential_profile"]
            or intent["body"].get("quota_domain") != scope["quota_domain"]):
        return None
    return {
        "scope": scope, "observed_limit_tokens": limit, "observed_at": observed_at,
        "source_state_path": str(store.root), "receipt_ref": receipt_ref,
        "request_sha256": request_sha, "basis": "verified_saved_provider_limit_header",
    }


def verified_saved_input_limit(
    store: StateStore, receipt_ref: dict, *, now: float | None = None,
) -> dict | None:
    """Revalidate a typed ITPM refusal against its exact saved request and scope."""
    moment = time.time() if now is None else now
    verified = _verified(store, receipt_ref, now=moment)
    if verified is None:
        return None
    receipt = store.get(**receipt_ref)
    body = receipt["body"]
    if (body.get("status") != "failed" or body.get("http_status") != 413
            or body.get("error_kind") != "http_error"):
        return None
    marker = store.get("model_dispatch_attempt", f"model-dispatch:{body['intent_ref']['id']}")
    if (marker is None or marker["body"].get("status") != "captured"
            or marker["body"].get("intent_ref") != body["intent_ref"]
            or marker["body"].get("receipt_ref") != receipt_ref
            or marker["body"].get("request_sha256") != body.get("request_sha256")):
        return None
    blob = body.get("blob")
    if not isinstance(blob, dict) or not isinstance(blob.get("sha256"), str):
        return None
    try:
        raw = store.read_blob(blob["sha256"])
        if (len(raw) != blob.get("size")
                or hashlib.sha256(raw).hexdigest() != blob["sha256"]):
            return None
        payload = json.loads(raw)
    except (OSError, ValueError, TypeError):
        return None
    if not isinstance(payload, dict):
        return None
    observation = input_limit_observation(
        body["http_status"], payload.get("provider_error"), verified["scope"]["model"],
    )
    if observation is None:
        return None
    intent = store.get(**body["intent_ref"])
    estimate = intent["body"].get("request_token_estimate") or {}
    local = estimate.get("tokens")
    if type(local) is not int or local <= 0:
        return None
    measurement = body.get("token_measurement") or {}
    if (measurement.get("local_input_estimate") not in {None, local}
            or measurement.get("provider_admission_scope") not in {
                None, "input_tokens_per_minute",
            }
            or measurement.get("provider_admission_tokens") not in {
                None, observation["requested_tokens"],
            }
            or measurement.get("provider_admission_limit_tokens") not in {
                None, observation["limit_tokens"],
            }):
        return None
    return {
        **verified,
        "observed_input_limit_tokens": observation["limit_tokens"],
        "provider_input_admission_tokens": observation["requested_tokens"],
        "local_input_estimate_tokens": local,
        "input_admission_discrepancy_tokens": max(
            0, observation["requested_tokens"] - local,
        ),
        "input_limit_basis": "verified_saved_provider_itpm_413",
    }


def _verified_from_snapshot(
    path: Path, receipt_ref: dict, *, now: float, input_limit: bool = False,
) -> dict | None:
    """Inspect cached evidence without creating sidecars in its original store."""
    sources = [path / "state.sqlite3", path / "state.sqlite3-wal"]

    def stamps():
        result = []
        for source in sources:
            try:
                stat = source.stat()
            except FileNotFoundError:
                result.append(None)
            else:
                result.append((stat.st_ino, stat.st_size, stat.st_mtime_ns, stat.st_ctime_ns))
        return result

    before = stamps()
    if before[0] is None:
        return None
    with tempfile.TemporaryDirectory(prefix="ora-provider-limit-read-") as scratch:
        root = Path(scratch)
        for source, stamp in zip(sources, before, strict=True):
            if stamp is not None:
                shutil.copyfile(source, root / source.name)
        # A concurrent write/checkpoint can make copied DB and WAL disagree.
        # An unavailable hint is safer than treating that mix as current proof.
        if stamps() != before:
            return None
        (root / "objects").symlink_to(path / "objects", target_is_directory=True)
        store = StateStore(root, read_only=True)
        verifier = verified_saved_input_limit if input_limit else _verified
        verified = verifier(store, receipt_ref, now=now)
        return ({**verified, "source_state_path": str(path)}
                if verified is not None else None)


class ProviderLimitCache:
    """One SQLite hint per exact profile, quota domain, endpoint and model."""

    def __init__(self, root: Path):
        self.root = Path(root).resolve()
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        self.root.chmod(0o700)
        self.path = self.root / "provider-limits.sqlite3"
        with self._connect() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS observations (
                credential_profile TEXT NOT NULL, quota_domain TEXT NOT NULL,
                endpoint TEXT NOT NULL, model TEXT NOT NULL,
                observed_limit_tokens INTEGER NOT NULL CHECK(observed_limit_tokens > 0),
                observed_at REAL NOT NULL, source_state_path TEXT NOT NULL,
                receipt_id TEXT NOT NULL, receipt_version INTEGER NOT NULL,
                request_sha256 TEXT NOT NULL,
                PRIMARY KEY (credential_profile, quota_domain, endpoint, model)
            )""")
            db.execute("""CREATE TABLE IF NOT EXISTS input_observations (
                credential_profile TEXT NOT NULL, quota_domain TEXT NOT NULL,
                endpoint TEXT NOT NULL, model TEXT NOT NULL,
                observed_input_limit_tokens INTEGER NOT NULL
                    CHECK(observed_input_limit_tokens > 0),
                observed_at REAL NOT NULL, source_state_path TEXT NOT NULL,
                receipt_id TEXT NOT NULL, receipt_version INTEGER NOT NULL,
                request_sha256 TEXT NOT NULL,
                PRIMARY KEY (credential_profile, quota_domain, endpoint, model,
                             source_state_path, receipt_id, receipt_version)
            )""")
        self.path.chmod(0o600)

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        db = sqlite3.connect(self.path, timeout=0.5)
        try:
            with db:
                yield db
        finally:
            db.close()

    def record(self, store: StateStore, receipt_ref: dict) -> dict | None:
        """Save a live caller-selected receipt; newer observations replace older ones."""
        if not isinstance(store, StateStore):
            return None
        now = time.time()
        try:
            item = _verified(store, receipt_ref, now=now)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.DatabaseError):
            return None
        if item is None:
            return None
        try:
            input_item = verified_saved_input_limit(store, receipt_ref, now=now)
        except (OSError, ValueError, KeyError, TypeError, sqlite3.DatabaseError):
            input_item = None
        scope = item["scope"]
        if input_item is None:
            # An older cache can hold an ITPM 413 only in its combined-header
            # row. Verify and promote that exact receipt before a newer 200
            # overwrites the single header slot; never infer input from TPM.
            prior = self.latest(scope, now=now)
            if prior is not None and prior.get("input_limit_receipt_ref") is not None:
                try:
                    promoted = _verified_from_snapshot(
                        Path(prior["input_limit_source_state_path"]),
                        prior["input_limit_receipt_ref"], now=now, input_limit=True,
                    )
                except (OSError, ValueError, KeyError, TypeError, sqlite3.DatabaseError):
                    promoted = None
                if promoted is not None and promoted["scope"] == scope:
                    input_item = promoted
        with self._connect() as db:
            db.execute("BEGIN IMMEDIATE")
            db.execute("""INSERT INTO observations VALUES (?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(credential_profile, quota_domain, endpoint, model)
                DO UPDATE SET observed_limit_tokens=excluded.observed_limit_tokens,
                    observed_at=excluded.observed_at,
                    source_state_path=excluded.source_state_path,
                    receipt_id=excluded.receipt_id,
                    receipt_version=excluded.receipt_version,
                    request_sha256=excluded.request_sha256
                WHERE excluded.observed_at > observations.observed_at""", (
                    *(scope[key] for key in SCOPE_KEYS), item["observed_limit_tokens"],
                    item["observed_at"], item["source_state_path"], receipt_ref["id"],
                    receipt_ref["version"], item["request_sha256"],
                ))
            if input_item is not None:
                db.execute("""INSERT INTO input_observations VALUES (?,?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(credential_profile, quota_domain, endpoint, model,
                                source_state_path, receipt_id, receipt_version)
                    DO UPDATE SET observed_input_limit_tokens=excluded.observed_input_limit_tokens,
                        observed_at=excluded.observed_at,
                        request_sha256=excluded.request_sha256
                    WHERE excluded.observed_at > input_observations.observed_at""", (
                        *(scope[key] for key in SCOPE_KEYS),
                        input_item["observed_input_limit_tokens"],
                        input_item["observed_at"], input_item["source_state_path"],
                        input_item["receipt_ref"]["id"],
                        input_item["receipt_ref"]["version"],
                        input_item["request_sha256"],
                    ))
        return self.latest(scope, now=now)

    def latest(self, scope: dict, now: float | None = None) -> dict | None:
        """Return only a fresh hint still backed by matching saved evidence."""
        selected = _scope(scope)
        moment = time.time() if now is None else now
        if selected is None or type(moment) not in {int, float} or not math.isfinite(moment):
            return None
        try:
            with self._connect() as db:
                row = db.execute("""SELECT observed_limit_tokens, observed_at,
                    source_state_path, receipt_id, receipt_version, request_sha256
                    FROM observations WHERE credential_profile=? AND quota_domain=?
                    AND endpoint=? AND model=?""", tuple(selected[key] for key in SCOPE_KEYS)).fetchone()
                input_rows = db.execute("""SELECT observed_input_limit_tokens, observed_at,
                    source_state_path, receipt_id, receipt_version, request_sha256
                    FROM input_observations WHERE credential_profile=? AND quota_domain=?
                    AND endpoint=? AND model=?""", tuple(selected[key] for key in SCOPE_KEYS)).fetchall()
            if row is None:
                return None
            limit, observed_at, path, receipt_id, version, request_sha = row
            if (type(limit) is not int or limit <= 0 or type(observed_at) not in {int, float}
                    or not math.isfinite(observed_at) or observed_at > moment + FUTURE_SKEW_SECONDS
                    or observed_at + TTL_SECONDS <= moment or not isinstance(path, str)
                    or not Path(path).is_absolute() or Path(path).resolve() != Path(path)
                    or type(version) is not int or version < 1):
                return None
            receipt_ref = {"kind": "model_response_receipt", "id": receipt_id, "version": version}
            verified = _verified_from_snapshot(Path(path), receipt_ref, now=moment)
            if (verified is None or verified["scope"] != selected
                    or verified["observed_limit_tokens"] != limit
                    or verified["observed_at"] != observed_at
                    or verified["request_sha256"] != request_sha
                    or verified["source_state_path"] != path):
                return None
            # Older caches can have a header row for an ITPM refusal without
            # the newer input table. Recheck its exact private receipt on read.
            if not input_rows:
                input_item = _verified_from_snapshot(
                    Path(path), receipt_ref, now=moment, input_limit=True,
                )
            else:
                verified_inputs = []
                for input_limit, input_at, input_path, input_id, input_version, input_sha in input_rows:
                    if (type(input_limit) is not int or input_limit <= 0
                            or type(input_at) not in {int, float} or not math.isfinite(input_at)
                            or not isinstance(input_path, str)
                            or not Path(input_path).is_absolute()
                            or Path(input_path).resolve() != Path(input_path)
                            or type(input_version) is not int or input_version <= 0):
                        return None
                    input_ref = {"kind": "model_response_receipt", "id": input_id,
                                 "version": input_version}
                    candidate = _verified_from_snapshot(
                        Path(input_path), input_ref, now=moment, input_limit=True,
                    )
                    if candidate is None:
                        # Stale rows do not make current evidence invalid.
                        if input_at + TTL_SECONDS <= moment:
                            continue
                        return None
                    if (candidate["scope"] != selected
                            or candidate["observed_input_limit_tokens"] != input_limit
                            or candidate["observed_at"] != input_at
                            or candidate["source_state_path"] != input_path
                            or candidate["request_sha256"] != input_sha):
                        return None
                    verified_inputs.append(candidate)
                input_item = min(
                    verified_inputs,
                    key=lambda item: item["observed_input_limit_tokens"]
                    - max(128, item["input_admission_discrepancy_tokens"] + 64),
                    default=None,
                )
            if input_item is not None:
                verified = {
                    **verified,
                    "observed_input_limit_tokens": input_item["observed_input_limit_tokens"],
                    "provider_input_admission_tokens": input_item["provider_input_admission_tokens"],
                    "local_input_estimate_tokens": input_item["local_input_estimate_tokens"],
                    "input_admission_discrepancy_tokens": input_item["input_admission_discrepancy_tokens"],
                    "input_limit_source_state_path": input_item["source_state_path"],
                    "input_limit_receipt_ref": input_item["receipt_ref"],
                    "input_limit_basis": input_item["input_limit_basis"],
                }
            return verified
        except (OSError, ValueError, KeyError, TypeError, sqlite3.DatabaseError):
            return None
