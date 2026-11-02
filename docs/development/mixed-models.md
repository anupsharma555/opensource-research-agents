# Pinned agent models and quota lanes

New mission starts can assign a model to each configured agent alias through
the optional `agentModels` field. An omitted alias retains the GPT-OSS default;
the host saves resolved choices for the entire alias catalog, including peers
that may be invited later. For example:

```json
{
  "agentModels": {
    "agent-synthesis": {"model": "openai/gpt-oss-120b"},
    "agent-critique": {
      "model": "qwen/qwen3.8-27b",
      "reasoningEffort": "medium",
      "maxCompletionTokens": 512
    }
  }
}
```

This is an optional field in the normal start request, not a complete request.
The Atlas form does not yet provide a model picker. The headless validation
runner accepts the corresponding `agent_models` start field in its private case.

`ora.mission.model_bindings.resolve_agent_model` is the lookup authority. New
runs save `agent_model_binding_version=1` and `profile.agentModelBindings`.
Configuration, display and later invitations use the saved alias choice.
Follow-ups inherit the full map. An explicitly selected follow-up credential
can change the credential binding, but not the inherited model, reasoning effort
or output cap. Reopening a run does not apply new defaults to its saved peers.
Runs without the version retain their historical run-level configuration.

The explicit new-run Qwen default is medium reasoning, parsed reasoning output
and a 512-token completion cap. GPT-OSS keeps its existing default. A caller may
select a higher model-supported cap; the host does not silently lower it after a
provider refusal. A small default is not a qualification of long scientific
answers. Token estimation and context packing remain separate requirements.

New-run Qwen choices also accept `reasoningEffort: "low"`; GPT-OSS choices
remain limited to medium or high. Python validation and the generated JSON
Schema enforce that model-specific distinction. A different effort is a new
condition, not a change applied when reopening an existing run.

Low and medium use separate saved local token-estimate bases with the same
hash-pinned Qwen tokenizer and template. The low template adds its own reasoning
directive; historical medium counts replay unchanged. These are admission
estimates, not Groq's private serialization or evidence that low effort improves
answer quality. Unsupported settings retain an explicit conservative fallback.

## Scheduling and accounting

Credentials identify a route; quota lanes identify the configured organization
domain and model. New runs use `model_quota_scope(domain, model)` while historical
work retains its saved scope. Qwen text and auxiliary figure calls use the same
Qwen lane. Two keys do not establish two independent organizations.

New model lanes retain applicable legacy debt. An exact saved model determines
which lane receives an older charge; debt with an unprovable model is retained
conservatively. Existing operations and receipts are not rewritten to erase
reservations or usage.

Request and combined-token debt follows saved credential-to-model migration
chains in either order. A new credential cutover freezes the existing operation
and quota-head references of its old model lanes; later observations from those
operations remain applicable, while new post-cutover operations are excluded.
Repeated references are counted once. Older cutovers without this snapshot
require explicit reconciliation while relevant debt or uncertain headroom
remains; a queued operation with no provider effect is not debt. Use the updated
reader with these records: older runtimes are not qualified for the added
snapshot metadata. Inheritance of output-only quota observations across these
cutovers is not established by this migration test.

Output tokens per minute are tracked separately from total/input rate hints.
An exact provider OTPM error can create a numeric-only observation tied to its
attempt. It records the limit and requested amount without copying account
identifiers into ordinary receipts. New work reserves its complete selected
output allowance, then substitutes reported completion usage after settlement.
Missing usage retains the reservation. The observations belong to the durable
state workspace; the live qualification files are not automatically imported
as current account balances.

If the selected output cap exceeds the observed full output window, the request
waits with an explanation that time alone cannot make it fit. Smaller eligible
requests can proceed; the oversized request does not impose a model-wide retry
block. A temporary shortage within a window receives a saved wake time instead.
No quota condition automatically switches an agent's model or credential.

Existing asynchronous executor callbacks permit independent peers to overlap.
An explicit peer wait does not poll with model calls: saved question, answer and
review events allow the host to prepare the next relevant input. These mechanics
do not establish that a model will answer or review correctly.

## Verification boundary

The focused gates include `tests/mission/test_model_bindings.py`,
`tests/mission/test_mixed_model_scheduling.py`, the model quota/output tests in
`tests/coordination/`, and session/figure receipt tests. They cover exact request
selection, pinned follow-ups, asynchronous injected callbacks, known and unknown
usage, restart and rejected output allowances. Injected callbacks establish host
mechanics, not natural multi-agent scientific behavior. Live mixed-model peer
qualification requires its own scoped allowance and saved evidence.
