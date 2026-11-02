"""Large numerical results remain exact, pageable and permission checked."""

import hashlib
import json

import pytest

from ora.contracts.models import LiveStartRequest
from ora.coordination.board import ref
from ora.mission.analysis_extended import adjust_pvalues
from ora.mission.context import ContextError, WorkingContext
from ora.mission.tool_results import MAX_RESULT_PAGE_CHARS, ResultProjectionChanged
from ora.models.token_estimate import estimate_request_tokens
from ora.state import StateStore
from ora.state.store import canonical
from tests.mission.test_context import bind_no_source_pack, fixture
from tests.mission.test_note_source_use import revoke, source_note
from tests.mission.test_service import answer, drain, service, tool
from tests.mission.test_tool_result_paging import save_result


def _family():
    return {
        "family_id": "synthetic-two-hundred-tests", "alpha": 0.05,
        "method": "benjamini_hochberg",
        "tests": [{"test_id": f"t{i:03d}", "p_value": round((i + 1) / 1000, 4)}
                  for i in range(200)],
    }


def _read(context, result, **kwargs):
    return context.read_tool_result(
        mission_id="m", actor_id="peer", task_id="task", result_id=result["id"],
        offset=kwargs.get("offset", 0), limit=kwargs.get("limit", 1800),
        projection_sha256=kwargs.get("projection_sha256", ""),
    )


def _bind_producing_input(store, call):
    store.transact("numeric-producer-intent", {}, lambda tx: ref(tx.put(
        "model_request_intent", f"model-intent:{call['id']}:{call['version']}", {
            "call_ref": call, "source_refs": [], "context_source_dependency_refs": [],
        },
    )), mission_id="m")


def _save_page_with_assistant_prose(store, call, page, arguments):
    intent = store.get("model_request_intent", f"model-intent:{call['id']}:{call['version']}")
    request_sha = intent["body"].get("request_sha256") if intent else None
    raw = store.put_blob(canonical({
        "content": "SOURCE_DERIVED_PAGE_INTERPRETATION",
        "tool_calls": [{
            "id": "tool-numeric-page", "type": "function",
            "function": {"name": "read_tool_result", "arguments": canonical(arguments)},
        }],
    }).encode())
    response = store.transact("numeric-page-response", {}, lambda tx: ref(tx.put(
        "model_response_receipt", "response-numeric-page", {
            "mission_id": "m", "task_id": "task", "call_ref": call,
            "status": "tool_requested", "blob": raw,
            **({"intent_ref": ref(intent), "request_sha256": request_sha}
               if isinstance(request_sha, str) else {}),
        },
    )), mission_id="m")
    WorkingContext(store).save_tool_result(
        "numeric-page-result", response_ref=response,
        tool_call_id="tool-numeric-page", name="read_tool_result", content=canonical(page),
    )
    return response


def test_large_numeric_result_pages_without_recomputing_or_replaying_arguments(tmp_path):
    store = StateStore(tmp_path)
    call, _ = fixture(store)
    _bind_producing_input(store, call)
    arguments = _family()
    content = adjust_pvalues(**arguments)
    content["source_dependency_refs"] = []
    response, result = save_result(
        store, call, "adjust_pvalues", content, arguments=arguments,
    )
    original_response = store.read_blob(store.get(**response)["body"]["blob"]["sha256"])
    original_result = store.read_blob(store.get(**result)["body"]["blob"]["sha256"])
    context = WorkingContext(store)
    exchange, receipt = context._tool_exchange(response, scope={
        "mission_id": "m", "actor_id": "peer", "task_id": "task",
    })
    assert exchange[0]["tool_calls"][0]["function"]["arguments"] == "{}"
    expected_hash = hashlib.sha256(canonical(arguments).encode()).hexdigest()
    assert receipt["original_arguments_sha256"] == expected_hash
    assert receipt["argument_status"] == "saved_numeric_arguments_archived_for_model_input"
    page = json.loads(exchange[1]["content"])
    assert page["schema"] == "ora.saved_tool_result_window.v1"
    assert page["original_arguments_sha256"] == expected_hash
    pieces = []
    while True:
        assert len(canonical(page)) <= MAX_RESULT_PAGE_CHARS
        pieces.append(page["text"])
        if not page["next_request"]:
            break
        next_args = {key: value for key, value in page["next_request"].items() if key != "tool"}
        page = WorkingContext(StateStore(tmp_path)).read_tool_result(
            mission_id="m", actor_id="peer", task_id="task", **next_args,
        )
    assert json.loads("".join(pieces)) == content
    assert len(pieces) > 2
    assert len(store.records("context_tool_result")) == 1
    assert store.read_blob(store.get(**response)["body"]["blob"]["sha256"]) == original_response
    assert store.read_blob(store.get(**result)["body"]["blob"]["sha256"]) == original_result


def test_numeric_page_rechecks_scope_and_revocation_before_replay_or_preparation(tmp_path):
    store = StateStore(tmp_path)
    call, task = fixture(store)
    _bind_producing_input(store, call)
    source = source_note(store, task)
    arguments = _family()
    content = adjust_pvalues(**arguments)
    content["source_dependency_refs"] = [source["copy"]]
    response, result = save_result(
        store, call, "adjust_pvalues", content, arguments=arguments,
    )
    context = WorkingContext(store)
    first = _read(context, result)
    assert "synthetic-two-hundred-tests" in first["text"]
    with pytest.raises(ContextError, match="another peer task"):
        context.read_tool_result(
            mission_id="m", actor_id="foreign", task_id="task", result_id=result["id"],
            offset=0, limit=100, projection_sha256="",
        )
    next_args = {key: value for key, value in first["next_request"].items() if key != "tool"}
    page = context.read_tool_result(mission_id="m", actor_id="peer", task_id="task", **next_args)
    page_response = _save_page_with_assistant_prose(store, call, page, next_args)
    pack = context.assemble(call, task, tool_turn_refs=[page_response])
    assert source["copy"] in context.context_source_dependency_refs(pack)
    revoke(store, source["use"])
    with pytest.raises(ResultProjectionChanged):
        context.read_tool_result(mission_id="m", actor_id="peer", task_id="task", **next_args)
    replay, _ = WorkingContext(StateStore(tmp_path))._tool_exchange(page_response)
    assert json.loads(replay[1]["content"])["status"] == "projection_changed_restart_required"
    assert replay[0]["content"] is None
    assert "synthetic-two-hundred-tests" not in canonical(replay)
    direct, _ = context._tool_exchange(response)
    assert direct[0]["tool_calls"][0]["function"]["arguments"] == "{}"
    assert "data_unavailable_due_source_use" in direct[1]["content"]
    current = _read(context, result)
    assert "data_unavailable_due_source_use" in current["text"]
    assert "synthetic-two-hundred-tests" not in current["text"]
    with pytest.raises(ContextError, match="source use changed"):
        context.context_source_dependency_refs(pack)


def test_reader_source_prose_revokes_without_hiding_unrelated_numeric_page(tmp_path):
    store = StateStore(tmp_path)
    producer_call, task = fixture(store)
    _bind_producing_input(store, producer_call)
    family = _family()
    content = adjust_pvalues(**family)
    content["source_dependency_refs"] = []
    _, result = save_result(
        store, producer_call, "adjust_pvalues", content, arguments=family,
    )
    source_b = source_note(store, task)
    original_body = store.get(**producer_call)["body"]
    source_text = store.get(**source_b["passage"])["body"]["text"]
    request_bytes = canonical({
        "model": original_body["model_id"],
        "messages": [
            {"role": "developer", "content": "Synthetic numerical reader"},
            {"role": "user", "content": canonical({"source_packets": [{
                "copy_ref": source_b["copy"],
                "passage_refs": [source_b["passage"]], "text": source_text,
            }]})},
        ],
    }).encode()
    assert source_text.encode() in request_bytes
    request_blob = store.put_blob(request_bytes)
    request_sha = hashlib.sha256(request_bytes).hexdigest()

    def reader_with_source(tx):
        reader = tx.put("agent_call", "numeric-reader-call", original_body)
        pointer = ref(reader)
        tx.put("model_request_intent", f"model-intent:{reader['id']}:{reader['version']}", {
            "mission_id": "m", "instance_id": "peer", "task_id": "task",
            "call_ref": pointer, "source_refs": [source_b["copy"]],
            "context_source_dependency_refs": [],
            "request_sha256": request_sha, "blob": request_blob,
        })
        return pointer

    reader_call = store.transact("numeric-reader-input", {}, reader_with_source, mission_id="m")
    context = WorkingContext(store)
    first = _read(context, result)
    next_args = {key: value for key, value in first["next_request"].items() if key != "tool"}
    page = context.read_tool_result(mission_id="m", actor_id="peer", task_id="task", **next_args)
    page_response = _save_page_with_assistant_prose(store, reader_call, page, next_args)
    page_result = store.get("context_tool_result", f"tool-result:{page_response['id']}")
    response_blob = store.read_blob(store.get(**page_response)["body"]["blob"]["sha256"])
    result_blob = store.read_blob(page_result["body"]["blob"]["sha256"])
    before, _ = context._tool_exchange(page_response)
    assert before[0]["content"] == "SOURCE_DERIVED_PAGE_INTERPRETATION"
    assert json.loads(before[1]["content"])["projection_sha256"] == page["projection_sha256"]

    next_call = store.transact("numeric-next-call", {}, lambda tx: ref(tx.put(
        "agent_call", "numeric-next-call", original_body,
    )), mission_id="m")
    pack = context.assemble(next_call, task, tool_turn_refs=[page_response])
    assert source_b["copy"] in context.context_source_dependency_refs(pack)
    bind_no_source_pack(store, context, pack, "numeric-page-prose")
    prepared = store.get("model_request_intent", f"model-intent:{next_call['id']}:1")
    assert source_b["copy"] in prepared["body"]["context_source_dependency_refs"]

    revoke(store, source_b["use"])
    assert context.read_tool_result(
        mission_id="m", actor_id="peer", task_id="task", **next_args,
    ) == page
    after, _ = WorkingContext(StateStore(tmp_path))._tool_exchange(page_response)
    assert after[0]["content"] is None
    assert json.loads(after[1]["content"])["projection_sha256"] == page["projection_sha256"]
    assert after[1]["content"] == before[1]["content"]
    with pytest.raises(ContextError, match="source use changed"):
        context.context_source_dependency_refs(pack)
    assert store.read_blob(store.get(**page_response)["body"]["blob"]["sha256"]) == response_blob
    assert store.read_blob(page_result["body"]["blob"]["sha256"]) == result_blob


def test_host_exposes_bounded_saved_fdr_page_and_reader_for_200_tests(tmp_path, monkeypatch):
    requests = []
    family = _family()

    def with_synthetic_limit(response):
        response.headers = {
            "x-request-id": "offline-fixture",
            "x-ratelimit-limit-tokens": "8000",
            "x-ratelimit-remaining-tokens": "8000",
        }
        return response

    def transport(request, *, timeout):
        packet = json.loads(request.data)
        requests.append(packet)
        step = len(requests)
        if step == 1:
            return with_synthetic_limit(tool("request_capability", {"tool_id": "adjust_pvalues"}, step))
        if step == 2:
            return with_synthetic_limit(tool("adjust_pvalues", family, step))
        assert estimate_request_tokens(request.data).tokens < 8000
        if step == 3:
            assistant = next(message for message in reversed(packet["messages"])
                             if message["role"] == "assistant" and message.get("tool_calls"))
            assert assistant["tool_calls"][0]["function"]["arguments"] == "{}"
            assert assistant["content"] is None
            last = next(message for message in reversed(packet["messages"])
                        if message["role"] == "tool")
            first = json.loads(last["content"])
            assert first["schema"] == "ora.saved_tool_result_window.v1"
            assert first["arguments_status"] == "saved_numeric_arguments_archived_for_model_input"
            assert "read_tool_result" in {item["function"]["name"] for item in packet["tools"]}
            return with_synthetic_limit(tool("read_tool_result", {
                key: value for key, value in first["next_request"].items() if key != "tool"
            }, step))
        assert step == 4
        last = next(message for message in reversed(packet["messages"])
                    if message["role"] == "tool")
        assert last["name"] == "read_tool_result"
        assert json.loads(last["content"])["schema"] == "ora.saved_tool_result_window.v1"
        return with_synthetic_limit(answer(
            "The supplied family was adjusted once; source validity is unverified.", step,
        ))

    host = service(tmp_path, monkeypatch, transport)
    try:
        run = host.start(LiveStartRequest(
            request_id="synthetic-large-fdr-page", question="Adjust one synthetic family of 200 p-values.",
            agent_ids=["agent-synthesis"],
        ))
        drain(host, run.run_id, limit=30)
        assert host._run(run.run_id)["body"]["status"] == "completed"
        assert len(requests) == 4
    finally:
        host.close()
    reopened = StateStore(tmp_path, read_only=True)
    names = []
    for row in reopened.records("context_tool_result"):
        payload = json.loads(reopened.read_blob(row["body"]["blob"]["sha256"]))
        names.append(payload["name"])
        if payload["name"] == "adjust_pvalues":
            assert len(json.loads(payload["content"])["tests"]) == 200
    assert names.count("adjust_pvalues") == 1
    assert names.count("read_tool_result") == 1
