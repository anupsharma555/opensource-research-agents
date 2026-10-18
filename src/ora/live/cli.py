"""Start the explicit local live host or inspect it without creating research work.

The launch record stays in private local state and supplies a browser session or
native window connection. Provider credentials remain in the Python host; opening
or inspecting the app never submits a model request.
"""

import hashlib
import json
import os
import re
import subprocess
import tempfile
import urllib.error
import urllib.request
import webbrowser
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlencode, urlsplit

from ora.contracts.models import LiveFollowupRequest, LiveStartRequest
from ora.credentials import GROQ_CREDENTIAL_ENV_NAMES

CONNECT_STATE_TIMEOUT_SECONDS = 45


def configure(parser):
    actions = parser.add_subparsers(dest="live_action", required=True)
    for action in ("serve", "open", "connect", "status", "provenance", "migrate-provenance"):
        command = actions.add_parser(action)
        command.add_argument("--state-dir", type=Path, default=Path(".local/live"))
        if action == "serve":
            command.add_argument("--allow-model-calls", action="store_true")
            command.add_argument("--port", type=int, default=0)
            command.add_argument("--runtime", choices=("mission", "legacy"), default="mission",
                                 help="Shared Stage 4 service, or the prior supplied-evidence host")
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


def configure_mission(parser):
    """Expose the same durable commands used by Atlas to terminal callers."""
    actions = parser.add_subparsers(dest="mission_action", required=True)
    for action in ("start", "followup", "status", "calls"):
        command = actions.add_parser(action)
        command.add_argument("--state-dir", type=Path, default=Path(".local/live"))
        if action in {"start", "followup"}:
            command.add_argument("--input", type=Path, required=True,
                                 help="Saved JSON command with a stable requestId")
        else:
            command.add_argument("--run-id", required=action == "calls")


def mission_main(args, root):
    """Return a receipt without keeping the submitting terminal in charge of work."""
    if args.mission_action in {"status", "calls"}:
        args.live_action = "status"
        args.calls = args.mission_action == "calls"
        return main(args, root)
    state_dir = args.state_dir.resolve()
    connection = _connection(state_dir)
    model = LiveStartRequest if args.mission_action == "start" else LiveFollowupRequest
    try:
        command = model.model_validate_json(args.input.read_text(encoding="utf-8"))
    except ValueError:
        raise ValueError("Invalid mission command; check the saved JSON fields and requestId.") from None
    path = "/api/live/start" if args.mission_action == "start" else "/api/live/followups"
    request = urllib.request.Request(
        connection["origin"] + path,
        data=command.model_dump_json(by_alias=True).encode(),
        headers={"Authorization": "Bearer " + connection["token"],
                 "Content-Type": "application/json"},
    )
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=15) as response:
            receipt = json.load(response)
    except urllib.error.HTTPError as error:
        # The server may have rejected a field or an exact-version conflict. Keep
        # the user's original file for correction or an identical idempotent retry.
        try:
            message = json.loads(error.read()).get("error", "Command was not accepted.")
        except (ValueError, AttributeError):
            message = "Command was not accepted."
        raise ValueError(f"Local service returned {error.code}: {message}") from None
    except (OSError, ValueError, urllib.error.URLError):
        raise ValueError(
            "No acceptance receipt was received. Inspect status, then retry the same "
            "saved command/requestId; do not create a second request identity."
        ) from None
    print(json.dumps(receipt, ensure_ascii=False, indent=2))


def _connection(state_dir):
    try:
        data = json.loads((state_dir / "connection.json").read_text())
    except (OSError, ValueError):
        raise ValueError("Start the local server before opening or inspecting a live run.") from None
    if not isinstance(data, dict):
        raise ValueError("Invalid local server connection record.")
    try:
        url = urlsplit(data.get("origin", ""))
        valid_origin = (
            url.scheme == "http" and url.hostname == "127.0.0.1"
            and url.port and url.netloc == f"127.0.0.1:{url.port}"
            and not (url.path or url.query or url.fragment)
        )
    except (TypeError, ValueError):
        valid_origin = False
    token = data.get("token")
    if not valid_origin or not isinstance(token, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", token):
        raise ValueError("Invalid local server connection record.")
    return data


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    """Keep local session credentials on the exact selected loopback host."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _request_state(connection, path="/api/live/state", *, timeout=10):
    request = urllib.request.Request(
        connection["origin"] + path,
        headers={"Authorization": "Bearer " + connection["token"]},
    )
    try:
        with urllib.request.build_opener(_NoRedirect).open(request, timeout=timeout) as response:
            data = json.load(response)
    except (OSError, ValueError, urllib.error.URLError):
        raise ValueError("The selected local host is unavailable; retained runs are not lost.") from None
    return data


def connect_url(state_dir):
    """Validate the saved host privately; this does not submit or resume research."""
    connection = _connection(state_dir)
    expected = connection["origin"] + "/live#token=" + connection["token"]
    if connection.get("launchUrl") != expected:
        raise ValueError("Invalid local launch URL.")
    # Reconnection currently verifies the full saved state, which can take more
    # than the ordinary ten-second inspection allowance. This bounded wait keeps
    # identity validation intact; it does not fix the synchronous native menu or
    # the cost of reconstructing a large snapshot.
    state = _request_state(connection, timeout=CONNECT_STATE_TIMEOUT_SECONDS)
    if not isinstance(state, dict) or not all(key in state for key in ("enabled", "agents", "runs")):
        raise ValueError("The selected local host did not return a research workspace.")
    # Historical hosts have no workspace ID. Their saved token still authenticates
    # this connection, but cannot prove a workspace identity they never recorded.
    recorded_id = connection.get("workspaceId")
    if recorded_id is not None and state.get("workspaceId") != recorded_id:
        raise ValueError("The connected host does not match the selected saved workspace.")
    return expected


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


def _record_host_build(root, state_dir):
    """Identify the loaded checkout for local run qualification without publishing it."""
    files = sorted((root / "src/ora").rglob("*.py")) + [root / "scripts/dev"]
    manifest = {
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        "source_sha256": {str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
                          for path in files if path.is_file()},
        "proof": "source_files_at_host_start; not a release or runtime-success claim",
    }
    digest = hashlib.sha256(json.dumps(manifest, sort_keys=True).encode()).hexdigest()
    folder = state_dir / "host-builds"
    folder.mkdir(exist_ok=True, mode=0o700)
    with os.fdopen(os.open(folder / f"{digest}.json", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600), "w") as handle:
        json.dump(manifest, handle, indent=2)
    return digest


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
        from .server import LocalResearchServer

        state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
        # A second host must not mistake another process's active calls for crashed work.
        lock = _open_host_lock(state_dir)
        try:
            host = _create_host(root, state_dir, args)
        except BaseException:
            lock.close()
            raise
        server = LocalResearchServer(host, root / "ui/dist", args.port)
        build_id = _record_host_build(root, state_dir)
        _write_connection(state_dir, {"origin": server.origin, "token": server.token,
                                      "launchUrl": server.launch_url, "pid": os.getpid(),
                                      "workspaceId": host.state().workspace_id,
                                      "hostBuildId": build_id})
        print(f"Local live host: {server.origin}/live", flush=True)
        print("Open with: scripts/dev live open (or add --browser). Opening the viewer does not submit work; saved pending missions can resume on this host.", flush=True)
        try:
            server.serve_forever(poll_interval=0.2)
        except KeyboardInterrupt:
            print("Stopping the local host; accepted evidence remains saved.", flush=True)
        finally:
            server.server_close()
            host.close()
            lock.close()
        return
    if args.live_action == "connect":
        # Consumed privately by the native viewer; never log this session URL.
        print(connect_url(state_dir))
        return
    if args.live_action == "open":
        expected = connect_url(state_dir)
        binary = root / "desktop/src-tauri/target/debug/bundle/macos/Research Atlas Sample.app/Contents/MacOS/research_atlas_prototype"
        if args.browser or not binary.is_file():
            webbrowser.open(expected)
            print("Opened the local live app in the browser.")
        else:
            # The native viewer needs its session URL, never the provider credential.
            viewer_env = {key: value for key, value in os.environ.items()
                          if key not in GROQ_CREDENTIAL_ENV_NAMES.values()}
            viewer_env["ORA_LIVE_URL"] = expected
            viewer_env["ORA_LIVE_STATE_DIR"] = str(state_dir)
            subprocess.Popen([str(binary)], env=viewer_env,
                             stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True)
            print("Opened the local live app in its Mac window.")
        return
    connection = _connection(state_dir)
    if args.calls and not args.run_id:
        raise ValueError("--calls requires --run-id.")
    path = "/api/live/calls" if args.calls else "/api/live/state"
    if args.run_id:
        path += "?" + urlencode({"runId": args.run_id})
    data = _request_state(connection, path)
    print(json.dumps(data, ensure_ascii=False, indent=2))


def _create_host(root, state_dir, args):
    """Select one state owner explicitly; preserve historical host databases."""
    if getattr(args, "runtime", "mission") == "legacy":
        from .engine import ResearchHost
        from .provider import GroqProvider

        return ResearchHost(root, state_dir, GroqProvider(root), args.run_budget_usd)
    if (state_dir / "research.sqlite3").exists() and not (state_dir / "state.sqlite3").exists():
        raise ValueError(
            "This directory contains legacy supplied-evidence runs. Use --runtime legacy "
            "to inspect that host, or select a new --state-dir for the shared research service. "
            "Historical runs were not migrated or rewritten."
        )
    from ora.mission import MissionService
    from ora.observability.config import runtime_export_config

    return MissionService(root, state_dir, trace_config=runtime_export_config(root))
