"""Check attempt-private scratch and port ownership across tasks and missions."""

import json

import pytest

from ora.contracts.resources import ResourceRegistry


def test_attempts_isolate_same_filenames_ports_and_stale_cleanup(tmp_path):
    registry = ResourceRegistry(tmp_path)
    a = registry.allocate("mission-a", "task-a", "attempt-a")
    b = registry.allocate("mission-a", "task-b", "attempt-b")
    replacement = None
    try:
        assert a.workspace != b.workspace and a.port != b.port
        registry.write_result(a, {"owner": "a"})
        b_path = registry.write_result(b, {"owner": "b"})
        replacement = registry.allocate("mission-a", "task-a", "attempt-a2")
        assert replacement.generation == a.generation + 1
        new_path = registry.write_result(replacement, {"owner": "a2"})
        with pytest.raises(PermissionError, match="stale owner"):
            registry.write_result(a, {"overwrite": "forbidden"})
        with pytest.raises(PermissionError, match="stale owner"):
            registry.cleanup(a)
        assert json.loads(new_path.read_text()) == {"owner": "a2"}
        assert json.loads(b_path.read_text()) == {"owner": "b"}
        registry.cleanup(replacement)
        registry.cleanup(b)
    finally:
        a.listener.close()
        b.listener.close()
        if replacement:
            replacement.listener.close()
        registry.close()


def test_unknown_output_is_retained_without_touching_sibling(tmp_path):
    registry = ResourceRegistry(tmp_path)
    a = registry.allocate("mission-a", "task-a", "attempt-a")
    b = registry.allocate("mission-a", "task-b", "attempt-b")
    try:
        (a.workspace / "unexpected.txt").write_text("retain for diagnosis")
        registry.write_result(b, {"healthy": True})
        with pytest.raises(ValueError, match="unknown files"):
            registry.cleanup(a)
        assert (a.workspace / "unexpected.txt").exists()
        assert (b.workspace / "result.json").exists()
    finally:
        a.listener.close()
        b.listener.close()
        registry.close()


def test_same_task_name_in_different_missions_does_not_replace_owner(tmp_path):
    registry = ResourceRegistry(tmp_path)
    a = registry.allocate("mission-a", "task-same", "attempt-same")
    b = registry.allocate("mission-b", "task-same", "attempt-same")
    try:
        registry.write_result(a, {"mission": "a"})
        registry.write_result(b, {"mission": "b"})
        assert a.generation == b.generation == 1
        registry.cleanup(a)
        assert b.workspace.exists()
        registry.cleanup(b)
    finally:
        a.listener.close()
        b.listener.close()
        registry.close()
