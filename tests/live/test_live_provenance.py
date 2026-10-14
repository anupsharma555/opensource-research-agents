"""Offline checks for exact live call provenance and crash-safe tool receipts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from pathlib import Path

from ora.agents.capabilities import content_hash
from ora.contracts.models import LiveFollowupRequest, LiveStartRequest
from ora.live.engine import LiveError, ResearchHost
from ora.live.provider import CredentialUnavailable, GroqProvider, ProviderError
from ora.state import StateStore

ROOT = Path(__file__).resolve().parents[2]


class LocalProvider:
    model = "synthetic-model"

    def __init__(self, tool_name: str = "source_read", bad_arguments: bool = False) -> None:
        self.tool_name = tool_name
        self.bad_arguments = bad_arguments
        self.calls = 0

    def estimate_max_cost(self, messages, tools=None):
        return 0.001

    def complete(self, messages, tools=None):
        self.calls += 1
        if any(message["role"] == "tool" for message in messages):
            return self._response(content="Answer based on supplied text.")
        source_id = json.loads(messages[1]["content"])["sources"][0]["id"]
        arguments = (
            {"unexpected": source_id}
            if self.bad_arguments
            else {"source_id": source_id}
            if self.tool_name == "source_read"
            else {"agent_id": "agent-critique", "question": "Check the passage."}
        )
        return self._response(
            tool_calls=[{
                "id": "provider-tool-1",
                "type": "function",
                "function": {
                    "name": self.tool_name,
                    "arguments": json.dumps(arguments),
                },
            }]
        )

    def _response(self, content=None, tool_calls=None):
        return {
            "content": content,
            "tool_calls": tool_calls or [],
            "usage": {"prompt_tokens": 4, "completion_tokens": 2, "total_tokens": 6},
            "model": self.model,
            "elapsed_seconds": 0.01,
            "finish_reason": "stop",
            "estimated_usd": 0.00001,
        }


def _start(host: ResearchHost, request_id: str):
    return host.start(LiveStartRequest(
        request_id=request_id,
        question="What does the supplied passage say?",
        evidence_text="Synthetic passage for an offline test.",
        agent_ids=["agent-synthesis"],
    ))


def _wait(host: ResearchHost, run_id: str, status: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        actual = next(item.status for item in host.state(run_id).runs if item.id == run_id)
        if actual == status:
            return
        time.sleep(0.01)
    raise AssertionError(f"run did not reach {status}")


def _rows(path: Path, table: str):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    try:
        return [dict(row) for row in db.execute(f"SELECT * FROM {table}")]
    finally:
        db.close()


def test_calls_pin_shared_actor_configuration_and_exact_loaded_capabilities(tmp_path):
    provider = LocalProvider()
    host = ResearchHost(ROOT, tmp_path, provider)
    first = _start(host, "first")
    _wait(host, first.run_id, "completed")
    calls = host.calls(first.run_id)
    assert len(calls) == 2
    db_calls = sorted(_rows(host.db_path, "calls"), key=lambda row: row["created_at"])
    assert {row["agent_instance_id"] for row in db_calls} == {db_calls[0]["agent_instance_id"]}
    assert all(row["config_id"] and row["config_version"] == 1 for row in db_calls)

    store = StateStore(tmp_path, database_name="research.sqlite3", read_only=True)
    actor = store.get("agent_instance", db_calls[0]["agent_instance_id"], version=1)
    mission_id = host.state(first.run_id).snapshot.mission.id
    assert mission_id != first.run_id
    assert actor["body"]["mission_id"] == mission_id
    assert actor["body"]["display_name"] == "Sage"
    assert actor["body"]["role_id"] == "evidence_preparation_synthesis"
    assert actor["body"]["run_id"] == first.run_id
    for row in db_calls:
        config = store.get("agent_configuration", row["config_id"], version=1)["body"]
        manifest = store.get("agent_call", row["id"], version=1)["body"]
        assert config["instance_id"] == actor["id"]
        assert config["mission_id"] == manifest["mission_id"] == mission_id
        assert config["provider"] == "unknown_custom"
        assert config["endpoint"] is None
        assert config["model_id"] == "synthetic-model"
        assert config["settings"] == {"availability": "unknown"}
        assert config["instructions"] == json.loads(row["messages"])[0]["content"]
        input_ref = config["input_refs"][0]
        saved_input = store.get(input_ref["kind"], input_ref["id"], input_ref["version"])
        assert input_ref["sha256"] == content_hash(saved_input["body"])
        assert saved_input["body"]["messages_ref"] == f"calls:{row['id']}.messages"
        assert saved_input["body"]["messages_sha256"] == content_hash(
            json.loads(row["messages"])
        )
        assert set(config["skills"]) == {"evidence-linking", "general-web-discovery"}
        assert set(manifest["exposed_tools"]) == {"source_read", "ask_peer", "publish_result"}
        assert set(manifest["exposed_skill_ids"]) == {"evidence-linking", "general-web-discovery"}
        assert manifest["configuration_id"] == row["config_id"]
    tool = _rows(host.db_path, "tool_invocations")[0]
    assert tool["status"] == "completed"
    assert tool["provider_tool_call_id"] == "provider-tool-1"
    assert tool["model_call_id"] == db_calls[0]["id"]
    assert tool["input_ref"] == "calls.response.tool_calls[0].function.arguments"
    assert tool["output_ref"] == "calls.tool_results[0].content"
    assert tool["started_at"] and tool["completed_at"]
    events = [row["status"] for row in _rows(host.db_path, "tool_invocation_events")]
    assert events == ["requested", "started", "completed"]
    overview = _rows(host.db_path, "live_call_overview")
    assert len(overview) == len(db_calls)
    assert {row["agent_name"] for row in overview} == {"Sage"}
    assert {row["requested_model"] for row in overview} == {"synthetic-model"}
    assert {row["configuration_basis"] for row in overview} == {"recorded_configuration"}
    assert _rows(host.db_path, "live_tool_overview")[0]["status"] == "completed"

    target = host.state(first.run_id).snapshot.artifacts[0]
    follow = host.followup(LiveFollowupRequest(
        request_id="follow",
        parent_run_id=first.run_id,
        parent_snapshot_id=host.state(first.run_id).snapshot.snapshot_id,
        target_id=target.id,
        target_version=target.version,
        question="Check the saved answer again.",
        agent_ids=["agent-synthesis"],
    ))
    _wait(host, follow.run_id, "completed")
    assert host.state(follow.run_id).snapshot.mission.id == mission_id
    assert {row["agent_instance_id"] for row in _rows(host.db_path, "calls")
            if row["run_id"] == follow.run_id} == {actor["id"]}
    other = _start(host, "other")
    _wait(host, other.run_id, "completed")
    assert {row["agent_instance_id"] for row in _rows(host.db_path, "calls")
            if row["run_id"] == other.run_id} != {actor["id"]}
    host.close()


def test_rejected_tool_never_starts_and_error_points_to_saved_result(tmp_path):
    host = ResearchHost(ROOT, tmp_path, LocalProvider(bad_arguments=True))
    receipt = _start(host, "reject")
    _wait(host, receipt.run_id, "completed")
    tool = _rows(host.db_path, "tool_invocations")[0]
    assert tool["status"] == "rejected"
    assert tool["started_at"] is None
    assert tool["error_ref"] == "calls.tool_results[0].content"
    assert tool["output_ref"] is None
    assert [row["status"] for row in _rows(host.db_path, "tool_invocation_events")] == [
        "requested", "rejected"
    ]
    assert "source_id" in host.calls(receipt.run_id)[0]["toolResults"][0]["content"]
    call_id = host.calls(receipt.run_id)[0]["id"]
    original = host.calls(receipt.run_id)[0]["toolResults"][0]
    try:
        host._append_tool_result(call_id, original, "completed")
    except LiveError as exc:
        assert exc.status == 409
    else:
        raise AssertionError("duplicate provider tool result was accepted")
    assert len(host.calls(receipt.run_id)[0]["toolResults"]) == 1
    host.close()


def test_failed_pure_tool_and_uncertain_effectful_tool_are_distinct(tmp_path):
    pure = ResearchHost(ROOT, tmp_path / "pure", LocalProvider())

    def fail(*_args):
        raise RuntimeError("private failure details")

    pure._tool = fail
    receipt = _start(pure, "pure-failure")
    _wait(pure, receipt.run_id, "completed")
    assert _rows(pure.db_path, "tool_invocations")[0]["status"] == "failed"
    assert [row["status"] for row in _rows(pure.db_path, "tool_invocation_events")] == [
        "requested", "started", "failed"
    ]
    assert "private failure details" not in json.dumps(pure.calls(receipt.run_id))
    pure.close()

    effectful_provider = LocalProvider(tool_name="ask_peer")
    effectful = ResearchHost(ROOT, tmp_path / "effectful", effectful_provider)
    effectful._tool = fail
    receipt = _start(effectful, "effectful-failure")
    _wait(effectful, receipt.run_id, "waiting")
    assert _rows(effectful.db_path, "tool_invocations")[0]["status"] == "uncertain"
    assert [row["status"] for row in _rows(effectful.db_path, "tool_invocation_events")] == [
        "requested", "started", "uncertain"
    ]
    assert effectful_provider.calls == 1
    effectful.close()
    reopened = ResearchHost(ROOT, tmp_path / "effectful", effectful_provider)
    assert effectful_provider.calls == 1
    assert _rows(reopened.db_path, "tool_invocations")[0]["status"] == "uncertain"
    assert [row["status"] for row in _rows(reopened.db_path, "tool_invocation_events")] == [
        "requested", "started", "uncertain"
    ]
    reopened.close()


def test_interrupted_started_tool_becomes_uncertain_without_replay(tmp_path):
    provider = LocalProvider()
    host = ResearchHost(ROOT, tmp_path, provider)

    def interrupt(*_args):
        raise KeyboardInterrupt

    host._tool = interrupt
    receipt = _start(host, "interrupted")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        tools = _rows(host.db_path, "tool_invocations")
        if tools and tools[0]["status"] == "started":
            break
        time.sleep(0.01)
    assert tools[0]["status"] == "started"
    host.close()
    reopened = ResearchHost(ROOT, tmp_path, provider)
    assert _rows(reopened.db_path, "tool_invocations")[0]["status"] == "uncertain"
    assert provider.calls == 1
    assert reopened.state(receipt.run_id).runs[0].status == "waiting"
    reopened.close()


def test_groq_effective_configuration_matches_request_without_loading_key(tmp_path):
    provider = GroqProvider(tmp_path, max_completion_tokens=321)
    messages = [{"role": "system", "content": "Synthetic instructions"}]
    actual = provider.effective_configuration(messages, [])
    body = provider._request_body(messages, [])
    assert actual == {
        "provider": "groq",
        "endpoint": "https://api.groq.com/openai/v1/chat/completions",
        "model_id": body["model"],
        "settings": {
            key: value for key, value in body.items()
            if key not in {"model", "messages", "tools", "reasoning_effort"}
        } | {
            "request_messages_sha256": hashlib.sha256(json.dumps(
                body["messages"], ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            ).encode()).hexdigest(),
            "request_tools_sha256": hashlib.sha256(json.dumps(
                body.get("tools", []), ensure_ascii=False, sort_keys=True,
                separators=(",", ":"), allow_nan=False,
            ).encode()).hexdigest(),
        },
        "reasoning_settings": {"reasoning_effort": body["reasoning_effort"]},
    }


def test_failed_provider_call_keeps_predispatch_configuration(tmp_path):
    class FailingProvider(LocalProvider):
        def complete(self, messages, tools=None):
            self.calls += 1
            raise ProviderError("Synthetic provider refusal", status=503)

    provider = FailingProvider()
    host = ResearchHost(ROOT, tmp_path, provider)
    receipt = _start(host, "provider-refusal")
    _wait(host, receipt.run_id, "waiting")
    call = _rows(host.db_path, "calls")[0]
    assert call["status"] == "uncertain"
    assert call["config_id"] and call["agent_instance_id"]
    store = StateStore(tmp_path, database_name="research.sqlite3", read_only=True)
    assert store.get("agent_configuration", call["config_id"], version=1)
    assert store.get("agent_call", call["id"], version=1)
    assert provider.calls == 1
    host.close()


def test_saved_legacy_database_needs_explicit_migration_before_host_writes(tmp_path):
    path = tmp_path / "research.sqlite3"
    with sqlite3.connect(path) as db:
        db.execute("CREATE TABLE runs(id TEXT PRIMARY KEY)")
        db.execute("CREATE TABLE calls(id TEXT PRIMARY KEY)")
        db.execute("INSERT INTO runs(id) VALUES ('saved-run')")
    before = path.read_bytes()
    try:
        ResearchHost(ROOT, tmp_path, LocalProvider())
    except LiveError as exc:
        assert exc.status == 503
        assert "migrate-provenance" in str(exc)
    else:
        raise AssertionError("host upgraded saved legacy state implicitly")
    assert path.read_bytes() == before


def test_groq_setting_drift_is_rejected_before_key_or_network(tmp_path, monkeypatch):
    provider = GroqProvider(tmp_path, max_completion_tokens=321)
    original = provider.complete

    def shifted(messages, tools=None, *, expected_configuration=None):
        provider.max_completion_tokens = 322
        return original(
            messages, tools, expected_configuration=expected_configuration
        )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("credential or network access was attempted")

    monkeypatch.setattr(provider, "complete", shifted)
    monkeypatch.setattr(provider, "_key", forbidden)
    monkeypatch.setattr("ora.live.provider.urllib.request.urlopen", forbidden)
    host = ResearchHost(ROOT, tmp_path, provider)
    receipt = _start(host, "drift")
    _wait(host, receipt.run_id, "waiting")
    call = _rows(host.db_path, "calls")[0]
    assert call["status"] == "rejected"
    assert call["phase"] == "configuration_drift"
    assert "changed before dispatch" in call["response"]
    assert host.state(receipt.run_id).runs[0].status == "waiting"
    host.close()


def test_loaded_skill_drift_stops_before_model_dispatch(tmp_path):
    provider = LocalProvider()
    host = ResearchHost(ROOT, tmp_path, provider)
    host._loaded_skills["evidence-linking"]["instructions"] = "Changed after host loading"
    receipt = _start(host, "skill-drift")
    _wait(host, receipt.run_id, "waiting")
    assert provider.calls == 0
    assert _rows(host.db_path, "calls") == []
    assert host.state(receipt.run_id).snapshot.attempts[0].status == "waiting"
    host.close()


def test_missing_credential_is_recorded_as_unsent_rejection(tmp_path, monkeypatch):
    provider = GroqProvider(tmp_path)

    def missing():
        raise CredentialUnavailable("Synthetic credential unavailable", status=503)

    def forbidden(*_args, **_kwargs):
        raise AssertionError("network was attempted")

    monkeypatch.setattr(provider, "_key", missing)
    monkeypatch.setattr("ora.live.provider.urllib.request.urlopen", forbidden)
    host = ResearchHost(ROOT, tmp_path, provider)
    receipt = _start(host, "credential-preflight")
    _wait(host, receipt.run_id, "waiting")
    call = _rows(host.db_path, "calls")[0]
    assert (call["status"], call["phase"]) == ("rejected", "credential_preflight")
    assert call["config_id"] and call["agent_instance_id"]
    assert _rows(host.db_path, "tool_invocations") == []
    host.close()


def test_committed_publish_before_receipt_crash_is_not_replayed(tmp_path):
    class PublishingProvider(LocalProvider):
        def complete(self, messages, tools=None):
            self.calls += 1
            return self._response(tool_calls=[{
                "id": "provider-publish-1",
                "type": "function",
                "function": {
                    "name": "publish_result",
                    "arguments": json.dumps({"text": "Saved answer before crash."}),
                },
            }])

    provider = PublishingProvider()
    host = ResearchHost(ROOT, tmp_path, provider)

    def crash_before_receipt(*_args):
        raise KeyboardInterrupt

    host._append_tool_result = crash_before_receipt
    receipt = _start(host, "effect-before-crash")
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        snapshot = host.state(receipt.run_id).snapshot
        if snapshot.artifacts:
            break
        time.sleep(0.01)
    assert [item.body for item in snapshot.artifacts] == ["Saved answer before crash."]
    assert _rows(host.db_path, "tool_invocations")[0]["status"] == "started"
    host.close()

    reopened = ResearchHost(ROOT, tmp_path, provider)
    assert _rows(reopened.db_path, "tool_invocations")[0]["status"] == "uncertain"
    assert [row["status"] for row in _rows(reopened.db_path, "tool_invocation_events")] == [
        "requested", "started", "uncertain"
    ]
    assert provider.calls == 1
    assert [item.body for item in reopened.state(receipt.run_id).snapshot.artifacts] == [
        "Saved answer before crash."
    ]
    reopened.close()
