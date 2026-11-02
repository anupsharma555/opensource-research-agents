"""Natural-run controller on the full mission host with injected HTTP only."""

import builtins
import hashlib
import importlib.util
import io
import json
import multiprocessing
import shutil
import subprocess
import sys
import time
import urllib.request
import urllib.response
from email.message import Message
from pathlib import Path

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.evidence import intake
from ora.mission.service import MissionService
from ora.models import GroqSelectedAdapter, ModelSession
from ora.models.token_estimate import estimate_request_tokens
from ora.observability.evaluation import read_evaluation_observation
from ora.observability.run_reports import load_run_report
from ora.state import StateError, StateStore
from ora.state.store import canonical
from tests.mission.test_service import MODEL, answer, drain, tool

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("search_agent_runner", ROOT / "scripts/validate_search_agent.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


def _child_ora_path(sender):
    import ora

    sender.send(str(Path(ora.__file__).resolve()))
    sender.close()


def project(tmp_path, monkeypatch):
    for name in ("src", "roles", "runtime_skills"):
        (tmp_path / name).symlink_to(ROOT / name, target_is_directory=True)
    (tmp_path / ".env").write_text("GROQ_API_KEY=synthetic-groq\nEXA_API_KEY=synthetic-exa\n")
    (tmp_path / ".local").mkdir()
    policy = tmp_path / runner.PROFILE
    policy.write_text('{"version":1,"profiles":{"primary":{"enabled":false},"backup":{"enabled":false}}}')
    monkeypatch.setenv("GROQ_API_KEY", "synthetic-groq")
    monkeypatch.setenv("EXA_API_KEY", "synthetic-exa")
    return policy


def response(request, body, content_type="application/json", status=200):
    headers = Message()
    headers["Content-Type"] = content_type
    value = urllib.response.addinfourl(io.BytesIO(body), headers, request.full_url, status)
    value.reason = "fixture"
    value.msg = "fixture"
    return value


def test_no_call_default(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runner, "run_case", lambda *_a, **_kw: pytest.fail("live invoked"))
    output = tmp_path / "no-call"
    assert runner.main(["--output", str(output)]) == 0
    assert json.loads(capsys.readouterr().out)["planned_tokens"] == 55000
    assert not output.exists()


def test_token_only_cli_preview_keeps_finite_budget_and_rejects_conflicting_modes(
    tmp_path, monkeypatch, capsys,
):
    monkeypatch.setattr(runner, "run_case", lambda *_a, **_kw: pytest.fail("live invoked"))
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"start_request": {
        "request_id": "synthetic-token-only-preview",
        "question": "Summarize the supplied synthetic note.",
        "credential_profile": "backup",
    }}))
    flags = ["--start-request", str(request), "--no-model-call-cap",
             "--token-limit", "20000", "--retrieval-limit", "0"]
    assert runner.main(flags) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["model_calls"] is None
    assert shown["planned_tokens"] == 20000
    assert shown["retrieval_attempts"] == 0
    assert shown["credential_profile"] == "backup"
    assert shown["accounting_only"] is False
    with pytest.raises(SystemExit):
        runner.main([*flags, "--model-limit", "2"])
    with pytest.raises(SystemExit):
        runner.main(["--start-request", str(request), "--no-model-call-cap",
                     "--no-aggregate-limits", "--until-complete"])


@pytest.mark.parametrize("drain_settled", [True, False])
def test_token_only_cli_entrypoint_uses_full_catalog_and_settles_backup_case(
    tmp_path, monkeypatch, capsys, drain_settled,
):
    policy = project(tmp_path, monkeypatch)
    original_policy = policy.read_bytes()
    monkeypatch.setenv("GROQ_API_KEY_BACKUP", "synthetic-backup")
    (tmp_path / ".env").write_text(
        "GROQ_API_KEY=synthetic-groq\nGROQ_API_KEY_BACKUP=synthetic-backup\nEXA_API_KEY=synthetic-exa\n"
    )
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    real_run_case = runner.run_case

    def offline_case(*args, **kwargs):
        return real_run_case(*args, execution_mode="offline_test", **kwargs)

    monkeypatch.setattr(runner, "run_case", offline_case)
    if not drain_settled:
        from ora.mission.dispatch import MissionDispatchDriver

        monkeypatch.setattr(MissionDispatchDriver, "drain_active",
                            lambda self, *, timeout: False)
    sent = []

    def http(_handler, _class, outgoing, **_kwargs):
        assert outgoing.full_url == "https://api.groq.com/openai/v1/chat/completions"
        packet = json.loads(outgoing.data)
        sent.append(packet)
        assert packet["model"] == MODEL
        assert packet["reasoning_effort"] == "medium"
        assert packet["include_reasoning"] is True
        assert "request_capability" in json.dumps(packet["tools"])
        return response(outgoing, json.dumps(answer(
            "The synthetic note reports a bounded observation.", len(sent),
        ).payload).encode())

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", http)
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"start_request": {
        "request_id": "synthetic-token-only-backup",
        "question": "Summarize the supplied synthetic note.",
        "evidence_text": "Synthetic note: one bounded observation was recorded.",
        "agent_ids": ["agent-synthesis"], "credential_profile": "backup",
    }}))
    folder = tmp_path / ".local/search-validation/token-only-backup"
    assert runner.main(["--live", "--output", str(folder),
                        "--start-request", str(request), "--no-model-call-cap",
                        "--token-limit", "100000", "--retrieval-limit", "0",
                        "--deadline-seconds", "8"]) == (0 if drain_settled else 1)
    shown = json.loads(capsys.readouterr().out)
    budget = json.loads((folder / "budget.json").read_text())
    case = json.loads((folder / "case.json").read_text())
    assert shown["outcome"] == ("completed" if drain_settled else "dispatch_drain_incomplete")
    assert shown["dispatch_drain_status"] == ("settled" if drain_settled else "timed_out")
    assert shown["dispatch_drain_error"] is None
    assert shown["evaluation_observation_saved"] is drain_settled
    assert shown["shutdown_pending"] is False
    assert shown["model_attempts"] == len(sent) == 1
    assert shown["retrieval_attempts"] == 0
    assert budget["limits"] == {"model": None, "tokens": 100000, "retrieval": 0}
    assert budget["planned_tokens"] <= 100000
    assert case["max_model_calls"] is None
    assert case["credential_profile"] == "backup"
    assert policy.read_bytes() == original_policy
    frozen = json.loads((folder / "runtime" / runner.PROFILE).read_text())
    assert frozen["profiles"]["backup"]["enabled"] is False
    store = StateStore(folder / "state", read_only=True)
    intent = store.records("model_request_intent")[0]
    assert json.loads(store.read_blob(intent["body"]["blob"]["sha256"])) == sent[0]
    assert bool(store.get("evaluation_observation", f"evaluation-observation:{shown['run_id']}")) is drain_settled


def test_token_only_cli_denies_over_budget_request_before_http(tmp_path, monkeypatch, capsys):
    policy = project(tmp_path, monkeypatch)
    original_policy = policy.read_bytes()
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    real_run_case = runner.run_case

    def offline_case(*args, **kwargs):
        return real_run_case(*args, execution_mode="offline_test", **kwargs)

    monkeypatch.setattr(runner, "run_case", offline_case)
    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open",
                        lambda *_a, **_kw: pytest.fail("HTTP dispatched past token ceiling"))
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"start_request": {
        "request_id": "synthetic-token-only-denial",
        "question": "Summarize the supplied synthetic note.",
        "evidence_text": "Synthetic note: one bounded observation was recorded.",
        "agent_ids": ["agent-synthesis"], "credential_profile": "primary",
    }}))
    folder = tmp_path / ".local/search-validation/token-only-denial"
    assert runner.main(["--live", "--output", str(folder),
                        "--start-request", str(request), "--no-model-call-cap",
                        "--token-limit", "1", "--retrieval-limit", "0",
                        "--deadline-seconds", "8"]) == 1
    shown = json.loads(capsys.readouterr().out)
    budget = json.loads((folder / "budget.json").read_text())
    assert shown["outcome"] == "token_cap"
    assert shown["model_attempts"] == 0
    assert shown["settled_or_reserved_tokens"] == 0
    assert budget["limits"] == {"model": None, "tokens": 1, "retrieval": 0}
    assert policy.read_bytes() == original_policy


def test_token_only_entrypoint_drains_captured_provider_failure_and_keeps_unknown_usage(
    tmp_path, monkeypatch, capsys,
):
    policy = project(tmp_path, monkeypatch)
    original_policy = policy.read_bytes()
    monkeypatch.setattr(runner, "ROOT", tmp_path)
    real_run_case = runner.run_case

    def offline_case(*args, **kwargs):
        return real_run_case(*args, execution_mode="offline_test", **kwargs)

    monkeypatch.setattr(runner, "run_case", offline_case)
    sent = []

    def http(_handler, _class, outgoing, **_kwargs):
        assert outgoing.full_url == "https://api.groq.com/openai/v1/chat/completions"
        sent.append(json.loads(outgoing.data))
        return response(outgoing, json.dumps({"error": {
            "message": "Synthetic request exceeded the provider's size limit",
        }}).encode(), status=413)

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", http)
    request = tmp_path / "request.json"
    request.write_text(json.dumps({"start_request": {
        "request_id": "synthetic-drained-provider-error",
        "question": "Summarize the supplied synthetic note.",
        "evidence_text": "Synthetic note: one bounded observation was recorded.",
        "agent_ids": ["agent-synthesis"], "credential_profile": "primary",
    }}))
    folder = tmp_path / ".local/search-validation/drained-provider-error"
    assert runner.main(["--live", "--output", str(folder),
                        "--start-request", str(request), "--no-model-call-cap",
                        "--token-limit", "100000", "--retrieval-limit", "0",
                        "--deadline-seconds", "8"]) == 1
    shown = json.loads(capsys.readouterr().out)
    budget = json.loads((folder / "budget.json").read_text())
    assert shown["outcome"] == "model_http_error"
    assert shown["dispatch_drain_status"] == "settled"
    assert shown["dispatch_drain_error"] is None
    assert shown["evaluation_observation_saved"] is True
    assert shown["shutdown_pending"] is False
    assert shown["model_attempts"] == len(sent) == 1
    assert shown["retrieval_attempts"] == 0
    assert budget["limits"] == {"model": None, "tokens": 100000, "retrieval": 0}
    assert budget["model_attempts"][0]["status"] == 413
    assert budget["model_attempts"][0].get("actual_tokens") is None
    assert budget["planned_tokens"] == shown["settled_or_reserved_tokens"] > 0
    assert policy.read_bytes() == original_policy
    store = StateStore(folder / "state", read_only=True)
    receipts = store.records("model_response_receipt")
    assert len(receipts) == 1
    assert receipts[0]["body"]["status"] == "failed"
    assert receipts[0]["body"]["http_status"] == 413
    assert store.get("evaluation_observation", f"evaluation-observation:{shown['run_id']}")


def test_token_only_direct_runner_rejects_missing_aggregate_limit(tmp_path):
    folder = tmp_path / ".local/search-validation/unbounded-unapproved"
    with pytest.raises(ValueError, match="Finite token and retrieval limits"):
        runner.run_case(tmp_path, folder, execution_mode="offline_test",
                        model_limit=None, token_limit=None, retrieval_limit=0)
    assert not folder.exists()


@pytest.mark.parametrize("allow_vision", [False, True])
def test_explicit_vision_permission_reaches_run_and_shared_budget(tmp_path, monkeypatch, allow_vision):
    project(tmp_path, monkeypatch)
    request = {
        "request_id": "synthetic-vision-allowance", "question": "Describe the supplied label.",
        "evidence_text": "Synthetic label: 17 units.", "agent_ids": ["agent-synthesis"],
        "credential_profile": "primary", "allow_vision": allow_vision,
    }

    def http(_handler, _class, outgoing, **_kwargs):
        assert outgoing.full_url == "https://api.groq.com/openai/v1/chat/completions"
        return response(outgoing, json.dumps(answer("The label says 17 units.", 1).payload).encode())

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", http)
    folder = tmp_path / ".local/search-validation/vision-permission"
    if allow_vision:
        # A caller may already have the same path. Remove only this run's copy.
        monkeypatch.syspath_prepend(str(folder.resolve() / "runtime" / "src"))
    path_before = sys.path.copy()
    runner.run_case(tmp_path, folder, execution_mode="offline_test", start_request=request,
                    model_limit=1, token_limit=20_000, retrieval_limit=0, deadline_seconds=30)
    assert sys.path == path_before
    if not allow_vision:
        context = multiprocessing.get_context("spawn")
        receiver, sender = context.Pipe(duplex=False)
        child = context.Process(target=_child_ora_path, args=(sender,))
        try:
            child.start()
            sender.close()
            assert receiver.poll(10), "Spawned child did not report its ora import"
            assert Path(receiver.recv()).is_relative_to(ROOT)
            child.join(10)
            assert child.exitcode == 0
        finally:
            if child.is_alive():
                child.terminate()
                child.join(10)
            receiver.close()
            sender.close()
    case = json.loads((folder / "case.json").read_text())
    budget = json.loads((folder / "budget.json").read_text())
    assert case["allow_vision"] is allow_vision
    assert budget["limits"].get("allow_vision", False) is allow_vision
    store = StateStore(folder / "state", read_only=True)
    assert store.get("mission_run", case["run_id"])["body"]["profile"].get("allowVision", False) is allow_vision


def test_frozen_import_path_is_restored_when_case_setup_fails(tmp_path, monkeypatch):
    project(tmp_path, monkeypatch)
    folder = tmp_path / ".local/search-validation/failed-setup"
    frozen_path = str(folder.resolve() / "runtime" / "src")
    monkeypatch.syspath_prepend(frozen_path)
    path_before = sys.path.copy()
    import_module = builtins.__import__

    def fail_during_frozen_import(name, *args, **kwargs):
        if name == "ora.observability.config":
            raise RuntimeError("Synthetic frozen import interruption")
        return import_module(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", fail_during_frozen_import)
    with pytest.raises(RuntimeError, match="Synthetic frozen import interruption"):
        runner.run_case(tmp_path, folder, execution_mode="offline_test")
    assert sys.path == path_before


def test_fresh_live_bootstrap_keeps_frozen_package_after_path_cleanup(tmp_path, monkeypatch):
    project(tmp_path, monkeypatch)
    folder = tmp_path / ".local/search-validation/live-import-only"
    code = """
import importlib
import importlib.util
import json
import sys
from pathlib import Path

script, root, folder = map(Path, sys.argv[1:])
spec = importlib.util.spec_from_file_location('isolated_runner', script)
runner = importlib.util.module_from_spec(spec)
spec.loader.exec_module(runner)
proof = {}
original = runner._atomic_json
def stop_before_host(path, value):
    if path.name == 'case.json':
        import ora
        late = importlib.import_module('ora.state.views')
        proof.update(ora=str(Path(ora.__file__).resolve()),
                     late=str(Path(late.__file__).resolve()))
        raise RuntimeError('Stopped before host or provider work')
    return original(path, value)
runner._atomic_json = stop_before_host
try:
    runner.run_case(root, folder, execution_mode='live')
except RuntimeError as exc:
    assert str(exc) == 'Stopped before host or provider work'
else:
    raise AssertionError('Expected the import-only stop')
proof['path_after'] = str(folder / 'runtime' / 'src') in sys.path
print(json.dumps(proof))
"""
    result = subprocess.run(
        [sys.executable, "-c", code, str(ROOT / "scripts/validate_search_agent.py"),
         str(tmp_path), str(folder)], capture_output=True, text=True, check=True, timeout=30,
    )
    proof = json.loads(result.stdout)
    frozen_src = folder / "runtime" / "src"
    assert Path(proof["ora"]).is_relative_to(frozen_src)
    assert Path(proof["late"]).is_relative_to(frozen_src)
    assert proof["path_after"] is False


def test_nonboolean_vision_permission_rejected_before_private_runtime(tmp_path):
    folder = tmp_path / ".local/search-validation/invalid-vision"
    with pytest.raises(ValueError, match="explicit boolean"):
        runner.run_case(tmp_path, folder, execution_mode="offline_test", start_request={
            "request_id": "synthetic-invalid-vision", "question": "Read the label.",
            "credential_profile": "primary", "allow_vision": "true",
        })
    assert not folder.exists()


def test_supplied_exa_candidate_reaches_saved_request_without_network_in_default(
    tmp_path, monkeypatch, capsys,
):
    provenance = (
        "Synthetic Exa Contents candidate; provider extraction, not checked original HTML.\n"
        "Candidate says a sample value is 17 units; verify against the original page."
    )
    request = {
        "request_id": "synthetic-exa-candidate-runner", "question": "Check the candidate claim.",
        "scope": "Distinguish provider extraction from original-source evidence.",
        "evidence_text": provenance, "agent_ids": ["agent-synthesis"],
        "credential_profile": "primary",
    }
    plan = tmp_path / "start-request.json"
    plan.write_text(json.dumps({"start_request": request}))
    monkeypatch.setattr(runner, "run_case", lambda *_a, **_kw: pytest.fail("live invoked"))
    assert runner.main(["--start-request", str(plan)]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown["live"] is False and shown["model_calls"] == 8
    assert shown["planned_tokens"] == 55_000 and shown["retrieval_attempts"] == 4
    assert provenance not in json.dumps(shown)
    monkeypatch.undo()

    policy = project(tmp_path, monkeypatch)
    original_policy = policy.read_bytes()
    requests = []

    def http(_handler, _class, outgoing, **_kwargs):
        assert outgoing.full_url == "https://api.groq.com/openai/v1/chat/completions"
        packet = json.loads(outgoing.data)
        requests.append(packet)
        assert packet["model"] == MODEL
        source_messages = [json.loads(item["content"])["source_packets"]
                           for item in packet["messages"] if item.get("role") == "user"
                           and '"source_packets"' in item.get("content", "")]
        assert len(source_messages) == 1 and len(source_messages[0]) == 1
        assert provenance in source_messages[0][0]["text"]
        assert packet["messages"][0]["role"] == "developer"
        assert "request_capability" in json.dumps(packet.get("tools", []))
        return response(outgoing, json.dumps(answer(
            "The supplied candidate is unverified against original HTML.", 1,
        ).payload).encode())

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", http)
    folder = tmp_path / ".local/search-validation/exa-candidate"
    observed = runner.run_case(
        tmp_path, folder, execution_mode="offline_test", start_request=request,
        model_limit=1, token_limit=20_000, retrieval_limit=0, deadline_seconds=30,
    )
    assert len(requests) == observed["model_attempts"] == 1
    assert observed["retrieval_attempts"] == 0
    assert observed["peak_planned_tokens"] <= 20_000
    assert policy.read_bytes() == original_policy
    store = StateStore(folder / "state", read_only=True)
    run_id = json.loads((folder / "case.json").read_text())["run_id"]
    assert store.get("mission_run", run_id)["body"]["profile"]["evidenceText"] == provenance
    copies = [item for item in store.records("source_copy")
              if item["body"].get("source_version") == "submitted-v1"]
    assert len(copies) == 1
    encounters = store.records("source_encounter")
    assert any(item["body"].get("route") == "user_supplied" for item in encounters)
    intents = store.records("model_request_intent")
    assert len(intents) == 1
    saved = intents[0]["body"]
    exact = store.read_blob(saved["blob"]["sha256"])
    assert json.loads(exact) == requests[0]
    source_packets = [json.loads(item["content"])["source_packets"]
                      for item in json.loads(exact)["messages"] if item.get("role") == "user"
                      and '"source_packets"' in item.get("content", "")]
    assert len(source_packets) == 1 and source_packets[0][0]["text"].count(provenance) == 1


def test_followup_plan_dry_run_has_exact_limits_without_network(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(runner, "run_case", lambda *_a, **_kw: pytest.fail("live invoked"))
    plan = tmp_path / "followup-plan.json"
    plan.write_text(json.dumps({"followup_request": {
        "question": "Check only the retained source.",
    }}))
    assert runner.main([
        "--followup-state", str(tmp_path / "state"),
        "--followup-request", str(plan),
        "--model-limit", "4", "--token-limit", "40000",
        "--retrieval-limit", "0", "--deadline-seconds", "360",
    ]) == 0
    shown = json.loads(capsys.readouterr().out)
    assert shown == {
        "question": "Check only the retained source.", "live": False,
        "followup": True, "model_calls": 4, "planned_tokens": 40000,
        "retrieval_attempts": 0,
    }


def test_followup_rejects_output_inside_parent_state_before_copy(tmp_path):
    parent = tmp_path / ".local/search-validation/parent/state"
    parent.mkdir(parents=True)
    (parent / "state.sqlite3").touch()
    output = parent / "nested-child"
    with pytest.raises(ValueError, match="must not be inside the parent state"):
        runner.run_case(tmp_path, output, execution_mode="offline_test",
                        followup_state=parent,
                        followup_request={"request_id": "synthetic-child"})
    assert not output.exists()


def test_followup_uses_copied_closed_parent_with_zero_external_retrieval(tmp_path, monkeypatch):
    policy = project(tmp_path, monkeypatch)
    original_policy = policy.read_bytes()
    parent_state = tmp_path / ".local/search-validation/closed-parent/state"
    parent_state.mkdir(parents=True)
    parent_store = StateStore(parent_state)
    parent_session = ModelSession(
        parent_store, ROOT,
        adapter=GroqSelectedAdapter(ROOT, transport=lambda *_a, **_kw: answer(
            "The synthetic retained source notes a winter-heating tradeoff.", 1,
        )),
    )
    parent_host = MissionService(
        ROOT, parent_state, model_session=parent_session,
        execution_mode="offline_test", start_supervisor=False,
    )
    try:
        parent = parent_host.start(LiveStartRequest(
            request_id="closed-parent-fixture", question="Summarize the synthetic source.",
            evidence_text="Synthetic retained source: winter heating may increase.",
            agent_ids=["agent-synthesis"],
            allow_vision=True,
        ))
        drain(parent_host, parent.run_id)
        snapshot = parent_host.state(parent.run_id).snapshot
        assert snapshot.mission.status == "completed"
        target = snapshot.artifacts[0]
        parent_host.store.transact(
            "synthetic-inherited-preparation-diagnostic", {},
            lambda tx: tx.put("mission_host_diagnostic", "historical-parent-diagnostic", {
                "schema": "ora.mission_preparation_diagnostic.v1",
                "mission_id": parent_host._run(parent.run_id)["body"]["mission_id"],
                "run_id": parent.run_id, "category": "local_context_preparation",
                "message": "Synthetic historical parent diagnostic",
            }), mission_id=parent_host._run(parent.run_id)["body"]["mission_id"],
        )
    finally:
        parent_host.close()
    parent_digest = runner._state_digest(parent_state)
    request_data = {
        "request_id": "synthetic-followup-child", "parent_run_id": parent.run_id,
        "parent_snapshot_id": snapshot.snapshot_id, "target_id": target.id,
        "target_version": target.version,
        "question": "Explain the retained source's winter-heating point in at most 80 words.",
        "scope": "Use only the retained source; no new web research.",
        "agent_ids": ["agent-synthesis"],
        "credential_profile": "primary",
    }
    packets, urls = [], []

    def http(_handler, _class, request, **_kwargs):
        urls.append(request.full_url)
        assert request.full_url == "https://api.groq.com/openai/v1/chat/completions"
        packet = json.loads(request.data)
        packets.append(packet)
        assert request_data["question"] in json.dumps(packet)
        return response(request, json.dumps(answer(
            "The retained synthetic source says winter heating may increase.",
            len(packets),
        ).payload).encode())

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", http)
    folder = tmp_path / ".local/search-validation/followup-child"
    observed = runner.run_case(
        tmp_path, folder, execution_mode="offline_test", deadline_seconds=30,
        model_limit=4, token_limit=40_000, retrieval_limit=0,
        followup_state=parent_state, followup_request=request_data,
    )
    assert observed["outcome"] == "completed", observed
    assert observed["model_attempts"] == len(packets) == 1
    assert observed["retrieval_attempts"] == 0
    assert observed["parent_state_unchanged"] is True
    assert observed["parent_lineage"] == {
        key: request_data[key] for key in (
            "parent_run_id", "parent_snapshot_id", "target_id", "target_version"
        )
    }
    assert runner._state_digest(parent_state) == parent_digest
    assert policy.read_bytes() == original_policy
    assert urls == ["https://api.groq.com/openai/v1/chat/completions"]
    selected = json.loads((folder / "case.json").read_text())
    assert selected["allow_vision"] is False
    assert json.loads((folder / "budget.json").read_text())["limits"].get("allow_vision", False) is False
    assert selected["max_retrieval_attempts"] == 0
    assert selected["parent_lineage"] == observed["parent_lineage"]
    assert selected["run_id"] != parent.run_id
    frozen_policy = json.loads((folder / "runtime" / runner.PROFILE).read_text())
    assert frozen_policy["profiles"]["primary"]["allowed_run_ids"] == []
    copied = StateStore(folder / "state", read_only=True)
    assert copied.get("mission_run", parent.run_id)["body"]["profile"]["allowVision"] is True
    assert copied.get("mission_run", selected["run_id"])["body"]["profile"].get("allowVision", False) is False
    assert copied.get("mission_run", parent.run_id)["body"]["status"] == "completed"
    assert copied.get("mission_run", selected["run_id"])["body"]["parent_run_id"] == parent.run_id


def _stopped_413_case(tmp_path, monkeypatch, *, finite_token_limit=None):
    policy = project(tmp_path, monkeypatch)
    request = {"request_id": "synthetic-resume-413", "question": "Use the synthetic roof note.",
               "evidence_text": "Synthetic retained roof note: reflective coating may reduce heat gain.",
               "agent_ids": ["agent-synthesis"], "credential_profile": "primary",
               "allow_vision": False}
    attempts = []

    def rejected(_handler, _class, outgoing, **_kwargs):
        attempts.append(json.loads(outgoing.data))
        reply = response(outgoing, json.dumps({"error": {
            "message": "Requested 8253 tokens; limit 8000",
        }}).encode(), status=413)
        reply.headers["x-ratelimit-limit-tokens"] = "8000"
        reply.headers["x-ratelimit-remaining-tokens"] = "8000"
        reply.headers["x-ratelimit-reset-tokens"] = "1ms"
        reply.headers["retry-after"] = "0"
        return reply

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", rejected)
    source = tmp_path / ".local/search-validation/stopped-413"
    accounting_only = finite_token_limit is None
    result = runner.run_case(
        tmp_path, source, execution_mode="offline_test", start_request=request,
        accounting_only=accounting_only, until_complete=accounting_only,
        model_limit=None, token_limit=finite_token_limit,
        retrieval_limit=None if accounting_only else 0,
        deadline_seconds=None if accounting_only else 1800,
    )
    assert result["outcome"] == "model_http_error"
    assert result["model_attempts"] == len(attempts) == 1
    assert result["mission_status"] == "active"
    assert result["evaluation_observation_saved"] is True
    assert policy.read_bytes() == (tmp_path / runner.PROFILE).read_bytes()
    return source, request, policy


def test_explicit_resume_copies_413_run_and_preserves_prior_bytes_and_accounting(
    tmp_path, monkeypatch,
):
    source, request, policy = _stopped_413_case(tmp_path, monkeypatch)
    protected = {name: (source / name).read_bytes() for name in (
        "case.json", "budget.json", "observation.json", "source-manifest.json",
    )}
    protected["private_profile"] = (source / "runtime" / runner.PROFILE).read_bytes()
    original = StateStore(source / "state", read_only=True)
    old_intent = original.records("model_request_intent")[0]
    old_receipt = original.records("model_response_receipt")[0]
    old_request_bytes = original.read_blob(old_intent["body"]["blob"]["sha256"])
    old_response_bytes = original.read_blob(old_receipt["body"]["blob"]["sha256"])
    old_observation = original.records("evaluation_observation")[0]
    state_digest = runner._state_digest(source / "state")
    root_profile = policy.read_bytes()
    seen = []

    def accepted(_handler, _class, outgoing, **_kwargs):
        seen.append(json.loads(outgoing.data))
        return response(outgoing, json.dumps(answer("The synthetic note was retained.", 2).payload).encode())

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", accepted)
    actual_time = time.time
    clock_start = time.monotonic()
    # Advance only the synthetic test clock through the saved capacity window.
    monkeypatch.setattr(time, "time", lambda: actual_time() + 120
                        + 100 * (time.monotonic() - clock_start))
    output = tmp_path / ".local/search-validation/resumed-413"
    plan = tmp_path / "resume-request.json"
    plan.write_text(json.dumps({"start_request": request}))
    actual_run_case = runner.run_case
    with monkeypatch.context() as cli_patch:
        cli_patch.setattr(runner, "ROOT", tmp_path)
        cli_patch.setattr(runner, "run_case", lambda *args, **kwargs: actual_run_case(
            *args, execution_mode="offline_test", **kwargs,
        ))
        assert runner.main([
            "--resume-case", str(source), "--start-request", str(plan),
            "--output", str(output), "--no-aggregate-limits", "--until-complete", "--live",
        ]) == 0
    result = json.loads((output / "observation.json").read_text())
    assert result["outcome"] == "completed", result
    assert request["evidence_text"] in json.dumps(seen[0])
    assert request["evidence_text"] in old_request_bytes.decode()
    assert result["run_id"] == old_observation["body"]["run_id"]
    assert result["phase_model_attempts"] == len(seen) == 1
    assert result["model_attempts"] == 2
    assert result["phase_accounting"]["model_http_attempts"] == 1
    assert result["cumulative_accounting"]["model_http_attempts"] == 2
    assert result["cumulative_accounting"]["unknown_model_reservations"] > 0
    assert result["cumulative_accounting"]["known_model_tokens"] == 11
    assert result["resume_source_unchanged"] is True
    assert result["prior_evaluation_observation_preserved"] is True
    assert result["evaluation_observation_saved"] is True
    assert result["resume_integrity_complete"] is True
    assert result["elapsed_seconds"] == (
        json.loads((source / "observation.json").read_text())["elapsed_seconds"]
        + result["phase_elapsed_seconds"]
    )
    assert (output / "prior-cumulative-budget.json").read_bytes() == protected["budget.json"]
    phase_budget = json.loads((output / "budget.json").read_text())
    aggregate = json.loads((output / "cumulative-budget.json").read_text())
    assert [row["number"] for row in aggregate["model_attempts"]] == [1, 2]
    assert phase_budget["model_attempts"][0]["number"] == 1
    report = load_run_report(output, kind="natural_agent")
    assert report["counts"]["model_http_attempts"] == 2
    assert report["counts"]["model_usage_unknown_attempts"] == 1
    assert report["attempts"][0]["phase"] == "prior"
    assert report["attempts"][1]["phase"] == "run"
    assert report["timing"]["observed_window_basis"] == (
        "segmented_evaluation_observation_controller"
    )
    assert report["timing"]["runner_total_basis"] == (
        "sum_of_runner_segments_after_each_shutdown"
    )
    assert report["timing"]["between_model_attempt_gap_basis"] == (
        "includes_interphase_pause"
    )
    for filename, mutate in (
        ("prior-cumulative-budget.json", lambda value: value.update(planned_tokens=0)),
        ("cumulative-budget.json", lambda value: value["model_attempts"][0].update(status=200)),
        ("budget.json", lambda value: value["model_attempts"][0].update(status=413)),
        ("observation.json", lambda value: value["controller_segments"][-1].update(
            controller_elapsed_seconds=0)),
    ):
        target = output / filename
        unchanged = target.read_bytes()
        tampered = json.loads(unchanged)
        mutate(tampered)
        target.write_text(json.dumps(tampered))
        with pytest.raises(StateError):
            load_run_report(output, kind="natural_agent")
        target.write_bytes(unchanged)
    assert load_run_report(output, kind="natural_agent")["counts"] == report["counts"]
    assert runner._state_digest(source / "state") == state_digest
    assert policy.read_bytes() == root_profile
    assert all((source / name).read_bytes() == contents for name, contents in protected.items()
               if name != "private_profile")
    assert (source / "runtime" / runner.PROFILE).read_bytes() == protected["private_profile"]
    copied = StateStore(output / "state", read_only=True)
    assert len(copied.records("mission_run")) == 1
    assert copied.get("mission_run", result["run_id"])["body"]["status"] == "completed"
    assert copied.get("model_request_intent", old_intent["id"]) == old_intent
    assert copied.read_blob(old_intent["body"]["blob"]["sha256"]) == old_request_bytes
    assert copied.get("model_response_receipt", old_receipt["id"]) == old_receipt
    assert copied.read_blob(old_receipt["body"]["blob"]["sha256"]) == old_response_bytes
    assert copied.get("evaluation_observation", old_observation["id"], 1) == old_observation
    current = read_evaluation_observation(copied, result["run_id"])
    assert current["outcome"] == "completed"
    assert current["model_http_attempts"] == 2
    assert current["retrieval_http_attempts"] == 0
    assert current["elapsed_seconds"] >= old_observation["body"]["elapsed_seconds"]


def test_finite_token_resume_reuses_run_and_loads_cumulative_report(
    tmp_path, monkeypatch, capsys,
):
    source, request, policy = _stopped_413_case(
        tmp_path, monkeypatch, finite_token_limit=50_000,
    )
    source_files = {name: (source / name).read_bytes() for name in (
        "case.json", "budget.json", "observation.json", "source-manifest.json",
    )}
    first_budget = json.loads(source_files["budget.json"])
    assert first_budget["planned_tokens"] < 50_000
    original = StateStore(source / "state", read_only=True)
    first_intent = original.records("model_request_intent")[0]
    first_receipt = original.records("model_response_receipt")[0]
    state_digest = runner._state_digest(source / "state")
    plan = tmp_path / "finite-resume-request.json"
    plan.write_text(json.dumps({"start_request": request}))
    assert runner.main([
        "--resume-case", str(source), "--start-request", str(plan),
        "--no-model-call-cap", "--token-limit", "50000",
        "--retrieval-limit", "0", "--deadline-seconds", "1800",
    ]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["planned_tokens"] == 50_000 and preview["model_calls"] is None
    seen = []

    def accepted(_handler, _class, outgoing, **_kwargs):
        seen.append(json.loads(outgoing.data))
        return response(outgoing, json.dumps(answer("The retained synthetic note was used.", 2).payload).encode())

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", accepted)
    actual_time = time.time
    clock_start = time.monotonic()
    monkeypatch.setattr(time, "time", lambda: actual_time() + 120
                        + 100 * (time.monotonic() - clock_start))
    state_init = StateStore.__init__
    def accelerated_store(self, *args, **kwargs):
        kwargs.setdefault("clock", lambda: time.time())
        return state_init(self, *args, **kwargs)
    monkeypatch.setattr(StateStore, "__init__", accelerated_store)
    output = tmp_path / ".local/search-validation/finite-resumed-413"
    resumed = runner.run_case(
        tmp_path, output, execution_mode="offline_test", start_request=request,
        resume_case=source, accounting_only=False, until_complete=False,
        model_limit=None, token_limit=50_000, retrieval_limit=0,
        deadline_seconds=1800,
    )
    phase = json.loads((output / "budget.json").read_text())
    cumulative = json.loads((output / "cumulative-budget.json").read_text())
    assert resumed["outcome"] == "completed" and resumed["resume_integrity_complete"]
    assert resumed["run_id"] == json.loads(source_files["case.json"])["run_id"]
    assert resumed["phase_model_attempts"] == len(seen) == 1
    assert phase["limits"] == {"model": None,
                               "tokens": 50_000 - first_budget["planned_tokens"],
                               "retrieval": 0}
    assert cumulative["limits"] == first_budget["limits"]
    assert cumulative["planned_tokens"] == (first_budget["planned_tokens"]
                                             + phase["planned_tokens"])
    assert cumulative["model_attempts"][0].get("actual_tokens") is None
    assert resumed["cumulative_accounting"]["unknown_model_reservations"] == (
        first_budget["planned_tokens"])
    assert load_run_report(output, kind="natural_agent")["counts"]["model_http_attempts"] == 2
    copied = StateStore(output / "state", read_only=True)
    assert copied.get("model_request_intent", first_intent["id"]) == first_intent
    assert copied.get("model_response_receipt", first_receipt["id"]) == first_receipt
    assert runner._state_digest(source / "state") == state_digest
    assert all((source / name).read_bytes() == value for name, value in source_files.items())
    assert policy.read_bytes() == (tmp_path / runner.PROFILE).read_bytes()
    for suffix, changed in (
        ("higher-token", {"token_limit": 50_001}),
        ("retrieval", {"retrieval_limit": 1}),
        ("call-cap", {"model_limit": 2}),
        ("unbounded", {"accounting_only": True, "until_complete": True,
                       "token_limit": None, "retrieval_limit": None,
                       "deadline_seconds": None}),
    ):
        unsafe = tmp_path / f".local/search-validation/finite-unsafe-{suffix}"
        options = {"accounting_only": False, "until_complete": False,
                   "model_limit": None, "token_limit": 50_000,
                   "retrieval_limit": 0, "deadline_seconds": 1800, **changed}
        with pytest.raises(ValueError, match="differs from stopped case"):
            runner.run_case(tmp_path, unsafe, execution_mode="offline_test",
                            start_request=request, resume_case=source, **options)
        assert not unsafe.exists()


def test_finite_resume_denies_new_request_locally_when_reservation_exceeds_remainder(
    tmp_path, monkeypatch,
):
    source, request, _ = _stopped_413_case(
        tmp_path, monkeypatch, finite_token_limit=20_000,
    )
    first_budget = json.loads((source / "budget.json").read_text())
    assert first_budget["planned_tokens"] < 20_000
    seen = []

    def unexpected(_handler, _class, outgoing, **_kwargs):
        seen.append(outgoing)
        raise AssertionError("Finite continuation dispatched beyond its remaining budget")

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", unexpected)
    actual_time = time.time
    clock_start = time.monotonic()
    monkeypatch.setattr(time, "time", lambda: actual_time() + 120
                        + 100 * (time.monotonic() - clock_start))
    state_init = StateStore.__init__
    def accelerated_store(self, *args, **kwargs):
        kwargs.setdefault("clock", lambda: time.time())
        return state_init(self, *args, **kwargs)
    monkeypatch.setattr(StateStore, "__init__", accelerated_store)
    output = tmp_path / ".local/search-validation/finite-blocked-413"
    resumed = runner.run_case(
        tmp_path, output, execution_mode="offline_test", start_request=request,
        resume_case=source, accounting_only=False, until_complete=False,
        model_limit=None, token_limit=20_000, retrieval_limit=0,
        deadline_seconds=1800,
    )
    assert seen == []
    assert resumed["outcome"] == "token_cap"
    assert resumed["phase_model_attempts"] == 0
    assert resumed["cumulative_accounting"]["unknown_model_reservations"] == (
        first_budget["planned_tokens"])
    assert resumed["settled_or_reserved_tokens"] == first_budget["planned_tokens"]
    assert json.loads((output / "budget.json").read_text())["limits"]["tokens"] == (
        20_000 - first_budget["planned_tokens"])
    assert load_run_report(output, kind="natural_agent")["counts"]["model_http_attempts"] == 1


def test_finite_resume_keeps_reported_provider_overrun_in_readable_ledger(
    tmp_path, monkeypatch,
):
    source, request, _ = _stopped_413_case(
        tmp_path, monkeypatch, finite_token_limit=40_000,
    )
    prior = json.loads((source / "budget.json").read_text())
    remaining = 40_000 - prior["planned_tokens"]
    assert remaining > 16_000
    seen = []

    def overrun(_handler, _class, outgoing, **_kwargs):
        seen.append(outgoing)
        payload = answer("The source remains available.", 2).payload
        payload["usage"] = {"prompt_tokens": remaining + 1,
                            "completion_tokens": 6,
                            "total_tokens": remaining + 7}
        return response(outgoing, json.dumps(payload).encode())

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", overrun)
    actual_time = time.time
    clock_start = time.monotonic()
    monkeypatch.setattr(time, "time", lambda: actual_time() + 120
                        + 100 * (time.monotonic() - clock_start))
    state_init = StateStore.__init__
    def accelerated_store(self, *args, **kwargs):
        kwargs.setdefault("clock", lambda: time.time())
        return state_init(self, *args, **kwargs)
    monkeypatch.setattr(StateStore, "__init__", accelerated_store)
    output = tmp_path / ".local/search-validation/finite-overrun-413"
    resumed = runner.run_case(
        tmp_path, output, execution_mode="offline_test", start_request=request,
        resume_case=source, accounting_only=False, until_complete=False,
        model_limit=None, token_limit=40_000, retrieval_limit=0,
        deadline_seconds=1800,
    )
    assert len(seen) == 1
    assert resumed["outcome"] == "reported_token_cap"
    assert resumed["phase_model_attempts"] == 1
    assert resumed["settled_or_reserved_tokens"] == prior["planned_tokens"] + remaining + 7
    assert resumed["settled_or_reserved_tokens"] > 40_000
    assert resumed["cumulative_accounting"]["unknown_model_reservations"] == prior["planned_tokens"]
    report = load_run_report(output, kind="natural_agent")
    assert report["counts"]["model_http_attempts"] == 2
    assert report["counts"]["model_usage_unknown_attempts"] == 1
    with pytest.raises(ValueError, match="recognized provider rejection"):
        runner._resume_source(
            tmp_path, output, request, "offline_test", model_limit=None,
            token_limit=40_000, retrieval_limit=0, deadline_seconds=1800,
            accounting_only=False, until_complete=False,
        )


def test_resume_requires_explicit_mode_and_rejects_changed_or_unsettled_source(
    tmp_path, monkeypatch, capsys,
):
    source, request, _policy = _stopped_413_case(tmp_path, monkeypatch)
    plan = tmp_path / "resume-start.json"
    plan.write_text(json.dumps({"start_request": request}))
    with monkeypatch.context() as preview_patch:
        preview_patch.setattr(runner, "run_case",
                              lambda *_a, **_kw: pytest.fail("Preview dispatched"))
        assert runner.main(["--resume-case", str(source), "--start-request", str(plan),
                            "--no-aggregate-limits", "--until-complete"]) == 0
        assert json.loads(capsys.readouterr().out)["resume_case"] == str(source)
        with pytest.raises(SystemExit):
            runner.main(["--resume-case", str(source), "--start-request", str(plan), "--live"])
        with pytest.raises(SystemExit):
            runner.main(["--resume-case", str(source), "--no-aggregate-limits",
                         "--until-complete", "--live"])
    changed = {**request, "question": "Different synthetic question."}
    output = tmp_path / ".local/search-validation/unsafe-resume"
    # The saved source's accounting mode and limits are also fenced by runtime
    # preflight before output or dispatch, independently of CLI validation.
    with pytest.raises(ValueError, match="differs from stopped case"):
        runner.run_case(tmp_path, output, execution_mode="offline_test",
                        start_request=request, resume_case=source,
                        accounting_only=False, until_complete=False,
                        model_limit=8, token_limit=55_000, retrieval_limit=4,
                        deadline_seconds=600)
    assert not output.exists()
    for altered in (changed, {**request, "credential_profile": "backup"},
                    {**request, "scope": "Different scope."}):
        with pytest.raises(ValueError, match="differs from stopped case|exact active run"):
            runner.run_case(tmp_path, output, execution_mode="offline_test",
                            start_request=altered, resume_case=source, accounting_only=True,
                            until_complete=True, model_limit=None, token_limit=None,
                            retrieval_limit=None, deadline_seconds=None)
    assert not output.exists()
    bad = tmp_path / ".local/search-validation/unsettled-413"
    shutil.copytree(source, bad, symlinks=True)
    observation_file = bad / "observation.json"
    budget_file = bad / "budget.json"
    observation_bytes, budget_bytes = observation_file.read_bytes(), budget_file.read_bytes()
    for field, value in (("shutdown_pending", True), ("mission_status", "completed"),
                         ("mission_status", "cancelled")):
        observation = json.loads(observation_bytes)
        observation[field] = value
        observation_file.write_text(json.dumps(observation))
        with pytest.raises(ValueError, match="closed, settled"):
            runner.run_case(tmp_path, output, execution_mode="offline_test",
                            start_request=request, resume_case=bad, accounting_only=True,
                            until_complete=True, model_limit=None, token_limit=None,
                            retrieval_limit=None, deadline_seconds=None)
    observation_file.write_bytes(observation_bytes)
    budget = json.loads(budget_bytes)
    budget["model_attempts"][-1]["state"] = "attempted"
    budget_file.write_text(json.dumps(budget))
    with pytest.raises(ValueError, match="closed, settled"):
        runner.run_case(tmp_path, output, execution_mode="offline_test",
                        start_request=request, resume_case=bad, accounting_only=True,
                        until_complete=True, model_limit=None, token_limit=None,
                        retrieval_limit=None, deadline_seconds=None)
    budget_file.write_bytes(budget_bytes)
    extra = StateStore(bad / "state")
    extra.transact("other-run", {}, lambda tx: tx.put("mission_run", "run-other", {
        "mission_id": "mission-other", "status": "active",
    }), mission_id="mission-other")
    with pytest.raises(ValueError, match="exact active run"):
        runner.run_case(tmp_path, output, execution_mode="offline_test",
                        start_request=request, resume_case=bad, accounting_only=True,
                        until_complete=True, model_limit=None, token_limit=None,
                        retrieval_limit=None, deadline_seconds=None)
    assert not output.exists()


@pytest.mark.parametrize("limit", [8, 2])
def test_full_catalog_web_source_and_budget_boundary(tmp_path, monkeypatch, limit):
    policy = project(tmp_path, monkeypatch)
    original = policy.read_bytes()
    packets, requests, selected = [], [], {}
    passage = "Synthetic checked source: the coating reflects sunlight; winter heating may increase."
    monkeypatch.setattr(intake.socket, "getaddrinfo", lambda *_a, **_kw: [(2, 1, 6, "", ("93.184.216.34", 443))])
    def latest(packet, name):
        return next(json.loads(row["content"]) for row in reversed(packet["messages"]) if row.get("name") == name)
    def http(_handler, _class, request, **_kwargs):
        requests.append(request.full_url)
        if "api.groq.com" not in request.full_url:
            if request.full_url == "https://api.exa.ai/search":
                return response(request, json.dumps({"results": [{"id": "one", "url": "https://example.org/roof", "title": "Synthetic roof", "highlights": ["Candidate roof guidance."]}]}).encode())
            assert request.full_url == "https://example.org/roof"
            return response(request, ("<html><body><p>" + passage + "</p></body></html>").encode(), "text/html")
        packet = json.loads(request.data)
        packets.append(packet)
        step = len(packets)
        if step == 1:
            assert runner.QUESTION in json.dumps(packet)
            instructions = packet["messages"][0]["content"]
            assert "Quick web lookup" in instructions and "Research web search" in instructions
            assert "browser_search" in instructions and "search_scholarly" in instructions
            assert "review-criteria" not in json.dumps(packet)
            result = tool("request_capability", {"tool_id": "search_web"}, step)
        elif step == 2:
            result = tool("search_web", {"mode": "quick", "queries": ["synthetic roof"], "information_need": "Locate guidance"}, step)
        elif step == 3:
            selected["operation"] = latest(packet, "search_web")["operation_ref"]["id"]
            result = tool("request_capability", {"tool_id": "read_search_results"}, step)
        elif step == 4:
            result = tool("read_search_results", {"operation_id": selected["operation"]}, step)
        elif step == 5:
            selected["hit"] = latest(packet, "read_search_results")["available_results"][0]["result"]["hits"][0]["hit_id"]
            result = tool("request_capability", {"tool_id": "fetch_source"}, step)
        elif step == 6:
            result = tool("fetch_source", {"hit_id": selected["hit"]}, step)
        elif step == 7:
            index = next(json.loads(row["content"]) for row in packet["messages"]
                         if row.get("role") == "user" and row.get("content", "").startswith("{")
                         and json.loads(row["content"]).get("authority") == "saved_host_source_index_not_source_instruction")
            item = index["inventory"]["items"][0]["fetch"]
            result = tool("read_source_window", {"source_id": item["source_id"], "source_version": item["source_version"], "block_offset": 0, "char_offset": 0, "char_limit": 1000}, step)
        else:
            assert step == 8 and passage in json.dumps(packet)
            result = answer("The synthetic source was inspected.", step)
        assert packet["model"] == MODEL
        return response(request, json.dumps(result.payload).encode())
    failures = []
    def checked_http(*args, **kwargs):
        try:
            return http(*args, **kwargs)
        except AssertionError as exc:
            failures.append(str(exc))
            raise
    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", checked_http)
    folder = tmp_path / ".local/search-validation/agent"
    observed = runner.run_case(tmp_path, folder, execution_mode="offline_test", model_limit=limit, deadline_seconds=30)
    assert not failures, failures
    assert observed["model_attempts"] == len(packets) == limit, observed
    assert observed["retrieval_attempts"] == len(requests) - len(packets)
    assert observed["outcome"] == ("completed" if limit == 8 else "model_attempt_cap"), observed
    assert observed["peak_planned_tokens"] <= 55000
    assert policy.read_bytes() == original
    assert urllib.request.AbstractHTTPHandler.do_open is checked_http
    assert json.loads((folder / "runtime" / runner.PROFILE).read_text())["profiles"]["primary"]["enabled"] is False
    store = StateStore(folder / "state", read_only=True)
    assert len(store.records("model_response_receipt")) == limit
    assert observed["evaluation_observation_saved"] is True
    observation = store.get("evaluation_observation", f"evaluation-observation:{observed['run_id']}")
    assert observation["body"]["outcome"] == observed["outcome"]
    assert observation["body"]["observation_stopped"] is True
    assert observation["body"]["model_http_attempts"] == len(packets)
    assert observation["body"]["retrieval_http_attempts"] == len(requests) - len(packets)
    assert observation["body"]["mission_id"] == store.get("mission_run", observed["run_id"])["body"]["mission_id"]
    assert observed["controller_elapsed_seconds"] == observation["body"]["elapsed_seconds"]
    assert observed["elapsed_seconds"] >= observed["controller_elapsed_seconds"]
    assert observed["post_controller_finalization_seconds"] == (
        observed["elapsed_seconds"] - observed["controller_elapsed_seconds"]
    )
    assert store.get("mission_run", observed["run_id"])["body"]["status"] == observed["mission_status"]
    if limit == 8:
        assert observed["retrieval_attempts"] == 2
        assert store.records("source_read_window")


def _synthetic_parent(tmp_path):
    for name in ("src", "roles", "runtime_skills"):
        (tmp_path / name).unlink()
        shutil.copytree(ROOT / name, tmp_path / name)
    profile_path = tmp_path / runner.PROFILE
    original_profile = profile_path.read_bytes()
    policy = json.loads(original_profile)
    parent_run_id = "run-" + hashlib.sha256(json.dumps(
        ("synthetic-resume-parent",), separators=(",", ":")
    ).encode()).hexdigest()[:32]
    policy["profiles"]["primary"].update(enabled=True, allowed_run_ids=[parent_run_id])
    profile_path.write_text(json.dumps(policy))
    state = tmp_path / ".local/search-validation/parent/state"
    state.mkdir(parents=True)
    store = StateStore(state)
    session = ModelSession(store, tmp_path, adapter=GroqSelectedAdapter(
        tmp_path, transport=lambda *_a, **_kw: answer("The synthetic source supports a small note.", 1),
    ))
    host = MissionService(tmp_path, state, model_session=session,
                          execution_mode="offline_test", start_supervisor=False)
    try:
        parent = host.start(LiveStartRequest(
            request_id="synthetic-resume-parent", question="Summarize the synthetic source.",
            evidence_text="Synthetic source: a small note is supported.",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, parent.run_id)
        snapshot = host.state(parent.run_id).snapshot
        assert snapshot.mission.status == "completed"
        target = snapshot.artifacts[0]
    finally:
        host.close()
        profile_path.write_bytes(original_profile)
    return state, parent.run_id, snapshot.snapshot_id, target


def test_full_followup_400_resume_recovers_without_replaying_parent(tmp_path, monkeypatch):
    policy = project(tmp_path, monkeypatch)
    original_policy = policy.read_bytes()
    parent_state, parent_id, snapshot_id, target = _synthetic_parent(tmp_path)
    parent_digest = runner._state_digest(parent_state)
    request = {
        "request_id": "synthetic-resume-followup-400",
        "parent_run_id": parent_id, "parent_snapshot_id": snapshot_id,
        "target_id": target.id, "target_version": target.version,
        "question": "Write a brief note using the retained synthetic source.",
        "scope": "Use the retained source only.", "agent_ids": ["agent-synthesis"],
        "credential_profile": "primary", "allow_vision": False,
    }
    rejected_packets = []

    def reject(_handler, _class, outgoing, **_kwargs):
        packet = json.loads(outgoing.data)
        rejected_packets.append(packet)
        attached = {item.get("function", {}).get("name") for item in packet.get("tools", [])}
        assert "read_source_inventory" not in attached
        error = {"error": {"type": "invalid_request_error", "code": "tool_use_failed",
                 "message": ("Tool call validation failed: tool call validation failed: "
                             "attempted to call tool 'read_source_inventory' "
                             "which was not in request.tools")}}
        return response(outgoing, json.dumps(error).encode(), status=400)

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", reject)
    source = tmp_path / ".local/search-validation/child-stopped-400"
    stopped = runner.run_case(
        tmp_path, source, execution_mode="offline_test", followup_state=parent_state,
        followup_request=request, accounting_only=True, until_complete=True,
        model_limit=None, token_limit=None, retrieval_limit=None, deadline_seconds=None,
    )
    assert stopped["outcome"] == "model_http_error" and stopped["model_attempts"] == 1
    assert stopped["parent_state_unchanged"] is True
    assert runner._state_digest(parent_state) == parent_digest
    source_files = {name: (source / name).read_bytes() for name in (
        "case.json", "budget.json", "observation.json",
    )}
    saved = StateStore(source / "state", read_only=True)
    child_id = json.loads((source / "case.json").read_text())["run_id"]
    parent_record = saved.get("mission_run", parent_id)
    parent_mission_id = parent_record["body"]["mission_id"]
    prior_parent_calls = [row for row in saved.records("agent_call")
                          if row["body"].get("mission_id") == parent_mission_id]
    assert prior_parent_calls
    prior_parent_report = saved.get("report", target.id, target.version)
    assert prior_parent_report is not None
    rejected_receipt = next(row for row in saved.records("model_response_receipt")
                            if row["body"].get("http_status") == 400)
    assert rejected_receipt["body"]["error_kind"] == "unregistered_tool_function"
    control = next(row for row in saved.records("mission_task_control")
                   if row["body"].get("run_id") == child_id)
    assert control["body"]["phase"] == "ready_model"
    assert control["body"]["feedback"]["previous_response_ref"]["id"] == rejected_receipt["id"]
    assert runner._resume_source(tmp_path, source, request, "offline_test", followup=True)
    with pytest.raises(ValueError):
        runner._resume_source(tmp_path, source,
                              {**request, "target_version": request["target_version"] + 1},
                              "offline_test", followup=True)
    unsafe = tmp_path / ".local/search-validation/child-unknown-400"
    shutil.copytree(source, unsafe, symlinks=True)
    unsafe_store = StateStore(unsafe / "state")
    unsafe_store.transact(
        "synthetic-unknown-400", {},
        lambda tx: tx.put("model_response_receipt", rejected_receipt["id"],
                          {**rejected_receipt["body"], "error_kind": "http_error"},
                          expected_version=rejected_receipt["version"]),
        mission_id=rejected_receipt["body"]["mission_id"],
    )
    with pytest.raises(ValueError, match="recovered rejection"):
        runner._resume_source(tmp_path, unsafe, request, "offline_test", followup=True)
    source_digest = runner._state_digest(source / "state")
    accepted_packets = []

    def accept(_handler, _class, outgoing, **_kwargs):
        accepted_packets.append(json.loads(outgoing.data))
        return response(outgoing, json.dumps(answer("The retained source supports a small note.", 2).payload).encode())

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", accept)
    output = tmp_path / ".local/search-validation/child-resumed-400"
    resumed = runner.run_case(
        tmp_path, output, execution_mode="offline_test", followup_request=request,
        resume_case=source, accounting_only=True, until_complete=True,
        model_limit=None, token_limit=None, retrieval_limit=None, deadline_seconds=None,
    )
    assert resumed["outcome"] == "completed" and resumed["resume_integrity_complete"] is True
    assert resumed["phase_model_attempts"] == len(accepted_packets) == 1
    assert resumed["model_attempts"] == 2
    assert resumed["cumulative_accounting"]["unknown_model_reservations"] > 0
    notices = []
    for message in accepted_packets[0]["messages"]:
        try:
            parsed = json.loads(message.get("content", ""))
        except (TypeError, ValueError):
            continue
        if isinstance(parsed, dict) and parsed.get("authority") == "saved_host_error":
            notices.append(parsed)
    assert any(item.get("error_kind") == "unregistered_tool_function"
               and item.get("rejected_function") == "read_source_inventory"
               for item in notices)
    assert "Synthetic source: a small note is supported." in json.dumps(accepted_packets[0])
    assert runner._state_digest(parent_state) == parent_digest
    assert runner._state_digest(source / "state") == source_digest
    assert all((source / name).read_bytes() == value for name, value in source_files.items())
    assert policy.read_bytes() == original_policy
    copied = StateStore(output / "state", read_only=True)
    assert copied.get("mission_run", child_id)["body"]["status"] == "completed"
    assert copied.get("mission_run", parent_id) == parent_record
    assert [row for row in copied.records("agent_call")
            if row["body"].get("mission_id") == parent_mission_id] == prior_parent_calls
    assert copied.get("report", target.id, target.version) == prior_parent_report
    report = load_run_report(output, kind="natural_agent")
    assert report["counts"]["model_http_attempts"] == 2
    assert report["counts"]["model_usage_unknown_attempts"] == 1


def test_full_daily_429_resume_obeys_saved_retry_and_keeps_unknown_usage(tmp_path, monkeypatch):
    policy = project(tmp_path, monkeypatch)
    original_policy = policy.read_bytes()
    request = {"request_id": "synthetic-resume-daily-429",
               "question": "Summarize a synthetic note.",
               "evidence_text": "Synthetic note: result is 17 units.",
               "agent_ids": ["agent-synthesis"], "credential_profile": "primary",
               "allow_vision": False}

    def reject(_handler, _class, outgoing, **_kwargs):
        body = {"error": {"code": "rate_limit_exceeded",
                          "message": "TPD limit reached; retry after the saved wait"}}
        reply = response(outgoing, json.dumps(body).encode(), status=429)
        reply.headers["retry-after"] = "3"
        return reply

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", reject)
    source = tmp_path / ".local/search-validation/start-stopped-429"
    stopped = runner.run_case(
        tmp_path, source, execution_mode="offline_test", start_request=request,
        accounting_only=True, until_complete=True, model_limit=None, token_limit=None,
        retrieval_limit=None, deadline_seconds=None,
    )
    assert stopped["outcome"] == "provider_daily_quota_wait"
    budget = json.loads((source / "budget.json").read_text())
    assert budget["model_attempts"][-1]["state"] == "rate_limited"
    assert budget["model_attempts"][-1]["quota_classification"] == "provider_daily_limit"
    saved = StateStore(source / "state", read_only=True)
    run_id = json.loads((source / "case.json").read_text())["run_id"]
    control = next(row for row in saved.records("mission_task_control")
                   if row["body"].get("run_id") == run_id)
    retry_at = control["body"]["retry_at"]
    assert retry_at > time.time()
    assert runner._resume_source(tmp_path, source, request, "offline_test")
    unsafe = tmp_path / ".local/search-validation/start-no-retry-429"
    shutil.copytree(source, unsafe, symlinks=True)
    unsafe_store = StateStore(unsafe / "state")
    unsafe_store.transact(
        "synthetic-missing-retry", {},
        lambda tx: tx.put("mission_task_control", control["id"],
                          {**control["body"], "retry_at": None},
                          expected_version=control["version"]),
        mission_id=control["body"]["mission_id"],
    )
    with pytest.raises(ValueError, match="recovered rejection"):
        runner._resume_source(tmp_path, unsafe, request, "offline_test")
    mismatched = tmp_path / ".local/search-validation/start-mismatched-retry-429"
    shutil.copytree(source, mismatched, symlinks=True)
    mismatched_store = StateStore(mismatched / "state")
    mismatched_store.transact(
        "synthetic-mismatched-retry", {},
        lambda tx: tx.put("mission_task_control", control["id"],
                          {**control["body"], "feedback": {
                              **control["body"]["feedback"],
                              "previous_response_ref": {
                                  "kind": "model_response_receipt", "id": "unrelated-response",
                                  "version": 1,
                              },
                          }}, expected_version=control["version"]),
        mission_id=control["body"]["mission_id"],
    )
    with pytest.raises(ValueError, match="recovered rejection"):
        runner._resume_source(tmp_path, mismatched, request, "offline_test")
    source_digest = runner._state_digest(source / "state")
    dispatched = []

    def accept(_handler, _class, outgoing, **_kwargs):
        dispatched.append(time.time())
        return response(outgoing, json.dumps(answer("The synthetic result is 17 units.", 2).payload).encode())

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", accept)
    output = tmp_path / ".local/search-validation/start-resumed-429"
    resumed = runner.run_case(
        tmp_path, output, execution_mode="offline_test", start_request=request,
        resume_case=source, accounting_only=True, until_complete=True,
        model_limit=None, token_limit=None, retrieval_limit=None, deadline_seconds=None,
    )
    assert resumed["outcome"] == "completed" and resumed["resume_integrity_complete"] is True
    assert len(dispatched) == 1 and dispatched[0] >= retry_at
    assert resumed["model_attempts"] == 2 and resumed["phase_model_attempts"] == 1
    assert resumed["cumulative_accounting"]["unknown_model_reservations"] > 0
    assert runner._state_digest(source / "state") == source_digest
    assert policy.read_bytes() == original_policy
    report = load_run_report(output, kind="natural_agent")
    assert report["counts"]["model_http_attempts"] == 2
    assert report["counts"]["model_usage_unknown_attempts"] == 1


def test_two_daily_429_resumes_keep_all_prior_ledgers_and_write_final_observation(
    tmp_path, monkeypatch,
):
    policy = project(tmp_path, monkeypatch)
    original_policy = policy.read_bytes()
    request = {"request_id": "synthetic-three-phase-daily-429",
               "question": "Summarize the synthetic measurement.",
               "evidence_text": "Synthetic measurement: 17 units.",
               "agent_ids": ["agent-synthesis"], "credential_profile": "primary",
               "allow_vision": False}
    calls = []

    def http(_handler, _class, outgoing, **_kwargs):
        calls.append(time.time())
        if len(calls) <= 2:
            reply = response(outgoing, json.dumps({"error": {
                "code": "rate_limit_exceeded", "message": "TPD limit reached"
            }}).encode(), status=429)
            reply.headers["retry-after"] = "1"
            return reply
        return response(outgoing, json.dumps(answer(
            "The synthetic measurement is 17 units.", len(calls),
        ).payload).encode())

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", http)
    first = tmp_path / ".local/search-validation/three-phase-first"
    second = tmp_path / ".local/search-validation/three-phase-second"
    third = tmp_path / ".local/search-validation/three-phase-third"
    common = dict(execution_mode="offline_test", start_request=request,
                  accounting_only=True, until_complete=True, model_limit=None,
                  token_limit=None, retrieval_limit=None, deadline_seconds=None)
    first_result = runner.run_case(tmp_path, first, **common)
    assert first_result["outcome"] == "provider_daily_quota_wait"
    second_result = runner.run_case(tmp_path, second, resume_case=first, **common)
    assert second_result["outcome"] == "provider_daily_quota_wait"
    assert second_result["resume_integrity_complete"] is True
    assert second_result["cumulative_accounting"]["model_http_attempts"] == 2
    first_files = {name: (first / name).read_bytes() for name in (
        "case.json", "budget.json", "observation.json",
    )}
    second_files = {name: (second / name).read_bytes() for name in (
        "case.json", "budget.json", "observation.json", "cumulative-budget.json",
    )}
    third_result = runner.run_case(tmp_path, third, resume_case=second, **common)
    assert third_result["outcome"] == "completed"
    assert third_result["resume_integrity_complete"] is True
    assert third_result["phase"] == 3
    assert third_result["phase_model_attempts"] == 1
    assert third_result["model_attempts"] == len(calls) == 3
    assert third_result["cumulative_accounting"]["unknown_model_reservations"] > 0
    assert third_result["resume_source_unchanged"] is True
    assert all((first / name).read_bytes() == value for name, value in first_files.items())
    assert all((second / name).read_bytes() == value for name, value in second_files.items())
    assert policy.read_bytes() == original_policy
    ledger = json.loads((third / "cumulative-budget.json").read_text())
    assert [row["status"] for row in ledger["model_attempts"]] == [429, 429, 200]
    assert [row["number"] for row in ledger["model_attempts"]] == [1, 2, 3]
    assert (third / "prior-cumulative-budget.json").read_bytes() == second_files[
        "cumulative-budget.json"
    ]
    report = load_run_report(third, kind="natural_agent")
    assert report["counts"]["model_http_attempts"] == 3
    assert report["counts"]["model_usage_unknown_attempts"] == 2
    assert report["counts"]["model_captured_responses"] == 1
    store = StateStore(third / "state", read_only=True)
    run_id = third_result["run_id"]
    head = store.get("evaluation_observation", f"evaluation-observation:{run_id}")
    assert head["version"] == 3
    assert store.get(head["kind"], head["id"], 1) is not None
    assert store.get(head["kind"], head["id"], 2) is not None

    # A later source-validation mismatch must still leave a local stop record.
    fourth = tmp_path / ".local/search-validation/three-phase-source-changed"
    saved_open = urllib.request.AbstractHTTPHandler.do_open

    def source_changed(handler, http_class, outgoing, **kwargs):
        (second / "cumulative-budget.json").write_text("changed after private copy")
        return saved_open(handler, http_class, outgoing, **kwargs)

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", source_changed)
    fourth_result = runner.run_case(tmp_path, fourth, resume_case=second, **common)
    assert fourth_result["outcome"] == "completed"
    assert fourth_result["resume_source_unchanged"] is False
    assert fourth_result["resume_integrity_complete"] is False
    assert json.loads((fourth / "observation.json").read_text()) == fourth_result


def test_clean_token_cap_accepts_two_local_peer_denials_and_rejects_tampered_work(
    tmp_path, monkeypatch,
):
    """A first-phase multi-peer cap has no unknown HTTP usage to unlock this path."""
    project(tmp_path, monkeypatch)
    request = {
        "request_id": "synthetic-clean-two-peer-cap",
        "question": "Compare the two synthetic measurements and give a bounded answer.",
        "scope": "Use only supplied synthetic data.",
        "evidence_text": "Synthetic measurements: A is 3 units and B is 4 units.",
        "agent_ids": ["agent-synthesis", "agent-critique"],
        "credential_profile": "primary", "allow_vision": False,
    }
    sent = []

    def first_peer_pair(_handler, _class, outgoing, **_kwargs):
        sent.append(json.loads(outgoing.data))
        assert len(sent) <= 2, "Token cap must prevent a third HTTP request"
        payload = tool("discover_capabilities", {}, len(sent)).payload
        payload["usage"] = {"prompt_tokens": 11_000,
                            "completion_tokens": 1_000, "total_tokens": 12_000}
        return response(outgoing, json.dumps(payload).encode())

    monkeypatch.setattr(urllib.request.AbstractHTTPHandler, "do_open", first_peer_pair)
    source = tmp_path / ".local/search-validation/clean-two-peer-cap"
    observed = runner.run_case(
        tmp_path, source, execution_mode="offline_test", start_request=request,
        model_limit=None, token_limit=35_000, retrieval_limit=0,
        deadline_seconds=30, accounting_only=False, until_complete=False,
    )
    assert observed["outcome"] == "token_cap"
    assert len(sent) == observed["model_attempts"] == 2
    store = StateStore(source / "state")
    blocked = [row for row in store.records("model_dispatch_attempt")
               if row["body"]["status"] == "blocked_before_external_effect"]
    assert len(blocked) == 1
    marker = blocked[0]
    intent = store.get(**marker["body"]["intent_ref"])
    call = store.get(**marker["body"]["call_ref"])
    work = next(row for row in store.records("executor_work")
                if row["body"].get("superseded_intent_ref") == ref(intent))
    capacity = store.get("capacity_operation", work["id"].replace(
        "executor-work:", "executor-cap:", 1))
    other = next(row for row in store.records("mission_task_control")
                 if row["body"]["actor_id"] != call["body"]["instance_id"])
    other_call = store.get(**other["body"]["call_ref"])
    other_proposal = store.get("board_proposal", other["body"]["task_id"], 1)
    packet = json.loads(store.read_blob(intent["body"]["blob"]["sha256"]))
    created = {}

    def add_local_only_candidates(tx):
        for suffix, text in (
            ("blocked", "Synthetic second peer candidate."),
            ("superseded", "Synthetic peer input changed before dispatch."),
            ("pending", "Synthetic peer candidate queued without dispatch."),
        ):
            encoded = canonical({
                **packet,
                "messages": [*packet["messages"], {"role": "user", "content": text}],
            }).encode()
            blob = store.put_blob(encoded)
            sha = hashlib.sha256(encoded).hexdigest()
            estimate = estimate_request_tokens(encoded).tokens
            call_id = f"synthetic-{suffix}-call"
            intent_id = f"model-intent:{call_id}:1"
            work_id = f"executor-work:synthetic-{suffix}"
            call_ref = {"kind": "agent_call", "id": call_id, "version": 1}
            intent_ref = {"kind": "model_request_intent", "id": intent_id, "version": 1}
            actor = (other["body"]["actor_id"] if suffix in {"blocked", "pending"}
                     else call["body"]["instance_id"])
            task = (other["body"]["task_id"] if suffix == "blocked"
                    else call["body"]["task_id"] if suffix == "superseded"
                    else "synthetic-pending-task")
            attempt = f"synthetic-{suffix}-attempt"
            actor_call = other_call if suffix in {"blocked", "pending"} else call
            configuration_ref = {
                "kind": "agent_configuration",
                "id": actor_call["body"]["configuration_id"], "version": 1,
            }
            tx.put("agent_call", call_id, {
                **actor_call["body"], "call_id": call_id, "task_id": task,
                "instance_id": actor, "attempt_id": attempt,
            })
            saved_intent = tx.put("model_request_intent", intent_id, {
                **intent["body"], "call_ref": call_ref, "task_id": task,
                "instance_id": actor, "attempt_id": attempt,
                "configuration_ref": configuration_ref,
                "configuration_sha256": actor_call["body"]["configuration_sha256"],
                "blob": blob, "request_sha256": sha,
                "request_token_estimate": {
                    **intent["body"]["request_token_estimate"], "tokens": estimate,
                },
            })
            spec = {
                **work["body"]["spec"], "call_id": call_id, "actor_id": actor,
                "attempt_id": attempt, "work_id": f"synthetic-{suffix}",
                "estimated_tokens": estimate, "lane": actor,
                "configuration_ref": configuration_ref,
                "proposal_ref": {"kind": "board_proposal", "id": task, "version": 1},
            }
            work_body = {**work["body"], "superseded_intent_ref": intent_ref,
                         "spec": spec}
            cap_body = {
                **capacity["body"], "call_id": call_id, "task_id": task,
                "lane": actor,
                "attempt_id": attempt,
                "request_token_estimate": {
                    **capacity["body"]["request_token_estimate"],
                    "intent_ref": intent_ref, "request_sha256": sha,
                    "tokens": estimate,
                },
            }
            if suffix == "blocked":
                tx.put("model_dispatch_attempt", f"model-dispatch:{intent_id}", {
                    **marker["body"], "call_ref": call_ref,
                    "intent_ref": intent_ref, "request_sha256": sha,
                })
                tx.put("mission_task_control", other["id"], {
                    **other["body"], "call_ref": call_ref,
                    "phase": "ready_model", "work_id": None,
                }, expected_version=other["version"])
            elif suffix == "superseded":
                work_body.update(status_basis="new_peer_input_before_dispatch")
                cap_body.update(
                    phase="canceled", diagnostic="new_peer_input_before_dispatch",
                    cancel_requested=True, attempts=[], local_active=False,
                    reservation=False, external_active=False, output_ref=None,
                )
            else:
                tx.put("board_proposal", task, {
                    **other_proposal["body"],
                    "actor_ref": {"kind": "agent_instance", "id": actor, "version": 1},
                })
                work_body.update(
                    phase="pending", status_basis=None,
                    superseded_intent_ref=None,
                )
                cap_body.update(
                    phase="queued", diagnostic=None, cancel_requested=False,
                    attempts=[], local_active=False, reservation=False,
                    external_active=False, owner=None, lease_until=None,
                    output_ref=None,
                )
                tx.put("mission_task_control", "synthetic-pending-control", {
                    **other["body"], "task_id": task, "call_ref": call_ref,
                    "phase": "pending_model", "work_id": spec["work_id"],
                    "proposal_ref": spec["proposal_ref"],
                    "origin_proposal_ref": spec["proposal_ref"],
                })
            tx.put("executor_work", work_id, work_body)
            tx.put("capacity_operation", work_id.replace(
                "executor-work:", "executor-cap:", 1), cap_body)
            created[suffix] = {"intent": saved_intent["id"], "call": call_id,
                               "work": work_id,
                               "capacity": work_id.replace(
                                   "executor-work:", "executor-cap:", 1)}
        return {"synthetic_no_external_effect": True}

    # These synthetic sibling denials belong to the same active controller
    # phase as the real denial, not the later fixture-construction time.
    with monkeypatch.context() as phase_clock:
        phase_clock.setattr(store, "clock", lambda: capacity["body"]["attempts"][0]["at"])
        store.transact("synthetic-clean-multi-peer-cap", {}, add_local_only_candidates,
                       mission_id=call["body"]["mission_id"])

    def validate(case):
        return runner._resume_source(
            tmp_path, case, request, "offline_test", model_limit=None,
            token_limit=35_000, retrieval_limit=0, deadline_seconds=30,
            accounting_only=False, until_complete=False,
            token_ceiling_increase=500,
        )

    before = runner._state_digest(source / "state")
    lineage = validate(source)
    assert lineage["source_boundary"] == "clean_token_cap_stop"
    assert lineage["prior_cumulative"]["model_http_attempts"] == 2
    assert lineage["prior_cumulative"]["unknown_model_reservations"] == 0
    assert runner._state_digest(source / "state") == before
    for name, kind, ident, key, bad_value in (
        ("blocked-capacity", "capacity_operation", created["blocked"]["capacity"],
         "diagnostic", "unverified"),
        ("blocked-intent", "model_request_intent", created["blocked"]["intent"],
         "configuration_sha256", "0" * 64),
        ("unsent-capacity", "capacity_operation", created["superseded"]["capacity"],
         "external_active", True),
        ("unsent-work", "executor_work", created["superseded"]["work"],
         "status_basis", "unrelated"),
        ("unsent-intent", "model_request_intent", created["superseded"]["intent"],
         "request_sha256", "0" * 64),
        ("pending-attempt", "capacity_operation", created["pending"]["capacity"],
         "attempts", [{"status": "dispatched", "usage_known": False}]),
        ("pending-phase", "capacity_operation", created["pending"]["capacity"],
         "phase", "uncertain"),
    ):
        tampered = tmp_path / ".local/search-validation" / f"clean-two-peer-{name}"
        shutil.copytree(source, tampered, symlinks=True)
        changed = StateStore(tampered / "state")
        row = changed.get(kind, ident)
        changed.transact(f"synthetic-tamper-{name}", {}, lambda tx: tx.put(
            kind, ident, {**row["body"], key: bad_value},
            expected_version=row["version"],
        ))
        with pytest.raises(ValueError, match="exact active run and terminal response"):
            validate(tampered)
    for suffix in ("blocked", "superseded", "pending"):
        for actor_label, wrong_actor in (
            ("other-peer", (call["body"]["instance_id"]
                            if suffix in {"blocked", "pending"}
                            else other["body"]["actor_id"])),
            ("nonexistent", "agent-nonexistent-synthetic"),
        ):
            tampered = (tmp_path / ".local/search-validation"
                        / f"clean-two-peer-{suffix}-{actor_label}")
            shutil.copytree(source, tampered, symlinks=True)
            changed = StateStore(tampered / "state")
            saved_call = changed.get("agent_call", created[suffix]["call"])
            saved_work = changed.get("executor_work", created[suffix]["work"])

            def swap_actor(tx):
                tx.put("agent_call", saved_call["id"], {
                    **saved_call["body"], "instance_id": wrong_actor,
                }, expected_version=saved_call["version"])
                tx.put("executor_work", saved_work["id"], {
                    **saved_work["body"], "spec": {
                        **saved_work["body"]["spec"], "actor_id": wrong_actor,
                    },
                }, expected_version=saved_work["version"])
                return {"synthetic_actor_substitution": True}

            changed.transact(f"synthetic-actor-{suffix}-{actor_label}", {}, swap_actor)
            with pytest.raises(ValueError, match="exact active run and terminal response"):
                validate(tampered)
