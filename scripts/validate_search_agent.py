"""One explicit natural search task on the real host, with no prescribed tools.

The default prints the case. --live scopes the selected profile to a private
runtime copy. Accounting-only observation requires two explicit opt-in flags.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import shutil
import sqlite3
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
REQUEST_ID = "doe-cool-roof-natural-20261015-v1"
QUESTION = ("Find official U.S. Department of Energy guidance on cool roofs. "
            "In at most 150 words, explain how a cool roof works and one climate-related "
            "tradeoff, with a supporting source link.")
PROFILE = Path(".local/groq-credential-profiles.json")
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"
DRAIN_TIMEOUT_SECONDS = 5.0


def _atomic_json(path: Path, value: dict) -> None:
    # Bootstrap without importing the mutable checkout's ora modules. Live
    # runtime imports happen only after the exact private snapshot is ready.
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd, name = tempfile.mkstemp(dir=path.parent)
    with os.fdopen(fd, "w") as out:
        json.dump(value, out, indent=2)
        out.flush()
        os.fsync(out.fileno())
    os.replace(name, path)


def freeze(root: Path, folder: Path, run_id: str, credential_profile: str = "primary", *,
           share_provider_limits: bool = False) -> dict:
    """Snapshot the full normal roles/skills/code; no reduced capability menu."""
    runtime = folder / "runtime"
    hashes = {}
    for directory in ("src/ora", "roles", "runtime_skills"):
        for source in sorted((root / directory).rglob("*")):
            if not source.is_file() or "__pycache__" in source.parts or source.suffix == ".pyc":
                continue
            relative = source.relative_to(root)
            target = runtime / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
            hashes[str(relative)] = hashlib.sha256(target.read_bytes()).hexdigest()
    original = (root / PROFILE).read_bytes() if (root / PROFILE).exists() else None
    policy = json.loads(original) if original else {"version": 1, "profiles": {}}
    if credential_profile not in {"primary", "backup"}:
        raise ValueError("Select Primary or Backup explicitly")
    for name in ("primary", "backup"):
        policy["profiles"].setdefault(name, {}).update(
            enabled=name == credential_profile,
            allowed_run_ids=[run_id] if name == credential_profile else [],
        )
    _atomic_json(runtime / PROFILE, policy)
    if share_provider_limits:
        # Independent live cases share verified numeric packing observations,
        # not research state, active reservations or credential settings.
        shared = (root / ".local/provider-limits").resolve()
        shared.mkdir(parents=True, exist_ok=True, mode=0o700)
        (runtime / ".local/provider-limits").symlink_to(shared, target_is_directory=True)
    if (root / ".env").exists():
        (runtime / ".env").symlink_to(root / ".env")
    manifest = {"files": hashes, "original_profile_sha256": hashlib.sha256(original).hexdigest() if original else None,
                "runtime_profile": policy, "original_profile_changed": False,
                "driver_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    _atomic_json(folder / "source-manifest.json", manifest)
    return manifest


def _state_digest(folder: Path) -> str:
    """Fingerprint a closed parent's files without opening its state store."""
    digest = hashlib.sha256()
    for path in sorted(folder.rglob("*")):
        if path.is_file():
            digest.update(str(path.relative_to(folder)).encode())
            digest.update(hashlib.sha256(path.read_bytes()).digest())
    return digest.hexdigest()


def _parent_catalog_uses_qwen(state_dir: Path, run_id: str) -> bool:
    """Read only the exact saved parent binding catalog for follow-up budget opt-in."""
    path = (Path(state_dir) / "state.sqlite3").resolve(strict=True)
    with sqlite3.connect(path.as_uri() + "?mode=ro", uri=True) as db:
        row = db.execute(
            "SELECT r.body FROM records r JOIN heads h USING(kind,id,version) "
            "WHERE r.kind='mission_run' AND r.id=?", (run_id,),
        ).fetchone()
    if row is None:
        raise ValueError("Follow-up parent run is unavailable")
    body = json.loads(row[0])
    bindings = body.get("profile", {}).get("agentModelBindings")
    if bindings is None:
        return False
    if type(bindings) is not dict or any(type(item) is not dict for item in bindings.values()):
        raise ValueError("Follow-up parent model bindings are invalid")
    return any(item.get("model") == "qwen/qwen3.8-27b" for item in bindings.values())


def _verify_saved_model_bindings(host, run_id: str, resolver) -> None:
    """Compare each active saved call to its pinned per-alias selection."""
    run = host._run(run_id)["body"]
    bindings = run.get("profile", {}).get("agentModelBindings")
    if type(bindings) is not dict or not bindings:
        raise ValueError("Saved per-agent model catalog is unavailable")
    for alias, choice in bindings.items():
        selected = resolver(run, alias)
        if (selected["model_id"] != choice.get("model")
                or selected["reasoning_settings"].get("reasoning_effort")
                != choice.get("reasoningEffort")
                or selected["settings"].get("max_completion_tokens")
                != choice.get("maxCompletionTokens")):
            raise ValueError("Saved per-agent model selection differs from catalog")
    for control in host.store.records("mission_task_control"):
        body = control["body"]
        if body.get("run_id") != run_id or not isinstance(body.get("call_ref"), dict):
            continue
        alias = body.get("alias")
        call = host.store.get(**body["call_ref"])
        config = (host.store.get("agent_configuration", call["body"]["configuration_id"], 1)
                  if call else None)
        if config is None:
            raise ValueError("Saved peer call lacks its model configuration")
        selected = resolver(run, alias, prior_configuration=config)
        if any(config["body"].get(key) != selected[key] for key in (
            "provider", "endpoint", "model_id", "settings", "reasoning_settings"
        )):
            raise ValueError("Saved peer call differs from pinned model selection")


def _resume_source_unchanged(lineage: dict) -> bool:
    """Check only the exact pinned private source files, including later ledgers."""
    try:
        source = Path(lineage["source_case"])
        if _state_digest(source / "state") != lineage["source_state_sha256"]:
            return False
        for name, expected in lineage["protected_sha256"].items():
            if name == "private_profile":
                path = source / "runtime" / PROFILE
            elif name in {"case.json", "budget.json", "observation.json",
                          "source-manifest.json", "cumulative-budget.json",
                          "prior-cumulative-budget.json",
                          "campaign-guard.json"}:
                path = source / name
            else:
                return False
            if hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                return False
        return True
    except (KeyError, OSError, TypeError, ValueError):
        return False


def _prior_accounting(budget: dict) -> dict:
    attempts = budget["model_attempts"]
    known = sum(item["actual_tokens"] for item in attempts
                if type(item.get("actual_tokens")) is int)
    unknown = sum(item["planned_tokens"] for item in attempts
                  if item.get("actual_tokens") is None)
    if known + unknown != budget["planned_tokens"]:
        raise ValueError("Stopped case token accounting is inconsistent")
    return {
        "model_http_attempts": len(attempts),
        "retrieval_http_attempts": len(budget["retrieval_attempts"]),
        "known_model_tokens": known,
        "unknown_model_reservations": unknown,
        "settled_or_reserved_tokens": budget["planned_tokens"],
        "stop_reason": budget["stop_reason"],
    }


def _captured_daily_429(state: Path, attempt: dict, receipt: dict) -> bool:
    """Verify one usage-unknown TPD refusal without exposing provider text."""
    if (attempt.get("status") != 429 or attempt.get("state") != "rate_limited"
            or attempt.get("actual_tokens") is not None
            or attempt.get("usage_status") != "unreported_reservation_retained"
            or type(attempt.get("estimated_input_tokens")) is not int
            or type(attempt.get("output_reservation")) is not int
            or attempt["estimated_input_tokens"] < 0
            or attempt["output_reservation"] < 1
            or attempt.get("planned_tokens") != (
                attempt["estimated_input_tokens"] + attempt["output_reservation"])
            or receipt.get("status") != "failed"
            or receipt.get("http_status") != 429
            or receipt.get("error_kind") != "http_error"
            or receipt.get("usage") not in ({}, None)
            or type(receipt.get("retry_after_seconds")) not in {int, float}
            or not math.isfinite(receipt["retry_after_seconds"])
            or receipt["retry_after_seconds"] < 0):
        return False
    pointer = receipt.get("blob")
    if (type(pointer) is not dict or set(pointer) != {"sha256", "size"}
            or type(pointer["size"]) is not int or pointer["size"] < 1
            or not isinstance(pointer["sha256"], str)
            or re.fullmatch(r"[a-f0-9]{64}", pointer["sha256"]) is None):
        return False
    path = state / "objects" / pointer["sha256"]
    if not path.is_file() or path.is_symlink():
        return False
    try:
        raw = path.read_bytes()
        payload = json.loads(raw)
        error = payload.get("provider_error", payload.get("error"))
        headers = receipt.get("rate_headers") or {}
        retry_header = headers.get("retry-after")
        retry_header_number = float(retry_header)
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    return bool(
        len(raw) == pointer["size"]
        and hashlib.sha256(raw).hexdigest() == pointer["sha256"]
        and type(error) is dict and error.get("type") == "tokens"
        and error.get("code") == "rate_limit_exceeded"
        and isinstance(error.get("message"), str)
        and re.search(r"\bTPD\b|tokens per day", error["message"], re.IGNORECASE)
        and math.isfinite(retry_header_number)
        and abs(retry_header_number - receipt["retry_after_seconds"]) < 1e-6
    )


def _controller_segment(budget: dict, controller: float, total: float) -> dict:
    return {"model_attempts": len(budget["model_attempts"]),
            "retrieval_attempts": len(budget["retrieval_attempts"]),
            "started_at": budget["started_at"],
            "controller_elapsed_seconds": controller,
            "elapsed_seconds": total}


_RECOVERED_TOOL_ERRORS = {
    "unregistered_tool_function": "provider_rejected_unregistered_function",
    "route_used_as_function_name": "provider_rejected_unregistered_function",
    "native_tool_validation": "provider_rejected_native_browser_call",
    "optional_selected_id_null": "provider_rejected_null_selected_id",
    "optional_null_argument": "provider_rejected_null_optional_argument",
    "missing_required_argument": "provider_rejected_missing_required_argument",
}

_PENDING_REQUIRED_TYPE_ERROR = re.compile(
    r"^(?:Tool call validation failed: )?(?:tool call validation failed: )?"
    r"parameters for tool (?P<tool>[A-Za-z_][A-Za-z0-9_]*) did not match schema: "
    r"errors: \[`/(?P<field>[A-Za-z_][A-Za-z0-9_]*)`: expected "
    r"(?P<expected>array|object|string|integer|number|boolean), but got "
    r"(?P<observed>array|object|string|integer|number|boolean|null)\]$",
    re.IGNORECASE,
)


def _recognized_tool_validation_repair(
    state: Path, *, run_id: str, mission_id: str, observation: dict,
    rejection_id: str, rejection: dict, last: dict,
    controls: list[tuple[str, dict]], intents: list[tuple[str, dict]],
    dispatches: list[tuple[str, dict]], works: list[tuple[str, dict]],
    capacities: list[tuple[str, dict]], calls: list[tuple[str, dict]],
    configurations: list[tuple[str, dict]], copies: list[tuple[str, dict]],
    uses: list[tuple[str, dict]], use_aliases: dict[str, str],
    generations: list[tuple[str, dict]],
    generation_links: set[tuple[str, str]],
) -> dict | None:
    """Recognize a captured tool correction, still pending or locally recovered.

    Bootstrap stays independent of mutable ora imports. This exact provider
    shape and schema check mirrors the frozen runtime's Groq classifier; the
    copied host must independently accept the local repair before dispatch.
    """
    def blob(pointer: object) -> bytes | None:
        if (type(pointer) is not dict or set(pointer) != {"sha256", "size"}
                or type(pointer["size"]) is not int or pointer["size"] < 1
                or not isinstance(pointer["sha256"], str)
                or re.fullmatch(r"[a-f0-9]{64}", pointer["sha256"]) is None):
            return None
        path = state / "objects" / pointer["sha256"]
        if not path.is_file() or path.is_symlink():
            return None
        raw = path.read_bytes()
        return raw if len(raw) == pointer["size"] and hashlib.sha256(raw).hexdigest() == (
            pointer["sha256"]
        ) else None

    if (not rejection_id or rejection.get("status") != "failed"
            or rejection.get("http_status") != 400
            or rejection.get("error_kind") != "http_error"
            or rejection.get("mission_id") != mission_id
            or rejection.get("request_sha256") != last.get("request_sha256")
            or rejection.get("response_sha256") != last.get("response_sha256")
            or rejection.get("usage") != {}
            or observation.get("dispatch_drain_status") != "settled"
            or observation.get("dispatch_drain_error") is not None):
        return None
    intent_ref = rejection.get("intent_ref")
    call_ref = rejection.get("call_ref")
    if (not isinstance(intent_ref, dict) or not isinstance(call_ref, dict)
            or intent_ref.get("kind") != "model_request_intent"
            or call_ref.get("kind") != "agent_call"
            or intent_ref.get("version") != 1 or call_ref.get("version") != 1):
        return None
    intent = dict(intents).get(intent_ref["id"])
    call = dict(calls).get(call_ref["id"])
    marker = dict(dispatches).get(f"model-dispatch:{intent_ref['id']}")
    if (intent is None or call is None or marker is None
            or intent.get("call_ref") != call_ref
            or intent.get("request_sha256") != last["request_sha256"]
            or marker.get("status") != "captured"
            or marker.get("intent_ref") != intent_ref
            or marker.get("call_ref") != call_ref
            or marker.get("receipt_ref") != {
                "kind": "model_response_receipt", "id": rejection_id, "version": 1,
            }
            or marker.get("request_sha256") != last["request_sha256"]
            or call.get("mission_id") != mission_id
            or call.get("instance_id") != intent.get("instance_id")
            or call.get("task_id") != intent.get("task_id")
            or call.get("configuration_id") != intent.get("configuration_ref", {}).get("id")):
        return None
    config = dict(configurations).get(call["configuration_id"])
    if (config is None or config.get("configuration_sha256")
            != call.get("configuration_sha256")
            or not isinstance(call.get("permitted_tools"), dict)
            or not isinstance(config.get("permitted_tools"), dict)
            or config.get("permitted_tools") != call.get("permitted_tools")
            or config.get("model_id") != call.get("model_id")):
        return None
    raw_request = blob(intent.get("blob"))
    raw_response = blob(rejection.get("blob"))
    if (raw_request is None or raw_response is None
            or hashlib.sha256(raw_request).hexdigest() != last["request_sha256"]):
        return None
    try:
        request = json.loads(raw_request)
        payload = json.loads(raw_response)
        if not isinstance(payload, dict):
            return None
        error = payload.get("provider_error")
        message = error.get("message") if isinstance(error, dict) else None
        match = _PENDING_REQUIRED_TYPE_ERROR.fullmatch(message) if isinstance(message, str) else None
        if (not isinstance(request, dict) or request.get("model") != call["model_id"]
                or not isinstance(error, dict)
                or error.get("type") != "invalid_request_error"
                or error.get("code") != "tool_use_failed"):
            return None
        if match is not None:
            tool, field = match.group("tool", "field")
            expected = match.group("expected").lower()
            observed = match.group("observed").lower()
            matched = [item["function"] for item in request.get("tools", [])
                       if isinstance(item, dict) and item.get("type") == "function"
                       and isinstance(item.get("function"), dict)
                       and item["function"].get("name") == tool]
            descriptor = call.get("permitted_tools", {}).get(tool)
            schema = descriptor.get("schema") if isinstance(descriptor, dict) else None
            required = schema.get("required") if isinstance(schema, dict) else None
            properties = schema.get("properties") if isinstance(schema, dict) else None
            if (expected == observed or len(matched) != 1
                    or tool not in call.get("exposed_tools", [])
                    or not isinstance(schema, dict) or schema.get("type") != "object"
                    or not isinstance(required, list) or field not in required
                    or not isinstance(properties, dict)
                    or not isinstance(properties.get(field), dict)
                    or properties[field].get("type") != expected
                    or matched[0] != {
                        "name": tool, "description": descriptor.get("description"),
                        "parameters": schema,
                    }):
                return None
            expected_feedback = {
                "status": "provider_rejected_wrong_type_argument",
                "function_name": tool, "field": field,
                "expected_type": expected, "observed_type": observed,
            }
        elif message == (
            "Failed to call a function. Please adjust your prompt. "
            "See 'failed_generation' for more details."
        ):
            pointer = rejection.get("failed_generation_ref")
            if (not isinstance(pointer, dict) or set(pointer) != {"kind", "id", "version"}
                    or pointer["kind"] != "restricted_failed_generation"
                    or pointer["version"] != 1
                    or pointer["id"] != f"provider-failed-generation:{intent_ref['id']}"
                    or (rejection_id, pointer["id"]) not in generation_links
                    or rejection.get("failed_generation_status") != "returned"):
                return None
            capture = dict(generations).get(pointer["id"])
            if (not isinstance(capture, dict)
                    or capture.get("schema") != "ora.provider_failed_generation.v1"
                    or capture.get("status") != "returned"
                    or capture.get("access") != "restricted_diagnostics"
                    or capture.get("origin") != "provider_http_error"
                    or capture.get("accepted_as_answer_or_tool") is not False
                    or capture.get("mission_id") != mission_id
                    or capture.get("run_id") != run_id
                    or capture.get("task_id") != call["task_id"]
                    or capture.get("instance_id") != call["instance_id"]
                    or capture.get("call_id") != call_ref["id"]
                    or capture.get("call_version") != call_ref["version"]
                    or capture.get("attempt_id") != intent.get("attempt_id")
                    or capture.get("configuration_ref") != intent.get("configuration_ref")
                    or capture.get("intent_ref") != intent_ref
                    or capture.get("request_sha256") != last["request_sha256"]
                    or capture.get("response_sha256") != last["response_sha256"]):
                return None
            generated = blob(capture.get("blob"))
            if (not generated or capture.get("text_bytes") != len(generated)
                    or capture.get("text_sha256") != hashlib.sha256(generated).hexdigest()):
                return None
            generated.decode("utf-8")  # Validate storage only; never parse rejected arguments.
            exposed = call.get("exposed_tools")
            if (not isinstance(exposed, list) or not exposed
                    or len(exposed) != len(set(exposed))):
                return None
            expected_tools = {name: {"type": "function", "function": {
                "name": name, "description": call["permitted_tools"][name]["description"],
                "parameters": call["permitted_tools"][name]["schema"],
            }} for name in exposed}
            actual_tools = request.get("tools")
            if (not isinstance(actual_tools, list) or len(actual_tools) != len(exposed)
                    or any(not isinstance(item, dict)
                           or not isinstance(item.get("function"), dict)
                           for item in actual_tools)):
                return None
            keyed_tools = {item["function"].get("name"): item for item in actual_tools}
            if keyed_tools != expected_tools:
                return None
            expected_feedback = {"status": "provider_rejected_malformed_tool_generation"}
        else:
            return None
    except (OSError, KeyError, TypeError, ValueError, UnicodeDecodeError, AttributeError):
        return None
    matching_work = [(identifier, body) for identifier, body in works
                     if body.get("mission_id") == mission_id
                     and isinstance(body.get("spec"), dict)
                     and body["spec"].get("call_id") == call_ref["id"]]
    if len(matching_work) != 1:
        return None
    work_id, work = matching_work[0]
    spec = work["spec"]
    cap_id = "executor-cap:" + work_id.removeprefix("executor-work:")
    cap = dict(capacities).get(cap_id)
    matching_control = [(identifier, body) for identifier, body in controls
                        if body.get("run_id") == run_id and body.get("actor_id")
                        == spec.get("actor_id") and body.get("task_id") == call["task_id"]]
    if (not work_id.startswith("executor-work:") or len(matching_control) != 1
            or work.get("phase") != "pending" or work.get("result_ref") is not None
            or spec.get("kind") != "model" or spec.get("handler_id") != "mission-model"
            or spec.get("call_version") != 1 or spec.get("run_id") != run_id
            or spec.get("input_refs") != intent.get("source_refs")
            or cap is None or cap.get("phase") != "failed"
            or cap.get("reservation") is not False
            or cap.get("external_active") is not False
            or cap.get("local_active") is not False
            or cap.get("cancel_requested") is True
            or not isinstance(cap.get("attempts"), list) or not cap["attempts"]
            or not isinstance(cap["attempts"][-1], dict)
            or cap["attempts"][-1].get("status") != "failed"
            or cap["attempts"][-1].get("usage_known") is not False
            or type(last.get("estimated_input_tokens")) is not int
            or type(last.get("output_reservation")) is not int
            or type(last.get("planned_tokens")) is not int
            or type(spec.get("estimated_tokens")) is not int
            or spec["estimated_tokens"] < last["estimated_input_tokens"]
            or type(spec.get("output_reservation_tokens")) is not int
            or spec["output_reservation_tokens"] != last["output_reservation"]
            or last["planned_tokens"] != (
                last["estimated_input_tokens"] + last["output_reservation"]
            )
            or cap["attempts"][-1].get("charged_tokens")
            != spec["estimated_tokens"]
            or cap["attempts"][-1].get("reserved_tokens")
            != spec["estimated_tokens"]
            or cap["attempts"][-1].get("charged_output_tokens")
            != spec["output_reservation_tokens"]
            or cap["attempts"][-1].get("reserved_output_tokens")
            != spec["output_reservation_tokens"]
            or matching_control[0][1].get("call_ref") != call_ref):
        return None
    control = matching_control[0][1]
    feedback = control.get("feedback")
    # A later rejected call can still carry feedback for an earlier failure.
    # It is pending recovery only when that unchanged feedback was in the exact
    # sent input, not when it claims to have recovered this latest receipt.
    prior_feedback_sent = False
    if (isinstance(feedback, dict) and feedback.get("previous_response_ref") != {
        "kind": "model_response_receipt", "id": rejection_id, "version": 1,
    }):
        for message in request.get("messages", []):
            try:
                card = json.loads(message.get("content"))
            except (ValueError, TypeError, AttributeError):
                continue
            if isinstance(card, dict) and card.get("host_feedback") == feedback:
                prior_feedback_sent = True
                break
    pending = (control.get("phase") == "waiting"
               and control.get("work_id") == spec.get("work_id")
               and (feedback is None or prior_feedback_sent))
    recovered = (control.get("phase") == "ready_model"
                 and control.get("work_id") is None
                 and isinstance(feedback, dict)
                 and feedback.get("previous_response_ref") == {
                     "kind": "model_response_receipt", "id": rejection_id,
                     "version": 1,
                 }
                 and all(feedback.get(key) == value
                         for key, value in expected_feedback.items()))
    if not (pending or recovered):
        return None
    # This pending-repair boundary is only for source copies still current and
    # explicitly permitted. A changed copy/use must not inherit the rich text.
    current_copies = dict(copies)
    current_uses = dict(uses)
    source_refs = intent.get("source_refs")
    input_refs = call.get("input_refs")
    if (not isinstance(source_refs, list) or not source_refs
            or not isinstance(input_refs, list)
            or len(input_refs) != len(source_refs)):
        return None
    for pointer, input_ref in zip(source_refs, input_refs, strict=True):
        if (not isinstance(pointer, dict) or pointer.get("kind") != "source_copy"
                or pointer.get("version") != 1):
            return None
        copy = current_copies.get(pointer.get("id"))
        use_id = use_aliases.get(f"{pointer['id']}:{pointer['version']}")
        use = current_uses.get(use_id)
        if (copy is None or use is None
                or copy["version"] != pointer["version"]
                or not isinstance(input_ref, dict)
                or {key: input_ref.get(key) for key in ("kind", "id", "version")}
                != pointer
                or input_ref.get("sha256") != hashlib.sha256(json.dumps(
                    copy["body"], sort_keys=True, separators=(",", ":"),
                    ensure_ascii=False,
                ).encode()).hexdigest()
                or use.get("copy_ref") != pointer
                or use.get("policy_status") != "granted"
                or "model_input" not in use.get("allowed_uses", [])
                or use.get("source_sha256") != copy["body"].get("content_sha256")
                or copy["body"].get("model_use") == "restricted"):
            return None
    return {"control_id": matching_control[0][0],
            "receipt_id": rejection_id,
            **{key: value for key, value in expected_feedback.items() if key != "status"},
            "expected_feedback": expected_feedback,
            "repair_state": "pending" if pending else "recovered"}


def _historical_blocked_capacity_ok(*, marker: dict, intent: dict, work: dict,
                                    capacity: dict, prior_segment: dict,
                                    prior_ledger: dict, run_id: str,
                                    mission_id: str) -> bool:
    """Match a prior no-effect denial to its saved capacity and finite phase."""
    if not all(isinstance(item, dict) for item in (
        marker, intent, work, capacity, prior_segment, prior_ledger,
    )):
        return False
    estimate = intent.get("request_token_estimate")
    cap_estimate = capacity.get("request_token_estimate")
    spec = work.get("spec")
    attempts = capacity.get("attempts")
    limits = prior_ledger.get("limits")
    if (not all(isinstance(item, dict) for item in (
            estimate, cap_estimate, spec, limits,
        )) or not isinstance(attempts, list) or len(attempts) != 1
            or not isinstance(attempts[0], dict)
            or not isinstance(spec.get("proposal_ref"), dict)
            or not isinstance(marker.get("call_ref"), dict)
            or type(estimate.get("tokens")) is not int
            or type(spec.get("output_reservation_tokens")) is not int
            or type(prior_ledger.get("planned_tokens")) is not int
            or type(limits.get("tokens")) is not int):
        return False
    attempt = attempts[0]
    at = attempt.get("at")
    started = prior_segment.get("started_at")
    duration = prior_segment.get("controller_elapsed_seconds")
    if (any(type(value) not in {int, float} or not math.isfinite(value)
            for value in (at, started, duration))):
        return False
    return bool(
        capacity.get("kind") == "model"
        and capacity.get("phase") == "failed"
        and capacity.get("diagnostic") == "confirmed_pre_dispatch_no_effect"
        and capacity.get("reservation") is False
        and capacity.get("external_active") is False
        and capacity.get("mission_id") == mission_id
        and capacity.get("run_id") == run_id
        and capacity.get("call_id") == marker["call_ref"].get("id")
        and capacity.get("task_id") == spec["proposal_ref"].get("id")
        and capacity.get("output_reservation_tokens")
        == spec["output_reservation_tokens"]
        and cap_estimate.get("intent_ref") == marker.get("intent_ref")
        and cap_estimate.get("request_sha256") == marker.get("request_sha256")
        and all(cap_estimate.get(key) == estimate.get(key)
                for key in ("tokens", "basis", "asset_sha256", "fallback_reason"))
        and capacity.get("estimate_basis") == cap_estimate.get("basis")
        and attempt.get("status") == "terminal_no_effect"
        and attempt.get("usage_known") is True
        and attempt.get("output_usage_known") is True
        and attempt.get("charged_tokens") == 0
        and attempt.get("charged_output_tokens") == 0
        and attempt.get("reserved_output_tokens")
        == spec["output_reservation_tokens"]
        and attempt.get("reserved_tokens") == capacity.get("estimated_tokens")
        and started <= at <= started + duration
        and prior_ledger["planned_tokens"] + estimate["tokens"]
        + spec["output_reservation_tokens"] > limits["tokens"]
    )


def _resume_source(root: Path, source_case: Path, request_data: dict,
                   execution_mode: str, *, followup: bool = False,
                   model_limit: int | None = None, token_limit: int | None = None,
                   retrieval_limit: int | None = None, deadline_seconds: int | None = None,
                   accounting_only: bool = True, until_complete: bool = True,
                   token_ceiling_increase: int | None = None) -> dict:
    """Validate one closed rejection or clean controlled stop without mutating ora."""
    source_case = Path(source_case).resolve()
    private = (root / ".local/search-validation").resolve()
    if source_case.parent != private or not source_case.is_dir():
        raise ValueError("Resume needs one existing private case directory")
    paths = {name: source_case / name for name in (
        "case.json", "budget.json", "observation.json", "source-manifest.json",
    )}
    paths["private_profile"] = source_case / "runtime" / PROFILE
    state = source_case / "state"
    if any(not path.is_file() for path in paths.values()) or not (state / "state.sqlite3").is_file():
        raise ValueError("Stopped case files or state are unavailable")
    if (state.is_symlink() or (state / "state.sqlite3").is_symlink()
            or (state / "state.sqlite3-wal").is_symlink()):
        raise ValueError("Stopped state must be a private directory, not a link")
    source_state_before = _state_digest(state)
    case = json.loads(paths["case.json"].read_text())
    budget = json.loads(paths["budget.json"].read_text())
    observation = json.loads(paths["observation.json"].read_text())
    profile = json.loads(paths["private_profile"].read_text())
    case_qwen = any(
        type(choice) is dict and choice.get("model") == "qwen/qwen3.8-27b"
        for choices in (case.get("agent_models"), case.get("agent_model_bindings"))
        if type(choices) is dict for choice in choices.values()
    )
    qwen_estimate_policy = case.get("qwen_estimate_policy", "legacy_surrogate")
    if (type(qwen_estimate_policy) is not str
            or qwen_estimate_policy not in {"legacy_surrogate", "native_when_supported"}
            or qwen_estimate_policy == "native_when_supported" and not case_qwen):
        raise ValueError("Stopped case has an invalid Qwen estimate policy")
    phase = case.get("phase", 1)
    if type(phase) is not int or phase < 1:
        raise ValueError("Stopped case phase is invalid")
    aggregate_path = (source_case / "cumulative-budget.json" if phase > 1
                      else paths["budget.json"])
    if not aggregate_path.is_file():
        raise ValueError("Stopped continuation lacks its cumulative HTTP ledger")
    aggregate = json.loads(aggregate_path.read_text())
    revisions = aggregate.get("token_ceiling_revisions", [])
    if (type(revisions) is not list
            or ("token_ceiling_revisions" in aggregate and not revisions)
            or case.get("token_ceiling_revision") != (revisions[-1] if revisions else None)):
        raise ValueError("Stopped case token ceiling history is inconsistent")
    previous_ceiling = None
    for revision in revisions:
        if (type(revision) is not dict
                or set(revision) != {"schema", "prior_limit_tokens", "additional_tokens",
                                     "new_limit_tokens", "source_ledger_sha256"}
                or revision["schema"] != "ora.validation_token_ceiling_revision.v1"
                or any(type(revision[key]) is not int or revision[key] < 1 for key in (
                    "prior_limit_tokens", "additional_tokens", "new_limit_tokens"))
                or revision["new_limit_tokens"] != revision["prior_limit_tokens"] + revision["additional_tokens"]
                or previous_ceiling is not None and revision["prior_limit_tokens"] != previous_ceiling
                or type(revision["source_ledger_sha256"]) is not str
                or len(revision["source_ledger_sha256"]) != 64
                or any(char not in "0123456789abcdef" for char in revision["source_ledger_sha256"])):
            raise ValueError("Stopped case token ceiling history is invalid")
        previous_ceiling = revision["new_limit_tokens"]
    if revisions and (not isinstance(aggregate.get("limits"), dict)
                      or previous_ceiling != aggregate["limits"].get("tokens")):
        raise ValueError("Stopped case token ceiling history disagrees with its limit")
    if phase > 1:
        prior_copy_path = source_case / "prior-cumulative-budget.json"
        if not prior_copy_path.is_file():
            raise ValueError("Stopped continuation lacks its copied prior ledger")
        paths["prior-cumulative-budget.json"] = prior_copy_path
        prior_copy_bytes = prior_copy_path.read_bytes()
        saved_lineage = case.get("resume_lineage")
        if (type(saved_lineage) is not dict
                or saved_lineage.get("prior_cumulative_budget_sha256")
                != hashlib.sha256(prior_copy_bytes).hexdigest()):
            raise ValueError("Stopped continuation prior ledger differs from its lineage")
        prior_copy = json.loads(prior_copy_bytes)
        if type(prior_copy) is not dict:
            raise ValueError("Stopped continuation copied prior ledger is invalid")
        introduced = saved_lineage.get("token_ceiling_revision")
        if introduced is not None:
            if (not revisions or introduced != revisions[-1]
                    or not isinstance(prior_copy.get("limits"), dict)
                    or prior_copy["limits"].get("tokens") != introduced["prior_limit_tokens"]
                    or prior_copy.get("token_ceiling_revisions", []) != revisions[:-1]
                    or introduced["source_ledger_sha256"] != hashlib.sha256(json.dumps(
                        prior_copy, sort_keys=True, separators=(",", ":"),
                        ensure_ascii=False,
                    ).encode()).hexdigest()):
                raise ValueError("Stopped token ceiling grant differs from its prior ledger")
        elif (prior_copy.get("limits") != aggregate.get("limits")
              or prior_copy.get("token_ceiling_revisions", []) != revisions):
            raise ValueError("Stopped continuation changed its ceiling without a grant")
    if phase > 1:
        paths["cumulative-budget.json"] = aggregate_path
    request_id = request_data.get("request_id")
    run_id = "run-" + hashlib.sha256(json.dumps((request_id,), separators=(",", ":")).encode()).hexdigest()[:32]
    expected_lineage = ({key: request_data.get(key) for key in (
        "parent_run_id", "parent_snapshot_id", "target_id", "target_version"
    )} if followup else None)
    if (case.get("request_id") != request_id or case.get("run_id") != run_id
            or case.get("question") != request_data.get("question")
            or case.get("credential_profile") != request_data.get("credential_profile")
            or case.get("model") != "openai/gpt-oss-120b"
            or ("agent_models" in case) != ("agent_models" in request_data)
            or case.get("agent_models") != request_data.get("agent_models")
            or ("agent_models" in case and "agent_model_bindings" not in case)
            or case.get("execution_mode") != execution_mode
            or case.get("allow_vision") != request_data.get("allow_vision", False)
            or case.get("parent_lineage") != expected_lineage
            or (followup and (not isinstance(case.get("copied_parent_state_sha256"), str)
                              or observation.get("parent_state_unchanged") is not True))
            or case.get("accounting_only") is not accounting_only
            or case.get("until_complete") is not until_complete
            or any(case.get(name) != value for name, value in (
                ("max_model_calls", model_limit),
                ("max_planned_tokens", token_limit),
                ("max_retrieval_attempts", retrieval_limit),
                ("deadline_seconds", deadline_seconds),
            ))):
        raise ValueError("Resume request, model or accounting mode differs from stopped case")
    attempts = budget.get("model_attempts")
    retrieval = budget.get("retrieval_attempts")
    cap_stop = budget.get("stop_reason") == "campaign_phase_cap"
    token_cap_stop = budget.get("stop_reason") == "token_cap"
    if token_cap_stop and token_ceiling_increase is None:
        raise ValueError("A settled token cap needs an explicit ceiling increase")
    clean_stop = budget.get("stop_reason") in {
        "observation_window_ended", "campaign_phase_cap", "token_cap",
    }
    uncertain_stop = (
        budget.get("stop_reason") == "uncertain_transport"
        and not accounting_only
    )
    finite_observation_429 = (
        budget.get("stop_reason") == "observation_window_ended"
        and not accounting_only
        and any(item.get("status") == 429 for item in aggregate.get("model_attempts", []))
    )
    prior_phase_attempts = prior_copy.get("model_attempts", []) if phase > 1 else []
    historical_unknown = ([item for item in prior_phase_attempts
                           if item.get("state") == "uncertain"]
                          if uncertain_stop and isinstance(prior_phase_attempts, list)
                          else [])
    historical_unknown_sha = {item.get("request_sha256")
                              for item in historical_unknown}
    if (uncertain_stop and phase > 1
            and (not isinstance(prior_phase_attempts, list)
                 or not isinstance(aggregate.get("model_attempts"), list)
                 or aggregate.get("model_attempts", [])[:len(prior_phase_attempts)]
                 != prior_phase_attempts
                 or aggregate.get("retrieval_attempts")
                 != prior_copy.get("retrieval_attempts"))):
        raise ValueError("Uncertain continuation changed its prior HTTP ledger")
    guard = None
    if cap_stop:
        guard_path = source_case / "campaign-guard.json"
        if not guard_path.is_file():
            raise ValueError("Campaign cap stop lacks its exact pre-dispatch guard receipt")
        paths["campaign-guard.json"] = guard_path
        guard = json.loads(guard_path.read_text())
        blocked = guard.get("blocked_candidate") if isinstance(guard, dict) else None
        if (not isinstance(guard, dict)
                or guard.get("schema") != "private_campaign_phase_guard.v1"
                or guard.get("canonical_case_cumulative_limit") != token_limit
                or not isinstance(budget.get("limits"), dict)
                or type(guard.get("canonical_new_phase_limit")) is not int
                or guard.get("canonical_new_phase_limit") != budget.get("limits", {}).get("tokens")
                or type(guard.get("effective_new_phase_limit")) is not int
                or not 0 < guard["effective_new_phase_limit"] <= guard["canonical_new_phase_limit"]
                or type(guard.get("campaign_prior_charge")) is not int
                or type(guard.get("campaign_total_limit")) is not int
                or guard["campaign_prior_charge"] < 0
                or guard["campaign_total_limit"] - guard["campaign_prior_charge"]
                != guard["effective_new_phase_limit"]
                or not isinstance(blocked, dict)
                or blocked.get("external_action") != "blocked_before_http"
                or type(blocked.get("prior_phase_charge")) is not int
                or blocked["prior_phase_charge"] != budget.get("planned_tokens")
                or type(blocked.get("estimated_input_tokens")) is not int
                or type(blocked.get("max_completion_tokens")) is not int
                or blocked["estimated_input_tokens"] < 0
                or blocked["max_completion_tokens"] < 1
                or type(blocked.get("candidate_reservation")) is not int
                or blocked["candidate_reservation"] != (
                    blocked["estimated_input_tokens"] + blocked["max_completion_tokens"])
                or blocked["prior_phase_charge"] + blocked["candidate_reservation"]
                <= guard["effective_new_phase_limit"]
                or not isinstance(blocked.get("request_sha256"), str)
                or len(blocked["request_sha256"]) != 64
                or any(char not in "0123456789abcdef" for char in blocked["request_sha256"])):
            raise ValueError("Campaign cap stop has an invalid guard receipt")
    # A campaign cap can reject the first new HTTP request after a prior phase.
    # Its last captured response is then in the cumulative ledger, not this phase.
    last = (attempts[-1] if isinstance(attempts, list) and attempts else
            aggregate["model_attempts"][-1] if cap_stop and phase > 1
            and isinstance(aggregate.get("model_attempts"), list)
            and aggregate["model_attempts"] else {})
    status = last.get("status")
    expected_outcome = (budget["stop_reason"] if clean_stop or uncertain_stop else
                        "provider_daily_quota_wait" if status == 429 else
                        "model_http_error")
    terminal_http = (
        isinstance(aggregate.get("model_attempts"), list)
        and bool(aggregate["model_attempts"])
        and isinstance(attempts, list)
        and (bool(attempts) or cap_stop and phase > 1)
        and (not attempts or [
            {**item, "number": index}
            for index, item in enumerate(aggregate["model_attempts"][-len(attempts):], 1)
        ] == attempts)
        and isinstance(aggregate.get("retrieval_attempts"), list)
        and aggregate["retrieval_attempts"] == []
        and retrieval == []
        and (not token_cap_stop or (
            aggregate["model_attempts"][: -len(attempts)] == (
                prior_copy.get("model_attempts") if phase > 1 else []
            )
            and (phase == 1 or prior_copy.get("retrieval_attempts") == [])
        ))
        and all(isinstance(item, dict)
            and isinstance(item.get("request_sha256"), str)
            and (isinstance(item.get("response_sha256"), str)
                 or uncertain_stop and item.get("response_sha256") is None
                 and (item.get("request_sha256") in historical_unknown_sha
                      or item.get("request_sha256") == last.get("request_sha256")))
            and (
                item.get("status") == 200 and item.get("state") == "observed"
                and type(item.get("actual_tokens")) is int
                and item["actual_tokens"] >= 0
                or uncertain_stop and (
                    item.get("request_sha256") in historical_unknown_sha
                    or item.get("request_sha256") == last.get("request_sha256"))
                and item.get("status") is None
                and item.get("state") == "uncertain"
                and item.get("actual_tokens") is None
                and item.get("failure_stage") == "opening"
                and item.get("failure_category") in {"connection", "dns", "timeout", "other"}
                and item.get("usage_status") is None
                and type(item.get("estimated_input_tokens")) is int
                and type(item.get("output_reservation")) is int
                and item.get("planned_tokens") == (
                    item["estimated_input_tokens"] + item["output_reservation"])
                or finite_observation_429 and item.get("status") == 429
                and item.get("state") == "rate_limited"
                and item.get("actual_tokens") is None
            )
            for item in (attempts if token_cap_stop else aggregate.get("model_attempts", [])))
        if clean_stop or uncertain_stop else
        status in {400, 413, 429}
        and last.get("state") == ("rate_limited" if status == 429 else "observed")
        and last.get("actual_tokens") is None
        and isinstance(last.get("request_sha256"), str)
        and isinstance(last.get("response_sha256"), str)
    )
    if (not isinstance(attempts, list) or cap_stop and phase <= 1
            or not attempts and not (cap_stop and phase > 1)
            or not isinstance(retrieval, list)
            or budget.get("stop_reason") != expected_outcome
            or not terminal_http
            or (not clean_stop and status == 429 and (
                last.get("quota_classification") != "provider_daily_limit"
                or type(last.get("retry_after_seconds")) not in {int, float}
                or not math.isfinite(last["retry_after_seconds"])
                or last["retry_after_seconds"] < 0))
            or any(item.get("state") in {"attempted", "uncertain"}
                   for item in [*(attempts[:-1] if uncertain_stop else attempts),
                                *retrieval])
            or observation.get("run_id") != run_id
            or observation.get("outcome") != expected_outcome
            or observation.get("mission_status") != "active"
            or observation.get("stop_reason") != budget["stop_reason"]
            or observation.get("model_attempts") != len(aggregate.get("model_attempts", []))
            or observation.get("retrieval_attempts") != len(aggregate.get("retrieval_attempts", []))
            or observation.get("settled_or_reserved_tokens") != aggregate.get("planned_tokens")
            or ((clean_stop or uncertain_stop) and (
                observation.get("dispatch_drain_status") != "settled"
                or observation.get("dispatch_drain_error") is not None))
            or observation.get("shutdown_pending") is not False
            or observation.get("private_profile_disabled") is not True
            or observation.get("original_profile_unchanged") is not True
            or observation.get("evaluation_observation_saved") is not True
            or any(profile.get("profiles", {}).get(name, {}).get("enabled") is not False
                   or profile["profiles"][name].get("allowed_run_ids") != []
                   for name in ("primary", "backup"))):
        raise ValueError("Resume needs a closed, settled, controlled stop" if clean_stop or uncertain_stop
                         else "Resume needs a closed, settled, recognized provider rejection")
    phase_accounting = _prior_accounting(budget)
    prior = observation.get("cumulative_accounting", phase_accounting)
    if (not isinstance(prior, dict)
            or any(type(prior.get(key)) is not int or prior[key] < phase_accounting[key]
                   for key in ("model_http_attempts", "retrieval_http_attempts",
                               "known_model_tokens", "unknown_model_reservations",
                               "settled_or_reserved_tokens"))
            or prior["known_model_tokens"] + prior["unknown_model_reservations"]
            != prior["settled_or_reserved_tokens"]
            or _prior_accounting(aggregate)["model_http_attempts"]
            != prior["model_http_attempts"]
            or _prior_accounting(aggregate)["retrieval_http_attempts"]
            != prior["retrieval_http_attempts"]
            or _prior_accounting(aggregate)["settled_or_reserved_tokens"]
            != prior["settled_or_reserved_tokens"]):
        raise ValueError("Stopped case cumulative accounting is inconsistent")
    cumulative_limits = {
        "model": model_limit, "tokens": token_limit, "retrieval": retrieval_limit,
        **({"accounting_only": True} if accounting_only else {}),
        **({"allow_vision": True} if case["allow_vision"] else {}),
        **({"allow_qwen_text": True} if case_qwen else {}),
        **({"qwen_estimate_policy": qwen_estimate_policy}
           if qwen_estimate_policy != "legacy_surrogate" else {}),
    }
    if accounting_only:
        phase_limits = cumulative_limits
    else:
        if (type(token_limit) is not int or token_limit < 1
                or type(retrieval_limit) is not int or retrieval_limit < 0
                or model_limit is not None
                and (type(model_limit) is not int or model_limit < 1)
                or type(deadline_seconds) is not int or deadline_seconds < 1):
            raise ValueError("Finite resume needs positive cumulative tokens and a deadline")
        prior_phase_tokens = aggregate["planned_tokens"] - budget["planned_tokens"]
        prior_phase_models = (len(aggregate["model_attempts"])
                              - len(budget["model_attempts"]))
        prior_phase_retrieval = (len(aggregate["retrieval_attempts"])
                                 - len(budget["retrieval_attempts"]))
        phase_limits = {
            "model": (None if model_limit is None else model_limit - prior_phase_models),
            "tokens": token_limit - prior_phase_tokens,
            "retrieval": retrieval_limit - prior_phase_retrieval,
            **({"allow_vision": True} if case["allow_vision"] else {}),
            **({"allow_qwen_text": True} if case_qwen else {}),
            **({"qwen_estimate_policy": qwen_estimate_policy}
               if qwen_estimate_policy != "legacy_surrogate" else {}),
        }
        if (phase_limits["tokens"] < 1 or phase_limits["retrieval"] < 0
                or phase_limits["model"] is not None and phase_limits["model"] < 1
                or prior["settled_or_reserved_tokens"] > token_limit
                or prior["retrieval_http_attempts"] > retrieval_limit
                or model_limit is not None and prior["model_http_attempts"] > model_limit):
            raise ValueError("Stopped case exhausted its finite cumulative allowance")
    if aggregate.get("limits") != cumulative_limits or budget.get("limits") != phase_limits:
        raise ValueError("Stopped case cumulative and phase limits disagree")
    grant = None
    effective_token_limit = token_limit
    if token_ceiling_increase is not None:
        if (accounting_only or type(token_ceiling_increase) is not int
                or token_ceiling_increase < 1 or type(token_limit) is not int):
            raise ValueError("Resume token ceiling increase needs a positive finite grant")
        effective_token_limit = token_limit + token_ceiling_increase
        grant = {
            "schema": "ora.validation_token_ceiling_revision.v1",
            "prior_limit_tokens": token_limit,
            "additional_tokens": token_ceiling_increase,
            "new_limit_tokens": effective_token_limit,
            "source_ledger_sha256": hashlib.sha256(json.dumps(
                aggregate, sort_keys=True, separators=(",", ":"),
                ensure_ascii=False,
            ).encode()).hexdigest(),
        }
    next_phase_limits = {
        "model": (None if model_limit is None else model_limit - prior["model_http_attempts"]),
        "tokens": (None if accounting_only else effective_token_limit - prior["settled_or_reserved_tokens"]),
        "retrieval": (None if accounting_only else retrieval_limit - prior["retrieval_http_attempts"]),
    }
    if (not accounting_only and (next_phase_limits["tokens"] < 1
            or next_phase_limits["retrieval"] < 0
            or next_phase_limits["model"] is not None
            and next_phase_limits["model"] < 1)):
        raise ValueError("Stopped case has no remaining finite allowance")
    prior_timing = {key: observation.get(key) for key in (
        "controller_elapsed_seconds", "elapsed_seconds", "post_controller_finalization_seconds",
    )}
    if (any(type(value) not in {int, float} or value < 0 for value in prior_timing.values())
            or abs(prior_timing["elapsed_seconds"] - prior_timing["controller_elapsed_seconds"]
                   - prior_timing["post_controller_finalization_seconds"]) > 1e-6):
        raise ValueError("Stopped case timing is inconsistent")
    segments = (observation.get("controller_segments") if phase > 1
                else [_controller_segment(budget, prior_timing["controller_elapsed_seconds"],
                                          prior_timing["elapsed_seconds"])])
    if (not isinstance(segments, list) or len(segments) != phase
            or any(not isinstance(item, dict)
                   or any(type(item.get(key)) is not int or item[key] < 0
                          for key in ("model_attempts", "retrieval_attempts"))
                   or any(type(item.get(key)) not in {int, float} or item[key] < 0
                          for key in ("started_at", "controller_elapsed_seconds",
                                      "elapsed_seconds"))
                   or item["elapsed_seconds"] < item["controller_elapsed_seconds"]
                   for item in segments)
            or sum(item["model_attempts"] for item in segments) != prior["model_http_attempts"]
            or sum(item["retrieval_attempts"] for item in segments)
            != prior["retrieval_http_attempts"]
            or abs(sum(item["controller_elapsed_seconds"] for item in segments)
                   - prior_timing["controller_elapsed_seconds"]) > 1e-6
            or abs(sum(item["elapsed_seconds"] for item in segments)
                   - prior_timing["elapsed_seconds"]) > 1e-6):
        raise ValueError("Stopped case controller segments are inconsistent")
    try:
        # Opening a WAL database in read-only mode can still checkpoint or
        # update its -shm sidecar on close. Inspect a private byte copy so the
        # stopped case remains exactly as it was before resume validation.
        with tempfile.TemporaryDirectory(prefix="ora-resume-sqlite-") as scratch:
            database = Path(scratch) / "state.sqlite3"
            shutil.copyfile(state / "state.sqlite3", database)
            wal = state / "state.sqlite3-wal"
            if wal.is_file():
                shutil.copyfile(wal, Path(scratch) / "state.sqlite3-wal")
            db = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
            try:
                db.execute("PRAGMA query_only=ON")
                def heads(kind):
                    return [(record_id, json.loads(body)) for record_id, body in db.execute(
                        "SELECT r.id,r.body FROM records r JOIN heads h USING(kind,id,version) "
                        "WHERE r.kind=?", (kind,),
                    )]
                runs = heads("mission_run")
                responses = heads("model_response_receipt")
                response_created_at = dict(db.execute(
                    "SELECT r.id,r.created_at FROM records r "
                    "JOIN heads h USING(kind,id,version) "
                    "WHERE r.kind='model_response_receipt'"
                )) if clean_stop else {}
                inspect_pending_repair = not clean_stop and not uncertain_stop and status == 400
                inspect_dispatch = clean_stop or uncertain_stop or inspect_pending_repair
                dispatches = heads("model_dispatch_attempt") if inspect_dispatch else []
                dispatch_created_at = dict(db.execute(
                    "SELECT id,created_at FROM records "
                    "WHERE kind='model_dispatch_attempt' AND version=1"
                )) if inspect_dispatch else {}
                intents = heads("model_request_intent") if inspect_dispatch else []
                works = heads("executor_work") if inspect_dispatch else []
                capacities = (heads("capacity_operation")
                              if inspect_dispatch else [])
                instances = ([(identifier, json.loads(body))
                              for identifier, body in db.execute(
                                  "SELECT id,body FROM records WHERE kind='agent_instance' "
                                  "AND version=1"
                              )] if inspect_dispatch else [])
                proposals = ([(identifier, json.loads(body))
                              for identifier, body in db.execute(
                                  "SELECT id,body FROM records WHERE kind='board_proposal' "
                                  "AND version=1"
                              )] if inspect_dispatch else [])
                calls = heads("agent_call") if inspect_dispatch else []
                configurations = (heads("agent_configuration")
                                  if inspect_dispatch else [])
                recoveries = heads("model_response_lost") if clean_stop or uncertain_stop else []
                generations = ([(identifier, json.loads(body)) for identifier, body in db.execute(
                    "SELECT id,body FROM records WHERE kind='restricted_failed_generation' "
                    "AND version=1"
                )] if inspect_pending_repair else [])
                generation_links = (set(db.execute(
                    "SELECT source_id,target_id FROM links "
                    "WHERE source_kind='model_response_receipt' AND source_version=1 "
                    "AND target_kind='restricted_failed_generation' AND target_version=1 "
                    "AND relation='has_restricted_failed_generation'"
                )) if inspect_pending_repair else set())
                copies = ([(record_id, {"version": version, "body": json.loads(body)})
                           for record_id, version, body in db.execute(
                               "SELECT r.id,r.version,r.body FROM records r "
                               "JOIN heads h USING(kind,id,version) WHERE r.kind='source_copy'"
                           )] if inspect_pending_repair else [])
                uses = heads("source_use") if inspect_pending_repair else []
                use_aliases = ({value: ident for value, ident in db.execute(
                    "SELECT value,id FROM aliases WHERE namespace='source_use.copy'"
                )} if inspect_pending_repair else {})
                evaluations = heads("evaluation_observation")
                evaluation_history = [json.loads(row[0]) for row in db.execute(
                    "SELECT body FROM records WHERE kind='evaluation_observation' AND id=?",
                    (f"evaluation-observation:{run_id}",),
                )] if inspect_dispatch and phase > 2 else []
                controls = heads("mission_task_control")
                control_history = [(identifier, json.loads(body), created_at)
                    for identifier, body, created_at in db.execute(
                        "SELECT id,body,created_at FROM records WHERE kind='mission_task_control'"
                    )] if inspect_dispatch and phase > 1 else []
                snapshots = ([(record_id, json.loads(body)) for record_id, body in db.execute(
                    "SELECT id,body FROM records WHERE kind='research_snapshot' AND version=1"
                )] if followup else [])
            finally:
                db.close()
    except (OSError, sqlite3.Error, ValueError, TypeError) as exc:
        raise ValueError("Stopped case has unreadable saved state") from exc
    if _state_digest(state) != source_state_before:
        raise ValueError("Stopped state changed during read-only validation")
    by_run = dict(runs)
    child = by_run.get(run_id, {})
    saved_profile = child.get("profile", {})
    expected_profile = {
        "requestId": request_id,
        "question": request_data.get("question"),
        "scope": request_data.get("scope", ""),
        "agentIds": request_data.get("agent_ids"),
        "credentialProfile": request_data.get("credential_profile"),
        "executionMode": execution_mode,
    }
    if followup:
        expected_profile.update(parentRunId=request_data.get("parent_run_id"),
                                parentSnapshotId=request_data.get("parent_snapshot_id"),
                                targetId=request_data.get("target_id"),
                                targetVersion=request_data.get("target_version"))
    else:
        expected_profile["evidenceText"] = request_data.get("evidence_text", "")
    selected_profile = profile["profiles"][case["credential_profile"]]
    expected_settings = {
        "max_completion_tokens": 8192,
        "stream": False,
        "unknown_source_policy": "reject",
        "credential_profile": case["credential_profile"],
        "quota_domain": selected_profile.get("quota_domain", "groq-selected"),
    }
    # This runner's clean observation boundary is the pinned GPT-OSS medium
    # profile. Reject a copied run whose saved model settings drift from it.
    observation_profile_ok = (
        saved_profile.get("selectedProvider") == "groq"
        and saved_profile.get("selectedEndpoint") == GROQ_CHAT_URL
        and saved_profile.get("selectedModel") == case["model"]
        and saved_profile.get("reasoningSettings") == case.get("reasoning")
        == {"reasoning_effort": "medium", "include_reasoning": True}
        and saved_profile.get("modelSettings") == expected_settings
        and ("agent_model_bindings" not in case or
             saved_profile.get("agentModelBindings") == case["agent_model_bindings"])
        and ("agent_models" not in case or
             saved_profile.get("agentModels") == case.get("saved_agent_models"))
    )
    ancestors = set()
    parent_id = child.get("parent_run_id") if followup else None
    while parent_id and parent_id not in ancestors and parent_id in by_run:
        ancestors.add(parent_id)
        parent_id = by_run[parent_id].get("parent_run_id")
    parent_snapshot = dict(snapshots).get(request_data.get("parent_snapshot_id")) if followup else None
    exact_target = {"kind": "report", "id": request_data.get("target_id"),
                    "version": request_data.get("target_version")}
    matching = [(identifier, body) for identifier, body in responses
                if not clean_stop
                and body.get("mission_id") == child.get("mission_id")
                and body.get("http_status") == status
                and body.get("status") == "failed"
                and body.get("request_sha256") == last.get("request_sha256")
                and body.get("response_sha256") == last.get("response_sha256")
                and body.get("credential_profile") == case["credential_profile"]]
    rejection_id, rejection = matching[0] if len(matching) == 1 else (None, {})
    rejection_ref = {"kind": "model_response_receipt", "id": rejection_id,
                     "version": 1}
    pending_type_repair = None
    recovered_type_repair = None
    recovered_uncertain_stop = False
    if clean_stop or uncertain_stop:
        prior_attempts = aggregate["model_attempts"]
        selected_responses = [(identifier, body) for identifier, body in responses
                              if body.get("mission_id") == child.get("mission_id")]
        selected_dispatches = [(identifier, body) for identifier, body in dispatches
                               if body.get("mission_id") == child.get("mission_id")]
        response_by_sha = {body.get("request_sha256"): (identifier, body)
                           for identifier, body in selected_responses}
        dispatch_by_sha = {body.get("request_sha256"): body
                           for _, body in selected_dispatches}
        expected_sha = {item["request_sha256"] for item in prior_attempts}
        captured_calls = {body["call_ref"]["id"]
                          for _, body in selected_dispatches
                          if body.get("status") == "captured"
                          if isinstance(body.get("call_ref"), dict)
                          and isinstance(body["call_ref"].get("id"), str)}
        blocked_dispatches = [body for _, body in selected_dispatches
                              if body.get("status") == "blocked_before_external_effect"]
        captured_dispatches = [body for _, body in selected_dispatches
                               if body.get("status") == "captured"]
        intent_by_id = dict(intents)
        uncertain_identity_ok = False
        uncertain_control_id = None
        work_id_for_uncertain = None
        if uncertain_stop:
            pending = [body for _, body in selected_dispatches
                       if body.get("status") == "attempted_external_effect"
                       and body.get("request_sha256") == last.get("request_sha256")]
            marker = pending[0] if len(pending) == 1 else None
            intent_ref = marker.get("intent_ref") if marker else None
            call_ref = marker.get("call_ref") if marker else None
            intent = (intent_by_id.get(intent_ref.get("id"))
                      if isinstance(intent_ref, dict) else None)
            call = (dict(calls).get(call_ref.get("id"))
                    if isinstance(call_ref, dict) else None)
            config = (dict(configurations).get(call.get("configuration_id"))
                      if isinstance(call, dict) else None)
            matching_work = [(work_id, work) for work_id, work in works
                             if isinstance(work.get("spec"), dict)
                             and work["spec"].get("call_id") == (call_ref or {}).get("id")]
            work_id, work = matching_work[0] if len(matching_work) == 1 else (None, None)
            work_id_for_uncertain = work_id
            cap = (dict(capacities).get(work_id.replace("executor-work:", "executor-cap:", 1))
                   if isinstance(work_id, str) and work_id.startswith("executor-work:")
                   else None)
            matching_control = [(control_id, control) for control_id, control in controls
                                if control.get("work_id") == (work or {}).get("spec", {}).get("work_id")]
            uncertain_control_id, control = (matching_control[0]
                                             if len(matching_control) == 1 else (None, None))
            pointer = intent.get("blob") if isinstance(intent, dict) else None
            packet = None
            if (isinstance(pointer, dict) and set(pointer) == {"sha256", "size"}
                    and type(pointer.get("size")) is int and pointer["size"] > 0
                    and isinstance(pointer.get("sha256"), str)
                    and re.fullmatch(r"[a-f0-9]{64}", pointer["sha256"])):
                blob_path = state / "objects" / pointer["sha256"]
                if blob_path.is_file() and not blob_path.is_symlink():
                    raw = blob_path.read_bytes()
                    if (len(raw) == pointer["size"]
                            and hashlib.sha256(raw).hexdigest() == pointer["sha256"]):
                        try:
                            packet = json.loads(raw)
                        except (TypeError, ValueError):
                            pass
            cap_attempts = cap.get("attempts") if isinstance(cap, dict) else None
            uncertain_identity_ok = bool(
                marker is not None and isinstance(intent, dict)
                and isinstance(call, dict) and isinstance(config, dict)
                and isinstance(work, dict) and isinstance(cap, dict)
                and isinstance(control, dict) and isinstance(packet, dict)
                and len(selected_dispatches)
                == len(prior_attempts) + len(blocked_dispatches)
                and len(captured_dispatches)
                == len(prior_attempts) - 1 - len(historical_unknown)
                and marker.get("request_sha256") == last.get("request_sha256")
                and marker.get("credential_profile") == case["credential_profile"]
                and marker.get("receipt_ref") is None
                and intent_ref == {"kind": "model_request_intent",
                                   "id": f"model-intent:{call_ref['id']}:1", "version": 1}
                and call_ref == {"kind": "agent_call", "id": call_ref["id"], "version": 1}
                and intent.get("mission_id") == child.get("mission_id")
                and intent.get("call_ref") == call_ref
                and intent.get("request_sha256") == last.get("request_sha256")
                and intent.get("credential_profile") == case["credential_profile"]
                and intent.get("request_token_estimate", {}).get("tokens")
                == last.get("estimated_input_tokens")
                and pointer["sha256"] == last.get("request_sha256")
                and intent.get("configuration_ref") == {
                    "kind": "agent_configuration", "id": call.get("configuration_id"),
                    "version": 1,
                }
                and intent.get("configuration_sha256")
                == call.get("configuration_sha256") == config.get("configuration_sha256")
                and intent.get("source_refs") == work["spec"].get("input_refs")
                and packet.get("model") == call.get("model_id") == config.get("model_id")
                == last.get("model")
                and packet.get("reasoning_effort")
                == call.get("reasoning_settings", {}).get("reasoning_effort")
                and ("reasoning_format" not in packet or
                     packet.get("model") == "qwen/qwen3.8-27b"
                     and packet["reasoning_format"] == "parsed")
                and packet.get("max_completion_tokens") == last.get("output_reservation")
                == work["spec"].get("output_reservation_tokens")
                and packet.get("stream") is False
                and packet.get("tool_choice") == "auto"
                and packet.get("parallel_tool_calls") is False
                and isinstance(packet.get("tools"), list) and bool(packet["tools"])
                and all(isinstance(tool, dict) and tool.get("type") == "function"
                        and isinstance(tool.get("function"), dict)
                        for tool in packet["tools"])
                and work.get("phase") == "pending"
                and work.get("receipt_ref") is None and work.get("result_ref") is None
                and work.get("recovery_ref") is None
                and work["spec"].get("kind") == "model"
                and work["spec"].get("handler_id") == "mission-model"
                and work["spec"].get("handler_version") == "1"
                and work["spec"].get("run_id") == run_id
                and work["spec"].get("mission_id") == child.get("mission_id")
                and work["spec"].get("call_version") == 1
                and work["spec"].get("configuration_ref")
                == intent.get("configuration_ref")
                and control.get("run_id") == run_id
                and control.get("mission_id") == child.get("mission_id")
                and control.get("phase") == "waiting"
                and control.get("waiting_reason") == "pending_model has uncertain external effect"
                and control.get("call_ref") == call_ref
                and control.get("feedback") is None
                and cap.get("phase") == "uncertain"
                and cap.get("kind") == "model"
                and cap.get("reservation") is True
                and cap.get("request_token_estimate", {}).get("intent_ref") == intent_ref
                and cap.get("request_token_estimate", {}).get("request_sha256")
                == last.get("request_sha256")
                and cap.get("request_token_estimate", {}).get("tokens")
                == last.get("estimated_input_tokens")
                and isinstance(cap_attempts, list) and len(cap_attempts) == 1
                and cap_attempts[0].get("status") == "dispatched"
                and cap_attempts[0].get("usage_known") is False
                and cap_attempts[0].get("output_usage_known") is False
                and cap_attempts[0].get("reserved_output_tokens")
                == last.get("output_reservation")
                and not any(body.get("request_sha256") == last.get("request_sha256")
                            for _, body in selected_responses)
            )
        cap_block = blocked_dispatches[0] if len(blocked_dispatches) == 1 else None
        cap_intent = (intent_by_id.get(cap_block["intent_ref"].get("id"))
                      if cap_block is not None and isinstance(cap_block.get("intent_ref"), dict)
                      else None)
        cap_work = [work for _, work in works
                    if cap_block is not None
                    and work.get("superseded_intent_ref") == cap_block.get("intent_ref")]
        cap_call = (dict(calls).get(cap_block["call_ref"].get("id"))
                    if cap_block is not None and isinstance(cap_block.get("call_ref"), dict)
                    else None)
        cap_config = (dict(configurations).get(cap_call.get("configuration_id"))
                      if isinstance(cap_call, dict) else None)

        def checked_token_cap_request():
            if not token_cap_stop or not isinstance(cap_intent, dict):
                return False
            pointer = cap_intent.get("blob")
            if (type(pointer) is not dict or set(pointer) != {"sha256", "size"}
                    or type(pointer["size"]) is not int or pointer["size"] < 1
                    or not isinstance(pointer["sha256"], str)
                    or re.fullmatch(r"[a-f0-9]{64}", pointer["sha256"]) is None):
                return False
            path = state / "objects" / pointer["sha256"]
            if not path.is_file() or path.is_symlink():
                return False
            raw = path.read_bytes()
            if (len(raw) != pointer["size"]
                    or hashlib.sha256(raw).hexdigest() != pointer["sha256"]
                    or pointer["sha256"] != cap_intent.get("request_sha256")):
                return False
            try:
                packet = json.loads(raw)
            except (TypeError, ValueError):
                return False
            return bool(
                isinstance(packet, dict) and isinstance(cap_call, dict)
                and isinstance(cap_config, dict)
                and cap_intent.get("configuration_ref") == {
                    "kind": "agent_configuration",
                    "id": cap_call.get("configuration_id"), "version": 1,
                }
                and cap_intent.get("configuration_sha256")
                == cap_call.get("configuration_sha256")
                == cap_config.get("configuration_sha256")
                and packet.get("model") == cap_call.get("model_id")
                == cap_config.get("model_id")
                and packet.get("reasoning_effort")
                == cap_call.get("reasoning_settings", {}).get("reasoning_effort")
                and packet.get("max_completion_tokens")
                == cap_call.get("settings", {}).get("max_completion_tokens")
                == cap_work[0]["spec"].get("output_reservation_tokens")
                and packet.get("stream") is False
            )

        token_cap_identity_ok = bool(
            token_cap_stop and cap_block is not None and len(cap_work) == 1
            and isinstance(cap_intent, dict)
            and cap_intent.get("mission_id") == child.get("mission_id")
            and cap_intent.get("call_ref") == cap_block.get("call_ref")
            and cap_intent.get("request_sha256") == cap_block.get("request_sha256")
            and cap_intent.get("credential_profile") == case["credential_profile"]
            and isinstance(cap_work[0].get("spec"), dict)
            and isinstance(cap_work[0]["spec"].get("proposal_ref"), dict)
            and isinstance(cap_call, dict)
            and cap_call.get("mission_id") == child.get("mission_id")
            and cap_call.get("task_id") == cap_work[0]["spec"]["proposal_ref"].get("id")
            and cap_call.get("instance_id") == cap_work[0]["spec"].get("actor_id")
            and isinstance(cap_intent.get("request_token_estimate"), dict)
            and type(cap_intent["request_token_estimate"].get("tokens")) is int
            and cap_intent["request_token_estimate"]["tokens"] >= 0
            and type(cap_work[0]["spec"].get("output_reservation_tokens")) is int
            and cap_work[0]["spec"]["output_reservation_tokens"] > 0
            and isinstance(cap_block.get("call_ref"), dict)
            and cap_work[0]["spec"].get("call_id")
            == cap_block["call_ref"].get("id")
            and checked_token_cap_request()
            and cap_work[0].get("phase") == "superseded"
            and cap_work[0].get("status_basis") == "local_model_admission_blocked"
            and cap_work[0].get("receipt_ref") is None
            and cap_work[0].get("result_ref") is None
            and type(budget.get("planned_tokens")) is int
            and budget["planned_tokens"] + cap_intent["request_token_estimate"]["tokens"]
            + cap_work[0]["spec"]["output_reservation_tokens"]
            > budget["limits"]["tokens"]
        )
        blocked_identity_ok = (
            (not blocked_dispatches or uncertain_stop and phase > 1)
            if not (cap_stop or token_cap_stop) else
            cap_block is not None
            and (token_cap_identity_ok if token_cap_stop else
                 cap_block.get("request_sha256")
                 == guard["blocked_candidate"]["request_sha256"])
            and cap_block.get("credential_profile") == case["credential_profile"]
            and cap_block.get("receipt_ref") is None
            and isinstance(cap_block.get("intent_ref"), dict)
            and isinstance(cap_block.get("call_ref"), dict)
            and cap_block["intent_ref"].get("kind") == "model_request_intent"
            and cap_block["call_ref"].get("kind") == "agent_call"
            and cap_block["call_ref"].get("id") not in captured_calls
            and cap_block["intent_ref"].get("version") == 1
            and cap_block["call_ref"].get("version") == 1
            and isinstance(cap_intent, dict)
            and cap_intent.get("mission_id") == child.get("mission_id")
            and cap_intent.get("request_sha256") == cap_block["request_sha256"]
            and cap_intent.get("call_ref") == cap_block["call_ref"]
            and cap_intent.get("credential_profile") == case["credential_profile"]
            and isinstance(cap_intent.get("request_token_estimate"), dict)
            and (token_cap_stop or cap_intent.get("request_token_estimate", {}).get("tokens")
                 == guard["blocked_candidate"]["estimated_input_tokens"])
            and cap_block["request_sha256"] not in expected_sha
            and not any(body.get("request_sha256") == cap_block["request_sha256"]
                        for _, body in selected_responses)
                and len(selected_dispatches) == len(captured_dispatches) + 1
        )
        prior_failed_shas = (
            {item["request_sha256"] for item in prior_copy["model_attempts"]
             if item.get("status") != 200}
            if (token_cap_stop or uncertain_stop) and phase > 1 else set()
        )
        prior_failed_call_ids = {
            response_by_sha[sha][1]["call_ref"]["id"]
            for sha in prior_failed_shas if sha in response_by_sha
            and isinstance(response_by_sha[sha][1].get("call_ref"), dict)
        }
        daily_429_call_ids = {
            response_by_sha[item["request_sha256"]][1]["call_ref"]["id"]
            for item in prior_attempts if finite_observation_429
            and item.get("status") == 429
            and item.get("request_sha256") in response_by_sha
            and isinstance(response_by_sha[item["request_sha256"]][1].get("call_ref"), dict)
        }
        capacity_by_id = dict(capacities)

        def captured_rate_wait(work_id, work):
            spec = work.get("spec") or {}
            cap_id = work_id.replace("executor-work:", "executor-cap:", 1)
            cap = capacity_by_id.get(cap_id)
            attempts_saved = cap.get("attempts") if isinstance(cap, dict) else None
            return bool(
                finite_observation_429
                and work_id.startswith("executor-work:")
                and work.get("phase") == "pending"
                and work.get("receipt_ref") is None
                and work.get("result_ref") is None
                and spec.get("kind") == "model"
                and spec.get("call_id") in daily_429_call_ids
                and isinstance(cap, dict) and cap.get("phase") == "canceled"
                and type(cap.get("wake_at")) in {int, float}
                and math.isfinite(cap["wake_at"]) and cap["wake_at"] >= 0
                and isinstance(attempts_saved, list) and attempts_saved
                and attempts_saved[-1].get("status") == "rate_limited"
            )

        pending_work_ok = all(
            work.get("phase") == "completed" or (
                work.get("phase") == "pending"
                and work.get("receipt_ref") is None
                and isinstance(work.get("spec"), dict)
                and work["spec"].get("kind") == "model"
                and isinstance(work["spec"].get("call_id"), str)
                and bool(work["spec"]["call_id"])
                and (work["spec"].get("call_id") not in captured_calls
                     or token_cap_stop
                     and work["spec"].get("call_id") in prior_failed_call_ids))
            or captured_rate_wait(work_id, work)
            or ((cap_stop or token_cap_stop) and cap_block is not None
                and work.get("phase") == "superseded"
                and work.get("status_basis") == "local_model_admission_blocked"
                and work.get("receipt_ref") is None
                and work.get("result_ref") is None
                and work.get("superseded_intent_ref") == cap_block["intent_ref"]
                and isinstance(work.get("spec"), dict)
                and work["spec"].get("kind") == "model"
                and work["spec"].get("call_id") == cap_block["call_ref"].get("id"))
            or (token_cap_stop and work.get("phase") == "superseded"
                and work.get("status_basis") == "new_peer_input_before_dispatch"
                and work.get("receipt_ref") is None
                and work.get("result_ref") is None
                and isinstance(work.get("superseded_intent_ref"), dict)
                and work["superseded_intent_ref"].get("kind") == "model_request_intent"
                and work["superseded_intent_ref"].get("version") == 1
                and isinstance(work.get("spec"), dict)
                and work["spec"].get("kind") == "model"
                and work["spec"].get("call_id") not in captured_calls
                and intent_by_id.get(work["superseded_intent_ref"].get("id"), {}).get(
                    "call_ref", {}).get("id") == work["spec"].get("call_id"))
            or (uncertain_stop and phase > 1
                and work.get("phase") in {"model_response_lost", "superseded"}
                and work.get("receipt_ref") is None
                and work.get("result_ref") is None)
            for work_id, work in works if work.get("mission_id") == child.get("mission_id")
        )
        blocked_work_count = sum(
            work.get("phase") == "superseded"
            and work.get("superseded_intent_ref") == (cap_block or {}).get("intent_ref")
            for _, work in works if work.get("mission_id") == child.get("mission_id")
        )
        def saved_attempt_ok(item):
            matched = response_by_sha.get(item.get("request_sha256"))
            dispatch = dispatch_by_sha.get(item.get("request_sha256"))
            if matched is None or dispatch is None:
                return False
            response_id, receipt = matched
            common = (
                receipt.get("response_sha256") == item.get("response_sha256")
                and receipt.get("credential_profile") == case["credential_profile"]
                and dispatch.get("status") == "captured"
                and dispatch.get("receipt_ref") == {
                    "kind": "model_response_receipt", "id": response_id, "version": 1}
                and dispatch.get("intent_ref") == receipt.get("intent_ref")
                and dispatch.get("call_ref") == receipt.get("call_ref")
            )
            if not common:
                return False
            if item.get("status") == 200:
                return (receipt.get("status") in {"complete", "tool_requested"}
                        and receipt.get("http_status") in {None, 200}
                        and isinstance(receipt.get("usage"), dict)
                        and receipt["usage"].get("total_tokens") == item.get("actual_tokens"))
            if finite_observation_429:
                return _captured_daily_429(state, item, receipt)
            return (token_cap_stop and item.get("request_sha256") in prior_failed_shas
                    and item.get("status") in {400, 413, 429}
                    and item.get("actual_tokens") is None
                    and item.get("state") == (
                        "rate_limited" if item["status"] == 429 else "observed")
                    and receipt.get("status") == "failed"
                    and receipt.get("http_status") == item["status"]
                    and receipt.get("usage") in ({}, None))

        last_response_id = response_by_sha.get(last["request_sha256"], (None,))[0]
        last_response_ref = ({"kind": "model_response_receipt",
                              "id": last_response_id, "version": 1}
                             if last_response_id is not None else None)
        last_rate_wait_ok = True
        if finite_observation_429 and last.get("status") == 429:
            last_receipt = response_by_sha.get(last["request_sha256"], (None, {}))[1]
            last_call = (last_receipt.get("call_ref") or {}).get("id")
            matching_controls = [body for _, body in controls
                                 if (body.get("feedback") or {}).get(
                                     "previous_response_ref") == last_response_ref]
            matching_works = [(work_id, body) for work_id, body in works
                              if (body.get("spec") or {}).get("call_id") == last_call]
            cap = (capacity_by_id.get(matching_works[0][0].replace(
                "executor-work:", "executor-cap:", 1))
                   if len(matching_works) == 1 else None)
            try:
                receipt_time = datetime.fromisoformat(
                    response_created_at[last_response_id].replace("Z", "+00:00")
                ).timestamp()
            except (KeyError, TypeError, ValueError, OverflowError):
                receipt_time = None
            expected_retry = (max(
                cap["wake_at"],
                receipt_time + last_receipt["retry_after_seconds"],
            ) if isinstance(cap, dict)
                 and type(cap.get("wake_at")) in {int, float}
                 and type(receipt_time) in {int, float}
                 and type(last_receipt.get("retry_after_seconds")) in {int, float}
                 else None)
            last_rate_wait_ok = bool(
                len(matching_controls) == 1 and len(matching_works) == 1
                and captured_rate_wait(*matching_works[0])
                and matching_controls[0].get("phase") in {"retry_at", "ready_model"}
                and matching_controls[0].get("work_id") is None
                and matching_controls[0]["feedback"].get("status")
                == "provider_rate_limit_wait_completed"
                and type(matching_controls[0].get("retry_at")) in {int, float}
                and math.isfinite(matching_controls[0]["retry_at"])
                and type(expected_retry) in {int, float}
                and math.isfinite(expected_retry)
                and abs(matching_controls[0]["retry_at"] - expected_retry) < 1
            )
        captured_attempts = ([item for item in prior_attempts[:-1]
                              if item.get("state") != "uncertain"]
                             if uncertain_stop else prior_attempts)
        expected_captured_sha = {item["request_sha256"] for item in captured_attempts}
        observation_receipts_ok = (
            len(expected_sha) == len(prior_attempts)
            and len(selected_responses) == len(captured_attempts)
            and len(captured_dispatches) == len(captured_attempts)
            and set(response_by_sha) == expected_captured_sha
            and {body.get("request_sha256") for body in captured_dispatches}
            == expected_captured_sha
            and (not uncertain_stop or uncertain_identity_ok)
            and blocked_identity_ok
            and blocked_work_count == (1 if cap_stop or token_cap_stop else 0)
            and pending_work_ok
            and last_rate_wait_ok
            and all(saved_attempt_ok(item) for item in captured_attempts)
        )
        # A concurrent cap can block more than one peer locally, even in a
        # first phase with no uncertain response. Validate each candidate
        # separately; a local denial has no response receipt or HTTP charge.
        # Later phases may also inherit an uncertain response reservation or a
        # local denial before a different, captured observation stop.
        historical_unknown = ([item for item in prior_copy["model_attempts"]
                               if item.get("state") == "uncertain"]
                              if (token_cap_stop or uncertain_stop) and phase > 1 else [])
        if (historical_unknown or uncertain_stop
                or token_cap_stop and len(blocked_dispatches) > 1
                or budget.get("stop_reason") == "observation_window_ended"
                and phase > 1 and blocked_dispatches):
            recovery_by_sha = {body.get("request_sha256"): (ident, body)
                               for ident, body in recoveries
                               if body.get("mission_id") == child.get("mission_id")}
            work_by_id = dict(works)
            call_by_id = dict(calls)
            config_by_id = dict(configurations)
            cap_by_id = dict(capacities)
            instance_by_id = dict(instances)
            proposal_by_id = dict(proposals)

            def candidate_actor_ok(intent, call, config, work):
                if not all(isinstance(item, dict) for item in (
                    intent, call, config, work,
                )):
                    return False
                spec = work.get("spec")
                actor = intent.get("instance_id")
                task = intent.get("task_id")
                instance = instance_by_id.get(actor)
                proposal = proposal_by_id.get(task)
                return bool(
                    isinstance(actor, str) and actor
                    and isinstance(task, str) and task
                    and isinstance(spec, dict)
                    and isinstance(instance, dict)
                    and instance.get("run_id") == run_id
                    and instance.get("mission_id") == child.get("mission_id")
                    and isinstance(proposal, dict)
                    and proposal.get("mission_id") == child.get("mission_id")
                    and proposal.get("actor_ref") == {
                        "kind": "agent_instance", "id": actor, "version": 1,
                    }
                    and call.get("instance_id") == actor
                    and call.get("task_id") == task
                    and config.get("instance_id") == actor
                    and config.get("mission_id") == child.get("mission_id")
                    and spec.get("actor_id") == actor
                    and spec.get("proposal_ref") == {
                        "kind": "board_proposal", "id": task, "version": 1,
                    }
                )

            def checked_block_packet(intent, call, config, work):
                pointer = intent.get("blob") if isinstance(intent, dict) else None
                if (not isinstance(pointer, dict)
                        or set(pointer) != {"sha256", "size"}
                        or type(pointer.get("size")) is not int or pointer["size"] < 1
                        or not isinstance(pointer.get("sha256"), str)
                        or re.fullmatch(r"[a-f0-9]{64}", pointer["sha256"]) is None):
                    return False
                path = state / "objects" / pointer["sha256"]
                if not path.is_file() or path.is_symlink():
                    return False
                raw = path.read_bytes()
                if (len(raw) != pointer["size"]
                        or hashlib.sha256(raw).hexdigest() != pointer["sha256"]
                        or pointer["sha256"] != intent.get("request_sha256")):
                    return False
                try:
                    packet = json.loads(raw)
                except (TypeError, ValueError):
                    return False
                reasoning = call.get("reasoning_settings", {})
                return bool(
                    isinstance(packet, dict)
                    and intent.get("configuration_ref") == {
                        "kind": "agent_configuration",
                        "id": call.get("configuration_id"), "version": 1,
                    }
                    and intent.get("configuration_sha256")
                    == call.get("configuration_sha256")
                    == config.get("configuration_sha256")
                    and hashlib.sha256(json.dumps(
                        {key: value for key, value in config.items()
                         if key != "configuration_sha256"},
                        sort_keys=True, separators=(",", ":"), ensure_ascii=False,
                    ).encode()).hexdigest() == config.get("configuration_sha256")
                    and intent.get("source_refs") == work.get("spec", {}).get("input_refs")
                    and packet.get("model") == call.get("model_id") == config.get("model_id")
                    and packet.get("reasoning_effort") == reasoning.get("reasoning_effort")
                    and ("reasoning_format" not in packet or
                         packet["reasoning_format"] == reasoning.get("reasoning_format"))
                    and ("include_reasoning" not in packet or
                         packet["include_reasoning"] == reasoning.get("include_reasoning"))
                    and packet.get("max_completion_tokens")
                    == call.get("settings", {}).get("max_completion_tokens")
                    == work.get("spec", {}).get("output_reservation_tokens")
                    and packet.get("stream") is False
                )

            def historical_unknown_ok(item):
                sha = item.get("request_sha256")
                marker = dispatch_by_sha.get(sha)
                recovery_pair = recovery_by_sha.get(sha)
                if (not isinstance(marker, dict) or recovery_pair is None
                        or sha in response_by_sha):
                    return False
                recovery_id, recovery = recovery_pair
                intent_ref = marker.get("intent_ref")
                intent = (intent_by_id.get(intent_ref.get("id"))
                          if isinstance(intent_ref, dict) else None)
                call_ref = marker.get("call_ref")
                call = (call_by_id.get(call_ref.get("id"))
                        if isinstance(call_ref, dict) else None)
                config = (config_by_id.get(call.get("configuration_id"))
                          if isinstance(call, dict) else None)
                work_ref = recovery.get("work_ref")
                work_id = work_ref.get("id") if isinstance(work_ref, dict) else None
                work = work_by_id.get(work_id)
                cap = (cap_by_id.get(work_id.replace("executor-work:", "executor-cap:", 1))
                       if isinstance(work_id, str) and work_id.startswith("executor-work:")
                       else None)
                cap_attempts = cap.get("attempts") if isinstance(cap, dict) else None
                return bool(
                    item.get("status") is None and item.get("state") == "uncertain"
                    and item.get("actual_tokens") is None
                    and type(item.get("estimated_input_tokens")) is int
                    and type(item.get("output_reservation")) is int
                    and item.get("planned_tokens") == (
                        item["estimated_input_tokens"] + item["output_reservation"])
                    and marker.get("status") == "attempted_external_effect"
                    and marker.get("mission_id") == child.get("mission_id")
                    and marker.get("credential_profile") == case["credential_profile"]
                    and marker.get("receipt_ref") is None
                    and isinstance(intent, dict) and intent.get("request_sha256") == sha
                    and isinstance(call, dict) and isinstance(config, dict)
                    and intent.get("call_ref") == marker.get("call_ref")
                    and intent.get("credential_profile") == case["credential_profile"]
                    and intent.get("request_token_estimate", {}).get("tokens")
                    == item["estimated_input_tokens"]
                    and recovery.get("intent_ref") == intent_ref
                    and recovery.get("call_ref") == marker.get("call_ref")
                    and recovery.get("dispatch_marker_ref") == {
                        "kind": "model_dispatch_attempt",
                        "id": f"model-dispatch:{intent_ref['id']}", "version": 1,
                    }
                    and recovery.get("usage") == "unknown_conservative_quota_charge_retained"
                    and recovery.get("remote_response") == "unknown"
                    and recovery.get("attempt_status") == "uncertain"
                    and isinstance(work, dict) and work.get("phase") == "model_response_lost"
                    and work.get("recovery_ref") == {
                        "kind": "model_response_lost", "id": recovery_id, "version": 1,
                    }
                    and work.get("receipt_ref") is None and work.get("result_ref") is None
                    and checked_block_packet(intent, call, config, work)
                    and isinstance(cap, dict) and cap.get("phase") == "model_response_lost"
                    and cap.get("reservation") is False
                    and isinstance(cap_attempts, list) and len(cap_attempts) == 1
                    and cap_attempts[0].get("status") == "superseded_unknown"
                    and cap_attempts[0].get("usage_known") is False
                )

            # A copied continuation retains each earlier no-effect denial.
            # Its ceiling belongs to the phase that recorded it, not the
            # newest phase. The two most recent ledgers are already pinned
            # above; older ceilings can be joined through saved evaluation
            # hashes and the validated prospective grant history.
            phase_ledgers = [None] * phase
            phase_ledgers[-1] = aggregate
            if phase > 1:
                phase_ledgers[-2] = prior_copy
            ceiling = (revisions[0]["prior_limit_tokens"] if revisions else
                       aggregate["limits"]["tokens"])
            grants_by_hash = {item["source_ledger_sha256"]: item for item in revisions}
            model_count = retrieval_count = 0
            elapsed = 0.0
            for index, segment in enumerate(segments[:-2]):
                model_count += segment["model_attempts"]
                retrieval_count += segment["retrieval_attempts"]
                elapsed += segment["controller_elapsed_seconds"]
                matching_evaluations = [item for item in evaluation_history
                    if item.get("run_id") == run_id
                    and item.get("mission_id") == child.get("mission_id")
                    and item.get("model_http_attempts") == model_count
                    and item.get("retrieval_http_attempts") == retrieval_count
                    and type(item.get("elapsed_seconds")) in {int, float}
                    and abs(item["elapsed_seconds"] - elapsed) <= 1e-6]
                if len(matching_evaluations) != 1:
                    break
                historical = matching_evaluations[0]
                phase_ledgers[index] = {
                    "limits": {"tokens": ceiling},
                    "planned_tokens": sum(
                        item["actual_tokens"] if type(item.get("actual_tokens")) is int
                        else item["planned_tokens"]
                        for item in aggregate["model_attempts"][:model_count]
                    ),
                }
                revision = grants_by_hash.get(historical.get("ledger_sha256"))
                if revision is not None:
                    if revision["prior_limit_tokens"] != ceiling:
                        phase_ledgers[index] = None
                        break
                    ceiling = revision["new_limit_tokens"]
            if (phase > 2 and (any(item is None for item in phase_ledgers[:-2])
                               or ceiling != prior_copy["limits"]["tokens"])):
                # Missing historical evaluations or an unjoined grant must
                # not make an older, smaller ceiling authorize a denial.
                phase_ledgers[:-2] = [None] * (phase - 2)

            def blocked_phase(marker):
                try:
                    at = datetime.fromisoformat(dispatch_created_at[
                        f"model-dispatch:{marker['intent_ref']['id']}"
                    ]).timestamp()
                except (KeyError, TypeError, ValueError):
                    return None
                matches = [index for index, segment in enumerate(segments)
                           if segment["started_at"] <= at
                           <= segment["started_at"] + segment["controller_elapsed_seconds"]]
                return matches[0] if len(matches) == 1 else None

            def blocked_candidate_ok(marker):
                sha = marker.get("request_sha256")
                intent_ref = marker.get("intent_ref")
                call_ref = marker.get("call_ref")
                intent = (intent_by_id.get(intent_ref.get("id"))
                          if isinstance(intent_ref, dict) else None)
                call = (call_by_id.get(call_ref.get("id"))
                        if isinstance(call_ref, dict) else None)
                config = (config_by_id.get(call.get("configuration_id"))
                          if isinstance(call, dict) else None)
                matching = [(work_id, work) for work_id, work in works
                            if work.get("superseded_intent_ref") == intent_ref]
                work_id, work = matching[0] if len(matching) == 1 else (None, None)
                cap_id = (work_id.replace("executor-work:", "executor-cap:", 1)
                          if isinstance(work_id, str)
                          and work_id.startswith("executor-work:") else None)
                phase_index = blocked_phase(marker)
                blocked_capacity_ok = (
                    phase_index is not None
                    and (token_cap_stop or uncertain_stop and phase > 1
                         or clean_stop and phase_index < phase - 1)
                    and phase_ledgers[phase_index] is not None
                    and _historical_blocked_capacity_ok(
                        marker=marker, intent=intent, work=work,
                        capacity=cap_by_id.get(cap_id),
                        prior_segment=segments[phase_index],
                        prior_ledger=phase_ledgers[phase_index],
                        run_id=run_id, mission_id=child.get("mission_id"),
                    )
                )
                control = [body for _, body in controls
                           if body.get("call_ref") == call_ref
                           and body.get("run_id") == run_id]
                if phase_index is not None and phase_index < phase - 1:
                    # The same control legitimately advances after a restart.
                    # Verify its ready state at the old denial and its durable
                    # actor/run identity, rather than requiring the head to
                    # still point to an obsolete call.
                    control = []
                    current_controls = dict(controls)
                    segment = segments[phase_index]
                    for identifier, old_control, created_at in control_history:
                        successor = current_controls.get(identifier, {})
                        try:
                            at = datetime.fromisoformat(created_at).timestamp()
                        except (TypeError, ValueError):
                            continue
                        if (old_control.get("call_ref") == call_ref
                                and old_control.get("run_id") == run_id
                                and old_control.get("phase") == "ready_model"
                                and old_control.get("work_id") is None
                                and segment["started_at"] <= at
                                <= segment["started_at"] + segment["controller_elapsed_seconds"]
                                and all(successor.get(key) == old_control.get(key)
                                        for key in ("actor_id", "run_id", "mission_id", "alias"))):
                            control = [old_control]
                            break
                return bool(
                    isinstance(sha, str) and re.fullmatch(r"[a-f0-9]{64}", sha)
                    and isinstance(call_ref, dict)
                    and sha not in expected_sha and sha not in response_by_sha
                    and marker.get("status") == "blocked_before_external_effect"
                    and marker.get("mission_id") == child.get("mission_id")
                    and marker.get("credential_profile") == case["credential_profile"]
                    and marker.get("receipt_ref") is None
                    and isinstance(intent_ref, dict) and intent_ref == {
                        "kind": "model_request_intent",
                        "id": f"model-intent:{call_ref['id']}:1", "version": 1,
                    }
                    and call_ref == {"kind": "agent_call", "id": call_ref["id"],
                                     "version": 1}
                    and isinstance(intent, dict) and isinstance(call, dict)
                    and isinstance(config, dict) and isinstance(work, dict)
                    and candidate_actor_ok(intent, call, config, work)
                    and intent.get("mission_id") == child.get("mission_id")
                    and intent.get("call_ref") == call_ref
                    and intent.get("request_sha256") == sha
                    and intent.get("credential_profile") == case["credential_profile"]
                    and call.get("mission_id") == child.get("mission_id")
                    and call.get("task_id") == work["spec"].get("proposal_ref", {}).get("id")
                    and call.get("instance_id") == work["spec"].get("actor_id")
                    and work.get("phase") == "superseded"
                    and work.get("status_basis") == "local_model_admission_blocked"
                    and work.get("receipt_ref") is None and work.get("result_ref") is None
                    and work["spec"].get("call_id") == call_ref["id"]
                    and work["spec"].get("kind") == "model"
                    and work["spec"].get("run_id") == run_id
                    and work["spec"].get("configuration_ref")
                    == intent.get("configuration_ref")
                    and cap_by_id.get(cap_id, {}).get("lane")
                    == work["spec"].get("lane") == intent.get("instance_id")
                    and blocked_capacity_ok
                    and isinstance(intent.get("request_token_estimate"), dict)
                    and type(intent["request_token_estimate"].get("tokens")) is int
                    and intent["request_token_estimate"]["tokens"] >= 0
                    and type(work["spec"].get("output_reservation_tokens")) is int
                    and work["spec"]["output_reservation_tokens"] > 0
                    and checked_block_packet(intent, call, config, work)
                    and (uncertain_stop or len(control) == 1
                         and control[0].get("phase") == "ready_model"
                         and control[0].get("work_id") is None
                         and control[0].get("actor_id") == intent.get("instance_id")
                         and control[0].get("task_id") == intent.get("task_id")
                         and control[0].get("mission_id") == child.get("mission_id")
                         and control[0].get("alias") in child.get("agent_ids", []))
                )

            terminal_recoveries = ([(identifier, body) for identifier, body in recoveries
                                    if body.get("request_sha256")
                                    == last.get("request_sha256")]
                                   if uncertain_stop else [])
            recovered_uncertain_stop = bool(terminal_recoveries)
            if recovered_uncertain_stop:
                historical_unknown = [item for item in prior_attempts
                                      if item.get("state") == "uncertain"]
            terminal_recovery_id = (terminal_recoveries[0][0]
                                    if len(terminal_recoveries) == 1 else None)
            terminal_recovery_ref = ({"kind": "model_response_lost",
                                      "id": terminal_recovery_id, "version": 1}
                                     if terminal_recovery_id else None)
            recovered_controls = ([body for _, body in controls
                                   if (body.get("feedback") or {}).get("old_call_ref")
                                   == call_ref] if uncertain_stop else [])
            recovered_terminal_ok = bool(
                recovered_uncertain_stop and len(terminal_recoveries) == 1
                and historical_unknown_ok(last)
                and len(recovered_controls) == 1
                and recovered_controls[0].get("run_id") == run_id
                and recovered_controls[0].get("mission_id") == child.get("mission_id")
                and recovered_controls[0].get("phase") in {"retry_at", "ready_model"}
                and recovered_controls[0].get("work_id") is None
                and (recovered_controls[0].get("feedback") or {}).get("status")
                == "model_response_lost"
                and recovered_controls[0]["feedback"].get("remote_response") == "unknown"
                and recovered_controls[0]["feedback"].get("recovery_ref")
                == terminal_recovery_ref
                and recovered_controls[0].get("model_response_lost_ref")
                == terminal_recovery_ref
                and type(recovered_controls[0].get("retry_at")) in {int, float}
                and math.isfinite(recovered_controls[0]["retry_at"])
                and recovered_controls[0]["retry_at"] >= 0
            )
            observed = [item for item in prior_attempts
                        if item.get("state") != "uncertain"]
            expected_observed = {item.get("request_sha256") for item in observed}
            expected_unknown = {item.get("request_sha256") for item in historical_unknown}
            blocked_sha = {item.get("request_sha256") for item in blocked_dispatches}
            blocked_actors = {call_by_id.get(item.get("call_ref", {}).get("id"), {}).get(
                "instance_id") for item in blocked_dispatches}
            blocked_actor_phases = {(call_by_id.get(item.get("call_ref", {}).get("id"), {}).get(
                "instance_id"), blocked_phase(item)) for item in blocked_dispatches}
            allowed_recovery_work = {body.get("work_ref", {}).get("id")
                                     for _, body in recoveries
                                     if body.get("request_sha256") in expected_unknown}
            allowed_blocked_work = {work_id for work_id, work in works
                                    if work.get("superseded_intent_ref") in [
                                        item.get("intent_ref") for item in blocked_dispatches]}
            selected_intents = [body for _, body in intents
                                if body.get("mission_id") == child.get("mission_id")]
            extra_intents = [body for body in selected_intents
                             if body.get("request_sha256") not in expected_sha | blocked_sha]

            def undispatched_intent_ok(intent):
                call_ref_pending = intent.get("call_ref")
                if not isinstance(call_ref_pending, dict):
                    return False
                matches = [(work_id, work) for work_id, work in works
                           if isinstance(work.get("spec"), dict)
                           and work["spec"].get("call_id")
                           == call_ref_pending.get("id")]
                controls_for_call = [body for _, body in controls
                                     if body.get("call_ref") == call_ref_pending]
                work_id, work = matches[0] if len(matches) == 1 else (None, {})
                cap_id = (work_id.replace("executor-work:", "executor-cap:", 1)
                          if isinstance(work_id, str)
                          and work_id.startswith("executor-work:") else None)
                capacity = cap_by_id.get(cap_id)
                call = call_by_id.get(call_ref_pending.get("id"), {})
                config = config_by_id.get(call.get("configuration_id"), {})
                spec = work.get("spec") if isinstance(work.get("spec"), dict) else {}
                cap_estimate = (capacity.get("request_token_estimate")
                                if isinstance(capacity, dict) else None)
                cap_estimate = cap_estimate if isinstance(cap_estimate, dict) else {}
                superseded = work.get("phase") == "superseded"
                return bool(
                    len(matches) == 1
                    and isinstance(capacity, dict)
                    and isinstance(call, dict) and isinstance(config, dict)
                    and candidate_actor_ok(intent, call, config, work)
                    and work.get("receipt_ref") is None
                    and work.get("result_ref") is None
                    and work.get("mission_id") == child.get("mission_id")
                    and spec.get("kind") == "model"
                    and spec.get("run_id") == run_id
                    and spec.get("mission_id") == child.get("mission_id")
                    and spec.get("call_version") == 1
                    and spec.get("configuration_ref") == intent.get("configuration_ref")
                    and call.get("mission_id") == child.get("mission_id")
                    and call.get("task_id") == spec.get("proposal_ref", {}).get("id")
                    and call.get("instance_id") == spec.get("actor_id")
                    and intent.get("mission_id") == child.get("mission_id")
                    and intent.get("call_ref") == call_ref_pending
                    and intent.get("credential_profile") == case["credential_profile"]
                    and intent.get("request_sha256") not in dispatch_by_sha
                    and intent.get("request_sha256") not in response_by_sha
                    and (superseded and (
                        work.get("status_basis") == "new_peer_input_before_dispatch"
                        and work.get("superseded_intent_ref") == {
                            "kind": "model_request_intent",
                            "id": f"model-intent:{call_ref_pending['id']}:1", "version": 1,
                        }
                        and not controls_for_call
                        and capacity.get("phase") == "canceled"
                        and capacity.get("diagnostic") == "new_peer_input_before_dispatch"
                        and capacity.get("cancel_requested") is True
                        and capacity.get("attempts") == []
                    ) or not superseded and (
                        work.get("phase") == "pending"
                        and work.get("superseded_intent_ref") is None
                        and len(controls_for_call) == 1
                        and controls_for_call[0].get("phase") == "pending_model"
                        and controls_for_call[0].get("run_id") == run_id
                        and controls_for_call[0].get("mission_id")
                        == child.get("mission_id")
                        and controls_for_call[0].get("actor_id")
                        == intent.get("instance_id")
                        and controls_for_call[0].get("task_id")
                        == intent.get("task_id")
                        and controls_for_call[0].get("alias") in child.get("agent_ids", [])
                        and controls_for_call[0].get("work_id") == spec.get("work_id")
                        and capacity.get("phase") in {"queued", "waiting"}
                        and capacity.get("attempts") == []
                        and capacity.get("cancel_requested") is False
                    ))
                    and capacity.get("kind") == "model"
                    and capacity.get("run_id") == run_id
                    and capacity.get("mission_id") == child.get("mission_id")
                    and capacity.get("call_id") == call_ref_pending["id"]
                    and capacity.get("task_id") == spec.get("proposal_ref", {}).get("id")
                    and capacity.get("lane") == spec.get("lane")
                    == intent.get("instance_id")
                    and capacity.get("reservation") is False
                    and capacity.get("local_active") is False
                    and capacity.get("external_active") is False
                    and capacity.get("owner") is None
                    and capacity.get("lease_until") is None
                    and capacity.get("output_ref") is None
                    and cap_estimate.get("intent_ref")
                    == {"kind": "model_request_intent",
                        "id": f"model-intent:{call_ref_pending['id']}:1", "version": 1}
                    and cap_estimate.get("request_sha256")
                    == intent.get("request_sha256")
                    and cap_estimate.get("tokens")
                    == intent.get("request_token_estimate", {}).get("tokens")
                    and capacity.get("output_reservation_tokens")
                    == spec.get("output_reservation_tokens")
                    and checked_block_packet(
                        intent, call, config, work,
                    )
                )
            allowed_undispatched_work = {work_id for intent in extra_intents
                                         for work_id, work in works
                                         if isinstance(work.get("spec"), dict)
                                         and isinstance(intent.get("call_ref"), dict)
                                         and work["spec"].get("call_id")
                                         == intent["call_ref"].get("id")}
            observation_receipts_ok = bool(
                len(expected_sha) == len(prior_attempts)
                and len(expected_unknown) == len(historical_unknown)
                and sum(body.get("request_sha256") in expected_unknown
                        for _, body in recoveries) == len(historical_unknown)
                and len(expected_observed) == len(observed)
                and len(selected_responses) == len(observed)
                and len(captured_dispatches) == len(observed)
                and set(response_by_sha) == expected_observed
                and {body.get("request_sha256") for body in captured_dispatches}
                == expected_observed
                and len(blocked_dispatches) <= len(controls) * phase
                and len(blocked_sha) == len(blocked_dispatches)
                and len(blocked_actor_phases) == len(blocked_dispatches)
                and all(index is not None for _, index in blocked_actor_phases)
                and None not in blocked_actors
                and {body.get("request_sha256") for _, body in selected_dispatches}
                == expected_sha | blocked_sha
                and len(selected_dispatches) == len(prior_attempts) + len(blocked_dispatches)
                and {body.get("request_sha256") for body in selected_intents}
                == expected_sha | blocked_sha | {
                    body.get("request_sha256") for body in extra_intents}
                and len(selected_intents) == len(selected_dispatches) + len(extra_intents)
                and len({body.get("request_sha256") for body in extra_intents})
                == len(extra_intents)
                and all(undispatched_intent_ok(item) for item in extra_intents)
                and all(saved_attempt_ok(item) for item in observed)
                and last_rate_wait_ok
                and all(historical_unknown_ok(item) for item in historical_unknown)
                and (not uncertain_stop or recovered_uncertain_stop
                     or uncertain_identity_ok)
                and (not recovered_uncertain_stop or recovered_terminal_ok)
                and all(blocked_candidate_ok(item) for item in blocked_dispatches)
                and all(work.get("phase") == "completed"
                        or captured_rate_wait(work_id, work)
                        or work_id in allowed_recovery_work
                        and work.get("phase") == "model_response_lost"
                        or work_id in allowed_blocked_work
                        and work.get("phase") == "superseded"
                        or uncertain_stop and work_id == work_id_for_uncertain
                        and work.get("phase") == "pending"
                        or work_id in allowed_undispatched_work
                        and work.get("phase") in {"pending", "superseded"}
                        for work_id, work in works
                        if work.get("mission_id") == child.get("mission_id"))
            )
        recovery_ok = True
    else:
        recovered = [body for _, body in controls
                     if body.get("run_id") == run_id
                     and body.get("task_id") == rejection.get("task_id")
                     and isinstance(body.get("feedback"), dict)
                     and body["feedback"].get("previous_response_ref") == rejection_ref]
        if status == 400:
            recovery_ok = (rejection.get("error_kind") in _RECOVERED_TOOL_ERRORS
                           and len(recovered) == 1
                           and recovered[0].get("phase") == "ready_model"
                           and recovered[0]["feedback"].get("status")
                           == _RECOVERED_TOOL_ERRORS[rejection["error_kind"]])
            if not recovery_ok and len(matching) == 1:
                recognized_type_repair = _recognized_tool_validation_repair(
                    state, run_id=run_id, mission_id=child.get("mission_id"),
                    observation=observation, rejection_id=rejection_id,
                    rejection=rejection, last=last, controls=controls,
                    intents=intents, dispatches=dispatches, works=works,
                    capacities=capacities, calls=calls,
                    configurations=configurations, copies=copies, uses=uses,
                    use_aliases=use_aliases, generations=generations,
                    generation_links=generation_links,
                )
                if recognized_type_repair is not None:
                    if recognized_type_repair["repair_state"] == "pending":
                        pending_type_repair = recognized_type_repair
                    else:
                        recovered_type_repair = recognized_type_repair
                    recovery_ok = True
        elif status == 429:
            recovery_ok = (len(recovered) == 1
                           and recovered[0].get("phase") in {"retry_at", "ready_model"}
                           and recovered[0]["feedback"].get("status")
                           == "provider_rate_limit_wait_completed"
                           and type(recovered[0].get("retry_at")) in {int, float}
                           and math.isfinite(recovered[0]["retry_at"])
                           and recovered[0]["retry_at"] >= 0)
        else:
            recovery_ok = True
        observation_receipts_ok = False
        last_response_ref = rejection_ref
    selected_evaluations = [body for identifier, body in evaluations
                            if identifier == f"evaluation-observation:{run_id}"]
    if (child.get("status") != "active"
            or (followup and (not ancestors or parent_id is not None
                              or set(by_run) != ancestors | {run_id}
                              or any(by_run[identifier].get("status") != "completed"
                                     for identifier in ancestors)
                              or child.get("parent_run_id") != request_data.get("parent_run_id")
                              or child.get("parent_snapshot_id")
                              != request_data.get("parent_snapshot_id")
                              or child.get("target_ref") != exact_target
                              or parent_snapshot is None
                              or parent_snapshot.get("mission_id")
                              != by_run[request_data["parent_run_id"]].get("mission_id")
                              or exact_target not in parent_snapshot.get("inspection", {}).get(
                                  "acceptedOutputReferences", [])))
            or (not followup and len(runs) != 1)
            or any(saved_profile.get(key) != value for key, value in expected_profile.items())
            or ((clean_stop or uncertain_stop or "agent_model_bindings" in case)
                and not observation_profile_ok)
            or (saved_profile.get("requiredAgentIds") or [])
            != request_data.get("required_agent_ids", [])
            or bool(saved_profile.get("allowVision", False))
            != request_data.get("allow_vision", False)
            or len(selected_evaluations) != 1
            or selected_evaluations[0].get("outcome") != expected_outcome
            or selected_evaluations[0].get("model_http_attempts") != prior["model_http_attempts"]
            or selected_evaluations[0].get("retrieval_http_attempts")
            != prior["retrieval_http_attempts"]
            or selected_evaluations[0].get("ledger_sha256")
            != hashlib.sha256(json.dumps(aggregate, sort_keys=True, separators=(",", ":"),
                                         ensure_ascii=False).encode()).hexdigest()
            or ((clean_stop or uncertain_stop) and not observation_receipts_ok)
            or (not clean_stop and not uncertain_stop
                and (len(matching) != 1 or not recovery_ok))):
        raise ValueError("Stopped state lacks the exact active run and terminal response"
                         if clean_stop or uncertain_stop else
                         "Stopped state lacks the exact active run and recovered rejection")
    if _state_digest(state) != source_state_before:
        raise ValueError("Stopped state changed during resume validation")
    return {"source_case": str(source_case), "source_run_id": run_id,
            "source_state_sha256": source_state_before,
            "protected_sha256": {name: hashlib.sha256(path.read_bytes()).hexdigest()
                                 for name, path in paths.items()},
            "prior_cumulative": prior, "prior_timing": prior_timing,
            "prior_controller_segments": segments,
            "prior_cumulative_budget_name": aggregate_path.name,
            "prior_cumulative_budget_sha256": hashlib.sha256(aggregate_path.read_bytes()).hexdigest(),
            "next_phase": phase + 1,
            "source_outcome": expected_outcome,
            "source_boundary": ("recovered_uncertain_model_response"
                                if recovered_uncertain_stop else
                                "uncertain_model_response" if uncertain_stop else
                                "clean_campaign_cap_stop" if cap_stop else
                                "clean_token_cap_stop" if token_cap_stop else
                                "clean_observation_stop" if clean_stop else
                                "provider_rejection_pending_local_repair"
                                if pending_type_repair is not None else
                                "provider_rejection"),
            **({"pending_type_repair": pending_type_repair}
               if pending_type_repair is not None else {}),
            **({"recovered_type_repair": recovered_type_repair}
               if recovered_type_repair is not None else {}),
            "last_response_ref": last_response_ref,
            **({"uncertain_control_id": uncertain_control_id,
                "uncertain_call_ref": call_ref,
                "uncertain_intent_ref": intent_ref,
                "uncertain_reservation_tokens": last["planned_tokens"]}
               if uncertain_stop else {}),
            "last_response_request_sha256": last.get("request_sha256"),
            "last_response_response_sha256": last.get("response_sha256"),
            "last_response_actual_tokens": last.get("actual_tokens"),
            **({"terminal_observation_429": True}
               if finite_observation_429 and status == 429 else {}),
            **({"last_rejection_status": status,
                "last_rejection_request_sha256": last.get("request_sha256"),
                "last_rejection_response_sha256": last.get("response_sha256"),
                "last_rejection_ref": rejection_ref}
               if not clean_stop and not uncertain_stop else {}),
            "followup": followup,
            "next_phase_limits": next_phase_limits,
            "effective_token_limit": effective_token_limit,
            "token_ceiling_revision": grant,
            "last_token_ceiling_revision": revisions[-1] if revisions else None,
            "qwen_estimate_policy": qwen_estimate_policy,
            "allow_qwen_text": case_qwen,
            "source_parent_state_sha256": case.get("copied_parent_state_sha256")}


def run_case(root: Path, folder: Path, *, execution_mode="live", deadline_seconds=600,
             model_limit=8, token_limit=55_000, retrieval_limit=4,
             accounting_only=False, until_complete=False, start_request: dict | None = None,
             followup_state: Path | None = None, followup_request: dict | None = None,
             resume_case: Path | None = None,
             token_ceiling_increase: int | None = None) -> dict:
    if start_request is not None and followup_request is not None:
        raise ValueError("Choose a start or follow-up request")
    if resume_case is not None and (bool(start_request) == bool(followup_request)
                                    or followup_state is not None):
        raise ValueError("Resume requires one exact start or follow-up request")
    if token_ceiling_increase is not None and (
        resume_case is None or accounting_only or type(token_ceiling_increase) is not int
        or token_ceiling_increase < 1
    ):
        raise ValueError("Token ceiling increase needs a finite resume and positive grant")
    if not accounting_only and (token_limit is None or retrieval_limit is None):
        raise ValueError("Finite token and retrieval limits are required without accounting-only mode")
    if accounting_only != (until_complete is True) or (
            accounting_only and any(value is not None for value in (
                model_limit, token_limit, retrieval_limit))):
        raise ValueError("Accounting-only mode requires no aggregate limits and until-complete observation")
    if until_complete and deadline_seconds is not None:
        raise ValueError("Until-complete observation cannot have a deadline")
    if (followup_state is None) != (followup_request is None) and resume_case is None:
        raise ValueError("Follow-up state and request metadata must be supplied together")
    followup = followup_request is not None
    request_data = None
    parent_digest = None
    if followup:
        allowed = {"request_id", "parent_run_id", "parent_snapshot_id", "target_id",
                   "target_version", "question", "scope", "agent_ids", "required_agent_ids",
                   "credential_profile", "allow_vision"}
        if not isinstance(followup_request, dict) or set(followup_request) - allowed:
            raise ValueError("Follow-up metadata contains unsupported fields")
        profile = followup_request.get("credential_profile", "primary")
        if profile not in {"primary", "backup"}:
            raise ValueError("Select Primary or Backup explicitly")
        request_data = {**followup_request, "credential_profile": profile,
                        "allow_vision": followup_request.get("allow_vision", False)}
        if resume_case is None:
            followup_state = Path(followup_state).resolve()
            if not (followup_state.is_dir() and (followup_state / "state.sqlite3").is_file()):
                raise ValueError("Follow-up needs an existing closed parent state directory")
            parent_digest = _state_digest(followup_state)
    elif start_request is not None:
        allowed = {"request_id", "question", "scope", "evidence_text", "agent_ids",
                   "required_agent_ids", "credential_profile", "allow_vision",
                   "agent_models"}
        if not isinstance(start_request, dict) or set(start_request) - allowed:
            raise ValueError("Start metadata contains unsupported fields")
        if start_request.get("credential_profile") not in {"primary", "backup"}:
            raise ValueError("Start request must select Primary or Backup")
        if ("agent_models" in start_request and start_request["agent_models"] is not None
                and type(start_request["agent_models"]) is not dict):
            raise ValueError("Per-agent model choices must be a mapping")
        request_data = {**start_request, "allow_vision": start_request.get("allow_vision", False)}
    allow_vision = request_data.get("allow_vision", False) if request_data else False
    if type(allow_vision) is not bool:
        raise ValueError("Vision authorization must be an explicit boolean")
    if accounting_only and request_data is None:
        raise ValueError("Accounting-only observation requires an explicit start or follow-up request")
    resume_lineage = (_resume_source(root, resume_case, request_data, execution_mode,
                                    followup=followup, model_limit=model_limit,
                                    token_limit=token_limit, retrieval_limit=retrieval_limit,
                                    deadline_seconds=deadline_seconds,
                                    accounting_only=accounting_only,
                                    until_complete=until_complete,
                                    token_ceiling_increase=token_ceiling_increase)
                      if resume_case is not None else None)
    phase_limits = (resume_lineage["next_phase_limits"] if resume_lineage is not None
                    else {"model": model_limit, "tokens": token_limit,
                          "retrieval": retrieval_limit})
    if followup and resume_lineage is not None:
        parent_digest = resume_lineage["source_parent_state_sha256"]
    folder = folder.resolve()
    private = (root / ".local/search-validation").resolve()
    if not folder.is_relative_to(private) or folder == private or folder.exists():
        raise ValueError("Use one fresh private search-validation case directory; no implicit resume")
    if resume_lineage is not None and folder.is_relative_to(Path(resume_lineage["source_case"])):
        raise ValueError("Resume output must not be inside the stopped case")
    if followup and followup_state is not None and folder.is_relative_to(followup_state):
        raise ValueError("Follow-up output must not be inside the parent state")
    folder.mkdir(parents=True, mode=0o700)
    request_id = request_data["request_id"] if request_data else REQUEST_ID
    run_id = "run-" + hashlib.sha256(json.dumps((request_id,), separators=(",", ":")).encode()).hexdigest()[:32]
    credential_profile = request_data["credential_profile"] if request_data else "primary"
    if resume_lineage is not None:
        shutil.copytree(Path(resume_lineage["source_case"]) / "state", folder / "state")
        if (_state_digest(folder / "state") != resume_lineage["source_state_sha256"]
                or _state_digest(Path(resume_lineage["source_case"]) / "state")
                != resume_lineage["source_state_sha256"]):
            raise ValueError("Stopped state changed during the private copy")
        prior_budget_source = (Path(resume_lineage["source_case"])
                               / resume_lineage["prior_cumulative_budget_name"])
        shutil.copyfile(prior_budget_source, folder / "prior-cumulative-budget.json")
        if hashlib.sha256((folder / "prior-cumulative-budget.json").read_bytes()).hexdigest() != (
            resume_lineage["prior_cumulative_budget_sha256"]
        ):
            raise ValueError("Stopped cumulative ledger changed during the private copy")
    manifest = freeze(root, folder, run_id, credential_profile,
                      share_provider_limits=execution_mode == "live")
    if followup and resume_lineage is None:
        shutil.copytree(followup_state, folder / "state")
    allow_qwen_text = (
        _parent_catalog_uses_qwen(folder / "state", request_data["parent_run_id"])
        if followup else
        any(type(choice) is dict and choice.get("model") == "qwen/qwen3.8-27b"
            for choice in (request_data.get("agent_models") or {}).values())
        if request_data is not None else False
    )
    if resume_lineage is not None and allow_qwen_text != resume_lineage["allow_qwen_text"]:
        raise ValueError("Resume Qwen model allowance differs from stopped case")
    qwen_estimate_policy = (
        resume_lineage["qwen_estimate_policy"] if resume_lineage is not None
        else "native_when_supported" if allow_qwen_text else "legacy_surrogate"
    )
    runtime = folder / "runtime"
    if execution_mode == "live" and any(name == "ora" or name.startswith("ora.") for name in sys.modules):
        raise ValueError("Launch live execution in a fresh process before importing ora")
    runtime_path = str(runtime / "src")
    sys.path.insert(0, runtime_path)
    try:
        from ora.contracts.models import LiveFollowupRequest, LiveStartRequest
        from ora.mission import MissionService
        from ora.observability.config import runtime_export_config
        from ora.observability.evaluation import (
            advance_evaluation_observation,
            combine_validation_ledgers,
            record_evaluation_observation,
        )
        from ora.retrieval.agent_validation_budget import (
            AgentBudget,
            BudgetAdmissionStop,
            BudgetStop,
        )
    finally:
        # The imported ora package retains its frozen __path__ for later lazy
        # imports. Do not leak this case's path into unrelated spawned processes.
        for index, entry in enumerate(sys.path):
            if entry is runtime_path:
                del sys.path[index]
                break
    selected = {"request_id": request_id, "run_id": run_id,
                "question": request_data["question"] if request_data else QUESTION,
                "model": "openai/gpt-oss-120b", "credential_profile": credential_profile,
                "reasoning": "pending saved run configuration",
                "max_model_calls": model_limit,
                "max_planned_tokens": (resume_lineage["effective_token_limit"]
                                       if resume_lineage is not None else token_limit),
                "max_retrieval_attempts": retrieval_limit, "deadline_seconds": deadline_seconds,
                **({"phase_limits": phase_limits} if resume_lineage is not None else {}),
                "accounting_only": accounting_only, "until_complete": until_complete,
                "allow_vision": allow_vision,
                **({"qwen_estimate_policy": qwen_estimate_policy}
                   if qwen_estimate_policy != "legacy_surrogate" else {}),
                "execution_mode": execution_mode, "prescribed_tool_sequence": None,
                "parent_lineage": ({key: request_data[key] for key in (
                    "parent_run_id", "parent_snapshot_id", "target_id", "target_version"
                )} if followup else None),
                "copied_parent_state_sha256": parent_digest}
    if resume_lineage is not None:
        saved_revision = (resume_lineage["token_ceiling_revision"]
                          or resume_lineage["last_token_ceiling_revision"])
        if saved_revision is not None:
            selected["token_ceiling_revision"] = saved_revision
    if request_data is not None and not followup and "agent_models" in request_data:
        selected["agent_models"] = request_data["agent_models"]
    if allow_qwen_text or "agent_models" in selected:
        selected["model_selection"] = "per_agent"
    if resume_lineage is not None:
        selected["resume_lineage"] = resume_lineage
        selected["phase"] = resume_lineage["next_phase"]
        selected["copied_resume_state_sha256"] = resume_lineage["source_state_sha256"]
    _atomic_json(folder / "case.json", selected)
    # Withheld review stays out of the submitted question and host feedback.
    _atomic_json(folder / "review-criteria.json", {
        "criteria": (["follow-up answer supported by retained permitted evidence",
                      "requested length and source link", "external retrieval allowance respected"]
                     if followup else
                     ["answer supported by inspected permitted evidence",
                      "requested scope and source links", "actual tool route and usage"]
                     if start_request else
                     ["official DOE source identity", "mechanism and climate tradeoff supported by delivered passages",
                      "at most 150 words", "source link", "actual route and checked-source use distinguished"]),
        "comparison": "none; natural choice and single-task support only",
    })
    host = None
    outcome = "observation_window_ended"
    original_http = urllib.request.AbstractHTTPHandler.do_open
    starting_threads = {thread.ident for thread in threading.enumerate()}
    closed = False
    evaluation_observation_saved = False
    prior_evaluation_observation = None
    cumulative_budget_snapshot = None
    dispatch_drain_status = "not_started"
    dispatch_drain_error = None
    started = time.time()
    controller_stop_elapsed = None
    with AgentBudget(folder / "budget.json", model_limit=phase_limits["model"],
                     token_limit=phase_limits["tokens"],
                     retrieval_limit=phase_limits["retrieval"], accounting_only=accounting_only,
                     allow_vision=allow_vision, allow_qwen_text=allow_qwen_text,
                     qwen_estimate_policy=qwen_estimate_policy) as budget:
        def observed_http(handler, http_class, request, **connection):
            if closed:
                raise BudgetAdmissionStop("case_closed")
            def perform(_handler, fullurl, data=None, timeout=None, **_kwargs):
                result = original_http(_handler, http_class, fullurl, **connection)
                if result.status >= 400:
                    raise urllib.error.HTTPError(fullurl.full_url, result.status,
                                                 result.reason, result.headers, result)
                return result
            return budget.open(perform, handler, request, timeout=getattr(request, "timeout", 120))

        urllib.request.AbstractHTTPHandler.do_open = observed_http
        try:
            host = MissionService(runtime, folder / "state", execution_mode=execution_mode,
                                  start_supervisor=False,
                                  trace_config=(runtime_export_config(runtime)
                                                if execution_mode == "live" else None))
            if resume_lineage is not None:
                saved_run = host.store.get("mission_run", run_id)
                if saved_run is None or saved_run["body"].get("status") != "active":
                    raise ValueError("Resume state lacks the selected active run")
                prior_evaluation_observation = host.store.get(
                    "evaluation_observation", f"evaluation-observation:{run_id}"
                )
                last_response = (host.store.get(**resume_lineage["last_response_ref"])
                                 if resume_lineage["last_response_ref"] is not None else None)
                if (prior_evaluation_observation is None
                        or prior_evaluation_observation["body"].get("outcome")
                        != resume_lineage["source_outcome"]
                        or (resume_lineage["source_boundary"] not in {
                            "uncertain_model_response", "recovered_uncertain_model_response",
                        }
                            and last_response is None)
                        or (last_response is not None and (
                            last_response["body"].get("mission_id")
                            != saved_run["body"].get("mission_id")
                            or last_response["body"].get("request_sha256")
                            != resume_lineage["last_response_request_sha256"]
                            or last_response["body"].get("response_sha256")
                            != resume_lineage["last_response_response_sha256"]))
                        or (resume_lineage["source_boundary"] in {
                            "provider_rejection", "provider_rejection_pending_local_repair",
                        } and (
                            last_response["body"].get("http_status")
                            != resume_lineage["last_rejection_status"]
                            or last_response["body"].get("status") != "failed"))
                        or (resume_lineage["source_boundary"] in {
                            "clean_observation_stop", "clean_campaign_cap_stop",
                            "clean_token_cap_stop",
                        } and not resume_lineage.get("terminal_observation_429") and (
                            last_response["body"].get("status")
                            not in {"complete", "tool_requested"}
                            or last_response["body"].get("http_status") not in {None, 200}
                            or (last_response["body"].get("usage") or {}).get("total_tokens")
                        != resume_lineage["last_response_actual_tokens"]))
                        or (resume_lineage.get("terminal_observation_429") and (
                            resume_lineage["source_boundary"] != "clean_observation_stop"
                            or last_response["body"].get("status") != "failed"
                            or last_response["body"].get("http_status") != 429
                            or last_response["body"].get("usage") not in ({}, None)))):
                    raise ValueError("Resume state lacks the exact saved terminal response"
                                     if resume_lineage["source_boundary"]
                                     == "clean_observation_stop" else
                                     "Resume state lacks the exact saved rejected response")
            schedule = host._schedule_model
            def bounded_schedule(control, **kwargs):
                if budget.data.get("stop_reason"):
                    return False
                if (phase_limits["model"] is not None
                        and len(budget.data["model_attempts"]) >= phase_limits["model"]):
                    budget.stop("model_attempt_cap")
                return schedule(control, **kwargs)
            host._schedule_model = bounded_schedule
            if followup:
                parent = host._run(request_data["parent_run_id"])
                if parent["body"]["status"] != "completed":
                    raise ValueError("Follow-up parent run must be closed and completed")
                run = host.followup(LiveFollowupRequest.model_validate(request_data))
            else:
                run = host.start(LiveStartRequest.model_validate(
                    request_data if request_data else {
                        "request_id": REQUEST_ID, "question": QUESTION,
                        "credential_profile": "primary", "agent_ids": ["agent-synthesis"],
                        "allow_vision": False,
                    }))
            assert run.run_id == run_id
            if resume_lineage is not None and run.reused is not True:
                raise ValueError("Resume must reuse the exact existing run")
            if resume_lineage is not None and resume_lineage["source_boundary"] == (
                "uncertain_model_response"
            ):
                pending = host.store.get("mission_task_control",
                                         resume_lineage["uncertain_control_id"])
                work = host._work_for(pending) if pending is not None else None
                spec = host.executor._spec_from(work) if work is not None else None
                capacity = host.capacity.get(host.executor._capacity_id(spec)) if spec else None
                if (pending is None or work is None or capacity is None
                        or pending["body"].get("call_ref")
                        != resume_lineage["uncertain_call_ref"]
                        or capacity.get("phase") != "uncertain"
                        or not host._repair_unknown_model(pending, work, capacity)):
                    raise ValueError("Copied uncertain model response could not be superseded")
                updated = host.store.get("mission_task_control",
                                         resume_lineage["uncertain_control_id"])
                feedback = updated["body"].get("feedback") if updated else None
                if (updated is None or updated["body"].get("work_id") is not None
                        or updated["body"].get("phase") not in {"retry_at", "ready_model"}
                        or not isinstance(feedback, dict)
                        or feedback.get("status") != "model_response_lost"
                        or feedback.get("old_call_ref")
                        != resume_lineage["uncertain_call_ref"]
                        or feedback.get("remote_response") != "unknown"
                        or updated["body"].get("model_response_lost_ref")
                        != feedback.get("recovery_ref")):
                    raise ValueError("Copied unknown-response recovery changed identity")
            if resume_lineage is not None and "pending_type_repair" in resume_lineage:
                repair = resume_lineage["pending_type_repair"]
                pending = host.store.get("mission_task_control", repair["control_id"])
                work = host._work_for(pending) if pending is not None else None
                spec = host.executor._spec_from(work) if work is not None else None
                capacity = host.capacity.get(host.executor._capacity_id(spec)) if spec else None
                if (pending is None or work is None or capacity is None
                        or not host._repair_tool_rejection(pending, work, capacity)):
                    raise ValueError("Copied pending tool rejection could not be repaired locally")
                updated = host.store.get("mission_task_control", repair["control_id"])
                feedback = updated["body"].get("feedback") if updated else None
                if (updated is None or updated["body"].get("phase") != "ready_model"
                        or updated["body"].get("work_id") is not None
                        or not isinstance(feedback, dict)
                        or feedback.get("previous_response_ref") != resume_lineage[
                            "last_response_ref"
                        ]
                        or any(feedback.get(key) != value
                               for key, value in repair["expected_feedback"].items())):
                    raise ValueError("Copied tool rejection feedback differs from validated error")
            saved_run = host._run(run_id)["body"]
            selected["reasoning"] = saved_run["profile"]["reasoningSettings"]
            if "model_selection" in selected:
                from ora.mission.model_bindings import resolve_agent_model

                _verify_saved_model_bindings(host, run_id, resolve_agent_model)
                selected["agent_model_bindings"] = saved_run["profile"]["agentModelBindings"]
                if "agent_models" in selected:
                    selected["saved_agent_models"] = saved_run["profile"].get("agentModels")
            _atomic_json(folder / "case.json", selected)
            while deadline_seconds is None or time.time() - started < deadline_seconds:
                host._drive_run(host._run(run_id))
                status = host._run(run_id)["body"]["status"]
                if budget.data.get("stop_reason"):
                    outcome = budget.data["stop_reason"]
                    break
                if status == "completed":
                    outcome = "completed"
                    break
                if status in {"cancelled", "failed"}:
                    outcome = "mission_" + status
                    budget.stop(outcome)
                if accounting_only:
                    controls = host._controls(run_id)
                    if any(item["body"].get("operator_credential_hold") for item in controls):
                        outcome = "operator_credential_hold"
                        budget.stop(outcome)
                    waits = [item["body"].get("capacity_wait_until") for item in controls]
                    if any(isinstance(wake, (int, float)) and wake - time.time() >= 3600
                           for wake in waits):
                        outcome = "long_provider_wait"
                        budget.stop(outcome)
                diagnostics = [row for row in host.store.records("mission_host_diagnostic")
                               if row["body"].get("run_id") == run_id
                               and row["body"].get("category") == "local_context_preparation"]
                if diagnostics:
                    outcome = "preparation_failure"
                    budget.stop(outcome)
                time.sleep(.05)
        except BudgetStop as exc:
            outcome = exc.reason
        except Exception as exc:
            outcome = "controller_error_" + type(exc).__name__
            try:
                budget.stop(outcome)
            except BudgetStop:
                pass
        finally:
            if host is not None:
                # Block new model work, but allow already returned responses and
                # local acceptance to settle before closing this private host.
                host._schedule_model = lambda *_args, **_kwargs: False
                try:
                    dispatch_drain_status = ("settled" if host.dispatch.drain_active(
                        timeout=DRAIN_TIMEOUT_SECONDS) else "timed_out")
                except Exception as exc:
                    dispatch_drain_status = "error"
                    dispatch_drain_error = type(exc).__name__
                if dispatch_drain_status != "settled" and outcome == "completed":
                    outcome = "dispatch_drain_incomplete"
                if outcome != "completed" and not budget.data.get("stop_reason"):
                    try:
                        budget.stop(outcome)
                    except BudgetStop:
                        pass
                until = time.time() + 3
                while time.time() < until:
                    if not any(row["state"] == "attempted" for key in ("model_attempts", "retrieval_attempts")
                               for row in budget.data[key]):
                        try:
                            host._drive_run(host._run(run_id))
                        except Exception:
                            pass
                        break
                    time.sleep(.05)
                # One stop reading feeds both the durable evaluation record and
                # the local observation. Shutdown time is measured separately.
                controller_stop_elapsed = max(0.0, time.time() - started)
                try:
                    if (dispatch_drain_status == "settled"
                            and host.store.get("mission_run", run_id) is not None):
                        with budget._mutex:
                            ledger_snapshot = json.loads(json.dumps(budget.data))
                        if resume_lineage is None:
                            record_evaluation_observation(
                                host.store, run_id, ledger_snapshot, outcome,
                                controller_stop_elapsed,
                            )
                        else:
                            prior_budget = json.loads(
                                (folder / "prior-cumulative-budget.json").read_text()
                            )
                            cumulative_budget_snapshot = combine_validation_ledgers(
                                prior_budget, ledger_snapshot,
                                token_ceiling_revision=resume_lineage["token_ceiling_revision"],
                            )
                            _atomic_json(folder / "cumulative-budget.json",
                                         cumulative_budget_snapshot)
                            advance_evaluation_observation(
                                host.store, run_id,
                                {key: prior_evaluation_observation[key]
                                 for key in ("kind", "id", "version")},
                                cumulative_budget_snapshot, outcome, controller_stop_elapsed,
                            )
                        evaluation_observation_saved = True
                except Exception:
                    # A trace diagnostic cannot prevent host shutdown or alter
                    # the research mission's accepted status.
                    pass
                host.close()
            if controller_stop_elapsed is None:
                controller_stop_elapsed = max(0.0, time.time() - started)
            closed = True
            # Closing the async loop does not prove its synchronous callbacks
            # stopped. Keep the deny guard installed if any worker remains.
            workers = [thread for thread in threading.enumerate() if thread.ident not in starting_threads]
            join_until = time.time() + 5
            for worker in workers:
                worker.join(timeout=max(0, join_until - time.time()))
            shutdown_pending = any(worker.is_alive() for worker in workers)
            if not shutdown_pending:
                urllib.request.AbstractHTTPHandler.do_open = original_http
            policy = json.loads((runtime / PROFILE).read_text())
            for name in ("primary", "backup"):
                policy["profiles"][name].update(enabled=False, allowed_run_ids=[])
            _atomic_json(runtime / PROFILE, policy)
            original = (root / PROFILE).read_bytes() if (root / PROFILE).exists() else None
            unchanged = (hashlib.sha256(original).hexdigest() if original else None) == manifest["original_profile_sha256"]
            total_elapsed = max(0.0, time.time() - started)
            result = {"run_id": run_id, "outcome": outcome, "elapsed_seconds": total_elapsed,
                      "controller_elapsed_seconds": controller_stop_elapsed,
                      "post_controller_finalization_seconds": total_elapsed - controller_stop_elapsed,
                      "evaluation_observation_saved": evaluation_observation_saved,
                      "model_attempts": len(budget.data["model_attempts"]),
                      "retrieval_attempts": len(budget.data["retrieval_attempts"]),
                      "settled_or_reserved_tokens": budget.data["planned_tokens"],
                      "peak_planned_tokens": budget.data["peak_planned_tokens"],
                      "stop_reason": budget.data.get("stop_reason"),
                      "original_profile_unchanged": unchanged, "private_profile_disabled": True,
                      "parent_state_unchanged": (
                          (_state_digest(followup_state) == parent_digest
                           if resume_lineage is None else
                           _state_digest(Path(resume_lineage["source_case"]) / "state")
                           == resume_lineage["source_state_sha256"])
                          if followup else None
                      ),
                      "parent_lineage": selected["parent_lineage"],
                      "dispatch_drain_status": dispatch_drain_status,
                      "dispatch_drain_error": dispatch_drain_error,
                      "shutdown_pending": shutdown_pending,
                      "tracing": host.tracing_status() if host else {"enabled": False},
                      "loaded_code_hashes": {str(Path(module.__file__).resolve()): hashlib.sha256(Path(module.__file__).read_bytes()).hexdigest()
                                             for name, module in list(sys.modules.items()) if name.startswith("ora.")
                                             and getattr(module, "__file__", None) and Path(module.__file__).is_file()},
                      "mission_status": host._run(run_id)["body"]["status"] if host and host.store.get("mission_run", run_id) else None}
            if resume_lineage is not None:
                phase_accounting = _prior_accounting(budget.data)
                prior_timing = resume_lineage["prior_timing"]
                cumulative_accounting = (_prior_accounting(cumulative_budget_snapshot)
                                         if cumulative_budget_snapshot is not None else None)
                phase_total_elapsed = result["elapsed_seconds"]
                phase_controller_elapsed = result["controller_elapsed_seconds"]
                result.update(
                    phase_model_attempts=result["model_attempts"],
                    phase_retrieval_attempts=result["retrieval_attempts"],
                    phase_elapsed_seconds=phase_total_elapsed,
                    phase_controller_elapsed_seconds=phase_controller_elapsed,
                    phase_post_controller_finalization_seconds=(
                        result["post_controller_finalization_seconds"]),
                    model_attempts=(cumulative_accounting or phase_accounting)["model_http_attempts"],
                    retrieval_attempts=(cumulative_accounting or phase_accounting)["retrieval_http_attempts"],
                    settled_or_reserved_tokens=(cumulative_accounting or phase_accounting)[
                        "settled_or_reserved_tokens"],
                    peak_planned_tokens=(cumulative_budget_snapshot or budget.data)[
                        "peak_planned_tokens"],
                    controller_elapsed_seconds=(prior_timing["controller_elapsed_seconds"]
                                                + phase_controller_elapsed),
                    elapsed_seconds=prior_timing["elapsed_seconds"] + phase_total_elapsed,
                    post_controller_finalization_seconds=(
                        prior_timing["post_controller_finalization_seconds"]
                        + result["post_controller_finalization_seconds"]),
                    controller_segments=[*resume_lineage["prior_controller_segments"],
                                         _controller_segment(budget.data,
                                                             phase_controller_elapsed,
                                                             phase_total_elapsed)],
                )
                result["post_controller_finalization_seconds"] = (
                    result["elapsed_seconds"] - result["controller_elapsed_seconds"]
                )
                result.update(
                    phase=resume_lineage["next_phase"],
                    resume_lineage=resume_lineage,
                    phase_accounting=phase_accounting,
                    cumulative_accounting=cumulative_accounting,
                    resume_source_unchanged=_resume_source_unchanged(resume_lineage),
                    prior_evaluation_observation_preserved=(
                        host is not None and prior_evaluation_observation is not None
                        and host.store.get(
                            "evaluation_observation", f"evaluation-observation:{run_id}",
                            prior_evaluation_observation["version"],
                        ) == prior_evaluation_observation
                    ),
                )
                current_evaluation = host.store.get(
                    "evaluation_observation", f"evaluation-observation:{run_id}"
                ) if host else None
                result["resume_integrity_complete"] = bool(
                    result["evaluation_observation_saved"]
                    and result["resume_source_unchanged"]
                    and result["prior_evaluation_observation_preserved"]
                    and result["original_profile_unchanged"]
                    and not result["shutdown_pending"]
                    and current_evaluation is not None
                    and current_evaluation["body"].get("outcome") == outcome
                    and current_evaluation["body"].get("model_http_attempts")
                    == result["cumulative_accounting"]["model_http_attempts"]
                    and current_evaluation["body"].get("retrieval_http_attempts")
                    == result["cumulative_accounting"]["retrieval_http_attempts"]
                    and current_evaluation["body"].get("ledger_sha256")
                    == hashlib.sha256(json.dumps(cumulative_budget_snapshot, sort_keys=True,
                                                  separators=(",", ":"), ensure_ascii=False).encode()).hexdigest()
                )
            _atomic_json(folder / "observation.json", result)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--output", type=Path, default=ROOT / ".local/search-validation/doe-cool-roof-20261015")
    parser.add_argument("--followup-state", type=Path)
    parser.add_argument("--followup-request", type=Path,
                        help="Private JSON with exact parent snapshot, target and new question")
    parser.add_argument("--resume-case", type=Path,
                        help="Closed private provider rejection or clean controlled stop to continue freshly")
    parser.add_argument("--start-request", type=Path,
                        help="Private JSON with a unique request ID, natural question, scope, peers and selected profile")
    parser.add_argument("--no-aggregate-limits", action="store_true",
                        help="Explicitly authorize accounting without model, token or retrieval totals")
    parser.add_argument("--until-complete", action="store_true",
                        help="Observe until mission completion or a classified stop, without an observation deadline")
    parser.add_argument("--model-limit", type=int, default=8)
    parser.add_argument("--no-model-call-cap", action="store_true",
                        help="Use the finite token limit without a separate model-call count")
    parser.add_argument("--token-limit", type=int, default=55_000)
    parser.add_argument("--resume-token-ceiling-increase", type=int,
                        help="Explicit positive addition to a stopped finite case's cumulative token ceiling")
    parser.add_argument("--retrieval-limit", type=int, default=4)
    parser.add_argument("--deadline-seconds", type=int, default=600)
    args = parser.parse_args(argv)
    if bool(args.followup_state) != bool(args.followup_request) and not args.resume_case:
        parser.error("--followup-state and --followup-request must be used together")
    if args.start_request and args.followup_request:
        parser.error("--start-request and --followup-request are mutually exclusive")
    if args.resume_case and (bool(args.start_request) == bool(args.followup_request)
                             or args.followup_state):
        parser.error("--resume-case requires one exact start or follow-up request")
    if args.resume_token_ceiling_increase is not None and (
        not args.resume_case or args.no_aggregate_limits
        or args.resume_token_ceiling_increase < 1
    ):
        parser.error("--resume-token-ceiling-increase needs a finite resume and positive grant")
    raw_args = argv if argv is not None else sys.argv[1:]
    if args.resume_case and not args.no_aggregate_limits:
        def explicit(flag):
            return any(word == flag or word.startswith(flag + "=")
                       for word in raw_args)
        if (not all(explicit(flag) for flag in (
                "--token-limit", "--retrieval-limit", "--deadline-seconds"))
                or not (args.no_model_call_cap or explicit("--model-limit"))):
            parser.error("Finite --resume-case needs explicit cumulative limits and deadline")
    if args.no_aggregate_limits != args.until_complete:
        parser.error("--no-aggregate-limits and --until-complete must be used together")
    if args.no_model_call_cap:
        if args.no_aggregate_limits or any(word == "--model-limit" or word.startswith("--model-limit=")
                                          for word in (argv if argv is not None else sys.argv[1:])):
            parser.error("--no-model-call-cap requires bounded mode without --model-limit")
        args.model_limit = None
    if args.no_aggregate_limits:
        if any(word == flag or word.startswith(flag + "=")
               for word in (argv if argv is not None else sys.argv[1:])
               for flag in ("--model-limit", "--token-limit", "--retrieval-limit", "--deadline-seconds")):
            parser.error("Do not mix aggregate limits or a deadline with accounting-only observation")
        args.model_limit = args.token_limit = args.retrieval_limit = args.deadline_seconds = None
    followup_request = json.loads(args.followup_request.read_text()) if args.followup_request else None
    if isinstance(followup_request, dict) and "followup_request" in followup_request:
        followup_request = followup_request["followup_request"]
    start_request = json.loads(args.start_request.read_text()) if args.start_request else None
    if isinstance(start_request, dict) and "start_request" in start_request:
        start_request = start_request["start_request"]
    if args.no_aggregate_limits and not (start_request or followup_request):
        parser.error("Accounting-only observation needs an explicit start or follow-up request")
    if not args.live:
        preview = {"question": (followup_request or start_request or {"question": QUESTION})["question"],
                   "live": False, "followup": bool(followup_request),
                   "model_calls": args.model_limit, "planned_tokens": args.token_limit,
                   "retrieval_attempts": args.retrieval_limit}
        if start_request or args.no_aggregate_limits:
            preview.update(start_request=bool(start_request),
                           credential_profile=(followup_request or start_request or {}).get(
                               "credential_profile", "primary"),
                           accounting_only=args.no_aggregate_limits,
                           until_complete=args.until_complete)
        if args.resume_case:
            preview["resume_case"] = str(args.resume_case)
        if args.resume_token_ceiling_increase is not None:
            preview["resume_token_ceiling_increase"] = args.resume_token_ceiling_increase
            preview["effective_cumulative_token_ceiling"] = (
                args.token_limit + args.resume_token_ceiling_increase)
        if (followup_request or start_request or {}).get("allow_vision") is True:
            preview["allow_vision"] = True
        if start_request is not None and "agent_models" in start_request:
            preview["agent_models"] = start_request["agent_models"]
        print(json.dumps(preview, indent=2))
        return 0
    result = run_case(ROOT, args.output, model_limit=args.model_limit,
                      token_limit=args.token_limit, retrieval_limit=args.retrieval_limit,
                      deadline_seconds=args.deadline_seconds,
                      accounting_only=args.no_aggregate_limits, until_complete=args.until_complete,
                      start_request=start_request,
                      followup_state=args.followup_state, followup_request=followup_request,
                      resume_case=args.resume_case,
                      token_ceiling_increase=args.resume_token_ceiling_increase)
    print(json.dumps({key: value for key, value in result.items() if key != "loaded_code_hashes"}, indent=2))
    return 0 if result["outcome"] == "completed" and result["dispatch_drain_status"] == "settled" and (
        not args.resume_case or result["resume_integrity_complete"]
    ) else 1


if __name__ == "__main__":
    raise SystemExit(main())
