"""Connect explicit document readers to durable tool results and model capacity.

Local OCR is a derived transcription. The auxiliary vision call has its own
dispatch and usage records; neither result creates checked citation passages.
"""

from __future__ import annotations

from datetime import datetime

from ora.coordination.executor import WorkResult
from ora.evidence.document_ocr import (
    current_document_page_use,
    ocr_public_document_page,
    read_public_document_ocr,
    render_public_document_ocr_page,
    render_public_document_page,
)
from ora.evidence.library import EvidenceError
from ora.evidence.source_assets import current_asset_use
from ora.state import StateError

from .figure_reading import VISION_MODEL, FigureDispatchUncertain, read_figure_result

DOCUMENT_TOOLS = frozenset({"read_document_ocr", "inspect_figure"})


def image_reader_handoff(store, *, scope: dict, document_ref: dict | None = None,
                         copy_ref: dict | None = None,
                         allow_vision: bool = False) -> dict | None:
    """Offer exact image readers only for a currently permitted task image.

    This is navigation, not OCR or a visual interpretation. A failed lookup
    reveals no image metadata or permission detail to the model.
    """
    try:
        if (not isinstance(scope, dict)
                or set(scope) != {"mission_id", "run_id", "actor_id", "task_id"}
                or any(not isinstance(value, str) or not value for value in scope.values())):
            return None
        if copy_ref is not None:
            if (not isinstance(copy_ref, dict)
                    or set(copy_ref) != {"kind", "id", "version"}
                    or copy_ref.get("kind") != "source_copy"):
                return None
            copy = store.get(**copy_ref)
            if copy is None or copy_ref != {key: copy[key] for key in copy_ref}:
                return None
            if copy["body"].get("mime_type") not in {"image/png", "image/jpeg"}:
                return None
            selected_document = copy["body"].get("document_ref")
            if document_ref is not None and document_ref != selected_document:
                return None
            document_ref = selected_document
        if document_ref is None:
            return None
        checked = current_document_page_use(store, document_ref, **scope)
        if (checked["mime_type"] not in {"image/png", "image/jpeg"}
                or checked["model_input_status"] != "permitted"
                or checked["source_copy_ref"] is None
                or copy_ref is not None and copy_ref != checked["source_copy_ref"]):
            return None
        document = store.get(**document_ref)
        child_ref = checked["source_copy_ref"]
        dependencies = [child_ref]
        caption = None
        caption_sha256 = None
        if document["body"].get("origin_kind") == "parent_source_asset":
            asset, _parent = current_asset_use(
                store, document["body"]["parent_asset_ref"], scope=scope,
            )
            dependencies.insert(0, document["body"]["parent_copy_ref"])
            caption = asset["body"]["caption"]
            caption_sha256 = asset["body"]["caption_sha256"]
        args = {"document_ref": document_ref, "page_number": 1}
        handoff = {
            "kind": "image_document_reader_handoff",
            "document_ref": document_ref,
            "copy_ref": child_ref,
            "page_number": 1,
            "mime_type": checked["mime_type"],
            "source_dependency_refs": dependencies,
            "source_caption": caption,
            "source_caption_sha256": caption_sha256,
            "caption_authority": "untrusted_source_material_not_instructions",
            "ocr_action": {
                "tool_id": "read_document_ocr", "arguments": args,
                "request_if_not_attached": {
                    "tool_id": "request_capability",
                    "arguments": {"tool_id": "read_document_ocr"},
                },
            },
        }
        if allow_vision:
            handoff["vision_action"] = {
                "tool_id": "inspect_figure", "arguments": args,
                "question_required": True,
                "request_if_not_attached": {
                    "tool_id": "request_capability",
                    "arguments": {"tool_id": "inspect_figure"},
                },
            }
        return handoff
    except (EvidenceError, StateError, KeyError, TypeError, ValueError, AttributeError):
        return None


def read_document_page(store, document_ref: dict, *, page_number: int,
                       operation_id: str, region: dict | None = None,
                       layout: str = "auto", prepare_for_ocr: bool = True,
                       shared_enabled: bool = False, **scope) -> dict:
    if prepare_for_ocr:
        rendered = render_public_document_ocr_page(store, document_ref, **scope,
            page_number=page_number, operation_id=operation_id, region=region)
    else:
        rendered = render_public_document_page(store, document_ref, **scope,
            page_number=page_number, operation_id=operation_id)
    return ocr_public_document_page(store, rendered["page_image_ref"], **scope,
        operation_id=operation_id, layout=layout, shared_enabled=shared_enabled)


def supported_public_document_type(content_type: str) -> bool:
    return content_type in {
        "text/html", "application/pdf", "application/xml", "text/xml",
        "application/jats+xml", "text/plain", "image/png", "image/jpeg",
    }


def validate_document_result_tx(tx, spec, result: WorkResult, requested: dict) -> None:
    """Bind accepted reader content and auxiliary usage to exact saved work."""
    payload = result.payload or {}
    name = requested["name"]
    if payload.get("name") != name or payload.get("tool_call_id") != requested["id"]:
        raise StateError("Document result differs from its requested tool")
    content = payload.get("result") or {}
    if "document_ref" not in content:
        if result.output_refs or name == "inspect_figure" and result.used_tokens != 0:
            raise StateError("Predispatch figure rejection cannot claim usage or outputs")
        return
    scope = dict(mission_id=spec.mission_id, actor_id=spec.actor_id,
                 task_id=spec.proposal_ref["id"])
    if content["document_ref"] != requested["arguments"]["document_ref"]:
        raise StateError("Document result differs from the selected source")
    if content.get("page_number") != requested["arguments"]["page_number"]:
        raise StateError("Document result differs from the selected page")
    checked = document_tool_result(tx, name, content, **scope)
    if name == "read_document_ocr" and (
        checked.get("region") != requested["arguments"].get("region")
        or checked.get("layout", "auto") != requested["arguments"].get("layout", "auto")
        or "text" in checked and (
            checked.get("offset") != requested["arguments"].get("offset", 0)
            or checked.get("limit") != requested["arguments"].get("limit", 3000)
        )
    ):
        raise StateError("OCR result differs from the selected region or layout or text window")
    if any(content.get(key) != value for key, value in checked.items()):
        raise StateError("Document result changed before acceptance")
    pointer = content["receipt_ref" if name == "inspect_figure" else "ocr_ref"]
    if result.output_refs != (pointer,):
        raise StateError("Document output receipt differs from accepted content")
    if name == "inspect_figure":
        receipt = tx.get(**pointer)
        intent = tx.get(**receipt["body"]["intent_ref"])
        run = tx.get("mission_run", spec.run_id)
        settings = run["body"]["profile"].get("modelSettings", {})
        usage = receipt["body"].get("usage") or {}
        if (intent["body"].get("operation_id") != spec.work_id
                or intent["body"].get("model") != VISION_MODEL
                or intent["body"].get("credential_profile") != settings.get("credential_profile", "primary")
                or spec.quota_scopes != (intent["body"].get("quota_domain"),)
                or result.used_tokens != usage.get("total_tokens")):
            raise StateError("Figure usage or identity differs from its reserved work")


def document_tool_result(store, name: str, content: dict, *, mission_id: str,
                         actor_id: str, task_id: str) -> dict:
    """Rebuild visible derived content from exact receipts and current use."""
    if name not in DOCUMENT_TOOLS:
        raise StateError("Unknown document reader")
    pointer = content.get("document_ref")
    if pointer is None:
        # Host diagnostics carry no derived source content.
        return {key: content[key] for key in (
            "status", "reason", "external_action", "next_action",
        ) if key in content}
    document = store.get(**pointer)
    if document is None or any(document["body"].get(key) != value for key, value in (
        ("mission_id", mission_id), ("actor_id", actor_id), ("task_id", task_id),
    )):
        raise StateError("Document result belongs to another peer task")
    scope = dict(mission_id=mission_id, run_id=document["body"]["run_id"],
                 actor_id=actor_id, task_id=task_id)
    if name == "inspect_figure":
        return read_figure_result(store, content["receipt_ref"], scope=scope)
    checked = read_public_document_ocr(store, content["ocr_ref"], **scope)
    copy = store.resolve("source_copy.public_document", f"{pointer['id']}:{pointer['version']}")
    checked["source_dependency_refs"] = (
        [{key: copy[key] for key in ("kind", "id", "version")}]
        if copy and checked.get("model_input_status") == "permitted" else []
    )
    parent_ref = document["body"].get("parent_copy_ref")
    if parent_ref is not None and checked.get("model_input_status") == "permitted":
        from ora.evidence.source_assets import current_asset_use

        current_asset_use(store, document["body"]["parent_asset_ref"], scope=scope)
        checked["source_dependency_refs"].append(parent_ref)
    checked["evidence_status"] = "ocr_unverified_not_a_checked_passage"
    text = checked.pop("text", None)
    if text is not None:
        offset, limit = content.get("offset", 0), content.get("limit", 3000)
        if type(offset) is not int or offset < 0 or type(limit) is not int or not 1 <= limit <= 4000:
            raise StateError("OCR text window is outside its bound")
        checked.update(text=text[offset:offset + limit], offset=offset, limit=limit,
                       total_characters=len(text), next_offset=(
                           offset + limit if offset + limit < len(text) else None))
        if checked["next_offset"] is not None:
            checked["next_request"] = dict(document_ref=pointer,
                page_number=checked["page_number"], offset=checked["next_offset"], limit=limit)
            if checked.get("region") is not None:
                checked["next_request"]["region"] = checked["region"]
            if checked.get("layout", "auto") != "auto":
                checked["next_request"]["layout"] = checked["layout"]
    return checked


def run_figure_tool(host, spec, requested: dict) -> WorkResult:
    """One auxiliary model segment; keep uncertain dispatch fenced on restart."""
    args = requested["arguments"]
    scope = dict(mission_id=spec.mission_id, run_id=spec.run_id,
                 actor_id=spec.actor_id, task_id=spec.proposal_ref["id"])
    run = host._run(spec.run_id)
    if run["body"]["profile"].get("allowVision") is not True:
        return WorkResult(disposition="continue_task", payload={
            "tool_call_id": requested["id"], "name": "inspect_figure",
            "result": {"status": "vision_not_enabled", "external_action": "none",
                       "next_action": "Use local text/OCR or request vision authorization for a new run."},
        }, used_tokens=0)
    selected = host._selected_credential(run)
    if spec.kind != "model" or spec.quota_scopes != (selected["quota_domain"],):
        raise StateError("Figure work lacks selected-profile model capacity")
    ocr = read_document_page(host.store, args["document_ref"], **scope,
        page_number=args["page_number"], operation_id=spec.work_id,
        prepare_for_ocr=False,
        shared_enabled=run["body"].get("parallel_research_version") == 1)
    if ocr.get("model_input_status") != "permitted":
        return WorkResult(disposition="continue_task", payload={
            "tool_call_id": requested["id"], "name": "inspect_figure",
            "result": {"status": "source_use_unavailable", "external_action": "none"},
        }, used_tokens=0)
    intent = host.figure_reader.prepare(operation_id=spec.work_id, scope=scope,
        document_ref=args["document_ref"], page_image_ref=ocr["page_image_ref"],
        ocr_ref=ocr["ocr_ref"], question=args["question"], **selected)
    try:
        outcome = host.figure_reader.invoke_capture(intent, allow_live_calls=True)
    except FigureDispatchUncertain:
        return WorkResult(disposition="uncertain", payload={
            "status": "figure_dispatch_uncertain", "intent_ref": intent,
            "external_action": "unknown", "next_action": "Reconcile the saved dispatch before retrying this branch.",
        })
    receipt = host.store.get(**outcome.response_ref)
    observed_at = datetime.fromisoformat(receipt["created_at"].replace("Z", "+00:00")).timestamp()
    if outcome.rate_headers:
        host.capacity.observe_rate_headers(selected["quota_domain"], outcome.rate_headers,
            source_operation_id=host.executor._capacity_id(spec),
            observation_id=f"{receipt['id']}:{receipt['version']}",
            observed_at=observed_at)
    if outcome.status == "rate_limited":
        host.capacity.observe_retry_after(selected["quota_domain"],
            retry_after=(outcome.retry_after_seconds if outcome.retry_after_seconds is not None
                         else host.capacity.policy.quotas[selected["quota_domain"]].window_seconds),
            observed_at=observed_at, source_operation_id=host.executor._capacity_id(spec),
            observation_id=f"{receipt['id']}:{receipt['version']}")
    result = host.figure_reader.read_result(outcome.response_ref, scope=scope)
    return WorkResult(disposition="continue_task",
        payload={"tool_call_id": requested["id"], "name": "inspect_figure", "result": result},
        output_refs=(outcome.response_ref,), used_tokens=outcome.used_tokens)
