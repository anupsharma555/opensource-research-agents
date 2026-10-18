"""Discover and fetch figure images authored in an exact retained source copy.

An asset is a pointer into parent HTML/XML bytes, not an arbitrary URL lead.
Captions are untrusted source content. Image fetches remain separate documents
and derived OCR/vision never become checked passages.
"""

from __future__ import annotations

import hashlib
import re
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from typing import Callable
from urllib.parse import parse_qs, urljoin, urlparse

from ora.coordination.board import ref
from ora.state.source_lineage import scoped_note_source_input
from ora.state.store import canonical

from .hit_use import hit_allows_reinspection
from .intake import MAX_COPY_BYTES, detected_content_type, fetch_public_source
from .library import EvidenceError

MAX_ASSETS = 100
MAX_CAPTION = 2000


def _sha(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def _scope(scope: dict) -> dict:
    keys = ("mission_id", "run_id", "actor_id", "task_id")
    if any(not isinstance(scope.get(key), str) or not scope[key] for key in keys):
        raise EvidenceError("Exact figure peer-task scope is required")
    return {key: scope[key] for key in keys}


def _url(base: str, authored: str) -> str | None:
    if not isinstance(authored, str) or not authored or authored.startswith(("//", "#")):
        return None
    parsed_raw = urlparse(authored)
    if parsed_raw.scheme and parsed_raw.scheme != "https":
        return None
    candidate = urljoin(base, authored)
    parsed = urlparse(candidate)
    try:
        port = parsed.port
    except ValueError:
        return None
    if (parsed.scheme != "https" or not parsed.hostname or parsed.username
            or parsed.password or port not in {None, 443} or parsed.fragment):
        return None
    return candidate


def _text(value: str) -> str:
    return " ".join(value.split())


def _caption_fields(caption: str) -> dict:
    truncated = len(caption) > MAX_CAPTION
    return {
        "caption": caption[:200] if truncated else caption,
        "caption_truncated": truncated,
        "caption_source_length": len(caption),
        "caption_source_sha256": _sha(caption.encode()),
    }


class _Figures(HTMLParser):
    def __init__(self, base: str, *, has_base_element: bool):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.has_base_element = has_base_element
        self.depth = 0
        self.figure: dict | None = None
        self.caption_levels: list[int] = []
        self.anchor_stack: list[tuple[int, str, str | None]] = []
        self.items: list[dict] = []
        self.ordinal = 0

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.handle_starttag(tag, attrs)
        if tag not in {"img", "br", "hr", "meta", "link", "input", "area", "source"}:
            self.handle_endtag(tag)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        self.depth += 1
        attributes = dict(attrs)
        classes = set((attributes.get("class") or "").split())
        if self.figure is None and (tag == "figure" or tag == "div" and "figure" in classes):
            self.ordinal += 1
            self.figure = {"depth": self.depth, "locator": f"html.figure.{self.ordinal}",
                           "doi": attributes.get("data-doi"), "images": [],
                           "links": [], "caption_parts": []}
        if self.figure is None:
            return
        if tag == "a" and attributes.get("href"):
            self.anchor_stack.append((self.depth, attributes["href"], attributes.get("data-uri")))
            self.figure["links"].append(attributes["href"])
        if tag == "img" and attributes.get("src"):
            self.figure["images"].append((attributes["src"],
                                          self.anchor_stack[-1][1] if self.anchor_stack else None,
                                          self.anchor_stack[-1][2] if self.anchor_stack else None))
        if tag == "figcaption" or classes.intersection({"figcaption", "caption_target"}):
            self.caption_levels.append(self.depth)
        if tag in {"img", "br", "hr", "meta", "link", "input", "area", "source"}:
            self.depth -= 1

    def handle_data(self, data: str) -> None:
        if self.figure is not None and self.caption_levels:
            self.figure["caption_parts"].append(data)

    def handle_endtag(self, tag: str) -> None:
        if self.figure is not None:
            if self.anchor_stack and self.anchor_stack[-1][0] == self.depth:
                self.anchor_stack.pop()
            if self.caption_levels and self.caption_levels[-1] == self.depth:
                self.caption_levels.pop()
            if self.depth == self.figure["depth"]:
                figure = self.figure
                self.figure = None
                caption = _text(" ".join(figure["caption_parts"]))
                if caption and figure["images"]:
                    raw = figure["images"][0][1] or figure["images"][0][0]
                    doi = figure["doi"]
                    for link in figure["links"]:
                        query = parse_qs(urlparse(urljoin(self.base, link)).query)
                        if ("download" in link and "size=large" in link
                                and (not doi or query.get("id") == [doi])):
                            raw = link
                            break
                    authored_ids = [parse_qs(urlparse(urljoin(self.base, item)).query).get("id")
                                    for item in (*figure["links"], figure["images"][0][0])]
                    data_uri = figure["images"][0][2]
                    if (doi and (any(ids and ids != [doi] for ids in authored_ids)
                                 or data_uri and data_uri != doi)):
                        self.caption_levels.clear()
                        self.anchor_stack.clear()
                        self.depth = max(0, self.depth - 1)
                        return
                    resolved = (None if (len(figure["images"]) != 1
                                or len(caption) > MAX_CAPTION
                                or self.has_base_element and not urlparse(raw).scheme)
                                else _url(self.base, raw))
                    if len(self.items) <= MAX_ASSETS:
                        self.items.append({"locator": figure["locator"], "figure_id": doi,
                                           **_caption_fields(caption), "raw_link": raw,
                                           "image_url": resolved,
                                           "link_state": (
                                               "unresolved_caption_too_long" if len(caption) > MAX_CAPTION
                                               else "fetchable" if resolved else "unresolved")})
                self.caption_levels.clear()
                self.anchor_stack.clear()
        self.depth = max(0, self.depth - 1)


def _xml_assets(content: bytes, base: str) -> list[dict]:
    try:
        root = ET.fromstring(content)
    except ET.ParseError as exc:
        raise EvidenceError("Exact source XML cannot be parsed for figures") from exc
    items = []
    for element in root.iter():
        tag = element.tag.rsplit("}", 1)[-1]
        if tag not in {"fig", "table-wrap"}:
            continue
        graphics = [child for child in element.iter()
                    if child.tag.rsplit("}", 1)[-1] == "graphic"]
        graphic = graphics[0] if graphics else None
        caption = next((child for child in element
                        if child.tag.rsplit("}", 1)[-1] == "caption"), None)
        if graphic is None or caption is None:
            continue
        raw = next((value for key, value in graphic.attrib.items()
                    if key.rsplit("}", 1)[-1] == "href"), None)
        if not raw:
            continue
        figure_id = element.attrib.get("id")
        locator = f"xml.{tag}.{figure_id or len(items) + 1}"
        label = next((" ".join(child.itertext()) for child in element
                      if child.tag.rsplit("}", 1)[-1] == "label"), "")
        description = _text(f"{label} {' '.join(caption.itertext())}")
        if not description:
            continue
        url = _url(base, raw) if len(graphics) == 1 and len(description) <= MAX_CAPTION else None
        items.append({"locator": locator, "figure_id": figure_id,
                      **_caption_fields(description), "raw_link": raw,
                      "image_url": url,
                      "link_state": (
                          "unresolved_caption_too_long" if len(description) > MAX_CAPTION
                          else "fetchable" if url else "unresolved")})
        if len(items) > MAX_ASSETS:
            break
    return items


def extract_source_assets(content: bytes, mime_type: str, base: str) -> list[dict]:
    """Only authored figure containers with captions become selectable assets."""
    if not content or len(content) > MAX_COPY_BYTES:
        raise EvidenceError("Figure source bytes are missing or too large")
    if mime_type == "text/html":
        source = content.decode("utf-8", errors="replace")
        parser = _Figures(base, has_base_element=bool(re.search(
            r"<base\b[^>]*\bhref\s*=", source, flags=re.I)))
        parser.feed(source)
        parser.close()
        return parser.items
    if mime_type in {"application/xml", "text/xml", "application/jats+xml"}:
        return _xml_assets(content, base)
    raise EvidenceError("Figure discovery supports retained HTML/XML only")


def task_asset_tool_available(store, control: dict) -> bool:
    """Advertise asset tools only for a permitted retained HTML/XML task input."""
    from ora.mission.context import WorkingContext

    body = control["body"]
    scope = _scope(body)
    for pointer in body.get("source_refs", []):
        if not isinstance(pointer, dict) or pointer.get("kind") != "source_copy":
            continue
        copy = scoped_note_source_input(
            store, mission_id=scope["mission_id"], run_id=scope["run_id"],
            actor_id=scope["actor_id"], task_id=scope["task_id"], pointer=pointer,
        )
        if copy is None or not copy["body"].get("retain_bytes"):
            continue
        if copy["body"].get("mime_type") not in {
            "text/html", "application/xml", "text/xml", "application/jats+xml",
        }:
            continue
        status = WorkingContext(store)._source_status(copy)
        if (status["selected_model_input_status"] == "permitted"
                and status["current_model_input_status"] == "permitted"):
            return True
    return False


def current_parent_use(store, copy_ref: dict, *, scope: dict) -> tuple[dict, bytes]:
    scope = _scope(scope)
    from ora.mission.context import WorkingContext
    if (not isinstance(copy_ref, dict) or set(copy_ref) != {"kind", "id", "version"}
            or copy_ref.get("kind") != "source_copy"):
        raise EvidenceError("Exact parent source copy is required")
    copy = store.get(**copy_ref)
    if copy is None or ref(copy) != copy_ref:
        raise EvidenceError("Figure parent source copy is missing")
    body = copy["body"]
    if scoped_note_source_input(store, mission_id=scope["mission_id"],
            run_id=scope["run_id"], actor_id=scope["actor_id"],
            task_id=scope["task_id"], pointer=copy_ref) != copy:
        raise EvidenceError("Figure parent is not this task's selected source")
    status = WorkingContext(store)._source_status(copy)
    if (status["selected_model_input_status"] != "permitted"
            or status["current_model_input_status"] != "permitted"):
        raise EvidenceError("Current parent source use does not permit figure delivery")
    document_ref = body.get("document_ref")
    if isinstance(document_ref, dict):
        document = store.get(**document_ref)
        origin = document["body"] if document else {}
        hit_ref = origin.get("hit_ref")
        response_ref = origin.get("response_ref")
        hit = store.get(**hit_ref) if isinstance(hit_ref, dict) else None
        response = store.get(**response_ref) if isinstance(response_ref, dict) else None
        backing_store = getattr(store, "store", store)
        if hasattr(store, "db") and hit and response:
            linked = store.db.execute(
                "SELECT 1 FROM links WHERE source_kind='retrieval_hit' "
                "AND source_id=? AND source_version=? AND target_kind=? "
                "AND target_id=? AND target_version=? AND relation='derived_from' LIMIT 1",
                (hit_ref["id"], hit_ref["version"], response_ref["kind"],
                 response_ref["id"], response_ref["version"]),
            ).fetchone() is not None
        else:
            linked = bool(hit and response and any(
                link["relation"] == "derived_from" and link["target"] == response_ref
                for link in backing_store.links(hit)))
        if (document is None or origin.get("origin_kind") == "parent_source_asset"
                or origin.get("content_sha256") != body.get("content_sha256")
                or origin.get("final_url") != body.get("reference")
                or hit is None or response is None or not linked
                or hit != store.get("retrieval_hit", hit["id"])
                or hit["body"].get("source", {}).get("original_url") != origin.get("requested_url")
                or hit["body"].get("source", {}).get("rights_status") in {
                    "restricted", "not_retained"}
                or not hit_allows_reinspection(store, hit)):
            raise EvidenceError("Figure parent lost its permitted original lead")
    elif isinstance(body.get("navigation_ref"), dict):
        navigation = store.get(**body["navigation_ref"])
        if (navigation is None
                or navigation["body"].get("content_sha256") != body.get("content_sha256")
                or navigation["body"].get("final_url") != body.get("reference")):
            raise EvidenceError("Figure navigation parent lost its exact origin")
    blob = body.get("blob")
    if not body.get("retain_bytes") or not isinstance(blob, dict):
        raise EvidenceError("Figure parent has no retained exact bytes")
    backing_store = getattr(store, "store", store)
    content = backing_store.read_blob(blob["sha256"])
    if _sha(content) != body.get("content_sha256"):
        raise EvidenceError("Figure parent bytes disagree with retained copy")
    return copy, content


def list_source_assets(store, copy_ref: dict, *, scope: dict,
                       offset: int = 0, limit: int = 10) -> dict:
    """Persist exact candidates after current parent use and byte verification."""
    scope = _scope(scope)
    if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 20:
        raise EvidenceError("Figure candidate page is outside its bound")
    copy, content = current_parent_use(store, copy_ref, scope=scope)
    found = extract_source_assets(content, copy["body"]["mime_type"],
                                  copy["body"]["reference"])
    scan_truncated = len(found) > MAX_ASSETS
    candidates = found[:MAX_ASSETS]
    output = []
    for candidate in candidates[offset:offset + limit]:
        identity = _sha(canonical([copy_ref, scope, candidate]).encode())[:32]
        body = {"schema": "ora.evidence.source_asset.v1", **scope,
                "parent_copy_ref": copy_ref,
                "parent_sha256": copy["body"]["content_sha256"],
                **candidate,
                "caption_sha256": _sha(candidate["caption"].encode())}
        asset_id = f"source-asset:{identity}"

        def save(tx, *, record_id=asset_id, record_body=body):
            parent, _ = current_parent_use(tx, copy_ref, scope=scope)
            prior = tx.get("source_asset", record_id)
            if prior is not None:
                if prior["body"] != record_body:
                    raise EvidenceError("Figure asset identity changed")
                return ref(prior)
            record = tx.put("source_asset", record_id, record_body)
            tx.link(record, parent, "derived_from")
            return ref(record)

        pointer = store.transact(f"source-asset-discover:{asset_id}", body, save,
                                 mission_id=scope["mission_id"])
        output.append({"asset_ref": pointer, "locator": candidate["locator"],
                       "figure_id": candidate["figure_id"], "caption": candidate["caption"],
                       "caption_truncated": candidate["caption_truncated"],
                       "caption_source_length": candidate["caption_source_length"],
                       "caption_source_sha256": candidate["caption_source_sha256"],
                       "link_state": candidate["link_state"],
                       "image_url": candidate["image_url"]})
    next_offset = offset + len(output)
    return {
        "assets": output, "offset": offset, "limit": limit,
        "total_retained_candidates": len(candidates),
        "scan_limit": MAX_ASSETS, "scan_truncated": scan_truncated,
        "has_more": next_offset < len(candidates),
        "next_request": ({"source_id": copy_ref["id"],
                          "source_version": copy_ref["version"],
                          "offset": next_offset, "limit": limit}
                         if next_offset < len(candidates) else None),
    }


def current_asset_use(store, asset_ref: dict, *, scope: dict) -> tuple[dict, dict]:
    """Reparse the parent so an asset cannot invent its URL, caption or locator."""
    scope = _scope(scope)
    if (not isinstance(asset_ref, dict) or set(asset_ref) != {"kind", "id", "version"}
            or asset_ref.get("kind") != "source_asset"):
        raise EvidenceError("Exact discovered source asset is required")
    asset = store.get(**asset_ref)
    if asset is None or ref(asset) != asset_ref or any(
            asset["body"].get(key) != value for key, value in scope.items()):
        raise EvidenceError("Figure asset belongs to another peer task")
    body = asset["body"]
    parent, content = current_parent_use(store, body["parent_copy_ref"], scope=scope)
    if parent["body"]["content_sha256"] != body["parent_sha256"]:
        raise EvidenceError("Figure parent hash changed")
    parsed = extract_source_assets(content, parent["body"]["mime_type"],
                                   parent["body"]["reference"])
    match = next((item for item in parsed if item["locator"] == body["locator"]), None)
    if (match is None or any(match.get(key) != body.get(key) for key in
            ("figure_id", "caption", "caption_truncated", "caption_source_length",
             "caption_source_sha256", "raw_link", "image_url", "link_state"))
            or _sha(body["caption"].encode()) != body["caption_sha256"]):
        raise EvidenceError("Figure URL or caption disagrees with parent bytes")
    return asset, parent


def document_copy_reference_matches(store, document: dict, reference: str) -> bool:
    """Validate saved authored URL lineage without granting current model use."""
    body = document["body"]
    if body.get("origin_kind") != "parent_source_asset":
        return body.get("final_url") == reference
    try:
        asset = store.get(**body["parent_asset_ref"])
        parent = store.get(**body["parent_copy_ref"])
        parent_body = parent["body"]
        backing_store = getattr(store, "store", store)
        content = backing_store.read_blob(parent_body["blob"]["sha256"])
        parsed = extract_source_assets(content, parent_body["mime_type"],
                                       parent_body["reference"])
        candidate = next(item for item in parsed
                         if item["locator"] == asset["body"]["locator"])
    except (EvidenceError, KeyError, TypeError, ValueError, StopIteration, AttributeError):
        return False
    return bool(
        asset is not None and parent is not None
        and all(asset["body"].get(key) == body.get(key) for key in (
            "mission_id", "run_id", "actor_id", "task_id"))
        and asset["body"].get("link_state") == "fetchable"
        and parent_body.get("content_sha256") == _sha(content)
        and reference == body.get("requested_url") == asset["body"].get("image_url")
        and body.get("parent_copy_ref") == asset["body"].get("parent_copy_ref")
        and body.get("parent_sha256") == asset["body"].get("parent_sha256")
        and body.get("caption_sha256") == asset["body"].get("caption_sha256")
        and all(candidate.get(key) == asset["body"].get(key) for key in (
            "figure_id", "caption", "caption_truncated", "caption_source_length",
            "caption_source_sha256", "raw_link", "image_url", "link_state",
        ))
    )


def save_source_asset_fetch(store, asset_ref: dict, fetched: dict, *, scope: dict,
                            operation_id: str) -> dict:
    """Save an independently fetched image with explicit parent-asset provenance."""
    scope = _scope(scope)
    asset, _ = current_asset_use(store, asset_ref, scope=scope)
    asset_body = asset["body"]
    content = fetched.get("content")
    if (asset_body["link_state"] != "fetchable" or not isinstance(content, bytes)
            or not content or len(content) > MAX_COPY_BYTES
            or fetched.get("status") != 200
            or fetched.get("requested_url") != asset_body["image_url"]
            or fetched.get("content_sha256") != _sha(content)
            or fetched.get("byte_length") != len(content)
            or not isinstance(fetched.get("retrieved_at"), str)
            or not isinstance(fetched.get("final_url"), str)
            or not isinstance(fetched.get("content_type"), str)):
        raise EvidenceError("Figure fetch differs from exact authored image link")
    mime = detected_content_type(content, fetched["content_type"])
    if mime not in {"image/png", "image/jpeg"}:
        raise EvidenceError("Figure asset fetch must return PNG or JPEG bytes")
    body = {"schema": "ora.evidence.public_document_fetch.v1", **scope,
            "origin_kind": "parent_source_asset", "parent_asset_ref": asset_ref,
            "parent_copy_ref": asset_body["parent_copy_ref"],
            "parent_sha256": asset_body["parent_sha256"],
            "caption_sha256": asset_body["caption_sha256"],
            "operation_id": operation_id, "status": 200,
            "requested_url": fetched["requested_url"], "final_url": fetched["final_url"],
            "redirect_chain": fetched.get("redirect_chain", []),
            "checked_at": fetched["retrieved_at"],
            "content_type": fetched["content_type"], "detected_content_type": mime,
            "content_sha256": _sha(content), "byte_length": len(content),
            "access_basis": fetched.get("access_basis"),
            "authenticated": fetched.get("authenticated"),
            "hit_rights_status": "unknown", "representation": "ordinary_html",
            "ordinary_document_ref": None, "blob": store.put_blob(content)}
    record_id = f"public-document-fetch:{operation_id}"
    binding_key = f"{asset_ref['id']}:{asset_ref['version']}"

    def save(tx):
        current_asset_use(tx, asset_ref, scope=scope)
        bound = tx.resolve("public_document_fetch.asset", binding_key)
        if bound is not None and bound["id"] != record_id:
            raise EvidenceError("Figure asset already has a saved image fetch")
        prior = tx.get("public_document_fetch", record_id)
        if prior is not None:
            if prior["body"] != body:
                raise EvidenceError("Figure fetch operation already names different bytes")
            return ref(prior)
        record = tx.put("public_document_fetch", record_id, body)
        tx.link(record, asset, "fetched_from_source_asset")
        tx.bind("public_document_fetch.asset", binding_key,
                "public_document_fetch", record["id"])
        return ref(record)

    return store.transact(f"source-asset-save:{operation_id}", body, save,
                          mission_id=scope["mission_id"])


def fetch_source_asset(store, asset_ref: dict, *, scope: dict, operation_id: str,
                       fetcher: Callable[[str], dict] = fetch_public_source) -> dict:
    """Mark before HTTP. A missing response after the marker is uncertain, not replayed."""
    scope = _scope(scope)
    asset, _ = current_asset_use(store, asset_ref, scope=scope)
    if asset["body"]["link_state"] != "fetchable":
        raise EvidenceError("This source-authored graphic has no verified HTTPS image link")
    binding_key = f"{asset_ref['id']}:{asset_ref['version']}"
    bound = store.resolve("public_document_fetch.asset", binding_key)
    if bound is not None:
        from .document_ocr import current_document_page_use

        pointer = ref(bound)
        current_document_page_use(store, pointer, **scope)
        return pointer
    document_id = f"public-document-fetch:{operation_id}"
    saved = store.get("public_document_fetch", document_id)
    if saved is not None:
        if saved["body"].get("parent_asset_ref") != asset_ref:
            raise EvidenceError("Figure fetch operation belongs to another asset")
        from .document_ocr import current_document_page_use

        current_document_page_use(store, ref(saved), **scope)
        return ref(saved)
    marker_id = f"source-asset-fetch-attempt:{asset_ref['id']}:{asset_ref['version']}"
    if store.get("source_asset_fetch_attempt", marker_id) is not None:
        raise EvidenceError("Figure fetch outcome is uncertain; do not replay this operation")
    marker = {"schema": "ora.evidence.source_asset_fetch_attempt.v1", **scope,
              "asset_ref": asset_ref, "requested_url_sha256": _sha(asset["body"]["image_url"].encode())}

    def mark(tx):
        current_asset_use(tx, asset_ref, scope=scope)
        if tx.resolve("public_document_fetch.asset", binding_key) is not None:
            raise EvidenceError("Figure asset already has a saved image fetch")
        if tx.get("source_asset_fetch_attempt", marker_id) is not None:
            raise EvidenceError("Figure fetch outcome is uncertain; do not replay this operation")
        return ref(tx.put("source_asset_fetch_attempt", marker_id, marker))

    store.transact(f"source-asset-attempt:{operation_id}", marker, mark,
                   mission_id=scope["mission_id"])
    fetched = fetcher(asset["body"]["image_url"])
    return save_source_asset_fetch(store, asset_ref, fetched, scope=scope,
                                   operation_id=operation_id)
