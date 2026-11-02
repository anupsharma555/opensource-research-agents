"""Offline, synthetic comparison of evidence metadata FTS5 and provider HTTPX.

Run with ``.venv/bin/python scripts/evaluate_stack_candidates.py --output PATH``.
No provider, model, public host, credential, or saved research state is accessed.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import socket
import sqlite3
import statistics
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from ora.mission.evidence_library import _terms
from ora.retrieval.providers import HTTPResult, http_transport

BYTE_LIMIT = 8 * 1024 * 1024 + 1


@dataclass(frozen=True)
class Entry:
    id: str
    version: int
    title: str
    note: str
    current: bool = True
    permitted: bool = True


PROBES = [
    ("phrase", "immune checkpoint", True),
    ("abbreviation", "PTSD", None),
    ("expanded_abbreviation", "posttraumatic stress disorder", None),
    ("negation", "no mortality benefit", True),
    ("numeric_unit", "2.5 mg", True),
    ("superseded_version", "original results", True),
    ("revoked_permission", "cytokine", None),
    ("punctuation", "SARS-CoV-2", True),
    ("fts_syntax_literal", "cytokine OR original", None),
]


def synthetic_entries(filler_count: int) -> list[Entry]:
    """Include deliberate traps; these are mechanical probes, not relevance labels."""
    entries = [
        Entry("ptsd-expanded", 1, "Posttraumatic stress disorder trial", "PTSD remission"),
        Entry("ptsd-abbrev", 1, "PTSD follow-up", "symptoms after discharge"),
        Entry("phrase-adjacent", 1, "Immune checkpoint inhibitor response", "cohort"),
        Entry("phrase-separated", 1, "Immune signaling with checkpoint inhibition", "cohort"),
        Entry("negated", 1, "No mortality benefit", "intervention arm"),
        Entry("negation-separated", 1, "Mortality benefit observed", "No safety signal"),
        Entry("dose-decimal", 1, "Dose 2.5 mg daily", "trial"),
        Entry("dose-integer", 1, "Dose 25 mg daily", "trial"),
        Entry("versioned", 1, "Results", "original results", current=False),
        Entry("versioned", 2, "Results", "corrected results"),
        Entry("cytokine-revoked", 1, "Cytokine trial", "review", permitted=False),
        Entry("cytokine-allowed", 1, "Cytokine trial", "review"),
        Entry("hyphenated", 1, "SARS-CoV-2 trial", "follow-up"),
    ]
    entries.extend(
        Entry(f"filler-{n:05d}", 1, f"Synthetic protocol record {n}", "unrelated metadata")
        for n in range(filler_count)
    )
    return entries


def _create_indexes(entries: list[Entry]) -> tuple[sqlite3.Connection, sqlite3.Connection, dict]:
    term_db, fts_db = sqlite3.connect(":memory:"), sqlite3.connect(":memory:")
    term_db.execute(
        "CREATE TABLE term_index (term TEXT, entry_id TEXT, entry_version INTEGER, "
        "PRIMARY KEY(term, entry_id)) WITHOUT ROWID"
    )
    fts_db.execute(
        "CREATE VIRTUAL TABLE fts_index USING fts5("
        "entry_id UNINDEXED, entry_version UNINDEXED, title, note, tokenize='unicode61')"
    )
    start = time.perf_counter_ns()
    for entry in entries:
        # The production term index is regenerated from current heads only.
        if entry.current:
            term_db.executemany(
                "INSERT INTO term_index VALUES (?,?,?)",
                [(term, entry.id, entry.version) for term in _terms(entry.title + " " + entry.note)],
            )
    term_db.commit()
    term_build_ms = (time.perf_counter_ns() - start) / 1e6
    start = time.perf_counter_ns()
    # Deliberately leave a superseded FTS row to test the mandatory head guard.
    fts_db.executemany(
        "INSERT INTO fts_index(entry_id,entry_version,title,note) VALUES (?,?,?,?)",
        [(x.id, x.version, x.title, x.note) for x in entries],
    )
    fts_db.commit()
    fts_build_ms = (time.perf_counter_ns() - start) / 1e6
    stats = {
        "term_build_ms": round(term_build_ms, 3),
        "fts_build_ms": round(fts_build_ms, 3),
        "term_pages_bytes": term_db.execute("PRAGMA page_count").fetchone()[0]
        * term_db.execute("PRAGMA page_size").fetchone()[0],
        "fts_pages_bytes": fts_db.execute("PRAGMA page_count").fetchone()[0]
        * fts_db.execute("PRAGMA page_size").fetchone()[0],
    }
    return term_db, fts_db, stats


def _term_candidates(db: sqlite3.Connection, query: str) -> list[tuple[str, int]]:
    words = sorted(_terms(query))
    placeholders = ",".join("?" for _ in words)
    rows = db.execute(
        "SELECT entry_id,entry_version FROM term_index "
        f"WHERE term IN ({placeholders}) GROUP BY entry_id,entry_version "
        "HAVING COUNT(*)=? ORDER BY entry_id",
        (*words, len(words)),
    )
    return [(entry_id, version) for entry_id, version in rows]


def _fts_candidates(db: sqlite3.Connection, query: str, *, phrase: bool) -> list[tuple[str, int]]:
    # Quote every term: user input cannot become MATCH operators or column selectors.
    words = sorted(_terms(query))
    match = ('"' + " ".join(_ordered_terms(query)) + '"') if phrase else (
        " AND ".join('"' + word + '"' for word in words)
    )
    rows = db.execute(
        "SELECT entry_id,entry_version FROM fts_index WHERE fts_index MATCH ? "
        "ORDER BY bm25(fts_index), entry_id", (match,),
    )
    return [(entry_id, version) for entry_id, version in rows]


def _ordered_terms(query: str) -> tuple[str, ...]:
    # Preserve phrase order while using the same literal-token vocabulary.
    import re

    return tuple(re.findall(r"\w+", query.casefold()))


def _guard(candidates: list[tuple[str, int]], entries: list[Entry]) -> list[str]:
    current = {(x.id, x.version) for x in entries if x.current and x.permitted}
    return [entry_id for entry_id, version in candidates if (entry_id, version) in current]


def _median_query_ms(fn, repetitions: int) -> float:
    samples = []
    for _ in range(repetitions):
        start = time.perf_counter_ns()
        fn()
        samples.append((time.perf_counter_ns() - start) / 1e6)
    return round(statistics.median(samples), 4)


def evaluate_fts(*, filler_count: int = 2_000, repetitions: int = 100) -> dict:
    entries = synthetic_entries(filler_count)
    term_db, fts_db, build_stats = _create_indexes(entries)
    try:
        cases = []
        for name, query, test_phrase in PROBES:
            term_raw = _term_candidates(term_db, query)
            fts_raw = _fts_candidates(fts_db, query, phrase=False)
            phrase_raw = _fts_candidates(fts_db, query, phrase=True) if test_phrase else None
            cases.append({
                "probe": name, "query": query,
                "current_raw": [list(x) for x in term_raw],
                "current_guarded": _guard(term_raw, entries),
                "fts_raw": [list(x) for x in fts_raw],
                "fts_guarded": _guard(fts_raw, entries),
                "fts_phrase_guarded": _guard(phrase_raw, entries) if phrase_raw is not None else None,
                "current_median_ms": _median_query_ms(
                    lambda: _term_candidates(term_db, query), repetitions
                ),
                "fts_median_ms": _median_query_ms(
                    lambda: _fts_candidates(fts_db, query, phrase=False), repetitions
                ),
            })
        return {
            "sqlite_version": sqlite3.sqlite_version,
            "fts5_compiled": bool(term_db.execute(
                "SELECT sqlite_compileoption_used('ENABLE_FTS5')"
            ).fetchone()[0]),
            "entries": len(entries), "filler_count": filler_count,
            "repetitions_per_probe": repetitions, "build": build_stats, "cases": cases,
        }
    finally:
        term_db.close()
        fts_db.close()


class _FixtureHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def setup(self):
        super().setup()
        with self.server.lock:
            self.server.connections += 1

    def log_message(self, *_args):
        pass

    def _send(self, status: int, body: bytes, **headers: str):
        self.send_response(status)
        self.send_header("Content-Length", str(len(body)))
        for key, value in headers.items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        path = urlsplit(self.path).path
        if path == "/redirect":
            self._send(302, b"", Location="/ok")
        elif path == "/ok":
            self._send(200, b"fixture-ok", **{"Content-Type": "text/plain"})
        elif path == "/error":
            self._send(503, b"temporary", **{"Retry-After": "2"})
        elif path == "/oversize":
            self._send(200, b"x" * (BYTE_LIMIT + 1024))
        elif path == "/compressed":
            self._send(200, gzip.compress(b"fixture-compressed"),
                       **{"Content-Encoding": "gzip"})
        elif path == "/unknown":
            self.connection.shutdown(socket.SHUT_RDWR)
            self.connection.close()
        else:
            self._send(404, b"missing")

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0"))
        body = self.rfile.read(length)
        value = json.dumps({
            "body": body.decode("ascii"),
            "content_type": self.headers.get("Content-Type"),
            "accept_encoding": self.headers.get("Accept-Encoding"),
        }, sort_keys=True).encode()
        self._send(201, value, **{"Content-Type": "application/json"})


def _httpx_transport(client: httpx.Client, method: str, url: str, **kwargs) -> HTTPResult:
    data = json.dumps(kwargs["body"]).encode() if kwargs.get("body") is not None else None
    headers = {"User-Agent": "ORA-stage4-discovery/0.1", "Accept-Encoding": "identity"}
    if data is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
    headers.update(kwargs.get("headers") or {})
    with client.stream(method, url, content=data, headers=headers,
                       timeout=kwargs.get("timeout", 20)) as response:
        content = bytearray()
        # urllib returns wire bytes and applies its limit before content decoding.
        for chunk in response.iter_raw():
            content.extend(chunk[: max(0, BYTE_LIMIT - len(content))])
            if len(content) >= BYTE_LIMIT:
                break
        return HTTPResult(response.status_code, bytes(content), dict(response.headers))


def _outcome(fn) -> dict:
    try:
        response = fn()
        return {
            "kind": "response", "status": response.status,
            "body_bytes": len(response.body),
            "body_sha256": hashlib.sha256(response.body).hexdigest(),
            "body_prefix": response.body[:32].decode("ascii", errors="replace"),
            "retry_after": response.headers.get("retry-after"),
        }
    except Exception as exc:
        return {"kind": "unknown_response", "exception": type(exc).__name__}


def evaluate_http(*, repetitions: int = 30) -> dict:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _FixtureHandler)
    server.lock = threading.Lock()
    server.connections = 0
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    origin = f"http://127.0.0.1:{server.server_port}"
    try:
        with httpx.Client(follow_redirects=True, trust_env=False) as client:
            cases = []
            for name, method, path, body in [
                ("success", "GET", "/ok", None),
                ("redirect", "GET", "/redirect", None),
                ("non_2xx", "GET", "/error", None),
                ("missing", "GET", "/missing", None),
                ("post", "POST", "/echo", {"synthetic": "value"}),
                ("byte_bound", "GET", "/oversize", None),
                ("compressed_raw", "GET", "/compressed", None),
                ("unknown_response", "GET", "/unknown", None),
            ]:
                kwargs = {"body": body, "timeout": 3}
                cases.append({
                    "probe": name,
                    "urllib": _outcome(lambda: http_transport(method, origin + path, **kwargs)),
                    "httpx": _outcome(lambda: _httpx_transport(
                        client, method, origin + path, **kwargs
                    )),
                })
            def measure(fn):
                before = server.connections
                start = time.perf_counter_ns()
                for _ in range(repetitions):
                    fn()
                return {
                    "elapsed_ms": round((time.perf_counter_ns() - start) / 1e6, 3),
                    "connections": server.connections - before,
                }

            urllib_repeated = measure(lambda: http_transport("GET", origin + "/ok", timeout=3))
            httpx_repeated = measure(lambda: _httpx_transport(
                client, "GET", origin + "/ok", timeout=3
            ))
        return {
            "httpx_version": httpx.__version__,
            "fixture": "loopback ThreadingHTTPServer; HTTP/1.1 persistent responses",
            "cases": cases, "repeated_gets": repetitions,
            "urllib_repeated": urllib_repeated,
            "httpx_repeated": httpx_repeated,
        }
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=3)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--filler-count", type=int, default=2_000)
    parser.add_argument("--repetitions", type=int, default=100)
    args = parser.parse_args()
    if args.filler_count < 0 or args.repetitions < 1:
        parser.error("filler-count must be >= 0 and repetitions must be >= 1")
    result = {
        "scope": "synthetic offline diagnostic; no scientific retrieval quality or remote reliability claim",
        "fts": evaluate_fts(filler_count=args.filler_count, repetitions=args.repetitions),
        "http": evaluate_http(repetitions=min(args.repetitions, 50)),
    }
    serialized = json.dumps(result, indent=2, sort_keys=True) + "\n"
    if args.output:
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(serialized)
    else:
        print(serialized, end="")


if __name__ == "__main__":
    main()
