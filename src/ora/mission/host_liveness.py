"""Private host heartbeat, separate from research state and model-visible context."""

from __future__ import annotations

import json
import math
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable


def _utc(timestamp: float) -> str | None:
    try:
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat(timespec="milliseconds")
    except (OverflowError, OSError, ValueError):
        return None


class HostLiveness:
    """Sample process and drive activity into one private append-only session file."""

    def __init__(
        self,
        state_dir: Path,
        *,
        mode: str,
        interval_seconds: float = 30.0,
        start_thread: bool = True,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        if (not isinstance(interval_seconds, (int, float))
                or not math.isfinite(interval_seconds) or interval_seconds <= 0):
            raise ValueError("Host heartbeat interval must be positive")
        self.session_id = f"host-{uuid.uuid4().hex}"
        self.pid = os.getpid()
        self.mode = mode
        self.path = Path(state_dir) / "host-liveness" / f"{self.session_id}.jsonl"
        self._clock = clock
        self._monotonic = monotonic
        self._interval_seconds = interval_seconds
        self._state_lock = threading.Lock()
        self._write_lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._fd: int | None = None
        self._write_error: dict | None = None
        self._runtime_error_class: str | None = None
        self._closed = False
        self._drive_count = 0
        self._completed_count = 0
        self._failed_count = 0
        self._active: dict[int, tuple[str | None, float, float]] = {}
        self._last_run_id: str | None = None
        self._last_started_run_id: str | None = None
        self._last_completed_run_id: str | None = None
        self._last_started_at: float | None = None
        self._last_completed_at: float | None = None
        self._last_duration_seconds: float | None = None
        self._last_changed: bool | None = None
        self._last_next_wake: float | None = None
        self._last_error_class: str | None = None
        try:
            directory = self.path.parent
            if directory.is_symlink():
                raise OSError("Host liveness directory must not be a symlink")
            directory.mkdir(parents=True, exist_ok=True, mode=0o700)
            directory.chmod(0o700)
            self._fd = os.open(
                self.path,
                os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_EXCL
                | getattr(os, "O_NOFOLLOW", 0),
                0o600,
            )
        except OSError as exc:
            self._record_write_error(exc)
            return
        try:
            self._emit("started")
        except Exception as exc:
            self.record_runtime_error(exc)
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None
            return
        if start_thread and self._fd is not None:
            self._thread = threading.Thread(
                target=self._heartbeat_loop, name="ora-host-liveness", daemon=True,
            )
            try:
                self._thread.start()
            except RuntimeError as exc:
                self.record_runtime_error(exc)
                self._thread = None

    def _record_write_error(self, exc: OSError) -> None:
        self._write_error = {"class": type(exc).__name__, "errno": exc.errno}

    def record_runtime_error(self, exc: Exception) -> None:
        self._runtime_error_class = type(exc).__name__

    def begin_drive(self, run_id: str | None) -> int:
        """Track an entry without touching disk or changing scheduler behavior."""
        with self._state_lock:
            self._drive_count += 1
            token = self._drive_count
            now = self._clock()
            self._active[token] = (run_id if isinstance(run_id, str) else None,
                                   now, self._monotonic())
            self._last_run_id = run_id if isinstance(run_id, str) else None
            self._last_started_run_id = self._last_run_id
            self._last_started_at = now
            return token

    def finish_drive(
        self,
        token: int,
        *,
        changed: bool | None = None,
        next_wake: float | None = None,
        error_class: str | None = None,
    ) -> None:
        with self._state_lock:
            active = self._active.pop(token, None)
            if active is None:
                return
            self._completed_count += 1
            self._last_run_id = active[0]
            self._last_completed_run_id = active[0]
            self._last_completed_at = self._clock()
            self._last_duration_seconds = round(max(0.0, self._monotonic() - active[2]), 3)
            self._last_changed = changed if type(changed) is bool else None
            self._last_next_wake = (
                float(next_wake) if isinstance(next_wake, (int, float))
                and math.isfinite(next_wake) and next_wake > 0 else None
            )
            self._last_error_class = error_class
            if error_class is not None:
                self._failed_count += 1

    def _snapshot(self, event: str) -> dict:
        now = self._clock()
        mono = self._monotonic()
        with self._state_lock:
            active = sorted(self._active.values(), key=lambda item: item[2])
            return {
                "schema": "ora.host_liveness.v1",
                "event": event,
                "session_id": self.session_id,
                "pid": self.pid,
                "mode": self.mode,
                "at_utc": _utc(now),
                "drive_count": self._drive_count,
                "drive_completed_count": self._completed_count,
                "drive_failed_count": self._failed_count,
                "active_drive_count": len(active),
                "active_run_ids": [item[0] for item in active[:8] if item[0] is not None],
                "oldest_active_seconds": (
                    round(max(0.0, mono - active[0][2]), 3) if active else None
                ),
                "last_run_id": self._last_run_id,
                "last_drive_started_run_id": self._last_started_run_id,
                "last_drive_completed_run_id": self._last_completed_run_id,
                "last_drive_started_utc": (
                    _utc(self._last_started_at) if self._last_started_at is not None else None
                ),
                "last_drive_completed_utc": (
                    _utc(self._last_completed_at) if self._last_completed_at is not None else None
                ),
                "last_drive_duration_seconds": self._last_duration_seconds,
                "last_drive_changed": self._last_changed,
                "last_next_wake_utc": (
                    _utc(self._last_next_wake) if self._last_next_wake is not None else None
                ),
                "last_drive_error_class": self._last_error_class,
            }

    def _emit(self, event: str, *, lock_timeout: float | None = None) -> dict:
        snapshot = self._snapshot(event)
        data = (json.dumps(snapshot, sort_keys=True, separators=(",", ":")) + "\n").encode()
        acquired = (self._write_lock.acquire() if lock_timeout is None
                    else self._write_lock.acquire(timeout=lock_timeout))
        if not acquired:
            self._runtime_error_class = "MonitorWriteBusy"
            return snapshot
        try:
            if self._fd is None:
                return snapshot
            try:
                remaining = memoryview(data)
                while remaining:
                    written = os.write(self._fd, remaining)
                    if written <= 0:
                        raise OSError("Host liveness write made no progress")
                    remaining = remaining[written:]
            except OSError as exc:
                self._record_write_error(exc)
                try:
                    os.close(self._fd)
                except OSError:
                    pass
                self._fd = None
        finally:
            self._write_lock.release()
        return snapshot

    def sample(self) -> dict:
        """Write one heartbeat; tests can call this with an injected clock."""
        return self._emit("heartbeat")

    def _heartbeat_loop(self) -> None:
        while not self._stop.wait(self._interval_seconds):
            try:
                self.sample()
            except Exception as exc:
                self.record_runtime_error(exc)
                return
            if self._fd is None:
                return

    def status(self) -> dict:
        """Local-only monitor status, without exception text or research content."""
        return {
            "enabled": self._fd is not None,
            "session_id": self.session_id,
            "path": str(self.path),
            "write_error": self._write_error,
            "runtime_error_class": self._runtime_error_class,
            "heartbeat_thread_alive": self._thread is not None and self._thread.is_alive(),
        }

    def close(self) -> None:
        with self._state_lock:
            if self._closed:
                return
            self._closed = True
        self._stop.set()
        if self._thread is not None and self._thread is not threading.current_thread():
            self._thread.join(timeout=5)
        try:
            self._emit("stopped", lock_timeout=0.25)
        except Exception as exc:
            self.record_runtime_error(exc)
        if not self._write_lock.acquire(timeout=0.25):
            self._runtime_error_class = "MonitorWriteBusy"
            return
        try:
            if self._fd is not None:
                try:
                    os.close(self._fd)
                except OSError as exc:
                    self._record_write_error(exc)
                self._fd = None
        finally:
            self._write_lock.release()
