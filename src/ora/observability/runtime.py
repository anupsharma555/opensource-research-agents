"""Opt-in, local-first observation of newly registered mission runs.

The mission host owns research execution. This worker only reads accepted state,
saves sanitized aggregate metrics, and sends already projected metadata spans.
Its binding survives restart without enrolling unrelated historical runs.
"""

from __future__ import annotations

import hashlib
import math
import threading
import time
from dataclasses import dataclass
from typing import Callable

from ora.state import StateStore
from ora.state.store import canonical

from . import export as export_module
from .export import (
    ExportConfig,
    ToolActivityCache,
    export_pending,
    project_run,
    project_tool_activity,
)
from .metrics import build_context_metrics

BINDING_KIND = "observability_run_binding"
SNAPSHOT_KIND = "observability_context_snapshot"
DIAGNOSTIC_KIND = "observability_runtime_diagnostic"
_END = "\U0010ffff"  # StateStore IDs sort before this; a complete scan resets on a new signal.


def _ref(record: dict) -> dict:
    return {key: record[key] for key in ("kind", "id", "version")}


def _hash(value: object) -> str:
    return hashlib.sha256(canonical(value).encode()).hexdigest()


@dataclass
class _Progress:
    receipt_cursor: str = ""
    figure_receipt_cursor: str = ""
    trace_cursor: str = ""
    tool_cursor: str = ""
    project_active: bool = True
    scan_signal: int = -1
    last_source_signal: int = -1
    last_summary_signal: int = -1
    export_cursor: str = ""
    export_active: bool = True
    last_export_scan: float = 0.0
    last_receipt_signal: int = -1


class RuntimeObserver:
    """Observe only explicit run bindings; never schedule research or model work."""

    def __init__(
        self, store: StateStore, config: ExportConfig, *,
        sender: Callable[[ExportConfig, dict], bool] | None = None,
        poll_interval: float = 2.0, page_size: int = 100,
        max_pages_per_wake: int = 2, start_worker: bool = True,
    ) -> None:
        if (type(poll_interval) not in {int, float} or not math.isfinite(poll_interval)
                or poll_interval < 0.1 or type(page_size) is not int
                or not 1 <= page_size <= 1000 or type(max_pages_per_wake) is not int
                or not 1 <= max_pages_per_wake <= 10):
            raise ValueError("Observer pacing and page limits must be bounded")
        self.store = store
        self.config = config.validated()
        self.sender = sender
        self.poll_interval = float(poll_interval)
        self.page_size = page_size
        self.max_pages_per_wake = max_pages_per_wake
        self._destination_hash = _hash([config.endpoint, config.project])
        db = store._connect()
        try:
            self._existing_run_rowid = db.execute(
                "SELECT COALESCE(MAX(rowid),0) FROM records WHERE kind='mission_run'"
            ).fetchone()[0]
        finally:
            db.close()
        self._lock = threading.RLock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._bindings: dict[str, str] = {}
        self._progress: dict[str, _Progress] = {}
        self._last_error_code: str | None = None
        self._cycles = 0
        self._export_outcomes = 0
        self._tool_activity_cache = ToolActivityCache()
        self._thread: threading.Thread | None = None
        if self.config.enabled:
            self._load_bindings()
            if start_worker:
                self._thread = threading.Thread(
                    target=self._work_loop, name="ora-observability", daemon=True,
                )
                self._thread.start()
                if self._bindings:
                    self._wake.set()

    def _load_bindings(self) -> None:
        after = ""
        while True:
            page = self.store.records(BINDING_KIND, limit=500, after_id=after)
            for saved in page:
                body = saved["body"]
                run_id = body.get("run_id")
                mission_id = body.get("mission_id")
                run = self.store.get("mission_run", run_id, 1) if isinstance(run_id, str) else None
                if (body.get("schema") == "ora.observability_run_binding.v1"
                        and body.get("destination_hash") == self._destination_hash
                        and isinstance(mission_id, str) and run is not None
                        and body.get("run_ref") == _ref(run)
                        and run["body"].get("mission_id") == mission_id):
                    self._bindings[run_id] = mission_id
                    self._progress[run_id] = _Progress()
            if len(page) < 500:
                return
            after = page[-1]["id"]

    def register(self, run_id: str, *, newly_created: bool) -> bool:
        """Bind a new run once; a reused run must already have a saved binding."""
        if not self.config.enabled or self._stop.is_set() or type(newly_created) is not bool:
            return False
        try:
            run = self.store.get("mission_run", run_id, 1)
            if run is None or not isinstance(run["body"].get("mission_id"), str):
                return False
            mission_id = run["body"]["mission_id"]
            binding_id = f"observability-run:{run_id}"
            existing = self.store.get(BINDING_KIND, binding_id)
            if existing is None and not newly_created:
                return False
            if existing is None:
                db = self.store._connect()
                try:
                    row = db.execute(
                        "SELECT rowid FROM records WHERE kind='mission_run' AND id=? AND version=1",
                        (run_id,),
                    ).fetchone()
                finally:
                    db.close()
                if row is None or row[0] <= self._existing_run_rowid:
                    return False  # A saved historical run was not opted in at creation.
            body = {
                "schema": "ora.observability_run_binding.v1",
                "mission_id": mission_id, "run_id": run_id,
                "run_ref": _ref(run), "destination_hash": self._destination_hash,
            }
            if existing is not None:
                if existing["body"] != body:
                    return False
            else:
                self.store.transact(
                    f"observability-bind:{run_id}", body,
                    lambda tx: {"binding_ref": _ref(tx.put(BINDING_KIND, binding_id, body))},
                    mission_id=mission_id,
                )
            with self._lock:
                self._bindings[run_id] = mission_id
                self._progress.setdefault(run_id, _Progress())
            self._wake.set()
            return True
        except Exception:
            self._last_error_code = "binding_failed"
            return False

    def wake(self, run_id: str | None = None) -> bool:
        """Nudge the worker after saved host progress without touching research state."""
        if not self.config.enabled or self._stop.is_set():
            return False
        with self._lock:
            if run_id is not None and run_id not in self._bindings:
                return False
        self._wake.set()
        return True

    def _signal(self, mission_id: str, *, receipts_only: bool = False) -> int:
        kinds = (("model_response_receipt", "figure_response_receipt",
                  "evaluation_observation") if receipts_only else
                 ("model_response_receipt", "figure_response_receipt", "coordination_trace",
                  "mission_run", "evaluation_observation", "context_tool_result"))
        placeholders = ",".join("?" for _ in kinds)
        db = self.store._connect()
        try:
            row = db.execute(
                "SELECT COALESCE(MAX(rowid),0) FROM records "
                f"WHERE (kind IN ({placeholders}) AND json_extract(body,'$.mission_id')=?) "
                "OR (kind='retrieval_attempt' "
                "AND json_extract(body,'$.request.mission_id')=?) "
                "OR (kind='retrieval_delivery' "
                "AND json_extract(body,'$.envelope.request.missionId')=?)",
                (*kinds, mission_id, mission_id, mission_id),
            ).fetchone()
            return row[0]
        finally:
            db.close()

    def _diagnostic(self, run_id: str, mission_id: str, code: str) -> None:
        self._last_error_code = code
        record_id = f"observability-runtime:{_hash([run_id, code])[:32]}"
        try:
            if self.store.get(DIAGNOSTIC_KIND, record_id) is None:
                body = {"schema": "ora.observability_runtime_diagnostic.v1",
                        "mission_id": mission_id, "run_id": run_id, "code": code}
                self.store.transact(
                    f"observability-diagnostic:{record_id}", body,
                    lambda tx: {"diagnostic_ref": _ref(tx.put(DIAGNOSTIC_KIND, record_id, body))},
                    mission_id=mission_id,
                )
        except Exception:
            pass  # A failing store cannot save its own observer diagnostic.

    def _snapshot(self, run_id: str, mission_id: str) -> bool:
        metrics = build_context_metrics(self.store, run_id, include_calls=False)
        digest = _hash(metrics)
        record_id = f"observability-context:{run_id}"
        current = self.store.get(SNAPSHOT_KIND, record_id)
        changed = current is None or current["body"].get("metrics_sha256") != digest
        if changed:
            body = {"schema": "ora.observability_context_snapshot.v1",
                    "mission_id": mission_id, "run_id": run_id,
                    "metrics_sha256": digest, "metrics": metrics}

            def save(tx):
                prior = tx.get(SNAPSHOT_KIND, record_id)
                if prior is not None and prior["body"].get("metrics_sha256") == digest:
                    return {"snapshot_ref": _ref(prior)}
                saved = tx.put(SNAPSHOT_KIND, record_id, body,
                               expected_version=prior["version"] if prior else 0)
                return {"snapshot_ref": _ref(saved)}

            self.store.transact(
                f"observability-context:{run_id}:{digest}",
                {"run_id": run_id, "metrics_sha256": digest}, save,
                mission_id=mission_id,
            )
        return changed

    def _summary(self, run_id: str, mission_id: str) -> bool:
        helper = getattr(export_module, "project_run_summary", None)
        if not callable(helper):
            return False
        return helper(self.store, mission_id, run_id,
                      activity_cache=self._tool_activity_cache).get("created", 0) > 0

    def _cycle_run(self, run_id: str, mission_id: str, progress: _Progress) -> dict:
        outcome = {"projected": 0, "sent": 0, "uncertain": 0, "snapshots": 0}
        source_signal = self._signal(mission_id)
        if source_signal != progress.last_summary_signal:
            try:
                if self._summary(run_id, mission_id):
                    progress.export_active = True
                    progress.export_cursor = ""
            except Exception:
                self._diagnostic(run_id, mission_id, "summary_failed")
            else:
                if callable(getattr(export_module, "project_run_summary", None)):
                    progress.last_summary_signal = source_signal
        if not progress.project_active and source_signal != progress.last_source_signal:
            progress.receipt_cursor = progress.figure_receipt_cursor = progress.trace_cursor = ""
            progress.tool_cursor = ""
            progress.project_active = True
            progress.scan_signal = source_signal
        if progress.project_active:
            if progress.scan_signal < 0:
                progress.scan_signal = source_signal
            for _ in range(self.max_pages_per_wake):
                if self._stop.is_set():
                    break
                result = project_run(
                    self.store, mission_id, run_id=run_id, limit=self.page_size,
                    after_receipt_id=progress.receipt_cursor,
                    after_figure_receipt_id=progress.figure_receipt_cursor,
                    after_trace_id=progress.trace_cursor,
                )
                outcome["projected"] += result["created"]
                progress.receipt_cursor = result["next_after_receipt_id"] or _END
                progress.figure_receipt_cursor = result["next_after_figure_receipt_id"] or _END
                progress.trace_cursor = result["next_after_trace_id"] or _END
                if progress.tool_cursor != _END:
                    tools = project_tool_activity(
                        self.store, mission_id, run_id, limit=self.page_size,
                        after_id=progress.tool_cursor,
                        activity_cache=self._tool_activity_cache,
                    )
                    outcome["projected"] += tools["created"]
                    progress.tool_cursor = tools["next_after_id"] or _END
                if (progress.receipt_cursor == progress.figure_receipt_cursor
                        == progress.trace_cursor == progress.tool_cursor == _END):
                    progress.project_active = False
                    progress.last_source_signal = progress.scan_signal
                    progress.scan_signal = -1
                    break
            if outcome["projected"]:
                progress.export_active = True
                progress.export_cursor = ""
        receipt_signal = self._signal(mission_id, receipts_only=True)
        if receipt_signal != progress.last_receipt_signal:
            try:
                if self._snapshot(run_id, mission_id):
                    outcome["snapshots"] += 1
                    progress.export_active = True
                    progress.export_cursor = ""
            except Exception:
                self._diagnostic(run_id, mission_id, "snapshot_failed")
            else:
                progress.last_receipt_signal = receipt_signal
        if time.monotonic() - progress.last_export_scan >= 60 and not progress.export_active:
            progress.export_active = True  # Revisit interrupted sends after the stale interval.
            progress.export_cursor = ""
        if progress.export_active and not self._stop.is_set():
            for _ in range(self.page_size):
                if self._stop.is_set() or outcome["sent"] + outcome["uncertain"] >= 2:
                    break
                result = export_pending(
                    self.store, self.config, sender=self.sender, mission_id=mission_id,
                    run_id=run_id,
                    limit=min(self.page_size, 2 - outcome["sent"] - outcome["uncertain"]),
                    after_id=progress.export_cursor,
                    activity_cache=self._tool_activity_cache,
                )
                outcome["sent"] += result["sent"]
                outcome["uncertain"] += result["uncertain"]
                progress.export_cursor = result["next_after_id"] or _END
                if progress.export_cursor == _END:
                    progress.export_active = False
                    progress.export_cursor = ""
                    progress.last_export_scan = time.monotonic()
                    break
        return outcome

    def process_once(self) -> dict:
        """One bounded observer tick, useful for offline tests and local inspection."""
        total = {"projected": 0, "sent": 0, "uncertain": 0, "snapshots": 0}
        if not self.config.enabled or self._stop.is_set():
            return total
        with self._lock:
            runs = list(self._bindings.items())
        for run_id, mission_id in runs:
            if self._stop.is_set():
                break
            progress = self._progress[run_id]
            try:
                result = self._cycle_run(run_id, mission_id, progress)
            except Exception:
                self._diagnostic(run_id, mission_id, "observer_tick_failed")
                continue
            for key in total:
                total[key] += result[key]
        self._cycles += 1
        self._export_outcomes += total["sent"] + total["uncertain"]
        return total

    def _pending_exports(self, run_id: str) -> int:
        db = self.store._connect()
        try:
            return db.execute(
                "SELECT COUNT(*) FROM records r JOIN heads h USING(kind,id,version) "
                "WHERE r.kind='observability_export' "
                "AND json_extract(r.body,'$.run_id')=? "
                "AND json_extract(r.body,'$.status') IN ('pending','sending')",
                (run_id,),
            ).fetchone()[0]
        finally:
            db.close()

    def _drained(self, run_ids: list[str]) -> bool:
        for run_id in run_ids:
            mission_id = self._bindings[run_id]
            progress = self._progress[run_id]
            if (progress.project_active or progress.export_active
                    or self._signal(mission_id) != progress.last_source_signal
                    or self._signal(mission_id, receipts_only=True)
                    != progress.last_receipt_signal
                    or callable(getattr(export_module, "project_run_summary", None))
                    and self._signal(mission_id) != progress.last_summary_signal
                    or self._pending_exports(run_id)):
                return False
        return True

    def flush(self, run_id: str | None = None, *, timeout_seconds: float = 5.0) -> bool:
        """Drain current saved metadata within a deadline; return False if work remains."""
        if type(timeout_seconds) not in {int, float} or not 0 <= timeout_seconds <= 5:
            return False
        if not self.config.enabled:
            return True
        with self._lock:
            if run_id is not None and run_id not in self._bindings:
                return False
            run_ids = [run_id] if run_id is not None else list(self._bindings)
            for selected in run_ids:
                progress = self._progress[selected]
                progress.export_active = True
                progress.export_cursor = ""
        deadline = time.monotonic() + timeout_seconds
        initial_outcomes = self._export_outcomes
        while not self._stop.is_set():
            if self._drained(run_ids):
                return True
            if (time.monotonic() >= deadline
                    or self._export_outcomes - initial_outcomes >= 10):
                return False
            if self._thread is None:
                self.process_once()
            else:
                self._wake.set()
                time.sleep(min(0.05, max(0, deadline - time.monotonic())))
            for selected in run_ids:
                progress = self._progress[selected]
                if not progress.export_active and self._pending_exports(selected):
                    progress.export_active = True
                    progress.export_cursor = ""
        return False

    def _work_loop(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(self.poll_interval)
            self._wake.clear()
            if not self._stop.is_set():
                self.process_once()

    def status(self) -> dict:
        with self._lock:
            alive = bool(self._thread and self._thread.is_alive())
            return {"enabled": self.config.enabled, "registered_runs": len(self._bindings),
                    "worker_alive": alive,
                    "worker_pending_after_close": bool(self._stop.is_set() and alive),
                    "cycles": self._cycles, "last_error_code": self._last_error_code}

    def close(self, timeout_seconds: float = 5.0) -> bool:
        """Stop new work within a bounded wait; a current HTTP send may remain uncertain."""
        if type(timeout_seconds) not in {int, float} or not 0 <= timeout_seconds <= 5:
            timeout_seconds = 5.0
        deadline = time.monotonic() + timeout_seconds
        if self.config.enabled:
            with self._lock:
                runs = list(self._bindings.items())
            for run_id, mission_id in runs:
                try:
                    self._snapshot(run_id, mission_id)
                    self._summary(run_id, mission_id)
                except Exception:
                    self._diagnostic(run_id, mission_id, "snapshot_failed")
        flushed = True
        if self.config.enabled and not self._stop.is_set():
            if self._thread is not None or self.sender is not None:
                flushed = self.flush(timeout_seconds=timeout_seconds)
            else:
                with self._lock:
                    run_ids = list(self._bindings)
                flushed = self._drained(run_ids)
        self._stop.set()
        self._wake.set()
        if self._thread and self._thread.is_alive():
            self._thread.join(max(0, deadline - time.monotonic()))
        quiesced = not self._thread or not self._thread.is_alive()
        return bool(quiesced and flushed)
