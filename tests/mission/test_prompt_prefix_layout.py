"""Saved model inputs reuse static prefixes without changing active tool access."""

from __future__ import annotations

import copy
import json

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.mission.skill_activation import CALLABILITY_GUIDANCE
from ora.models.token_estimate import encode_context_token_ids, estimate_request_tokens
from ora.state.store import canonical
from tests.mission.test_service import service

_STABLE_FIRST = (
    "discover_capabilities", "request_capability", "publish_result", "read_context_page",
)


def _prepared(host, run_id: str, alias: str) -> tuple[dict, dict, dict]:
    control = host._control(run_id, alias)
    assert host._schedule_model(control)
    call = host.store.get(**host._control(run_id, alias)["body"]["call_ref"])
    intent = host.store.get(
        "model_request_intent", f"model-intent:{call['id']}:{call['version']}",
    )
    packet = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
    return packet, call, intent


def _prior_layout(packet: dict) -> dict:
    """Restore the former developer identity and alphabetical tool presentation."""
    prior = copy.deepcopy(packet)
    parts = prior["messages"][0]["content"].split("\n\n")
    identities = [part for part in parts if part.startswith("You are ")]
    assert len(identities) == 1
    parts.remove(identities[0])
    prior["messages"][0]["content"] = "\n\n".join([identities[0], *parts])
    prior["tools"].sort(key=lambda tool: tool["function"]["name"])
    return prior


def _common_prefix(left: str, right: str) -> int:
    first = encode_context_token_ids(left)
    second = encode_context_token_ids(right)
    return next((index for index, (a, b) in enumerate(zip(first, second)) if a != b),
                min(len(first), len(second)))


def test_serialized_peer_prefix_keeps_role_and_exact_callable_set(tmp_path, monkeypatch):
    def no_transport(*_args, **_kwargs):
        raise AssertionError("This layout check must not call a provider")

    host = service(tmp_path, monkeypatch, no_transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="stable-prefix-layout", question="Review a synthetic comparison.",
            evidence_text="Synthetic checked passage for both peers.",
            agent_ids=["agent-synthesis", "agent-critique"],
        ))
        critique = host._control(run.run_id, "agent-critique")
        host._update_control(critique, requested_capabilities=["calculate"])
        sage, sage_call, sage_intent = _prepared(host, run.run_id, "agent-synthesis")
        mxra, mxra_call, _mxra_intent = _prepared(host, run.run_id, "agent-critique")

        for packet, call, identity in ((sage, sage_call, "You are Sage,"),
                                       (mxra, mxra_call, "You are Mxra,")):
            parts = packet["messages"][0]["content"].split("\n\n")
            assert parts[0] == CALLABILITY_GUIDANCE
            assert sum(part.startswith(identity) for part in parts) == 1
            assert next(i for i, part in enumerate(parts) if part.startswith(identity)) < next(
                i for i, part in enumerate(parts)
                if part.startswith("Role capability catalog: ")
            )
            names = [tool["function"]["name"] for tool in packet["tools"]]
            assert names == sorted(names, key=lambda name: (
                0, _STABLE_FIRST.index(name)
            ) if name in _STABLE_FIRST else (1, name))
            assert set(names) == set(call["body"]["exposed_tools"])
            assert call["body"]["exposed_tools"] == sorted(names)
            assert set(names) < set(call["body"]["permitted_tools"])
            assert "invite_peer" not in names
            prior = _prior_layout(packet)
            assert sorted(parts) == sorted(prior["messages"][0]["content"].split("\n\n"))
            assert {tool["function"]["name"]: tool for tool in packet["tools"]} == {
                tool["function"]["name"]: tool for tool in prior["tools"]
            }
            assert estimate_request_tokens(canonical(packet).encode()).tokens == (
                estimate_request_tokens(canonical(prior).encode()).tokens
            )

        old_sage, old_mxra = _prior_layout(sage), _prior_layout(mxra)
        assert _common_prefix(canonical(sage["messages"]), canonical(mxra["messages"])) > (
            _common_prefix(canonical(old_sage["messages"]), canonical(old_mxra["messages"]))
            + 250
        )
        assert _common_prefix(canonical(sage["tools"]), canonical(mxra["tools"])) > (
            _common_prefix(canonical(old_sage["tools"]), canonical(old_mxra["tools"]))
        )
        assert sage["messages"][1] == mxra["messages"][1]  # Same selected source packet.

        # A later configuration revision never rewrites an already pinned input.
        original_blob = host.store.read_blob(sage_intent["body"]["blob"]["sha256"])
        original_config_id = sage_call["body"]["configuration_id"]
        current = host._control(run.run_id, "agent-synthesis")
        host._update_control(current, requested_capabilities=["invite_peer"])
        revised = host._configuration(host._control(run.run_id, "agent-synthesis"))
        assert revised["id"] != original_config_id
        assert host.store.get(**ref(sage_intent)) == sage_intent
        assert host.store.read_blob(sage_intent["body"]["blob"]["sha256"]) == original_blob
        assert host.session.load(ref(sage_intent)).request.request_sha256 == sage_intent[
            "body"
        ]["request_sha256"]
    finally:
        host.close()
