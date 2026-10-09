/* Summarize saved execution evidence without treating configuration as execution.
   Exact inspection references determine which records belong to an attempt. */
import type { ResearchSnapshot, SavedRecordRef } from './generated/contracts';
import { RecordGroup, referenceKey } from './InspectionDetails';

type SavedRecord = NonNullable<ResearchSnapshot['inspection']>['records'][number];
type AttemptProvenance = NonNullable<ResearchSnapshot['inspection']>['attempts'][number];
type Data = Record<string, unknown>;

export type ReasoningAvailability = {
  request: boolean | null;
  requestBasis: 'saved_call' | 'saved_configuration' | 'unavailable' | 'conflict';
  outcome: 'returned' | 'restricted' | 'partial' | 'absent' | 'unknown';
  outcomeBasis: 'response_receipt' | 'attempt_field' | 'unavailable' | 'conflict';
};

function object(value: unknown): Data | null {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Data : null;
}

function savedString(value: unknown): string | null {
  return typeof value === 'string' && value.trim() ? value : null;
}

function strings(value: unknown): string[] | null {
  return Array.isArray(value) && value.every(item => typeof item === 'string') ? value : null;
}

function exactRef(value: unknown): SavedRecordRef | null {
  const item = object(value);
  return item && savedString(item.kind) && savedString(item.id) && Number.isInteger(item.version) && Number(item.version) > 0
    ? { kind: item.kind as string, id: item.id as string, version: item.version as number }
    : null;
}

function recordFor(snapshot: ResearchSnapshot, reference: SavedRecordRef | null | undefined): SavedRecord | null {
  return reference ? snapshot.inspection?.records.find(item => referenceKey(item.reference) === referenceKey(reference)) ?? null : null;
}

function uniqueRecords(records: SavedRecord[]): SavedRecord[] {
  return [...new Map(records.map(record => [referenceKey(record.reference), record])).values()];
}

function reasonFlag(data: Data | null): boolean | null {
  const settings = object(data?.reasoning_settings ?? data?.reasoningSettings);
  const flag = settings?.include_reasoning ?? settings?.includeReasoning;
  return typeof flag === 'boolean' ? flag : null;
}

function evidence(snapshot: ResearchSnapshot, attemptId: string) {
  const attempt = snapshot.attempts.find(item => item.id === attemptId);
  const provenance = snapshot.inspection?.attempts.find(item => item.attemptId === attemptId);
  const instance = provenance && attempt && provenance.instanceReference.id === attempt.agentId
    ? recordFor(snapshot, provenance.instanceReference) : null;
  const validInstance = instance && instance.reference.kind === 'agent_instance'
    && (instance.data.mission_id === undefined || instance.data.mission_id === snapshot.mission.id);
  const configuration = validInstance && provenance?.configurationReference
    ? recordFor(snapshot, provenance.configurationReference) : null;
  const validConfiguration = configuration?.reference.kind === 'agent_configuration'
    && configuration.data.instance_id === attempt?.agentId
    && (configuration.data.mission_id === undefined || configuration.data.mission_id === snapshot.mission.id)
    ? configuration : null;
  const calls = validConfiguration && attempt && provenance ? uniqueRecords((provenance.callReferences ?? []).flatMap(reference => {
    const record = recordFor(snapshot, reference);
    const data = record?.data;
    const manifestInputs = Array.isArray(data?.input_refs) ? data.input_refs.map(exactRef) : null;
    const inputsMatch = manifestInputs === null || (
      manifestInputs.every(item => item !== null)
      && manifestInputs.map(item => referenceKey(item!)).join('|') === (provenance.inputReferences ?? []).map(referenceKey).join('|')
    );
    return record?.reference.kind === 'agent_call'
      && data?.attempt_id === attempt.id
      && data?.task_id === attempt.taskId
      && data?.instance_id === attempt.agentId
      && data?.configuration_id === validConfiguration.reference.id
      && (data?.configuration_sha256 === undefined || data.configuration_sha256 === validConfiguration.data.configuration_sha256)
      && (data?.mission_id === undefined || data?.mission_id === snapshot.mission.id)
      && inputsMatch
      ? [record] : [];
  })) : [];
  const callIds = new Set(calls.map(call => savedString(call.data.call_id)).filter((id): id is string => !!id));
  const callRefs = new Set(calls.map(call => referenceKey(call.reference)));
  const records = snapshot.inspection?.records ?? [];
  const linkedToCall = (record: SavedRecord) => {
    const callRef = exactRef(record.data.call_ref);
    const configRef = exactRef(record.data.configuration_ref);
    return callRef !== null
      && callRefs.has(referenceKey(callRef))
      && record.data.attempt_id === attemptId
      && (record.data.mission_id === undefined || record.data.mission_id === snapshot.mission.id)
      && (record.data.task_id === undefined || record.data.task_id === attempt?.taskId)
      && (record.data.configuration_ref === undefined || (configRef !== null && validConfiguration !== null && referenceKey(configRef) === referenceKey(validConfiguration.reference)))
      && (record.data.configuration_id === undefined || record.data.configuration_id === validConfiguration?.reference.id)
      && (record.data.configuration_sha256 === undefined || record.data.configuration_sha256 === validConfiguration?.data.configuration_sha256);
  };
  const requestIntents = uniqueRecords(records.filter(record => record.reference.kind === 'model_request_intent'
    && record.data.attempt_id === attemptId
    && linkedToCall(record)));
  const intentsByRef = new Map(requestIntents.map(intent => [referenceKey(intent.reference), intent]));
  const linkedIntent = (record: SavedRecord) => {
    if (record.data.intent_ref === undefined) return true;
    const intentRef = exactRef(record.data.intent_ref);
    const intent = intentRef ? intentsByRef.get(referenceKey(intentRef)) : null;
    return !!intent && (record.data.request_sha256 === undefined || intent.data.request_sha256 === record.data.request_sha256);
  };
  const responseReceipts = uniqueRecords(records.filter(record => record.reference.kind === 'model_response_receipt'
    && linkedToCall(record)
    && linkedIntent(record)));
  const invocations = uniqueRecords(records.filter(record => ['capability_invocation', 'skill_invocation'].includes(record.reference.kind)
    && callIds.has(savedString(record.data.call_id) ?? '')
    && (record.data.mission_id === undefined || record.data.mission_id === snapshot.mission.id)));
  return { attempt, provenance, instance: validInstance ? instance : null, configuration: validConfiguration, calls, responseReceipts, requestIntents, invocations };
}

function distinctFlag(flags: Array<boolean | null>): { value: boolean | null; conflict: boolean } {
  const known = [...new Set(flags.filter((item): item is boolean => item !== null))];
  return { value: known.length === 1 ? known[0] : null, conflict: known.length > 1 };
}

function receiptOutcome(value: unknown): ReasoningAvailability['outcome'] {
  if (value === 'returned') return 'returned';
  if (value === 'restricted') return 'restricted';
  if (value === 'partial' || value === 'truncated') return 'partial';
  if (value === 'absent' || value === 'not_returned') return 'absent';
  return 'unknown';
}

export function getReasoningAvailability(snapshot: ResearchSnapshot, attemptId: string): ReasoningAvailability {
  const saved = evidence(snapshot, attemptId);
  const callFlags = saved.calls.map(call => reasonFlag(call.data));
  const callChoice = distinctFlag(callFlags);
  const configuredFlag = reasonFlag(saved.configuration?.data ?? null);
  const requestBasis = callChoice.conflict ? 'conflict'
    : callChoice.value !== null ? 'saved_call'
      : configuredFlag !== null ? 'saved_configuration' : 'unavailable';
  const request = callChoice.conflict ? null : callChoice.value ?? configuredFlag;
  const receiptOutcomes = [...new Set(saved.responseReceipts.map(record => receiptOutcome(record.data.reasoning_status)).filter(value => value !== 'unknown'))];
  if (receiptOutcomes.length > 1) return { request, requestBasis, outcome: 'unknown', outcomeBasis: 'conflict' };
  if (receiptOutcomes.length === 1) return { request, requestBasis, outcome: receiptOutcomes[0], outcomeBasis: 'response_receipt' };
  if (saved.responseReceipts.length) return { request, requestBasis, outcome: 'unknown', outcomeBasis: 'response_receipt' };
  const availability = saved.attempt?.returnedReasoning.availability;
  if (availability === 'returned' || availability === 'restricted' || availability === 'truncated' || availability === 'absent') {
    return { request, requestBasis, outcome: availability === 'truncated' ? 'partial' : availability, outcomeBasis: 'attempt_field' };
  }
  return { request, requestBasis, outcome: 'unknown', outcomeBasis: 'unavailable' };
}

function list(value: string[] | null, empty: string, missing = 'Not recorded') {
  return value === null ? missing : value.length ? value.join(', ') : empty;
}

function namedVersions(value: unknown): string[] | null {
  const entries = object(value);
  if (!entries) return null;
  return Object.entries(entries).map(([name, item]) => {
    const version = savedString(object(item)?.version ?? object(item)?.manifest_version);
    return version ? `${name}@${version}` : name;
  });
}

function savedOutcome(status: unknown) {
  const value = savedString(status);
  if (!value) return 'Outcome not recorded';
  return ['completed', 'succeeded', 'success', 'returned'].includes(value) ? `Saved success (${value})` : `Saved outcome (${value})`;
}

function metrics(value: unknown, fields: Array<[key: string, label: string, unit: string]>) {
  const data = object(value);
  if (!data) return 'Not included';
  const entries = fields.flatMap(([key, label, unit]) => {
    const measure = data[key];
    return typeof measure === 'number' && Number.isFinite(measure) && measure >= 0
      ? [`${label} ${measure}${unit}`] : [];
  });
  return entries.length ? entries.join(' · ') : Object.keys(data).length ? 'Other fields saved in exact receipt' : 'Not included';
}

const usageFields: Array<[string, string, string]> = [
  ['prompt_tokens', 'Prompt', ' tokens'],
  ['completion_tokens', 'Completion', ' tokens'],
  ['total_tokens', 'Total', ' tokens'],
  ['cached_tokens', 'Cached', ' tokens'],
];
const timingFields: Array<[string, string, string]> = [
  ['elapsed_seconds', 'Elapsed', ' s'],
  ['provider_queue_seconds', 'Provider queue', ' s'],
  ['queue_seconds', 'Queue', ' s'],
  ['prompt_seconds', 'Prompt', ' s'],
  ['completion_seconds', 'Completion', ' s'],
];

export function ExecutionDiagnostics({ snapshot, attemptId }: { snapshot: ResearchSnapshot; attemptId: string }) {
  const saved = evidence(snapshot, attemptId);
  const reasoning = getReasoningAvailability(snapshot, attemptId);
  if (!saved.attempt) return <section className="execution-diagnostics" aria-label="Execution diagnostics"><h4>Execution diagnostics</h4><p>Attempt {attemptId} is not in this snapshot.</p></section>;
  if (!saved.provenance) return <section className="execution-diagnostics" aria-label="Execution diagnostics"><h4>Execution diagnostics</h4><p>Versioned execution inspection was not included for this attempt.</p></section>;

  const config = saved.configuration?.data ?? null;
  const configuredTools = namedVersions(config?.permitted_tools);
  const configuredSkills = namedVersions(config?.skills);
  const exposedTools = [...new Set(saved.calls.flatMap(call => strings(call.data.exposed_tools) ?? []))];
  const loadedSkills = [...new Set(saved.calls.flatMap(call => strings(call.data.loaded_skill_ids) ?? []))];
  const exposedSkills = [...new Set(saved.calls.flatMap(call => strings(call.data.exposed_skill_ids) ?? []))];
  const configUnavailable = !!saved.provenance.configurationReference && !saved.configuration;
  const missingCalls = new Set((saved.provenance.callReferences ?? []).map(referenceKey)).size - saved.calls.length;

  return <section className="execution-diagnostics" aria-label="Execution diagnostics">
    <h4>Execution diagnostics</h4>
    <p>Saved configuration describes what was prepared. A call manifest describes what was exposed; only an invocation or response receipt establishes a recorded outcome.</p>
    {snapshot.inspection?.timingBasis === 'controlled_clock' && <p>Controlled fixture times are not measured provider latency.</p>}
    <dl className="detail-list">
      <div><dt>Attempt</dt><dd>{saved.attempt.id} · {saved.attempt.status}</dd></div>
      <div><dt>Attempt model field</dt><dd>{saved.attempt.selectedModel || 'Not recorded'}</dd></div>
      <div><dt>Configured provider and model</dt><dd>{saved.configuration ? `${savedString(config?.provider) ?? 'Provider not recorded'} · ${savedString(config?.model_id) ?? 'model not recorded'}` : 'Exact configuration unavailable'}</dd></div>
      <div><dt>Attempt skill versions</dt><dd>{list(saved.attempt.skillVersions, 'None recorded')}</dd></div>
      <div><dt>Configured skills</dt><dd>{saved.configuration ? list(configuredSkills, 'None configured') : 'Exact configuration unavailable'}</dd></div>
      <div><dt>Loaded skills in saved calls</dt><dd>{saved.calls.length ? list(loadedSkills, 'None recorded as loaded') : 'No verified call manifest'}</dd></div>
      <div><dt>Exposed skills in saved calls</dt><dd>{saved.calls.length ? list(exposedSkills, 'None recorded as exposed') : 'No verified call manifest'}</dd></div>
      <div><dt>Configured tools</dt><dd>{saved.configuration ? list(configuredTools, 'None configured') : 'Exact configuration unavailable'}</dd></div>
      <div><dt>Exposed tools in saved calls</dt><dd>{saved.calls.length ? list(exposedTools, 'None recorded as exposed') : 'No verified call manifest'}</dd></div>
      <div><dt>{reasoning.requestBasis === 'saved_configuration' ? 'Configured reasoning flag' : 'Reasoning request flag'}</dt><dd>{reasoning.request === null ? 'Unknown' : reasoning.request ? 'True' : 'False'} · {reasoning.requestBasis.replaceAll('_', ' ')}{reasoning.requestBasis === 'saved_configuration' ? '; request not verified' : ''}</dd></div>
      <div><dt>Reasoning outcome metadata</dt><dd>{reasoning.outcome} · {reasoning.outcomeBasis.replaceAll('_', ' ')}</dd></div>
    </dl>
    {!saved.instance && <p>Exact agent instance is unavailable or does not match this attempt.</p>}
    {configUnavailable && <p>Configuration reference is missing or belongs to another agent; no nearby version was substituted.</p>}
    {missingCalls > 0 && <p>{missingCalls} call reference(s) are unavailable or do not match this attempt and configuration.</p>}
    {(saved.provenance.callReferences ?? []).length === 0 && <p>No call manifest was included for this attempt. Prepared configuration does not establish a model invocation.</p>}
    <RecordGroup snapshot={snapshot} title="Exact agent instance" references={saved.instance ? [saved.instance.reference] : []} />
    <RecordGroup snapshot={snapshot} title="Exact configuration" references={saved.configuration ? [saved.configuration.reference] : []} />
    <RecordGroup snapshot={snapshot} title="Exact call manifests" references={saved.calls.map(call => call.reference)} />
    {saved.calls.map((call, index) => {
      const receipts = saved.responseReceipts.filter(record => referenceKey(exactRef(record.data.call_ref)!) === referenceKey(call.reference));
      return <section key={referenceKey(call.reference)} aria-label={`Saved call ${index + 1}`}>
        <h5>Saved call {index + 1} · {call.reference.id} · v{call.reference.version}</h5>
        <p>Call provider and model: {savedString(call.data.provider) ?? 'provider not recorded'} · {savedString(call.data.model_id) ?? 'model not recorded'}</p>
        <p>Exposed tools: {list(strings(call.data.exposed_tools), 'None recorded as exposed')}</p>
        {receipts.length ? receipts.map(receipt => <dl className="detail-list" key={referenceKey(receipt.reference)}>
          <div><dt>Model response</dt><dd>{savedOutcome(receipt.data.status)}</dd></div>
          <div><dt>Returned model</dt><dd>{savedString(receipt.data.returned_model) ?? 'Not recorded'}</dd></div>
          <div><dt>Usage</dt><dd>{metrics(receipt.data.usage, usageFields)}</dd></div>
          <div><dt>Timing</dt><dd>{metrics(receipt.data.timings, timingFields)}</dd></div>
        </dl>) : <p>No response receipt was included for this call; invocation and outcome are unverified.</p>}
      </section>;
    })}
    <h5>Recorded invocations</h5>
    {saved.invocations.length ? <ul>{saved.invocations.map(record => <li key={referenceKey(record.reference)}>{record.reference.kind === 'skill_invocation' ? 'Skill' : 'Tool'} {savedString(record.data.skill_id ?? record.data.tool_id) ?? record.reference.id}{savedString(record.data.skill_version ?? record.data.manifest_version) ? `@${savedString(record.data.skill_version ?? record.data.manifest_version)}` : ''} · {savedOutcome(record.data.status)}</li>)}</ul> : <p>No linked skill or tool invocation receipt was included. Exposed capability does not establish use or success.</p>}
    <RecordGroup snapshot={snapshot} title="Exact invocation receipts" references={saved.invocations.map(record => record.reference)} />
    <RecordGroup snapshot={snapshot} title="Exact request intents" references={saved.requestIntents.map(record => record.reference)} />
    <RecordGroup snapshot={snapshot} title="Exact response receipts" references={saved.responseReceipts.map(record => record.reference)} />
    <RecordGroup snapshot={snapshot} title="Exact saved inputs" references={saved.provenance.inputReferences ?? []} />
    <RecordGroup snapshot={snapshot} title="Exact accepted outputs" references={saved.provenance.acceptedOutputReferences ?? []} />
  </section>;
}
