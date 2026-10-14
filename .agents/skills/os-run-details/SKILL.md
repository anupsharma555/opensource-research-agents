---
name: os-run-details
description: Explain an exact OpenSource Research Agents run or parent/follow-up sequence in chat from saved requests, model inputs, calls, tools, evidence, output, usage and state. Use when someone asks what happened in a run or wants its full result; this is a read-only report, not a new evaluation.
metadata:
  version: 0.1.1
---

# Explain a saved research run

Give the user one readable, end-to-end account of the **selected** run. Default to
read-only inspection. Do not start or resume a host, submit or retry work, alter
holds, change profiles, or invoke providers to fill a gap. Do not open Atlas
unless the user asks for interface evidence. This is a Codex development skill;
research agents do not load it.

First identify the exact state directory, run ID, parent/follow-up chain and
observation time. Establish whether records are live-provider, injected, replayed
or synthetic. Read current `AGENTS.md`, `README.md` and the relevant runtime
documentation before interpreting records. For the mission runtime, read
[mission record map](references/mission-records.md); use the selected runtime's
documented read-only path if its records differ. Do not broaden to other private
workspaces merely because they are available.

Make the report understandable without prior knowledge of the runtime. Define
unfamiliar terms briefly where they first appear, using a plain-language
explanation followed by the technical term. Explain distinctions that matter to
the selected run, such as a saved search result (search hit), a retained copy of
source material (source copy), a specific cited segment (passage/span), and records linking
evidence to its origin and use (provenance). Explain status labels by their actual
meaning; for example, host acceptance does not establish scientific correctness.
Avoid a separate exhaustive glossary or repeating definitions already given.

Reconstruct the sequence from the original request through accepted output:

1. **Input.** Show the saved user question and scope, supplied material or source
   identities, chosen agents, model/settings and credential *profile label*. For
   follow-ups, identify the exact parent snapshot and accepted target. Check the
   captured request for what evidence and task text actually reached each model
   call. A source in the library or a prepared intent alone does not prove delivery.
   Separate worker-facing task/evidence from controller conditions and withheld
   review. Summarize repeated system/developer scaffolding and tool definitions;
   never expose secrets or private instructions.
2. **Actions.** Walk through each meaningful model response, tool choice, saved
   tool result, local rejection/error, peer exchange and accepted artifact in time
   order. Group repetitive reads while preserving counts and decisive arguments,
   results and source references. Distinguish a controller stopping before a
   tool executes from a saved host/tool rejection; do not infer an external effect
   from a tool name alone. Say what the agent visibly chose and what that choice
   accomplished. Explain apparent rationale as an inference from its
   actions or final text, labeled as such; do not invent private reasoning or
   reveal hidden chain-of-thought, scratchpads or restricted provider reasoning.
   For each distinct requested tool, include its plain-language purpose, the kind
   of input it expects, and what it actually added in this run. Distinguish new
   source content from citation/provenance metadata, saving an output, and rereading
   information already in context. Explain whether it reads or writes local state
   or can contact an external service, using the actual handler and saved request
   rather than the tool name. When relevant, explain whether the step was needed,
   redundant, or the wrong operation, and whether direct reading or a direct final
   answer was supported. Do not assume a local tool execution adds another model
   API call. A compact **Tool / What it does / What happened here** table can keep
   these explanations within the action sequence without repeating every call.
3. **Output and support.** Put the actual accepted final answer in chat when the
   user asks for the full output; preserve its substantive wording and citations,
   with narrow redactions for credentials or unrelated private identifiers. If
   long, give the full requested text in parts rather than replacing it with an
   artifact link. Identify draft, direct model text, published report and accepted
   output separately. Check consequential claims against the exact permitted
   passages and counterevidence; label scientific assessment separately from
   execution status. A citation pointer alone is not support.
4. **Usage, timing and state.** Count every attempted provider request, including
   failures and uncertain dispatches, separately from captured responses and
   usage-bearing responses. Report provider-reported input, output and total
   tokens by call and total when available; reasoning and cached tokens are
   subsets, not additions. State unknown usage as unknown. Separate configured
   maxima, estimated usage and provider totals. Give saved start/end times,
   elapsed/wait intervals only where timestamps support them, current run/task
   state, continuing work, model/profile selections, and any observed hold or
   restoration. A completed report may coexist with continuing work.

Write the result in a concise sequence: **request and delivered context → agent
actions → final output → evidence assessment → usage, time and current state →
limits**. Adapt detail to the user's request; omit empty sections, not material
failures. Cite exact private run/call/source references or local receipt paths
sparingly so each consequential statement is auditable. Keep raw request packets,
source bodies, restricted reasoning and identifiers out of tracked files.

Use `$os-qualify-run` when the user also wants a deeper execution or scientific
qualification, and `$os-groq-usage` for broader account-period accounting. Read
those skills before applying them. Their helpers may write private receipts, so
do not run them merely to produce this read-only chat snapshot. Missing capture,
unsupported record shapes and unavailable host state remain explicit limits.
