# Run and inspect a local research workspace

The Stage 4 host connects the shared state store, peer registry, executor,
model session and retrieval tools. It is an explicitly launched local service.
Offline checks do not start it or activate providers. Live qualification is
ongoing: a model response or a completed status alone does not establish that
the answer is supported by its sources.

## Launch the host and viewer

Use the [shared setup](setup.md) and build the frontend with `scripts/dev check`.
For a native Mac window, also build `scripts/dev desktop`. Supply `GROQ_API_KEY`
through the environment or the untracked project `.env`; do not place keys in
requests, screenshots or saved reports. Exa is available only with its separately
configured credential. Public scholarly routes do not require a model credential
for their own HTTP requests.

```sh
scripts/dev live serve --runtime mission --allow-model-calls --state-dir .local/research
```

Keep this host process running. Starting it also reconstructs unfinished eligible
work already saved in that directory. In a second terminal:

```sh
scripts/dev live open --state-dir .local/research
scripts/dev mission status --state-dir .local/research
```

Add `--browser` to `live open` for browser inspection. The native app remembers
the selected state directory, validates the corresponding local connection and
keeps drafts independently of research execution. Connection credentials remain
private. A disconnected or recorded view is not evidence that a mission stopped.
Closing the viewer does not stop the separately running host.

The native shell saves an unsent Start/Follow-up draft only for the selected
workspace after its local connection has been authenticated. The remote live
page can invoke the two draft commands only at the verified loopback origin;
recorded and other local pages cannot read those files. Drafts retain the
question, supplied evidence, peer choices, exact follow-up parent/target and
any frozen pending request ID. If a native draft cannot be read or parsed,
Atlas leaves it untouched and offers **Retry loading draft** before the form
can submit or save. If a later draft write fails, Atlas keeps the visible form
in memory and warns that it was not saved locally. Before a native submission,
Atlas saves that frozen request ID; a failed draft write leaves the request
unsent so a retry keeps the same identity.

If the host restarts on a new local port, choose **File → Reconnect saved
workspace** in the Mac app. The native shell validates the remembered workspace's
current connection, then opens Saved runs with the selected run retained. This
does not start another research task. Refresh reloads the existing address; it
cannot restore a stopped host or discover its replacement port.

The default selected model is Groq `openai/gpt-oss-120b` with high reasoning.
The mission runtime does not impose a monetary research budget or a fixed number
of scientific turns. Actual provider rate limits can delay a call; the saved
wait and next attempt remain inspectable. Model, source-use and capability
settings are saved for each attempt. There is no implicit provider/account
fallback.

### Explicit primary and backup credentials

New mission and follow-up requests can select `credentialProfile` as `primary`
or `backup`. The primary profile resolves `GROQ_API_KEY`; the backup resolves
`GROQ_API_KEY_BACKUP`, from the process environment or the untracked project
`.env`. Missing or invalid selected credentials produce a local failure; the
adapter never silently tries the other key. An omitted profile selects primary
for a new mission and inherits the parent's selection for a follow-up. Existing
saved calls without a profile retain their primary-key interpretation.

Each new selection pins a nonsecret profile and quota domain in the run's model
settings, configuration, request intent and dispatch record. A quota domain is
the set of calls sharing a provider limit. Both profiles share `groq-selected`
by default; separate keys or email addresses do not prove independent quotas.
After independently verifying the provider's organization relationship, an
operator can save this local-only configuration to explicitly separate a domain:

```json
{
  "version": 1,
  "profiles": {
    "backup": {
      "quota_domain": "groq-secondary",
      "quota_domain_verified": true
    }
  }
}
```

The configuration path is `.local/groq-credential-profiles.json`. It contains
labels only, never keys, emails or account identifiers. Profiles in the same
organization must use the same domain label. The host reads this mapping on
startup. When Primary keeps the historical domain and Backup has an explicitly
verified new domain, startup can add that one domain to an existing default
capacity policy. It preserves the old records and carries forward outstanding
charges, reservations, waits and uncertain outcomes. Later Primary activity does
not become new Backup debt. Other capacity-policy changes require explicit
reconciliation; this path does not migrate a custom policy.

A mapping change does not rewrite old attempts, reset their ledger or move an
in-flight call to another account. Historical Backup runs whose saved domain no
longer matches are held with an explanation. Select a profile on a new mission
or exact follow-up to create a traceable run with the current mapping; old work
and its provider constraints remain preserved. An explicit hold still applies
to new work, and this feature does not activate Backup in a running host.

The transition has offline migration, restart and concurrent-account regression
coverage. That coverage does not establish current provider allowance or prove
that a particular live workspace has adopted the mapping.

To hold calls on a selected key while retaining its unfinished runs, add
`"enabled": false` to that profile's entry and restart the host at a known
local boundary. An entry containing only `enabled` is valid and does not alter
its quota domain. The host leaves affected work waiting with an operator-pause
explanation. Current executor admission is per mission, so that mission's queued
local tools also wait; work is preserved, not cancelled. Missions using other
enabled profiles may proceed when their capacity allows.
Re-enabling the same profile at a later restart resumes its saved configuration.
This is an explicit operator control, never an automatic fallback or a new
research lifetime limit. A primary hold does not establish independent backup
quota; the existing shared provider wait still applies until a separate domain
has actually been verified.

For a selective restart, an enabled profile may also contain
`"allowed_run_ids": ["run-example"]`. Only those exact runs can advance in the
mission host; other runs retain their saved phase and work with a clear operator
pause. An empty list holds every run on that profile. Omitting the field preserves
the usual behavior of resuming all runs on an enabled profile. A disabled profile
stays disabled even when a run is listed. This startup-only operator selection
does not change pinned credentials, quotas, model settings or scientific scope.
To qualify one new follow-up without waking historical work, prepare its stable
request ID and resulting run ID, save that run selection, restart at a verified
idle boundary, then submit the exact request. Changing the file alone does not
alter an already running host. This selection applies to the mission host, not
standalone model probes or the legacy supplied-evidence runtime.

### Project model choices

| Work | Selected model |
| --- | --- |
| Reasoning, function/tool use and text-to-text research | Groq `openai/gpt-oss-120b`, with high reasoning |
| Vision when image interpretation is useful, including figures and scanned pages | Groq `qwen/qwen3.8-27b` |

These are project preferences, not a comparative benchmark result. Groq documents
Qwen's image/text inputs and text output in its [vision guide](https://console.groq.com/docs/vision)
and [model card](https://console.groq.com/docs/model/qwen/qwen3.8-27b)
(checked 10 October 2026). The current selected-model adapter implements GPT-OSS;
the Qwen vision path still needs explicit integration and qualification. Preserve
the image/source identity and selected model in each vision call so its output
can be checked against the original figure or page and reused by research peers.

## Start and follow up

In Atlas, select **Start or follow up**, enter the research question and scope,
and choose starting peers from the enabled role registry. Pasted evidence is
optional in the mission runtime. Agents choose useful queries and actions;
the host records their work and manages execution resources.

Starting peers are independent contributors. For new missions, requiring a
particular peer's contribution is a separate, optional selection. An agent can
propose that the requested scope is complete while optional work continues;
Atlas shows that continuing work separately. Existing saved missions retain
their original participation rules. Delivery, acknowledgment, an answer and
the requesting peer's review are distinct records, so a delivered message is
never presented as scientific agreement.

The same command is available without the UI. Save a private JSON file containing
`requestId`, `question`, `scope` and `agentIds` taken from the current roster,
then submit it with:

```sh
scripts/dev mission start --state-dir .local/research --input /path/to/private-request.json
```

The command returns a saved acceptance receipt and exits while the host continues.
Retry the same unchanged request identity if receipt delivery was interrupted;
do not generate a new request ID merely to retry transport. A materially changed
request with the same ID returns a conflict.

A follow-up identifies the exact parent run, saved snapshot and target version.
Atlas fills these references from the selected work. Headless callers use
`mission followup --input /path/to/private-followup.json` with that same contract.
Earlier reports and source copies remain available for comparison.

## Review actual work

**Research review** contains the question, contributions and saved findings.
**Communication** separates directed requests/replies from work visible on the
shared board. **Evidence** exposes inspected copies and passages. **Activity**
and **Details** identify actual attempts, calls, tool results and waits.
**Saved runs** provides dates, filters and parent/follow-up relationships.

For matching records in Codex or a terminal:

```sh
scripts/dev mission status --state-dir .local/research --run-id RUN_ID
scripts/dev mission calls --state-dir .local/research --run-id RUN_ID
```

Reading these views does not dispatch research. Provider-returned reasoning,
when actually captured, stays in restricted diagnostic records. Reader-facing
explanations describe observable decisions and evidence; they do not invent
private reasoning. Keep raw call/source exports private until reviewed.

## Qualify changes

`benchmarks/stage4/cases.json` defines real-publication questions from very simple
to complex, with meaningful variations. The explicit qualification runner sends
the same HTTP commands as Atlas and saves private inputs, source-linked snapshots,
call records and observations under `.local/stage4/mission-qualifications/`.

```sh
.venv/bin/python scripts/qualify_stage4_mission.py
.venv/bin/python scripts/qualify_stage4_mission.py --state-dir .local/research \
  --case S4-VS-01 --allow-live
```

The observation window does not cancel or complete a mission. The runner records
active work and ongoing consumption in `qualification-status.json`. By default,
it exits with code 2 before adding tests when existing work remains active, or
when the preceding test is still running after observation. Use `--allow-overlap`
for an intentional concurrent test batch; this affects only test submissions,
not the agents' lifetime or scientific choices. Repetitions and longer cases
are explicit options. A reviewer must compare the output with the
exact retrieved sources, assess whether collaboration added value, and check
the same facts in the browser and native app. Retain failures and recovery
attempts; do not turn fixture success into a live or scientific claim.

Choose the next test from the preceding diagnosis. Reuse an exact input to
verify a specific repair, vary one meaningful input to check generalization,
then advance to an integrated workflow that exercises a new project capability.
For example, a corrected abstract lookup can be followed by a different trial,
then a two-trial comparison with useful peer review. Predictable faults belong
in offline tests; live runs resolve model/provider behavior that those tests
cannot establish. Do not add repetitions merely to increase the run count.

Before submission, record the hypothesis, linked acceptance criteria, expected
facts or behavior, input/source versions and comparison conditions. Afterward,
use `os-qualify-run` to assess scientific support and useful completion alongside
calls, context, tokens, waits, storage and human inspection. A run resumed after
an implementation repair demonstrates assisted recovery, not an independent
unchanged-condition comparison. Later tests should cover evidence synthesis,
research-question development and study planning without claiming that a shared
runtime test alone qualifies those scientific roles.

The connection's `hostBuildId` points to a private file-hash manifest in
`host-builds/`, allowing a run to identify its host code even before a commit.
The run configuration separately identifies model, tools, instructions and
skills. Ten-hour endurance, remote deployment and unattended production
supervision require their own later evidence.
