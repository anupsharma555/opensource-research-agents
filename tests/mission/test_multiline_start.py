"""A natural multiline question survives start and model-request preparation."""

from __future__ import annotations

import json

import pytest

from ora.contracts.models import LiveStartRequest
from tests.mission.test_service import service


def test_multiline_question_is_exact_in_saved_task_and_prepared_request(
    tmp_path, monkeypatch,
):
    question = (
        "Review the supplied draft:\n"
        "\tKeep its measures and qualifiers.\n"
        "\tExplain which conclusions are inferred."
    )
    host = service(tmp_path, monkeypatch, lambda *_args, **_kwargs: pytest.fail(
        "Preparing this request must not dispatch a model call"
    ))
    try:
        request = LiveStartRequest(
            request_id="multiline-start", question=question,
            scope="Use only supplied material.", agent_ids=["agent-synthesis"],
        )
        first = host.start(request)
        repeated = host.start(request)
        assert repeated.reused is True
        assert (repeated.run_id, repeated.task_id) == (first.run_id, first.task_id)
        run = host._run(first.run_id)
        control = host._control(first.run_id, "agent-synthesis")
        task = host.store.get("board_proposal", control["body"]["task_id"])
        assert run["body"]["question"] == task["body"]["objective"] == question

        assert host._prepare_model(control)
        call = host._control(first.run_id, "agent-synthesis")["body"]["call_ref"]
        intent = host.store.get(
            "model_request_intent", f"model-intent:{call['id']}:{call['version']}"
        )
        packet = json.loads(host.store.read_blob(intent["body"]["blob"]["sha256"]))
        index = json.loads(packet["messages"][-1]["content"])
        assert index["task"]["objective"] == question
        assert host.store.records("model_dispatch_attempt") == []
        assert host.store.records("model_response_receipt") == []
    finally:
        host.close()
