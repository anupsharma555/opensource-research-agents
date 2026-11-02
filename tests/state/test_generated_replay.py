"""Vary command identity and conflicting payloads across real store reopenings.

Generated sequences check durable at-most-once acceptance and preservation of
earlier results, rather than a mocked database or a particular SQL statement.
"""

from tempfile import TemporaryDirectory

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from ora.state import StateConflict, StateStore


@settings(max_examples=25, deadline=None)
@given(st.lists(st.tuples(st.integers(0, 6), st.integers(0, 50)), min_size=2, max_size=20))
def test_generated_replay_and_conflicts_survive_reopen(commands):
    with TemporaryDirectory(prefix="ora-replay-") as directory:
        accepted = {}
        for number, value in commands:
            store = StateStore(directory)
            operation = f"synthetic-{number}"

            def save(tx):
                record = tx.put("synthetic_result", operation, {"value": value})
                tx.emit("synthetic-mission", "accepted", {"id": operation})
                return record

            if operation in accepted and accepted[operation] != value:
                with pytest.raises(StateConflict):
                    store.transact(operation, {"value": value}, save)
            else:
                result = store.transact(operation, {"value": value}, save)
                accepted[operation] = value
                assert result["body"]["value"] == value
            reopened = StateStore(directory)
            assert {r["id"]: r["body"]["value"] for r in reopened.records("synthetic_result")} == accepted
            assert all(r["version"] == 1 for r in reopened.records("synthetic_result"))
            assert reopened.reconcile()["database_integrity"] == "ok"
