"""Build a local, readable comparison from explicitly selected saved API cases.

The manifest selects evidence directories; this command never loads credentials,
starts a run, or exports traces. Full task/answer reports remain linked locally.
"""

from __future__ import annotations

import argparse
import html
import json
import os
from datetime import datetime
from pathlib import Path
from urllib.parse import quote, urlsplit
from zoneinfo import ZoneInfo

from ora.observability.run_reports import load_run_report
from ora.state import StateError

_OUTCOMES = {
    "completed": "Completed agent task",
    "token_cap": "Stopped at planned-token allowance",
    "retrieval_attempt_cap": "Stopped at retrieval limit; no final answer",
    "single_response_observed": "One API response captured; tools not executed",
    "incomplete_pair": "Incomplete pair: one failed attempt, one successful response",
}


def number(value: int | float | None) -> str:
    return f"{value:,}" if value is not None else "Unknown"


def cache_text(report: dict) -> str:
    usage = report["usage"]
    cache = usage["cache_fraction_reported_subset"]
    covered, total = cache["covered_calls"], cache["total_calls"]
    absent = usage["cache_read"]["not_reported_attempts"]
    if cache["value"] is not None:
        return (f"{cache['value'] * 100:.1f}% of input on reporting calls; "
                f"{covered}/{total} attempts covered ({cache['coverage_status']})")
    if absent:
        response = "response" if absent == 1 else "responses"
        return (f"Not reported by Groq on {absent} successful {response}; "
                f"{covered}/{total} attempts covered. Cache percentage unknown.")
    return f"Cache percentage unknown; {covered}/{total} attempts covered."


def known_total(report: dict) -> int | None:
    values = [report["usage"][key]["reported_tokens"] for key in ("input", "output")]
    return sum(values) if all(value is not None for value in values) else None


def local_reuse_text(report: dict) -> str:
    reuse = report.get("local_prefix_reuse") or {}
    structural = reuse.get("local_structural_input_reuse_fraction")
    coverage = reuse.get("structural_coverage_status")
    if structural is not None:
        return (f"Local input reuse: {structural * 100:.1f}% "
                f"({number(reuse['repeated_local_structural_input_tokens'])}/"
                f"{number(reuse['local_structural_input_tokens'])} locally measured tokens; "
                f"{coverage} coverage, {reuse['structural_measured_calls']}/"
                f"{reuse['structural_candidate_calls']} request records). "
                "Unchanged opening messages plus identical whole tool definitions; "
                "first requests included at zero local reuse. This is not a provider cache hit.")
    if coverage == "baseline_only":
        return ("Local input reuse unavailable: only first requests in separate comparison "
                "scopes; no comparable follow-up. This is not a 0% provider cache rate.")
    fraction = reuse.get("estimated_message_prefix_token_fraction")
    if reuse.get("status") == "measured" and fraction is not None:
        return (f"Prompt overlap (local estimate): {fraction * 100:.1f}% of compared follow-up "
                f"message tokens repeat at the start "
                f"({number(reuse['estimated_repeated_message_tokens'])}/"
                f"{number(reuse['estimated_current_message_tokens'])}; "
                f"{reuse['measured_pairs']}/{reuse['eligible_pairs']} pairs measured). "
                f"Tool definitions unchanged in {reuse['stable_tool_schema_pairs']}/"
                f"{reuse['eligible_pairs']} pairs. This is request overlap, not a provider cache hit.")
    reason = {"no_eligible_pair": "no comparable follow-up request",
              "tokenizer_unavailable": "local tokenizer unavailable"}.get(
                  reuse.get("status"), "verified saved request content unavailable")
    return f"Prompt overlap unavailable: {reason}. This is separate from provider caching."


def _stamp(report: dict) -> str:
    return datetime.fromisoformat(report["started_at_utc"]).astimezone(
        ZoneInfo("America/New_York")
    ).strftime("%b %d, %Y · %I:%M:%S %p %Z")


def timing_text(report: dict) -> str:
    timing = report["timing"]
    label = "Controller elapsed" if report["kind"] == "natural_agent" else "Observation window"
    suffix = (" Includes the pause before continuation."
              if report["outcome_status"] == "incomplete_pair" else "")
    shutdown = ""
    if report["kind"] == "natural_agent" and timing.get("runner_total_elapsed_seconds") is not None:
        shutdown = (f" Runner through shutdown: {number(timing['runner_total_elapsed_seconds'])} "
                    f"seconds; post-controller finalization: "
                    f"{number(timing['post_controller_finalization_seconds'])} seconds.")
    return (f"{label}: {number(timing['observed_window_seconds'])} seconds. "
            f"Sum of measured HTTP durations: {number(timing['measured_http_seconds_sum'])} seconds "
            f"across {timing['measured_http_attempts']} attempts.{shutdown}{suffix} "
            "Whole-run time includes waits and local work; it is not model generation latency.")


def _link(path: Path, destination: Path) -> str:
    return quote(os.path.relpath(path, destination), safe="/.")


def load_manifest(path: Path, destination: Path) -> list[dict]:
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or value.get("schema") != "ora.run_report_manifest.v1"
            or not isinstance(value.get("cases"), list) or not 1 <= len(value["cases"]) <= 20):
        raise StateError("Expected a manifest with 1 to 20 selected cases")
    rows = []
    for selected in value["cases"]:
        case_dir = (path.parent / selected["case_dir"]).resolve()
        continuation = selected.get("continuation_dir")
        report = load_run_report(
            case_dir, kind=selected["kind"],
            continuation_dir=(path.parent / continuation).resolve() if continuation else None,
        )
        full_report = case_dir / "RESULT.md"
        if not full_report.is_file():
            raise StateError("Selected case has no full local RESULT.md report")
        row = {"metrics": report, "full_report": _link(full_report, destination)}
        cloud = selected.get("cloud_url")
        if cloud is not None:
            parsed = urlsplit(cloud)
            if (parsed.scheme != "https" or parsed.netloc != "smith.langchain.com"
                    or parsed.username or parsed.password):
                raise StateError("Expected an explicit LangSmith trace link")
            row["cloud_url"] = cloud
        rows.append(row)
    if len({row["metrics"]["report_id"] for row in rows}) != len(rows):
        raise StateError("The same case was selected twice")
    return rows


def render_markdown(rows: list[dict]) -> str:
    reports = [row["metrics"] for row in rows]
    attempts = sum(item["counts"]["model_http_attempts"] for item in reports)
    unknown = sum(item["counts"]["model_usage_unknown_attempts"] for item in reports)
    known = sum(known_total(item) or 0 for item in reports)
    lines = ["# API run comparison", "",
             f"**{len(rows)} saved runs · {attempts} model HTTP attempts · "
             f"{known:,} known tokens · {unknown} attempts with unknown usage.**", "",
             "These reports reconstruct saved evidence without new model or retrieval calls. "
             "Reasoning is already included in output tokens. Missing cache data is unknown, "
             "not a measured zero. Direct API diagnostics do not establish agent task completion.", "",
             "| Run | Type and outcome | Input | Output | Reasoning subset | Cache reporting |",
             "| --- | --- | ---: | ---: | ---: | --- |"]
    for index, row in enumerate(rows, 1):
        report = row["metrics"]
        usage = report["usage"]
        kind = "Natural agent" if report["kind"] == "natural_agent" else "API diagnostic"
        outcome = _OUTCOMES.get(report["outcome_status"], report["outcome_status"].replace("_", " "))
        label = html.escape(report['task_label']).replace('|', '/')
        for char in "[]*`":
            label = label.replace(char, "\\" + char)
        lines.append(f"| [{index}. {label}](#run-{index}) "
                     f"| {kind}: {outcome} | {number(usage['input']['reported_tokens'])} "
                     f"| {number(usage['output']['reported_tokens'])} "
                     f"| {number(usage['reasoning']['reported_tokens'])} | {cache_text(report)} |")
    for index, row in enumerate(rows, 1):
        report, counts = row["metrics"], row["metrics"]["counts"]
        lines.extend(["", f'<a id="run-{index}"></a>', f"## Run {index}: {html.escape(report['task_label'])}", "",
                      f"**{_OUTCOMES.get(report['outcome_status'], report['outcome_status'])}.**", "",
                      f"**Cache: {cache_text(report)}**", "",
                      local_reuse_text(report), "",
                      f"{counts['model_http_attempts']} model HTTP attempts; "
                      f"{counts['model_captured_responses']} captured responses; "
                      f"{counts['retrieval_http_attempts']} retrieval HTTP attempts. "
                      f"Known usage: {number(known_total(report))} tokens. "
                      f"Unknown-usage attempts: {counts['model_usage_unknown_attempts']}.", "",
                      timing_text(report), "",
                      "| Attempt | Phase | HTTP | HTTP seconds | Input | Output | Reasoning subset | Cache |",
                      "| ---: | --- | ---: | ---: | ---: | ---: | ---: | --- |"])
        for attempt in report["attempts"]:
            lines.append(f"| {attempt['index']} | {attempt['phase']} | {attempt['http_status']} "
                         f"| {number(attempt['http_duration_seconds'])} "
                         f"| {number(attempt['input_tokens'])} | {number(attempt['output_tokens'])} "
                         f"| {number(attempt['reasoning_tokens'])} "
                         f"| {attempt['cache_observation'].replace('_', ' ')} |")
        lines.extend(["", f"[Full input-to-output report]({row['full_report']})" +
                      (f" · [Visible LangSmith metrics summary]({row['cloud_url']})" if row.get("cloud_url") else ""),
                      "", f"Metadata: {_stamp(report)}; {report['profile_label'].title()} Groq API key; "
                      f"model `{report['model_label']}`.", ""])
        if report["kind"] == "natural_agent":
            lines.append(f"Saved mission status: `{report['mission_status']}`. "
                         "The evaluation host has stopped; saved active state does not imply a running host.")
        else:
            lines.append("Direct transport diagnostic: no research mission, no returned tool execution, "
                         "and no retained prose answer. A failed call with unknown usage is not a cache miss.")
    return "\n".join(lines) + "\n"


def render_html(rows: list[dict]) -> str:
    e = html.escape
    cards = []
    for index, row in enumerate(rows, 1):
        r, u, c = row["metrics"], row["metrics"]["usage"], row["metrics"]["counts"]
        kind = "Natural agent task" if r["kind"] == "natural_agent" else "Direct API diagnostic"
        attempts = "".join(
            "<tr>" + "".join(f"<td>{e(str(cell))}</td>" for cell in (
                a["index"], a["http_status"], number(a['http_duration_seconds']), number(a["input_tokens"]),
                number(a["output_tokens"]), number(a["reasoning_tokens"]),
                a["cache_observation"].replace("_", " "))) + "</tr>"
            for a in r["attempts"])
        cloud = (f'<a href="{e(row["cloud_url"], quote=True)}">LangSmith metrics summary ↗</a>'
                 if row.get("cloud_url") else "")
        cards.append(f'''<article id="run-{index}">
<div class="eyebrow">RUN {index} · {e(kind)}</div><h2>{e(r['task_label'])}</h2>
<p class="outcome">{e(_OUTCOMES.get(r['outcome_status'], r['outcome_status']))}</p>
<div class="cache"><strong>Provider cache</strong><p>{e(cache_text(r))}</p></div>
<p>{e(local_reuse_text(r))}</p>
<div class="numbers"><div><b>{number(known_total(r))}</b>known total tokens</div>
<div><b>{number(u['input']['reported_tokens'])}</b>input</div>
<div><b>{number(u['output']['reported_tokens'])}</b>output</div>
<div><b>{number(u['reasoning']['reported_tokens'])}</b>reasoning within output</div></div>
<p>{c['model_http_attempts']} model attempts · {c['model_captured_responses']} captured responses ·
{c['retrieval_http_attempts']} retrieval attempts · {c['model_usage_unknown_attempts']} attempts with unknown usage</p>
<p class="timing">{e(timing_text(r))}</p>
<details><summary>Per-attempt evidence</summary><div class="table-wrap"><table><thead><tr>
<th>Attempt</th><th>HTTP</th><th>Seconds</th><th>Input</th><th>Output</th><th>Reasoning</th><th>Cache observation</th>
</tr></thead><tbody>{attempts}</tbody></table></div></details>
<p class="links"><a href="{e(row['full_report'], quote=True)}">Full input-to-output report ↗</a>{cloud}</p>
<p class="meta">{e(_stamp(r))} · {e(r['profile_label'].title())} Groq API key · {e(r['model_label'])}</p></article>''')
    known = sum(known_total(row["metrics"]) or 0 for row in rows)
    attempts = sum(row["metrics"]["counts"]["model_http_attempts"] for row in rows)
    unknown = sum(row["metrics"]["counts"]["model_usage_unknown_attempts"] for row in rows)
    return '''<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>OSRA · API run metrics review</title><style>
:root{color-scheme:light}*{box-sizing:border-box}body{margin:0;background:#f2f4f5;color:#18302c;font:16px/1.55 system-ui,sans-serif}
main{max-width:1150px;margin:42px auto;padding:0 24px}h1{font-size:36px;line-height:1.15;margin:10px 0}h2{font-size:21px;line-height:1.3;margin:8px 0 16px}
.eyebrow{font-size:12px;font-weight:750;letter-spacing:.1em;color:#4e6f67}.intro{max-width:850px;color:#415954}.totals{display:flex;gap:24px;flex-wrap:wrap;padding:22px 0 28px;font-weight:650}
.grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:22px}article{background:white;border:1px solid #d7e2de;border-radius:14px;padding:26px}
.outcome{min-height:50px;font-weight:650}.cache{background:#fff5d9;border-left:4px solid #bb8931;padding:13px 16px;min-height:120px}.cache p{margin:5px 0 0}
.numbers{display:grid;grid-template-columns:repeat(2,1fr);gap:20px;margin:22px 0;font-size:13px;color:#5b6a66}.numbers b{display:block;font-size:25px;color:#173c32}
details{border-top:1px solid #dce4df;padding-top:14px}summary{cursor:pointer;font-weight:600}.table-wrap{overflow-x:auto}table{width:100%;border-collapse:collapse;font-size:12px;margin-top:10px}td,th{text-align:left;padding:7px;border-bottom:1px solid #e2e8e5}
a{color:#166454;text-underline-offset:3px}.links{display:flex;gap:12px;flex-wrap:wrap;font-size:14px}.meta{font-size:12px;color:#66746f;border-top:1px solid #e5e9e7;padding-top:12px}footer{margin:25px 0;color:#5e6d67;font-size:14px}
@media(max-width:740px){.grid{grid-template-columns:1fr}main{margin-top:25px;padding:0 15px}h1{font-size:29px}}
</style><main><div class="eyebrow">OSRA · LOCAL EVIDENCE REVIEW</div><h1>API runs, clearly accounted for</h1>
<p class="intro">The cache measurement is visible for every run. Missing provider counts remain unknown.
Reasoning tokens are already part of output. Natural-agent tasks and direct API diagnostics are labeled separately.</p>''' + (
        f'<div class="totals"><span>{len(rows)} saved cases</span><span>{attempts} model HTTP attempts</span>'
        f'<span>{known:,} known tokens</span><span>{unknown} attempt(s) with unknown usage</span></div>'
        '<div class="grid">' + "".join(cards) + '</div><footer>Read-only reconstruction from saved evidence. '
        'No new model or retrieval calls. Full questions, answers and source material stay in local reports; '
        'LangSmith receives metrics, status and short labels only. '
        '<a href="INDEX.md">Markdown comparison</a> · <a href="metrics.json">Machine-readable metrics</a></footer></main></html>')


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    args = parser.parse_args(argv)
    try:
        rows = load_manifest(args.manifest.resolve(), args.output_dir.resolve())
        args.output_dir.mkdir(parents=True, exist_ok=True)
        files = {"INDEX.md": render_markdown(rows), "index.html": render_html(rows),
                 "metrics.json": json.dumps([row["metrics"] for row in rows], indent=2) + "\n"}
        for name, content in files.items():
            target = args.output_dir / name
            with target.open("w", encoding="utf-8") as stream:
                stream.write(content)
            target.chmod(0o600)
    except (OSError, ValueError, KeyError, TypeError) as exc:
        parser.exit(2, f"report_api_runs: {type(exc).__name__}: selected evidence could not be rendered\n")
    print(f"Rendered {len(rows)} saved cases; no API calls. Open {args.output_dir / 'index.html'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
