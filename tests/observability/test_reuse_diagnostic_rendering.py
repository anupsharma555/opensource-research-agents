"""Changed-tool explanations remain separate from run reuse and provider caching."""

from copy import deepcopy

from ora.observability.prefix_reuse import analyze_local_prefix_reuse
from tests.observability.test_prefix_reuse import _candidate
from tests.observability.test_trace_cli import trace_run


def test_changed_tool_list_report_explains_retained_definitions_without_relabeling_cache():
    tool = {"type": "function", "function": {
        "name": "synthetic_reader", "parameters": {"type": "object"},
    }}
    added = deepcopy(tool)
    added["function"]["name"] = "synthetic_reviewer"
    reuse = analyze_local_prefix_reuse([
        _candidate(1, "Read synthetic evidence", tools=[tool]),
        _candidate(2, "Review synthetic evidence", tools=[tool, added]),
    ])
    original = deepcopy(reuse)
    result = {"local_prefix_reuse": reuse, "loops": [
        {"call_id": "call-1", "dispatch_recorded": True, "status": "captured"},
        {"call_id": "call-2", "dispatch_recorded": True, "status": "captured"},
    ]}
    text = "\n".join(trace_run._local_input_reuse_markdown(result))
    assert "Unchanged definitions within changed tool lists" in text
    assert "| 2 | 1 | 1 / 2 |" in text
    assert "does not change the run-level reuse percentage or establish cache hits" in text
    assert "separately tokenized definition tokens" in text
    assert "Read synthetic evidence" not in text
    assert reuse == original
    legacy = {key: value for key, value in reuse.items()
              if not key.startswith("changed_schema_")}
    legacy_text = "\n".join(trace_run._local_input_reuse_markdown({
        **result, "local_prefix_reuse": legacy,
    }))
    assert "Run-level local input reuse" in legacy_text
    assert "Unchanged definitions within changed tool lists" not in legacy_text


def test_duplicate_call_ids_do_not_receive_a_misleading_request_number():
    first = _candidate(1, "Synthetic repeated input")
    duplicate = _candidate(2, "Synthetic repeated input")
    duplicate["call_id"] = first["call_id"]
    third = _candidate(3, "Synthetic later input")
    reuse = analyze_local_prefix_reuse([first, duplicate, third])
    result = {"local_prefix_reuse": reuse, "loops": [
        {"call_id": item["call_id"], "dispatch_recorded": True, "status": "captured"}
        for item in [first, duplicate, third]
    ]}
    text = "\n".join(trace_run._local_input_reuse_markdown(result))
    assert text.count("| — | — | unavailable: duplicate call id |") == 2
    assert "| 3 | — | unavailable: prior request unavailable |" in text
    assert "Ambiguous duplicate call IDs have no request number" in text
    assert "| 2 |" not in text
