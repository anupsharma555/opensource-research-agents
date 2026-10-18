"""Explicit Browserless rendering after a saved ordinary public HTML fetch.

The request and uncertain boundary are durable. Rendered bytes form another copy
and pass the ordinary intake policy; they never replace the original extraction.
"""

from __future__ import annotations

import hashlib
import json
from urllib.parse import urlsplit

from ora.coordination.board import ref
from ora.evidence.intake import _EXPLICIT_USE_PROHIBITION, _LOGIN, MAX_COPY_BYTES, extract_blocks
from ora.state import StateError

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


def render_public_document(store, intake, spec, arguments, values, *, transport=None):
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
        try:
            envelope = json.loads(response.body)
            # Browserless documents a {data, type} function return, while its
            # 2.x API may write only data as the HTTP response. Both shapes
            # must still carry the same exact navigation fields.
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
        fetched = {
            "status": 200, "content": content, "content_sha256": hashlib.sha256(content).hexdigest(),
            # The HTTP response is a JSON envelope; its html field is the source copy.
            "byte_length": len(content), "content_type": "text/html",
            "requested_url": url, "final_url": final_url, "redirect_chain": redirects,
            "access_basis": "public_https_unauthenticated", "authenticated": False,
            "retrieved_at": store.now(), "representation": "browserless_rendered_html",
            "ordinary_document_ref": pointer,
        }
        saved_ref = intake.save_public_document_fetch(operation_id, body["hit_ref"], body["response_ref"],
            mission_id=spec.mission_id, run_id=spec.run_id, actor_id=spec.actor_id,
            task_id=spec.proposal_ref["id"], fetched=fetched)
        saved = store.get(**saved_ref)
    inspected = intake.inspect_public_document(operation_id + ":inspect", ref(saved),
        mission_id=spec.mission_id, run_id=spec.run_id, actor_id=spec.actor_id, task_id=spec.proposal_ref["id"])
    copy = inspected["copy_ref"]
    return {"status": "inspected" if copy else "source_unavailable", "document_ref": ref(saved),
            "ordinary_document_ref": pointer, "representation": "browserless_rendered_html",
            "redirect_verification": "navigation_response_and_chain_checked",
            "classification": inspected["classification"], "copy_ref": copy,
            **({"source_id": copy["id"], "source_version": copy["version"]} if copy else {}),
            "next_action": "Read exact retained text with read_source_window before citing"}, (copy,) if copy else ()
