"""Mission-scoped public fetch and extraction reuse without sharing source-use grants.

Only the host calls this module. A durable claim precedes I/O, and an unresolved
claim is never silently dispatched again after a process restart.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from typing import Any, Callable
from urllib.parse import urlsplit, urlunsplit

from ora.state.store import canonical

from .intake import (
    HTML_PARSER_VERSION,
    PDF_PARSER_VERSION,
    XML_PARSER_VERSION,
    Block,
    EvidenceError,
    IntakeService,
    PublicSourceHTTPError,
    detected_content_type,
    extract_blocks,
    fetch_public_source,
)

_ACTIVE: set[tuple[str, str]] = set()
_ACTIVE_LOCK = threading.Lock()
_EXTRACTION_LEASE_SECONDS = 60.0
_TEXT_TYPES = {"text/html", "application/pdf", "application/xml", "text/xml",
               "application/jats+xml", "text/plain"}


def _ref(record: dict[str, Any]) -> dict[str, Any]:
    return {key: record[key] for key in ("kind", "id", "version")}


def _hash(value: Any) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _url_key(url: str) -> str:
    """Retain path and ordered query exactly; a fragment has no HTTP representation."""
    if not isinstance(url, str):
        raise EvidenceError("Public fetch URL must be text")
    parts = urlsplit(url)
    if parts.scheme.lower() != "https" or not parts.hostname or parts.username or parts.password:
        raise EvidenceError("Shared fetch requires a public HTTPS URL")
    try:
        port = parts.port
    except ValueError as exc:
        raise EvidenceError("Public fetch URL has an invalid port") from exc
    if port not in {None, 443}:
        raise EvidenceError("Shared fetch requires HTTPS port 443")
    host = parts.hostname.lower()
    authority = f"[{host}]" if ":" in host else host
    return urlunsplit(("https", authority, parts.path or "/", parts.query, ""))


def extraction_key(content_sha256: str, actual_type: str, representation: str) -> str:
    return _hash({"content_sha256": content_sha256, "actual_type": actual_type,
                  "representation": representation, "allow_partial_xml": False,
                  "html_parser_version": HTML_PARSER_VERSION,
                  "xml_parser_version": XML_PARSER_VERSION,
                  "pdf_parser_version": PDF_PARSER_VERSION})


def checked_extraction(store: Any, pointer: dict[str, Any], content: bytes,
                       actual_type: str, representation: str) -> tuple[list[Block], str, str | None]:
    """Load only parsed blocks for these exact bytes and current parser options."""
    record = store.get(**pointer)
    digest = hashlib.sha256(content).hexdigest()
    if (record is None or pointer.get("kind") != "shared_extraction"
            or record["body"].get("phase") != "completed"
            or record["body"].get("key") != extraction_key(digest, actual_type, representation)):
        raise EvidenceError("Shared extraction no longer matches document bytes and parser")
    data = store.read_blob(record["body"]["blob"]["sha256"])
    if len(data) != record["body"]["blob"]["size"]:
        raise EvidenceError("Shared extraction byte count changed")
    payload = json.loads(data)
    if not isinstance(payload, dict) or not isinstance(payload.get("blocks"), list):
        raise EvidenceError("Shared extraction has invalid blocks")
    blocks = []
    for item in payload["blocks"]:
        if not isinstance(item, dict) or not isinstance(item.get("text"), str) or not isinstance(item.get("locator"), str):
            raise EvidenceError("Shared extraction has invalid block coordinates")
        blocks.append(Block(item["text"], item["locator"]))
    return blocks, payload["parser"], payload.get("parse_failure")


class SharedPublicFetch:
    """Atomic in-flight claims, immutable bytes, and task-local adoption."""

    def __init__(self, store: Any, *, fetcher: Callable[..., dict[str, Any]] = fetch_public_source):
        self.store = store
        self.fetcher = fetcher

    def _active_key(self, record_id: str) -> tuple[str, str]:
        return str(self.store.db_path), record_id

    def _extract(self, content: bytes, actual_type: str, representation: str,
                 mission_id: str) -> dict[str, Any]:
        digest = hashlib.sha256(content).hexdigest()
        key = extraction_key(digest, actual_type, representation)
        record_id = f"shared-extraction:{key}"
        active_key = self._active_key(record_id)
        nonce = uuid.uuid4().hex

        existing = self.store.get("shared_extraction", record_id)
        if existing and existing["body"].get("phase") == "completed":
            return {"status": "completed", "ref": _ref(existing), "reused": True}
        if existing and existing["body"].get("phase") == "dispatched":
            with _ACTIVE_LOCK:
                active = active_key in _ACTIVE
            if active or existing["body"].get("lease_expires_at", 0) > self.store.clock():
                return {"status": "pending", "reason": "local_extraction_claim_in_flight"}

            def recover(tx: Any) -> dict[str, Any]:
                old = tx.get("shared_extraction", record_id)
                if (old is None or old["body"].get("phase") != "dispatched"
                        or old["body"].get("lease_expires_at", 0) > self.store.clock()):
                    return {"owner_nonce": None}
                tx.put("shared_extraction", record_id, {
                    **old["body"], "owner_nonce": nonce,
                    "lease_expires_at": self.store.clock() + _EXTRACTION_LEASE_SECONDS,
                }, expected_version=old["version"])
                return {"owner_nonce": nonce}

            claimed = self.store.transact(
                f"shared-extraction-recover:{key}:{nonce}", {"key": key, "nonce": nonce},
                recover,
            )
        else:

            def claim(tx: Any) -> dict[str, Any]:
                record = tx.get("shared_extraction", record_id)
                if record is None:
                    record = tx.put("shared_extraction", record_id,
                                    {"schema": "ora.evidence.shared_extraction.v1",
                                     "key": key, "phase": "dispatched",
                                     "owner_nonce": nonce,
                                     "lease_expires_at": self.store.clock()
                                     + _EXTRACTION_LEASE_SECONDS})
                    return {"owner_nonce": nonce, "ref": _ref(record)}
                return {"owner_nonce": None, "ref": _ref(record)}

            claimed = self.store.transact(f"shared-extraction-claim:{key}", {"key": key},
                                          claim)

        record = self.store.get("shared_extraction", record_id)
        if record["body"]["phase"] == "completed":
            return {"status": "completed", "ref": _ref(record), "reused": True}
        if claimed["owner_nonce"] != nonce:
            with _ACTIVE_LOCK:
                active = active_key in _ACTIVE
            return {"status": "pending", "reason": "local_extraction_claim_in_flight"
                    if active or record["body"].get("phase") == "dispatched"
                    else "local_extraction_claim_changed"}
        with _ACTIVE_LOCK:
            _ACTIVE.add(active_key)
        try:
            parser = "unavailable"
            failure = None
            blocks: list[Block] = []
            if actual_type in _TEXT_TYPES:
                try:
                    blocks, parser = extract_blocks(content, actual_type)
                except EvidenceError as exc:
                    failure = str(exc)
            payload = {"blocks": [{"text": item.text, "locator": item.locator} for item in blocks],
                       "parser": parser, "parse_failure": failure}
            blob = self.store.put_blob(canonical(payload).encode("utf-8"))
            body = {"schema": "ora.evidence.shared_extraction.v1", "key": key,
                    "phase": "completed", "blob": blob}

            def complete(tx: Any) -> dict[str, Any]:
                old = tx.get("shared_extraction", record_id)
                if (old is None or old["body"].get("phase") != "dispatched"
                        or old["body"].get("owner_nonce") != nonce):
                    raise EvidenceError("Shared extraction claim changed")
                return _ref(tx.put("shared_extraction", record_id, body,
                                   expected_version=old["version"]))

            pointer = self.store.transact(f"shared-extraction-complete:{key}", body,
                                          complete)
            return {"status": "completed", "ref": pointer, "reused": False}
        finally:
            with _ACTIVE_LOCK:
                _ACTIVE.discard(active_key)

    def fetch(self, *, mission_id: str, operation_id: str, url: str,
              representation: str = "ordinary_html", access_scope: str = "public",
              refresh: bool = False) -> dict[str, Any]:
        """Return completed bytes or a nonblocking pending/uncertain/failed state."""
        if not mission_id or not operation_id or representation not in {
            "ordinary_html", "rendered_html", "pdf", "ocr"
        } or access_scope != "public" or type(refresh) is not bool:
            raise EvidenceError("Invalid shared public-fetch scope or representation")
        family_key = _hash({"mission_id": mission_id, "url": _url_key(url),
                            "representation": representation, "access_scope": access_scope})
        family_id = f"shared-fetch-family:{family_key}"
        operation_record_id = f"shared-fetch-operation:{_hash(operation_id)}"
        nonce = uuid.uuid4().hex

        def claim(tx: Any) -> dict[str, Any]:
            previous = tx.get("shared_fetch_operation", operation_record_id)
            if previous is not None:
                return {"owner_nonce": None, "fetch_id": previous["body"]["fetch_id"],
                        "previous_fetch_id": None}
            family = tx.get("shared_fetch_family", family_id)
            previous_fetch_id = family["body"]["fetch_id"] if family else None
            if family is not None and not refresh:
                fetch_id = family["body"]["fetch_id"]
                owned = False
            else:
                fetch_id = f"shared-fetch:{_hash([family_key, operation_id])}"
                tx.put("shared_public_fetch", fetch_id,
                       {"schema": "ora.evidence.shared_public_fetch.v1",
                        "phase": "dispatched", "mission_id": mission_id,
                        "family_key": family_key,
                        "requested_url": url, "representation": representation,
                        "access_scope": access_scope, "owner_operation_id": operation_id})
                family_body = {"schema": "ora.evidence.shared_fetch_family.v1",
                               "fetch_id": fetch_id}
                tx.put("shared_fetch_family", family_id, family_body,
                       expected_version=family["version"] if family else 0)
                owned = True
            tx.put("shared_fetch_operation", operation_record_id,
                   {"schema": "ora.evidence.shared_fetch_operation.v1",
                    "operation_id": operation_id, "fetch_id": fetch_id,
                    "family_key": family_key, "refresh": refresh})
            return {"owner_nonce": nonce if owned else None, "fetch_id": fetch_id,
                    "previous_fetch_id": previous_fetch_id}

        claimed = self.store.transact(f"shared-fetch-claim:{operation_id}",
                                      {"family_key": family_key, "refresh": refresh},
                                      claim, mission_id=mission_id)
        fetch_id = claimed["fetch_id"]
        record = self.store.get("shared_public_fetch", fetch_id)
        active_key = self._active_key(fetch_id)
        if record["body"]["phase"] == "completed":
            return self._ready(record, mission_id, reused=True)
        if record["body"]["phase"] == "failed":
            return {"status": "failed", "reason": record["body"]["reason"],
                    "http_status": record["body"].get("http_status")}
        if claimed["owner_nonce"] != nonce:
            with _ACTIVE_LOCK:
                status = "pending" if active_key in _ACTIVE else "uncertain"
            return {"status": status, "reason": "shared_fetch_in_flight" if status == "pending"
                    else "unresolved_previous_dispatch"}
        with _ACTIVE_LOCK:
            _ACTIVE.add(active_key)
        try:
            try:
                fetched = self.fetcher(url)
            except PublicSourceHTTPError as exc:
                return self._failure(record, mission_id, "http_error", exc.status_code)
            except EvidenceError:
                # A generic transport error does not prove whether the request
                # reached the remote host. Keep the claim for explicit recovery.
                return {"status": "uncertain", "reason": "fetch_outcome_unknown"}
            except Exception:
                # A transport exception may have occurred after dispatch; never auto-retry.
                return {"status": "uncertain", "reason": "fetch_outcome_unknown"}
            content = fetched.get("content")
            if (fetched.get("status") != 200 or not isinstance(content, bytes)
                    or not content or fetched.get("requested_url") != url
                    or fetched.get("content_sha256") != hashlib.sha256(content).hexdigest()
                    or fetched.get("byte_length") != len(content)
                    or fetched.get("authenticated") is not False
                    or fetched.get("access_basis") != "public_https_unauthenticated"):
                return self._failure(record, mission_id, "invalid_public_fetch_receipt")
            blob = self.store.put_blob(content)
            actual_type = detected_content_type(content, fetched["content_type"])
            final_url = fetched.get("final_url")
            redirects = fetched.get("redirect_chain", [])
            if (not isinstance(final_url, str) or not isinstance(redirects, list)
                    or (redirects and (
                        not all(isinstance(item, dict)
                                and item.get("status") in {301, 302, 303, 307, 308}
                                for item in redirects)
                        or redirects[0].get("from_url") != url
                        or redirects[-1].get("to_url") != final_url
                        or any(left.get("to_url") != right.get("from_url")
                               for left, right in zip(redirects, redirects[1:]))))
                    or (not redirects and final_url != url)):
                return self._failure(record, mission_id, "invalid_redirect_receipt")
            final_key = _url_key(final_url)
            body = {"schema": "ora.evidence.shared_public_fetch.v1", "phase": "completed",
                    "mission_id": mission_id, "family_key": family_key,
                    "requested_url": url,
                    "representation": representation, "access_scope": access_scope,
                    "owner_operation_id": operation_id, "final_url": final_url,
                    "redirect_chain": redirects,
                    "retrieved_at": fetched["retrieved_at"],
                    "content_type": fetched["content_type"], "actual_type": actual_type,
                    "content_sha256": fetched["content_sha256"], "byte_length": len(content),
                    "access_basis": fetched["access_basis"], "authenticated": False,
                    "blob": blob}

            def complete(tx: Any) -> dict[str, Any]:
                old = tx.get("shared_public_fetch", fetch_id)
                if old is None or old["body"].get("phase") != "dispatched":
                    raise EvidenceError("Shared fetch claim changed")
                saved = tx.put("shared_public_fetch", fetch_id, body,
                               expected_version=old["version"])
                if redirects and final_key != _url_key(url):
                    alias_key = _hash({"mission_id": mission_id, "url": final_key,
                                       "representation": representation,
                                       "access_scope": access_scope})
                    alias_id = f"shared-fetch-family:{alias_key}"
                    alias = tx.get("shared_fetch_family", alias_id)
                    if alias is None:
                        tx.put("shared_fetch_family", alias_id,
                               {"schema": "ora.evidence.shared_fetch_family.v1",
                                "fetch_id": fetch_id, "redirect_alias_of": family_key})
                    elif (alias["body"].get("redirect_alias_of") == family_key
                          and alias["body"].get("fetch_id") == claimed["previous_fetch_id"]):
                        tx.put("shared_fetch_family", alias_id, {
                            **alias["body"], "fetch_id": fetch_id,
                        }, expected_version=alias["version"])
                return _ref(saved)

            pointer = self.store.transact(f"shared-fetch-complete:{operation_id}", body,
                                          complete, mission_id=mission_id)
            return self._ready(self.store.get(**pointer), mission_id, reused=False)
        finally:
            with _ACTIVE_LOCK:
                _ACTIVE.discard(active_key)

    def _failure(self, record: dict[str, Any], mission_id: str, reason: str,
                 http_status: int | None = None) -> dict[str, Any]:
        body = {**record["body"], "phase": "failed", "reason": reason,
                "http_status": http_status}

        def complete(tx: Any) -> dict[str, Any]:
            old = tx.get("shared_public_fetch", record["id"])
            if old is None or old["body"].get("phase") != "dispatched":
                raise EvidenceError("Shared fetch claim changed")
            return _ref(tx.put("shared_public_fetch", record["id"], body,
                               expected_version=old["version"]))

        self.store.transact(f"shared-fetch-failed:{record['id']}", body,
                            complete, mission_id=mission_id)
        return {"status": "failed", "reason": reason, "http_status": http_status}

    def _ready(self, record: dict[str, Any], mission_id: str, *, reused: bool) -> dict[str, Any]:
        body = record["body"]
        content = self.store.read_blob(body["blob"]["sha256"])
        if (hashlib.sha256(content).hexdigest() != body["content_sha256"]
                or len(content) != body["byte_length"]):
            raise EvidenceError("Shared fetch bytes changed")
        extracted = self._extract(content, body["actual_type"], body["representation"], mission_id)
        if extracted["status"] != "completed":
            return {"status": extracted["status"], "reason": "shared_extraction_in_flight"}
        return {"status": "completed", "shared_fetch_ref": _ref(record),
                "shared_extraction_ref": extracted["ref"],
                "reused_fetch": reused, "reused_extraction": extracted["reused"]}

    def adopt(self, intake: IntakeService, *, mission_id: str, run_id: str,
              actor_id: str, task_id: str, operation_id: str,
              hit_ref: dict[str, Any], response_ref: dict[str, Any],
              shared_fetch_ref: dict[str, Any],
              shared_extraction_ref: dict[str, Any],
              ordinary_document_ref: dict[str, Any] | None = None,
              representation: str | None = None) -> dict[str, Any]:
        """Mint a peer-local document; normal intake still decides model use."""
        record = self.store.get(**shared_fetch_ref)
        if record is None or record["body"].get("phase") != "completed":
            raise EvidenceError("Shared fetch is not completed")
        body = record["body"]
        if (body.get("mission_id") != mission_id or body.get("access_scope") != "public"
                or body.get("authenticated") is not False):
            raise EvidenceError("Shared fetch has no public access basis")
        selected_representation = representation or body.get("representation")
        if selected_representation != body.get("representation"):
            raise EvidenceError("Shared fetch representation differs from the selected source")
        if selected_representation == "browserless_rendered_html":
            ordinary = self.store.get(**ordinary_document_ref) if (
                isinstance(ordinary_document_ref, dict)
                and set(ordinary_document_ref) == {"kind", "id", "version"}
                and ordinary_document_ref.get("kind") == "public_document_fetch"
            ) else None
            if (ordinary is None
                    or self.store.get("public_document_fetch", ordinary["id"]) != ordinary
                    or any(ordinary["body"].get(key) != value for key, value in (
                        ("mission_id", mission_id), ("run_id", run_id),
                        ("actor_id", actor_id), ("task_id", task_id),
                        ("hit_ref", hit_ref), ("response_ref", response_ref),
                        ("representation", "ordinary_html"),
                    ))
                    or _url_key(ordinary["body"].get("requested_url"))
                    != _url_key(body.get("requested_url"))
                    or ordinary["body"].get("hit_rights_status") in {
                        "restricted", "not_retained",
                    }):
                raise EvidenceError("Rendered bytes need this peer's exact ordinary document")
            ordinary_content = self.store.read_blob(ordinary["body"]["blob"]["sha256"])
            if hashlib.sha256(ordinary_content).hexdigest() != ordinary["body"].get(
                "content_sha256"
            ):
                raise EvidenceError("Peer ordinary bytes changed before rendered adoption")
        elif ordinary_document_ref is not None:
            raise EvidenceError("Ordinary source reuse has no rendered-document parent")
        content = self.store.read_blob(body["blob"]["sha256"])
        if hashlib.sha256(content).hexdigest() != body["content_sha256"]:
            raise EvidenceError("Shared fetch bytes changed")
        hit = self.store.get(**hit_ref)
        if selected_representation == "browserless_rendered_html":
            from ora.mission.shared_materials import shared_hit_access

            if hit is None or not shared_hit_access(self.store, hit):
                raise EvidenceError("Rendered source lead is no longer permitted")
        hit_url = hit["body"].get("source", {}).get("original_url") if hit else None
        if (not isinstance(hit_url, str)
                or _url_key(hit_url) not in {_url_key(body["requested_url"]),
                                             _url_key(body["final_url"])}
                or (_url_key(hit_url) != _url_key(body["requested_url"])
                    and not body["redirect_chain"])):
            raise EvidenceError("Shared fetch URL differs from this peer's exact lead")
        if _url_key(hit_url) != _url_key(body["requested_url"]):
            alias_key = _hash({"mission_id": mission_id, "url": _url_key(hit_url),
                               "representation": body["representation"],
                               "access_scope": body["access_scope"]})
            alias = self.store.get("shared_fetch_family", f"shared-fetch-family:{alias_key}")
            if alias is None or alias["body"].get("fetch_id") != record["id"]:
                raise EvidenceError("Redirect alias is not bound to this exact fetch")
        checked_extraction(self.store, shared_extraction_ref, content,
                           body["actual_type"], body["representation"])
        fetched = {"status": 200, "content": content, "content_sha256": body["content_sha256"],
                   "byte_length": body["byte_length"], "content_type": body["content_type"],
                   "requested_url": hit_url, "final_url": body["final_url"],
                   "redirect_chain": body["redirect_chain"] if hit_url == body["requested_url"] else [],
                   "retrieved_at": body["retrieved_at"],
                   "access_basis": body["access_basis"], "authenticated": False,
                   "representation": selected_representation,
                   "ordinary_document_ref": ordinary_document_ref,
                   "shared_fetch_ref": shared_fetch_ref,
                   "shared_extraction_ref": shared_extraction_ref}
        return intake.save_public_document_fetch(
            operation_id, hit_ref, response_ref, mission_id=mission_id, run_id=run_id,
            actor_id=actor_id, task_id=task_id, fetched=fetched,
        )
