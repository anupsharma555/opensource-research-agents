"""Bounded live Groq protocol qualification; exact records remain under ignored .local.

Run only after the focused offline tests pass. This calls one selected model in
serial, at most four times: two dependent tool choices, a collection answer,
and separate strict JSON output. It does not evaluate scientific competence.
"""

from __future__ import annotations

import hashlib
import json
import os
import sys
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from ora.live.provider import DEFAULT_REASONING_EFFORT  # noqa: E402
from ora.models import CallConfiguration, GroqSelectedAdapter, SourceUse  # noqa: E402

PRIVATE_ROOT = ROOT / ".local" / "stage3" / "model"
MAX_CALLS = 4
MAX_OUTPUT_TOKENS_EACH = 8192
INSTRUCTIONS = (
    "This is a synthetic protocol check. First call read_marker with key 'alpha'. "
    "After that result, call the tool named by its next_tool field with the supplied "
    "left value and right='beta'. Only after both tool results, state the combined "
    "marker in one short sentence. Use no outside facts."
)
TOOLS = (
    {"type": "function", "function": {"name": "read_marker", "description": "Read a synthetic marker",
     "parameters": {"type": "object", "properties": {"key": {"type": "string"}},
                    "required": ["key"], "additionalProperties": False}}},
    {"type": "function", "function": {"name": "combine_marker", "description": "Combine two synthetic markers",
     "parameters": {"type": "object", "properties": {"left": {"type": "string"},
                                               "right": {"type": "string"}},
                    "required": ["left", "right"], "additionalProperties": False}}},
)
SCHEMA = {"type": "object", "properties": {"combined_marker": {"type": "string"},
                                           "tool_calls_used": {"type": "integer"}},
          "required": ["combined_marker", "tool_calls_used"], "additionalProperties": False}


def save(path: Path, data: bytes) -> None:
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "wb") as output:
        output.write(data)


def main() -> int:
    PRIVATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_dir = PRIVATE_ROOT / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" +
                              uuid.uuid4().hex[:8])
    run_dir.mkdir(mode=0o700)
    cfg_hash = hashlib.sha256(json.dumps({"instructions": INSTRUCTIONS,
        "model": "openai/gpt-oss-120b", "reasoning_effort": DEFAULT_REASONING_EFFORT,
        "include_reasoning": True, "max_completion_tokens": MAX_OUTPUT_TOKENS_EACH,
    }, sort_keys=True).encode()).hexdigest()
    config = CallConfiguration("stage3-model-protocol-v1", cfg_hash, INSTRUCTIONS,
                               max_completion_tokens=MAX_OUTPUT_TOKENS_EACH)
    source_text = "Synthetic marker task; no research source or private information."
    source = SourceUse("synthetic-marker-v1", 1,
                       hashlib.sha256(source_text.encode()).hexdigest(),
                       "permitted", True, text=source_text)
    index = 0

    class RecordingResponse:
        def __init__(self, inner, path: Path):
            self.inner = inner
            self.path = path
            self.headers = inner.headers

        def __enter__(self):
            self.inner.__enter__()
            return self

        def __exit__(self, *args):
            return self.inner.__exit__(*args)

        def read(self, limit):
            raw = self.inner.read(limit)
            save(self.path, raw)
            return raw

    def transport(request, *, timeout):
        nonlocal index
        index += 1
        if index > MAX_CALLS:
            raise RuntimeError("live model call ceiling reached")
        save(run_dir / f"request-{index:02d}.json", request.data)
        response = urllib.request.urlopen(request, timeout=timeout)
        return RecordingResponse(response, run_dir / f"response-{index:02d}.json")

    adapter = GroqSelectedAdapter(ROOT, transport=transport)
    history = [{"role": "user", "content": "Execute the synthetic two-tool marker sequence."}]
    summary = {"schema": "ora.stage3.model_qualification.v1",
               "started_at": datetime.now(timezone.utc).isoformat(),
               "model": config.model_id, "endpoint": config.endpoint,
               "reasoning_effort": config.reasoning_effort,
               "include_reasoning": config.include_reasoning,
               "maximum_calls": MAX_CALLS, "maximum_output_tokens_per_call": MAX_OUTPUT_TOKENS_EACH,
               "calls": [], "result": "incomplete"}

    def call(phase: str, number: int):
        prepared = adapter.prepare(
            config, call_id=f"stage3-{number}", attempt_id=f"stage3-attempt-{number}",
            parent_call_id=f"stage3-{number-1}" if number > 1 else None,
            task_category="protocol_qualification", phase=phase, messages=history,
            sources=(source,), tools=TOOLS if phase == "collect" else (),
            schema_name="marker_result" if phase == "final" else None,
            schema=SCHEMA if phase == "final" else None,
        )
        save(run_dir / f"manifest-{number:02d}.json",
             json.dumps(prepared.manifest(), indent=2).encode())
        result = adapter.invoke(prepared)
        summary["calls"].append({
            "call_id": result.call_id, "phase": phase, "status": result.status,
            "finish_reason": result.finish_reason, "reasoning_status": result.reasoning_status,
            "response_id": result.response_id, "provider_request_id": result.provider_request_id,
            "returned_model": result.returned_model, "request_sha256": result.request_sha256,
            "response_sha256": result.response_sha256, "usage": result.usage,
            "rate_headers": result.rate_headers, "timings": result.timings,
            "error_kind": result.error_kind, "http_status": result.http_status,
            "retry_after_seconds": result.retry_after_seconds,
            "tool_name": result.tool_calls[0]["function"]["name"] if result.tool_calls else None,
        })
        save(run_dir / f"outcome-{number:02d}.json", json.dumps({
            "manifest": prepared.manifest(), "status": result.status,
            "reasoning_status": result.reasoning_status, "reasoning": result.reasoning,
            "content": result.content, "tool_calls": result.tool_calls,
            "usage": result.usage, "rate_headers": result.rate_headers,
            "timings": result.timings, "error_kind": result.error_kind,
        }, ensure_ascii=False, indent=2).encode())
        return result

    try:
        for number, expected_name in ((1, "read_marker"), (2, "combine_marker")):
            result = call("collect", number)
            if result.status != "tool_requested" or len(result.executable_tool_calls) != 1:
                summary["result"] = f"stopped_at_{number}_{result.status}"
                break
            tool = result.executable_tool_calls[0]
            if tool["function"]["name"] != expected_name:
                summary["result"] = f"unexpected_tool_at_{number}"
                break
            arguments = json.loads(tool["function"]["arguments"])
            if number == 1:
                if arguments != {"key": "alpha"}:
                    summary["result"] = "unexpected_first_arguments"
                    break
                tool_content = json.dumps({"next_tool": "combine_marker", "left": "alpha"})
            else:
                if arguments != {"left": "alpha", "right": "beta"}:
                    summary["result"] = "unexpected_second_arguments"
                    break
                tool_content = json.dumps({"combined_marker": "alpha-beta"})
            history.extend([result.assistant_tool_message(),
                            result.tool_result_message(content=tool_content)])
        else:
            collected = call("collect", 3)
            if collected.status == "complete":
                history.append({"role": "assistant", "content": collected.content})
                history.append({"role": "user", "content":
                                "Return the marker and number of completed tool calls in the schema."})
                final = call("final", 4)
                summary["result"] = "complete" if final.status == "complete" else (
                    f"final_{final.status}")
            else:
                summary["result"] = f"collection_{collected.status}"
    except Exception as exc:
        summary["result"] = "exception"
        summary["exception_type"] = type(exc).__name__
    finally:
        save(run_dir / "summary.json", json.dumps(summary, indent=2).encode())
        print(json.dumps({"private_run_dir": str(run_dir), "result": summary["result"],
                          "calls": len(summary["calls"]),
                          "statuses": [item["status"] for item in summary["calls"]]},
                         separators=(",", ":")))
    return 0 if summary["result"] == "complete" else 1


def stream_smoke() -> int:
    """One collection stream; hold tool arguments until its terminal event."""
    PRIVATE_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    run_dir = PRIVATE_ROOT / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") +
                              "-stream-" + uuid.uuid4().hex[:8])
    run_dir.mkdir(mode=0o700)
    instructions = "Synthetic protocol check. Call lookup with key 'alpha' and no other tool."
    config = CallConfiguration(
        "stage3-stream-protocol-v1",
        hashlib.sha256(instructions.encode()).hexdigest(), instructions,
        max_completion_tokens=MAX_OUTPUT_TOKENS_EACH, stream=True,
    )
    tool = {"type": "function", "function": {"name": "lookup",
            "description": "Read a synthetic marker", "parameters": {"type": "object",
            "properties": {"key": {"type": "string"}}, "required": ["key"],
            "additionalProperties": False}}}

    class RecordedStream:
        def __init__(self, inner):
            self.inner = inner
            self.headers = inner.headers

        def __enter__(self):
            self.inner.__enter__()
            return self

        def __exit__(self, *args):
            return self.inner.__exit__(*args)

        def read(self, limit):
            raw = self.inner.read(limit)
            save(run_dir / "response-01.sse", raw)
            return raw

    def transport(request, *, timeout):
        save(run_dir / "request-01.json", request.data)
        return RecordedStream(urllib.request.urlopen(request, timeout=timeout))

    adapter = GroqSelectedAdapter(ROOT, transport=transport)
    prepared = adapter.prepare(
        config, call_id="stage3-stream-1", attempt_id="stage3-stream-attempt-1",
        task_category="protocol_qualification", phase="collect",
        messages=[{"role": "user", "content": "Read marker alpha."}], tools=(tool,),
    )
    save(run_dir / "manifest-01.json", json.dumps(prepared.manifest(), indent=2).encode())
    result = adapter.invoke(prepared)
    valid = (result.status == "tool_requested" and len(result.executable_tool_calls) == 1
             and result.executable_tool_calls[0]["function"]["name"] == "lookup"
             and json.loads(result.executable_tool_calls[0]["function"]["arguments"])
             == {"key": "alpha"})
    summary = {"schema": "ora.stage3.model_stream_qualification.v1",
               "model": config.model_id, "request_sha256": result.request_sha256,
               "response_sha256": result.response_sha256, "status": result.status,
               "finish_reason": result.finish_reason,
               "reasoning_status": result.reasoning_status,
               "response_id": result.response_id,
               "provider_request_id": result.provider_request_id,
               "usage": result.usage, "rate_headers": result.rate_headers,
               "timings": result.timings, "error_kind": result.error_kind,
               "valid_complete_tool_request": valid}
    save(run_dir / "summary.json", json.dumps(summary, indent=2).encode())
    save(run_dir / "restricted-outcome.json", json.dumps({
        "reasoning": result.reasoning, "content": result.content,
        "tool_calls": result.tool_calls,
        "partial_response": result.partial_response.decode("utf-8", errors="replace")
        if result.partial_response is not None else None,
    }, ensure_ascii=False, indent=2).encode())
    print(json.dumps({"private_run_dir": str(run_dir), "status": result.status,
                      "valid_complete_tool_request": valid}, separators=(",", ":")))
    return 0 if valid else 1


if __name__ == "__main__":
    if len(sys.argv) == 2 and sys.argv[1] == "--stream-smoke":
        raise SystemExit(stream_smoke())
    if len(sys.argv) != 1:
        raise SystemExit("usage: qualify_stage3_model.py [--stream-smoke]")
    raise SystemExit(main())
