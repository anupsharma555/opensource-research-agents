"""Exercise the local host with model-shaped responses and no network."""

import json
import sqlite3
import time
from copy import deepcopy
from pathlib import Path

import pytest

from ora.contracts.models import LiveFollowupRequest, LiveStartRequest
from ora.live.engine import LiveError, ResearchHost
from ora.live.provider import ProviderError

ROOT = Path(__file__).resolve().parents[2]


class FakeProvider:
    model = "fake-research-model"

    def __init__(self):
        self.sage_calls = 0
        self.mira_calls = 0
        self.followup = False

    def estimate_max_cost(self, messages, tools=None):
        return 0.001

    def complete(self, messages, tools=None):
        agent = "sage" if "You are Sage" in messages[0]["content"] else "mira"
        peers = json.loads(messages[1]["content"])["availablePeers"]
        assert peers[0]["id"] == ("agent-critique" if agent == "sage" else "agent-synthesis")
        assert peers[0]["name"] == ("Mira" if agent == "sage" else "Sage")
        if self.followup:
            prompt = json.loads(messages[1]["content"])
            assert prompt["target"]["version"] >= 1
            assert prompt["target"]["record"]
            return self._response(content="The follow-up answer is limited to the saved passage.")
        if agent == "sage":
            self.sage_calls += 1
            number = self.sage_calls
            if number == 1:
                source = json.loads(messages[1]["content"])["sources"][0]
                return self._tool("source_read", {"source_id": source["id"]})
            if number == 2:
                return self._tool(
                    "ask_peer",
                    {"agent_id": "agent-critique", "question": "Check the passage for limits."},
                )
            if number == 3:
                source = json.loads(messages[1]["content"])["sources"][0]
                return self._tool(
                    "publish_result",
                    {
                        "text": "Initial answer grounded in supplied evidence.",
                        "citations": [
                            {
                                "source_id": source["id"],
                                "span_id": source["passages"][0]["spanId"],
                            }
                        ],
                        "limitations": ["Only supplied text was examined."],
                    },
                )
            if number == 4:
                continuation = json.loads(messages[-1]["content"])
                assert continuation["originalQuestion"].endswith("What does this text say?")
                assert (
                    continuation["priorAnswer"] == "Initial answer grounded in supplied evidence."
                )
                assert (
                    continuation["peerResponse"] == "The saved passage has an important limitation."
                )
                assert any(message["role"] == "tool" for message in messages)
                publish_assistant = next(
                    message
                    for message in messages
                    if message["role"] == "assistant"
                    and any(
                        call["function"]["name"] == "publish_result"
                        for call in message.get("tool_calls", [])
                    )
                )
                publish_id = publish_assistant["tool_calls"][0]["id"]
                assert any(
                    message["role"] == "tool"
                    and message["tool_call_id"] == publish_id
                    and json.loads(message["content"]) == {"status": "published"}
                    for message in messages
                )
                return self._response(content="Revised answer after Mira's critique.")
        else:
            self.mira_calls += 1
            if self.mira_calls == 1:
                prompt = json.loads(messages[1]["content"])
                assert prompt["peerRequest"]["fromAgentId"] == "agent-synthesis"
                assert prompt["peerRequest"]["question"] == "Check the passage for limits."
                assert "Perform the assigned check yourself" in messages[0]["content"]
                source = json.loads(messages[1]["content"])["sources"][0]
                return self._tool("source_read", {"source_id": source["id"]})
            if self.mira_calls == 2:
                return self._tool(
                    "publish_result",
                    {"text": "The saved passage has an important limitation.", "limitations": []},
                )
        raise AssertionError("Unexpected model call")

    @staticmethod
    def _response(content=None, tool_calls=None, finish_reason="stop"):
        return {
            "content": content,
            "tool_calls": tool_calls or [],
            "usage": {"prompt_tokens": 80, "completion_tokens": 20, "total_tokens": 100},
            "model": "fake-research-model",
            "elapsed_seconds": 0.01,
            "finish_reason": finish_reason,
            "estimated_usd": 0.0002,
        }

    def _tool(self, name, arguments):
        return self._response(
            tool_calls=[
                {
                    "id": f"tool-{name}",
                    "type": "function",
                    "function": {"name": name, "arguments": json.dumps(arguments)},
                }
            ]
        )


class TruncatedProvider(FakeProvider):
    def complete(self, messages, tools=None):
        return self._response(content="Incomplete answer", finish_reason="length")


class TwoPeersProvider(FakeProvider):
    def complete(self, messages, tools=None):
        name = "Sage" if "You are Sage" in messages[0]["content"] else "Mira"
        return self._response(content=f"{name} independent answer.")


class MentionProvider(FakeProvider):
    def complete(self, messages, tools=None):
        return self._response(content="Mira may agree, but I have not asked her.")


class PlainTextProvider(FakeProvider):
    def __init__(self, read_source, include_exact=True, limitation_block=None):
        super().__init__()
        self.read_source = read_source
        self.include_exact = include_exact
        self.limitation_block = limitation_block
        self.answer = None

    def complete(self, messages, tools=None):
        source = json.loads(messages[1]["content"])["sources"][0]
        if self.read_source and not any(message["role"] == "tool" for message in messages):
            return self._tool("source_read", {"source_id": source["id"]})
        reference = f"{source['id']}::{source['passages'][0]['spanId']}"
        visible_reference = reference if self.include_exact else f"{reference}-extra"
        self.answer = (
            f"\nThe supplied observation is narrow {visible_reference}. "
            f"An unresolved pointer {source['id']}::span-not-saved remains visible.\n\n"
            + (self.limitation_block or "Limitations:\n- Only the supplied observation was examined.")
            + "\n"
        )
        return self._response(content=self.answer)


class PublishMetadataProvider(FakeProvider):
    def __init__(self, explicit_empty):
        super().__init__()
        self.explicit_empty = explicit_empty
        self.answer = None

    def complete(self, messages, tools=None):
        source = json.loads(messages[1]["content"])["sources"][0]
        if not any(message["role"] == "tool" for message in messages):
            return self._tool("source_read", {"source_id": source["id"]})
        self.answer = (
            f"Observed pattern {source['id']}::{source['passages'][0]['spanId']}.\n\n"
            "Limitations: Only the supplied text was read."
        )
        arguments = {"text": self.answer}
        if self.explicit_empty:
            arguments.update({"citations": [], "limitations": []})
        return self._tool("publish_result", arguments)


class NestedPeersProvider(FakeProvider):
    def complete(self, messages, tools=None):
        agent = "sage" if "You are Sage" in messages[0]["content"] else "mira"
        user = json.loads(messages[-1]["content"]) if messages[-1]["role"] == "user" else {}
        if "peerResponse" in user:
            return self._response(content=f"{agent} revised after peer reply.")
        original = json.loads(messages[1]["content"])
        if agent == "sage" and original["question"] == "Assess":
            if not any(message["role"] == "tool" for message in messages):
                return self._tool(
                    "ask_peer", {"agent_id": "agent-critique", "question": "Challenge this."}
                )
            return self._response(content="Sage initial.")
        if agent == "mira":
            if not any(message["role"] == "tool" for message in messages):
                return self._tool(
                    "ask_peer", {"agent_id": "agent-synthesis", "question": "Check a detail."}
                )
            return self._response(content="Mira initial.")
        return self._response(content="Sage nested reply.")


class RateLimitOnceProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.requests = []

    def complete(self, messages, tools=None):
        self.requests.append(deepcopy(messages))
        number = len(self.requests)
        if number == 1:
            source = json.loads(messages[1]["content"])["sources"][0]
            return self._tool("source_read", {"source_id": source["id"]})
        if number == 2:
            raise ProviderError("Groq rate limit reached.", status=429, retry_after=0.03)
        if number == 3:
            source = json.loads(messages[1]["content"])["sources"][0]
            return self._tool(
                "publish_result",
                {
                    "text": "Answer after quota wait.",
                    "citations": [
                        {
                            "source_id": source["id"],
                            "span_id": source["passages"][0]["spanId"],
                        }
                    ],
                },
            )
        return self._response(content="Answer after quota wait.")


class LongRateLimitProvider(FakeProvider):
    def __init__(self):
        super().__init__()
        self.waiting_calls = 0

    def complete(self, messages, tools=None):
        question = json.loads(messages[1]["content"])["question"]
        if question == "Wait":
            self.waiting_calls += 1
            raise ProviderError("Groq rate limit reached.", status=429, retry_after=120)
        return self._response(content="Other run completed.")


class PeerFollowupProvider(FakeProvider):
    def complete(self, messages, tools=None):
        prompt = json.loads(messages[1]["content"])
        if prompt["target"] is None:
            return self._response(content="Five included out of twenty screened.")
        is_sage = "You are Sage" in messages[0]["content"]
        if not is_sage:
            assert prompt["peerRequest"]["fromAgentId"] == "agent-synthesis"
            assert prompt["target"]["record"]["body"] == ("Five included out of twenty screened.")
            assert "Ask for one focused peer check" in prompt["scope"]
            assert "was fulfilled" in prompt["assignmentContext"]
            assert "primary assignment" in messages[0]["content"]
            return self._response(content="Mira checked the saved arithmetic directly.")
        if messages[-1]["role"] == "user" and "peerResponse" in messages[-1]["content"]:
            return self._response(content="Revised answer after Mira's arithmetic check.")
        if any(message["role"] == "tool" for message in messages):
            return self._response(content="Preliminary answer pending Mira's check.")
        return self._tool(
            "ask_peer",
            {"agent_id": "agent-critique", "question": "Check the saved arithmetic."},
        )


def wait_done(host, run_id):
    end = time.monotonic() + 5
    while time.monotonic() < end:
        state = host.state(run_id)
        status = next(run.status for run in state.runs if run.id == run_id)
        if status in ("completed", "waiting"):
            return state, status
        time.sleep(0.01)
    pytest.fail("background run did not settle")


def wait_status(host, run_id, wanted):
    end = time.monotonic() + 5
    while time.monotonic() < end:
        state = host.state(run_id)
        status = next(run.status for run in state.runs if run.id == run_id)
        if status == wanted:
            return state
        time.sleep(0.01)
    pytest.fail(f"run did not reach {wanted}")


def test_start_peer_followup_and_restart(tmp_path):
    provider = FakeProvider()
    host = ResearchHost(ROOT, tmp_path, provider)
    request = LiveStartRequest(
        request_id="request-start",
        question="What does this text say?",
        evidence_text="The measured result was small. Methods were not supplied.",
        agent_ids=["agent-synthesis"],
    )
    receipt = host.start(request)
    assert receipt.status == "queued"
    assert host.start(request).model_dump()["reused"] is True
    with pytest.raises(LiveError) as conflict:
        host.start(request.model_copy(update={"question": "Changed question"}))
    assert conflict.value.status == 409

    state, status = wait_done(host, receipt.run_id)
    assert status == "completed", state.error
    snapshot = state.snapshot
    assert snapshot.proof_kind == "live_model_on_supplied_evidence"
    assert snapshot.sources[0].passages[0].text == request.evidence_text
    assert {agent.name for agent in host.agents} == {"Sage", "Mira"}
    assert any(message.status == "fulfilled" for message in snapshot.messages)
    assert any(attempt.agent_id == "agent-critique" for attempt in snapshot.attempts)
    assert any("Revised answer" in artifact.body for artifact in snapshot.artifacts)
    report = next(
        artifact
        for artifact in snapshot.artifacts
        if artifact.id == snapshot.mission.report_artifact_id
    )
    assert report.body == "Revised answer after Mira's critique."
    assert report.producer_attempt_id == next(
        attempt.id
        for attempt in snapshot.attempts
        if attempt.agent_id == "agent-synthesis"
        and attempt.result_summary == report.body
    )
    structured = next(
        artifact
        for artifact in snapshot.artifacts
        if artifact.body == "Initial answer grounded in supplied evidence."
    )
    assert [(item.source_id, item.span_id) for item in structured.citations] == [
        (snapshot.sources[0].id, snapshot.sources[0].passages[0].span_id)
    ]
    assert structured.limitations == ["Only supplied text was examined."]
    assert structured.producer_attempt_id in {attempt.id for attempt in snapshot.attempts}
    sage_task = next(
        task for task in snapshot.tasks if task.id == f"{receipt.task_id}-agent-synthesis"
    )
    peer_task = next(
        task
        for task in snapshot.tasks
        if task.parent_task_id == sage_task.id and task.id != sage_task.id
    )
    assert peer_task.id != receipt.task_id
    assert len(host.calls(receipt.run_id)) == 6
    traces = [record["trace"] for record in host.calls(receipt.run_id)]
    resumed = next(trace for trace in traces if trace["peerInputs"])
    peer_input = resumed["peerInputs"][0]
    assert resumed["basis"] == "linked_saved_records"
    assert resumed["sourceSnapshotId"] == snapshot.snapshot_id
    assert resumed["author"]["agentId"] == "agent-synthesis"
    assert resumed["author"]["name"] == "Sage"
    assert resumed["author"]["role"] == "evidence preparation and synthesis"
    assert resumed["author"]["taskId"] == sage_task.id
    assert resumed["author"]["attemptId"]
    assert resumed["author"]["runId"] == receipt.run_id
    assert resumed["outputs"] == [
        {
            "authoredByAgentId": "agent-synthesis",
            "recordPath": "response.content",
        }
    ]
    assert peer_input["linkStatus"] == "linked"
    assert peer_input["requestMessageId"]
    assert peer_input["responseMessageId"]
    assert peer_input["peerAgentId"] == "agent-critique"
    assert peer_input["peerAgentName"] == "Mira"
    assert peer_input["sourceAttemptResolution"] == "unique"
    assert peer_input["sourceAttemptId"] == next(
        attempt.id
        for attempt in snapshot.attempts
        if attempt.task_id == peer_task.id and attempt.agent_id == "agent-critique"
    )
    assert all(
        not record["trace"]["peerInputs"]
        for record in host.calls(receipt.run_id)
        if record["agentId"] == "agent-critique"
    )
    assert any(
        record["response"]["tool_calls"]
        and record["response"]["tool_calls"][0]["function"]["name"] == "publish_result"
        and record["toolResults"][0]["content"] == '{"status":"published"}'
        for record in host.calls(receipt.run_id)
    )
    assert all(
        record["response"]["usage"]["total_tokens"] == 100 for record in host.calls(receipt.run_id)
    )
    first_snapshot_id = snapshot.snapshot_id
    target = snapshot.artifacts[0]

    provider.followup = True
    follow = LiveFollowupRequest(
        request_id="request-follow",
        parent_run_id=receipt.run_id,
        parent_snapshot_id=first_snapshot_id,
        target_id=target.id,
        target_version=target.version,
        question="What limitation remains?",
        agent_ids=["agent-synthesis"],
    )
    follow_receipt = host.followup(follow)
    follow_state, follow_status = wait_done(host, follow_receipt.run_id)
    assert follow_status == "completed", follow_state.error
    assert follow_state.comparison_snapshot.snapshot_id == first_snapshot_id
    assert follow_state.snapshot.mission.id == snapshot.mission.id
    assert follow_state.snapshot.mission.version == snapshot.mission.version + 1
    assert follow_state.snapshot.parent_snapshot_id == first_snapshot_id
    assert follow_state.snapshot.sources == snapshot.sources
    inherited_attempt = snapshot.attempts[0].id
    inherited_calls = host.calls(follow_receipt.run_id, inherited_attempt)
    parent_calls = host.calls(receipt.run_id, inherited_attempt)
    assert len(inherited_calls) == len(parent_calls)
    assert all(
        child["id"] == parent["id"]
        and child["messages"] == parent["messages"]
        and child["response"] == parent["response"]
        and child["trace"]["sourceSnapshotId"] == follow_state.snapshot.snapshot_id
        for child, parent in zip(inherited_calls, parent_calls)
    )
    assert inherited_calls and all(item["runId"] == receipt.run_id for item in inherited_calls)
    assert len(host.calls(follow_receipt.run_id)) == 1
    assert host.calls(follow_receipt.run_id, "missing-attempt") == []
    assert host.followup(follow).reused
    with pytest.raises(LiveError) as stale:
        host.followup(
            follow.model_copy(
                update={"request_id": "stale-target", "target_version": target.version + 1}
            )
        )
    assert stale.value.status == 409
    task_follow = follow.model_copy(
        update={
            "request_id": "task-follow",
            "target_id": receipt.task_id,
            "target_version": snapshot.mission.version,
        }
    )
    task_receipt = host.followup(task_follow)
    task_state, task_status = wait_done(host, task_receipt.run_id)
    assert task_status == "completed"
    assert host.calls(follow_receipt.run_id, task_state.snapshot.attempts[-1].id) == []
    host.close()

    reopened = ResearchHost(ROOT, tmp_path, provider)
    assert (
        reopened.state(follow_receipt.run_id).snapshot.snapshot_id
        == follow_state.snapshot.snapshot_id
    )
    assert len(reopened.calls(receipt.run_id)) == 6
    reopened.close()


def test_budget_waits_without_call_or_false_completion(tmp_path):
    host = ResearchHost(ROOT, tmp_path, FakeProvider(), run_budget_usd=0.0001)
    receipt = host.start(
        LiveStartRequest(
            request_id="budget",
            question="Assess",
            evidence_text="Saved source.",
            agent_ids=["agent-synthesis"],
        )
    )
    state, status = wait_done(host, receipt.run_id)
    assert status == "waiting"
    assert state.snapshot.mission.status == "waiting"
    assert host.calls(receipt.run_id) == []
    host.close()


def test_selected_peers_have_separate_work_and_critique_does_not_replace_report(tmp_path):
    host = ResearchHost(ROOT, tmp_path, TwoPeersProvider())
    receipt = host.start(
        LiveStartRequest(
            request_id="two-peers",
            question="Assess",
            evidence_text="Saved source.",
            agent_ids=["agent-synthesis", "agent-critique"],
        )
    )
    state, status = wait_done(host, receipt.run_id)
    assert status == "completed", state.error
    snapshot = state.snapshot
    selected = [task for task in snapshot.tasks if task.parent_task_id == receipt.task_id]
    assert {task.id for task in selected} == {
        f"{receipt.task_id}-agent-synthesis",
        f"{receipt.task_id}-agent-critique",
    }
    assert all(task.status == "completed" for task in selected)
    report = next(
        artifact
        for artifact in snapshot.artifacts
        if artifact.id == snapshot.mission.report_artifact_id
    )
    assert report.body == "Sage independent answer."
    assert report.limitations == []
    assert report.producer_attempt_id == next(
        attempt.id
        for attempt in snapshot.attempts
        if attempt.agent_id == "agent-synthesis"
    )
    assert any(artifact.body == "Mira independent answer." for artifact in snapshot.artifacts)
    assert all(record["trace"]["peerInputs"] == [] for record in host.calls(receipt.run_id))
    host.close()


def test_name_mention_without_saved_peer_exchange_has_no_peer_link(tmp_path):
    host = ResearchHost(ROOT, tmp_path, MentionProvider())
    receipt = host.start(
        LiveStartRequest(
            request_id="mention",
            question="Assess",
            evidence_text="Saved source.",
            agent_ids=["agent-synthesis"],
        )
    )
    wait_status(host, receipt.run_id, "completed")
    record = host.calls(receipt.run_id)[0]
    assert "Mira" in record["response"]["content"]
    assert record["trace"]["peerInputs"] == []
    assert record["trace"]["outputs"] == [
        {
            "authoredByAgentId": "agent-synthesis",
            "recordPath": "response.content",
        }
    ]
    host.close()


@pytest.mark.parametrize(
    ("read_source", "include_exact"), [(True, True), (True, False), (False, True)]
)
def test_plain_text_keeps_exact_read_citation_and_explicit_limitations(
    tmp_path, read_source, include_exact
):
    provider = PlainTextProvider(read_source, include_exact)
    host = ResearchHost(ROOT, tmp_path, provider)
    receipt = host.start(
        LiveStartRequest(
            request_id="plain-text",
            question="Assess the observation",
            evidence_text="Eight of twenty had the recorded outcome.",
            agent_ids=["agent-synthesis"],
        )
    )
    state, status = wait_done(host, receipt.run_id)
    assert status == "completed", state.error
    snapshot = state.snapshot
    report = next(
        item for item in snapshot.artifacts if item.id == snapshot.mission.report_artifact_id
    )
    assert report.body == provider.answer
    assert report.producer_attempt_id == snapshot.attempts[0].id
    assert report.limitations == ["Only the supplied observation was examined."]
    expected = (
        [(snapshot.sources[0].id, snapshot.sources[0].passages[0].span_id)]
        if read_source and include_exact
        else []
    )
    assert [(item.source_id, item.span_id) for item in report.citations] == expected
    assert [(item.source_id, item.span_id) for item in snapshot.claims[0].supports] == expected
    assert "span-not-saved" in report.body
    host.close()


@pytest.mark.parametrize(
    ("limitation_block", "expected"),
    [
        ("**Limitation:** Only supplied evidence.", ["Only supplied evidence."]),
        (
            "## Limitations\n\nOnly one supplied observation was examined.\n"
            "The result may not generalize.\n\n## Next steps\nObtain more evidence.",
            ["Only one supplied observation was examined. The result may not generalize."],
        ),
        ("~~~text\n# Limitations\nExample-only caution.\n~~~", []),
        (
            "```text\n# Limitations\nExample-only caution.\n```\n\n"
            "## Limitations\n\nActual caution.\n\n## Next steps\nCheck more sources.",
            ["Actual caution."],
        ),
    ],
)
def test_plain_text_markdown_limitations_stop_at_next_section(
    tmp_path, limitation_block, expected
):
    provider = PlainTextProvider(False, limitation_block=limitation_block)
    host = ResearchHost(ROOT, tmp_path, provider)
    receipt = host.start(
        LiveStartRequest(
            request_id="plain-limitations",
            question="Assess the observation",
            evidence_text="Supplied observation.",
            agent_ids=["agent-synthesis"],
        )
    )
    state, status = wait_done(host, receipt.run_id)
    assert status == "completed", state.error
    report = next(
        item
        for item in state.snapshot.artifacts
        if item.id == state.snapshot.mission.report_artifact_id
    )
    assert report.body == provider.answer
    assert report.limitations == expected
    host.close()


@pytest.mark.parametrize("explicit_empty", [False, True])
def test_publish_result_omitted_metadata_recovers_but_explicit_empty_is_preserved(
    tmp_path, explicit_empty
):
    provider = PublishMetadataProvider(explicit_empty)
    host = ResearchHost(ROOT, tmp_path, provider)
    receipt = host.start(
        LiveStartRequest(
            request_id="publish-metadata",
            question="Assess the observation",
            evidence_text="Supplied observation.",
            agent_ids=["agent-synthesis"],
        )
    )
    state, status = wait_done(host, receipt.run_id)
    assert status == "completed", state.error
    snapshot = state.snapshot
    report = next(
        item for item in snapshot.artifacts if item.id == snapshot.mission.report_artifact_id
    )
    assert report.body == provider.answer
    assert [(item.source_id, item.span_id) for item in report.citations] == (
        [] if explicit_empty else [(snapshot.sources[0].id, snapshot.sources[0].passages[0].span_id)]
    )
    assert report.limitations == (
        [] if explicit_empty else ["Only the supplied text was read."]
    )
    host.close()


def test_ambiguous_peer_source_attempt_is_not_guessed(tmp_path):
    host = ResearchHost(ROOT, tmp_path, FakeProvider())
    receipt = host.start(
        LiveStartRequest(
            request_id="ambiguous",
            question="What does this text say?",
            evidence_text="Saved source.",
            agent_ids=["agent-synthesis"],
        )
    )
    state = wait_status(host, receipt.run_id, "completed")
    peer_attempt = next(
        attempt for attempt in state.snapshot.attempts if attempt.agent_id == "agent-critique"
    )

    def add_equally_matching_attempt(snapshot):
        original = next(item for item in snapshot["attempts"] if item["id"] == peer_attempt.id)
        duplicate = deepcopy(original)
        duplicate["id"] = "attempt-other-matching-peer"
        snapshot["attempts"].append(duplicate)

    host._mutate(receipt.run_id, add_equally_matching_attempt)
    linked = next(
        item for record in host.calls(receipt.run_id) for item in record["trace"]["peerInputs"]
    )
    assert linked["linkStatus"] == "linked"
    assert linked["peerAgentId"] == "agent-critique"
    assert linked["sourceAttemptResolution"] == "ambiguous"
    assert linked["sourceAttemptId"] is None
    host.close()


def test_nested_peer_reply_revises_its_actual_requesting_task(tmp_path):
    host = ResearchHost(ROOT, tmp_path, NestedPeersProvider())
    receipt = host.start(
        LiveStartRequest(
            request_id="nested-peers",
            question="Assess",
            evidence_text="Saved source.",
            agent_ids=["agent-synthesis"],
        )
    )
    state, status = wait_done(host, receipt.run_id)
    assert status == "completed", state.error
    snapshot = state.snapshot
    sage_task_id = f"{receipt.task_id}-agent-synthesis"
    mira_task = next(task for task in snapshot.tasks if task.parent_task_id == sage_task_id)
    sage_nested = next(task for task in snapshot.tasks if task.parent_task_id == mira_task.id)
    assert sage_nested.id != sage_task_id
    assert any(
        claim.task_id == mira_task.id and claim.text == "mira revised after peer reply."
        for claim in snapshot.claims
    )
    assert any(
        claim.task_id == sage_task_id and claim.text == "sage revised after peer reply."
        for claim in snapshot.claims
    )
    assert len([message for message in snapshot.messages if message.status == "delivered"]) == 2
    host.close()


def test_truncated_completion_is_counted_but_not_published(tmp_path):
    host = ResearchHost(ROOT, tmp_path, TruncatedProvider())
    receipt = host.start(
        LiveStartRequest(
            request_id="truncated",
            question="Assess",
            evidence_text="Saved source.",
            agent_ids=["agent-synthesis"],
        )
    )
    state, status = wait_done(host, receipt.run_id)
    assert status == "waiting"
    assert "truncated" in state.error
    assert state.snapshot.artifacts == []
    records = host.calls(receipt.run_id)
    assert len(records) == 1
    assert records[0]["actualUsd"] == 0.0002
    host.close()


def test_interrupted_call_is_uncertain_and_never_replayed(tmp_path):
    provider = FakeProvider()
    host = ResearchHost(ROOT, tmp_path, provider)
    receipt = host.start(
        LiveStartRequest(
            request_id="interrupted",
            question="Assess",
            evidence_text="Saved source.",
            agent_ids=["agent-synthesis"],
        )
    )
    wait_done(host, receipt.run_id)
    host.close()
    # Simulate process loss after a call was durably admitted but before it returned.
    with sqlite3.connect(tmp_path / "research.sqlite3") as db:
        db.execute("UPDATE runs SET status='running' WHERE id=?", (receipt.run_id,))
        db.execute(
            "UPDATE calls SET status='in_flight',phase='provider',completed_at=NULL "
            "WHERE id=(SELECT id FROM calls WHERE run_id=? LIMIT 1)",
            (receipt.run_id,),
        )
    count = provider.sage_calls
    reopened = ResearchHost(ROOT, tmp_path, provider)
    assert (
        next(run.status for run in reopened.state(receipt.run_id).runs if run.id == receipt.run_id)
        == "waiting"
    )
    assert reopened.calls(receipt.run_id)[0]["status"] == "uncertain"
    assert provider.sage_calls == count
    reopened.close()


def test_queued_run_without_provider_call_resumes_after_restart(tmp_path):
    provider = TwoPeersProvider()
    host = ResearchHost(ROOT, tmp_path, provider)
    host._executor.submit = lambda *_args: None
    receipt = host.start(
        LiveStartRequest(
            request_id="queued-restart",
            question="Assess",
            evidence_text="Saved source.",
            agent_ids=["agent-synthesis"],
        )
    )
    assert host.calls(receipt.run_id) == []
    host.close()
    reopened = ResearchHost(ROOT, tmp_path, provider)
    state, status = wait_done(reopened, receipt.run_id)
    assert status == "completed", state.error
    assert len(reopened.calls(receipt.run_id)) == 1
    reopened.close()


def test_rate_limit_wait_resumes_same_attempt_and_messages(tmp_path):
    provider = RateLimitOnceProvider()
    host = ResearchHost(ROOT, tmp_path, provider)
    receipt = host.start(
        LiveStartRequest(
            request_id="rate-limit",
            question="Assess",
            evidence_text="Saved source.",
            agent_ids=["agent-synthesis"],
        )
    )
    state = wait_status(host, receipt.run_id, "completed")
    assert state.snapshot.mission.status == "completed"
    records = host.calls(receipt.run_id)
    assert [record["status"] for record in records] == [
        "returned",
        "rate_limited",
        "returned",
    ]
    assert records[1]["actualUsd"] is None
    assert records[1]["response"]["retry_after"] == 0.03
    assert records[2]["retryOfCallId"] == records[1]["id"]
    assert provider.requests[1] == provider.requests[2]
    assert any(
        call["tool_call_id"] == "tool-source_read"
        for call in provider.requests[2]
        if call["role"] == "tool"
    )
    attempt = state.snapshot.attempts[0]
    assert attempt.status == "completed"
    assert [tool.name for tool in attempt.tool_calls].count("source_read") == 1
    assert sum(record["actualUsd"] or 0 for record in records) == pytest.approx(0.0004)
    assert any(diagnostic.kind == "quota" for diagnostic in state.snapshot.diagnostics)
    host.close()


def test_close_interrupts_long_quota_wait_and_other_run_can_finish(tmp_path):
    provider = LongRateLimitProvider()
    host = ResearchHost(ROOT, tmp_path, provider)
    waiting = host.start(
        LiveStartRequest(
            request_id="wait",
            question="Wait",
            evidence_text="Saved source.",
            agent_ids=["agent-synthesis"],
        )
    )
    state = wait_status(host, waiting.run_id, "waiting")
    assert "retrying" in state.error
    other = host.start(
        LiveStartRequest(
            request_id="other",
            question="Other",
            evidence_text="Another saved source.",
            agent_ids=["agent-synthesis"],
        )
    )
    wait_status(host, other.run_id, "completed")
    started = time.monotonic()
    host.close()
    assert time.monotonic() - started < 1
    assert provider.waiting_calls == 1
    assert host.calls(waiting.run_id)[0]["status"] == "rate_limited"


def test_existing_local_call_table_adds_retry_link_without_losing_rows(tmp_path):
    with sqlite3.connect(tmp_path / "research.sqlite3") as db:
        db.execute(
            "CREATE TABLE calls (id TEXT PRIMARY KEY, run_id TEXT NOT NULL,"
            "attempt_id TEXT NOT NULL, agent_id TEXT NOT NULL, status TEXT NOT NULL,"
            "phase TEXT NOT NULL, messages TEXT NOT NULL, response TEXT,"
            "estimated_max_usd REAL NOT NULL, actual_usd REAL,"
            "created_at TEXT NOT NULL, completed_at TEXT)"
        )
        db.execute(
            "INSERT INTO calls(id,run_id,attempt_id,agent_id,status,phase,messages,"
            "estimated_max_usd,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "saved-call",
                "saved-run",
                "saved-attempt",
                "agent-synthesis",
                "returned",
                "response",
                "[]",
                0.001,
                "2026-01-01T00:00:00Z",
            ),
        )
    host = ResearchHost(ROOT, tmp_path, TwoPeersProvider())
    with sqlite3.connect(tmp_path / "research.sqlite3") as db:
        columns = {row[1] for row in db.execute("PRAGMA table_info(calls)")}
        preserved = db.execute("SELECT id,tool_results FROM calls WHERE id='saved-call'").fetchone()
    assert "retry_of_call_id" in columns
    assert "tool_results" in columns
    assert preserved == ("saved-call", "[]")
    host.close()


def test_followup_peer_receives_exact_target_and_fulfilled_delegation_context(tmp_path):
    host = ResearchHost(ROOT, tmp_path, PeerFollowupProvider())
    started = host.start(
        LiveStartRequest(
            request_id="start-for-peer",
            question="Calculate rate",
            evidence_text="Five of twenty screened records were included.",
            agent_ids=["agent-synthesis"],
        )
    )
    start_state = wait_status(host, started.run_id, "completed")
    report = next(
        artifact
        for artifact in start_state.snapshot.artifacts
        if artifact.id == start_state.snapshot.mission.report_artifact_id
    )
    followed = host.followup(
        LiveFollowupRequest(
            request_id="follow-with-peer",
            parent_run_id=started.run_id,
            parent_snapshot_id=start_state.snapshot.snapshot_id,
            target_id=report.id,
            target_version=report.version,
            question="Ask Mira to check the saved arithmetic, then revise.",
            scope="Use saved evidence. Ask for one focused peer check; do not repeat it.",
            agent_ids=["agent-synthesis"],
        )
    )
    state = wait_status(host, followed.run_id, "completed")
    assert state.snapshot.mission.report_artifact_id
    peer_requests = [
        tool
        for record in host.calls(followed.run_id)
        for tool in (record["response"] or {}).get("tool_calls", [])
        if tool["function"]["name"] == "ask_peer"
    ]
    assert len(peer_requests) == 1
    assert len(host.calls(followed.run_id)) == 4
    host.close()
