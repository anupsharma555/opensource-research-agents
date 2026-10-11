"""Synthetic offline checks for exact, read-only mission wait diagnosis."""

from __future__ import annotations

from ora.coordination.board import ref
from ora.coordination.executor import _id
from ora.mission.waits import inspect_waits
from ora.state import StateStore


def peer_cycle_fixture(store: StateStore, *, phase: str = "waiting") -> dict:
    a_ref = {"kind": "mission_task_control", "id": "control-a", "version": 1}
    b_ref = {"kind": "mission_task_control", "id": "control-b", "version": 1}
    ab_ref = {"kind": "mail_request", "id": "request-ab", "version": 1}
    ba_ref = {"kind": "mail_request", "id": "request-ba", "version": 1}

    def save(tx):
        tx.put("board_proposal", "task-a", {"mission_id": "m", "status": "open"})
        tx.put("board_proposal", "task-b", {"mission_id": "m", "status": "open"})
        tx.put(
            "mail_request",
            "request-ab",
            {
                "mission_id": "m",
                "status": "unanswered",
                "requester_id": "actor-a",
                "recipient_id": "actor-b",
            },
        )
        tx.put(
            "mail_request",
            "request-ba",
            {
                "mission_id": "m",
                "status": "unanswered",
                "requester_id": "actor-b",
                "recipient_id": "actor-a",
            },
        )
        a = tx.put(
            "mission_task_control",
            "control-a",
            {
                "mission_id": "m",
                "actor_id": "actor-a",
                "task_id": "task-a",
                "phase": phase,
                "work_id": None,
                "wait_for": [
                    {"kind": "peer_request", "request_ref": ab_ref, "target_control_ref": b_ref}
                ]
                if phase == "waiting"
                else [],
            },
        )
        b = tx.put(
            "mission_task_control",
            "control-b",
            {
                "mission_id": "m",
                "actor_id": "actor-b",
                "task_id": "task-b",
                "phase": phase,
                "work_id": None,
                "wait_for": [
                    {"kind": "peer_request", "request_ref": ba_ref, "target_control_ref": a_ref}
                ]
                if phase == "waiting"
                else [],
            },
        )
        tx.put(
            "mission_task_control",
            "healthy-c",
            {
                "mission_id": "m",
                "actor_id": "actor-c",
                "task_id": "task-c",
                "phase": "ready_model",
                "work_id": None,
            },
        )
        return {"a": ref(a), "b": ref(b), "ab": ab_ref, "ba": ba_ref}

    return store.transact("seed-cycle", {}, save, mission_id="m")


def test_only_explicit_current_waits_form_a_peer_cycle(tmp_path) -> None:
    store = StateStore(tmp_path)
    saved = peer_cycle_fixture(store)
    report = inspect_waits(store, "m")
    assert len(report["cycles"]) == 1
    cycle = report["cycles"][0]
    assert cycle["control_refs"] == [saved["a"], saved["b"]]
    assert cycle["actor_ids"] == ["actor-a", "actor-b"]
    assert {item["input_ref"]["id"] for item in cycle["edges"]} == {"request-ab", "request-ba"}
    assert all(item["reason"] != "no_recorded_future_wake" for item in report["stranded_waits"])
    assert inspect_waits(StateStore(tmp_path), "m")["fingerprint"] == report["fingerprint"]
    assert store.records("wait_diagnosis") == []  # Pure observation has no saved side effect.


def test_reciprocal_messages_while_working_are_not_a_cycle(tmp_path) -> None:
    store = StateStore(tmp_path)
    peer_cycle_fixture(store, phase="ready_model")
    report = inspect_waits(store, "m")
    assert report["cycles"] == []
    assert len(report["pending_requests"]) == 2
    assert report["stranded_waits"] == []


def test_explicit_unanswered_request_to_working_peer_is_a_normal_wait(tmp_path) -> None:
    store = StateStore(tmp_path)
    target_ref = {"kind": "mission_task_control", "id": "target", "version": 1}
    request_ref = {"kind": "mail_request", "id": "request", "version": 1}

    def save(tx):
        tx.put(
            "mail_request",
            "request",
            {
                "mission_id": "m",
                "status": "unanswered",
                "requester_id": "requester",
                "recipient_id": "worker",
            },
        )
        tx.put(
            "mission_task_control",
            "target",
            {
                "mission_id": "m",
                "actor_id": "worker",
                "phase": "ready_model",
                "work_id": None,
            },
        )
        source = tx.put(
            "mission_task_control",
            "source",
            {
                "mission_id": "m",
                "actor_id": "requester",
                "phase": "waiting",
                "work_id": None,
                "wait_for": [
                    {
                        "kind": "peer_request",
                        "request_ref": request_ref,
                        "target_control_ref": target_ref,
                    }
                ],
            },
        )
        return ref(source)

    source_ref = store.transact("one-way-wait", {}, save, mission_id="m")
    report = inspect_waits(store, "m")
    assert report["cycles"] == []
    assert report["stale_waits"] == []
    assert report["stranded_waits"] == []
    assert report["normal_waits"] == [
        {
            "control_ref": source_ref,
            "actor_id": "requester",
            "kind": "explicit_peer_or_dependency_wait",
            "dependencies": [
                {
                    "kind": "peer_request",
                    "input_ref": request_ref,
                    "target_control_ref": target_ref,
                    "target_phase": "ready_model",
                }
            ],
            "waiting_target_count": 0,
        }
    ]


def test_changed_target_or_answer_needs_reread_not_a_cycle(tmp_path) -> None:
    store = StateStore(tmp_path)
    peer_cycle_fixture(store)

    def revise(tx):
        target = tx.get("mission_task_control", "control-b")
        updated = tx.put("mission_task_control", "control-b", target["body"], expected_version=1)
        answered = tx.get("mail_request", "request-ba")
        tx.put(
            "mail_request",
            "request-ba",
            {**answered["body"], "status": "awaiting_review"},
            expected_version=1,
        )
        return ref(updated)

    store.transact("revision", {}, revise, mission_id="m")
    report = inspect_waits(store, "m")
    assert report["cycles"] == []
    reasons = {item["reason"] for item in report["stale_waits"]}
    assert "target_not_current_at_exact_version" in reasons
    assert "request_answered_changed_or_not_owned" in reasons
    assert any(item["status"] == "awaiting_review" for item in report["pending_requests"])


def _same_run_peer_wait(store: StateStore, *, target_run: str = "run") -> tuple[dict, dict]:
    peer_cycle_fixture(store)

    def stamp(tx):
        source = tx.get("mission_task_control", "control-a")
        target = tx.get("mission_task_control", "control-b")
        current_target = tx.put(
            "mission_task_control", target["id"],
            {**target["body"], "run_id": target_run, "wait_for": []},
            expected_version=target["version"],
        )
        current_source = tx.put(
            "mission_task_control", source["id"],
            {**source["body"], "run_id": "run", "wait_for": [{
                "kind": "peer_request",
                "request_ref": {"kind": "mail_request", "id": "request-ab", "version": 1},
                "target_control_ref": ref(current_target),
            }]},
            expected_version=source["version"],
        )
        return {"source": ref(current_source), "target": ref(current_target)}

    saved = store.transact("same-run-wait", {}, stamp, mission_id="m")
    return saved["source"], saved["target"]


def test_peer_wait_survives_target_progress_but_not_terminal_target(tmp_path) -> None:
    store = StateStore(tmp_path)
    source_ref, target_ref = _same_run_peer_wait(store)

    def advance(tx, phase):
        target = tx.get("mission_task_control", target_ref["id"])
        return ref(tx.put(
            "mission_task_control", target["id"],
            {**target["body"], "phase": phase}, expected_version=target["version"],
        ))

    progressed = store.transact(
        "peer-progress", {}, lambda tx: advance(tx, "pending_model"), mission_id="m",
    )
    report = inspect_waits(store, "m")
    waiting = next(item for item in report["normal_waits"] if item["control_ref"] == source_ref)
    assert waiting["kind"] == "explicit_peer_or_dependency_wait"
    assert waiting["dependencies"][0]["target_control_ref"] == progressed
    assert not any(item["control_ref"] == source_ref for item in report["stale_waits"])

    store.transact(
        "peer-done-without-answer", {}, lambda tx: advance(tx, "done"), mission_id="m",
    )
    report = inspect_waits(store, "m")
    assert any(
        item["control_ref"] == source_ref
        and item["reason"] == "target_finished_or_deferred_without_waited_input"
        for item in report["stale_waits"]
    )
    assert not any(item["control_ref"] == source_ref for item in report["normal_waits"])


def test_peer_wait_answer_or_cross_run_target_needs_review(tmp_path) -> None:
    answered = StateStore(tmp_path / "answered")
    source_ref, _target_ref = _same_run_peer_wait(answered)

    def save_answer(tx):
        request = tx.get("mail_request", "request-ab")
        updated = tx.put(
            "mail_request", request["id"],
            {**request["body"], "status": "awaiting_review"},
            expected_version=request["version"],
        )
        return {"request_ref": ref(updated)}

    answered.transact("peer-answered", {}, save_answer, mission_id="m")
    report = inspect_waits(answered, "m")
    assert any(
        item["control_ref"] == source_ref
        and item["reason"] == "request_answered_changed_or_not_owned"
        for item in report["stale_waits"]
    )

    wrong_run = StateStore(tmp_path / "wrong-run")
    source_ref, _target_ref = _same_run_peer_wait(wrong_run, target_run="other")
    report = inspect_waits(wrong_run, "m")
    assert any(
        item["control_ref"] == source_ref
        and item["reason"] == "peer_target_outside_waiting_run"
        for item in report["stale_waits"]
    )


def test_explicit_current_proposal_dependencies_can_form_a_cycle(tmp_path) -> None:
    store = StateStore(tmp_path)
    pa = {"kind": "board_proposal", "id": "proposal-a", "version": 1}
    pb = {"kind": "board_proposal", "id": "proposal-b", "version": 1}
    ca = {"kind": "mission_task_control", "id": "control-a", "version": 1}
    cb = {"kind": "mission_task_control", "id": "control-b", "version": 1}

    def save(tx):
        tx.put(
            "board_proposal",
            "proposal-a",
            {"mission_id": "m", "status": "open", "depends_on": [pb]},
        )
        tx.put(
            "board_proposal",
            "proposal-b",
            {"mission_id": "m", "status": "open", "depends_on": [pa]},
        )
        tx.put(
            "mission_task_control",
            "control-a",
            {
                "mission_id": "m",
                "actor_id": "actor-a",
                "task_id": "proposal-a",
                "proposal_ref": pa,
                "phase": "waiting",
                "work_id": None,
                "wait_for": [
                    {"kind": "proposal_dependency", "proposal_ref": pb, "target_control_ref": cb}
                ],
            },
        )
        tx.put(
            "mission_task_control",
            "control-b",
            {
                "mission_id": "m",
                "actor_id": "actor-b",
                "task_id": "proposal-b",
                "proposal_ref": pb,
                "phase": "waiting",
                "work_id": None,
                "wait_for": [
                    {"kind": "proposal_dependency", "proposal_ref": pa, "target_control_ref": ca}
                ],
            },
        )
        return {"seeded": True}

    store.transact("dependency-cycle", {}, save, mission_id="m")
    report = inspect_waits(store, "m")
    assert len(report["cycles"]) == 1
    assert {item["kind"] for item in report["cycles"][0]["edges"]} == {"proposal_dependency"}


def test_open_proposal_dependency_on_active_control_is_not_a_cycle(tmp_path) -> None:
    store = StateStore(tmp_path)
    target_ref = {"kind": "mission_task_control", "id": "target", "version": 1}
    source_task_ref = {"kind": "board_proposal", "id": "source-task", "version": 1}
    prerequisite_ref = {"kind": "board_proposal", "id": "prerequisite", "version": 1}

    def save(tx):
        tx.put("board_proposal", "prerequisite", {"mission_id": "m", "status": "open"})
        tx.put(
            "board_proposal",
            "source-task",
            {
                "mission_id": "m",
                "status": "open",
                "depends_on": [prerequisite_ref],
            },
        )
        tx.put(
            "mission_task_control",
            "target",
            {
                "mission_id": "m",
                "actor_id": "worker",
                "task_id": "prerequisite",
                "phase": "pending_model",
                "work_id": "active-work",
            },
        )
        source = tx.put(
            "mission_task_control",
            "source",
            {
                "mission_id": "m",
                "actor_id": "requester",
                "task_id": "source-task",
                "proposal_ref": source_task_ref,
                "phase": "waiting",
                "work_id": None,
                "wait_for": [
                    {
                        "kind": "proposal_dependency",
                        "proposal_ref": prerequisite_ref,
                        "target_control_ref": target_ref,
                    }
                ],
            },
        )
        return ref(source)

    source_ref = store.transact("proposal-one-way", {}, save, mission_id="m")
    report = inspect_waits(store, "m")
    assert report["cycles"] == []
    assert report["stale_waits"] == []
    assert not any(item["control_ref"] == source_ref for item in report["stranded_waits"])
    normal = next(item for item in report["normal_waits"] if item["control_ref"] == source_ref)
    assert normal["waiting_target_count"] == 0
    assert normal["dependencies"][0]["target_phase"] == "pending_model"


def test_quota_timer_and_uncertain_effect_are_not_stranded_peer_waits(tmp_path) -> None:
    clock = [100.0]
    store = StateStore(tmp_path, clock=lambda: clock[0])

    def save(tx):
        for control_id, phase, work_id, extra in (
            ("quota", "waiting", "quota-work", {}),
            ("queued", "pending_model", "queued-work", {}),
            ("unscheduled", "waiting", "unscheduled-work", {}),
            ("uncertain", "waiting", "uncertain-work", {}),
            ("local", "waiting", None, {"waiting_reason": "ContextOverflow: local repair"}),
            ("timer", "retry_at", None, {"retry_at": 160.0}),
            ("known", "waiting", "known-work", {}),
        ):
            tx.put(
                "mission_task_control",
                control_id,
                {
                    "mission_id": "m",
                    "actor_id": control_id,
                    "task_id": control_id,
                    "phase": phase,
                    "work_id": work_id,
                    **extra,
                },
            )
        for work_id, phase in (
            ("quota-work", "pending"),
            ("queued-work", "pending"),
            ("unscheduled-work", "pending"),
            ("uncertain-work", "pending"),
            ("known-work", "result_unaccepted"),
        ):
            tx.put(
                "executor_work",
                _id("executor-work:", ["m", work_id]),
                {
                    "mission_id": "m",
                    "spec": {"mission_id": "m", "work_id": work_id},
                    "phase": phase,
                    "result_ref": {"kind": "executor_result", "id": "saved", "version": 1}
                    if phase == "result_unaccepted"
                    else None,
                },
            )
        tx.put(
            "capacity_operation",
            _id("executor-cap:", ["m", "quota-work"]),
            {"mission_id": "m", "phase": "waiting", "wait_reason": "quota", "wake_at": 150.0},
        )
        tx.put(
            "capacity_operation",
            _id("executor-cap:", ["m", "queued-work"]),
            {"mission_id": "m", "phase": "queued", "wake_at": None},
        )
        tx.put(
            "capacity_operation",
            _id("executor-cap:", ["m", "uncertain-work"]),
            {"mission_id": "m", "phase": "uncertain", "wake_at": None},
        )
        tx.put(
            "capacity_operation",
            _id("executor-cap:", ["m", "unscheduled-work"]),
            {"mission_id": "m", "phase": "waiting", "wake_at": None},
        )
        return {"seeded": True}

    store.transact("wait-classes", {}, save, mission_id="m")
    report = inspect_waits(store, "m", now=100.0)
    assert report["cycles"] == []
    normal = {item["control_ref"]["id"]: item for item in report["normal_waits"]}
    assert normal["quota"]["wake_at"] == 150.0 and normal["quota"]["due"] is False
    assert normal["queued"]["kind"] == "capacity_ready_for_local_admission"
    assert normal["timer"]["wake_at"] == 160.0 and normal["timer"]["due"] is False
    assert {item["control_ref"]["id"] for item in report["uncertain_effects"]} == {"uncertain"}
    stranded = {item["control_ref"]["id"]: item for item in report["stranded_waits"]}
    assert stranded["local"]["reason"] == "no_recorded_future_wake"
    assert stranded["known"]["reason"] == "known_result_needs_acceptance_repair"
    assert stranded["unscheduled"]["reason"] == "capacity_has_no_saved_future_wake"
    assert "quota" not in stranded and "queued" not in stranded and "uncertain" not in stranded
    due = inspect_waits(store, "m", now=200.0)
    assert {item["control_ref"]["id"] for item in due["normal_waits"]} == {
        "quota",
        "queued",
        "timer",
    }
    assert due["fingerprint"] != report["fingerprint"]
