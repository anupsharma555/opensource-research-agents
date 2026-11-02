"""The FTS experiment must filter index mistakes before paging or relevance output."""

from __future__ import annotations

import importlib.util
import json
import sqlite3
import sys
from dataclasses import replace
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SPEC = importlib.util.spec_from_file_location(
    "evaluate_evidence_fts", ROOT / "scripts/evaluate_evidence_fts.py"
)
evaluation = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
sys.modules[SPEC.name] = evaluation
SPEC.loader.exec_module(evaluation)


def entry(identifier: str, *, version=1, head=1, permission="permitted",
          source=None, title="Marker term", note="A synthetic note", curated="2026-01-01T00:00:00Z"):
    return evaluation.Entry(identifier, version, head, source or identifier + ":v1",
                            title, note, curated, permission)


def query(text="marker term"):
    return evaluation.Query("q", text, (), ())


def test_raw_stale_revoked_foreign_and_corrupt_index_rows_never_escape_guard():
    rows = [entry("allowed"), entry("obsolete", version=1, head=2),
            entry("obsolete", version=2, head=2, title="Current unrelated text"),
            entry("revoked", permission="revoked"),
            entry("foreign", permission="foreign"),
            entry("unrelated", title="Completely different", note="No matching words")]
    term, fts = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
    try:
        evaluation._build_term(term, rows)
        evaluation._build_fts(fts, rows)
        q = query()
        raw_fts = evaluation._fts_candidates(fts, q, phrase=False)
        assert {row[0] for row in raw_fts} >= {"allowed", "obsolete", "revoked", "foreign"}
        found, rejected = evaluation.guarded_sources(raw_fts, rows, q, "fts_conjunction")
        assert found == ["allowed:v1"]
        assert rejected["missing_or_stale"] >= 1 and rejected["denied"] == 2

        # A damaged term index can nominate an unrelated current row. The text
        # and permission check must still reject it before a result is shown.
        term.executemany("INSERT INTO evidence_library_term_index VALUES (?,?,?)", [
            ("marker", "unrelated", 1), ("term", "unrelated", 1),
        ])
        nominated = evaluation._term_candidates(term, q)
        assert "unrelated" in {row[0] for row in nominated}
        found, rejected = evaluation.guarded_sources(nominated, rows, q, "term_index")
        assert found == ["allowed:v1"] and rejected["index_mismatch"] == 1

        fts.execute(
            "INSERT INTO evidence_library_fts(entry_id,entry_version,title,relevance_note) "
            "VALUES (?,?,?,?)", ("unrelated", 1, "Marker term", "forged index content"),
        )
        nominated = evaluation._fts_candidates(fts, q, phrase=False)
        assert "unrelated" in {row[0] for row in nominated}
        found, rejected = evaluation.guarded_sources(nominated, rows, q, "fts_conjunction")
        assert found == ["allowed:v1"] and rejected["index_mismatch"] >= 1
    finally:
        term.close()
        fts.close()


def test_permission_revocation_and_head_change_apply_without_index_rebuild():
    row = entry("once-visible")
    db = sqlite3.connect(":memory:")
    try:
        evaluation._build_fts(db, [row])
        candidates = evaluation._fts_candidates(db, query(), phrase=False)
        assert evaluation.guarded_sources(candidates, [row], query(), "fts_conjunction")[0] == [
            "once-visible:v1"
        ]
        revoked = replace(row, permission="revoked")
        stale = replace(row, head_version=2)
        assert evaluation.guarded_sources(candidates, [revoked], query(), "fts_conjunction")[0] == []
        assert evaluation.guarded_sources(candidates, [stale], query(), "fts_conjunction")[0] == []
    finally:
        db.close()


def test_paging_is_applied_after_guard_and_deduplication():
    rows = [entry("a", source="one", curated="2026-01-03T00:00:00Z"),
            entry("a-duplicate", source="one", curated="2026-01-02T00:00:00Z"),
            entry("b", source="two", curated="2026-01-01T00:00:00Z"),
            entry("revoked", permission="revoked"),
            entry("foreign", permission="foreign")]
    candidates = [(row.entry_id, row.version, 0.0) for row in rows]
    visible, _ = evaluation.guarded_sources(candidates, rows, query(), "term_index")
    assert visible == ["one", "two"]
    first = evaluation.page_sources(visible, cursor=0, limit=1)
    second = evaluation.page_sources(visible, cursor=first["next_cursor"], limit=1)
    assert first["source_keys"] + second["source_keys"] == visible
    assert first["matching_sources"] == second["matching_sources"] == 2
    assert second["next_cursor"] is None
    with pytest.raises(ValueError):
        evaluation.page_sources(visible, cursor=-1)


def test_versioned_qrels_refuse_denied_sources_and_conflicting_heads(tmp_path):
    data = json.loads(evaluation.CORPUS.read_text())
    data["queries"][0]["relevant_source_keys"] = ["s17:v1"]
    file = tmp_path / "bad-qrels.json"
    file.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="unauthorized query labels"):
        evaluation.load_corpus(file)
    data = json.loads(evaluation.CORPUS.read_text())
    stale = next(row for row in data["entries"] if row["entry_id"] == "e16" and row["version"] == 1)
    stale["head_version"] = 1
    file.write_text(json.dumps(data))
    with pytest.raises(ValueError, match="conflicts"):
        evaluation.load_corpus(file)


def test_literal_operators_numeric_queries_and_report_scope():
    result = evaluation.evaluate(evaluation.CORPUS, repeats=2)
    rows = {row["query_id"]: row for row in result["per_query"]}
    assert result["entries"] == 23 and result["queries"] == 16
    assert result["relevance_basis"] == "root_authored_synthetic_scenarios_not_scientific_ground_truth"
    for mode in evaluation.MODES:
        assert rows["q09"]["modes"][mode]["source_keys"] == ["s12:v1"]
        assert rows["q05"]["modes"][mode]["source_keys"] == ["s07:v1"]
        assert rows["q06"]["modes"][mode]["source_keys"] == ["s08:v1"]
        assert rows["q14"]["modes"][mode]["source_keys"] == []
        assert rows["q15"]["modes"][mode]["source_keys"] == []
        assert "s16:v1" not in rows["q12"]["modes"][mode]["source_keys"]
    assert result["aggregate"]["fts_phrase"]["macro_recall_at_5"] < (
        result["aggregate"]["term_index"]["macro_recall_at_5"]
    )
