"""Natural-run controller on the full mission host with injected HTTP only."""

import importlib.util
import io
import json
import urllib.request
import urllib.response
from email.message import Message
from pathlib import Path

import pytest

from ora.contracts.models import LiveStartRequest
from ora.evidence import intake
from ora.mission.service import MissionService
from ora.models import GroqSelectedAdapter, ModelSession
from ora.state import StateStore
from tests.mission.test_service import MODEL, answer, drain, tool

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location("search_agent_runner", ROOT / "scripts/validate_search_agent.py")
runner = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(runner)


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
    runner.run_case(tmp_path, folder, execution_mode="offline_test", start_request=request,
                    model_limit=1, token_limit=20_000, retrieval_limit=0, deadline_seconds=30)
    case = json.loads((folder / "case.json").read_text())
    budget = json.loads((folder / "budget.json").read_text())
    assert case["allow_vision"] is allow_vision
    assert budget["limits"].get("allow_vision", False) is allow_vision
    store = StateStore(folder / "state", read_only=True)
    assert store.get("mission_run", case["run_id"])["body"]["profile"].get("allowVision", False) is allow_vision


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
