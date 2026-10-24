"""Observe durable executor progress without blocking the mission supervisor.

One background asyncio loop runs the existing executor.  Its capacity scheduler,
claims, result acceptance and uncertain-effect records remain the authority.
This driver only starts a per-mission drain and reads exact work revisions.
"""

from __future__ import annotations

import asyncio
import math
import threading
import time
from concurrent.futures import Future
from concurrent.futures import TimeoutError as FutureTimeoutError

from ora.coordination.board import ref
from ora.coordination.executor import ConcurrentExecutor


class DispatchError(RuntimeError):
    """A background executor drain failed; its durable records need inspection."""


class MissionDispatchDriver:
    """Keep at most one executor drain active per mission; never choose work."""

    def __init__(
        self,
        executor: ConcurrentExecutor,
        *,
        poll_interval: float = 0.1,
        idle_retry_interval: float = 2.0,
    ) -> None:
        if (
            not math.isfinite(poll_interval)
            or poll_interval <= 0
            or not math.isfinite(idle_retry_interval)
            or idle_retry_interval <= 0
        ):
            raise ValueError("Dispatch polling intervals must be positive and finite")
        self.executor = executor
        self.poll_interval = poll_interval
        self.idle_retry_interval = idle_retry_interval
        self._loop = asyncio.new_event_loop()
        self._lock = threading.Lock()
        self._futures: dict[str, Future] = {}
        self._seen: dict[str, dict[str, tuple[int, str]]] = {}
        self._last_input: dict[str, tuple] = {}
        self._next_due: dict[str, float] = {}
        self._closed = False
        self._draining = False
        self._started = threading.Event()
        self._thread = threading.Thread(
            target=self._run_loop, name="ora-mission-dispatch", daemon=True
        )
        self._thread.start()
        if not self._started.wait(timeout=5):
            raise DispatchError("Dispatch event loop did not start")

    def _run_loop(self) -> None:
        asyncio.set_event_loop(self._loop)
        self._started.set()
        try:
            self._loop.run_forever()
        finally:
            self._loop.close()
            asyncio.set_event_loop(None)

    def poll(self, mission_id: str) -> dict:
        """Return newly terminal work refs immediately, even while a peer runs.

        ``next_wake`` is an epoch timestamp suitable for a host's existing wait.
        The host still advances task controls from saved executor records and
        submits newly chosen work through ``ConcurrentExecutor.submit``.
        """
        if not isinstance(mission_id, str) or not mission_id:
            raise ValueError("Dispatch polling needs a mission ID")
        with self._lock:
            if self._closed:
                raise DispatchError("Dispatch driver is closed")
            future = self._futures.get(mission_id)
            observed: list[dict] = []
            next_wake = None
            if future is not None and future.done():
                del self._futures[mission_id]
                try:
                    outcome = future.result()
                except Exception as exc:
                    raise DispatchError("Background executor drain failed") from exc
                observed = list(outcome["observed"])
                next_wake = outcome["next_wake"]
                self._next_due[mission_id] = (
                    max(time.time(), next_wake)
                    if next_wake is not None
                    else time.time() + self.idle_retry_interval
                )
                future = None

            work = self.executor._work(mission_id)
            seen = self._seen.setdefault(mission_id, {})
            terminal_refs = []
            for item in work:
                phase = item["body"]["phase"]
                marker = (item["version"], phase)
                if seen.get(item["id"]) != marker:
                    seen[item["id"]] = marker
                    if phase in {"completed", "result_unaccepted"}:
                        terminal_refs.append(ref(item))

            pending = [item for item in work if item["body"]["phase"] == "pending"]
            if pending:
                ready = self.executor.board.ready_work(mission_id)
                input_signature = (
                    tuple(sorted((item["id"], item["version"]) for item in pending)),
                    tuple(
                        sorted(
                            (item["proposal_ref"]["id"], item["proposal_ref"]["version"])
                            for item in ready
                        )
                    ),
                )
            else:
                input_signature = ()
            now = time.time()
            if (
                future is None
                and pending
                and not self._draining
                and (
                    input_signature != self._last_input.get(mission_id)
                    or now >= self._next_due.get(mission_id, 0)
                )
            ):
                future = asyncio.run_coroutine_threadsafe(
                    self.executor.run_ready(mission_id), self._loop
                )
                self._futures[mission_id] = future
                self._last_input[mission_id] = input_signature

            active = future is not None
            if active:
                next_wake = now + self.poll_interval
            elif pending:
                next_wake = self._next_due.get(mission_id, now + self.idle_retry_interval)
            return {
                "active": active,
                "completed_refs": terminal_refs,
                "observed": observed,
                "next_wake": next_wake,
            }

    def drain_active(self, *, timeout: float = 5.0) -> bool:
        """Stop scheduling new drains and await callbacks already in flight.

        The caller must stop creating new work first. False means an active
        drain did not settle in time; ``close`` will then record uncertainty.
        """
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Drain timeout must be positive and finite")
        with self._lock:
            if self._closed:
                raise DispatchError("Dispatch driver is closed")
            self._draining = True
            futures = list(self._futures.values())
        deadline = time.monotonic() + timeout
        for future in futures:
            try:
                future.result(timeout=max(0, deadline - time.monotonic()))
            except FutureTimeoutError:
                return False
            except Exception as exc:
                raise DispatchError("Background executor drain failed") from exc
        return True

    def close(self, *, timeout: float = 5.0) -> bool:
        """Cancel drains and await executor uncertainty recording before loop stop.

        A synchronous callback already dispatched through ``to_thread`` can
        continue outside the loop; the executor records its effect as uncertain.
        False means the loop did not quiesce within the bound and remains alive.
        """
        if not math.isfinite(timeout) or timeout <= 0:
            raise ValueError("Shutdown timeout must be positive and finite")
        with self._lock:
            self._closed = True
            if not self._thread.is_alive():
                return True

        async def quiesce() -> None:
            current = asyncio.current_task()
            tasks = [task for task in asyncio.all_tasks() if task is not current]
            for task in tasks:
                task.cancel()
            if tasks:
                await asyncio.gather(*tasks, return_exceptions=True)

        settled = asyncio.run_coroutine_threadsafe(quiesce(), self._loop)
        try:
            settled.result(timeout=timeout)
        except Exception:
            return False
        self._loop.call_soon_threadsafe(self._loop.stop)
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()
