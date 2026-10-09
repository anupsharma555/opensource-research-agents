"""Exercise queued-work pressure through the real Stage 3 SQLite components."""

from ora.state import StateStore
from scripts.measure_stage3_bursts import measure_trial


def test_burst_retains_work_and_cancellation_across_duplicate_wake_and_restart(tmp_path):
    state_dir = tmp_path / "parallel"
    receipt = measure_trial(state_dir, size=10, model_permits=3,
                            slow_seconds=0.5, commit_delay_seconds=0.001)
    observed = receipt["observed"]
    assert observed["ready_backlog_at_submission"] == 9
    assert observed["max_active_callbacks"] <= 4  # Three model, one retrieval.
    assert observed["accepted_artifacts"] == observed["accepted_receipts"] == 9
    assert observed["duplicate_notification_observed"] == []
    assert observed["restart_observed"] == []
    assert observed["work_phases_after_restart"]["zz-cancel"] == "canceled"
    assert observed["healthy_retrieval_finished_during_slow_model"]
    assert receipt["timeline_ms"]["accepted"]["c-retrieval"] < receipt["timeline_ms"]["b-slow"]["callback_end"]

    reopened = StateStore(state_dir, read_only=True)
    artifacts = reopened.records("executor_artifact")
    receipts = reopened.records("executor_receipt")
    assert {row["body"]["work_id"] for row in artifacts} == (
        set(observed["work_phases_after_restart"]) - {"zz-cancel"})
    assert len({row["body"]["work_id"] for row in artifacts}) == len(artifacts)
    assert len(receipts) == len(artifacts)
    assert all(row["body"]["configuration_ref"] in receipt["input"]["configuration_refs"]
               for row in artifacts)
    canceled = [row for row in reopened.records("capacity_operation")
                if row["body"]["task_id"] == "zz-cancel"]
    assert len(canceled) == 1 and canceled[0]["body"]["phase"] == "canceled"


def test_same_inputs_under_serial_and_eligible_parallel_permits(tmp_path):
    serial = measure_trial(tmp_path / "serial", size=8, model_permits=1,
                           slow_seconds=0.5, commit_delay_seconds=0.001)
    parallel = measure_trial(tmp_path / "parallel", size=8, model_permits=3,
                             slow_seconds=0.5, commit_delay_seconds=0.001)
    for key in ("size", "retrieval_permits", "slow_seconds",
                "commit_delay_seconds", "configuration_refs", "synthetic_quota_scopes"):
        assert serial["input"][key] == parallel["input"][key]
    assert serial["observed"]["accepted_artifacts"] == parallel["observed"]["accepted_artifacts"] == 7
    assert set(serial["observed"]["work_phases_after_restart"].values()) == {"completed", "canceled"}
    assert set(parallel["observed"]["work_phases_after_restart"].values()) == {"completed", "canceled"}
    assert serial["observed"]["max_active_callbacks"] <= 2
    assert parallel["observed"]["max_active_callbacks"] <= 4
    # Timings are evidence in the receipt, not a machine-specific pass threshold.
    assert serial["observed"]["first_accepted_artifact_ms"] > 0
    assert parallel["observed"]["first_accepted_artifact_ms"] > 0
    assert serial["observed"]["run_elapsed_ms"] > 0
    assert parallel["observed"]["run_elapsed_ms"] > 0
