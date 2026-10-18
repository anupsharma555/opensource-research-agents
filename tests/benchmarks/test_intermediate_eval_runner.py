"""One natural evaluation can account without aggregate caps only by explicit choice."""

import json
import urllib.request

import pytest

from ora.state import StateStore
from tests.benchmarks.test_search_agent_runner import project, response, runner
from tests.mission.test_service import answer


def test_accounting_only_dry_run_never_calls_host(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runner, "run_case", lambda *_a, **_kw: pytest.fail("live invoked"))
    request = tmp_path / "start.json"
    request.write_text(json.dumps({"start_request": {
        "request_id": "synthetic-intermediate-v1",
        "question": "Compare two synthetic claims using available evidence.",
        "scope": "Synthetic scope", "agent_ids": ["agent-synthesis"],
        "credential_profile": "backup",
    }}))
    assert runner.main(["--start-request", str(request), "--no-aggregate-limits",
                        "--until-complete"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["credential_profile"] == "backup"
    assert preview["model_calls"] is None
    assert preview["planned_tokens"] is None
    assert preview["retrieval_attempts"] is None
    assert preview["accounting_only"] is True
    with pytest.raises(SystemExit):
        runner.main(["--start-request", str(request), "--no-aggregate-limits"])
    with pytest.raises(SystemExit):
        runner.main(["--start-request", str(request), "--no-aggregate-limits",
                     "--until-complete", "--deadline-seconds", "600"])
    with pytest.raises(SystemExit):
        runner.main(["--start-request", str(request), "--no-aggregate-limits",
                     "--until-complete", "--model-limit=4"])


def test_fresh_start_uses_only_selected_backup_and_restores_profiles(tmp_path, monkeypatch):
    policy = project(tmp_path, monkeypatch)
    original_policy = policy.read_bytes()
    (tmp_path / ".env").write_text(
        "GROQ_API_KEY=synthetic-primary\n"
        "GROQ_API_KEY_BACKUP=synthetic-backup\n"
    )
    monkeypatch.setenv("GROQ_API_KEY_BACKUP", "synthetic-backup")
    old = tmp_path / ".local/search-validation/old-case/state"
    old.mkdir(parents=True)
    (old / "unrelated-marker").write_text("unchanged")
    packets = []

    def http(_handler, _class, request, **_kwargs):
        assert request.full_url == "https://api.groq.com/openai/v1/chat/completions"
        packets.append(json.loads(request.data))
        return response(request, json.dumps(answer("A synthetic answer.", 1).payload).encode())

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", http)
    folder = tmp_path / ".local/search-validation/new-intermediate"
    start = {"request_id": "synthetic-intermediate-v1",
             "question": "Explain the synthetic research question.",
             "scope": "Use only synthetic evidence.", "agent_ids": ["agent-synthesis"],
             "credential_profile": "backup"}
    observed = runner.run_case(
        tmp_path, folder, execution_mode="offline_test", deadline_seconds=None,
        model_limit=None, token_limit=None, retrieval_limit=None,
        accounting_only=True, until_complete=True, start_request=start,
    )
    assert observed["outcome"] == "completed", observed
    assert observed["model_attempts"] == len(packets) == 1
    assert policy.read_bytes() == original_policy
    assert (old / "unrelated-marker").read_text() == "unchanged"
    selected = json.loads((folder / "case.json").read_text())
    assert selected["credential_profile"] == "backup"
    assert selected["max_model_calls"] is None
    assert selected["accounting_only"] is True
    private = json.loads((folder / "runtime" / runner.PROFILE).read_text())
    assert all(not private["profiles"][name]["enabled"] for name in ("primary", "backup"))
    assert all(private["profiles"][name]["allowed_run_ids"] == []
               for name in ("primary", "backup"))
    store = StateStore(folder / "state", read_only=True)
    saved_run = store.get("mission_run", observed["run_id"])
    assert saved_run and saved_run["body"]["profile"]["credentialProfile"] == "backup"
    assert len(store.records("mission_run")) == 1
