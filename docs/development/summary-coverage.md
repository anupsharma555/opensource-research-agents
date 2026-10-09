# Summary coverage and trace relationships

`TraceJournal` can save a derived explanation with a declared event-read boundary.
This boundary says which saved events were available to the summarizer; it does
not say that the summary is scientifically correct or that a model's returned
reasoning caused its answer. The original `summarize` call remains valid without
coverage. Such historical summaries read as `coverage_status: unknown` and
`currentness: unknown_without_boundary`.

## Saving a bounded summary

Read the relevant saved records and note the `nextCursor` from
`TraceJournal.export(mission_id)`. Pass that cursor explicitly to
`summarize(..., coverage=...)`:

```python
coverage = {
    "scope": {"run_id": "run-1", "instance_ids": ["agent-1"]},
    "through_event_sequence": read_page["nextCursor"],
    "event_refs": [observed_trace_ref],
    "generation_state": "complete",
    "known_gaps": [
        {"kind": "omitted", "reason_code": "not_selected", "reference": source_ref},
        {"kind": "delayed", "reason_code": "pending"},
    ],
}
summary_ref = journal.summarize(
    "summary-operation", mission_id="mission-1", level="agent",
    summary="The saved evidence changed the draft interpretation.",
    source_refs=[observed_trace_ref, source_ref], coverage=coverage,
)
```

The example identifiers stand for previously saved local records. `scope` must
name a run; optional `instance_ids` and `task_ids` narrow it. An agent-level
summary names exactly one registered instance in that run. The named run must
have a registered actor in the mission. A summary may declare
an exact `through_event_sequence`, an exact `event_refs` set, or both. A set
without a cursor is useful for saying which events were considered, but it
cannot say whether later events exist. The caller supplies the boundary it
actually read. The host verifies that the cursor belongs to the mission and
that cited trace events belong to the declared run and lie at or before it.
The host never substitutes the latest event position. If a peer reply commits
between reading and saving, the summary remains at the earlier boundary.
`source_refs` lists the exact records cited by this summary; the event cursor
does not track every nontrace record write. For example, a `source_copy`
saved after a declared cursor of zero can still be cited while no later
mission event exists.
The reader therefore reports `source_completeness: not_established`, even when
event currentness is `as_of_boundary`. `coverage_basis` says the read boundary
was declared by the caller, not measured by the host.

`generation_state` describes the summary artifact (`complete` or `partial`),
not completeness of the research mission. `known_gaps` keeps `omitted`,
`delayed` and `unavailable` distinct. `reason_code` is an extensible, short
lowercase identifier such as `study_passage_not_yet_supplied`; use letters,
digits and single underscores, beginning with a letter (maximum 96 characters).
It is not a place for private detail or free text. A gap may reference
an exact accessible record, but cannot point to restricted provider reasoning
or another mission. A missing record can be noted without inventing a pointer;
the reader detects unavailable saved references separately.

The coverage object is saved with the versioned `trace_summary` record. Source
and covered-event references get `derived_from` links. Reusing an operation ID
with the same request returns the original summary even if newer events have
arrived; changing its declared boundary under that ID conflicts. A new summary
uses a new operation ID, preserving the old as-of explanation. Saving a summary
does not invoke a model, enqueue a refresh or hold an independent research
branch.

## Reading currentness without rewriting history

`journal.summary_coverage(summary_ref, mission_id=...)` reads one SQLite
snapshot and returns the saved scope, cursor, event set, generation state and
known gaps. This currentness describes the event stream, not completeness of
the scientific source set. Its `currentness` is one of:

| Value | Meaning |
| --- | --- |
| `unknown_without_boundary` | Historical summary or explicit event set without a cursor; no claim of current coverage. |
| `as_of_boundary` | No later in-scope trace or unclassified mission event was observed in this snapshot. |
| `known_gaps` | No newer events were found, but the summary declared omissions, delays or unavailable detail. |
| `newer_scoped_events` | Later saved trace events match the run and optional agent/task scope. |
| `unclassified_newer_events` | Later mission events have no trace scope in this projection; their relevance remains unknown. |
| `missing_records` | An exact source/covered event or a newer trace's payload is unavailable. |

The read result provides `newer_scoped_event_count`,
`newer_unclassified_event_count`, `unresolved_newer_trace_count`,
`missing_source_refs`, `missing_event_refs` and `observed_event_sequence`
separately. It does not promote a cursor merely because the store advanced.
A saved peer reply or source correction without an associated trace is labeled
unclassified rather than silently called relevant or irrelevant. A later
qualified reader can reconcile it with the exact research records.

## Typed additional relationships

`append` and `append_tx` optionally accept `relationships` entries with a
`relation` and exact `{kind, id, version}` `reference`. The supported labels are
`input_consumed`, `reply_to`, `revision_of` and `retry_of`. These records add
specific SQL links and appear in the trace body. They do not replace its one
execution parent, `input_refs` or `output_refs`. Targets must resolve in the
same mission or be shared records with no mission assignment; restricted
provider reasoning is rejected. Duplicate event keys with identical inputs
remain idempotent, while changed facts under a reused key conflict. Replaying a
legacy trace without the new optional relationships keeps its original record
shape and emits no new event.

A relationship documents a declared observed association. It does not prove
that a cited passage supports a claim, that a peer's suggestion was adopted,
or that a retry had the same external effect. Source rights and critic access
remain controlled by their owning records and callers. The shared API/viewer
can project these safe metadata fields; scientific acceptance and full mission
continuation belong to their separate owners.

The focused offline check is
`.venv/bin/python -m pytest tests/coordination/test_summary_coverage.py`.
