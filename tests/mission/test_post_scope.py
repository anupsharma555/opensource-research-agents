"""Late peer-request disposition uses exact event order and saved scope state."""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor

from ora.mission.post_scope import record_post_scope_disposition
from ora.state import StateStore

MISSION = "synthetic-post-scope-mission"
RUN = "synthetic-post-scope-run"
ACTOR = "synthetic-requester"


def ref(record: dict) -> dict:
    return {key: record[key] for key in ("kind", "id", "version")}


def fixture(tmp_path, *, scope_status: str = "continued") -> tuple[StateStore, dict, dict, dict]:
    store = StateStore(tmp_path)

    def seed(tx):
        run = tx.put("mission_run", RUN, {"mission_id": MISSION, "run_id": RUN, "status": "active"})
        proposal = tx.put("board_proposal", "synthetic-optional-task", {
            "mission_id": MISSION, "actor_ref": {"kind": "agent_instance", "id": ACTOR, "version": 1},
        })
        control = tx.put("mission_task_control", "synthetic-optional-control", {
            "mission_id": MISSION, "run_id": RUN, "task_id": proposal["id"],
            "actor_id": ACTOR, "proposal_ref": ref(proposal), "scope_status": scope_status,
        })
        scope = tx.put("mission_scope_disposition", "synthetic-scope-choice", {
            "mission_id": MISSION, "run_ref": ref(run), "decisions": [],
        })
        return {"run": ref(run), "proposal": ref(proposal), "control": ref(control), "scope": ref(scope)}

    records = store.transact("synthetic-post-scope-seed", {}, seed, mission_id=MISSION)
    return store, records["run"], records["proposal"], records["scope"]


def request(store: StateStore, proposal_ref: dict, request_id: str) -> dict:
    def open_request(tx):
        saved = tx.put("mail_request", request_id, {
            "mission_id": MISSION, "requester_id": ACTOR,
            "cause_ref": proposal_ref, "status": "unanswered",
        })
        tx.emit(MISSION, "peer_request_opened", {"request_ref": ref(saved)})
        return ref(saved)

    return store.transact(f"open:{request_id}", {}, open_request, mission_id=MISSION)


def close(store: StateStore, run_ref: dict, scope_ref: dict | None, *, legacy: bool = False, status: str = "completed") -> dict:
    def finish(tx):
        current = tx.get(**run_ref)
        cutoff = tx.db.execute(
            "SELECT COALESCE(MAX(sequence),0) FROM events WHERE mission_id=?", (MISSION,)
        ).fetchone()[0]
        body = {**current["body"], "status": status, "scope_disposition_ref": scope_ref}
        if not legacy:
            body["closure_event_through"] = cutoff
        saved = tx.put("mission_run", RUN, body, expected_version=run_ref["version"])
        tx.emit(MISSION, "mission_scope_completed", {"run_ref": ref(saved)})
        return ref(saved)

    return store.transact("synthetic-scope-close", {}, finish, mission_id=MISSION)


def test_late_continued_request_is_exact_and_idempotent_across_restart(tmp_path):
    store, run_ref, proposal_ref, scope_ref = fixture(tmp_path)
    completed = close(store, run_ref, scope_ref)
    request_ref = request(store, proposal_ref, "synthetic-late-request")
    event_count = len(store.events(MISSION))
    result = record_post_scope_disposition(
        store, completed_run_ref=completed, request_id=request_ref["id"]
    )
    assert result["decision"] == "continue_under_saved_optional_scope"
    assert result["created"] is True
    saved = store.get(**result["disposition_ref"])["body"]
    assert saved["completed_run_ref"] == completed
    assert saved["scope_disposition_ref"] == scope_ref
    assert saved["request_ref"] == request_ref
    assert saved["cause_proposal_ref"] == proposal_ref
    assert saved["cause_control_ref"] == {
        "kind": "mission_task_control", "id": "synthetic-optional-control", "version": 1,
    }
    assert saved["cause_control_scope_status"] == "continued"
    assert saved["opening_event_sequence"] > store.get(**completed)["body"]["closure_event_through"]
    assert store.get("mission_run", RUN) == store.get(**completed)
    assert len(store.events(MISSION)) == event_count

    control = store.get(**saved["cause_control_ref"])
    store.transact("synthetic-later-control-change", {}, lambda tx: ref(tx.put(
        "mission_task_control", control["id"],
        {**control["body"], "scope_status": "deferred"},
        expected_version=control["version"],
    )), mission_id=MISSION)
    reopened = StateStore(tmp_path)
    replay = record_post_scope_disposition(
        reopened, completed_run_ref=completed, request_id=request_ref["id"]
    )
    assert replay == {**result, "created": False}
    assert len(reopened.records("mission_post_scope_disposition")) == 1


def test_historical_request_opened_before_cutoff_stays_historical_after_review(tmp_path):
    store, run_ref, proposal_ref, scope_ref = fixture(tmp_path)
    request_ref = request(store, proposal_ref, "synthetic-before-close")
    completed = close(store, run_ref, scope_ref)
    opened = store.get(**request_ref)
    store.transact("synthetic-review-after-close", {}, lambda tx: ref(tx.put(
        "mail_request", request_ref["id"], {**opened["body"], "status": "answered"},
        expected_version=request_ref["version"],
    )), mission_id=MISSION)
    assert record_post_scope_disposition(
        store, completed_run_ref=completed, request_id=request_ref["id"]
    ) is None
    assert store.records("mission_post_scope_disposition") == []


def test_late_request_from_noncontinued_scope_requires_followup(tmp_path):
    store, run_ref, proposal_ref, scope_ref = fixture(tmp_path, scope_status="deferred")
    completed = close(store, run_ref, scope_ref)
    request_ref = request(store, proposal_ref, "synthetic-deferred-request")
    result = record_post_scope_disposition(
        store, completed_run_ref=completed, request_id=request_ref["id"]
    )
    assert result["decision"] == "followup_required"
    assert store.get(**result["disposition_ref"])["body"]["cause_control_scope_status"] == "deferred"


def test_legacy_completion_and_cancelled_run_do_not_invent_late_classification(tmp_path):
    for label, legacy, status in (("legacy", True, "completed"), ("cancelled", False, "cancelled")):
        store, run_ref, proposal_ref, scope_ref = fixture(tmp_path / label)
        closed = close(store, run_ref, scope_ref, legacy=legacy, status=status)
        request_ref = request(store, proposal_ref, f"synthetic-{label}-late")
        assert record_post_scope_disposition(
            store, completed_run_ref=closed, request_id=request_ref["id"]
        ) is None
        assert store.records("mission_post_scope_disposition") == []


def test_cause_proposal_version_can_advance_but_control_relation_must_be_unique(tmp_path):
    store, run_ref, proposal_ref, scope_ref = fixture(tmp_path)
    completed = close(store, run_ref, scope_ref)
    request_ref = request(store, proposal_ref, "synthetic-advanced-proposal")
    proposal = store.get(**proposal_ref)

    def advance(tx):
        updated = tx.put("board_proposal", proposal["id"], proposal["body"], expected_version=1)
        control = tx.get("mission_task_control", "synthetic-optional-control")
        return ref(tx.put("mission_task_control", control["id"], {
            **control["body"], "proposal_ref": ref(updated),
        }, expected_version=control["version"]))

    advanced_control = store.transact("synthetic-advance-proposal", {}, advance, mission_id=MISSION)
    result = record_post_scope_disposition(
        store, completed_run_ref=completed, request_id=request_ref["id"]
    )
    saved = store.get(**result["disposition_ref"])["body"]
    assert saved["cause_proposal_ref"] == proposal_ref
    assert saved["cause_control_ref"] == advanced_control

    # A separate request has no unique cause control, so no authorization is invented.
    second_ref = request(store, proposal_ref, "synthetic-ambiguous-request")
    store.transact("synthetic-duplicate-control", {}, lambda tx: ref(tx.put(
        "mission_task_control", "synthetic-duplicate-control", {
            **store.get(**advanced_control)["body"],
        },
    )), mission_id=MISSION)
    assert record_post_scope_disposition(
        store, completed_run_ref=completed, request_id=second_ref["id"]
    ) is None
    assert len(store.records("mission_post_scope_disposition")) == 1


def test_two_disposition_writers_keep_one_immutable_record(tmp_path):
    store, run_ref, proposal_ref, scope_ref = fixture(tmp_path)
    completed = close(store, run_ref, scope_ref)
    request_ref = request(store, proposal_ref, "synthetic-racing-request")
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: record_post_scope_disposition(
            store, completed_run_ref=completed, request_id=request_ref["id"]
        ), range(2)))
    assert results[0]["disposition_ref"] == results[1]["disposition_ref"]
    assert results[0]["decision"] == results[1]["decision"]
    assert all(item["created"] in {True, False} for item in results)
    assert record_post_scope_disposition(
        store, completed_run_ref=completed, request_id=request_ref["id"]
    )["created"] is False
    assert len(store.records("mission_post_scope_disposition")) == 1
    assert store.get("mission_run", RUN)["version"] == completed["version"]
