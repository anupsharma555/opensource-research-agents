"""Exact, task-scoped reuse of saved progressive web-search operations."""

from __future__ import annotations

import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import asdict, replace

import pytest

from ora.coordination.board import ref
from ora.retrieval.web_search import KIND, WebSearch, identity
from ora.state import StateError
from tests.coordination.test_executor import propose
from tests.retrieval.test_web_search import response, setup_search

QUICK = {"mode": "quick", "information_need": "Find a public source",
         "queries": ["cool roof guidance"], "limit": 5}


def test_pending_completed_restart_refresh_and_invocation_replay(tmp_path):
    calls = []
    def transport(method, url, **kwargs):
        calls.append(kwargs["body"]["query"])
        return response(kwargs["body"]["query"])
    store, executor, engine, spec = setup_search(tmp_path, transport)
    first = engine.start(spec, QUICK)
    second_spec = replace(spec, work_id="same-search-second-turn")
    second = engine.start(second_spec, QUICK)
    assert first["reuse_status"] == "new"
    assert second["reuse_status"] == "reused"
    assert second["operation_ref"]["id"] == first["operation_ref"]["id"]
    assert first["status"] == second["status"] == "pending"
    assert len(store.records(KIND)) == 1
    assert len([work for work in executor._work("mission")
                if work["body"]["spec"]["handler_id"] == "web-query"]) == 1
    asyncio.run(executor.run_ready("mission"))
    assert calls == ["cool roof guidance"]

    restarted = WebSearch(store, executor, engine.values, transport=transport)
    third = restarted.start(replace(spec, work_id="third-turn"), QUICK)
    assert third["reuse_status"] == "reused"
    assert third["status"] == "completed"
    assert third["operation_ref"]["id"] == first["operation_ref"]["id"]
    assert calls == ["cool roof guidance"]

    refreshed = restarted.start(replace(spec, work_id="explicit-refresh"),
                                {**QUICK, "refresh": True})
    assert refreshed["reuse_status"] == "refreshed"
    assert refreshed["operation_ref"]["id"] != first["operation_ref"]["id"]
    asyncio.run(executor.run_ready("mission"))
    assert calls == ["cool roof guidance", "cool roof guidance"]
    assert restarted.start(second_spec, QUICK)["operation_ref"]["id"] == first["operation_ref"]["id"]
    assert len(calls) == 2
    after_refresh = restarted.start(replace(spec, work_id="after-refresh"), QUICK)
    assert after_refresh["operation_ref"]["id"] == refreshed["operation_ref"]["id"]


def test_first_explicit_refresh_reports_refreshed_and_can_be_reused(tmp_path):
    _store, _executor, engine, spec = setup_search(tmp_path, lambda *_a, **_k: response())
    first = engine.start(spec, {**QUICK, "refresh": True})
    assert first["reuse_status"] == "refreshed"
    repeated = engine.start(replace(spec, work_id="after-first-refresh"), QUICK)
    assert repeated["reuse_status"] == "reused"
    assert repeated["operation_ref"]["id"] == first["operation_ref"]["id"]


def test_distinct_terms_modes_order_provider_and_options_remain_distinct(tmp_path):
    store, _executor, engine, spec = setup_search(tmp_path, lambda *_a, **_k: response())
    cases = [
        {**QUICK, "queries": ["cool roofs guidance"]},
        {**QUICK, "mode": "research"},
        {**QUICK, "limit": 6},
        {**QUICK, "information_need": "Compare regional guidance"},
        {"mode": "research", "provider": "exa", "information_need": "Compare sources",
         "queries": ["roofs", "climate"]},
        {"mode": "research", "provider": "exa", "information_need": "Compare sources",
         "queries": ["climate", "roofs"]},
        {"mode": "research", "provider": "tavily", "information_need": "Compare sources",
         "queries": ["roofs", "climate"]},
        {"mode": "research", "provider": "searxng", "information_need": "Compare sources",
         "queries": ["roofs", "climate"]},
    ]
    operations = {engine.start(spec, QUICK)["operation_ref"]["id"]}
    for index, args in enumerate(cases):
        result = engine.start(replace(spec, work_id=f"distinct-{index}"), args)
        assert result["reuse_status"] == "new"
        assert result["operation_ref"]["id"] not in operations
        operations.add(result["operation_ref"]["id"])
    assert len(operations) == len(cases) + 1
    assert engine.start(replace(spec, work_id="exact-again"), QUICK)["reuse_status"] == "reused"


def test_task_and_actor_boundaries_do_not_share_searches(tmp_path):
    store, _executor, engine, spec = setup_search(tmp_path, lambda *_a, **_k: response())
    first = engine.start(spec, QUICK)
    other_task = propose(engine.executor.board, spec.actor_id, "different-task")
    second = engine.start(replace(spec, work_id="other-task-search",
                                  proposal_ref=other_task), QUICK)
    assert second["operation_ref"]["id"] != first["operation_ref"]["id"]
    other_actor = next(item["id"] for item in store.records("agent_instance")
                       if item["id"] != spec.actor_id)
    other_config = next(ref(item) for item in store.records("agent_configuration")
                        if item["body"]["instance_id"] == other_actor)
    actor_task = propose(engine.executor.board, other_actor, "other-actor-task")
    third = engine.start(replace(spec, work_id="other-actor-search",
                                 actor_id=other_actor, configuration_ref=other_config,
                                 proposal_ref=actor_task), QUICK)
    assert third["operation_ref"]["id"] not in {
        first["operation_ref"]["id"], second["operation_ref"]["id"],
    }


def test_cancelled_and_uncertain_are_reused_without_retry(tmp_path):
    calls = []
    def transport(*_args, **_kwargs):
        from urllib.error import URLError
        calls.append(True)
        raise URLError("synthetic uncertain dispatch")
    _store, executor, engine, spec = setup_search(tmp_path, transport)
    cancelled = engine.start(spec, QUICK)
    engine.cancel(spec, {"operation_id": cancelled["operation_ref"]["id"]})
    same_cancelled = engine.start(replace(spec, work_id="cancelled-again"), QUICK)
    assert same_cancelled["reuse_status"] == "reused"
    assert same_cancelled["status"] == "cancelled"
    assert calls == []

    uncertain_args = {**QUICK, "queries": ["uncertain query"]}
    uncertain = engine.start(replace(spec, work_id="uncertain-first"), uncertain_args)
    asyncio.run(executor.run_ready("mission"))
    assert engine.read(spec, {"operation_id": uncertain["operation_ref"]["id"]})["status"] == "uncertain"
    repeated = engine.start(replace(spec, work_id="uncertain-repeat"), uncertain_args)
    assert repeated["reuse_status"] == "reused"
    assert repeated["operation_ref"]["id"] == uncertain["operation_ref"]["id"]
    assert len(calls) == 1
    fresh = engine.start(replace(spec, work_id="uncertain-refresh"),
                         {**uncertain_args, "refresh": True})
    assert fresh["reuse_status"] == "refreshed"
    asyncio.run(executor.run_ready("mission"))
    assert len(calls) == 2


def test_concurrent_starts_share_planned_pending_work_and_one_dispatch(tmp_path, monkeypatch):
    calls = []
    def transport(method, url, **kwargs):
        calls.append(kwargs["body"]["query"])
        return response()
    store, executor, engine, spec = setup_search(tmp_path, transport)
    entered, release = threading.Event(), threading.Event()
    original_submit = engine._submit
    count_lock = threading.Lock()
    count = 0
    def delayed_submit(*args, **kwargs):
        nonlocal count
        with count_lock:
            count += 1
            first = count == 1
        if first:
            entered.set()
            assert release.wait(5)
        return original_submit(*args, **kwargs)
    monkeypatch.setattr(engine, "_submit", delayed_submit)
    with ThreadPoolExecutor(max_workers=2) as pool:
        first_future = pool.submit(engine.start, spec, QUICK)
        assert entered.wait(5)
        second = pool.submit(engine.start, replace(spec, work_id="racing-second"), QUICK).result(5)
        assert second["status"] == "pending"
        assert second["pending"] == 1
        assert second["reuse_status"] == "reused"
        release.set()
        first = first_future.result(5)
    assert first["operation_ref"]["id"] == second["operation_ref"]["id"]
    assert len(store.records(KIND)) == 1
    asyncio.run(executor.run_ready("mission"))
    assert calls == ["cool roof guidance"]


def test_read_recovers_crash_between_operation_claim_and_work_submission(tmp_path, monkeypatch):
    calls = []
    def transport(method, url, **kwargs):
        calls.append(kwargs["body"]["query"])
        return response()
    store, executor, engine, spec = setup_search(tmp_path, transport)
    def interrupted(*_args, **_kwargs):
        raise RuntimeError("synthetic process stop after atomic claim")
    monkeypatch.setattr(engine, "_submit", interrupted)
    with pytest.raises(RuntimeError, match="synthetic process stop"):
        engine.start(spec, QUICK)
    operation_id = identity("web-search-", spec.mission_id, spec.work_id)
    operation = store.get(KIND, operation_id)
    assert len(operation["body"]["items"]) == 1
    assert not [work for work in executor._work("mission")
                if work["body"]["spec"]["handler_id"] == "web-query"]
    restarted = WebSearch(store, executor, engine.values, transport=transport)
    pending = restarted.read(spec, {"operation_id": operation_id})
    assert pending["status"] == "pending" and pending["pending"] == 1
    asyncio.run(executor.run_ready("mission"))
    assert calls == ["cool roof guidance"]


def test_caller_source_revocation_between_precheck_and_binding_is_denied(tmp_path, monkeypatch):
    store, _executor, engine, spec = setup_search(tmp_path, lambda *_a, **_k: response())
    original = engine.start(spec, QUICK)
    _copy_ref, use_ref, call_ref = _source_input(store)
    caller = replace(spec, work_id="racing-sourced-caller",
                     call_id=call_ref["id"], call_version=call_ref["version"])
    checked, continue_start = threading.Event(), threading.Event()
    original_check = engine._require_source_scope
    first = True
    def pause_after_precheck(body):
        nonlocal first
        original_check(body)
        if body["parent_spec"]["work_id"] == caller.work_id and first:
            first = False
            checked.set()
            assert continue_start.wait(5)
    monkeypatch.setattr(engine, "_require_source_scope", pause_after_precheck)
    with ThreadPoolExecutor(max_workers=1) as pool:
        pending = pool.submit(engine.start, caller, QUICK)
        assert checked.wait(5)
        store.transact("revoke-racing-caller-source", {}, lambda tx: tx.put(
            "source_use", use_ref["id"], {
                **tx.get(**use_ref)["body"], "allowed_uses": [],
                "policy_status": "revoked",
            }, expected_version=use_ref["version"]), mission_id="mission")
        continue_start.set()
        with pytest.raises(StateError, match="restricts this model input"):
            pending.result(5)
    assert store.get("web_search_invocation", identity(
        "web-search-invocation-", spec.mission_id, caller.work_id,
    )) is None
    assert store.get(KIND, original["operation_ref"]["id"])


def test_cached_invocation_replay_rechecks_caller_after_precheck(tmp_path, monkeypatch):
    store, _executor, engine, spec = setup_search(tmp_path, lambda *_a, **_k: response())
    original = engine.start(spec, QUICK)
    _copy_ref, use_ref, call_ref = _source_input(store)
    caller = replace(spec, work_id="sourced-replay",
                     call_id=call_ref["id"], call_version=call_ref["version"])
    first = engine.start(caller, QUICK)
    assert first["operation_ref"]["id"] == original["operation_ref"]["id"]
    checked, continue_start = threading.Event(), threading.Event()
    original_check = engine._require_source_scope
    pause_once = True
    def pause_after_precheck(body):
        nonlocal pause_once
        original_check(body)
        if body["parent_spec"]["work_id"] == caller.work_id and pause_once:
            pause_once = False
            checked.set()
            assert continue_start.wait(5)
    monkeypatch.setattr(engine, "_require_source_scope", pause_after_precheck)
    with ThreadPoolExecutor(max_workers=1) as pool:
        replay = pool.submit(engine.start, caller, QUICK)
        assert checked.wait(5)
        store.transact("revoke-replay-source", {}, lambda tx: tx.put(
            "source_use", use_ref["id"], {
                **tx.get(**use_ref)["body"], "allowed_uses": [],
                "policy_status": "revoked",
            }, expected_version=use_ref["version"]), mission_id="mission")
        continue_start.set()
        with pytest.raises(StateError, match="restricts this model input"):
            replay.result(5)


def test_legacy_operation_and_source_revocation_do_not_refresh_implicitly(tmp_path):
    calls = []
    def transport(method, url, **kwargs):
        calls.append(kwargs["body"]["query"])
        return response()
    store, executor, engine, spec = setup_search(tmp_path, transport)
    old_id = identity("web-search-", spec.mission_id, "legacy-work")
    # A pre-reuse operation had no refresh field or family/invocation records.
    legacy_args = {"mode": "quick", "information_need": QUICK["information_need"],
                   "queries": QUICK["queries"], "provider": "exa", "limit": QUICK["limit"]}
    store.transact("legacy-web-operation", {}, lambda tx: ref(tx.put(KIND, old_id, {
        "schema": "ora.web_search.operation.v1", "mission_id": spec.mission_id,
        "run_id": spec.run_id, "actor_id": spec.actor_id,
        "task_id": spec.proposal_ref["id"], "arguments": legacy_args,
        "parent_spec": asdict(spec), "cancel_requested": False,
        "fallback_provider": "tavily", "items": [], "deliveries": [],
        "created_at": store.now(),
    })), mission_id=spec.mission_id)
    with pytest.raises(StateError, match="changed inputs"):
        engine.start(replace(spec, work_id="legacy-work"), {**QUICK, "refresh": True})
    reused = engine.start(replace(spec, work_id="legacy-reuse"), QUICK)
    assert reused["reuse_status"] == "reused"
    assert reused["operation_ref"]["id"] == old_id
    asyncio.run(executor.run_ready("mission"))
    assert calls == ["cool roof guidance"]

    # A new caller's permitted input cannot reauthorize a revoked producer.
    copy_ref, use_ref, original_call = _source_input(store)
    sourced = replace(spec, work_id="source-origin", call_id=original_call["id"],
                      call_version=original_call["version"])
    sourced_args = {**QUICK, "queries": ["source-derived query"]}
    source_operation = engine.start(sourced, sourced_args)
    store.transact("revoke-source", {}, lambda tx: tx.put(
        "source_use", use_ref["id"], {
            **tx.get(**use_ref)["body"], "allowed_uses": [],
            "policy_status": "revoked",
        }, expected_version=use_ref["version"]), mission_id="mission")
    with pytest.raises(StateError, match="restricts this model input"):
        engine.start(replace(spec, work_id="source-reuse"), sourced_args)
    assert store.get("web_search_invocation", identity(
        "web-search-invocation-", spec.mission_id, "source-reuse",
    )) is None
    with pytest.raises(StateError, match="restricts this model input"):
        engine.read(spec, {"operation_id": source_operation["operation_ref"]["id"]})
    with pytest.raises(StateError, match="restricts this model input"):
        engine.start(replace(spec, work_id="revoked-caller",
                             call_id=original_call["id"],
                             call_version=original_call["version"]), QUICK)
    assert calls == ["cool roof guidance"]
    assert copy_ref["id"] == "reuse-source"


def _source_input(store):
    def save(tx):
        copy = tx.put("source_copy", "reuse-source", {
            "mission_id": "mission", "rights_status": "permitted",
        })
        use = tx.put("source_use", "reuse-use", {
            "copy_ref": ref(copy), "allowed_uses": ["model_input"],
            "policy_status": "granted",
        })
        tx.bind("source_use.copy", f"{copy['id']}:{copy['version']}", "source_use", use["id"])
        call = tx.put("agent_call", "reuse-origin-call", {"mission_id": "mission"})
        tx.put("model_request_intent", f"model-intent:{call['id']}:{call['version']}", {
            "call_ref": ref(call), "source_refs": [ref(copy)],
            "context_source_dependency_refs": [],
        })
        return {"copy_ref": ref(copy), "use_ref": ref(use), "call_ref": ref(call)}
    saved = store.transact("reuse-source-input", {}, save, mission_id="mission")
    return saved["copy_ref"], saved["use_ref"], saved["call_ref"]
