"""Start the explicit local live host or inspect it without creating research work.

The launch record stays in private local state and supplies a browser session or
native window connection. Provider credentials remain in the Python host; opening
or inspecting the app never submits a model request.
"""

import json
import os
import subprocess
import tempfile
import urllib.error
import urllib.request
import webbrowser
from pathlib import Path
from urllib.parse import urlencode, urlsplit


def configure(parser):
    actions = parser.add_subparsers(dest="live_action", required=True)
    for action in ("serve", "open", "status", "provenance", "migrate-provenance"):
        command = actions.add_parser(action)
        command.add_argument("--state-dir", type=Path, default=Path(".local/live"))
        if action == "serve":
            command.add_argument("--allow-model-calls", action="store_true")
            command.add_argument("--port", type=int, default=0)
            command.add_argument("--run-budget-usd", type=float, default=0.10)
        elif action == "open":
            command.add_argument("--browser", action="store_true")
        elif action == "status":
            command.add_argument("--run-id")
            command.add_argument("--calls", action="store_true")
        elif action == "provenance":
            command.add_argument("--run-id")
            command.add_argument(
                "--view", "--section",
                choices=("all", "calls", "tools", "configurations", "skills", "sources"),
                default="all",
            )
            command.add_argument("--limit", type=int, default=100)


def _connection(state_dir):
    try:
        data = json.loads((state_dir / "connection.json").read_text())
    except (OSError, ValueError):
        raise ValueError("Start the local server before opening or inspecting a live run.") from None
    url = urlsplit(data.get("origin", ""))
    if url.scheme != "http" or url.hostname != "127.0.0.1" or not url.port or url.path:
        raise ValueError("Invalid local server connection record.")
    return data


def _open_host_lock(state_dir):
    """Open the lock without following a preexisting directory-entry symlink."""
    import fcntl

    try:
        fd = os.open(state_dir / "host.lock",
                     os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
    except OSError:
        raise ValueError("The live host lock cannot be opened safely.") from None
    lock = os.fdopen(fd, "a")
    try:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock.close()
        raise ValueError("A live host already owns this state directory.") from None
    return lock


def _write_connection(state_dir, value):
    """Atomically replace only the connection entry with a private regular file."""
    fd, temporary_name = tempfile.mkstemp(prefix=".connection-", dir=state_dir)
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(value, handle)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, state_dir / "connection.json")
    finally:
        temporary.unlink(missing_ok=True)


def main(args, root):
    state_dir = args.state_dir.resolve()
    if state_dir.is_relative_to(root) and not state_dir.is_relative_to(root / ".local"):
        raise ValueError("Keep private run state under .local or outside the repository.")
    if args.live_action == "provenance":
        from .inspection import inspect_provenance

        print(json.dumps(
            inspect_provenance(
                state_dir / "research.sqlite3", view=args.view,
                run_id=args.run_id, limit=args.limit,
            ),
            ensure_ascii=False, indent=2,
        ))
        return
    if args.live_action == "migrate-provenance":
        from .inspection import migrate_provenance

        print(json.dumps(migrate_provenance(state_dir), ensure_ascii=False, indent=2))
        return
    if args.live_action == "serve":
        if not args.allow_model_calls:
            raise ValueError("Live serving requires --allow-model-calls; no provider was activated.")
        if not (root / "ui/dist/index.html").is_file():
            raise ValueError("Build the React assets first: scripts/dev check.")
        from .engine import ResearchHost
        from .provider import GroqProvider
        from .server import LocalResearchServer

        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # A second host must not mistake another process's active calls for crashed work.
        lock = _open_host_lock(state_dir)
        host = ResearchHost(root, state_dir, GroqProvider(root), args.run_budget_usd)
        server = LocalResearchServer(host, root / "ui/dist", args.port)
        _write_connection(state_dir, {"origin": server.origin, "token": server.token,
                                      "launchUrl": server.launch_url, "pid": os.getpid()})
        print(f"Local live host: {server.origin}/live", flush=True)
        print("Open with: .venv/bin/ora live open (or add --browser). No model call occurs until submission.", flush=True)
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            print("Stopping the local host; accepted evidence remains saved.", flush=True)
        finally:
            server.server_close()
            host.close()
            lock.close()
        return
    connection = _connection(state_dir)
    if args.live_action == "open":
        # Validate the launcher URL too, rather than trusting an arbitrary local edit.
        expected = connection["origin"] + "/live#token=" + connection["token"]
        if connection.get("launchUrl") != expected:
            raise ValueError("Invalid local launch URL.")
        binary = root / "desktop/src-tauri/target/debug/bundle/macos/Research Atlas Sample.app/Contents/MacOS/research_atlas_prototype"
        if args.browser or not binary.is_file():
            webbrowser.open(expected)
            print("Opened the local live app in the browser.")
        else:
            # The native viewer needs its session URL, never the provider credential.
            viewer_env = {key: value for key, value in os.environ.items()
                          if key != "GROQ_API_KEY"}
            viewer_env["ORA_LIVE_URL"] = expected
            subprocess.Popen([str(binary)], env=viewer_env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            print("Opened the local live app in its Mac window.")
        return
    if args.calls and not args.run_id:
        raise ValueError("--calls requires --run-id.")
    path = "/api/live/calls" if args.calls else "/api/live/state"
    if args.run_id:
        path += "?" + urlencode({"runId": args.run_id})
    request = urllib.request.Request(connection["origin"] + path,
                                     headers={"Authorization": "Bearer " + connection["token"]})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            data = json.load(response)
    except (OSError, urllib.error.URLError):
        raise ValueError("The local live host is unavailable; retained runs are not lost.") from None
    print(json.dumps(data, ensure_ascii=False, indent=2))
