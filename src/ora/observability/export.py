"""Project saved mission activity into optional, metadata-only OTLP traces.

The StateStore remains authoritative. Projection never calls a provider or an
observability endpoint; ``export_pending`` is a separate explicit operation.
An uncertain acknowledgement is retained and is not retried automatically.
"""

from __future__ import annotations

import hashlib
import math
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Callable, Mapping
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo

from ora.models.token_measurement import read_token_measurement
from ora.observability.evaluation import _OUTCOMES as _EVALUATION_OUTCOMES
from ora.observability.metrics import build_context_metrics, read_cache_observation
from ora.observability.tool_activity import TRUSTED_TOOL_NAMES, build_tool_activity
from ora.retrieval.redact import redact_text
from ora.state.store import StateConflict, StateError, StateStore, canonical, identifier

EXPORT_KIND = "observability_export"
EXPORT_SCHEMA = "ora.observability_export.v1"
MAX_PAGE = 1000
_STAGE_KINDS = frozenset({
    "request_envelope", "pinned_instructions", "attached_tool_schemas",
    "source_packet", "working_context", "tool_outcome", "assistant_tool_call",
    "context_or_skill_message",
})
_HASH_ATTRIBUTES = frozenset({
    "osra.mission_id_hash", "osra.run_id_hash", "osra.task_id_hash",
    "osra.attempt_id_hash", "osra.call_id_hash", "osra.request_sha256",
    "osra.tool.response_id_hash", "osra.tool.call_id_hash",
})
_TOKEN_ATTRIBUTES = frozenset({
    "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
    "gen_ai.usage.total_tokens", "gen_ai.usage.cache_read.input_tokens",
    "gen_ai.usage.reasoning.output_tokens", "osra.estimated.input_tokens",
    "osra.admission.requested_tokens", "osra.admission.limit_tokens",
    "osra.estimated.rate_input_headroom_tokens",
    *(f"osra.estimated.stage.{kind}_tokens" for kind in _STAGE_KINDS),
})
_USAGE_STATUS_ATTRIBUTES = frozenset(
    f"osra.usage.{name}.status" for name in (
        "input_tokens", "output_tokens", "total_tokens", "cache_read", "reasoning",
    )
)
_LABEL_ATTRIBUTES = frozenset({
    "gen_ai.system", "gen_ai.provider.name", "osra.outcome.status",
    "osra.event.category", "osra.event.status",
    "osra.model.role",
})
_SUMMARY_COUNTS = frozenset({
    "call_count", "model_call_count", "figure_call_count", "captured_call_count",
    "model_dispatch_recorded_count", "model_captured_response_count",
    "prepared_only_count",
    "error_or_unresolved_count", "input_tokens", "output_tokens", "total_tokens",
    "cache_read_tokens", "reasoning_output_tokens", "peak_reported_input_tokens",
    "peak_estimated_input_tokens", "input_reported_calls", "output_reported_calls",
    "cache_covered_calls", "cache_total_calls", "cache_covered_input_tokens",
    "cache_confirmed_input_calls",
    "token_mix_covered_calls", "reasoning_covered_calls", "reasoning_total_calls",
    "exact_ref_reuse_occurrences", "missing_exact_refs",
    "cache_hit_calls", "cache_miss_calls", "cache_invalid_calls",
    "cache_not_reported_calls", "cache_unknown_calls", "cache_legacy_uncertain_calls",
    "retrieval_recorded_attempt_count", "retrieval_recorded_delivery_count",
    "evaluation_model_http_attempts", "evaluation_retrieval_http_attempts",
    "diagnostic_model_http_attempts", "diagnostic_model_captured_responses",
    "diagnostic_model_usage_unknown_attempts", "diagnostic_retrieval_http_attempts",
})
_SUMMARY_PERCENTS = frozenset({
    "input_percent", "output_percent", "cache_read_percent",
    "cache_confirmed_percent",
    "reasoning_output_percent",
    "cache_input_coverage_percent",
})
_TOOL_SUMMARY_COUNTS = frozenset({
    "selected_count", "executed_count", "capability_selection_count",
    "operation_count", "publication_count", "reused_count",
    "delivered_count", "timed_count", "untrusted_count",
})
_TOOL_EXECUTION_STATES = frozenset({
    "not_executed", "executed", "accepted_report", "uncertain",
})
_TOOL_OUTCOMES = frozenset({"pending", "completed", "rejected", "failed", "unknown"})
_TOOL_REUSE_STATES = frozenset({"new", "reused", "unknown"})
_TOOL_REUSE_BASES = frozenset({
    "explicit_operation_reuse_status", "capability_attachment_flag",
    "public_document_http_fetch", "same_task_exact_read_window",
    "same_task_new_checked_location",
    "saved_peer_request_record",
})
_TOOL_DELIVERY_STATES = frozenset({
    "captured_later", "dispatched_later", "not_observed_yet",
    "not_delivered_after_terminal", "not_delivered_after_observation", "no_result_message",
})
_NEXT_TOOL_DELIVERY_STATES = frozenset({
    "no_next_captured_request", "no_result_message", "result_not_saved",
    "captured_without_result", "captured_with_exact_result",
    "captured_with_projected_or_restricted_result",
    "captured_request_not_confirmed_model_input",
})
_TOOL_CATEGORIES = frozenset({
    "capability_selection", "operation", "publication",
})
_TOOL_ATTRIBUTE_KEYS = frozenset({
    "langsmith.span.kind", "openinference.span.kind", "gen_ai.tool.name",
    "osra.mission_id_hash", "osra.run_id_hash", "osra.task_id_hash",
    "osra.call_id_hash", "osra.tool.response_id_hash", "osra.tool.call_id_hash",
    "osra.tool.step_id", "osra.tool.loop_index", "osra.tool.projection_revision",
    "osra.tool.category", "osra.tool.selection_status", "osra.tool.execution_status",
    "osra.tool.outcome", "osra.tool.reuse_status", "osra.tool.reuse_basis",
    "osra.tool.delivery_status",
    "osra.tool.delivery_final", "osra.tool.result_in_later_dispatched_request",
    "osra.tool.result_in_later_captured_request",
    "osra.tool.result_in_later_prepared_request", "osra.tool.delivery_form",
    "osra.tool.next_loop_index", "osra.tool.next_delivery_status",
    "osra.tool.next_delivery_form", "osra.tool.result_in_next_prepared_request",
    "osra.tool.result_in_next_captured_request",
    "osra.tool.selected_capability_name",
    "osra.tool.active_seconds", "osra.tool.active_duration_basis",
    "osra.tool.active_duration_status", "osra.tool.interval_status",
    "osra.tool.interval_basis", "osra.observed.wall_seconds",
    "osra.tool.digest", "output.value",
})
_TOOL_REQUIRED_KEYS = frozenset({
    "langsmith.span.kind", "openinference.span.kind", "gen_ai.tool.name",
    "osra.mission_id_hash", "osra.run_id_hash", "osra.task_id_hash",
    "osra.call_id_hash", "osra.tool.response_id_hash", "osra.tool.call_id_hash",
    "osra.tool.step_id", "osra.tool.loop_index", "osra.tool.projection_revision",
    "osra.tool.category", "osra.tool.selection_status", "osra.tool.execution_status",
    "osra.tool.outcome", "osra.tool.reuse_status", "osra.tool.delivery_status",
    "osra.tool.delivery_final", "osra.tool.result_in_later_dispatched_request",
    "osra.tool.result_in_later_captured_request",
    "osra.tool.result_in_later_prepared_request", "osra.tool.delivery_form",
    "osra.tool.next_delivery_status", "osra.tool.next_delivery_form",
    "osra.tool.result_in_next_prepared_request",
    "osra.tool.result_in_next_captured_request",
    "osra.tool.active_duration_status",
    "osra.tool.interval_status", "osra.tool.digest", "output.value",
})
_CACHE_STATES = frozenset({
    "reported_hit", "reported_zero", "not_reported", "malformed",
    "unavailable", "legacy_uncertain",
})
_MISSION_STATES = frozenset({
    "active", "waiting", "paused", "completed", "failed", "cancelled", "other",
})
_DIAGNOSTIC_LABELS = frozenset({"Cache wire probe", "Synthetic exact-repeat check"})
_DIAGNOSTIC_OUTCOMES = frozenset({"single_response_observed", "incomplete_pair"})
_LOCAL_REUSE_STATES = frozenset({
    "measured", "no_eligible_pair", "tokenizer_unavailable", "request_unavailable",
})
_LOCAL_REUSE_BASIS = "local_o200k_canonical_request_structure.v1"
_STRUCTURAL_REUSE_BASIS = "local_o200k_canonical_messages_and_whole_tool_schema.v1"
_STRUCTURAL_REUSE_STATES = frozenset({"complete", "partial", "baseline_only", "unavailable"})
_LOCAL_REUSE_COUNTS = frozenset({
    "eligible_requests", "eligible_pairs", "measured_pairs", "unsupported_requests",
    "overlap_skipped_pairs", "stable_tool_schema_pairs",
    "estimated_repeated_message_tokens", "estimated_current_message_tokens",
    "estimated_stable_tool_schema_tokens",
    "captured_terminal_model_calls", "request_unavailable_calls",
    "structural_candidate_calls", "structural_measured_calls",
    "structural_measured_followup_pairs", "structural_unavailable_calls",
    "structural_overlap_skipped_calls", "local_structural_input_tokens",
    "repeated_local_structural_input_tokens",
})
_URL_IN_TEXT = re.compile(r"(?i)\b(?:[a-z][a-z0-9+.-]*://|www\.)\S+")
_EMAIL_IN_TEXT = re.compile(r"(?i)\b[^\s@]+@[^\s@]+\.[^\s@]+\b")
_PHONE_IN_TEXT = re.compile(r"(?<!\w)\+?\d[\d\s().-]{7,}\d(?!\w)")
_LONG_TOKEN = re.compile(r"(?<![\w-])[A-Za-z0-9_-]{28,}(?![\w-])")
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b[\w-]*(?:api[_-]?key|token|secret|password|passwd|authorization|cookie)"
    r"\s*[=:]\s*[^\s,;]+"
)
_SECRET_TOKEN = re.compile(r"(?i)\b(?:sk|gsk|ghp|xoxb)-[a-z0-9_-]{8,}\b")
_PRIVATE_PATH = re.compile(r"(?i)(?<!\w)(?:/(?:users|home|private|tmp|var|etc)/\S+|[a-z]:\\\S+)")
_TRANSPORT_CODES = frozenset({
    "timeout", "connection_lost", "http_rejected", "redirect_rejected",
    "configuration_error", "dependency_unavailable", "transport_error", "sdk_export_failed",
    "sender_error",
})


class TransportError(StateError):
    """One attempted export failed; retain only a safe class and HTTP code."""

    def __init__(self, error_code: str, http_status: int | None = None) -> None:
        if (error_code not in _TRANSPORT_CODES
                or http_status is not None
                and (type(http_status) is not int or not 100 <= http_status <= 599)):
            raise ValueError("Invalid trace transport diagnostic")
        self.error_code = error_code
        self.http_status = http_status
        super().__init__(f"Trace export {error_code}")


@dataclass(frozen=True)
class ExportConfig:
    """The opt-in destination; secrets and private URLs never enter saved records."""

    enabled: bool = False
    endpoint: str | None = field(default=None, repr=False)
    api_key: str | None = field(default=None, repr=False)
    project: str | None = field(default=None, repr=False)
    timeout_seconds: float = 10.0

    @classmethod
    def from_env(cls, values: Mapping[str, str] | None = None) -> ExportConfig:
        env = os.environ if values is None else values
        flag = env.get("OSRA_TRACE_EXPORT_ENABLED", "false").strip().lower()
        if flag not in {"true", "false"}:
            raise StateError("Trace export flag must be true or false")
        if flag == "false":
            return cls()
        return cls(
            enabled=True,
            endpoint=env.get("OSRA_TRACE_OTLP_TRACES_ENDPOINT"),
            api_key=env.get("LANGSMITH_API_KEY"),
            project=env.get("OSRA_TRACE_LANGSMITH_PROJECT"),
        ).validated()

    def validated(self) -> ExportConfig:
        if not self.enabled:
            return self
        if not all(isinstance(value, str) and value.strip() for value in (
            self.endpoint, self.api_key, self.project,
        )):
            raise StateError("Enabled trace export needs an endpoint, API key and tracing project")
        if any(ord(char) < 32 or ord(char) == 127 for value in (
            self.endpoint, self.api_key,
        ) for char in value or ""):
            raise StateError("Trace endpoint or API key contains control characters")
        parsed = urlsplit(self.endpoint or "")
        allowed_hosts = {
            "api.smith.langchain.com", "eu.api.smith.langchain.com",
            "apac.api.smith.langchain.com", "aws.api.smith.langchain.com",
        }
        if (parsed.scheme != "https" or parsed.netloc not in allowed_hosts
                or parsed.geturl() != self.endpoint or parsed.username
                or parsed.password or parsed.query or parsed.fragment
                or parsed.path != "/otel/v1/traces"):
            raise StateError("Trace export needs a LangSmith HTTPS OTLP traces endpoint")
        if (len(self.project or "") > 255
                or any(ord(char) < 32 or ord(char) == 127 for char in self.project or "")):
            raise StateError("Trace project contains control characters")
        if (type(self.timeout_seconds) not in {int, float}
                or not math.isfinite(self.timeout_seconds)
                or not 0 < self.timeout_seconds <= 30):
            raise StateError("Trace export timeout must be between 0 and 30 seconds")
        return self


class ToolActivityCache:
    """Reuse one verified read projection until source records change.

    Observability exports do not invalidate it. A new mission, model, tool,
    executor, or evaluation record does, including a new version of a record.
    This cache is local to one observer or explicit CLI invocation.
    """

    def __init__(self) -> None:
        self._entries: dict[tuple[str, int, int, str, str], tuple[int, dict]] = {}

    def get(self, store: StateStore, mission_id: str, run_id: str) -> dict:
        if not _valid_run(store, mission_id, run_id):
            raise StateError("Requested trace run does not belong to this mission")
        stat = store.db_path.stat()
        key = (str(store.db_path), stat.st_dev, stat.st_ino, mission_id, run_id)
        db = store._connect()
        try:
            marker = db.execute(
                "SELECT COALESCE(MAX(rowid), 0) FROM records "
                "WHERE kind NOT GLOB 'observability_*'"
            ).fetchone()[0]
        finally:
            db.close()
        existing = self._entries.get(key)
        if existing is not None and existing[0] == marker:
            return existing[1]
        activity = build_tool_activity(store, run_id)
        if activity.get("mission_id") != mission_id or activity.get("run_id") != run_id:
            raise StateError("Saved tool activity and requested trace mission disagree")
        if len(self._entries) >= 16 and key not in self._entries:
            self._entries.clear()
        self._entries[key] = (marker, activity)
        return activity


def _hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _ids(run_id: str, source_ref: dict) -> tuple[str, str, str]:
    trace_id = _hash("osra.otel.trace.v1:" + run_id)[:32]
    root_id = _hash("osra.otel.root.v1:" + run_id)[:16]
    span_id = _hash("osra.otel.span.v1:" + canonical(source_ref))[:16]
    return trace_id, root_id, span_id


def _ref(record: dict) -> dict:
    return {key: record[key] for key in ("kind", "id", "version")}


def _nonnegative_int(value: object) -> int | None:
    return value if type(value) is int and value >= 0 else None


def _nonnegative_number(value: object) -> float | None:
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        return None
    return float(value)


def _safe_label(value: object) -> str:
    if (not isinstance(value, str) or not value
            or len(value) > 64 or any(char not in "abcdefghijklmnopqrstuvwxyz_0123456789" for char in value)):
        return "other"
    return value


def _safe_model(value: object) -> str | None:
    """Preserve a model identifier without exporting a URL or free-form text."""
    if (isinstance(value, str) and len(value) <= 100
            and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]*", value)):
        return value
    return None


def _task_label(value: object) -> str:
    """Keep a small input hint while removing common private identifiers."""
    if not isinstance(value, str):
        return "Research task"
    clean = _URL_IN_TEXT.sub("[link]", value)
    clean = _EMAIL_IN_TEXT.sub("[email]", clean)
    clean = _PHONE_IN_TEXT.sub("[number]", clean)
    clean = _PRIVATE_PATH.sub("[path]", clean)
    clean = _SECRET_ASSIGNMENT.sub("[credential]", clean)
    clean = _SECRET_TOKEN.sub("[credential]", clean)
    clean = _LONG_TOKEN.sub("[identifier]", clean)
    clean, _ = redact_text(clean)
    clean = "".join(char for char in clean if char.isprintable()).strip()
    clean = " ".join(clean.split())
    if not clean:
        return "Research task"
    if len(clean) <= 72:
        return clean
    prefix = clean[:71].rsplit(" ", 1)[0] or clean[:71]
    return prefix.rstrip(".,;: ") + "…"


def _run_identity(run: dict) -> tuple[str, dict[str, str]]:
    label = _task_label(run["body"].get("question"))
    started = datetime.fromisoformat(run["created_at"]).astimezone(timezone.utc)
    utc_stamp = started.strftime("%Y-%m-%dT%H:%M:%SZ")
    eastern_stamp = started.astimezone(ZoneInfo("America/New_York")).strftime(
        "%b %d %I:%M %p %Z"
    )
    short_ref = _hash(run["id"])[:10]
    return f"OSRA · {label} · {eastern_stamp} · {short_ref}", {
        "osra.run.label": label,
        "osra.run.started_utc": utc_stamp,
        "osra.run.started_et": eastern_stamp,
        "osra.run.short_ref": short_ref,
    }


def _diagnostic_title(label: object, started_utc: object, report_id: object) -> str | None:
    """Build a trace name from a fixed label, saved UTC time, and opaque ID."""
    if (not isinstance(label, str) or label not in _DIAGNOSTIC_LABELS
            or not isinstance(report_id, str)
            or not re.fullmatch(r"[a-f0-9]{16}", report_id)
            or not isinstance(started_utc, str)):
        return None
    try:
        started = datetime.fromisoformat(started_utc)
        if started.utcoffset() is None or started.utcoffset().total_seconds() != 0:
            return None
        if started.isoformat() != started_utc:
            return None
    except (TypeError, ValueError, OverflowError):
        return None
    eastern = started.astimezone(ZoneInfo("America/New_York")).strftime(
        "%b %d %I:%M %p %Z"
    )
    return f"OSRA diagnostic · {label} · {eastern} · {report_id[:10]}"


def _window_end(started_at: object, seconds: object) -> str | None:
    if not isinstance(started_at, str):
        return None
    duration = _nonnegative_number(seconds)
    if duration is None:
        return None
    try:
        start = datetime.fromisoformat(started_at)
        if start.utcoffset() is None:
            return None
        return (start + timedelta(seconds=duration)).isoformat()
    except (TypeError, ValueError, OverflowError):
        return None


def _local_reuse_attributes(value: object) -> dict:
    """Export a small structural-reuse proxy; never substitute provider usage."""
    if not isinstance(value, dict) or value.get("basis") != _LOCAL_REUSE_BASIS:
        return {"osra.local_reuse.status": "request_unavailable"}
    status = value.get("status")
    if not isinstance(status, str) or status not in _LOCAL_REUSE_STATES:
        return {"osra.local_reuse.status": "request_unavailable"}
    result = {"osra.local_reuse.status": status, "osra.local_reuse.basis": _LOCAL_REUSE_BASIS}
    for key in _LOCAL_REUSE_COUNTS:
        number = _nonnegative_int(value.get(key))
        if number is not None:
            result[f"osra.local_reuse.{key}"] = number
    fraction = _nonnegative_number(value.get("estimated_message_prefix_token_fraction"))
    if fraction is not None and fraction <= 1:
        result["osra.local_reuse.message_prefix_percent"] = round(fraction * 100, 3)
    if value.get("structural_basis") == _STRUCTURAL_REUSE_BASIS:
        result["osra.local_reuse.structural_basis"] = _STRUCTURAL_REUSE_BASIS
        result["osra.local_reuse.structural_coverage_status"] = value.get("structural_coverage_status")
        structural_fraction = _nonnegative_number(value.get("local_structural_input_reuse_fraction"))
        if structural_fraction is not None and structural_fraction <= 1:
            result["osra.local_reuse.structural_input_percent"] = round(structural_fraction * 100, 3)
    return result if _valid_local_reuse_attributes(result) else {
        "osra.local_reuse.status": "request_unavailable",
    }


def _valid_local_reuse_attributes(attributes: Mapping[str, object]) -> bool:
    status = attributes.get("osra.local_reuse.status")
    if status is None:
        return not any(key.startswith("osra.local_reuse.") for key in attributes)
    if not isinstance(status, str) or status not in _LOCAL_REUSE_STATES:
        return False
    def value(key: str):
        return attributes.get("osra.local_reuse." + key)
    for low, high in (("measured_pairs", "eligible_pairs"),
                      ("stable_tool_schema_pairs", "eligible_pairs"),
                      ("eligible_pairs", "eligible_requests"),
                      ("eligible_requests", "captured_terminal_model_calls"),
                      ("estimated_repeated_message_tokens", "estimated_current_message_tokens")):
        if value(low) is not None and value(high) is not None:
            if (_nonnegative_int(value(low)) is None or _nonnegative_int(value(high)) is None
                    or value(low) > value(high)):
                return False
    percentage = value("message_prefix_percent")
    if status == "measured":
        measured, total, repeated = (value("measured_pairs"),
                                     value("estimated_current_message_tokens"),
                                     value("estimated_repeated_message_tokens"))
        if (any(_nonnegative_int(item) is None for item in (measured, total, repeated))
                or measured == 0 or total == 0 or repeated > total
                or _nonnegative_number(percentage) is None
                or not math.isclose(percentage, 100 * repeated / total, abs_tol=0.00051)):
            return False
    elif percentage is not None:
        return False
    return _valid_structural_reuse_attributes(attributes)


def _valid_structural_reuse_attributes(attributes: Mapping[str, object]) -> bool:
    def value(key: str):
        return attributes.get("osra.local_reuse." + key)

    if value("structural_coverage_status") is None:
        return value("structural_input_percent") is None  # Historical projections.
    state = value("structural_coverage_status")
    if (not isinstance(state, str) or state not in _STRUCTURAL_REUSE_STATES
            or value("structural_basis") != _STRUCTURAL_REUSE_BASIS):
        return False
    candidates, measured, pairs, unavailable, overlap = (
        value(key) for key in ("structural_candidate_calls", "structural_measured_calls",
                              "structural_measured_followup_pairs", "structural_unavailable_calls",
                              "structural_overlap_skipped_calls"))
    if (any(_nonnegative_int(n) is None for n in (candidates, measured, pairs, unavailable, overlap))
            or measured + unavailable + overlap != candidates or pairs > measured):
        return False
    if ((state == "unavailable") != (measured == 0)
            or state == "baseline_only" and (pairs or unavailable or overlap)
            or state == "complete" and (not pairs or unavailable or overlap)
            or state == "partial" and not (unavailable or overlap)):
        return False
    denominator = value("local_structural_input_tokens")
    numerator = value("repeated_local_structural_input_tokens")
    percent = value("structural_input_percent")
    if measured and (_nonnegative_int(denominator) is None or denominator == 0):
        return False
    if not pairs:
        return numerator is None and percent is None
    return (_nonnegative_int(numerator) is not None and numerator <= denominator
            and _nonnegative_number(percent) is not None
            and math.isclose(percent, 100 * numerator / denominator, abs_tol=0.00051))


def _structural_reuse_output(attributes: Mapping[str, object]) -> str | None:
    """A run-wide local denominator includes each scope's first request."""
    state = attributes.get("osra.local_reuse.structural_coverage_status")
    if state is None:
        return None
    def value(key: str):
        return attributes.get("osra.local_reuse." + key)
    coverage = (f"{value('structural_measured_calls')}/{value('structural_candidate_calls')} "
                f"request records measured; {value('structural_unavailable_calls')} unavailable, "
                f"{value('structural_overlap_skipped_calls')} overlapping.")
    percent = value("structural_input_percent")
    if _nonnegative_number(percent) is not None:
        return (f"Local input reuse: {percent:.1f}% "
                f"({value('repeated_local_structural_input_tokens'):,}/"
                f"{value('local_structural_input_tokens'):,} local tokens; {state} coverage). "
                "Unchanged message prefixes plus identical whole tool definitions; "
                "first requests included at zero local reuse. " + coverage)
    reason = ("only first requests in separate comparison scopes"
              if state == "baseline_only" else "no measured follow-up comparison")
    return f"Local input reuse: unavailable ({reason}). " + coverage


def _local_reuse_output(attributes: Mapping[str, object]) -> str | None:
    status = attributes.get("osra.local_reuse.status")
    if status is None:
        return None  # Preserve validation of historical summary Outputs.
    if status == "measured":
        percent = attributes.get("osra.local_reuse.message_prefix_percent")
        if _nonnegative_number(percent) is not None:
            repeated = attributes.get("osra.local_reuse.estimated_repeated_message_tokens")
            total = attributes.get("osra.local_reuse.estimated_current_message_tokens")
            measured = attributes.get("osra.local_reuse.measured_pairs")
            pairs = attributes.get("osra.local_reuse.eligible_pairs")
            stable = attributes.get("osra.local_reuse.stable_tool_schema_pairs")
            if all(_nonnegative_int(n) is not None for n in (repeated, total, measured, pairs, stable)):
                eligible = attributes.get("osra.local_reuse.eligible_requests")
                captured = attributes.get("osra.local_reuse.captured_terminal_model_calls")
                coverage = (f" {eligible}/{captured} captured requests inspectable."
                            if all(_nonnegative_int(n) is not None for n in (eligible, captured)) else "")
                return (f"Prompt overlap (local estimate): {percent:.1f}% of compared follow-up message "
                        f"tokens repeat at the start ({repeated:,}/{total:,}; {measured}/{pairs} pairs). "
                        f"Tool definitions unchanged in {stable}/{pairs} pairs. "
                        "This measures request overlap, not provider cache hits." + coverage)
    reasons = {
        "no_eligible_pair": "no comparable follow-up request",
        "tokenizer_unavailable": "local tokenizer unavailable",
        "request_unavailable": "verified saved request content unavailable",
    }
    return (f"Prompt overlap: unavailable ({reasons.get(status, 'incomplete measurement')}); "
            "separate from provider caching.")


def _tool_category(name: str) -> str:
    if name == "request_capability":
        return "capability_selection"
    if name == "publish_result":
        return "publication"
    return "operation"


def _tool_summary_attributes(activity: dict) -> dict[str, int]:
    """Count trusted selections without treating capability requests as research work."""
    steps = activity["steps"]
    counts = activity["counts"]
    if any(step.get("tool_name") not in TRUSTED_TOOL_NAMES for step in steps):
        raise StateError("Tool summary contains an untrusted name")
    result = {
        "selected_count": len(steps),
        "executed_count": sum(step["execution_status"] in {"executed", "accepted_report"}
                              for step in steps),
        "capability_selection_count": sum(step["tool_name"] == "request_capability"
                                          for step in steps),
        "operation_count": sum(_tool_category(step["tool_name"]) == "operation"
                               for step in steps),
        "publication_count": sum(step["tool_name"] == "publish_result" for step in steps),
        "reused_count": sum(step["reuse_status"] == "reused" for step in steps),
        "delivered_count": sum(step["delivery_status"] == "captured_later" for step in steps),
        "timed_count": sum(_nonnegative_number(step.get("duration_seconds")) is not None
                           for step in steps),
        "untrusted_count": counts["untrusted_or_unmatched_tool_calls"],
    }
    if any(_nonnegative_int(value) is None for value in result.values()):
        raise StateError("Tool summary counts are invalid")
    return {f"osra.tools.{key}": value for key, value in result.items()}


def _tool_output(attributes: Mapping[str, object]) -> str:
    """Render only trusted names, enums and measured timing for LangSmith Outputs."""
    name = attributes["gen_ai.tool.name"]
    loop = attributes["osra.tool.loop_index"]
    category = attributes["osra.tool.category"].replace("_", " ")
    execution = attributes["osra.tool.execution_status"].replace("_", " ")
    delivery = attributes["osra.tool.delivery_status"].replace("_", " ")
    reuse = attributes["osra.tool.reuse_status"]
    reuse_basis = attributes.get("osra.tool.reuse_basis")
    reuse_text = reuse + (f" ({reuse_basis.replace('_', ' ')})" if reuse_basis else "")
    lines = [f"Loop {loop}: selected {name} ({category}).",
             f"Execution: {execution}; outcome: {attributes['osra.tool.outcome']}; "
             f"reuse: {reuse_text}."]
    next_loop = attributes.get("osra.tool.next_loop_index")
    next_label = f"loop {next_loop}" if next_loop is not None else "none captured"
    next_status = attributes["osra.tool.next_delivery_status"].replace("_", " ")
    lines.append(f"Next recorded model attempt: {next_label}; tool-message status: {next_status}.")
    if (attributes["osra.tool.result_in_next_prepared_request"] is True
            and attributes["osra.tool.result_in_next_captured_request"] is False):
        lines.append("Tool message was serialized for that attempt; model receipt did not confirm delivery.")
    lines.append(f"Any later tool-message delivery: {delivery}.")
    if attributes["osra.tool.delivery_form"] == "host_projected_or_restricted":
        lines.append("A host-projected tool message reached a later request; full source text is unproven.")
    elif attributes["osra.tool.delivery_form"] == "exact_saved_message":
        lines.append("The exact saved tool message reached a later request.")
    selected = attributes.get("osra.tool.selected_capability_name")
    if selected is not None:
        lines.append(f"Requested capability: {selected}.")
    active = attributes.get("osra.tool.active_seconds")
    if active is not None:
        lines.append(f"Executor active time: {active:.3f}s (receipt reported; excludes model time).")
    else:
        lines.append("Executor active time: unavailable.")
    if attributes.get("osra.tool.interval_status") == "single_attempt_measured":
        lines.append("Span duration: one validated tool attempt; not model-call latency.")
    else:
        lines.append("Span timestamp marks tool selection; execution interval unavailable.")
    lines.append("Source/context packet inclusion is assessed separately.")
    revision = attributes["osra.tool.projection_revision"]
    if revision > 1:
        lines.append(f"Status update {revision} for the same selected tool.")
    return "\n".join(lines)


def _tool_attributes(step: dict, revision: int) -> dict:
    name = step.get("tool_name")
    if (name not in TRUSTED_TOOL_NAMES or type(revision) is not int or revision < 1
            or step.get("delivery_final") is not True):
        raise StateError("Tool projection needs a final trusted selection")
    attributes: dict[str, str | int | float | bool] = {
        "langsmith.span.kind": "tool", "openinference.span.kind": "TOOL",
        "gen_ai.tool.name": name,
        "osra.mission_id_hash": _hash(step["mission_id"]),
        "osra.run_id_hash": _hash(step["run_id"]),
        "osra.task_id_hash": _hash(step["task_id"]),
        "osra.call_id_hash": _hash(step["call_ref"]["id"]),
        "osra.tool.response_id_hash": _hash(step["response_ref"]["id"]),
        "osra.tool.call_id_hash": _hash(step["tool_call_id"]),
        "osra.tool.step_id": step["step_id"],
        "osra.tool.loop_index": step["loop_index"],
        "osra.tool.projection_revision": revision,
        "osra.tool.category": _tool_category(name),
        "osra.tool.selection_status": step["selection_status"],
        "osra.tool.execution_status": step["execution_status"],
        "osra.tool.outcome": step["outcome"],
        "osra.tool.reuse_status": step["reuse_status"],
        "osra.tool.delivery_status": step["delivery_status"],
        "osra.tool.delivery_final": True,
        "osra.tool.delivery_form": step["delivery_form"],
        "osra.tool.next_delivery_status": step["next_delivery_status"],
        "osra.tool.next_delivery_form": step["next_delivery_form"],
        "osra.tool.result_in_next_prepared_request": step[
            "result_in_next_prepared_request"],
        "osra.tool.result_in_next_captured_request": step[
            "result_in_next_captured_request"],
        "osra.tool.result_in_later_prepared_request": step[
            "result_in_later_prepared_request"],
        "osra.tool.result_in_later_dispatched_request": step[
            "result_in_later_dispatched_request"],
        "osra.tool.result_in_later_captured_request": step[
            "result_in_later_captured_request"],
    }
    reuse_basis = step.get("reuse_basis")
    if reuse_basis is not None:
        if reuse_basis not in _TOOL_REUSE_BASES or step["reuse_status"] == "unknown":
            raise StateError("Tool reuse basis is unsupported")
        attributes["osra.tool.reuse_basis"] = reuse_basis
    elif step["reuse_status"] != "unknown":
        raise StateError("Tool reuse status needs a basis")
    next_loop = step.get("next_model_loop_index")
    if next_loop is not None:
        if type(next_loop) is not int or next_loop <= step["loop_index"]:
            raise StateError("Tool next-model loop index is invalid")
        attributes["osra.tool.next_loop_index"] = next_loop
    selected = step.get("selected_capability_name")
    if selected is not None:
        if name != "request_capability" or selected not in TRUSTED_TOOL_NAMES:
            raise StateError("Tool projection has an untrusted capability selection")
        attributes["osra.tool.selected_capability_name"] = selected
    active = _nonnegative_number(step.get("duration_seconds"))
    if active is not None:
        if step.get("duration_basis") != "executor_receipt_active_seconds_store_clock":
            raise StateError("Tool active duration has no validated basis")
        attributes["osra.tool.active_seconds"] = active
        attributes["osra.tool.active_duration_basis"] = step["duration_basis"]
    elif step.get("duration_basis") is not None:
        raise StateError("Tool duration basis is unsupported")
    attributes["osra.tool.active_duration_status"] = (
        "reported" if active is not None else "unavailable"
    )
    start = step.get("interval_start_utc")
    end = step.get("interval_end_utc")
    basis = step.get("interval_basis")
    if start is not None or end is not None or basis is not None:
        if (not isinstance(start, str) or not isinstance(end, str)
                or basis != "single_capacity_attempt_wall_interval"):
            raise StateError("Tool interval has no validated single-attempt basis")
        try:
            start_time = datetime.fromisoformat(start)
            end_time = datetime.fromisoformat(end)
            interval = (end_time - start_time).total_seconds()
        except (TypeError, ValueError, OverflowError) as exc:
            raise StateError("Tool interval is invalid") from exc
        if (start_time.utcoffset() is None or end_time.utcoffset() is None
                or not math.isfinite(interval) or interval < 0):
            raise StateError("Tool interval is invalid")
        attributes["osra.tool.interval_status"] = "single_attempt_measured"
        attributes["osra.tool.interval_basis"] = basis
        attributes["osra.observed.wall_seconds"] = interval
    else:
        attributes["osra.tool.interval_status"] = "unavailable"
    attributes["output.value"] = _tool_output(attributes)
    attributes["osra.tool.digest"] = _hash(canonical({
        key: value for key, value in attributes.items()
        if key not in {"output.value", "osra.tool.digest"}
    }))
    return attributes


def _tool_recorded_at(step: dict) -> str:
    """Use the exact attempt end, or an instantaneous model-selection marker."""
    return step.get("interval_end_utc") or step["selected_at_utc"]


def _diagnostic_span_ids(report_id: str, digest: str | None = None) -> tuple[str, str]:
    version = "v2" if digest is not None else "v1"
    suffix = report_id + (":" + digest if digest is not None else "")
    return (_hash(f"osra.otel.diagnostic.trace.{version}:" + suffix)[:32],
            _hash(f"osra.otel.diagnostic.root.{version}:" + suffix)[:16])


def render_summary_output(attributes: Mapping[str, object]) -> str:
    """Render only enumerated and numeric summary metadata for LangSmith Outputs."""
    def bounded_count(key: str) -> int | None:
        value = _nonnegative_int(attributes.get(f"osra.summary.{key}"))
        return value if value is not None and value <= 10**12 else None

    def number(key: str) -> str:
        value = bounded_count(key)
        return f"{value:,}" if value is not None else "not reported"

    def percent(key: str) -> str | None:
        value = _nonnegative_number(attributes.get(f"osra.summary.{key}"))
        return f"{value:.1f}%" if value is not None and value <= 100 else None

    diagnostic = attributes.get("osra.summary.kind") == "api_diagnostic"
    if diagnostic:
        label = attributes.get("osra.diagnostic.task_label")
        label = label if isinstance(label, str) and label in _DIAGNOSTIC_LABELS else "Diagnostic"
        outcome = attributes.get("osra.summary.diagnostic_outcome")
        outcome = outcome if isinstance(outcome, str) and outcome in _DIAGNOSTIC_OUTCOMES else "other"
        captured = bounded_count("diagnostic_model_captured_responses")
        captured_label = "response" if captured == 1 else "responses"
        lines = [f"Diagnostic: {label}.",
                 f"Outcome: {outcome.replace('_', ' ')}; no research mission completion claimed.",
                 "HTTP attempts (validation ledger): "
                 f"{number('diagnostic_model_http_attempts')} model, "
                 f"{number('diagnostic_retrieval_http_attempts')} retrieval; "
                 f"{number('diagnostic_model_captured_responses')} captured model {captured_label}."]
    else:
        status = attributes.get("osra.summary.status")
        status = status if isinstance(status, str) and status in _MISSION_STATES else "other"
        lines = [f"Mission status: {status}."]
        outcome = attributes.get("osra.summary.evaluation_outcome")
        if isinstance(outcome, str) and outcome in _EVALUATION_OUTCOMES:
            lines.append(f"Evaluation controller: {outcome.replace('_', ' ')}; observation stopped.")
            lines.append(
                "Actual HTTP attempts (validation ledger): "
                f"{number('evaluation_model_http_attempts')} model, "
                f"{number('evaluation_retrieval_http_attempts')} retrieval."
            )
        else:
            lines.append("Evaluation controller: no saved stop observation.")
    window = _nonnegative_number(attributes.get("osra.summary.observed_window_seconds"))
    basis = attributes.get("osra.summary.duration_basis")
    if window is not None and basis == "ledger_first_start_to_last_attempt_end":
        lines.append(f"Observation window: {window:.2f}s from first ledger start "
                     "to last attempt end; includes any continuation gap, "
                     "not API latency.")
        measured_http = _nonnegative_number(attributes.get(
            "osra.summary.diagnostic_measured_http_seconds_sum"))
        gap = _nonnegative_number(attributes.get(
            "osra.summary.diagnostic_between_attempt_gap_seconds_sum"))
        if measured_http is not None:
            detail = f"Measured HTTP time summed: {measured_http:.2f}s"
            if gap is not None:
                detail += f"; between-attempt gap: {gap:.2f}s"
            lines.append(detail + ".")
    elif window is not None and basis == "saved_run_start_plus_evaluation_controller_elapsed":
        lines.append(f"Observation window: {window:.2f}s from saved run start "
                     "using controller elapsed time; not API latency.")
    provider_tokens = ("Provider tokens (reported subset): "
                       f"{number('input_tokens')} input, {number('output_tokens')} output")
    if not diagnostic or bounded_count("total_tokens") is not None:
        provider_tokens += f", {number('total_tokens')} total"
    lines.append(provider_tokens +
                 f"; {number('reasoning_output_tokens')} reasoning within output.")
    input_percent = percent("input_percent")
    output_percent = percent("output_percent")
    mix_covered = number("token_mix_covered_calls")
    if input_percent is not None and output_percent is not None:
        if diagnostic:
            lines.append(f"Token split: {input_percent} input / {output_percent} output "
                         f"({mix_covered}/{number('diagnostic_model_http_attempts')} "
                         "attempts with both fields).")
        else:
            mix_calls = bounded_count("token_mix_covered_calls")
            call_label = "call" if mix_calls == 1 else "calls"
            lines.append(f"Token split: {input_percent} input / {output_percent} output "
                         f"({mix_covered} {call_label} with both fields).")
    else:
        lines.append("Token split: unavailable without matching reported input and output.")
    covered = bounded_count("cache_covered_calls") or 0
    total = bounded_count("cache_total_calls") or 0
    not_reported = bounded_count("cache_not_reported_calls") or 0
    unknown = bounded_count("cache_unknown_calls") or 0
    legacy = bounded_count("cache_legacy_uncertain_calls") or 0
    hits = bounded_count("cache_hit_calls") or 0
    misses = bounded_count("cache_miss_calls") or 0
    cache_percent = percent("cache_read_percent")
    coverage_unit = "HTTP attempts" if diagnostic else "saved request records"
    confirmed_status = attributes.get("osra.summary.cache_confirmed_status")
    confirmed_percent = percent("cache_confirmed_percent")
    confirmed_input_calls = bounded_count("cache_confirmed_input_calls")
    captured = bounded_count(
        "diagnostic_model_captured_responses" if diagnostic else "captured_call_count"
    )
    response_coverage = (
        f" {covered}/{captured} captured responses had paired cache and input detail."
        if captured is not None and captured >= covered else ""
    )
    if not diagnostic and confirmed_status in {"complete", "minimum", "unknown"}:
        # The saved-record denominator includes preparation-only intents and
        # failed calls without usage. Lead with the paired-input denominator.
        paired = f"{covered}/{number('cache_confirmed_input_calls')} input-reporting calls"
        if confirmed_percent is not None and confirmed_status in {"complete", "minimum"}:
            qualifier = ("complete detail for reported input" if confirmed_status == "complete"
                         else "confirmed minimum; omitted cache detail remains unknown")
            lines.append(
                f"Cache: confirmed cached share of reported input {confirmed_percent} "
                f"({number('cache_read_tokens')}/{number('input_tokens')} tokens; "
                f"{paired} with detail; {qualifier})."
            )
        else:
            lines.append(
                f"Cache: confirmed cached share of reported input unknown "
                f"({paired} with detail); absent detail is not zero."
            )
        if total > (confirmed_input_calls or 0):
            without_input = total - (confirmed_input_calls or 0)
            prepared_only = bounded_count("prepared_only_count")
            detail = (
                f"{prepared_only} preparation-only and "
                f"{without_input - prepared_only} other "
                f"{'record' if without_input - prepared_only == 1 else 'records'} "
                "without reported input"
                if prepared_only is not None and prepared_only <= without_input
                else f"{without_input} without reported input"
            )
            lines.append(
                f"Cache records: {total} saved request records ({detail})."
            )
        http_attempts = bounded_count("evaluation_model_http_attempts")
        if (http_attempts is not None and confirmed_input_calls is not None
                and http_attempts > confirmed_input_calls):
            lines.append(
                f"Input usage unknown on {http_attempts - confirmed_input_calls} "
                "ledger-counted model HTTP "
                f"{'attempt' if http_attempts - confirmed_input_calls == 1 else 'attempts'}."
            )
    elif total == 0:
        lines.append(f"Cache: no {coverage_unit.lower()} to measure.")
    elif covered == 0 and not_reported == total:
        lines.append(f"Cache: Not reported by Groq (0/{total} {coverage_unit}); "
                     "cached-input percentage unknown.")
    elif covered == 0 and diagnostic and not_reported and unknown:
        response_label = "response" if not_reported == 1 else "responses"
        attempt_label = "attempt" if unknown == 1 else "attempts"
        lines.append(f"Cache: Not reported by Groq on {not_reported} {response_label}; "
                     f"{unknown} {attempt_label} unavailable "
                     f"(0/{total} {coverage_unit} covered); cached-input percentage unknown.")
    elif covered == 0 and legacy:
        lines.append(f"Cache: historical receipts do not establish provider reporting "
                     f"(0/{total} {coverage_unit}); cached-input percentage unknown.")
    elif covered == 0:
        lines.append(f"Cache: unavailable (0/{total} {coverage_unit} with paired input and cache detail).")
    elif cache_percent is None:
        lines.append(f"Cache: detail reported on {covered}/{total} {coverage_unit}; "
                     "cached-input percentage unavailable because reported input is zero."
                     + response_coverage)
    else:
        qualifier = "partial coverage" if covered < total else "complete coverage"
        subset = ("of input cached on reporting records" if covered < total
                  else "cached input")
        lines.append(f"Cache: {cache_percent} {subset} "
                     f"({number('cache_read_tokens')} tokens); {covered}/{total} "
                     f"{coverage_unit} with paired detail ({qualifier}; "
                     f"{hits} reported hit, {misses} reported zero)."
                     + response_coverage
                     + (" Whole-run cached-input percentage unknown."
                        if covered < total else ""))
    local_reuse = _local_reuse_output(attributes)
    structural_reuse = _structural_reuse_output(attributes)
    if structural_reuse is not None:
        lines.append(structural_reuse)
    if local_reuse is not None:
        lines.append(local_reuse)
    if diagnostic:
        unknown_usage = bounded_count("diagnostic_model_usage_unknown_attempts")
        unknown_label = "attempt" if unknown_usage == 1 else "attempts"
        lines.append(f"Unknown model usage: "
                     f"{number('diagnostic_model_usage_unknown_attempts')} {unknown_label}. "
                     "Evidence: recognized direct result and exact HTTP ledger.")
    else:
        retrieval_count = bounded_count("retrieval_recorded_attempt_count")
        retrieval_label = "attempt" if retrieval_count == 1 else "attempts"
        unresolved_count = bounded_count("error_or_unresolved_count")
        unresolved_label = "record" if unresolved_count == 1 else "records"
        lines.append(
            f"Progress: {number('model_dispatch_recorded_count')} pre-provider model dispatch markers, "
            f"{number('model_captured_response_count')} captured responses; "
            f"{number('retrieval_recorded_attempt_count')} saved retrieval {retrieval_label}; "
            f"{number('error_or_unresolved_count')} model/figure request {unresolved_label} with errors "
            "or unresolved status. Markers do not establish HTTP attempts."
        )
        lines.append(
            f"Context: largest reported input {number('peak_reported_input_tokens')} tokens; "
            f"exact reference reuse {number('exact_ref_reuse_occurrences')}; "
            f"missing exact refs {number('missing_exact_refs')}."
        )
        selected = _nonnegative_int(attributes.get("osra.tools.selected_count"))
        if selected is not None:
            capability_count = attributes["osra.tools.capability_selection_count"]
            operation_count = attributes["osra.tools.operation_count"]
            publication_count = attributes["osra.tools.publication_count"]
            tool_line = (
                f"Tools: {selected} selected "
                f"({capability_count} capability "
                f"{'request' if capability_count == 1 else 'requests'}, "
                f"{operation_count} {'operation' if operation_count == 1 else 'operations'}, "
                f"{publication_count} "
                f"{'publication' if publication_count == 1 else 'publications'}); "
                f"{attributes['osra.tools.executed_count']} executed/accepted, "
                f"{attributes['osra.tools.delivered_count']} tool messages reached a later captured model call; "
                f"{attributes['osra.tools.reused_count']} reused. "
                f"Active tool time recorded for {attributes['osra.tools.timed_count']}/{selected}; "
                "tool selection and model latency are separate."
            )
            if len("\n".join([*lines, tool_line])) <= 1600:
                lines.append(tool_line)
            else:
                short = f"Tools: {selected} selected; see named tool spans for outcomes and timing."
                if len("\n".join([*lines, short])) <= 1600:
                    lines.append(short)
    return "\n".join(lines)


def _safe_export_body(body: dict) -> bool:
    """Fail closed if a saved projection contains any unreviewed outbound data."""
    attributes = body.get("attributes")
    name = body.get("name")
    ordinary = {"osra.model.call", "osra.figure.call", "osra.operation",
                "osra.run.anchor", "osra.run.summary", "osra.run.final_report",
                "osra.diagnostic.summary"}
    named_anchor = isinstance(name, str) and name.startswith("OSRA · ")
    named_tool = isinstance(name, str) and name.startswith("Loop ")
    if (not isinstance(name, str) or name not in ordinary and not named_anchor and not named_tool
            or not isinstance(attributes, dict)
            or not isinstance(body.get("recorded_at"), str)
            or not _valid_local_reuse_attributes(attributes)):
        return False
    if named_anchor and (
        len(name) > 140
        or name != (
            f"OSRA · {attributes.get('osra.run.label')} · "
            f"{attributes.get('osra.run.started_et')} · "
            f"{attributes.get('osra.run.short_ref')}"
        )
    ):
        return False
    if name == "osra.diagnostic.summary":
        report_id = attributes.get("osra.diagnostic.report_id")
        revision = body.get("projection_revision", 1)
        digest = attributes.get("osra.summary.digest")
        if (revision not in {1, 2} or type(revision) is not int
                or revision == 2 and (
                    not isinstance(digest, str) or digest != _hash(canonical({
                        key: value for key, value in attributes.items()
                        if key != "osra.summary.digest"
                    })))):
            return False
        expected_title = _diagnostic_title(
            attributes.get("osra.diagnostic.task_label"),
            attributes.get("osra.diagnostic.started_utc"), report_id,
        )
        if (expected_title is None
                or body.get("mission_id") != f"diagnostic:{report_id}"
                or body.get("run_id") != f"diagnostic:{report_id}"
                or body.get("trace_id") != _diagnostic_span_ids(
                    report_id, digest if revision == 2 else None)[0]
                or body.get("span_id") != _diagnostic_span_ids(
                    report_id, digest if revision == 2 else None)[1]
                or attributes.get("osra.mission_id_hash") != _hash(f"diagnostic:{report_id}")
                or attributes.get("osra.run_id_hash") != _hash(f"diagnostic:{report_id}")
                or body.get("source_ref") is not None
                or body.get("parent_span_id") is not None
                or body.get("recorded_at") != _window_end(
                    attributes.get("osra.diagnostic.started_utc"),
                    attributes.get("osra.summary.observed_window_seconds"))
                or attributes.get("osra.observed.wall_seconds") != attributes.get(
                    "osra.summary.observed_window_seconds")
                or attributes.get("langsmith.trace.name") != expected_title):
            return False
    if name == "osra.run.final_report":
        run_id = body.get("run_id")
        digest = attributes.get("osra.summary.digest")
        title = (f"OSRA report · {attributes.get('osra.run.label')} · "
                 f"{attributes.get('osra.run.started_et')} · "
                 f"{attributes.get('osra.run.short_ref')}")
        if (not isinstance(run_id, str) or not isinstance(digest, str)
                or not re.fullmatch(r"[a-f0-9]{64}", digest)
                or body.get("trace_id") != _hash(
                    "osra.otel.final.trace.v1:" + run_id + ":" + digest)[:32]
                or body.get("span_id") != _hash(
                    "osra.otel.final.root.v1:" + run_id + ":" + digest)[:16]
                or body.get("source_ref") is not None
                or body.get("parent_span_id") is not None
                or attributes.get("langsmith.trace.name") != title
                or attributes.get("osra.summary.duration_basis")
                != "saved_run_start_plus_evaluation_controller_elapsed"
                or attributes.get("osra.summary.evaluation_observation_stopped") is not True
                or attributes.get("osra.observed.wall_seconds") != attributes.get(
                    "osra.summary.observed_window_seconds")
                or body.get("recorded_at") != _window_end(
                    attributes.get("osra.summary.final_started_at"),
                    attributes.get("osra.summary.observed_window_seconds"))):
            return False
    if named_tool:
        tool = attributes.get("gen_ai.tool.name")
        loop = attributes.get("osra.tool.loop_index")
        revision = attributes.get("osra.tool.projection_revision")
        step_id = attributes.get("osra.tool.step_id")
        digest = attributes.get("osra.tool.digest")
        run_id = body.get("run_id")
        source_ref = body.get("source_ref")
        if (not isinstance(tool, str) or tool not in TRUSTED_TOOL_NAMES
                or type(loop) is not int or not 1 <= loop <= 1_000_000
                or type(revision) is not int or not 1 <= revision <= 1_000_000
                or not isinstance(step_id, str)
                or not re.fullmatch(r"[a-f0-9]{24}", step_id)
                or not isinstance(digest, str)
                or not re.fullmatch(r"[a-f0-9]{64}", digest)
                or not isinstance(run_id, str)
                or not isinstance(source_ref, dict)
                or set(source_ref) != {"kind", "id", "version"}
                or source_ref.get("kind") != "model_response_receipt"
                or type(source_ref.get("version")) is not int
                or name != f"Loop {loop} · {tool}"
                or not _TOOL_REQUIRED_KEYS <= set(attributes)
                or set(attributes) - _TOOL_ATTRIBUTE_KEYS
                or body.get("trace_id") != _ids(run_id, source_ref)[0]
                or body.get("parent_span_id") != _ids(run_id, source_ref)[1]
                or body.get("span_id") != _hash(
                    f"osra.otel.tool.span.v1:{run_id}:{step_id}:{revision}")[:16]
                or digest != _hash(canonical({
                    key: value for key, value in attributes.items()
                    if key not in {"osra.tool.digest", "output.value"}
                }))
                or attributes.get("osra.tool.category") != _tool_category(tool)
                or attributes.get("osra.tool.delivery_final") is not True
                or attributes.get("osra.tool.selection_status") != "verified"
                or not isinstance(attributes.get("osra.tool.execution_status"), str)
                or attributes["osra.tool.execution_status"] not in _TOOL_EXECUTION_STATES
                or not isinstance(attributes.get("osra.tool.outcome"), str)
                or attributes["osra.tool.outcome"] not in _TOOL_OUTCOMES
                or not isinstance(attributes.get("osra.tool.reuse_status"), str)
                or attributes["osra.tool.reuse_status"] not in _TOOL_REUSE_STATES
                or (attributes["osra.tool.reuse_status"] == "unknown")
                != ("osra.tool.reuse_basis" not in attributes)
                or not isinstance(attributes.get("osra.tool.delivery_status"), str)
                or attributes["osra.tool.delivery_status"] not in _TOOL_DELIVERY_STATES
                or not isinstance(attributes.get("osra.tool.next_delivery_status"), str)
                or attributes["osra.tool.next_delivery_status"] not in _NEXT_TOOL_DELIVERY_STATES
                or not isinstance(attributes.get("osra.tool.delivery_form"), str)
                or not isinstance(attributes.get("osra.tool.next_delivery_form"), str)
                or attributes.get("osra.tool.active_duration_status") not in {
                    "reported", "unavailable"}
                or attributes.get("osra.tool.interval_status") not in {
                    "single_attempt_measured", "unavailable"}
                or (attributes.get("osra.tool.active_duration_status") == "reported")
                != ("osra.tool.active_seconds" in attributes)
                or ("osra.tool.active_seconds" in attributes)
                != ("osra.tool.active_duration_basis" in attributes)
                or (attributes.get("osra.tool.interval_status") == "unavailable"
                    and ("osra.observed.wall_seconds" in attributes
                         or "osra.tool.interval_basis" in attributes))
                or (attributes.get("osra.tool.interval_status") == "single_attempt_measured"
                    and ("osra.observed.wall_seconds" not in attributes
                         or attributes.get("osra.tool.interval_basis")
                         != "single_capacity_attempt_wall_interval"))):
            return False
        if (attributes.get("osra.tool.result_in_later_captured_request") is True
                and attributes.get("osra.tool.delivery_status") != "captured_later"):
            return False
        if (attributes.get("osra.tool.delivery_status") == "captured_later"
                and (attributes.get("osra.tool.result_in_later_dispatched_request") is not True
                     or attributes.get("osra.tool.result_in_later_captured_request") is not True
                     or attributes.get("osra.tool.result_in_later_prepared_request") is not True
                     or attributes.get("osra.tool.delivery_form") == "none")):
            return False
        next_status = attributes.get("osra.tool.next_delivery_status")
        next_form = attributes.get("osra.tool.next_delivery_form")
        next_loop = attributes.get("osra.tool.next_loop_index")
        if (next_status not in _NEXT_TOOL_DELIVERY_STATES
                or (next_status == "no_next_captured_request") != (next_loop is None)
                or next_loop is not None and (
                    type(next_loop) is not int or not loop < next_loop <= 1_000_000)
                or (next_status in {
                    "captured_with_exact_result",
                    "captured_with_projected_or_restricted_result",
                }) != (attributes.get("osra.tool.result_in_next_captured_request") is True)
                or (attributes.get("osra.tool.result_in_next_captured_request") is True
                    and attributes.get("osra.tool.result_in_next_prepared_request") is not True)
                or (next_status == "captured_with_exact_result")
                != (next_form == "exact_saved_message")
                or (next_status == "captured_with_projected_or_restricted_result")
                != (next_form == "host_projected_or_restricted")):
            return False
    tool_counts = {key: attributes.get(f"osra.tools.{key}") for key in _TOOL_SUMMARY_COUNTS}
    if any(value is not None for value in tool_counts.values()):
        if (name not in {"osra.run.summary", "osra.run.final_report"}
                or any(_nonnegative_int(value) is None for value in tool_counts.values())
                or tool_counts["selected_count"] != (
                    tool_counts["capability_selection_count"]
                    + tool_counts["operation_count"] + tool_counts["publication_count"])
                or any(tool_counts[key] > tool_counts["selected_count"] for key in (
                    "executed_count", "reused_count", "delivered_count", "timed_count"))):
            return False
    try:
        _timestamp_ns(body["recorded_at"])
    except (TypeError, ValueError, OverflowError):
        return False
    for key, length in (("trace_id", 32), ("span_id", 16), ("parent_span_id", 16)):
        value = body.get(key)
        if key == "parent_span_id" and value is None:
            continue
        if not isinstance(value, str) or not re.fullmatch(rf"[a-f0-9]{{{length}}}", value):
            return False
    for key, value in attributes.items():
        if key in _HASH_ATTRIBUTES:
            valid = isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value)
        elif key in _TOKEN_ATTRIBUTES or key == "osra.http_status":
            valid = _nonnegative_int(value) is not None
        elif key == "gen_ai.tool.name":
            valid = named_tool and value in TRUSTED_TOOL_NAMES
        elif key in {"osra.tool.step_id", "osra.tool.digest"}:
            length = 24 if key.endswith("step_id") else 64
            valid = named_tool and isinstance(value, str) and bool(re.fullmatch(
                rf"[a-f0-9]{{{length}}}", value))
        elif key in {"osra.tool.loop_index", "osra.tool.projection_revision"}:
            valid = named_tool and type(value) is int and 1 <= value <= 1_000_000
        elif key == "osra.tool.next_loop_index":
            valid = named_tool and type(value) is int and 1 <= value <= 1_000_000
        elif key == "osra.tool.category":
            valid = named_tool and value in _TOOL_CATEGORIES
        elif key == "osra.tool.selection_status":
            valid = named_tool and value == "verified"
        elif key == "osra.tool.execution_status":
            valid = named_tool and value in _TOOL_EXECUTION_STATES
        elif key == "osra.tool.outcome":
            valid = named_tool and value in _TOOL_OUTCOMES
        elif key == "osra.tool.reuse_status":
            valid = named_tool and value in _TOOL_REUSE_STATES
        elif key == "osra.tool.reuse_basis":
            valid = named_tool and isinstance(value, str) and value in _TOOL_REUSE_BASES
        elif key == "osra.tool.delivery_status":
            valid = named_tool and value in _TOOL_DELIVERY_STATES
        elif key == "osra.tool.next_delivery_status":
            valid = named_tool and value in _NEXT_TOOL_DELIVERY_STATES
        elif key in {"osra.tool.delivery_final", "osra.tool.result_in_later_prepared_request",
                     "osra.tool.result_in_next_prepared_request",
                     "osra.tool.result_in_next_captured_request",
                     "osra.tool.result_in_later_dispatched_request",
                     "osra.tool.result_in_later_captured_request"}:
            valid = named_tool and type(value) is bool
        elif key in {"osra.tool.delivery_form", "osra.tool.next_delivery_form"}:
            valid = named_tool and value in {
                "exact_saved_message", "host_projected_or_restricted", "none",
            }
        elif key == "osra.tool.selected_capability_name":
            valid = named_tool and isinstance(value, str) and value in TRUSTED_TOOL_NAMES
        elif key == "osra.tool.active_seconds":
            valid = named_tool and _nonnegative_number(value) is not None
        elif key == "osra.tool.active_duration_basis":
            valid = named_tool and value == "executor_receipt_active_seconds_store_clock"
        elif key == "osra.tool.active_duration_status":
            valid = named_tool and value in {"reported", "unavailable"}
        elif key == "osra.tool.interval_status":
            valid = named_tool and value in {"single_attempt_measured", "unavailable"}
        elif key == "osra.tool.interval_basis":
            valid = named_tool and value == "single_capacity_attempt_wall_interval"
        elif key.startswith("osra.tools.") and key.removeprefix("osra.tools.") in _TOOL_SUMMARY_COUNTS:
            valid = _nonnegative_int(value) is not None
        elif key in _USAGE_STATUS_ATTRIBUTES:
            valid = value in {"reported", "not_reported", "unknown", "invalid", "legacy_uncertain"}
        elif key == "osra.cache.observation":
            valid = value in _CACHE_STATES
        elif key == "osra.local_reuse.status":
            valid = isinstance(value, str) and value in _LOCAL_REUSE_STATES
        elif key == "osra.local_reuse.basis":
            valid = value == _LOCAL_REUSE_BASIS
        elif key == "osra.local_reuse.structural_basis":
            valid = value == _STRUCTURAL_REUSE_BASIS
        elif key == "osra.local_reuse.structural_coverage_status":
            valid = isinstance(value, str) and value in _STRUCTURAL_REUSE_STATES
        elif key.startswith("osra.local_reuse.") and key.removeprefix("osra.local_reuse.") in _LOCAL_REUSE_COUNTS:
            valid = _nonnegative_int(value) is not None
        elif key in {"osra.local_reuse.message_prefix_percent", "osra.local_reuse.structural_input_percent"}:
            valid = _nonnegative_number(value) is not None and value <= 100
        elif key in _LABEL_ATTRIBUTES:
            valid = _safe_label(value) == value
        elif key in {"gen_ai.request.model", "gen_ai.response.model"}:
            valid = _safe_model(value) == value
        elif key == "osra.observed.wall_seconds":
            valid = _nonnegative_number(value) is not None
        elif key == "langsmith.span.kind":
            valid = value == "tool" if named_tool else value in {"llm", "chain"}
        elif key == "gen_ai.operation.name":
            valid = value == "chat"
        elif key == "openinference.span.kind":
            valid = value == "TOOL" if named_tool else value in {"CHAIN", "LLM"}
        elif key == "osra.projection.anchor":
            valid = value is True
        elif key == "osra.run.label":
            valid = isinstance(value, str) and value == _task_label(value) and len(value) <= 72
        elif key == "osra.run.started_utc":
            valid = (isinstance(value, str) and len(value) == 20
                     and bool(re.fullmatch(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z", value)))
        elif key == "osra.run.started_et":
            valid = (isinstance(value, str) and len(value) <= 24
                     and bool(re.fullmatch(
                         r"[A-Z][a-z]{2} \d{2} \d{2}:\d{2} [AP]M (?:EDT|EST)", value,
                     )))
        elif key == "osra.run.short_ref":
            valid = isinstance(value, str) and bool(re.fullmatch(r"[a-f0-9]{10}", value))
        elif key == "osra.summary.digest":
            valid = isinstance(value, str) and bool(re.fullmatch(r"[a-f0-9]{64}", value))
        elif key == "osra.summary.status":
            valid = isinstance(value, str) and value in _MISSION_STATES
        elif key == "osra.summary.evaluation_outcome":
            valid = isinstance(value, str) and value in _EVALUATION_OUTCOMES
        elif key == "output.value":
            valid = (named_tool and isinstance(value, str) and len(value) <= 600
                     and value == _tool_output(attributes)) or (
                     name in {"osra.run.summary", "osra.run.final_report",
                              "osra.diagnostic.summary"}
                     and isinstance(value, str)
                     and len(value) <= 1600
                     and value == render_summary_output({
                         label: item for label, item in attributes.items()
                         if label != "output.value"
                     }))
        elif key == "osra.summary.evaluation_observation_stopped":
            valid = value is True
        elif key == "osra.summary.evaluation_basis":
            valid = value == "agent_validation_transport_ledger"
        elif key == "osra.summary.evaluation_elapsed_seconds":
            valid = _nonnegative_number(value) is not None
        elif key == "osra.summary.diagnostic_elapsed_seconds":
            valid = _nonnegative_number(value) is not None
        elif key == "osra.summary.final_started_at":
            valid = name == "osra.run.final_report" and _window_end(value, 0) is not None
        elif key in {"osra.summary.diagnostic_measured_http_seconds_sum",
                     "osra.summary.diagnostic_between_attempt_gap_seconds_sum",
                     "osra.summary.observed_window_seconds"}:
            valid = _nonnegative_number(value) is not None
        elif key == "osra.summary.duration_basis":
            valid = isinstance(value, str) and value in {
                "ledger_first_start_to_last_attempt_end",
                "saved_run_start_plus_evaluation_controller_elapsed",
            }
        elif key == "osra.summary.kind":
            valid = name == "osra.diagnostic.summary" and value == "api_diagnostic"
        elif key == "osra.summary.diagnostic_outcome":
            valid = (name == "osra.diagnostic.summary" and isinstance(value, str)
                     and value in _DIAGNOSTIC_OUTCOMES)
        elif key == "osra.diagnostic.report_id":
            valid = (name == "osra.diagnostic.summary" and isinstance(value, str)
                     and bool(re.fullmatch(r"[a-f0-9]{16}", value)))
        elif key == "osra.diagnostic.task_label":
            valid = (name == "osra.diagnostic.summary" and isinstance(value, str)
                     and value in _DIAGNOSTIC_LABELS)
        elif key == "osra.diagnostic.started_utc":
            valid = (name == "osra.diagnostic.summary" and isinstance(value, str)
                     and _diagnostic_title(attributes.get("osra.diagnostic.task_label"),
                                           value, attributes.get("osra.diagnostic.report_id"))
                     is not None)
        elif key == "langsmith.trace.name":
            valid = (name == "osra.diagnostic.summary" and isinstance(value, str)
                     and value == _diagnostic_title(
                         attributes.get("osra.diagnostic.task_label"),
                         attributes.get("osra.diagnostic.started_utc"),
                         attributes.get("osra.diagnostic.report_id"))) or (
                             name == "osra.run.final_report" and isinstance(value, str)
                             and value == (
                                 f"OSRA report · {attributes.get('osra.run.label')} · "
                                 f"{attributes.get('osra.run.started_et')} · "
                                 f"{attributes.get('osra.run.short_ref')}"
                             ))
        elif key == "osra.summary.cache_coverage_status":
            valid = value in {"complete", "partial", "unavailable"}
        elif key == "osra.summary.cache_confirmed_status":
            valid = value in {"complete", "minimum", "unknown"}
        elif key == "osra.summary.retrieval_http_attempt_status":
            valid = value == "unavailable_from_retrieval_attempt_receipts"
        elif key.startswith("osra.summary.retrieval_outcome."):
            valid = (_safe_label(key.removeprefix("osra.summary.retrieval_outcome."))
                     == key.removeprefix("osra.summary.retrieval_outcome.")
                     and _nonnegative_int(value) is not None)
        elif key in {"osra.summary.call_count_basis", "osra.summary.cache_coverage_basis"}:
            valid = value == "saved_request_intents"
        elif key.startswith("osra.summary.") and key.removeprefix("osra.summary.") in _SUMMARY_COUNTS:
            valid = _nonnegative_int(value) is not None
        elif key.startswith("osra.summary.") and key.removeprefix("osra.summary.") in _SUMMARY_PERCENTS:
            valid = (_nonnegative_number(value) is not None and value <= 100)
        elif key == "osra.summary.observed_elapsed_seconds":
            valid = _nonnegative_number(value) is not None
        else:
            valid = False
        if not valid:
            return False
    return True


def _matches_saved_run_label(store: StateStore, body: dict) -> bool:
    """A readable trace name must come from this exact saved run question."""
    name = body.get("name")
    if name != "osra.run.final_report" and not str(name).startswith("OSRA · "):
        return True
    if not isinstance(body.get("run_id"), str):
        return False
    run = store.get("mission_run", body["run_id"], 1)
    if run is None or run["body"].get("mission_id") != body.get("mission_id"):
        return False
    expected_name, expected_identity = _run_identity(run)
    if name == "osra.run.final_report":
        expected_title = (
            f"OSRA report · {expected_identity['osra.run.label']} · "
            f"{expected_identity['osra.run.started_et']} · "
            f"{expected_identity['osra.run.short_ref']}"
        )
        return (body["attributes"].get("langsmith.trace.name") == expected_title
                and body["attributes"].get("osra.summary.final_started_at")
                == run["created_at"]
                and all(body["attributes"].get(key) == value
                        for key, value in expected_identity.items()))
    return (body["name"] == expected_name
            and all(body["attributes"].get(key) == value
                    for key, value in expected_identity.items()))


def _matches_tool_lineage(store: StateStore, body: dict,
                          activity: dict | None = None) -> bool:
    """Recheck a named tool against the exact current saved-run activity before send."""
    if not str(body.get("name")).startswith("Loop "):
        return True
    run_id = body.get("run_id")
    if not isinstance(run_id, str) or not _valid_run(store, body.get("mission_id"), run_id):
        return False
    try:
        activity = activity if activity is not None else build_tool_activity(store, run_id)
        if activity["mission_id"] != body["mission_id"]:
            return False
        attributes = body["attributes"]
        step = next((item for item in activity["steps"]
                     if item["step_id"] == attributes.get("osra.tool.step_id")), None)
        if step is None or step.get("delivery_final") is not True:
            return False
        expected = _tool_attributes(step, attributes["osra.tool.projection_revision"])
        return (body.get("source_ref") == step["response_ref"]
                and body.get("recorded_at") == _tool_recorded_at(step)
                and body.get("name") == f"Loop {step['loop_index']} · {step['tool_name']}"
                and expected == attributes)
    except (KeyError, StateError, TypeError, ValueError):
        return False


def _valid_run(store: StateStore, mission_id: str, run_id: str) -> bool:
    run = store.get("mission_run", run_id, 1)
    return bool(run and run["body"].get("mission_id") == mission_id)


def _record_run(store: StateStore, receipt: dict) -> str | None:
    body = receipt["body"]
    call_ref = body.get("call_ref")
    if not isinstance(call_ref, dict) or set(call_ref) != {"kind", "id", "version"}:
        return None
    call = store.get(**call_ref)
    if (call is None or call["kind"] != "agent_call"
            or call["body"].get("mission_id") != body.get("mission_id")
            or call["body"].get("task_id") != body.get("task_id")
            or call["body"].get("attempt_id") != body.get("attempt_id")):
        return None
    actor_id = call["body"].get("instance_id")
    actor = store.get("agent_instance", actor_id, 1) if isinstance(actor_id, str) else None
    if actor is None or actor["body"].get("mission_id") != body.get("mission_id"):
        return None
    run_id = actor["body"].get("run_id")
    if (not isinstance(run_id, str) or not _valid_run(store, body["mission_id"], run_id)
            or body.get("run_id", run_id) != run_id
            or call["body"].get("run_id", run_id) != run_id):
        return None
    return run_id


def _figure_run(store: StateStore, receipt: dict) -> str | None:
    """Resolve a figure receipt through its exact intent and enrolled actor."""
    body = receipt["body"]
    pointer = body.get("intent_ref")
    if (not isinstance(pointer, dict) or set(pointer) != {"kind", "id", "version"}
            or pointer.get("kind") != "figure_request_intent"):
        return None
    intent = store.get(**pointer)
    if intent is None:
        return None
    saved = intent["body"]
    scope = ("mission_id", "run_id", "task_id", "actor_id")
    if (any(not isinstance(saved.get(key), str) or body.get(key) != saved[key] for key in scope)
            or not isinstance(saved.get("request_sha256"), str)
            or body.get("request_sha256") != saved["request_sha256"]):
        return None
    actor = store.get("agent_instance", saved["actor_id"], 1)
    if (actor is None or actor["body"].get("mission_id") != saved["mission_id"]
            or actor["body"].get("run_id") != saved["run_id"]
            or not _valid_run(store, saved["mission_id"], saved["run_id"])):
        return None
    return saved["run_id"]


def _trace_run(store: StateStore, record: dict) -> str | None:
    body = record["body"]
    run_id = body.get("run_id")
    actor_id = body.get("instance_id")
    if not isinstance(run_id, str) or not isinstance(actor_id, str):
        return None
    actor = store.get("agent_instance", actor_id, 1)
    if (actor is None or actor["body"].get("mission_id") != body.get("mission_id")
            or actor["body"].get("run_id") != run_id
            or not _valid_run(store, body.get("mission_id"), run_id)):
        return None
    return run_id


def _stage_attributes(store: StateStore, receipt: dict) -> dict[str, int]:
    call_ref = receipt["body"].get("call_ref")
    if not isinstance(call_ref, dict):
        return {}
    budget = store.get("mission_call_budget", call_ref.get("id", ""))
    stage = budget["body"].get("request_stage_token_estimate") if budget else None
    if (budget is None
            or budget["body"].get("mission_id") != receipt["body"].get("mission_id")
            or not isinstance(stage, dict)
            or stage.get("intent_ref") != receipt["body"].get("intent_ref")
            or stage.get("request_sha256") != receipt["body"].get("request_sha256")):
        return {}
    result = {}
    final = _nonnegative_int(stage.get("final_input_tokens"))
    if final is not None:
        result["osra.estimated.input_tokens"] = final
    headroom = _nonnegative_int(stage.get("rate_input_headroom_tokens"))
    if headroom is not None:
        result["osra.estimated.rate_input_headroom_tokens"] = headroom
    rows = stage.get("stages")
    if not isinstance(rows, list):
        return result
    totals: dict[str, int] = {}
    for row in rows:
        if not isinstance(row, dict) or row.get("kind") not in _STAGE_KINDS:
            continue
        marginal = _nonnegative_int(row.get("marginal_tokens"))
        if marginal is not None:
            kind = row["kind"]
            totals[kind] = totals.get(kind, 0) + marginal
    for kind, value in totals.items():
        result[f"osra.estimated.stage.{kind}_tokens"] = value
    return result


def _receipt_attributes(store: StateStore, receipt: dict, run_id: str) -> dict:
    body = receipt["body"]
    attributes: dict[str, str | int | float] = {
        "langsmith.span.kind": "llm",
        "gen_ai.operation.name": "chat",
        "osra.mission_id_hash": _hash(body["mission_id"]),
        "osra.run_id_hash": _hash(run_id),
        "osra.task_id_hash": _hash(body["task_id"]),
        "osra.attempt_id_hash": _hash(body["attempt_id"]),
        "osra.call_id_hash": _hash(body["call_ref"]["id"]),
        "osra.outcome.status": _safe_label(body.get("status")),
        "osra.model.role": "research",
    }
    request_hash = body.get("request_sha256")
    if isinstance(request_hash, str) and re.fullmatch(r"[a-f0-9]{64}", request_hash):
        attributes["osra.request_sha256"] = request_hash
    call = store.get(**body["call_ref"])
    if call and call["kind"] == "agent_call":
        provider = call["body"].get("provider")
        if _safe_label(provider) != "other":
            attributes["gen_ai.system"] = provider
            attributes["gen_ai.provider.name"] = provider
        requested_model = _safe_model(call["body"].get("model_id"))
        if requested_model is not None:
            attributes["gen_ai.request.model"] = requested_model
    model = _safe_model(body.get("returned_model"))
    if model is not None:
        attributes["gen_ai.response.model"] = model
    attributes.update(_usage_attributes(body))
    attributes.update(_stage_attributes(store, receipt))
    measurements = read_token_measurement(body)
    for key, attribute in (
        ("provider_admission_tokens", "osra.admission.requested_tokens"),
        ("provider_admission_limit_tokens", "osra.admission.limit_tokens"),
    ):
        if key in measurements:
            attributes[attribute] = measurements[key]
    return attributes


def _usage_attributes(body: dict) -> dict:
    attributes: dict[str, str | int | float] = {}
    usage = body.get("usage")
    usage = usage if isinstance(usage, dict) else {}
    missing_status = ("unknown" if not usage and body.get("status") != "completed"
                      else "not_reported")
    for source, target, label in (
        ("prompt_tokens", "gen_ai.usage.input_tokens", "input_tokens"),
        ("completion_tokens", "gen_ai.usage.output_tokens", "output_tokens"),
        ("total_tokens", "gen_ai.usage.total_tokens", "total_tokens"),
    ):
        value = _nonnegative_int(usage.get(source))
        attributes[f"osra.usage.{label}.status"] = (
            "reported" if value is not None else missing_status
        )
        if value is not None:
            attributes[target] = value
    for section, detail_key, target, label in (
        ("completion_tokens_details", "reasoning_tokens", "gen_ai.usage.reasoning.output_tokens", "reasoning"),
    ):
        detail = usage.get(section)
        value = _nonnegative_int(detail.get(detail_key)) if isinstance(detail, dict) else None
        attributes[f"osra.usage.{label}.status"] = (
            "reported" if value is not None else missing_status
        )
        if value is not None:
            attributes[target] = value
    observed = read_cache_observation(body)
    attributes["osra.cache.observation"] = observed["status"]
    if observed["status"] in {"reported_hit", "reported_zero"}:
        attributes["gen_ai.usage.cache_read.input_tokens"] = observed["cached_tokens"]
        attributes["osra.usage.cache_read.status"] = "reported"
    else:
        attributes["osra.usage.cache_read.status"] = {
            "not_reported": "not_reported", "malformed": "invalid",
            "unavailable": "unknown", "legacy_uncertain": "legacy_uncertain",
        }[observed["status"]]
    wall = _nonnegative_number((body.get("timings") or {}).get("wall_seconds", body.get("wall_seconds")))
    if wall is not None:
        attributes["osra.observed.wall_seconds"] = wall
    http = _nonnegative_int(body.get("http_status"))
    if http is not None:
        attributes["osra.http_status"] = http
    return attributes


def _figure_attributes(store: StateStore, receipt: dict, run_id: str) -> dict:
    body = receipt["body"]
    intent = store.get(**body["intent_ref"])
    attributes = {
        "langsmith.span.kind": "llm", "gen_ai.operation.name": "chat",
        "gen_ai.system": "groq", "gen_ai.provider.name": "groq",
        "osra.model.role": "figure",
        "osra.mission_id_hash": _hash(body["mission_id"]),
        "osra.run_id_hash": _hash(run_id),
        "osra.task_id_hash": _hash(body["task_id"]),
        "osra.outcome.status": _safe_label(body.get("status")),
        **_usage_attributes(body),
    }
    for key, value in (("gen_ai.request.model", intent["body"].get("model")),
                       ("gen_ai.response.model", body.get("model"))):
        model = _safe_model(value)
        if model is not None:
            attributes[key] = model
    request_hash = body.get("request_sha256")
    if isinstance(request_hash, str) and re.fullmatch(r"[a-f0-9]{64}", request_hash):
        attributes["osra.request_sha256"] = request_hash
    return attributes


def _trace_attributes(record: dict) -> dict:
    body = record["body"]
    attributes: dict[str, str | int | float] = {
        "langsmith.span.kind": "chain",
        "openinference.span.kind": "CHAIN",
        "osra.mission_id_hash": _hash(body["mission_id"]),
        "osra.run_id_hash": _hash(body["run_id"]),
        "osra.task_id_hash": _hash(body["task_id"]),
        "osra.attempt_id_hash": _hash(body["attempt_id"]),
        "osra.event.category": _safe_label(body.get("category")),
        "osra.event.status": _safe_label(body.get("status")),
    }
    if isinstance(body.get("call_id"), str):
        attributes["osra.call_id_hash"] = _hash(body["call_id"])
    return attributes


def _summary_attributes(metrics: dict, status: object) -> dict[str, str | int | float]:
    """Aggregate only reported subsets; custom keys avoid duplicate LLM usage."""
    usage = metrics["run_usage"]
    activity = metrics["run_activity"]
    attributes: dict[str, str | int | float] = {
        "langsmith.span.kind": "chain",
        "openinference.span.kind": "CHAIN",
        "osra.mission_id_hash": _hash(metrics["mission_id"]),
        "osra.run_id_hash": _hash(metrics["run_id"]),
        "osra.summary.status": _safe_label(status),
        "osra.summary.call_count_basis": "saved_request_intents",
        "osra.summary.cache_coverage_basis": "saved_request_intents",
    }
    for key in ("call_count", "model_call_count", "figure_call_count",
                "model_dispatch_recorded_count", "model_captured_response_count",
                "prepared_only_count", "captured_call_count",
                "error_or_unresolved_count"):
        attributes[f"osra.summary.{key}"] = activity[key]
    elapsed = _nonnegative_number(activity.get("observed_elapsed_seconds"))
    if elapsed is not None:
        attributes["osra.summary.observed_elapsed_seconds"] = elapsed
    for source, target in (
        ("prompt_tokens", "input_tokens"),
        ("completion_tokens", "output_tokens"),
        ("total_tokens", "total_tokens"),
        ("cache_read", "cache_read_tokens"),
        ("reasoning", "reasoning_output_tokens"),
    ):
        entry = usage[source]
        if entry["reported_calls"]:
            attributes[f"osra.summary.{target}"] = entry["tokens"]
    attributes["osra.summary.input_reported_calls"] = usage["prompt_tokens"]["reported_calls"]
    attributes["osra.summary.output_reported_calls"] = usage["completion_tokens"]["reported_calls"]
    peak = usage.get("peak_reported_input_tokens")
    if peak is not None:
        attributes["osra.summary.peak_reported_input_tokens"] = peak
    estimated_peak = metrics["model"]["stage_estimate"].get("peak_input_tokens")
    if estimated_peak is not None:
        attributes["osra.summary.peak_estimated_input_tokens"] = estimated_peak
    cache = usage["cache_fraction"]
    confirmed_cache = usage["cache_fraction_confirmed"]
    attributes["osra.summary.cache_covered_calls"] = cache["covered_calls"]
    attributes["osra.summary.cache_total_calls"] = cache["total_calls"]
    attributes["osra.summary.cache_covered_input_tokens"] = cache["covered_input_tokens"]
    attributes["osra.summary.cache_coverage_status"] = cache["coverage_status"]
    attributes["osra.summary.cache_hit_calls"] = cache["reported_hit_calls"]
    attributes["osra.summary.cache_miss_calls"] = cache["reported_miss_calls"]
    attributes["osra.summary.cache_confirmed_status"] = confirmed_cache["status"]
    attributes["osra.summary.cache_confirmed_input_calls"] = confirmed_cache[
        "input_reported_calls"
    ]
    for label in ("invalid", "not_reported", "unknown", "legacy_uncertain"):
        attributes[f"osra.summary.cache_{label}_calls"] = usage["cache_read"][f"{label}_calls"]
    retrieval = metrics["retrieval"]
    for key in ("recorded_attempt_count", "recorded_delivery_count"):
        attributes[f"osra.summary.retrieval_{key}"] = retrieval[key]
    attributes["osra.summary.retrieval_http_attempt_status"] = retrieval["http_attempt_status"]
    for outcome, count in retrieval["outcome_counts"].items():
        key = f"osra.summary.retrieval_outcome.{_safe_label(outcome)}"
        attributes[key] = attributes.get(key, 0) + count
    token_mix = usage["token_mix"]
    attributes["osra.summary.token_mix_covered_calls"] = token_mix["covered_calls"]
    reasoning = usage["reasoning_fraction"]
    attributes["osra.summary.reasoning_covered_calls"] = reasoning["covered_calls"]
    attributes["osra.summary.reasoning_total_calls"] = reasoning["total_calls"]
    for key, value in (
        ("input_percent", token_mix["input_fraction"]),
        ("output_percent", token_mix["output_fraction"]),
        ("cache_read_percent", cache["value"]),
        ("cache_confirmed_percent", confirmed_cache["value"]),
        ("cache_input_coverage_percent", cache["eligible_input_coverage_fraction"]),
        ("reasoning_output_percent", reasoning["value"]),
    ):
        if value is not None and 0 <= value <= 1:
            attributes[f"osra.summary.{key}"] = round(value * 100, 3)
    attributes["osra.summary.exact_ref_reuse_occurrences"] = metrics["context"][
        "reused_exact_ref_occurrences"
    ]
    attributes["osra.summary.missing_exact_refs"] = metrics["context"]["missing_exact_refs"]
    evaluation = metrics.get("evaluation")
    if evaluation is not None:
        attributes.update({
            "osra.summary.evaluation_outcome": evaluation["outcome"],
            "osra.summary.evaluation_observation_stopped": True,
            "osra.summary.evaluation_basis": evaluation["basis"],
            "osra.summary.evaluation_model_http_attempts": evaluation["model_http_attempts"],
            "osra.summary.evaluation_retrieval_http_attempts": evaluation["retrieval_http_attempts"],
            "osra.summary.evaluation_elapsed_seconds": evaluation["elapsed_seconds"],
        })
    attributes.update(_local_reuse_attributes(metrics.get("local_prefix_reuse")))
    return attributes


def _create_export(store: StateStore, source: dict, run_id: str) -> bool:
    source_ref = _ref(source)
    trace_id, root_id, span_id = _ids(run_id, source_ref)
    is_model = source["kind"] == "model_response_receipt"
    is_figure = source["kind"] == "figure_response_receipt"
    body = {
        "schema": EXPORT_SCHEMA,
        "mission_id": source["body"]["mission_id"],
        "run_id": run_id,
        "source_ref": source_ref,
        "status": "pending",
        "trace_id": trace_id,
        "span_id": span_id,
        "parent_span_id": root_id,
        "name": "osra.model.call" if is_model else "osra.figure.call" if is_figure else "osra.operation",
        "recorded_at": source["created_at"],
        "attributes": (_receipt_attributes(store, source, run_id) if is_model
                       else _figure_attributes(store, source, run_id) if is_figure
                       else _trace_attributes(source)),
    }
    export_id = "otel-export:" + _hash(canonical(source_ref))
    if store.get(EXPORT_KIND, export_id) is not None:
        return False

    def save(tx):
        existing = tx.get(EXPORT_KIND, export_id)
        if existing is not None:
            return {"created": False}
        if tx.get(**source_ref) is None:
            raise StateConflict("Export source changed before projection")
        tx.put(EXPORT_KIND, export_id, body)
        return {"created": True}

    result = store.transact(
        "otel-project:" + uuid.uuid4().hex, {"source_ref": source_ref}, save,
        mission_id=body["mission_id"],
    )
    return result["created"]


def _anchor(store: StateStore, mission_id: str, run_id: str) -> bool:
    run = store.get("mission_run", run_id, 1)
    if run is None or run["body"].get("mission_id") != mission_id:
        raise StateError("Trace anchor requires an exact mission run")
    trace_id, root_id, _ = _ids(run_id, {"kind": "run", "id": run_id, "version": 1})
    export_id = "otel-export-run:" + _hash(run_id)
    if store.get(EXPORT_KIND, export_id) is not None:
        return False
    name, identity = _run_identity(run)
    body = {
        "schema": EXPORT_SCHEMA, "mission_id": mission_id, "run_id": run_id,
        "source_ref": None, "status": "pending", "trace_id": trace_id,
        "span_id": root_id, "parent_span_id": None, "name": name,
        "recorded_at": run["created_at"],
        "attributes": {"langsmith.span.kind": "chain", "openinference.span.kind": "CHAIN",
                       "osra.mission_id_hash": _hash(mission_id),
                       "osra.run_id_hash": _hash(run_id),
                       "osra.projection.anchor": True, **identity},
    }

    def save(tx):
        if tx.get(EXPORT_KIND, export_id) is not None:
            return {"created": False}
        tx.put(EXPORT_KIND, export_id, body)
        return {"created": True}

    return store.transact("otel-project:" + uuid.uuid4().hex, {"run_id": run_id}, save,
                          mission_id=mission_id)["created"]


def project_run_summary(store: StateStore, mission_id: str, run_id: str, *,
                        activity_cache: ToolActivityCache | None = None) -> dict:
    """Save one local summary version per distinct receipt-based metric snapshot."""
    identifier(mission_id)
    identifier(run_id)
    if not _valid_run(store, mission_id, run_id):
        raise StateError("Requested trace run does not belong to this mission")
    metrics = build_context_metrics(store, run_id, include_calls=False)
    if metrics["mission_id"] != mission_id:
        raise StateError("Saved metrics and requested trace mission disagree")
    run = store.get("mission_run", run_id)
    attributes = _summary_attributes(metrics, run["body"].get("status"))
    if activity_cache is not None and type(activity_cache) is not ToolActivityCache:
        raise StateError("Tool activity cache must be a local observer cache")
    tool_activity = (activity_cache.get(store, mission_id, run_id)
                     if activity_cache is not None else build_tool_activity(store, run_id))
    if tool_activity["mission_id"] != mission_id:
        raise StateError("Saved tool activity and requested trace mission disagree")
    attributes.update(_tool_summary_attributes(tool_activity))
    evaluation = metrics.get("evaluation")
    if evaluation is not None:
        first_run = store.get("mission_run", run_id, 1)
        if first_run is None or first_run["body"].get("mission_id") != mission_id:
            raise StateError("Final report needs the exact first run version")
        duration = _nonnegative_number(evaluation.get("elapsed_seconds"))
        end_at = _window_end(first_run["created_at"], duration)
        if duration is None or end_at is None:
            raise StateError("Final report needs a bounded controller interval")
        _anchor_name, identity = _run_identity(first_run)
        title = (f"OSRA report · {identity['osra.run.label']} · "
                 f"{identity['osra.run.started_et']} · {identity['osra.run.short_ref']}")
        attributes.update(identity)
        attributes.update({
            "langsmith.trace.name": title,
            "osra.summary.final_started_at": first_run["created_at"],
            "osra.summary.observed_window_seconds": duration,
            "osra.summary.duration_basis": "saved_run_start_plus_evaluation_controller_elapsed",
            "osra.observed.wall_seconds": duration,
        })
        attributes["output.value"] = render_summary_output(attributes)
        digest = _hash(canonical(attributes))
        attributes["osra.summary.digest"] = digest
        export_id = "otel-export-final:" + _hash(run_id + ":" + digest)
        summary_ref = {"kind": EXPORT_KIND, "id": export_id, "version": 1}
        if store.get(EXPORT_KIND, export_id) is not None:
            return {"created": 0, "digest": digest, "summary_ref": summary_ref}
        body = {
            "schema": EXPORT_SCHEMA, "mission_id": mission_id, "run_id": run_id,
            "source_ref": None, "status": "pending",
            "trace_id": _hash("osra.otel.final.trace.v1:" + run_id + ":" + digest)[:32],
            "span_id": _hash("osra.otel.final.root.v1:" + run_id + ":" + digest)[:16],
            "parent_span_id": None, "name": "osra.run.final_report",
            "recorded_at": end_at, "attributes": attributes,
        }
        if not _safe_export_body(body):
            raise StateError("Final report failed outbound validation")

        def save_final(tx):
            if tx.get(EXPORT_KIND, export_id) is not None:
                return {"created": False}
            tx.put(EXPORT_KIND, export_id, body)
            return {"created": True}

        saved = store.transact("otel-project:" + uuid.uuid4().hex,
                              {"run_id": run_id, "digest": digest}, save_final,
                              mission_id=mission_id)
        return {"created": int(saved["created"]), "digest": digest,
                "summary_ref": summary_ref}
    attributes["output.value"] = render_summary_output(attributes)
    digest = _hash(canonical(attributes))
    attributes["osra.summary.digest"] = digest
    trace_id, root_id, span_id = _ids(run_id, {
        "kind": "run_summary", "id": run_id, "version": digest,
    })
    export_id = "otel-export-summary:" + _hash(run_id + ":" + digest)
    summary_ref = {"kind": EXPORT_KIND, "id": export_id, "version": 1}
    anchor_created = _anchor(store, mission_id, run_id)
    if store.get(EXPORT_KIND, export_id) is not None:
        return {"created": int(anchor_created), "digest": digest, "summary_ref": summary_ref}
    body = {
        "schema": EXPORT_SCHEMA, "mission_id": mission_id, "run_id": run_id,
        "source_ref": None, "status": "pending", "trace_id": trace_id,
        "span_id": span_id, "parent_span_id": root_id,
        "name": "osra.run.summary",
        "recorded_at": metrics["run_activity"].get("last_observed_at") or run["created_at"],
        "attributes": attributes,
    }

    def save(tx):
        if tx.get(EXPORT_KIND, export_id) is not None:
            return {"created": False}
        tx.put(EXPORT_KIND, export_id, body)
        return {"created": True}

    saved = store.transact("otel-project:" + uuid.uuid4().hex,
                           {"run_id": run_id, "digest": digest}, save,
                           mission_id=mission_id)
    return {"created": int(anchor_created) + int(saved["created"]),
            "digest": digest, "summary_ref": summary_ref}


def project_diagnostic_summary(store: StateStore, report: dict) -> dict:
    """Save one standalone trace from a recognized, sanitized API diagnostic.

    The caller supplies ``load_run_report(..., kind='api_diagnostic')`` output.
    This creates no mission run and never sends a network request.
    """
    if (not isinstance(report, dict) or report.get("schema") != "ora.local_run_report.v1"
            or report.get("kind") != "api_diagnostic"
            or report.get("mission_status") is not None
            or report.get("outcome_status") not in _DIAGNOSTIC_OUTCOMES
            or report.get("evidence_basis") != "recognized_direct_result_and_exact_http_ledger"):
        raise StateError("Unsupported diagnostic report")
    report_id = report.get("report_id")
    label = report.get("task_label")
    started_utc = report.get("started_at_utc")
    title = _diagnostic_title(label, started_utc, report_id)
    if title is None:
        raise StateError("Diagnostic identity is not a safe saved label and time")
    counts = report.get("counts")
    usage = report.get("usage")
    if not isinstance(counts, dict) or not isinstance(usage, dict):
        raise StateError("Diagnostic report lacks bounded counts and usage")

    def count(value: object) -> int:
        if type(value) is not int or not 0 <= value <= 1_000_000:
            raise StateError("Diagnostic count is outside the supported range")
        return value

    attempts = count(counts.get("model_http_attempts"))
    captured = count(counts.get("model_captured_responses"))
    unknown_usage = count(counts.get("model_usage_unknown_attempts"))
    retrieval = count(counts.get("retrieval_http_attempts"))
    if (captured > attempts or unknown_usage > attempts
            or counts.get("retrieval_recorded_attempts") is not None):
        raise StateError("Diagnostic attempt counts disagree")
    expected = ({"Cache wire probe": ("single_response_observed", 1, 1, 0),
                 "Synthetic exact-repeat check": ("incomplete_pair", 2, 1, 1)}
                .get(label))
    if expected != (report["outcome_status"], attempts, captured, unknown_usage) or retrieval:
        raise StateError("Diagnostic report is not a recognized direct API case")
    attributes: dict[str, str | int | float] = {
        "langsmith.span.kind": "chain", "openinference.span.kind": "CHAIN",
        "langsmith.trace.name": title,
        "osra.diagnostic.report_id": report_id,
        "osra.diagnostic.task_label": label,
        "osra.diagnostic.started_utc": started_utc,
        "osra.mission_id_hash": _hash(f"diagnostic:{report_id}"),
        "osra.run_id_hash": _hash(f"diagnostic:{report_id}"),
        "osra.summary.kind": "api_diagnostic",
        "osra.summary.diagnostic_outcome": report["outcome_status"],
        "osra.summary.diagnostic_model_http_attempts": attempts,
        "osra.summary.diagnostic_model_captured_responses": captured,
        "osra.summary.diagnostic_model_usage_unknown_attempts": unknown_usage,
        "osra.summary.diagnostic_retrieval_http_attempts": retrieval,
    }
    elapsed = report.get("elapsed_seconds")
    timing = report.get("timing")
    if (not isinstance(timing, dict)
            or timing.get("observed_window_basis") != "ledger_first_start_to_last_attempt_end"
            or timing.get("first_ledger_started_at_utc") != started_utc):
        raise StateError("Diagnostic observation window lacks exact ledger basis")
    measured = _nonnegative_number(timing.get("observed_window_seconds"))
    if (measured is None or _window_end(started_utc, measured) is None
            or elapsed is not None and (_nonnegative_number(elapsed) is None
                                        or abs(elapsed - measured) > 0.001)):
        raise StateError("Diagnostic observation window is invalid")
    attributes["osra.summary.diagnostic_elapsed_seconds"] = measured
    attributes["osra.summary.observed_window_seconds"] = measured
    attributes["osra.summary.duration_basis"] = "ledger_first_start_to_last_attempt_end"
    attributes["osra.observed.wall_seconds"] = measured
    for source, target in (
        ("measured_http_seconds_sum", "diagnostic_measured_http_seconds_sum"),
        ("between_model_attempt_gap_seconds_sum", "diagnostic_between_attempt_gap_seconds_sum"),
    ):
        value = timing.get(source)
        if value is not None:
            duration = _nonnegative_number(value)
            if duration is None:
                raise StateError("Diagnostic measured interval is invalid")
            attributes[f"osra.summary.{target}"] = duration
    for source, target in (("input", "input_tokens"), ("output", "output_tokens"),
                           ("reasoning", "reasoning_output_tokens"),
                           ("cache_read", "cache_read_tokens")):
        entry = usage.get(source)
        if not isinstance(entry, dict):
            raise StateError("Diagnostic usage field is absent")
        reported_calls = count(entry.get("reported_attempts"))
        reported_tokens = entry.get("reported_tokens")
        if reported_calls > attempts or (reported_tokens is None) != (reported_calls == 0):
            raise StateError("Diagnostic reported usage coverage disagrees")
        if reported_tokens is not None:
            if type(reported_tokens) is not int or not 0 <= reported_tokens <= 10**12:
                raise StateError("Diagnostic reported tokens are invalid")
            attributes[f"osra.summary.{target}"] = reported_tokens
        if source == "cache_read":
            for field in ("not_reported", "unknown", "invalid", "legacy_uncertain"):
                attributes[f"osra.summary.cache_{field}_calls"] = count(
                    entry.get(f"{field}_attempts"))
    rows = report.get("attempts")
    if not isinstance(rows, list) or len(rows) != attempts:
        raise StateError("Diagnostic attempt rows disagree with the ledger count")
    paired_input = paired_output = paired_count = 0
    for index, row in enumerate(rows, 1):
        if not isinstance(row, dict) or row.get("index") != index:
            raise StateError("Diagnostic attempt rows are out of order")
        input_tokens = row.get("input_tokens")
        output_tokens = row.get("output_tokens")
        if (input_tokens is None) != (output_tokens is None):
            raise StateError("Diagnostic paired token fields disagree")
        if input_tokens is None:
            continue
        if (type(input_tokens) is not int or type(output_tokens) is not int
                or not 0 <= input_tokens <= 10**12
                or not 0 <= output_tokens <= 10**12):
            raise StateError("Diagnostic paired tokens are invalid")
        paired_input += input_tokens
        paired_output += output_tokens
        paired_count += 1
    if (paired_count != captured
            or usage["input"].get("reported_attempts") != paired_count
            or usage["output"].get("reported_attempts") != paired_count
            or (paired_count and (attributes.get("osra.summary.input_tokens") != paired_input
                                  or attributes.get("osra.summary.output_tokens") != paired_output))):
        raise StateError("Diagnostic token totals disagree with captured attempts")
    if paired_count:
        attributes["osra.summary.total_tokens"] = paired_input + paired_output
    attributes["osra.summary.token_mix_covered_calls"] = paired_count
    cache = usage.get("cache_fraction_reported_subset")
    if not isinstance(cache, dict):
        raise StateError("Diagnostic cache coverage is absent")
    covered = count(cache.get("covered_calls"))
    total = count(cache.get("total_calls"))
    hits = count(cache.get("reported_hit_calls"))
    misses = count(cache.get("reported_miss_calls"))
    coverage = cache.get("coverage_status")
    if (total != attempts or covered > total or hits + misses != covered
            or coverage not in {"complete", "partial", "unavailable"}):
        raise StateError("Diagnostic cache coverage disagrees with attempts")
    attributes.update({
        "osra.summary.cache_covered_calls": covered,
        "osra.summary.cache_total_calls": total,
        "osra.summary.cache_coverage_status": coverage,
        "osra.summary.cache_hit_calls": hits,
        "osra.summary.cache_miss_calls": misses,
    })
    for source, target in (("input_fraction_reported_subset", "input_percent"),
                           ("output_fraction_reported_subset", "output_percent"),
                           ("reasoning_fraction_reported_subset", "reasoning_output_percent")):
        fraction = usage.get(source)
        if fraction is not None:
            if _nonnegative_number(fraction) is None or fraction > 1:
                raise StateError("Diagnostic token fraction is invalid")
            attributes[f"osra.summary.{target}"] = round(fraction * 100, 3)
    for source, target in (("value", "cache_read_percent"),
                           ("eligible_input_coverage_fraction", "cache_input_coverage_percent")):
        fraction = cache.get(source)
        if fraction is not None:
            if _nonnegative_number(fraction) is None or fraction > 1:
                raise StateError("Diagnostic cache fraction is invalid")
            attributes[f"osra.summary.{target}"] = round(fraction * 100, 3)
    attributes.update(_local_reuse_attributes(report.get("local_prefix_reuse")))
    attributes["output.value"] = render_summary_output(attributes)
    digest = _hash(canonical(attributes))
    attributes["osra.summary.digest"] = digest
    scoped_id = f"diagnostic:{report_id}"
    export_id = "otel-export-diagnostic-v2:" + _hash(report_id + ":" + digest)
    trace_id, span_id = _diagnostic_span_ids(report_id, digest)
    body = {
        "schema": EXPORT_SCHEMA, "mission_id": scoped_id, "run_id": scoped_id,
        "projection_revision": 2,
        "source_ref": None, "status": "pending",
        "trace_id": trace_id, "span_id": span_id,
        "parent_span_id": None, "name": "osra.diagnostic.summary",
        "recorded_at": _window_end(started_utc, measured), "attributes": attributes,
    }
    if not _safe_export_body(body):
        raise StateError("Diagnostic projection failed outbound validation")
    existing = store.get(EXPORT_KIND, export_id)
    if existing is not None:
        if (existing["body"].get("attributes") != attributes
                or not _safe_export_body(existing["body"])):
            raise StateConflict("Diagnostic report changed after projection")
        return {"created": 0, "mission_id": scoped_id,
                "export_ref": _ref(existing), "digest": digest}

    def save(tx):
        prior = tx.get(EXPORT_KIND, export_id)
        if prior is not None:
            if prior["body"].get("attributes") != attributes:
                raise StateConflict("Diagnostic projection changed concurrently")
            return {"created": False, "export_ref": _ref(prior)}
        saved = tx.put(EXPORT_KIND, export_id, body)
        return {"created": True, "export_ref": _ref(saved)}

    result = store.transact(
        "otel-project-diagnostic:" + report_id + ":" + digest,
        {"report_id": report_id, "digest": digest}, save, mission_id=scoped_id,
    )
    return {"created": int(result["created"]), "mission_id": scoped_id,
            "export_ref": result["export_ref"], "digest": digest}


def project_run(store: StateStore, mission_id: str, *, run_id: str | None = None,
                limit: int = 100, after_receipt_id: str = "",
                after_trace_id: str = "", after_figure_receipt_id: str = "") -> dict:
    """Save bounded export projections; no imports, credentials or network calls."""
    identifier(mission_id)
    if run_id is not None:
        identifier(run_id)
        if not _valid_run(store, mission_id, run_id):
            raise StateError("Requested trace run does not belong to this mission")
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise StateError("Projection page size is outside the allowed range")
    created = 0
    matched_runs: set[str] = set()
    receipts = store.records("model_response_receipt", limit=limit, after_id=after_receipt_id)
    figures = store.records("figure_response_receipt", limit=limit, after_id=after_figure_receipt_id)
    traces = store.records("coordination_trace", limit=limit, after_id=after_trace_id)
    for record in receipts:
        if record["body"].get("mission_id") != mission_id:
            continue
        resolved_run = _record_run(store, record)
        if not isinstance(resolved_run, str) or (run_id is not None and resolved_run != run_id):
            continue
        matched_runs.add(resolved_run)
        created += _create_export(store, record, resolved_run)
    for record in figures:
        if record["body"].get("mission_id") != mission_id:
            continue
        resolved_run = _figure_run(store, record)
        if resolved_run is None or run_id is not None and resolved_run != run_id:
            continue
        matched_runs.add(resolved_run)
        created += _create_export(store, record, resolved_run)
    for record in traces:
        body = record["body"]
        resolved_run = _trace_run(store, record)
        if (body.get("mission_id") != mission_id or not isinstance(resolved_run, str)
                or run_id is not None and resolved_run != run_id):
            continue
        matched_runs.add(resolved_run)
        created += _create_export(store, record, resolved_run)
    for matched_run in matched_runs:
        created += _anchor(store, mission_id, matched_run)
    return {
        "created": created,
        "scanned": len(receipts) + len(figures) + len(traces),
        "next_after_receipt_id": receipts[-1]["id"] if len(receipts) == limit else None,
        "next_after_figure_receipt_id": figures[-1]["id"] if len(figures) == limit else None,
        "next_after_trace_id": traces[-1]["id"] if len(traces) == limit else None,
    }


def _tool_export_records(store: StateStore, prefix: str) -> list[dict]:
    """Find revisions of one selected tool without reading unrelated export bodies."""
    db = store._connect()
    try:
        ids = [row[0] for row in db.execute(
            "SELECT id FROM heads WHERE kind=? AND id>=? AND id<? ORDER BY id LIMIT ?",
            (EXPORT_KIND, prefix, prefix + "\uffff", MAX_PAGE + 1),
        )]
    finally:
        db.close()
    if len(ids) > MAX_PAGE:
        raise StateError("Too many tool export revisions")
    return [store.get(EXPORT_KIND, record_id) for record_id in ids]


def _tool_export_body(step: dict, revision: int) -> dict:
    attributes = _tool_attributes(step, revision)
    run_id = step["run_id"]
    trace_id, root_id, _ = _ids(run_id, step["response_ref"])
    return {
        "schema": EXPORT_SCHEMA, "mission_id": step["mission_id"],
        "run_id": run_id, "source_ref": step["response_ref"],
        "status": "pending", "trace_id": trace_id,
        "span_id": _hash(
            f"osra.otel.tool.span.v1:{run_id}:{step['step_id']}:{revision}")[:16],
        "parent_span_id": root_id,
        "name": f"Loop {step['loop_index']} · {step['tool_name']}",
        "recorded_at": _tool_recorded_at(step), "attributes": attributes,
    }


def _refresh_pending_tool(store: StateStore, record: dict, activity: dict) -> dict | None:
    """Refresh a never-sent status snapshot; preserve any sending/uncertain fence."""
    body = record["body"]
    attributes = body["attributes"]
    step = next((item for item in activity.get("steps", ())
                 if item["step_id"] == attributes.get("osra.tool.step_id")), None)
    if step is None:
        return record  # The normal lineage check will reject the projection.
    if step.get("delivery_final") is not True:
        return None  # Wait for an actual later request or a bounded terminal observation.
    expected = _tool_export_body(step, attributes["osra.tool.projection_revision"])
    identity_keys = ("schema", "mission_id", "run_id", "source_ref", "trace_id",
                     "span_id", "parent_span_id", "name")
    if any(body.get(key) != expected[key] for key in identity_keys):
        return record
    if (body["attributes"] == expected["attributes"]
            and body["recorded_at"] == expected["recorded_at"]):
        return record

    def save(tx):
        current = tx.get(EXPORT_KIND, record["id"])
        if (current is None or current["version"] != record["version"]
                or current["body"].get("status") != "pending"):
            raise StateConflict("Tool projection changed before refresh")
        return tx.put(EXPORT_KIND, record["id"], expected,
                      expected_version=record["version"])

    try:
        return store.transact(
            "otel-refresh-tool:" + uuid.uuid4().hex,
            {"export_id": record["id"], "step_id": step["step_id"]}, save,
            mission_id=body["mission_id"],
        )
    except StateConflict:
        return None


def _project_tool_step(store: StateStore, step: dict) -> bool:
    prefix = f"otel-export-tool:{_hash(step['run_id'])[:16]}:{step['step_id']}:"
    prior = _tool_export_records(store, prefix)
    latest = prior[-1] if prior else None
    revision = int(prior[-1]["id"].removeprefix(prefix)) if prior else 1
    body = _tool_export_body(step, revision)
    if latest is not None:
        if (latest["body"].get("attributes") == body["attributes"]
                and latest["body"].get("recorded_at") == body["recorded_at"]):
            return False
        if latest["body"].get("status") in {"sending", "uncertain"}:
            return False  # A possibly delivered older snapshot must not be replayed.
        if latest["body"].get("status") == "sent":
            revision += 1
            body = _tool_export_body(step, revision)
            latest = None
    export_id = prefix + f"{revision:06d}"
    if not _safe_export_body(body):
        raise StateError("Tool activity failed outbound validation")

    def save(tx):
        response = tx.get(**step["response_ref"])
        if response is None:
            raise StateConflict("Tool response changed before projection")
        if latest is not None:
            current = tx.get(EXPORT_KIND, export_id)
            if current is None or current["version"] != latest["version"]:
                raise StateConflict("Tool projection changed concurrently")
            if current["body"].get("status") != "pending":
                return {"created": False}
            tx.put(EXPORT_KIND, export_id, body, expected_version=current["version"])
            return {"created": False}  # Refined a not-yet-sent projection.
        if tx.get(EXPORT_KIND, export_id) is not None:
            return {"created": False}
        tx.put(EXPORT_KIND, export_id, body)
        return {"created": True}

    return store.transact(
        "otel-project-tool:" + uuid.uuid4().hex,
        {"response_ref": step["response_ref"], "step_id": step["step_id"],
         "revision": revision}, save, mission_id=step["mission_id"],
    )["created"]


def project_tool_activity(store: StateStore, mission_id: str, run_id: str, *,
                          limit: int = 100, after_id: str = "",
                          activity_cache: ToolActivityCache | None = None) -> dict:
    """Project final, metadata-only selected-tool states as a separate bounded stream.

    A step waits until its delivery status is final for the saved observation.
    Previously sent snapshots can gain a new numbered status span; uncertain
    sends are never retried or silently superseded.
    """
    identifier(mission_id)
    identifier(run_id)
    if not _valid_run(store, mission_id, run_id):
        raise StateError("Requested trace run does not belong to this mission")
    if (type(limit) is not int or not 1 <= limit <= MAX_PAGE
            or not isinstance(after_id, str)
            or after_id and not re.fullmatch(r"[a-f0-9]{24}", after_id)):
        raise StateError("Tool activity page is invalid")
    if activity_cache is not None and type(activity_cache) is not ToolActivityCache:
        raise StateError("Tool activity cache must be a local observer cache")
    activity = (activity_cache.get(store, mission_id, run_id)
                if activity_cache is not None else build_tool_activity(store, run_id))
    if activity["mission_id"] != mission_id:
        raise StateError("Saved tool activity and requested trace mission disagree")
    steps = sorted((step for step in activity["steps"] if step["step_id"] > after_id),
                   key=lambda step: step["step_id"])
    page = steps[:limit]
    created = 0
    for step in page:
        if step.get("delivery_final") is not True:
            continue
        created += int(_anchor(store, mission_id, run_id))
        created += int(_project_tool_step(store, step))
    return {"created": created, "scanned": len(page),
            "next_after_id": page[-1]["step_id"] if len(page) == limit else None}


def _change_status(store: StateStore, record: dict, status: str, reason: str | None,
                   *, error_code: str | None = None,
                   http_status: int | None = None) -> dict:
    if error_code is not None:
        TransportError(error_code, http_status)  # Validate without storing raw errors.
    updated = {**record["body"], "status": status, "status_at": store.now()}
    if reason is not None:
        updated["reason"] = reason
    if error_code is not None:
        updated["error_code"] = error_code
    if http_status is not None:
        updated["http_status"] = http_status
    # Claim operations must be distinct: replaying one deterministic operation
    # would return its prior success to a competing sender of the same version.
    operation_id = f"otel-status:{uuid.uuid4().hex}"

    def save(tx):
        current = tx.get(EXPORT_KIND, record["id"])
        if current is None or current["version"] != record["version"]:
            raise StateConflict("Export status changed concurrently")
        saved = tx.put(EXPORT_KIND, record["id"], updated,
                       expected_version=record["version"])
        return {"version": saved["version"]}

    result = store.transact(operation_id, {"source": _ref(record), "status": status},
                            save, mission_id=record["body"]["mission_id"])
    return store.get(EXPORT_KIND, record["id"], result["version"])


def _timestamp_ns(value: str) -> int:
    return int(datetime.fromisoformat(value).timestamp() * 1_000_000_000)


def _send_otlp(config: ExportConfig, body: dict) -> bool:
    """Export one already saved span synchronously; optional packages load here only."""
    import requests
    from opentelemetry.context import Context
    from opentelemetry.exporter.otlp.proto.http.trace_exporter import Compression, OTLPSpanExporter
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SimpleSpanProcessor, SpanExportResult
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
    from opentelemetry.sdk.trace.id_generator import IdGenerator
    from opentelemetry.trace import (
        NonRecordingSpan,
        SpanContext,
        TraceFlags,
        TraceState,
        set_span_in_context,
    )

    class FixedIds(IdGenerator):
        def generate_trace_id(self) -> int:
            return int(body["trace_id"], 16)

        def generate_span_id(self) -> int:
            return int(body["span_id"], 16)

    class OneShotSession(requests.Session):
        """Refuse SDK retries, redirects and ambient headers for this explicit send."""

        def __init__(self) -> None:
            super().__init__()
            self.trust_env = False
            self.attempted = False
            self.failure: TransportError | None = None

        def request(self, method, url, **kwargs):
            if method != "POST" or url != config.endpoint or self.attempted:
                if self.failure is None:
                    self.failure = TransportError("configuration_error")
                raise self.failure
            self.attempted = True
            kwargs["allow_redirects"] = False
            kwargs["headers"] = {
                "Content-Type": "application/x-protobuf",
                "x-api-key": config.api_key,
                "Langsmith-Project": config.project,
            }
            try:
                response = super().request(method, url, **kwargs)
            except requests.Timeout as exc:
                self.failure = TransportError("timeout")
                raise self.failure from exc
            except requests.ConnectionError as exc:
                self.failure = TransportError("connection_lost")
                raise self.failure from exc
            except requests.RequestException as exc:
                self.failure = TransportError(
                    "configuration_error" if isinstance(exc, (
                        requests.exceptions.InvalidURL, requests.exceptions.InvalidSchema,
                        requests.exceptions.MissingSchema, requests.exceptions.InvalidHeader,
                    )) else "transport_error"
                )
                raise self.failure from exc
            except Exception as exc:
                self.failure = TransportError("transport_error")
                raise self.failure from exc
            if not 200 <= response.status_code < 300:
                self.failure = TransportError(
                    "redirect_rejected" if 300 <= response.status_code < 400
                    else "http_rejected", response.status_code,
                )
                response.close()
                raise self.failure
            return response

    provider = TracerProvider(id_generator=FixedIds(), resource=Resource({
        "service.name": "osra-research",
    }))
    capture = InMemorySpanExporter()
    provider.add_span_processor(SimpleSpanProcessor(capture))
    tracer = provider.get_tracer("ora.observability.export")
    context = Context()
    if body["parent_span_id"] is not None:
        parent = SpanContext(
            trace_id=int(body["trace_id"], 16),
            span_id=int(body["parent_span_id"], 16),
            is_remote=True, trace_flags=TraceFlags(TraceFlags.SAMPLED),
            trace_state=TraceState(),
        )
        context = set_span_in_context(NonRecordingSpan(parent), context)
    end_ns = _timestamp_ns(body["recorded_at"])
    wall = body["attributes"].get("osra.observed.wall_seconds")
    start_ns = end_ns - int(wall * 1_000_000_000) if wall is not None else end_ns
    span = tracer.start_span(body["name"], context=context, start_time=start_ns,
                             attributes=body["attributes"])
    span.end(end_time=end_ns)
    spans = capture.get_finished_spans()
    provider.shutdown()
    if len(spans) != 1:
        raise TransportError("sdk_export_failed")
    session = OneShotSession()
    try:
        exporter = OTLPSpanExporter(
            endpoint=config.endpoint,
            headers={"x-api-key": config.api_key,
                     "Langsmith-Project": config.project},
            timeout=config.timeout_seconds,
            compression=Compression.NoCompression,
            session=session,
        )
    except (TypeError, ValueError) as exc:
        session.close()
        raise TransportError("configuration_error") from exc
    try:
        try:
            result = exporter.export(spans)
        except Exception as exc:
            raise session.failure or TransportError("sdk_export_failed") from exc
        if result is not SpanExportResult.SUCCESS:
            raise session.failure or TransportError("sdk_export_failed")
        return True
    finally:
        exporter.shutdown()


def export_pending(store: StateStore, config: ExportConfig | None = None, *,
                   sender: Callable[[ExportConfig, dict], bool] | None = None,
                   mission_id: str | None = None, run_id: str | None = None, limit: int = 25,
                   after_id: str = "", stale_after_seconds: float = 60.0,
                   activity_cache: ToolActivityCache | None = None) -> dict:
    """Send explicitly enabled pending spans; failed/unknown sends stay uncertain.

    A stale ``sending`` record can be marked uncertain after a restart, but is
    never resent automatically. The caller may page results with ``next_after_id``.
    """
    config = (config or ExportConfig.from_env()).validated()
    if activity_cache is not None and type(activity_cache) is not ToolActivityCache:
        raise StateError("Tool activity cache must be a local observer cache")
    if not config.enabled:
        return {"status": "disabled", "sent": 0, "uncertain": 0,
                "next_after_id": None}
    if mission_id is not None:
        identifier(mission_id)
    if run_id is not None:
        identifier(run_id)
        run = store.get("mission_run", run_id)
        if run is None or (mission_id is not None and run["body"].get("mission_id") != mission_id):
            raise StateError("Requested trace run does not belong to this mission")
        mission_id = run["body"].get("mission_id")
    if type(limit) is not int or not 1 <= limit <= MAX_PAGE:
        raise StateError("Export page size is outside the allowed range")
    if (type(stale_after_seconds) not in {int, float}
            or not math.isfinite(stale_after_seconds)
            or stale_after_seconds < config.timeout_seconds):
        raise StateError("Stale-send interval must exceed the transport timeout")
    send = sender or _send_otlp
    records = store.records(EXPORT_KIND, limit=limit, after_id=after_id)
    sent = uncertain = 0
    tool_activity_by_run: dict[tuple[str, str], dict] = {}
    for record in records:
        body = record["body"]
        if (body.get("schema") != EXPORT_SCHEMA
                or mission_id is not None and body.get("mission_id") != mission_id
                or run_id is not None and body.get("run_id") != run_id):
            continue
        if body["status"] == "sending":
            status_at = body.get("status_at")
            try:
                age = (datetime.fromisoformat(store.now())
                       - datetime.fromisoformat(status_at)).total_seconds()
            except (TypeError, ValueError):
                age = stale_after_seconds
            if age >= stale_after_seconds:
                try:
                    _change_status(store, record, "uncertain", "interrupted_export")
                    uncertain += 1
                except StateConflict:
                    pass
            continue
        if body["status"] != "pending":
            continue
        if _safe_export_body(body) and str(body.get("name", "")).startswith("Loop "):
            key = (body["mission_id"], body["run_id"])
            if key not in tool_activity_by_run:
                try:
                    tool_activity_by_run[key] = (
                        activity_cache.get(store, key[0], key[1])
                        if activity_cache is not None
                        else build_tool_activity(store, body["run_id"])
                    )
                except (StateError, KeyError, TypeError, ValueError):
                    tool_activity_by_run[key] = {}
            refreshed = _refresh_pending_tool(store, record, tool_activity_by_run[key])
            if refreshed is None:
                continue
            record, body = refreshed, refreshed["body"]
        if (not _safe_export_body(body) or not _matches_saved_run_label(store, body)
                or not _matches_tool_lineage(
                    store, body,
                    tool_activity_by_run.get((body.get("mission_id"), body.get("run_id"))),
                )):
            try:
                _change_status(store, record, "uncertain", "invalid_projection")
                uncertain += 1
            except StateConflict:
                pass
            continue
        try:
            claimed = _change_status(store, record, "sending", None)
        except StateConflict:
            continue
        failure: TransportError | None = None
        try:
            accepted = send(config, claimed["body"])
        except TransportError as exc:
            accepted = False
            failure = exc
        except ImportError:
            accepted = False
            failure = TransportError("dependency_unavailable")
        except Exception:
            accepted = False
            failure = TransportError("sender_error")
        try:
            _change_status(
                store, claimed, "sent" if accepted is True else "uncertain",
                "acknowledged" if accepted is True else (
                    failure.error_code if failure else "acknowledgement_unknown"
                ),
                error_code=failure.error_code if failure else None,
                http_status=failure.http_status if failure else None,
            )
        except StateConflict:
            # A stale-send reconciler may have marked the send uncertain.
            continue
        if accepted is True:
            sent += 1
        else:
            uncertain += 1
    return {"status": "processed", "sent": sent, "uncertain": uncertain,
            "next_after_id": records[-1]["id"] if len(records) == limit else None}
