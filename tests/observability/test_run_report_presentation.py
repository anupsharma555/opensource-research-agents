"""Readable reports preserve unknown cache usage and escape local labels."""

from scripts.report_api_runs import cache_text, render_html, render_markdown


def _row():
    return {"full_report": "../case/RESULT.md", "metrics": {
        "report_id": "a" * 16, "kind": "api_diagnostic", "task_label": "<script>example</script>",
        "outcome_status": "incomplete_pair", "started_at_utc": "2026-01-01T00:00:00+00:00",
        "profile_label": "synthetic", "model_label": "synthetic/model", "elapsed_seconds": 4,
        "timing": {"observed_window_seconds": 4, "measured_http_seconds_sum": 2,
                   "measured_http_attempts": 2},
        "counts": {"model_http_attempts": 2, "model_captured_responses": 1,
                   "model_usage_unknown_attempts": 1, "retrieval_http_attempts": 0},
        "usage": {"input": {"reported_tokens": 100}, "output": {"reported_tokens": 20},
                  "reasoning": {"reported_tokens": 10},
                  "cache_read": {"not_reported_attempts": 1},
                  "cache_fraction_reported_subset": {"value": None, "covered_calls": 0,
                                                      "total_calls": 2, "coverage_status": "unavailable"}},
        "attempts": [{"index": 1, "phase": "initial", "http_status": 403,
                      "http_duration_seconds": 1,
                      "input_tokens": None, "output_tokens": None, "reasoning_tokens": None,
                      "cache_observation": "unavailable"},
                     {"index": 2, "phase": "continuation", "http_status": 200,
                      "http_duration_seconds": 1,
                      "input_tokens": 100, "output_tokens": 20, "reasoning_tokens": 10,
                      "cache_observation": "not_reported"}],
    }}


def test_failed_attempt_unknown_and_provider_omission_are_visible():
    row = _row()
    output = render_html([row])
    assert "Not reported by Groq on 1 successful response" in output
    assert "0/2 attempts covered" in output
    assert "120" in output and "Unknown" in output
    assert "Incomplete pair" in output and "403" in output
    assert "&lt;script&gt;example&lt;/script&gt;" in output
    assert "<script>" not in output
    assert "1 attempt(s) with unknown usage" in output
    markdown = render_markdown([row])
    assert "<script>" not in markdown
    assert "120 known tokens" in markdown
    assert "| 1 | initial | synthetic/model | 403 | 1 | Unknown | Unknown | Unknown | unavailable |" in markdown
    assert "Includes the pause before continuation" in output
    assert "Sum of measured HTTP durations: 2 seconds" in output
    assert "first two cases" not in output
    assert "Natural-agent tasks and direct API diagnostics are labeled separately" in output


def test_natural_timing_distinguishes_controller_from_shutdown():
    row = _row()
    report = row["metrics"]
    report.update(kind="natural_agent", outcome_status="completed", mission_status="completed")
    report["timing"].update(observed_window_seconds=126.288,
                            runner_total_elapsed_seconds=127.195,
                            post_controller_finalization_seconds=0.907)
    rendered = render_html([row])
    assert "Controller elapsed: 126.288 seconds" in rendered
    assert "Runner through shutdown: 127.195 seconds" in rendered
    assert "post-controller finalization: 0.907 seconds" in rendered


def test_per_attempt_model_is_visible_without_guessing_legacy_natural_rows():
    row = _row()
    report = row["metrics"]
    report.update(kind="natural_agent", model_label="mixed", mission_status="active")
    report["attempts"][0]["model_id"] = "qwen/qwen3.8-27b"
    report["attempts"][1]["model_id"] = "openai/gpt-oss-120b"
    markdown = render_markdown([row])
    html = render_html([row])
    assert "| 1 | initial | qwen/qwen3.8-27b | 403 |" in markdown
    assert "| 2 | continuation | openai/gpt-oss-120b | 200 |" in markdown
    assert "<th>Model</th>" in html
    assert "<td>qwen/qwen3.8-27b</td>" in html
    assert "<td>openai/gpt-oss-120b</td>" in html
    del report["attempts"][0]["model_id"]
    assert "| 1 | initial | Unknown | 403 |" in render_markdown([row])
    report["attempts"][0]["model_id"] = "<script>bad</script>"
    assert "<script>bad</script>" not in render_html([row])
    assert "&lt;script&gt;bad&lt;/script&gt;" in render_html([row])


def test_reported_zero_and_partial_cache_share_remain_distinct():
    report = _row()["metrics"]
    report["usage"]["cache_fraction_reported_subset"].update(
        value=0, covered_calls=1, coverage_status="partial")
    assert cache_text(report) == "0.0% of input on reporting calls; 1/2 attempts covered (partial)"
    report["usage"]["cache_fraction_reported_subset"]["value"] = 0.6
    assert cache_text(report).startswith("60.0% of input on reporting calls")
    assert "1/2 attempts covered (partial)" in cache_text(report)
