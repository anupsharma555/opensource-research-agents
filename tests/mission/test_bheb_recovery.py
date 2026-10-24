"""BHEB search recovery uses the real mission executor with injected transports."""

from __future__ import annotations

import json
import threading
import time

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.capacity import CapacityPolicy, QuotaLimit
from ora.credentials import GroqCredentialProfiles
from ora.mission import MissionService
from ora.models import GroqSelectedAdapter, ModelSession
from ora.retrieval.providers import HTTPResult
from ora.state import StateStore
from tests.mission.test_bheb_corpus import _host, _last_result
from tests.mission.test_service import ROOT, answer, drain, serialized_peer_name, tool
from tests.retrieval.test_bheb import _fixture, _response


def _until(host, run_id: str, predicate, *, seconds: float = 6) -> None:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        host._drive_run(host._run(run_id))
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("Mission did not reach the expected bounded state")


def _search_peer_transport(request, *, timeout):
    packet = json.loads(request.data)
    if serialized_peer_name(packet) == "Mxra":
        return answer("Independent synthetic sibling finding.", 100)
    seen = sum(item.get("name") == "request_capability" for item in packet["messages"])
    if seen == 0:
        return tool("request_capability", {"tool_id": "search_bheb_corpus"}, 1)
    if not any(item.get("name") == "search_bheb_corpus" for item in packet["messages"]):
        return tool("search_bheb_corpus", {"query": "fictional behavioral outcome"}, 2)
    return answer("The saved corpus lead was reviewed.", 3)


def _search_work(host, run_id: str):
    control = host._control(run_id, "agent-synthesis")
    work = host._work_for(control)
    if work is None or work["body"]["phase"] != "pending":
        return None
    spec = host.executor._spec_from(work)
    return (work, spec) if spec.kind == "retrieval" else None


def test_queued_cancellation_prevents_bheb_dispatch_and_sibling_finishes(
    tmp_path, monkeypatch,
):
    host, _, _ = _host(tmp_path, monkeypatch, source=False,
                       model_transport=_search_peer_transport)
    calls = []
    original_transport = host.bheb.transport
    host.bheb.transport = lambda *a, **k: (calls.append(1), original_transport(*a, **k))[1]
    original_admit = host.capacity.admit

    def hold_retrieval(*args, **kwargs):
        if len(args) >= 2 and args[1] == "retrieval":
            return None
        return original_admit(*args, **kwargs)

    host.capacity.admit = hold_retrieval
    try:
        run = host.start(LiveStartRequest(
            request_id="bheb-queued-cancel", question="Review two synthetic leads.",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        _until(host, run.run_id, lambda: _search_work(host, run.run_id) is not None)
        work, spec = _search_work(host, run.run_id)
        assert spec.kind == "retrieval"
        cap_id = host.executor._capacity_id(spec)
        _until(host, run.run_id, lambda: host.capacity.get(cap_id) is not None)
        assert host.capacity.get(cap_id)["phase"] == "queued"
        cancelled = host.capacity.cancel(cap_id, reason="branch_cancel")
        assert cancelled["phase"] == "canceled"
        host.capacity.admit = original_admit
        _until(host, run.run_id, lambda: host._control(
            run.run_id, "agent-critique")["body"]["phase"] == "done")
        for _ in range(10):
            host._drive_run(host._run(run.run_id))
        assert calls == []
        assert host.store.records("bheb_search") == []
        assert host.capacity.get(cap_id)["phase"] == "canceled"
        assert work["body"]["spec"]["work_id"] == spec.work_id
    finally:
        host.close()


def test_inflight_cancellation_keeps_captured_search_but_rejects_branch_output(
    tmp_path, monkeypatch,
):
    started, release = threading.Event(), threading.Event()
    host, _, _ = _host(tmp_path, monkeypatch, source=False,
                       model_transport=_search_peer_transport)
    original_transport = host.bheb.transport
    calls = []

    def blocked_transport(*args, **kwargs):
        calls.append(1)
        started.set()
        if not release.wait(timeout=5):
            raise TimeoutError("synthetic blocked BHEB transport")
        return original_transport(*args, **kwargs)

    host.bheb.transport = blocked_transport
    try:
        run = host.start(LiveStartRequest(
            request_id="bheb-inflight-cancel", question="Review two synthetic leads.",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        _until(host, run.run_id, started.is_set)
        control = host._control(run.run_id, "agent-synthesis")
        work = host._work_for(control)
        spec = host.executor._spec_from(work)
        cap_id = host.executor._capacity_id(spec)
        assert host.capacity.cancel(cap_id, reason="branch_cancel")["cancel_requested"] is True
        release.set()
        _until(host, run.run_id, lambda: bool(host.store.records("bheb_search")))
        _until(host, run.run_id, lambda: host.store.get("executor_work", work["id"])["body"]["phase"]
            in {"completed", "result_unaccepted"})
        _until(host, run.run_id, lambda: host._control(
            run.run_id, "agent-critique")["body"]["phase"] == "done")
        assert len(calls) == 1
        assert host.store.records("bheb_search_hit")
        assert host.capacity.get(cap_id)["cancel_requested"] is True
        work_phase = host.store.get("executor_work", work["id"])["body"]["phase"]
        assert host._control(run.run_id, "agent-synthesis")["body"]["source_refs"] == []
        accepted_search_results = [
            item for item in host.store.records("context_tool_result")
            if item["body"].get("name") == "search_bheb_corpus"
        ]
        assert not accepted_search_results, (work_phase, host.capacity.get(cap_id)["phase"])
    finally:
        release.set()
        host.close()


def test_completed_search_result_restarts_without_another_bheb_request(
    tmp_path, monkeypatch,
):
    packets = []

    def model_transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        if len(packets) == 1:
            return tool("request_capability", {"tool_id": "search_bheb_corpus"}, 1)
        if len(packets) == 2:
            return tool("search_bheb_corpus", {"query": "fictional outcome"}, 2)
        assert _last_result(packet, "search_bheb_corpus")["status"] == "completed"
        return answer("The saved lead was read after restart.", len(packets))

    host, _, _ = _host(tmp_path, monkeypatch, source=False,
                       model_transport=model_transport)
    calls = []
    original_transport = host.bheb.transport

    def counted(*args, **kwargs):
        calls.append(1)
        return original_transport(*args, **kwargs)

    host.bheb.transport = counted
    original_advance = host._advance_tool

    def hold_saved_search(control, work):
        if (control["body"].get("tool_call", {}).get("name") == "search_bheb_corpus"
                and host.store.records("bheb_search")):
            return False
        return original_advance(control, work)

    host._advance_tool = hold_saved_search
    try:
        run = host.start(LiveStartRequest(
            request_id="bheb-saved-restart", question="Review a fictional corpus lead.",
            agent_ids=["agent-synthesis"],
        ))
        _until(host, run.run_id, lambda: bool(host.store.records("bheb_search")))
        assert len(calls) == 1
    finally:
        host.close()

    state_path = tmp_path / "state"
    session = ModelSession(
        StateStore(state_path), ROOT,
        adapter=GroqSelectedAdapter(ROOT, transport=model_transport),
    )
    reopened = MissionService(
        ROOT, state_path, model_session=session, bheb_transport=counted,
        execution_mode="offline_test", start_supervisor=False,
    )
    try:
        drain(reopened, run.run_id, limit=20)
        assert reopened._run(run.run_id)["body"]["status"] == "completed"
        assert len(calls) == 1
        assert len(reopened.store.records("bheb_search")) == 1
    finally:
        reopened.close()


def test_one_retrieval_slot_serializes_two_peer_searches_and_releases_capacity(
    tmp_path, monkeypatch,
):
    corpus_dir = tmp_path / "synthetic-corpus"
    corpus_dir.mkdir()
    config, attrs, _ = _fixture(corpus_dir, monkeypatch, source=False)
    monkeypatch.setenv("OSRA_BHEB_CONFIG", str(config))
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-model-key")
    state_path = tmp_path / "state"
    model_counts = {"Sage": 0, "Mxra": 0}

    def model_transport(request, *, timeout):
        packet = json.loads(request.data)
        peer = serialized_peer_name(packet)
        model_counts[peer] += 1
        step = model_counts[peer]
        if step == 1:
            return tool("request_capability", {"tool_id": "search_bheb_corpus"},
                        100 if peer == "Mxra" else 1)
        if step == 2:
            return tool("search_bheb_corpus", {"query": f"fictional {peer} question"},
                        200 if peer == "Mxra" else 2)
        assert _last_result(packet, "search_bheb_corpus")["status"] == "completed"
        return answer("The peer's saved lead was reviewed.", 300 if peer == "Mxra" else 3)

    permits = {"model": 4, "retrieval": 1, "compute": 2}
    quotas = {scope: QuotaLimit(60) for scope in GroqCredentialProfiles(ROOT).scopes()}
    session = ModelSession(
        StateStore(state_path), ROOT,
        adapter=GroqSelectedAdapter(ROOT, transport=model_transport),
    )
    host = MissionService(
        ROOT, state_path, model_session=session,
        capacity_policy=CapacityPolicy(permits, quotas, review_reserve=1),
        bheb_transport=lambda *a, **k: _response(attrs),
        execution_mode="offline_test", start_supervisor=False,
    )
    started, release = threading.Event(), threading.Event()
    active, peak, calls = 0, 0, 0
    lock = threading.Lock()
    original_transport = host.bheb.transport

    def bounded_transport(*args, **kwargs):
        nonlocal active, peak, calls
        with lock:
            active += 1
            calls += 1
            peak = max(peak, active)
            first = calls == 1
        if first:
            started.set()
            if not release.wait(timeout=5):
                raise TimeoutError("synthetic first retrieval did not release")
        try:
            return original_transport(*args, **kwargs)
        finally:
            with lock:
                active -= 1

    host.bheb.transport = bounded_transport
    try:
        run = host.start(LiveStartRequest(
            request_id="bheb-one-retrieval-slot", question="Compare two fictional topics.",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        _until(host, run.run_id, started.is_set)
        _until(host, run.run_id, lambda: len([
            item for item in host.store.records("capacity_operation")
            if item["body"]["kind"] == "retrieval"
        ]) == 2)
        retrieval_ops = [item for item in host.store.records("capacity_operation")
                         if item["body"]["kind"] == "retrieval"]
        assert len(retrieval_ops) == 2 and calls == 1 and peak == 1
        assert any(item["body"]["phase"] == "queued" for item in retrieval_ops)
        release.set()
        drain(host, run.run_id, limit=25)
        assert host._run(run.run_id)["body"]["status"] == "completed"
        assert calls == 2 and peak == 1 and active == 0
        assert all(host.capacity.get(item["id"])["phase"] == "completed"
                   for item in retrieval_ops)
    finally:
        release.set()
        host.close()


def test_provider_429_waits_in_executor_then_retries_without_intermediate_model_turn(
    tmp_path, monkeypatch,
):
    packets = []
    calls = []

    def model_transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        if len(packets) == 1:
            return tool("request_capability", {"tool_id": "search_bheb_corpus"}, 1)
        if len(packets) == 2:
            return tool("search_bheb_corpus", {"query": "fictional rate recovery"}, 2)
        assert _last_result(packet, "search_bheb_corpus")["status"] == "completed"
        return answer("The bounded search completed after waiting.", len(packets))

    host, _, _ = _host(tmp_path, monkeypatch, source=False,
                       model_transport=model_transport)
    original_transport = host.bheb.transport

    def once_limited(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return HTTPResult(429, b"{}", {"retry-after": "1"})
        return original_transport(*args, **kwargs)

    host.bheb.transport = once_limited
    try:
        run = host.start(LiveStartRequest(
            request_id="bheb-rate-wait", question="Find a fictional research lead.",
            agent_ids=["agent-synthesis"],
        ))
        _until(host, run.run_id, lambda: len(calls) == 1 and any(
            item["body"].get("phase") == "waiting"
            for item in host.store.records("bheb_search")
        ))
        control = host._control(run.run_id, "agent-synthesis")
        work = host._work_for(control)
        cap_id = host.executor._capacity_id(host.executor._spec_from(work))
        _until(host, run.run_id, lambda: host.capacity.get(cap_id)["phase"] == "waiting")
        assert len(packets) == 2 and len(calls) == 1
        drain(host, run.run_id, limit=35)
        assert host._run(run.run_id)["body"]["status"] == "completed"
        assert len(calls) == 2
        assert len(packets) == 3
        assert len(host.store.records("bheb_search")) == 2
        assert host.capacity.get(cap_id)["phase"] == "completed"
    finally:
        host.close()


def test_repeated_429_exhaustion_returns_bounded_failure_to_model(
    tmp_path, monkeypatch,
):
    packets = []
    calls = []

    def model_transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        if len(packets) == 1:
            return tool("request_capability", {"tool_id": "search_bheb_corpus"}, 1)
        if len(packets) == 2:
            return tool("search_bheb_corpus", {"query": "fictional persistent rate limit"}, 2)
        result = _last_result(packet, "search_bheb_corpus")
        assert result["status"] == "failed"
        assert result["reason"] == "provider_rate_retry_exhausted"
        return answer("The corpus search was unavailable after bounded retries.", 3)

    host, _, _ = _host(tmp_path, monkeypatch, source=False,
                       model_transport=model_transport)

    def always_limited(*_args, **_kwargs):
        calls.append(1)
        return HTTPResult(429, b"{}", {"retry-after": "1"})

    host.bheb.transport = always_limited
    try:
        run = host.start(LiveStartRequest(
            request_id="bheb-repeated-429", question="Find a fictional research lead.",
            agent_ids=["agent-synthesis"],
        ))
        _until(host, run.run_id, lambda: len(packets) >= 3, seconds=10)
        drain(host, run.run_id, limit=15)
        assert host._run(run.run_id)["body"]["status"] == "completed"
        assert len(calls) == 3
        assert len(packets) == 3  # No extra model turn while either retry waited.
        assert len(host.store.records("bheb_search")) == 3
        for _ in range(5):
            host._drive_run(host._run(run.run_id))
        assert len(calls) == 3  # Exhaustion never launches a fourth HTTP attempt.
    finally:
        host.close()


@pytest.mark.parametrize(
    ("failure", "expected_status"),
    [
        ("missing_key", "unavailable"),
        ("http_401", "failed"),
        ("http_404", "failed"),
        ("http_503", "uncertain"),
        ("timeout", "uncertain"),
    ],
)
def test_bheb_failure_is_model_visible_and_local_continuation_remains_available(
    tmp_path, monkeypatch, failure, expected_status,
):
    packets = []
    calls = []
    host = None

    def model_transport(request, *, timeout):
        packet = json.loads(request.data)
        packets.append(packet)
        if len(packets) == 1:
            assert "search_bheb_corpus" in packet["messages"][0]["content"]
            return tool("request_capability", {"tool_id": "search_bheb_corpus"}, 1)
        if len(packets) == 2:
            assert "search_bheb_corpus" in {
                item["function"]["name"] for item in packet["tools"]
            }
            if failure == "missing_key":
                host.bheb.credential = ""  # Simulate key removal after selection.
            return tool("search_bheb_corpus", {"query": "fictional failure path"}, 2)
        result = _last_result(packet, "search_bheb_corpus")
        assert result["status"] == expected_status
        assert not result.get("hits")
        assert "calculate" in packet["messages"][0]["content"]
        return answer("The corpus was unavailable; the local task can still finish.", 3)

    host, _, _ = _host(tmp_path, monkeypatch, source=False,
                       model_transport=model_transport)

    def failed_transport(*_args, **_kwargs):
        calls.append(1)
        if failure == "timeout":
            raise TimeoutError("synthetic timeout")
        return HTTPResult(int(failure.removeprefix("http_")), b"{}", {})

    host.bheb.transport = failed_transport
    try:
        run = host.start(LiveStartRequest(
            request_id=f"bheb-{failure}-continuation",
            question="Review a fictional source under a provider failure.",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id, limit=20)
        assert len(packets) == 3
        assert host._run(run.run_id)["body"]["status"] == "completed"
        assert len(calls) == (0 if failure == "missing_key" else 1)
        assert host.store.records("source_copy") == []
    finally:
        host.close()
