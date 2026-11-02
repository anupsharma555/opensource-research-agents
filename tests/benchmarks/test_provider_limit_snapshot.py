"""Frozen live evaluations share numeric provider hints; offline cases do not."""
import importlib.util
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def test_live_snapshot_links_shared_hints_without_copying_research_state(tmp_path):
    spec = importlib.util.spec_from_file_location("limit_snapshot_runner", ROOT / "scripts/validate_search_agent.py")
    runner = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(runner)
    project = tmp_path / "project"
    project.mkdir()
    for name in ("src/ora", "roles", "runtime_skills"):
        (project / name).mkdir(parents=True)
    one, two, offline = [tmp_path / name for name in ("one", "two", "offline")]
    runner.freeze(project, one, "run-one", share_provider_limits=True)
    runner.freeze(project, two, "run-two", share_provider_limits=True)
    runner.freeze(project, offline, "run-offline")
    shared = project / ".local/provider-limits"
    assert (one / "runtime/.local/provider-limits").resolve() == shared
    assert (two / "runtime/.local/provider-limits").resolve() == shared
    assert not (offline / "runtime/.local/provider-limits").exists()
    assert not (one / "state").exists()
    assert not (two / "state").exists()
