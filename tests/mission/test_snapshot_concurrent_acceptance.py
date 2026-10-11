"""Accept one immutable projection when builders share a fingerprint."""

from concurrent.futures import ThreadPoolExecutor
from threading import Barrier, current_thread

import ora.mission.projection as projection
from ora.contracts.models import LiveStartRequest
from ora.state.inspection import load_snapshot
from tests.mission.test_service import service


def test_same_fingerprint_with_distinct_capture_times_reuses_accepted_snapshot(
    tmp_path, monkeypatch
):
    def no_model_call(*_args, **_kwargs):
        raise AssertionError("Snapshot publication must not call a model")

    host = service(tmp_path, monkeypatch, no_model_call)
    try:
        run_id = host.start(LiveStartRequest(
            request_id="same-fingerprint-builders",
            question="Inspect saved progress",
            agent_ids=["agent-synthesis"],
        )).run_id
        control = host._control(run_id, "agent-synthesis")
        host._update_control(control, phase="waiting", waiting_reason="Saved update")

        arrive = Barrier(2)
        original = projection.save_snapshot
        captured_times = []

        def distinct_capture_time():
            return (
                "2026-01-01T00:00:00Z"
                if current_thread().name.endswith("_0")
                else "2026-01-01T00:00:01Z"
            )

        def wait_before_acceptance(store, operation_id, snapshot, **kwargs):
            captured_times.append(snapshot["capturedAt"])
            arrive.wait(timeout=10)
            return original(store, operation_id, snapshot, **kwargs)

        monkeypatch.setattr(projection, "_now", distinct_capture_time)
        monkeypatch.setattr(projection, "save_snapshot", wait_before_acceptance)
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="projection-builder") as pool:
            futures = [pool.submit(host._refresh_snapshot, run_id) for _ in range(2)]
            accepted = [future.result(timeout=15) for future in futures]

        assert len(set(captured_times)) == 2
        assert accepted[0] == accepted[1]
        assert host.store.get("mission_snapshot_head", run_id)["body"]["snapshot_ref"] == accepted[0]
        assert load_snapshot(host.store, accepted[0]["id"])["capturedAt"] in captured_times
        assert host.store.get("research_snapshot", accepted[0]["id"], 2) is None
        assert host.store.records("model_dispatch_attempt") == []
    finally:
        host.close()
