"""Offline comparison of the current metadata lookup with guarded SQLite FTS5.

The labeled corpus is synthetic and authored separately from this harness.
This does not read research state, contact providers, or change production indexes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import statistics
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ora.mission.evidence_library import _terms

ROOT = Path(__file__).resolve().parents[1]
CORPUS = ROOT / "benchmarks/evidence_fts/corpus.json"
SCHEMA = "ora.evidence_fts_eval.v1"
MODES = ("term_index", "fts_conjunction", "fts_phrase")
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._-]{0,79}\Z")


@dataclass(frozen=True)
class Entry:
    entry_id: str
    version: int
    head_version: int
    source_key: str
    title: str
    relevance_note: str
    curated_at: str
    permission: str

    @property
    def eligible(self) -> bool:
        return self.version == self.head_version and self.permission == "permitted"


@dataclass(frozen=True)
class Query:
    query_id: str
    text: str
    relevant_source_keys: tuple[str, ...]
    tags: tuple[str, ...]


def _ordered_terms(text: str) -> tuple[str, ...]:
    return tuple(re.findall(r"\w+", text.casefold()))


def _phrase_in(needle: tuple[str, ...], haystack: tuple[str, ...]) -> bool:
    return any(haystack[index:index + len(needle)] == needle
               for index in range(len(haystack) - len(needle) + 1))


def load_corpus(path: Path) -> tuple[list[Entry], list[Query], str]:
    raw = path.read_bytes()
    data = json.loads(raw)
    if (not isinstance(data, dict) or set(data) != {"schema", "entries", "queries"}
            or data["schema"] != SCHEMA
            or not isinstance(data["entries"], list) or not data["entries"]
            or not isinstance(data["queries"], list) or not data["queries"]):
        raise ValueError("FTS evaluation needs the versioned corpus and query lists")
    entries = []
    seen_refs, heads, source_permissions = set(), {}, {}
    for item in data["entries"]:
        if not isinstance(item, dict) or set(item) != set(Entry.__dataclass_fields__):
            raise ValueError("Corpus entry fields differ from the pinned format")
        entry = Entry(**item)
        if (not _ID.fullmatch(entry.entry_id) or not _ID.fullmatch(entry.source_key)
                or type(entry.version) is not int or entry.version < 1
                or type(entry.head_version) is not int or entry.head_version < 1
                or not isinstance(entry.title, str) or not entry.title.strip()
                or len(entry.title) > 180
                or not isinstance(entry.relevance_note, str) or len(entry.relevance_note) > 240
                or entry.permission not in {"permitted", "revoked", "foreign"}
                or not isinstance(entry.curated_at, str)
                or not entry.curated_at.endswith("Z")):
            raise ValueError("Corpus has an invalid entry value")
        try:
            datetime.fromisoformat(entry.curated_at.replace("Z", "+00:00"))
        except ValueError as exc:
            raise ValueError("Corpus needs exact ISO timestamps") from exc
        ref = (entry.entry_id, entry.version)
        if (ref in seen_refs or heads.setdefault(entry.entry_id, entry.head_version)
                != entry.head_version
                or source_permissions.setdefault(entry.source_key, entry.permission)
                != entry.permission):
            raise ValueError("Corpus repeats a reference or conflicts on a current head or use")
        seen_refs.add(ref)
        entries.append(entry)
    if any(not any(row.entry_id == entry_id and row.version == head for row in entries)
           for entry_id, head in heads.items()):
        raise ValueError("Every entry ID needs its current head row")
    eligible_sources = {entry.source_key for entry in entries if entry.eligible}
    queries, seen_queries = [], set()
    for item in data["queries"]:
        if not isinstance(item, dict) or set(item) != set(Query.__dataclass_fields__):
            raise ValueError("Corpus query fields differ from the pinned format")
        if (not isinstance(item["relevant_source_keys"], list)
                or not isinstance(item["tags"], list)):
            raise ValueError("Corpus query labels and tags must be lists")
        query = Query(item["query_id"], item["text"],
                      tuple(item["relevant_source_keys"]), tuple(item["tags"]))
        if (not _ID.fullmatch(query.query_id) or query.query_id in seen_queries
                or not isinstance(query.text, str) or not 1 <= len(query.text) <= 500
                or not _terms(query.text)
                or any(not isinstance(value, str) for value in item["relevant_source_keys"])
                or len(query.relevant_source_keys) != len(set(query.relevant_source_keys))
                or set(query.relevant_source_keys) - eligible_sources
                or any(not isinstance(tag, str) or not _ID.fullmatch(tag)
                       for tag in query.tags)):
            raise ValueError("Corpus has invalid or unauthorized query labels")
        seen_queries.add(query.query_id)
        queries.append(query)
    return entries, queries, hashlib.sha256(raw).hexdigest()


def _build_term(db: sqlite3.Connection, entries: list[Entry]) -> None:
    db.execute("DROP TABLE IF EXISTS evidence_library_term_index")
    db.execute(
        "CREATE TABLE evidence_library_term_index ("
        "term TEXT NOT NULL, entry_id TEXT NOT NULL, entry_version INTEGER NOT NULL, "
        "PRIMARY KEY(term, entry_id)) WITHOUT ROWID"
    )
    for entry in entries:
        if entry.version == entry.head_version:
            db.executemany(
                "INSERT INTO evidence_library_term_index VALUES (?,?,?)",
                [(term, entry.entry_id, entry.version)
                 for term in _terms(entry.title + " " + entry.relevance_note)],
            )
    db.commit()


def _build_fts(db: sqlite3.Connection, entries: list[Entry]) -> None:
    db.execute("DROP TABLE IF EXISTS evidence_library_fts")
    db.execute(
        "CREATE VIRTUAL TABLE evidence_library_fts USING fts5("
        "entry_id UNINDEXED, entry_version UNINDEXED, title, relevance_note, "
        "tokenize='unicode61')"
    )
    # Include stale and denied rows to challenge the mandatory result guard.
    db.executemany(
        "INSERT INTO evidence_library_fts(entry_id,entry_version,title,relevance_note) "
        "VALUES (?,?,?,?)",
        [(row.entry_id, row.version, row.title, row.relevance_note) for row in entries],
    )
    db.commit()


def _term_candidates(db: sqlite3.Connection, query: Query) -> list[tuple[str, int, float]]:
    words = sorted(_terms(query.text))
    placeholders = ",".join("?" for _ in words)
    rows = db.execute(
        "SELECT entry_id,entry_version FROM evidence_library_term_index "
        f"WHERE term IN ({placeholders}) GROUP BY entry_id,entry_version "
        "HAVING COUNT(*)=? ORDER BY entry_id", (*words, len(words)),
    )
    return [(row[0], row[1], 0.0) for row in rows]


def _fts_candidates(db: sqlite3.Connection, query: Query, *, phrase: bool) -> list[tuple[str, int, float]]:
    words = _ordered_terms(query.text) if phrase else sorted(_terms(query.text))
    # Each term is quoted, so a user-supplied OR, colon or dash cannot become MATCH syntax.
    match = ('"' + " ".join(words) + '"') if phrase else " AND ".join(
        '"' + word + '"' for word in words
    )
    rows = db.execute(
        "SELECT entry_id,entry_version,bm25(evidence_library_fts) "
        "FROM evidence_library_fts WHERE evidence_library_fts MATCH ? "
        "ORDER BY bm25(evidence_library_fts),entry_id,entry_version", (match,),
    )
    return [(row[0], row[1], float(row[2])) for row in rows]


def guarded_sources(candidates: list[tuple[str, int, float]], entries: list[Entry],
                    query: Query, mode: str) -> tuple[list[str], dict]:
    """Resolve the current authorized row again before any ID is returned."""
    if mode not in MODES:
        raise ValueError("Unknown evaluation mode")
    authority = {(row.entry_id, row.version): row for row in entries}
    selected: dict[str, tuple[Entry, float]] = {}
    rejected = {"missing_or_stale": 0, "denied": 0, "index_mismatch": 0}
    words, ordered = _terms(query.text), _ordered_terms(query.text)
    for entry_id, version, score in candidates:
        entry = authority.get((entry_id, version))
        if entry is None or entry.version != entry.head_version:
            rejected["missing_or_stale"] += 1
            continue
        if entry.permission != "permitted":
            rejected["denied"] += 1
            continue
        text = entry.title + " " + entry.relevance_note
        if mode == "fts_phrase":
            matches = (_phrase_in(ordered, _ordered_terms(entry.title))
                       or _phrase_in(ordered, _ordered_terms(entry.relevance_note)))
        else:
            matches = words <= _terms(text)
        if not matches:
            rejected["index_mismatch"] += 1
            continue
        previous = selected.get(entry.source_key)
        if (previous is None
                or mode == "term_index" and (entry.curated_at, entry.entry_id)
                > (previous[0].curated_at, previous[0].entry_id)
                or mode != "term_index" and (score, -int(entry.version), entry.entry_id)
                < (previous[1], -int(previous[0].version), previous[0].entry_id)):
            selected[entry.source_key] = (entry, score)
    if mode == "term_index":
        ranked = sorted(selected, key=lambda key: (
            selected[key][0].curated_at, selected[key][0].entry_id
        ), reverse=True)
    else:
        ranked = sorted(selected, key=lambda key: (
            selected[key][1], selected[key][0].entry_id, key
        ))
    return ranked, rejected


def page_sources(sources: list[str], *, cursor: int = 0, limit: int = 5) -> dict:
    if type(cursor) is not int or cursor < 0 or type(limit) is not int or not 1 <= limit <= 20:
        raise ValueError("Paging needs cursor >= 0 and limit 1 to 20")
    page = sources[cursor:cursor + limit]
    return {"source_keys": page, "matching_sources": len(sources), "cursor": cursor,
            "next_cursor": cursor + len(page) if cursor + len(page) < len(sources) else None}


def _quality(sources: list[str], relevant: tuple[str, ...], *, k: int = 5) -> dict:
    top = sources[:k]
    hits = [index + 1 for index, key in enumerate(top) if key in relevant]
    return {"relevant_count": len(relevant), "returned_count": len(top),
            "relevant_returned": len(hits),
            "recall_at_5": len(hits) / len(relevant) if relevant else None,
            "precision_among_returned_at_5": len(hits) / len(top) if top else None,
            "reciprocal_rank_at_5": 1 / hits[0] if hits else 0.0,
            "empty_qrel_false_positives": len(top) if not relevant else None}


def evaluate(corpus: Path, *, repeats: int = 100) -> dict:
    if type(repeats) is not int or not 1 <= repeats <= 10_000:
        raise ValueError("Timing repeats must be between 1 and 10000")
    entries, queries, corpus_sha = load_corpus(corpus)
    with tempfile.TemporaryDirectory(prefix="ora-evidence-fts-") as folder:
        paths = {"term_index": Path(folder) / "term.sqlite",
                 "fts_conjunction": Path(folder) / "fts.sqlite"}
        term, fts = (sqlite3.connect(paths["term_index"]),
                     sqlite3.connect(paths["fts_conjunction"]))
        try:
            options = {row[0] for row in term.execute("PRAGMA compile_options")}
            if "ENABLE_FTS5" not in options:
                raise RuntimeError("Selected Python SQLite does not include FTS5")
            build, rebuild, sizes = {}, {}, {}
            for name, db, builder in (
                ("term_index", term, _build_term), ("fts", fts, _build_fts)
            ):
                start = time.perf_counter_ns()
                builder(db, entries)
                build[name] = (time.perf_counter_ns() - start) / 1e6
                sizes[name] = paths["term_index" if name == "term_index" else "fts_conjunction"].stat().st_size
                start = time.perf_counter_ns()
                builder(db, entries)
                rebuild[name] = (time.perf_counter_ns() - start) / 1e6
            rows = []
            per_mode_times: dict[str, list[float]] = {name: [] for name in MODES}
            for query in queries:
                modes = {}
                for mode in MODES:
                    db = term if mode == "term_index" else fts

                    def retrieve() -> tuple[list[str], dict, int]:
                        raw = (_term_candidates(db, query) if mode == "term_index" else
                               _fts_candidates(db, query, phrase=mode == "fts_phrase"))
                        guarded, rejected = guarded_sources(raw, entries, query, mode)
                        return guarded, rejected, len(raw)

                    sources, rejected, raw_count = retrieve()
                    measurements = []
                    for _ in range(repeats):
                        start = time.perf_counter_ns()
                        retrieve()
                        measurements.append((time.perf_counter_ns() - start) / 1e6)
                    median_ms = statistics.median(measurements)
                    per_mode_times[mode].append(median_ms)
                    modes[mode] = {"source_keys": sources[:5], "matching_sources": len(sources),
                                   "raw_candidate_rows": raw_count, "rejected_raw_rows": rejected,
                                   "median_query_and_guard_ms": median_ms,
                                   "quality": _quality(sources, query.relevant_source_keys)}
                rows.append({"query_id": query.query_id, "text": query.text,
                             "tags": list(query.tags),
                             "relevant_source_keys": list(query.relevant_source_keys),
                             "modes": modes})
            aggregate = {}
            for mode in MODES:
                positive = [row["modes"][mode]["quality"] for row in rows
                            if row["relevant_source_keys"]]
                negatives = [row["modes"][mode]["quality"] for row in rows
                             if not row["relevant_source_keys"]]
                precision_values = [row["precision_among_returned_at_5"] for row in positive
                                    if row["precision_among_returned_at_5"] is not None]
                aggregate[mode] = {
                    "positive_queries": len(positive), "empty_qrel_queries": len(negatives),
                    "macro_recall_at_5": (statistics.mean(row["recall_at_5"] for row in positive)
                                          if positive else None),
                    "macro_precision_among_returned_at_5": (statistics.mean(precision_values)
                                                            if precision_values else None),
                    "precision_returned_query_coverage": len(precision_values),
                    "macro_reciprocal_rank_at_5": (
                        statistics.mean(row["reciprocal_rank_at_5"] for row in positive)
                        if positive else None),
                    "empty_qrel_false_positives": sum(
                        row["empty_qrel_false_positives"] for row in negatives),
                    "median_query_and_guard_ms": statistics.median(per_mode_times[mode]),
                }
            by_tag = {}
            for tag in sorted({tag for query in queries for tag in query.tags}):
                tagged = [row for row in rows if tag in row["tags"]]
                by_tag[tag] = {}
                for mode in MODES:
                    positive = [row["modes"][mode]["quality"] for row in tagged
                                if row["relevant_source_keys"]]
                    negatives = [row["modes"][mode]["quality"] for row in tagged
                                 if not row["relevant_source_keys"]]
                    by_tag[tag][mode] = {
                        "positive_queries": len(positive),
                        "macro_recall_at_5": (
                            statistics.mean(row["recall_at_5"] for row in positive)
                            if positive else None),
                        "macro_reciprocal_rank_at_5": (
                            statistics.mean(row["reciprocal_rank_at_5"] for row in positive)
                            if positive else None),
                        "empty_qrel_queries": len(negatives),
                        "empty_qrel_false_positives": sum(
                            row["empty_qrel_false_positives"] for row in negatives),
                    }
            return {"schema": "ora.evidence_fts_evaluation_result.v1",
                    "corpus_sha256": corpus_sha,
                    "harness_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
                    "sqlite_version": sqlite3.sqlite_version, "fts5_compiled": True,
                    "entries": len(entries), "queries": len(queries), "repeats_per_query": repeats,
                    "relevance_basis": "root_authored_synthetic_scenarios_not_scientific_ground_truth",
                    "guard_basis": "synthetic_head_version_and_permission_before_any_result",
                    "ranking": {"term_index": "current_curated_at_desc_entry_id_desc",
                                "fts_conjunction": "bm25_asc_entry_id_asc",
                                "fts_phrase": "bm25_asc_entry_id_asc"},
                    "metric_definitions": {
                        "recall_at_5": "relevant returned in first five / all labeled relevant; positive queries only",
                        "precision_among_returned_at_5": "relevant returned / number actually returned, up to five",
                        "reciprocal_rank_at_5": "1 / rank of first relevant in first five, else 0",
                        "empty_qrel_false_positives": "number returned in first five when label set is empty",
                        "median_query_and_guard_ms": "median wall time over repeated SQL plus authoritative synthetic guard",
                    },
                    "index_build_ms": build, "same_data_rebuild_ms": rebuild,
                    "index_file_bytes_after_first_build": sizes,
                    "aggregate": aggregate, "by_tag": by_tag, "per_query": rows}
        finally:
            term.close()
            fts.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--corpus", type=Path, default=CORPUS)
    parser.add_argument("--repeats", type=int, default=100)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args(argv)
    result = evaluate(args.corpus, repeats=args.repeats)
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(json.dumps(result, indent=2, sort_keys=True) + "\n")
    print(json.dumps({"entries": result["entries"], "queries": result["queries"],
                      "aggregate": result["aggregate"]}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
