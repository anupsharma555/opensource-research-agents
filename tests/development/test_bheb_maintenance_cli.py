"""Offline corpus inspection exposes aggregates and writes only an explicit local report."""

import importlib.util
import json
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("corpus_cli", ROOT / "scripts/validate_bheb_corpus.py")
CLI = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(CLI)


def test_unconfigured_default_has_no_network_or_writes(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(CLI, "ROOT", tmp_path)
    monkeypatch.setenv("OSRA_BHEB_CONFIG", "")
    assert CLI.main([]) == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "config_unavailable"
    assert list(tmp_path.iterdir()) == []


def test_env_resolution_does_not_expose_key(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(CLI, "ROOT", tmp_path)
    monkeypatch.delenv("OSRA_BHEB_CONFIG", raising=False)
    (tmp_path / ".env").write_text(
        'OSRA_OPENAI_API_KEY=synthetic-secret\nOSRA_BHEB_CONFIG=".local/absent.json"\n'
    )
    assert CLI.configured_path(tmp_path) == tmp_path / ".local/absent.json"
    assert CLI.main([]) == 0
    output = capsys.readouterr().out
    assert "synthetic-secret" not in output and str(tmp_path) not in output
    assert json.loads(output)["status"] == "unavailable"
    assert not (tmp_path / ".local").exists()


def test_explicit_private_output_is_protected_and_stdout_is_aggregate(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(CLI, "ROOT", tmp_path)
    report = {"status": "metadata_only", "counts": {"receipt_files": 1},
              "candidate_counts": {"unknown": 1}, "candidates": [{"title": "private synthetic title"}]}
    monkeypatch.setattr(CLI, "plan_candidates", lambda *_: report)
    output = tmp_path / ".local/report.json"
    assert CLI.main(["--config", "synthetic-config", "--candidates", "synthetic-candidates",
                     "--private-output", str(output)]) == 0
    assert "private synthetic title" not in capsys.readouterr().out
    assert json.loads(output.read_text()) == report
    assert output.stat().st_mode & 0o777 == 0o600
    with pytest.raises(FileExistsError):
        CLI.save_private({"changed": True}, output, tmp_path)
    assert json.loads(output.read_text()) == report


def test_report_cannot_write_archive_or_follow_symlink_outside_local(tmp_path):
    archive = tmp_path / "archive"
    archive.mkdir()
    local = tmp_path / ".local"
    local.mkdir()
    (local / "redirect").symlink_to(archive, target_is_directory=True)
    for output in (archive / "report.json", local / "redirect/report.json"):
        with pytest.raises(ValueError, match=".local"):
            CLI.save_private({"status": "plan"}, output, tmp_path)
    assert list(archive.iterdir()) == []
