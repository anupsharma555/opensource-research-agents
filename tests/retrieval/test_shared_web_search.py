"""Opt-in physical web-query sharing with separate peer task views."""

import asyncio
import hashlib
import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace

import pytest

from ora.agents import AgentRegistry
from ora.coordination.board import ProposalBoard, ref
from ora.coordination.capacity import CapacityPolicy, CapacityScheduler
from ora.coordination.executor import ConcurrentExecutor, WorkSpec
from ora.coordination.mailbox import PeerMailbox
from ora.coordination.traces import TraceJournal
from ora.evidence.intake import IntakeService
from ora.evidence.library import EvidenceError, EvidenceLibrary
from ora.retrieval.providers import HTTPResult
from ora.retrieval.web_config import provider_policy
from ora.retrieval.web_search import WebSearch, identity
from ora.state import StateError, StateStore
from tests.coordination.test_executor import ROOT, foundation, propose
from tests.retrieval.test_web_search_reuse import _source_input


def _setup(tmp_path, transport, *, peers=2, clock=None, source_use_guard=None):
    quotas = provider_policy(CapacityPolicy({"model": 2, "retrieval": 6, "compute": 2}), {}).quotas
    store, board, _, executor, actors, configs = foundation(
        tmp_path, retrieval=6, quotas=quotas, clock=clock
    )
    if peers > len(actors):
        registry = AgentRegistry(store, ROOT)
        for index in range(len(actors), peers):
            actor = registry.register(
                f"extra-{index}",
                mission_id="mission",
                run_id="run",
                role_id="evidence_preparation_synthesis",
                display_name=f"Peer {index}",
            )["id"]
            config = store.transact(
                f"extra-config-{index}",
                {"actor": actor},
                lambda tx, actor=actor, index=index: tx.put(
                    "agent_configuration",
                    f"extra-config-{index}",
                    {
                        "mission_id": "mission",
                        "instance_id": actor,
                        "configuration_sha256": f"fixture-extra-{index}",
                    },
                ),
                mission_id="mission",
            )
            actors.append(actor)
            configs.append(ref(config))
    engine = WebSearch(
        store,
        executor,
        {"EXA_API_KEY": "synthetic-exa", "TAVILY_API_KEY": "synthetic-tavily"},
        transport=transport,
        shared_enabled=lambda spec: True,
        source_use_guard=source_use_guard,
    )
    specs = []
    for index in range(peers):
        parent = propose(board, actors[index], f"peer-{index}")
        specs.append(
            WorkSpec(
                work_id=f"search-{index}",
                mission_id="mission",
                run_id="run",
                proposal_ref=parent,
                actor_id=actors[index],
                configuration_ref=configs[index],
                input_refs=(),
                handler_id="mission-tool",
                handler_version="1",
                kind="compute",
                lane=actors[index],
                attempt_id=f"parent-{index}",
            )
        )
    return store, executor, engine, specs


def _response(query="query"):
    return HTTPResult(
        200,
        json.dumps(
            {
                "results": [
                    {
                        "id": query,
                        "url": "https://example.org/source",
                        "title": "Synthetic source",
                        "highlights": ["Candidate text, not inspected evidence."],
                    }
                ]
            }
        ).encode(),
        {},
    )


def _start(
    engine,
    spec,
    queries,
    *,
    need="Find evidence",
    refresh=False,
    provider="exa",
    limit=10,
    mode="research",
):
    return engine.start(
        spec,
        {
            "mode": mode,
            "information_need": need,
            "queries": queries,
            "provider": provider,
            "refresh": refresh,
            "limit": limit,
        },
    )


def test_two_peers_share_one_physical_query_and_keep_scoped_hits(tmp_path):
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        return _response()

    store, executor, engine, (first, second) = _setup(tmp_path, transport)
    a = _start(engine, first, ["same question"], need="first need")
    b = _start(engine, second, ["same question"], need="second need")
    assert a["operation_ref"]["id"] != b["operation_ref"]["id"]
    assert len(store.records("web_shared_query_job")) == 1
    asyncio.run(executor.run_ready("mission"))
    av = engine.read(first, {"operation_id": a["operation_ref"]["id"]})
    bv = engine.read(second, {"operation_id": b["operation_ref"]["id"]})
    assert av["status"] == bv["status"] == "completed"
    assert len(calls) == 1
    ahit = av["available_results"][0]["result"]["hits"][0]
    bhit = bv["available_results"][0]["result"]["hits"][0]
    assert ahit["hit_id"] != bhit["hit_id"]
    assert store.get("retrieval_hit", ahit["hit_id"])["body"]["task_id"] == first.proposal_ref["id"]
    assert (
        store.get("retrieval_hit", bhit["hit_id"])["body"]["task_id"] == second.proposal_ref["id"]
    )
    text = b"<html><body><p>Synthetic public page.</p></body></html>"
    hit = store.get("retrieval_hit", bhit["hit_id"])
    fetched = {
        "status": 200,
        "content": text,
        "requested_url": "https://example.org/source",
        "final_url": "https://example.org/source",
        "content_sha256": hashlib.sha256(text).hexdigest(),
        "byte_length": len(text),
        "retrieved_at": store.now(),
        "content_type": "text/html",
    }
    intake = IntakeService(EvidenceLibrary(store))
    accepted = intake.save_public_document_fetch(
        "shared-consumer-fetch",
        ref(hit),
        hit["body"]["delivery_ref"],
        mission_id="mission",
        run_id="run",
        actor_id=second.actor_id,
        task_id=second.proposal_ref["id"],
        fetched=fetched,
    )
    assert accepted["kind"] == "public_document_fetch"
    with pytest.raises(EvidenceError):
        intake.save_public_document_fetch(
            "cross-task-fetch",
            ref(hit),
            hit["body"]["delivery_ref"],
            mission_id="mission",
            run_id="run",
            actor_id=first.actor_id,
            task_id=first.proposal_ref["id"],
            fetched=fetched,
        )


def test_query_order_and_information_need_do_not_duplicate_physical_work(tmp_path):
    calls = []

    def transport(method, url, **kwargs):
        calls.append(kwargs["body"]["query"])
        return _response(kwargs["body"]["query"])

    store, executor, engine, (first, second) = _setup(tmp_path, transport)
    a = _start(engine, first, ["population question", "comparison question"], need="one purpose")
    b = _start(
        engine, second, ["comparison question", "population question"], need="another purpose"
    )
    assert len(store.records("web_shared_query_job")) == 2
    asyncio.run(executor.run_ready("mission"))
    assert sorted(calls) == ["comparison question", "population question"]
    for spec, started in ((first, a), (second, b)):
        view = engine.read(spec, {"operation_id": started["operation_ref"]["id"]})
        assert view["status"] == "completed"
        assert len(view["available_results"]) == 2


def test_cancel_one_subscriber_preserves_other_and_last_cancel_prevents_io(tmp_path):
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        return _response()

    store, executor, engine, (first, second) = _setup(tmp_path, transport)
    a = _start(engine, first, ["shared question"])
    b = _start(engine, second, ["shared question"])
    cancelled = engine.cancel(first, {"operation_id": a["operation_ref"]["id"]})
    assert cancelled["status"] == "cancelled"
    job = store.records("web_shared_query_job")[0]
    proposal = store.get("board_proposal", job["id"])
    assert proposal["body"]["branch_of"] is None
    asyncio.run(executor.run_ready("mission"))
    assert len(calls) == 1
    assert engine.read(second, {"operation_id": b["operation_ref"]["id"]})["status"] == "completed"

    calls2 = []
    _, executor2, engine2, (only,) = _setup(
        tmp_path / "other", lambda *a, **k: calls2.append(1) or _response(), peers=1
    )
    started = _start(engine2, only, ["question"])
    engine2.cancel(only, {"operation_id": started["operation_ref"]["id"]})
    asyncio.run(executor2.run_ready("mission"))
    assert not calls2


def test_new_subscriber_after_last_predispatch_cancel_gets_fresh_job(tmp_path):
    calls = []
    store, executor, engine, (first, second) = _setup(
        tmp_path, lambda *a, **k: calls.append(1) or _response()
    )
    old = _start(engine, first, ["same query"])
    engine.cancel(first, {"operation_id": old["operation_ref"]["id"]})
    new = _start(engine, second, ["same query"])
    jobs = store.records("web_shared_query_job")
    assert len(jobs) == 2
    assert sum(job["body"]["phase"] == "known_no_dispatch" for job in jobs) == 1
    asyncio.run(executor.run_ready("mission"))
    assert calls == [1]
    assert (
        engine.read(second, {"operation_id": new["operation_ref"]["id"]})["status"] == "completed"
    )


def test_external_source_guard_revocation_while_queued_blocks_http_and_read(tmp_path):
    allowed = [True]
    calls = []

    def source_guard(body):
        assert body["parent_spec"]["mission_id"] == "mission"
        if not allowed[0]:
            raise StateError("Synthetic external source use revoked")

    store, executor, engine, (first,) = _setup(
        tmp_path,
        lambda *a, **k: calls.append(1) or _response(),
        peers=1,
        source_use_guard=source_guard,
    )
    started = _start(engine, first, ["queued query"])
    allowed[0] = False
    asyncio.run(executor.run_ready("mission"))
    assert calls == []
    with pytest.raises(StateError, match="revoked"):
        engine.read(first, {"operation_id": started["operation_ref"]["id"]})
    assert store.records("web_shared_query_job")[0]["body"]["phase"] == "known_no_dispatch"


def test_quick_known_empty_exa_joins_one_shared_tavily_fallback(tmp_path):
    calls = []

    def transport(method, url, **kwargs):
        calls.append(url)
        if "exa" in url:
            return HTTPResult(200, json.dumps({"results": []}).encode(), {})
        return _response("tavily-result")

    store, executor, engine, (first, second) = _setup(tmp_path, transport)
    a = _start(engine, first, ["quick question"], mode="quick")
    b = _start(engine, second, ["quick question"], mode="quick", need="another purpose")
    asyncio.run(executor.run_ready("mission"))
    av = engine.read(first, {"operation_id": a["operation_ref"]["id"]})
    bv = engine.read(second, {"operation_id": b["operation_ref"]["id"]})
    assert av["status"] == bv["status"] == "completed"
    assert len(calls) == 2 and sum("exa" in url for url in calls) == 1
    assert sum("tavily" in url for url in calls) == 1
    assert len(store.records("web_shared_query_job")) == 2
    assert any(x["fallback_of"] for x in av["available_results"])
    tavily_work = next(
        x
        for x in executor._work("mission")
        if x["body"]["spec"]["work_id"]
        in {
            j["id"]
            for j in store.records("web_shared_query_job")
            if j["body"]["provider"] == "tavily"
        }
    )
    assert tavily_work["body"]["spec"]["quota_scopes"] == ["web:tavily", "web:tavily:minute"]


def test_explicit_refresh_and_changed_options_keep_physical_work_separate(tmp_path):
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        return _response()

    store, executor, engine, (first, second) = _setup(tmp_path, transport)
    _start(engine, first, ["question"])
    _start(engine, second, ["question"], limit=5)
    _start(engine, replace(first, work_id="explicit-refresh"), ["question"], refresh=True)
    assert len(store.records("web_shared_query_job")) == 3
    asyncio.run(executor.run_ready("mission"))
    assert len(calls) == 3


def test_source_lineage_compatibility_and_revocation_prevent_widened_sharing(tmp_path):
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        return _response()

    store, executor, engine, (first, second) = _setup(tmp_path, transport)
    _copy, use, call = _source_input(store)
    sourced = replace(first, call_id=call["id"], call_version=call["version"])
    _start(engine, sourced, ["question"])
    unsourced = _start(engine, second, ["question"])
    assert len(store.records("web_shared_query_job")) == 2
    store.transact(
        "shared-source-revoke",
        {},
        lambda tx: tx.put(
            "source_use",
            use["id"],
            {**tx.get(**use)["body"], "allowed_uses": [], "policy_status": "revoked"},
            expected_version=use["version"],
        ),
        mission_id="mission",
    )
    asyncio.run(executor.run_ready("mission"))
    assert len(calls) == 1
    assert (
        engine.read(second, {"operation_id": unsourced["operation_ref"]["id"]})["status"]
        == "completed"
    )


def test_completed_shared_job_reuses_after_fresh_state_store_restart(tmp_path):
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        return _response()

    _, executor, engine, (first, second) = _setup(tmp_path, transport)
    initial = _start(engine, first, ["restart query"])
    asyncio.run(executor.run_ready("mission"))
    assert (
        engine.read(first, {"operation_id": initial["operation_ref"]["id"]})["status"]
        == "completed"
    )
    fresh_store = StateStore(tmp_path)
    fresh_executor = ConcurrentExecutor(
        fresh_store,
        ProposalBoard(fresh_store),
        PeerMailbox(fresh_store),
        CapacityScheduler(fresh_store, executor.capacity.policy),
        TraceJournal(fresh_store),
        {},
        owner="host-restarted",
    )
    restarted = WebSearch(
        fresh_store,
        fresh_executor,
        engine.values,
        transport=transport,
        shared_enabled=lambda _spec: True,
    )
    reused = _start(restarted, second, ["restart query"], need="later need")
    assert reused["status"] == "completed" and len(calls) == 1
    assert len(fresh_store.records("web_shared_query_job")) == 1


def test_concurrent_starts_join_pending_physical_query_once(tmp_path, monkeypatch):
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        return _response()

    store, executor, engine, (first, second) = _setup(tmp_path, transport)
    entered, release = threading.Event(), threading.Event()
    original = engine.shared._submit_job
    first_call = [True]

    def delayed(job_id):
        if first_call[0]:
            first_call[0] = False
            entered.set()
            assert release.wait(5)
        return original(job_id)

    monkeypatch.setattr(engine.shared, "_submit_job", delayed)
    with ThreadPoolExecutor(max_workers=2) as pool:
        one = pool.submit(_start, engine, first, ["same query"])
        assert entered.wait(5)
        two = pool.submit(_start, engine, second, ["same query"])
        second_result = two.result(5)
        release.set()
        first_result = one.result(5)
    assert first_result["status"] == second_result["status"] == "pending"
    assert len(store.records("web_shared_query_job")) == 1
    asyncio.run(executor.run_ready("mission"))
    assert len(calls) == 1


@pytest.mark.parametrize("peers", [2, 4, 8])
def test_independent_peer_lanes_all_make_progress(tmp_path, peers):
    calls = []
    now = [time.time()]

    def transport(method, url, **kwargs):
        calls.append(kwargs["body"]["query"])
        return _response(kwargs["body"]["query"])

    store, executor, engine, specs = _setup(tmp_path, transport, peers=peers, clock=lambda: now[0])
    starts = [
        _start(engine, spec, [f"distinct question {index}"]) for index, spec in enumerate(specs)
    ]
    asyncio.run(executor.run_ready("mission"))
    if peers > 5:
        assert len(set(calls)) == 5  # Shared Exa second limit remains active.
        now[0] += 2
        asyncio.run(executor.run_ready("mission"))
    views = [
        engine.read(spec, {"operation_id": started["operation_ref"]["id"]})
        for spec, started in zip(specs, starts)
    ]
    assert len(store.records("web_shared_query_job")) == peers
    assert len(set(calls)) == peers
    assert all(view["status"] == "completed" for view in views)


def test_eight_uneven_peer_lanes_surface_fast_results_before_long_round_finishes(tmp_path):
    now = [time.time()]
    calls = []

    def transport(method, url, **kwargs):
        query = kwargs["body"]["query"]
        calls.append(query)
        return _response(query)

    store, executor, engine, specs = _setup(tmp_path, transport, peers=8,
                                            clock=lambda: now[0])
    starts = []
    for index, spec in enumerate(specs):
        queries = ([f"peer-0-round-{round_number}" for round_number in range(4)]
                   if index == 0 else [f"peer-{index}-fast"])
        starts.append(_start(engine, spec, queries))
    assert len(store.records("web_shared_query_job")) == 11

    asyncio.run(executor.run_ready("mission"))
    assert len(calls) == 5  # The saved Exa second gate admitted one bounded batch.
    first_owners = {query.split("-", 2)[1] for query in calls}
    assert len(first_owners) >= 3, calls
    assert sum(query.startswith("peer-0-") for query in calls) <= 2
    fast_ready = [
        index for index in range(1, 8)
        if engine.read(specs[index], {"operation_id": starts[index]["operation_ref"]["id"]})[
            "status"
        ] == "completed"
    ]
    assert fast_ready, calls
    assert engine.read(specs[0], {"operation_id": starts[0]["operation_ref"]["id"]})[
        "status"
    ] == "pending"

    for _ in range(3):
        now[0] += 2
        asyncio.run(executor.run_ready("mission"))
        if len(calls) == 11:
            break
    assert len(calls) == 11
    assert all(engine.read(spec, {"operation_id": started["operation_ref"]["id"]})[
        "status"
    ] == "completed" for spec, started in zip(specs, starts))


def test_fast_multi_hit_page_cursor_stays_stable_when_earlier_query_finishes_later(tmp_path):
    slow_entered, release_slow = threading.Event(), threading.Event()
    calls = []

    def transport(method, url, **kwargs):
        query = kwargs["body"]["query"]
        calls.append(query)
        if query == "slow first query":
            slow_entered.set()
            assert release_slow.wait(10)
            ids = ["slow-hit"]
        else:
            ids = ["fast-hit-one", "fast-hit-two"]
        return HTTPResult(200, json.dumps({"results": [{
            "id": source_id, "url": f"https://example.org/{source_id}",
            "title": source_id, "highlights": ["Uninspected candidate."],
        } for source_id in ids]}).encode(), {})

    _store, executor, engine, (spec,) = _setup(tmp_path, transport, peers=1)
    started = _start(engine, spec, ["slow first query", "fast second query"])
    operation_id = started["operation_ref"]["id"]
    try:
        with ThreadPoolExecutor(max_workers=1) as pool:
            running = pool.submit(asyncio.run, executor.run_ready("mission"))
            assert slow_entered.wait(5)
            deadline = time.monotonic() + 5
            first = None
            while time.monotonic() < deadline:
                first = engine.read(spec, {"operation_id": operation_id,
                                           "cursor": 0, "limit": 1})
                if first["available_results"]:
                    break
                time.sleep(0.01)
            assert first and first["available_results"]
            assert first["available_results"][0]["query"] == "fast second query"
            assert first["pending"] == 1
            second = engine.read(spec, {"operation_id": operation_id,
                                        "cursor": first["continuation_cursor"],
                                        "limit": 1})
            assert second["available_results"][0]["query"] == "fast second query"
            assert second["continuation_cursor"] == 2
            assert second["pending"] == 1
            fast_ids = [
                view["available_results"][0]["result"]["hits"][0]["hit_id"]
                for view in (first, second)
            ]
            assert len(set(fast_ids)) == 2
            release_slow.set()
            running.result(timeout=10)
        third = engine.read(spec, {"operation_id": operation_id,
                                   "cursor": second["continuation_cursor"],
                                   "limit": 1})
        assert third["status"] == "completed"
        assert third["available_results"][0]["query"] == "slow first query"
        assert third["continuation_cursor"] == 3
        assert third["has_more"] is False
        assert third["available_results"][0]["result"]["hits"][0]["hit_id"] not in fast_ids
        assert sorted(calls) == ["fast second query", "slow first query"]
    finally:
        release_slow.set()


def test_restart_after_subscription_before_job_submission_recovers_once(tmp_path, monkeypatch):
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        return _response()

    store, executor, engine, (first, _) = _setup(tmp_path, transport)

    def interrupted(_job_id):
        raise RuntimeError("synthetic stop before executor submission")

    monkeypatch.setattr(engine.shared, "_submit_job", interrupted)
    with pytest.raises(RuntimeError, match="synthetic stop"):
        _start(engine, first, ["pending restart"])
    operation_id = identity("web-search-", first.mission_id, first.work_id)
    assert store.get("web_search_operation", operation_id)
    fresh_store = StateStore(tmp_path)
    fresh_executor = ConcurrentExecutor(
        fresh_store,
        ProposalBoard(fresh_store),
        PeerMailbox(fresh_store),
        CapacityScheduler(fresh_store, executor.capacity.policy),
        TraceJournal(fresh_store),
        {},
        owner="host-restarted",
    )
    restarted = WebSearch(
        fresh_store,
        fresh_executor,
        engine.values,
        transport=transport,
        shared_enabled=lambda _spec: True,
    )
    pending = restarted.read(first, {"operation_id": operation_id})
    assert pending["status"] == "pending"
    asyncio.run(fresh_executor.run_ready("mission"))
    assert len(calls) == 1
    assert restarted.read(first, {"operation_id": operation_id})["status"] == "completed"


def test_uncertain_physical_transport_is_joined_without_replay(tmp_path):
    from urllib.error import URLError

    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        raise URLError("synthetic network outcome unknown")

    store, executor, engine, (first, second) = _setup(tmp_path, transport)
    a = _start(engine, first, ["uncertain query"])
    asyncio.run(executor.run_ready("mission"))
    first_view = engine.read(first, {"operation_id": a["operation_ref"]["id"]})
    assert first_view["status"] == "uncertain"
    b = _start(engine, second, ["uncertain query"])
    assert b["status"] == "uncertain" and len(calls) == 1
    assert len(store.records("web_shared_query_job")) == 1
    _start(engine, replace(second, work_id="explicit-refresh"), ["uncertain query"], refresh=True)
    asyncio.run(executor.run_ready("mission"))
    assert len(calls) == 2


def test_shared_provider_wait_survives_restart_and_delivers_to_joiner(tmp_path):
    now = [time.time()]
    calls = []

    def transport(*args, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return HTTPResult(429, b'{"error":"rate"}', {"Retry-After": "2"})
        return _response()

    store, executor, engine, (first, second) = _setup(tmp_path, transport, clock=lambda: now[0])
    a = _start(engine, first, ["rate question"])
    asyncio.run(executor.run_ready("mission"))
    b = _start(engine, second, ["rate question"])
    assert len(store.records("web_shared_query_job")) == 1
    assert len(calls) == 1
    fresh_store = StateStore(tmp_path, clock=lambda: now[0])
    fresh_executor = ConcurrentExecutor(
        fresh_store,
        ProposalBoard(fresh_store),
        PeerMailbox(fresh_store),
        CapacityScheduler(fresh_store, executor.capacity.policy),
        TraceJournal(fresh_store),
        {},
        owner="host-restarted",
    )
    restarted = WebSearch(
        fresh_store,
        fresh_executor,
        engine.values,
        transport=transport,
        shared_enabled=lambda _spec: True,
    )
    now[0] += 61  # Discovery's saved 429 has no retry_at; existing route uses 60s.
    asyncio.run(fresh_executor.run_ready("mission"))
    assert len(calls) == 2
    for spec, started in ((first, a), (second, b)):
        assert (
            restarted.read(spec, {"operation_id": started["operation_ref"]["id"]})["status"]
            == "completed"
        )
