"""Explicit Browserless rendering after a saved ordinary public HTML fetch.

The request and uncertain boundary are durable. Rendered bytes form another copy
and pass the ordinary intake policy; they never replace the original extraction.
"""

from __future__ import annotations

import hashlib
import json
import threading
import uuid
from urllib.parse import urlsplit

from ora.coordination.board import ref
from ora.evidence.intake import _EXPLICIT_USE_PROHIBITION, _LOGIN, MAX_COPY_BYTES, extract_blocks
from ora.evidence.shared_fetch import SharedPublicFetch, _url_key
from ora.state import StateConflict, StateError
from ora.state.store import canonical

from .providers import _selected_public_url, http_transport

# The Content API returns HTML without the page's final URL or redirect history.
# This fixed Function API program returns both with the rendered HTML. It does
# not accept model-authored code, browser credentials, or an unblock option.
_RENDER_FUNCTION = """export default async ({ page, context }) => {
  const selected = new URL(context.url);
  await page.setBypassServiceWorker(true);
  await page.setRequestInterception(true);
  page.on('request', request => {
    if (request.isInterceptResolutionHandled()) return;
    if (request.isNavigationRequest()) {
      let target;
      try { target = new URL(request.url()); } catch (_) { request.abort(); return; }
      if (target.protocol !== 'https:' || target.hostname !== selected.hostname ||
          (target.port && target.port !== '443')) {
        request.abort();
        return;
      }
    }
    request.continue();
  });
  const response = await page.goto(context.url, {
    waitUntil: 'networkidle2', timeout: 20000
  });
  if (!response) throw new Error('No navigation response');
  const request = response.request();
  const requests = [...request.redirectChain(), request];
  const redirects = [];
  for (let index = 0; index + 1 < requests.length; index++) {
    const redirectResponse = await requests[index].response();
    if (!redirectResponse) throw new Error('Missing redirect response');
    redirects.push({from_url: requests[index].url(),
                    to_url: requests[index + 1].url(),
                    status: redirectResponse.status()});
  }
  return {data: {html: await page.content(),
                 requested_url: requests[0].url(),
                 final_url: page.url(),
                 navigation_url: response.url(),
                 navigation_status: response.status(),
                 redirect_chain: redirects}, type: 'application/json'};
};"""

_RENDER_ACTIVE: set[tuple[str, str]] = set()
_RENDER_LOCK = threading.Lock()


def _hash(value: object) -> str:
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def _verified_rendered(response, url: str, store) -> dict:
    """Accept only an exact same-host navigation receipt and bounded HTML bytes."""
    try:
        envelope = json.loads(response.body)
        rendered = envelope["data"] if "data" in envelope else envelope
        content = rendered["html"].encode("utf-8")
        final_url = _selected_public_url(rendered["final_url"])
        redirects = rendered["redirect_chain"]
        selected_host = urlsplit(url).hostname
        if (rendered["requested_url"] != url or rendered["navigation_url"] != final_url
                or rendered["navigation_status"] != 200 or not isinstance(redirects, list)):
            raise ValueError("Rendered navigation lacks a matching final response")
        if urlsplit(final_url).hostname != selected_host:
            raise ValueError("Rendered page left the selected source host")
        previous = url
        for leg in redirects:
            if (not isinstance(leg, dict) or leg.get("from_url") != previous
                    or type(leg.get("status")) is not int
                    or leg["status"] not in {301, 302, 303, 307, 308}):
                raise ValueError("Rendered redirect provenance is incomplete")
            previous = _selected_public_url(leg["to_url"])
            if urlsplit(previous).hostname != selected_host:
                raise ValueError("Rendered redirect left the selected source host")
        if previous != final_url:
            raise ValueError("Rendered final URL does not match its redirect chain")
    except (KeyError, TypeError, ValueError, UnicodeError, AttributeError) as exc:
        raise StateError("Browserless returned unverifiable rendered page provenance") from exc
    if not content or len(content) > MAX_COPY_BYTES:
        raise StateError("Rendered response exceeds the retained document bound")
    return {
        "status": 200, "content": content, "content_sha256": hashlib.sha256(content).hexdigest(),
        "byte_length": len(content), "content_type": "text/html", "requested_url": url,
        "final_url": final_url, "redirect_chain": redirects,
        "access_basis": "public_https_unauthenticated", "authenticated": False,
        "retrieved_at": store.now(), "representation": "browserless_rendered_html",
    }


class SharedRenderedFetch:
    """One mission/provider-bound render attempt with task-local adoption."""

    def __init__(self, store, *, transport=None):
        self.store = store
        self.transport = transport or http_transport
        self.shared = SharedPublicFetch(store)

    def fetch(self, *, mission_id: str, operation_id: str, url: str,
              ordinary_document_ref: dict, reason: str, endpoint: str,
              token: str) -> dict:
        selected_url = _selected_public_url(url)
        family_key = _hash({
            "mission_id": mission_id, "url": _url_key(selected_url),
            "representation": "browserless_rendered_html", "endpoint": endpoint,
            "credential_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "program_sha256": hashlib.sha256(_RENDER_FUNCTION.encode()).hexdigest(),
        })
        family_id = f"shared-render-family:{family_key}"
        operation_record_id = f"shared-render-operation:{_hash([mission_id, operation_id])}"
        nonce = uuid.uuid4().hex
        request = {"mission_id": mission_id, "family_key": family_key,
                   "operation_id": operation_id, "ordinary_document_ref": ordinary_document_ref,
                   "reason": reason}

        def claim(tx):
            previous = tx.get("shared_render_operation", operation_record_id)
            if previous is not None:
                return {"owner_nonce": None, "fetch_id": previous["body"]["fetch_id"]}
            family = tx.get("shared_render_family", family_id)
            if family is None:
                fetch_id = f"shared-render:{_hash([family_key, operation_id])}"
                tx.put("shared_public_fetch", fetch_id, {
                    "schema": "ora.evidence.shared_public_fetch.v1", "phase": "dispatched",
                    "mission_id": mission_id, "family_key": family_key,
                    "requested_url": selected_url, "representation": "browserless_rendered_html",
                    "access_scope": "public", "owner_operation_id": operation_id,
                })
                tx.put("shared_render_family", family_id, {
                    "schema": "ora.evidence.shared_render_family.v1", "fetch_id": fetch_id,
                    "mission_id": mission_id,
                })
                owner_nonce = nonce
            else:
                fetch_id = family["body"]["fetch_id"]
                owner_nonce = None
            tx.put("shared_render_operation", operation_record_id, {
                "schema": "ora.evidence.shared_render_operation.v1",
                "operation_id": operation_id, "fetch_id": fetch_id,
                "family_key": family_key,
            })
            return {"owner_nonce": owner_nonce, "fetch_id": fetch_id}

        claimed = self.store.transact(
            f"shared-render-claim:{mission_id}:{operation_id}", request, claim,
            mission_id=mission_id,
        )
        record = self.store.get("shared_public_fetch", claimed["fetch_id"])
        if record is None or record["body"].get("mission_id") != mission_id:
            raise StateError("Shared rendered fetch lost its mission scope")
        if record["body"]["phase"] == "completed":
            return self.shared._ready(record, mission_id, reused=True)
        if record["body"]["phase"] == "failed":
            return {"status": "unavailable", "reason": record["body"]["reason"],
                    "http_status": record["body"].get("http_status")}
        active_key = str(self.store.db_path), record["id"]
        if claimed["owner_nonce"] != nonce:
            with _RENDER_LOCK:
                active = active_key in _RENDER_ACTIVE
            return {"status": "pending" if active else "uncertain",
                    "reason": "shared_render_in_flight" if active else "unresolved_previous_dispatch"}
        with _RENDER_LOCK:
            _RENDER_ACTIVE.add(active_key)
        try:
            response = self.transport("POST", endpoint + "/function", body={
                "code": _RENDER_FUNCTION, "context": {"url": selected_url},
            }, headers={"Authorization": f"Bearer {token}",
                        "Content-Type": "application/json"}, timeout=30)
            if response.status != 200:
                body = {**record["body"], "phase": "failed", "reason": "provider_http_error",
                        "http_status": response.status}
                def fail(tx):
                    old = tx.get("shared_public_fetch", record["id"])
                    if old is None or old["body"].get("phase") != "dispatched":
                        raise StateConflict("Shared render outcome changed")
                    return ref(tx.put("shared_public_fetch", record["id"], body,
                                      expected_version=old["version"]))
                self.store.transact(f"shared-render-failed:{record['id']}", body, fail,
                                    mission_id=mission_id)
                return {"status": "unavailable", "reason": "provider_http_error",
                        "http_status": response.status}
            fetched = _verified_rendered(response, selected_url, self.store)
            blob = self.store.put_blob(fetched["content"])
            body = {**record["body"], "phase": "completed",
                    **{key: value for key, value in fetched.items() if key != "content"},
                    "actual_type": "text/html", "blob": blob,
                    "navigation_verification": "navigation_response_and_chain_checked",
                    "authenticated": False}

            def complete(tx):
                old = tx.get("shared_public_fetch", record["id"])
                if old is None or old["body"].get("phase") != "dispatched":
                    raise StateConflict("Shared render claim changed")
                return ref(tx.put("shared_public_fetch", record["id"], body,
                                  expected_version=old["version"]))

            pointer = self.store.transact(f"shared-render-complete:{record['id']}", body,
                                          complete, mission_id=mission_id)
            return self.shared._ready(self.store.get(**pointer), mission_id, reused=False)
        finally:
            with _RENDER_LOCK:
                _RENDER_ACTIVE.discard(active_key)


def _inspect_rendered_document(store, intake, spec, ordinary_ref: dict,
                               rendered_document: dict) -> tuple[dict, tuple]:
    inspected = intake.inspect_public_document(
        f"web-render:{spec.work_id}:inspect", ref(rendered_document),
        mission_id=spec.mission_id, run_id=spec.run_id, actor_id=spec.actor_id,
        task_id=spec.proposal_ref["id"],
    )
    copy = inspected["copy_ref"]
    return {
        "status": "inspected" if copy else "source_unavailable",
        "document_ref": ref(rendered_document),
        "ordinary_document_ref": ordinary_ref,
        "representation": "browserless_rendered_html",
        "redirect_verification": "navigation_response_and_chain_checked",
        "classification": inspected["classification"], "copy_ref": copy,
        **({"source_id": copy["id"], "source_version": copy["version"]} if copy else {}),
        "next_action": "Read exact retained text with read_source_window before citing",
    }, (copy,) if copy else ()


def render_public_document(store, intake, spec, arguments, values, *, transport=None,
                           shared_enabled: bool = False):
    from ora.mission.shared_materials import shared_hit_access

    pointer = arguments["ordinary_document_ref"]
    document = store.get(**pointer)
    if document is None or document["kind"] != "public_document_fetch":
        raise StateError("Render requires an exact saved ordinary document reference")
    body = document["body"]
    if (any(body.get(key) != value for key, value in (
        ("mission_id", spec.mission_id), ("run_id", spec.run_id),
        ("actor_id", spec.actor_id), ("task_id", spec.proposal_ref["id"]),
    )) or body.get("representation", "ordinary_html") != "ordinary_html"
            or not arguments["reason"].strip()):
        raise StateError("Render requires this peer task's ordinary extraction and its remaining gap")
    hit = store.get(**body["hit_ref"])
    if hit is None or not shared_hit_access(store, hit) or body.get("hit_rights_status") in {"restricted", "not_retained"}:
        raise StateError("Original source permissions do not allow another extraction")
    original = store.read_blob(body["blob"]["sha256"])
    if hashlib.sha256(original).hexdigest() != body.get("content_sha256"):
        raise StateError("Ordinary source bytes no longer match their saved fetch")
    if body.get("detected_content_type") != "text/html":
        raise StateError("Rendering requires an ordinary HTML extraction")
    blocks, _ = extract_blocks(original, body["detected_content_type"])
    text = "\n".join(block.text for block in blocks)
    if _LOGIN.search(text[:3000]) or _EXPLICIT_USE_PROHIBITION.search(original.decode(errors="ignore")):
        raise StateError("Rendering must not bypass login or source-use restrictions")
    endpoint = values.get("OSRA_BROWSERLESS_BASE_URL", "").rstrip("/")
    token = values.get("BROWSERLESS_API_KEY")
    if not endpoint or not token:
        return {"status": "unavailable", "reason": "optional_browserless_not_configured"}, ()
    parsed = urlsplit(endpoint)
    if (parsed.scheme != "https" or not parsed.hostname
            or not parsed.hostname.endswith(".browserless.io") or parsed.path not in {"", "/"}
            or parsed.port not in {None, 443} or parsed.username or parsed.password
            or parsed.query or parsed.fragment):
        raise StateError("Browserless requires an operator-configured HTTPS provider base URL")
    url = _selected_public_url(body["requested_url"])
    operation_id = f"web-render:{spec.work_id}"
    if shared_enabled:
        shared = SharedRenderedFetch(store, transport=transport)
        chosen = shared.fetch(
            mission_id=spec.mission_id, operation_id=operation_id, url=url,
            ordinary_document_ref=pointer, reason=arguments["reason"],
            endpoint=endpoint, token=token,
        )
        if chosen["status"] != "completed":
            return chosen, ()
        saved_ref = shared.shared.adopt(
            intake, mission_id=spec.mission_id, run_id=spec.run_id,
            actor_id=spec.actor_id, task_id=spec.proposal_ref["id"],
            operation_id=operation_id, hit_ref=body["hit_ref"],
            response_ref=body["response_ref"],
            shared_fetch_ref=chosen["shared_fetch_ref"],
            shared_extraction_ref=chosen["shared_extraction_ref"],
            ordinary_document_ref=pointer, representation="browserless_rendered_html",
        )
        return _inspect_rendered_document(store, intake, spec, pointer, store.get(**saved_ref))
    saved = store.get("public_document_fetch", f"public-document-fetch:{operation_id}")
    if saved is None:
        known_outcome = store.get("web_render_outcome", operation_id)
        if known_outcome:
            return known_outcome["body"]["result"], ()
        marker = store.get("web_render_dispatch", operation_id)
        if marker:
            return {"status": "uncertain", "dispatch_ref": ref(marker), "next_action": "Reconcile the saved external attempt; do not replay it blindly"}, ()
        request = {"mission_id": spec.mission_id, "ordinary_document_ref": pointer,
                   "reason": arguments["reason"], "requested_url": url}
        store.transact(operation_id + ":dispatch", request, lambda tx: tx.put("web_render_dispatch", operation_id, request), mission_id=spec.mission_id)
        # No login state, stealth, unblock, summaries, or arbitrary page scripts.
        response = (transport or http_transport)("POST", endpoint + "/function", body={
            "code": _RENDER_FUNCTION, "context": {"url": url},
        }, headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"}, timeout=30)
        if response.status != 200:
            result = {"status": "unavailable", "http_status": response.status}
            store.transact(operation_id + ":outcome", result,
                           lambda tx: tx.put("web_render_outcome", operation_id,
                                             {"mission_id": spec.mission_id, "result": result}),
                           mission_id=spec.mission_id)
            return result, ()
        fetched = {**_verified_rendered(response, url, store),
                   "ordinary_document_ref": pointer}
        saved_ref = intake.save_public_document_fetch(operation_id, body["hit_ref"], body["response_ref"],
            mission_id=spec.mission_id, run_id=spec.run_id, actor_id=spec.actor_id,
            task_id=spec.proposal_ref["id"], fetched=fetched)
        saved = store.get(**saved_ref)
    return _inspect_rendered_document(store, intake, spec, pointer, saved)
