# Local mission-host liveness journal

Live `MissionService` hosts write a private, per-process session journal at
`<state_dir>/host-liveness/host-<random-id>.jsonl`. It starts even when a caller
drives the host manually with `start_supervisor=False`. Offline test hosts do not
start the monitor. The directory is mode `0700`, each session file is mode `0600`,
and the journal is separate from `StateStore`: sampling does not create research
records, wake the Atlas invalidation stream, or alter scheduling.

One `started` record is written at initialization, a daemon thread writes a
`heartbeat` every 30 seconds, and `stopped` is written after the existing
dispatch shutdown attempt. The `_drive_run` wrapper updates in-memory counters
and timestamps; it does not write on every drive. A heartbeat contains only the
host session ID, PID, drive mode (`background` or `manual`), UTC sample time, completed/failed/active
drive counts, up to eight active run IDs, oldest active drive duration, last run
ID, separate IDs for the last started and completed drives, last drive
start/completion/duration, whether that completed drive reported progress,
its returned next wake, and an exception **class** if it raised. It contains no
prompts, source text, model output, credentials, URLs, or exception messages.
`HostLiveness.status()` exposes local-only file/write error codes for diagnosis;
monitor write failure does not stop research work.
Sampling is bounded to one line per interval per host session; drive calls only
take a short in-memory lock. The journal is local diagnostic data, not a durable
research transaction or a reason to replay an uncertain provider effect.

## Reading a long gap

- Heartbeats continuing with `active_drive_count > 0` show that the process was
  alive while a driver call remained inside `_drive_run`.
- Heartbeats continuing with no active drive and a past `last_next_wake_utc`
  show an observed process that was not in that drive at those sample times.
  This is a lead for controller scheduling, not proof that the same run was
  continuously eligible: another run, an operator hold, or a changed dependency
  may intervene.
- A missing heartbeat is **unknown**, not proof the host was off. The process
  may have stopped, the machine may have slept, the monitor may have failed, or
  the filesystem may not have persisted a sample. A new session ID on restart
  separates old and new process observations. A missing `stopped` record is not
  by itself evidence of a crash.

Join the journal with saved `capacity_operation` wake/phase records,
`model_dispatch_attempt` markers, `model_response_receipt` timestamps,
`mission_task_control` state, and the controller's own observation window before
attributing a delay. The journal does not measure provider generation, tool
execution, peer review, or browser visibility. It also does not establish a
global uptime guarantee; the project currently has no production mission host.

## Verification

`tests/mission/test_host_liveness.py` uses an injected clock to distinguish a
long active drive from a living idle host, checks separate restart sessions and
private permissions, confirms that heartbeats do not modify SQLite, and proves
that the service wrapper preserves return values and exceptions without writing
research text. A short real-thread check confirms that heartbeats continue
while a drive is active. No provider call is made.
