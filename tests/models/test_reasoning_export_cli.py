"""The private reasoning export command does not dispatch or print model text."""

import json
from pathlib import Path

import pytest

from ora.state import StateStore
from scripts import export_reasoning_traces as cli


def _state(tmp_path):
    state = tmp_path / "state"
    state.mkdir()
    (state / "state.sqlite3").touch()
    return state


def test_cli_opens_selected_state_read_only_and_prints_only_metadata(tmp_path, monkeypatch, capsys):
    state = _state(tmp_path)
    observed = {}

    def store(root, *, read_only):
        observed["root"] = root
        observed["read_only"] = read_only
        return type("ReadOnlyStore", (), {"root": root})()

    def export(_store, run_id, *, output_dir):
        observed["run_id"] = run_id
        observed["output_dir"] = output_dir
        return {
            "run_id": run_id, "output_dir": state / "reasoning" / run_id,
            "status": "complete", "receipt_count": 1, "dispatch_only_count": 0,
            "exported_count": 1,
            "unavailable_count": 0, "truncated_count": 0,
            "steps": [{"status": "returned", "capture_fidelity": "provider_exact",
                       "text_path": state / "reasoning" / run_id / "step-01" / "reasoning_trace.txt",
                       "json_path": state / "reasoning" / run_id / "step-01" / "reasoning_trace.json",
                       "markdown_path": state / "reasoning" / run_id / "step-01" / "reasoning_trace.md",
                       "reasoning": "private provider text"}],
            "reasoning": "private provider text",
        }

    monkeypatch.setattr(cli, "StateStore", store)
    monkeypatch.setattr(cli, "export_run_reasoning", export)
    assert cli.main(["--state-dir", str(state), "--run-id", "run-one"]) == 0
    output = capsys.readouterr().out
    assert "private provider text" not in output
    step = json.loads(output)["steps"][0]
    assert step["capture_fidelity"] == "provider_exact"
    assert Path(step["markdown_path"]).name == "reasoning_trace.md"
    assert observed == {"root": state, "read_only": True,
                        "run_id": "run-one", "output_dir": None}


def test_cli_rejects_symlink_destination_before_export(tmp_path, monkeypatch, capsys):
    state = _state(tmp_path)
    target = tmp_path / "target"
    target.mkdir()
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    monkeypatch.setattr(cli, "export_run_reasoning", lambda *_a, **_k: pytest.fail("export called"))
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--state-dir", str(state), "--run-id", "run-one",
                  "--output-dir", str(link / "reasoning")])
    assert stopped.value.code == 2
    assert "reasoning export failed" in capsys.readouterr().err
    assert not (target / "reasoning").exists()


def test_cli_sanitizes_record_errors_and_does_not_create_missing_state(tmp_path, monkeypatch, capsys):
    missing = tmp_path / "missing"
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--state-dir", str(missing), "--run-id", "run-one"])
    assert stopped.value.code == 2
    assert not missing.exists()

    state = _state(tmp_path)
    monkeypatch.setattr(cli, "StateStore", lambda *_a, **_k: type("ReadOnlyStore", (), {"root": state})())
    monkeypatch.setattr(cli, "export_run_reasoning",
                        lambda *_a, **_k: (_ for _ in ()).throw(ValueError("private provider text")))
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--state-dir", str(state), "--run-id", "run-one"])
    assert stopped.value.code == 2
    assert "private provider text" not in capsys.readouterr().err


def test_cli_rejects_unexpected_metadata_without_printing_it(tmp_path, monkeypatch, capsys):
    state = _state(tmp_path)
    monkeypatch.setattr(cli, "StateStore",
                        lambda *_a, **_k: type("ReadOnlyStore", (), {"root": state})())
    monkeypatch.setattr(cli, "export_run_reasoning", lambda *_a, **_k: {
        "run_id": "run-one", "output_dir": state / "reasoning",
        "status": "private provider text", "receipt_count": 0,
        "dispatch_only_count": 0,
        "exported_count": 0, "unavailable_count": 0, "truncated_count": 0, "steps": [],
    })
    with pytest.raises(SystemExit) as stopped:
        cli.main(["--state-dir", str(state), "--run-id", "run-one"])
    assert stopped.value.code == 2
    captured = capsys.readouterr()
    assert "private provider text" not in captured.out + captured.err


def test_cli_exports_empty_synthetic_run_without_inventing_reasoning(tmp_path, capsys):
    state = tmp_path / "state"
    store = StateStore(state)
    store.transact("seed-run", {}, lambda tx: tx.put(
        "mission_run", "run-empty", {"mission_id": "mission-synthetic"},
    ), mission_id="mission-synthetic")

    assert cli.main(["--state-dir", str(state), "--run-id", "run-empty"]) == 0
    report = json.loads(capsys.readouterr().out)
    assert report["status"] == "no_receipts"
    assert report["receipt_count"] == 0
    assert report["dispatch_only_count"] == 0
    assert report["steps"] == []
    assert (state / "reasoning").is_dir()
    assert report["combined_markdown_path"]
    assert Path(report["combined_markdown_path"]).name == "reasoning_trace.md"
