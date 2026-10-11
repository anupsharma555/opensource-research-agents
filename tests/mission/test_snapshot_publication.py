"""Keep Atlas's latest view current when projection builders finish out of order."""

from concurrent.futures import ThreadPoolExecutor
from threading import Event, current_thread

import pytest

import ora.mission.service as service_module
from ora.contracts.models import LiveStartRequest
from ora.state.inspection import load_snapshot
from tests.mission.test_service import service


@pytest.mark.parametrize("pause_at", ["after_save", "before_commit"])
def test_late_projection_cannot_replace_a_newer_published_view(tmp_path, monkeypatch, pause_at):
    def no_model_call(*_args, **_kwargs):
        raise AssertionError("Reading and publishing snapshots must not call a model")

    host = service(tmp_path, monkeypatch, no_model_call)
    run_id = host.start(LiveStartRequest(
        request_id="projection-publication-race", question="Inspect saved progress",
        agent_ids=["agent-synthesis"],
    )).run_id
    control = host._control(run_id, "agent-synthesis")
    host._update_control(control, phase="waiting", waiting_reason="Earlier progress")
    prepared, release = Event(), Event()
    original = service_module.persist_snapshot
    original_transact = host.store.transact
    older_refs = []
    paused = False

    def pause_once():
        nonlocal paused
        if current_thread().name.startswith("older-projection") and not paused:
            paused = True
            prepared.set()
            assert release.wait(10), "Test did not release the earlier projection"

    def pause_older_after_save(store, run, controls):
        pointer = original(store, run, controls)
        if current_thread().name.startswith("older-projection") and not older_refs:
            older_refs.append(pointer)
            if pause_at == "after_save":
                pause_once()
        return pointer

    def pause_older_before_commit(operation_id, *args, **kwargs):
        if pause_at == "before_commit" and operation_id.startswith("snapshot-head:"):
            pause_once()
        return original_transact(operation_id, *args, **kwargs)

    monkeypatch.setattr(service_module, "persist_snapshot", pause_older_after_save)
    monkeypatch.setattr(host.store, "transact", pause_older_before_commit)
    with ThreadPoolExecutor(max_workers=1, thread_name_prefix="older-projection") as pool:
        earlier = pool.submit(host._refresh_snapshot, run_id)
        try:
            assert prepared.wait(10), "Earlier projection did not reach publication"
            control = host._control(run_id, "agent-synthesis")
            host._update_control(control, phase="waiting", waiting_reason="New peer update needed")
            newer_ref = host._refresh_snapshot(run_id)
            assert newer_ref != older_refs[0]
        finally:
            release.set()
        returned_ref = earlier.result(timeout=10)

    current = host.store.get("mission_snapshot_head", run_id)["body"]["snapshot_ref"]
    assert current == newer_ref
    assert returned_ref == newer_ref
    assert load_snapshot(host.store, older_refs[0]["id"])["snapshotId"] == older_refs[0]["id"]
    assert host.state(run_id).snapshot.snapshot_id == newer_ref["id"]
    assert host.store.records("model_dispatch_attempt") == []
    host.close()
