"""Notify authenticated local viewers when durable state changes.

The stream carries invalidations, never research content or an event history.
Every connection begins with a full-read hint, so reconnect does not depend on
replaying a missed cursor. Periodic client reads still recheck permissions and
artifact integrity, including changes outside SQLite.
"""

import sqlite3
import time
from contextlib import closing
from pathlib import Path


def updates_db_path(host):
    store = getattr(host, "store", None)
    db_path = getattr(store, "db_path", None)
    if not isinstance(db_path, Path) or not db_path.is_file() or db_path.is_symlink():
        return None
    return db_path


def supports_updates(host):
    db_path = updates_db_path(host)
    if db_path is None:
        return False
    try:
        with closing(sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)) as db:
            return db.execute("PRAGMA data_version").fetchone() is not None
    except sqlite3.Error:
        return False


def stream_updates(handler):
    db_path = updates_db_path(handler.server.research_host)
    if db_path is None:
        return handler._json(501, {"error": "Change notifications are unavailable."})
    if not handler.server.update_slots.acquire(blocking=False):
        return handler._json(503, {"error": "Too many active update streams."})
    db = None
    try:
        try:
            db = sqlite3.connect(f"{db_path.as_uri()}?mode=ro", uri=True)
            version = db.execute("PRAGMA data_version").fetchone()[0]
        except sqlite3.Error:
            return handler._json(503, {"error": "Change notifications are unavailable."})
        handler.connection.settimeout(10)
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Cache-Control", "no-store")
        handler.send_header("X-Content-Type-Options", "nosniff")
        handler.send_header("Referrer-Policy", "no-referrer")
        handler.send_header("Connection", "close")
        handler.end_headers()
        handler.wfile.write(b"event: invalidate\ndata: {}\n\n")
        handler.wfile.flush()
        heartbeat = time.monotonic()
        while not handler.server.updates_stopped.wait(0.2):
            current = db.execute("PRAGMA data_version").fetchone()[0]
            changed = current != version
            if changed or time.monotonic() - heartbeat >= 5:
                event = b"invalidate" if changed else b"heartbeat"
                handler.wfile.write(b"event: " + event + b"\ndata: {}\n\n")
                handler.wfile.flush()
                heartbeat = time.monotonic()
                version = current
    except (BrokenPipeError, ConnectionResetError, sqlite3.Error, OSError):
        # A closed stream triggers client fallback/reconnect; never expose paths.
        pass
    finally:
        if db is not None:
            db.close()
        handler.server.update_slots.release()
        handler.close_connection = True
