"""Private, local copies of reasoning actually returned by model providers.

The immutable state records are authoritative. These files are repairable views
for a trusted local operator and must never enter ordinary or cloud traces.
"""

from __future__ import annotations

import hashlib
import html
import json
import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

from ora.state import StateStore
from ora.state.blobs import IntegrityFault, sync_directory


class ReasoningArtifactError(ValueError):
    """Saved lineage or a private artifact destination is unsafe or inconsistent."""


_RECEIPT_KINDS = ("model_response_receipt", "figure_response_receipt")
_DISPATCH_KINDS = ("model_dispatch_attempt", "figure_dispatch_attempt")


def _safe_name(value: str) -> str:
    if not isinstance(value, str) or not value:
        raise ReasoningArtifactError("A saved artifact identity is missing")
    prefix = re.sub(r"[^A-Za-z0-9._-]+", "-", value).strip(".-")[:64] or "id"
    return f"{prefix}-{hashlib.sha256(value.encode('utf-8')).hexdigest()[:12]}"


def _exact_record(store: StateStore, pointer: dict, kind: str) -> dict:
    if (type(pointer) is not dict or set(pointer) != {"kind", "id", "version"}
        or pointer.get("kind") != kind or type(pointer.get("version")) is not int
        or pointer["version"] < 1 or not isinstance(pointer.get("id"), str)):
        raise ReasoningArtifactError("An exact saved record reference is required")
    record = store.get(**pointer)
    if record is None:
        raise ReasoningArtifactError("An exact saved record is unavailable")
    return record


def _has_link(store: StateStore, source: dict, target: dict, relation: str) -> bool:
    return any(edge["relation"] == relation and edge["target"] == {
        key: target[key] for key in ("kind", "id", "version")
    } for edge in store.links(source))


def _receipt_run(store: StateStore, receipt: dict) -> tuple[str, str]:
    body = receipt["body"]
    mission_id = body.get("mission_id")
    run_id = body.get("run_id")
    if receipt["kind"] == "model_response_receipt":
        intent = _exact_record(store, body.get("intent_ref"), "model_request_intent")
        if (intent["body"].get("mission_id") != mission_id
            or intent["body"].get("call_ref") != body.get("call_ref")
            or intent["body"].get("configuration_ref") != body.get("configuration_ref")
            or intent["body"].get("task_id") != body.get("task_id")
            or intent["body"].get("attempt_id") != body.get("attempt_id")
            or intent["body"].get("request_sha256") != body.get("request_sha256")):
            raise ReasoningArtifactError("Model receipt lineage differs from its request")
        actor = store.get("agent_instance", intent["body"].get("instance_id"), 1)
        if (actor is None or actor["body"].get("mission_id") != mission_id):
            raise ReasoningArtifactError("Model receipt actor lineage is unavailable")
        run_id = actor["body"].get("run_id")
    elif receipt["kind"] == "figure_response_receipt":
        # Figure receipts carry the run directly; the existing reader verifies
        # their request/dispatch lineage before saving.
        if body.get("intent_ref"):
            intent = _exact_record(store, body["intent_ref"], "figure_request_intent")
            if (intent["body"].get("mission_id") != mission_id
                or intent["body"].get("run_id") != run_id
                or any(intent["body"].get(key) != body.get(key)
                       for key in ("actor_id", "task_id", "operation_id", "request_sha256"))):
                raise ReasoningArtifactError("Figure receipt lineage differs from its request")
    if not isinstance(mission_id, str) or not isinstance(run_id, str) or not run_id:
        raise ReasoningArtifactError("Receipt run lineage is missing")
    run = store.get("mission_run", run_id, 1)
    if run is not None and run["body"].get("mission_id") != mission_id:
        raise ReasoningArtifactError("Receipt belongs to a different run mission")
    return run_id, mission_id


def _expected_reason_scope(store: StateStore, receipt: dict, run_id: str,
                           mission_id: str) -> dict:
    body = receipt["body"]
    expected = {key: body.get(key) for key in (
        "intent_ref", "request_sha256", "call_ref", "configuration_ref",
        "actor_id", "task_id", "attempt_id", "operation_id",
    )}
    expected.update({"mission_id": mission_id, "run_id": run_id})
    if receipt["kind"] == "model_response_receipt":
        intent = _exact_record(store, body["intent_ref"], "model_request_intent")
        call = body["call_ref"]
        expected.update({
            "instance_id": intent["body"]["instance_id"],
            "call_id": call["id"], "call_version": call["version"],
            "operation_id": f"model-call:{call['id']}:{call['version']}",
            "parent_operation_id": (
                f"model-call:{intent['body']['parent_call_ref']['id']}:"
                f"{intent['body']['parent_call_ref']['version']}"
                if intent["body"].get("parent_call_ref") else None
            ),
        })
    return expected


def _private_dir(path: Path) -> None:
    if any(part.is_symlink() for part in (path, *path.parents)):
        raise ReasoningArtifactError("Reasoning artifact directory contains a symbolic link")
    if not path.exists():
        parent = path.parent
        if parent != path and not parent.exists():
            _private_dir(parent)
        path.mkdir(mode=0o700, exist_ok=True)
    if path.is_symlink() or not path.is_dir():
        raise ReasoningArtifactError("Reasoning artifact directory is unsafe")
    if stat.S_IMODE(path.stat().st_mode) & 0o077:
        raise ReasoningArtifactError("Reasoning artifact directory is not private")


def _write_private(path: Path, data: bytes) -> None:
    if path.is_symlink():
        raise ReasoningArtifactError("Reasoning artifact file is a symbolic link")
    if path.exists():
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        try:
            with os.fdopen(descriptor, "rb") as saved:
                metadata = os.fstat(saved.fileno())
                if not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1:
                    raise ReasoningArtifactError("Reasoning artifact file is not private")
                if saved.read() == data:
                    path.chmod(0o600)
                    return
        except OSError:
            raise
    descriptor, staged_name = tempfile.mkstemp(prefix=".reasoning-", dir=path.parent)
    staged = Path(staged_name)
    try:
        with os.fdopen(descriptor, "wb") as staged_file:
            os.fchmod(staged_file.fileno(), 0o600)
            staged_file.write(data)
            staged_file.flush()
            os.fsync(staged_file.fileno())
        if path.is_symlink():
            raise ReasoningArtifactError("Reasoning artifact file became a symbolic link")
        os.replace(staged, path)
        sync_directory(path.parent)
    finally:
        staged.unlink(missing_ok=True)


def _saved_text(store: StateStore, body: dict, *, mission_id: str,
                run_id: str) -> tuple[str | None, str]:
    raw = body.get("raw_blob")
    if body.get("raw_ref") is not None:
        raw_record = _exact_record(store, body["raw_ref"], "restricted_reasoning_raw")
        if (raw_record["body"].get("mission_id") != mission_id
            or raw_record["body"].get("run_id") != run_id):
            raise ReasoningArtifactError("Raw reasoning lineage differs from receipt")
        raw = raw_record["body"].get("blob")
    saved = body.get("blob")
    if raw is not None:
        try:
            return store.read_blob(raw["sha256"]).decode("utf-8"), "provider_exact"
        except (KeyError, TypeError, UnicodeError, IntegrityFault) as exc:
            raise ReasoningArtifactError("Raw reasoning blob is invalid") from exc
    if saved is None:
        return None, "unavailable"
    try:
        text = store.read_blob(saved["sha256"]).decode("utf-8")
    except (KeyError, TypeError, UnicodeError, IntegrityFault) as exc:
        raise ReasoningArtifactError("Saved reasoning blob is invalid") from exc
    return text, "saved_redacted" if body.get("redacted") else "saved_unredacted"


def _markdown_escape(value: str) -> str:
    escaped = html.escape(value, quote=False)
    return re.sub(r"([\\`*_{}\[\]()#+\-.!|:])", r"\\\1", escaped)


def _markdown_quote(value: str) -> str:
    """Render untrusted provider text as inert, readable Markdown prose."""
    escaped = _markdown_escape(value)
    return "\n".join("> " + line if line else ">" for line in escaped.split("\n"))


def _step_markdown(document: dict) -> bytes:
    """Render one private step without changing its exact saved text or status."""
    pointer = document.get("receipt_ref") or document.get("dispatch_ref")
    lines = [
        "# Provider-emitted reasoning", "",
        f"- Run: {_markdown_escape(str(document['run_id']))}",
        f"- Agent: {_markdown_escape(str(document.get('agent') or 'unknown'))}",
        f"- Model: {_markdown_escape(str(document.get('model') or 'unknown'))}",
        f"- Provider: {_markdown_escape(str(document.get('provider') or 'unknown'))}",
        f"- Timestamp: {_markdown_escape(str(document['timestamp']))}",
        f"- Status: {_markdown_escape(str(document['status']))}",
        f"- Capture fidelity: {_markdown_escape(str(document['capture_fidelity']))}",
        f"- Capture note: {_markdown_escape(str(document['capture_note']))}",
        f"- Step: {_markdown_escape(pointer['kind'] + ' / ' + pointer['id'])}",
        "- Structured record: [reasoning_trace.json](reasoning_trace.json)",
    ]
    if document["content"] is not None:
        lines.append("- Exact text: [reasoning_trace.txt](reasoning_trace.txt)")
    if document.get("reasoning_tokens") is not None:
        lines.append(f"- Reported reasoning tokens: {document['reasoning_tokens']}")
    lines.extend(["", "## Saved provider text", ""])
    if document["content"] is None:
        lines.append("No saved provider reasoning text is available for this step.")
    else:
        lines.append(_markdown_quote(document["content"]))
    return ("\n".join(lines) + "\n").encode("utf-8")


def _capture_note(status: str, fidelity: str) -> tuple[str, str | None]:
    if status == "not_captured":
        return "A dispatch marker exists without a saved provider response.", "response_not_captured"
    if status == "not_requested":
        return "The selected request did not ask the provider to return reasoning.", "not_requested"
    if status == "unsupported":
        return "The requested reasoning mode was unsupported.", "unsupported"
    if status == "unknown":
        return "The available response does not establish whether reasoning was returned.", "unknown"
    if status == "absent":
        return "The parsed provider response did not include reasoning text.", "provider_omitted"
    if fidelity == "unavailable":
        return "Returned reasoning was recorded, but no exact saved text is available.", "saved_text_unavailable"
    if fidelity == "saved_redacted":
        return "Only the saved redacted text is available; original bytes are unknown.", "original_not_saved"
    if status == "truncated":
        return "The provider returned an exact partial reasoning prefix before cutoff.", None
    if fidelity == "saved_unredacted":
        return "Exact saved text is available from a legacy unredacted record.", None
    return "Exact provider-returned text is saved.", None


def export_receipt_reasoning(
    store: StateStore, receipt_ref: dict[str, Any], *, output_dir: Path | None = None,
) -> dict[str, Any]:
    """Write one exact saved receipt; return paths and fidelity, never text."""
    kind = receipt_ref.get("kind") if type(receipt_ref) is dict else None
    if kind not in _RECEIPT_KINDS:
        raise ReasoningArtifactError("Reasoning export needs a model or figure receipt")
    receipt = _exact_record(store, receipt_ref, kind)
    run_id, mission_id = _receipt_run(store, receipt)
    body = receipt["body"]
    reasoning = _exact_record(store, body.get("reasoning_ref"), "restricted_reasoning")
    if not _has_link(store, receipt, reasoning, "has_restricted_reasoning"):
        raise ReasoningArtifactError("Receipt has no exact restricted reasoning link")
    reason_body = reasoning["body"]
    if (reason_body.get("origin") != "provider_returned"
        or reason_body.get("access") != "restricted_diagnostics"):
        raise ReasoningArtifactError("Reasoning record is not provider-returned private material")
    if (reason_body.get("mission_id") != mission_id
        or reason_body.get("status") != body.get("reasoning_status")
        or (reason_body.get("run_id") not in (None, run_id))):
        raise ReasoningArtifactError("Reasoning record differs from receipt lineage")
    expected_scope = _expected_reason_scope(store, receipt, run_id, mission_id)
    for key, expected in expected_scope.items():
        if key in reason_body and reason_body[key] != expected:
            raise ReasoningArtifactError("Reasoning record differs from receipt step")
    if reason_body.get("raw_ref") is not None:
        raw_record = _exact_record(store, reason_body["raw_ref"],
                                   "restricted_reasoning_raw")
        if not _has_link(store, reasoning, raw_record, "has_exact_provider_text"):
            raise ReasoningArtifactError("Reasoning has no exact raw-text link")
        if (raw_record["body"].get("origin") != "provider_returned"
            or raw_record["body"].get("access") != "restricted_diagnostics"):
            raise ReasoningArtifactError("Raw reasoning is not provider-returned private material")
        for key, expected in expected_scope.items():
            if key in raw_record["body"] and raw_record["body"][key] != expected:
                raise ReasoningArtifactError("Raw reasoning differs from receipt step")
    content, fidelity = _saved_text(store, reason_body, mission_id=mission_id,
                                    run_id=run_id)
    status = reason_body["status"]
    if status not in {"returned", "truncated", "absent", "unknown", "unsupported", "not_requested"}:
        raise ReasoningArtifactError("Saved reasoning status is invalid")
    if status in {"absent", "unknown", "unsupported", "not_requested"} and content is not None:
        raise ReasoningArtifactError("Unavailable reasoning has saved content")
    capture_note, unavailable_reason = _capture_note(status, fidelity)
    root = Path(output_dir) if output_dir is not None else store.root / "reasoning"
    if not root.is_absolute():
        raise ReasoningArtifactError("Reasoning artifact root must be absolute")
    _private_dir(root)
    step_dir = root / _safe_name(run_id) / _safe_name(receipt["id"])
    _private_dir(step_dir)
    text_path = step_dir / "reasoning_trace.txt"
    json_path = step_dir / "reasoning_trace.json"
    markdown_path = step_dir / "reasoning_trace.md"
    provider = body.get("provider") or "groq"
    model = body.get("returned_model") or body.get("model")
    actor_id = body.get("actor_id")
    agent = actor_id
    if receipt["kind"] == "model_response_receipt":
        config = _exact_record(store, body.get("configuration_ref"), "agent_configuration")
        model = model or config["body"].get("model_id")
        provider = config["body"].get("provider")
        intent = _exact_record(store, body["intent_ref"], "model_request_intent")
        actor_id = intent["body"].get("instance_id")
        actor = store.get("agent_instance", actor_id, 1)
        agent = actor["body"].get("display_name") or actor_id
    model = model or body.get("selected_model")
    usage = body.get("usage") or {}
    details = usage.get("completion_tokens_details") if isinstance(usage, dict) else None
    reasoning_tokens = (details.get("reasoning_tokens") if isinstance(details, dict)
                        else None)
    if type(reasoning_tokens) is not int or reasoning_tokens < 0:
        reasoning_tokens = None
    document = {
        "schema": "ora.reasoning_artifact.v1",
        "run_id": run_id,
        "mission_id": mission_id,
        "receipt_ref": receipt_ref,
        "reasoning_ref": body["reasoning_ref"],
        "call_ref": body.get("call_ref"),
        "intent_ref": body.get("intent_ref"),
        "dispatch_ref": body.get("dispatch_ref"),
        "actor_id": actor_id,
        "agent": agent,
        "task_id": body.get("task_id"),
        "attempt_id": body.get("attempt_id"),
        "operation_id": body.get("operation_id"),
        "response_id": body.get("response_id"),
        "provider": provider,
        "model": model,
        "timestamp": receipt["created_at"],
        "status": status,
        "capture_fidelity": fidelity,
        "capture_note": capture_note,
        "unavailable_reason": unavailable_reason,
        "reasoning_tokens": reasoning_tokens,
        "content": content,
    }
    if content is not None:
        _write_private(text_path, content.encode("utf-8"))
    _write_private(json_path, (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    _write_private(markdown_path, _step_markdown(document))
    return {
        "receipt_ref": receipt_ref, "status": status, "capture_fidelity": fidelity,
        "capture_note": capture_note, "unavailable_reason": unavailable_reason,
        "timestamp": receipt["created_at"],
        "text_path": str(text_path) if content is not None else None,
        "json_path": str(json_path),
        "markdown_path": str(markdown_path),
    }


def _uncaptured_dispatch(store: StateStore, marker: dict, *,
                         output_dir: Path | None) -> dict[str, Any] | None:
    body = marker["body"]
    intent_kind = ("model_request_intent" if marker["kind"] == "model_dispatch_attempt"
                   else "figure_request_intent")
    response_kind = ("model_response_receipt" if marker["kind"] == "model_dispatch_attempt"
                     else "figure_response_receipt")
    intent = _exact_record(store, body.get("intent_ref"), intent_kind)
    if (intent["body"].get("mission_id") != body.get("mission_id")
        or intent["body"].get("request_sha256") != body.get("request_sha256")):
        raise ReasoningArtifactError("Dispatch marker differs from its request")
    receipt_id = ("model-response:" if response_kind == "model_response_receipt"
                  else "figure-response:") + intent["id"]
    if store.get(response_kind, receipt_id) is not None:
        return None
    if body.get("status") == "captured":
        raise ReasoningArtifactError("Captured dispatch has no response receipt")
    run_id = body.get("run_id")
    agent = body.get("actor_id")
    model = intent["body"].get("model")
    provider = "groq"
    if marker["kind"] == "model_dispatch_attempt":
        actor = store.get("agent_instance", intent["body"].get("instance_id"), 1)
        if actor is None or actor["body"].get("mission_id") != body.get("mission_id"):
            raise ReasoningArtifactError("Dispatch actor lineage is unavailable")
        run_id = actor["body"].get("run_id")
        agent = actor["body"].get("display_name") or actor["id"]
        config = _exact_record(store, intent["body"].get("configuration_ref"),
                               "agent_configuration")
        model = config["body"].get("model_id")
        provider = config["body"].get("provider")
    if not isinstance(run_id, str) or not run_id:
        raise ReasoningArtifactError("Dispatch run lineage is missing")
    run = store.get("mission_run", run_id, 1)
    if run is not None and run["body"].get("mission_id") != body.get("mission_id"):
        raise ReasoningArtifactError("Dispatch belongs to a foreign run")
    root = Path(output_dir) if output_dir is not None else store.root / "reasoning"
    if not root.is_absolute():
        raise ReasoningArtifactError("Reasoning artifact root must be absolute")
    _private_dir(root)
    step_dir = root / _safe_name(run_id) / _safe_name(marker["id"])
    _private_dir(step_dir)
    pointer = {key: marker[key] for key in ("kind", "id", "version")}
    json_path = step_dir / "reasoning_trace.json"
    markdown_path = step_dir / "reasoning_trace.md"
    locally_blocked = body.get("status") == "blocked_before_external_effect"
    unavailable_reason = ("blocked_before_http" if locally_blocked
                          else "response_not_captured")
    capture_note = ("A local transport guard blocked this request before HTTP; no inference occurred."
                    if locally_blocked else _capture_note("not_captured", "unavailable")[0])
    document = {
        "schema": "ora.reasoning_artifact.v1", "run_id": run_id,
        "mission_id": body["mission_id"], "receipt_ref": None,
        "dispatch_ref": pointer, "intent_ref": body["intent_ref"],
        "reasoning_ref": None, "agent": agent,
        "actor_id": body.get("actor_id") or intent["body"].get("instance_id"),
        "task_id": body.get("task_id") or intent["body"].get("task_id"),
        "call_ref": body.get("call_ref") or intent["body"].get("call_ref"),
        "operation_id": body.get("operation_id") or intent["body"].get("operation_id"),
        "provider": provider, "model": model,
        "timestamp": marker["created_at"], "status": "not_captured",
        "capture_fidelity": "unavailable",
        "capture_note": capture_note,
        "unavailable_reason": unavailable_reason,
        "reasoning_tokens": None, "content": None,
    }
    _write_private(json_path,
                   (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    _write_private(markdown_path, _step_markdown(document))
    return {"receipt_ref": pointer, "status": "not_captured",
            "capture_fidelity": "unavailable",
            "capture_note": document["capture_note"],
            "unavailable_reason": unavailable_reason,
            "timestamp": marker["created_at"],
            "text_path": None, "json_path": str(json_path),
            "markdown_path": str(markdown_path)}


def _legacy_receipt_without_reasoning(store: StateStore, receipt: dict, *,
                                      output_dir: Path | None) -> dict[str, Any]:
    """Represent historical figure receipts that never saved reasoning state."""
    run_id, mission_id = _receipt_run(store, receipt)
    root = Path(output_dir) if output_dir is not None else store.root / "reasoning"
    if not root.is_absolute():
        raise ReasoningArtifactError("Reasoning artifact root must be absolute")
    _private_dir(root)
    step_dir = root / _safe_name(run_id) / _safe_name(receipt["id"])
    _private_dir(step_dir)
    pointer = {key: receipt[key] for key in ("kind", "id", "version")}
    json_path = step_dir / "reasoning_trace.json"
    markdown_path = step_dir / "reasoning_trace.md"
    body = receipt["body"]
    note = "This historical response receipt did not save provider reasoning state."
    document = {
        "schema": "ora.reasoning_artifact.v1", "run_id": run_id,
        "mission_id": mission_id, "receipt_ref": pointer,
        "dispatch_ref": body.get("dispatch_ref"),
        "intent_ref": body.get("intent_ref"), "reasoning_ref": None,
        "agent": body.get("actor_id"), "actor_id": body.get("actor_id"),
        "task_id": body.get("task_id"), "call_ref": body.get("call_ref"),
        "operation_id": body.get("operation_id"),
        "provider": body.get("provider") or "groq",
        "model": body.get("returned_model") or body.get("model")
                 or body.get("selected_model"),
        "timestamp": receipt["created_at"], "status": "unknown",
        "capture_fidelity": "unavailable", "capture_note": note,
        "unavailable_reason": "legacy_not_captured",
        "reasoning_tokens": None, "content": None,
    }
    _write_private(json_path,
                   (json.dumps(document, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    _write_private(markdown_path, _step_markdown(document))
    return {"receipt_ref": pointer, "status": "unknown",
            "capture_fidelity": "unavailable", "capture_note": note,
            "unavailable_reason": "legacy_not_captured",
            "timestamp": receipt["created_at"], "text_path": None,
            "json_path": str(json_path), "markdown_path": str(markdown_path)}


def _candidate_run(store: StateStore, record: dict) -> str | None:
    """Select a run cheaply before applying strict exact-lineage checks."""
    body = record["body"]
    if record["kind"].startswith("figure_"):
        return body.get("run_id")
    pointer = body.get("intent_ref")
    if not isinstance(pointer, dict):
        return None
    try:
        intent = store.get(**pointer)
    except (TypeError, ValueError):
        return None
    if intent is None:
        return None
    actor = store.get("agent_instance", intent["body"].get("instance_id"), 1)
    return actor["body"].get("run_id") if actor else None


def export_run_reasoning(
    store: StateStore, run_id: str, *, output_dir: Path | None = None,
) -> dict[str, Any]:
    """Repair/export all saved model and figure reasoning for one exact run."""
    run = store.get("mission_run", run_id, 1)
    if run is None:
        raise ReasoningArtifactError("Selected run is unavailable")
    steps = []
    for kind in (*_RECEIPT_KINDS, *_DISPATCH_KINDS):
        after = ""
        while True:
            page = store.records(kind, after_id=after, limit=500)
            if not page:
                break
            for receipt in page:
                if receipt["body"].get("mission_id") != run["body"].get("mission_id"):
                    continue
                if _candidate_run(store, receipt) != run_id:
                    continue
                if kind in _RECEIPT_KINDS:
                    selected_run, _ = _receipt_run(store, receipt)
                    if selected_run == run_id:
                        pointer = {"kind": kind, "id": receipt["id"],
                                   "version": receipt["version"]}
                        if (kind == "figure_response_receipt"
                            and receipt["body"].get("reasoning_ref") is None):
                            steps.append(_legacy_receipt_without_reasoning(
                                store, receipt, output_dir=output_dir))
                        else:
                            steps.append(export_receipt_reasoning(
                                store, pointer, output_dir=output_dir))
                else:
                    step = _uncaptured_dispatch(store, receipt, output_dir=output_dir)
                    if step is not None:
                        steps.append(step)
            after = page[-1]["id"]
    steps.sort(key=lambda item: (item["timestamp"], item["receipt_ref"]["kind"],
                                 item["receipt_ref"]["id"]))
    root = Path(output_dir) if output_dir is not None else store.root / "reasoning"
    run_dir = root / _safe_name(run_id)
    _private_dir(run_dir)
    combined_text_path = run_dir / "reasoning_trace.txt"
    combined_json_path = run_dir / "reasoning_trace.json"
    combined_markdown_path = run_dir / "reasoning_trace.md"
    combined_steps = []
    combined_text_parts = []
    combined_markdown_parts = ["# Provider-returned reasoning", ""]
    for index, step in enumerate(steps, 1):
        private_step = json.loads(Path(step["json_path"]).read_text(encoding="utf-8"))
        combined_steps.append(private_step)
        pointer = step["receipt_ref"]
        json_relative = Path(step["json_path"]).relative_to(run_dir).as_posix()
        markdown_relative = Path(step["markdown_path"]).relative_to(run_dir).as_posix()
        text_relative = (Path(step["text_path"]).relative_to(run_dir).as_posix()
                         if step["text_path"] is not None else None)
        combined_markdown_parts.extend([
            f"## Step {index}", "",
            f"- Agent: {_markdown_escape(str(private_step.get('agent') or 'unknown'))}",
            f"- Model: {_markdown_escape(str(private_step.get('model') or 'unknown'))}",
            f"- Provider: {_markdown_escape(str(private_step.get('provider') or 'unknown'))}",
            f"- Timestamp: {_markdown_escape(str(private_step['timestamp']))}",
            f"- Status: {step['status']}",
            f"- Capture fidelity: {step['capture_fidelity']}",
            f"- Capture note: {_markdown_escape(step['capture_note'])}",
            f"- Receipt: {_markdown_escape(pointer['kind'] + ' / ' + pointer['id'])}",
            f"- Step Markdown: [reasoning_trace.md]({markdown_relative})",
            f"- Step JSON: [reasoning_trace.json]({json_relative})",
        ])
        if text_relative is not None:
            combined_markdown_parts.append(
                f"- Exact text: [reasoning_trace.txt]({text_relative})"
            )
        if private_step.get("reasoning_tokens") is not None:
            combined_markdown_parts.append(
                f"- Reported reasoning tokens: {private_step['reasoning_tokens']}"
            )
        combined_markdown_parts.extend(["", "### Returned text", ""])
        combined_text_parts.append(
            f"===== STEP {index}: {pointer['kind']} {pointer['id']} "
            f"({step['status']}; {step['capture_fidelity']}) =====\n"
        )
        if private_step["content"] is None:
            combined_text_parts.append("[No saved provider reasoning text]\n")
            combined_markdown_parts.append("No saved provider reasoning text.")
        else:
            combined_text_parts.append(private_step["content"])
            combined_text_parts.append("\n")
            combined_markdown_parts.append(_markdown_quote(private_step["content"]))
        combined_markdown_parts.append("")
    _write_private(combined_text_path, "\n".join(combined_text_parts).encode("utf-8"))
    _write_private(combined_json_path, (json.dumps({
        "schema": "ora.reasoning_run_artifact.v1", "run_id": run_id,
        "steps": combined_steps,
    }, ensure_ascii=False, indent=2) + "\n").encode("utf-8"))
    _write_private(combined_markdown_path,
                   ("\n".join(combined_markdown_parts) + "\n").encode("utf-8"))
    unavailable = sum(item["capture_fidelity"] in {"unavailable", "saved_redacted"}
                      for item in steps)
    truncated = sum(item["status"] == "truncated" for item in steps)
    dispatch_only = sum(item["status"] == "not_captured" for item in steps)
    return {
        "run_id": run_id,
        "output_dir": str(root),
        "combined_text_path": str(combined_text_path),
        "combined_json_path": str(combined_json_path),
        "combined_markdown_path": str(combined_markdown_path),
        "status": "no_receipts" if not steps else "partial" if unavailable or truncated else "complete",
        "receipt_count": len(steps) - dispatch_only,
        "dispatch_only_count": dispatch_only,
        "exported_count": len(steps),
        "unavailable_count": unavailable,
        "truncated_count": truncated,
        "steps": steps,
    }


def record_export_failure(store: StateStore, receipt_ref: dict, exc: Exception) -> None:
    """Best-effort class-only diagnostic after a response is already durable."""
    try:
        receipt = store.get(**receipt_ref)
        if receipt is None:
            return
        mission_id = receipt["body"]["mission_id"]
        identity = hashlib.sha256(json.dumps(receipt_ref, sort_keys=True).encode()).hexdigest()[:20]
        body = {"schema": "ora.reasoning_export_failure.v1",
                "mission_id": mission_id, "receipt_ref": receipt_ref,
                "error_class": type(exc).__name__}
        def save(tx):
            diagnostic = tx.put("reasoning_export_failure",
                                f"reasoning-export-failure:{identity}", body)
            return {"diagnostic_ref": {"kind": diagnostic["kind"],
                                       "id": diagnostic["id"],
                                       "version": diagnostic["version"]}}
        store.transact(f"reasoning-export-failure:{identity}", body,
                       save, mission_id=mission_id)
    except Exception:
        pass  # Local export failure must never discard a captured provider response.
