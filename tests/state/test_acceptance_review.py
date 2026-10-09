"""Check the richer ANU-395 recovery cases with synthetic saved research work.

These are local SQLite/file fixtures, not a live host, provider run, scientific
appraisal, or hours-long endurance measurement.
"""

import errno
import sqlite3

import pytest

from ora.state import IntegrityFault, StateConflict, StateStore


def _ref(record):
    return {key: record[key] for key in ("kind", "id", "version")}


def _save_case(store, mission_id, status):
    blob = store.put_blob(b"Synthetic depression abstract; outcome methods not supplied.")

    def accept(tx):
        mission = tx.put("mission", mission_id, {"status": status})
        task = tx.put("task", f"{mission_id}-task", {"status": "waiting", "mission_ref": _ref(mission)})
        source = tx.put("source_copy", f"{mission_id}-source", {
            "blob": blob, "access_surface": "abstract", "source_version": "synthetic-v1",
        })
        configuration = tx.put("agent_configuration", f"{mission_id}-configuration", {
            "model_id": "synthetic-model", "input_refs": [_ref(source)],
        })
        claim = tx.put("claim", f"{mission_id}-claim", {
            "text": "A synthetic report has an unresolved outcome method.", "source_ref": _ref(source),
        })
        report = tx.put("report", f"{mission_id}-report", {
            "text": "Synthetic partial report; no clinical conclusion.", "claim_ref": _ref(claim),
        })
        request = tx.put("peer_request", f"{mission_id}-request", {
            "status": "requested", "task_ref": _ref(task),
        })
        tx.link(claim, source, "depends_on")
        tx.link(report, claim, "depends_on")
        tx.emit(mission_id, "work_saved", {"report": _ref(report), "request": _ref(request)})
        return {name: _ref(record) for name, record in {
            "mission": mission, "task": task, "source": source,
            "configuration": configuration, "claim": claim,
            "report": report, "request": request,
        }.items()}

    return store.transact(
        f"seed-{mission_id}", {"status": status, "blob": blob}, accept,
        mission_id=mission_id,
    )


def _assert_case(store, refs, mission_id, status):
    for pointer in refs.values():
        assert store.get(**pointer) is not None
    assert store.get(**refs["mission"])["body"]["status"] == status
    assert store.get(**refs["request"])["body"]["status"] == "requested"
    source = store.get(**refs["source"])
    assert store.read_blob(source["body"]["blob"]["sha256"])
    assert any(link["target"] == refs["source"] for link in store.links(refs["claim"]))
    assert any(link["target"] == refs["claim"] for link in store.links(refs["report"]))
    assert len(store.events(mission_id)) == 1
    assert store.events(mission_id)[0]["delivered"] is False


def test_cache_loss_and_index_rebuild_keep_active_and_completed_records(tmp_path):
    root = tmp_path / "state"
    store = StateStore(root)
    cases = {
        status: _save_case(store, f"mission-{status}", status)
        for status in ("active", "completed")
    }
    # The completed label is a storage fixture, not an assertion that an open
    # peer request constitutes scientifically finished work.
    disposable_cache = root / "disposable-view-cache.json"
    disposable_cache.write_text('{"stale": true}')
    disposable_cache.unlink()
    with sqlite3.connect(store.db_path) as db:
        db.execute("DROP INDEX reverse_links")
    store.rebuild_indexes()
    reopened = StateStore(root)
    for status, refs in cases.items():
        _assert_case(reopened, refs, f"mission-{status}", status)
    assert len(list((root / "objects").iterdir())) == 1  # Shared bytes, separate source records.
    assert reopened.reconcile()["pending_events"] == 2


def test_completion_replay_pins_task_input_and_configuration_versions(tmp_path):
    store = StateStore(tmp_path / "state")
    refs = _save_case(store, "mission-active", "active")
    request = {name: refs[name] for name in ("task", "source", "configuration")}

    def complete(tx):
        assert all(tx.get(**pointer) for pointer in request.values())
        result = tx.put("completion", "completion-1", {"inputs": request})
        tx.link(result, tx.get(**request["source"]), "depends_on")
        tx.emit("mission-active", "completion_saved", {"result": _ref(result)})
        return {"completion_ref": _ref(result)}

    first = store.transact("complete-1", request, complete, mission_id="mission-active")
    reopened = StateStore(store.root)
    repeated = reopened.transact(
        "complete-1", request,
        lambda _: pytest.fail("An identical completion must replay without another write"),
        mission_id="mission-active",
    )
    assert repeated == first
    assert len(reopened.events("mission-active")) == 2
    changed = reopened.transact(
        "change-configuration", {"previous": request["configuration"]},
        lambda tx: tx.put(
            "agent_configuration", request["configuration"]["id"],
            {"model_id": "synthetic-model-v2", "input_refs": [request["source"]]},
            expected_version=1,
        ),
    )
    with pytest.raises(StateConflict, match="different inputs"):
        reopened.transact(
            "complete-1", {**request, "configuration": _ref(changed)},
            lambda _: pytest.fail("A changed version cannot reuse the operation ID"),
            mission_id="mission-active",
        )
    assert reopened.get(**first["completion_ref"])["body"]["inputs"] == request
    assert len(reopened.events("mission-active")) == 2


def test_scoped_rejection_keeps_quota_wakeup_and_pending_work(tmp_path):
    store = StateStore(tmp_path / "state")
    refs = _save_case(store, "mission-active", "active")
    wake = store.transact(
        "save-wakeup", {"task": refs["task"]},
        lambda tx: tx.put("quota_wakeup", "wakeup-1", {
            "status": "scheduled", "task_ref": refs["task"],
        }),
    )
    prior_events = store.events("mission-active")
    with pytest.raises(IntegrityFault):
        store.transact(
            "reject-artifact", {"candidate": "missing-bytes"},
            lambda tx: tx.put("artifact", "bad-artifact", {
                "blob": {"sha256": "a" * 64, "size": 17},
            }),
            mission_id="mission-active",
        )
    assert store.get("artifact", "bad-artifact") is None
    assert store.get(**_ref(wake))["body"]["status"] == "scheduled"
    assert store.get(**refs["task"])["body"]["status"] == "waiting"
    assert store.get(**refs["request"])["body"]["status"] == "requested"
    assert store.events("mission-active") == prior_events
    with sqlite3.connect(f"file:{store.db_path}?mode=ro", uri=True) as db:
        assert db.execute(
            "SELECT code FROM diagnostics WHERE operation_id='reject-artifact'"
        ).fetchone()[0] == "IntegrityFault"


@pytest.mark.parametrize(
    "point,path_kind",
    [("during_blob_write", "objects"), ("before_outbox_write", "database")],
)
def test_path_labeled_capacity_fault_recovers_without_losing_prior_work(tmp_path, point, path_kind):
    store = StateStore(tmp_path / "state")
    refs = _save_case(store, "mission-active", "active")
    prior_events = store.events("mission-active")
    affected_path = store.blobs.root if path_kind == "objects" else store.db_path

    def inject(actual):
        if actual == point:
            raise OSError(errno.ENOSPC, "Synthetic capacity failure", str(affected_path))

    def attempt(target):
        blob = target.put_blob(b"A separate synthetic source after capacity recovery.")

        def accept(tx):
            artifact = tx.put("artifact", "resumed-artifact", {"blob": blob})
            tx.emit("mission-active", "artifact_saved", {"artifact": _ref(artifact)})
            return _ref(artifact)

        return target.transact(
            "resume-artifact", {"blob": blob}, accept, mission_id="mission-active"
        )

    with pytest.raises(OSError) as failed:
        attempt(StateStore(store.root, fault=inject))
    assert failed.value.errno == errno.ENOSPC
    assert failed.value.filename == str(affected_path)
    assert store.get("artifact", "resumed-artifact") is None
    assert store.events("mission-active") == prior_events
    _assert_case(store, refs, "mission-active", "active")
    assert store.reconcile()["degraded_artifacts"] == []

    resumed = attempt(StateStore(store.root))
    assert StateStore(store.root).get(**resumed) is not None
    assert len(store.events("mission-active")) == len(prior_events) + 1
    assert store.reconcile()["degraded_artifacts"] == []


def test_v1_to_v2_migration_preserves_richer_active_work_and_restore(tmp_path):
    root = tmp_path / "state"
    old = StateStore(root, schema_version=1)
    refs = _save_case(old, "mission-active", "active")
    old.backup(tmp_path / "backup")

    def interrupt(point):
        if point == "migration_2_step_0":
            raise RuntimeError("Synthetic interrupted schema migration")

    with pytest.raises(RuntimeError, match="interrupted schema migration"):
        StateStore(root, fault=interrupt)
    _assert_case(StateStore(root, schema_version=1), refs, "mission-active", "active")
    with sqlite3.connect(root / "state.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 1

    upgraded = StateStore(root)
    _assert_case(upgraded, refs, "mission-active", "active")
    with sqlite3.connect(root / "state.sqlite3") as db:
        assert db.execute("PRAGMA user_version").fetchone()[0] == 3
    restored = StateStore.restore(tmp_path / "backup", tmp_path / "restored")
    _assert_case(restored, refs, "mission-active", "active")
    with pytest.raises(ValueError, match="Unsupported store schema"):
        StateStore(root, schema_version=1)
