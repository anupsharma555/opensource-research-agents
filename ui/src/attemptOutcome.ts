/* Describe one saved attempt from its exact call and response records. A tool
   step, provider rejection, and accepted research report are different facts. */
import type { ResearchSnapshot, SavedRecordRef } from './generated/contracts';

type Attempt = ResearchSnapshot['attempts'][number];
type ToolCall = Attempt['toolCalls'][number];
type SavedData = Record<string, unknown>;

const TOOL_ACTIONS: Record<string, string> = {
  discover_capabilities: 'See available tools',
  search_scholarly: 'Scholarly source search',
  peer_request: 'Request peer assistance',
  inspect_hit: 'Inspect a saved search result',
  read_source_window: 'Read a saved source excerpt',
  read_source_inventory: 'List saved sources',
  select_working_sources: 'Choose sources for the next step',
  fetch_source: 'Retrieve a selected source',
  publish_result: 'Submit a research contribution',
  read_context_page: 'List additional saved task information',
  read_context_record: 'Open an additional saved task record',
  list_venues: 'Browse suggested places to search',
  propose_venue: 'Suggest a place to search',
  navigate_venue: 'Inspect a suggested search location',
};

export const toolActionLabel = (name: string): string => TOOL_ACTIONS[name] ?? name;

export function recordedToolAction(tool: ToolCall): string {
  const status = tool.status === 'waiting' ? 'requested; result not saved'
    : tool.status === 'result_unaccepted' ? 'result saved; task did not accept'
    : tool.status === 'acceptance_recorded' ? 'local recovery accepted; continuation pending'
    : tool.status === 'metadata_only' ? 'metadata only'
      : tool.status.replaceAll('_', ' ');
  return `${toolActionLabel(tool.name)} (${status})`;
}

const object = (value: unknown): SavedData | null => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as SavedData : null;
const savedText = (value: unknown) => typeof value === 'string' && value.trim() ? value : null;
const exactRef = (value: unknown): SavedRecordRef | null => {
  const candidate = object(value);
  return candidate && typeof candidate.kind === 'string' && typeof candidate.id === 'string'
    && Number.isInteger(candidate.version) && Number(candidate.version) > 0
    ? candidate as unknown as SavedRecordRef : null;
};
const refKey = (ref: SavedRecordRef) => `${ref.kind}:${ref.id}:${ref.version}`;

export type AttemptOutcome = {
  label: string;
  badgeStatus: string;
  summary: string;
  explanation: string;
  responseStatus: string | null;
  httpStatus: number | null;
  retryAfterSeconds: number | null;
  responseAt: string | null;
  configuredProvider: string | null;
};

export function showsOriginalRunningRecord(snapshot: ResearchSnapshot, attempt: Attempt, outcome: AttemptOutcome | null): boolean {
  const explanation = snapshot.inspection?.attempts.find(item => item.attemptId === attempt.id)?.explanation;
  return (attempt.status === 'waiting' && outcome?.label === 'Context preparation needs local repair')
    || (attempt.status === 'waiting' && outcome?.label === 'Model call paused by operator')
    || (attempt.status === 'superseded' && outcome?.label === 'Local context preparation failed')
    || (attempt.status === 'completed' && Boolean(explanation?.startsWith('An accepted report names this attempt. Its original status and unknown completion time remain')))
    || (attempt.status === 'superseded' && Boolean(explanation?.startsWith('No current task control names this call. Its original running status')));
}

export function describeLiveAttempt(snapshot: ResearchSnapshot, attempt: Attempt): AttemptOutcome | null {
  if (!snapshot.proofKind?.startsWith('live_') || !snapshot.inspection?.attempts.some(item => item.attemptId === attempt.id)) return null;
  return describeAttempt(snapshot, attempt);
}

export function describeAttempt(snapshot: ResearchSnapshot, attempt: Attempt): AttemptOutcome {
  const provenance = snapshot.inspection?.attempts.find(item => item.attemptId === attempt.id);
  const callKeys = new Set((provenance?.callReferences ?? []).map(refKey));
  const responses = snapshot.inspection?.records.filter(item => {
    const call = exactRef(item.data.call_ref);
    return item.reference.kind === 'model_response_receipt' && call && callKeys.has(refKey(call))
      && item.data.attempt_id === attempt.id;
  }) ?? [];
  const receipt = responses.length === 1 ? responses[0].data : null;
  const configured = provenance?.configurationReference
    ? snapshot.inspection?.records.find(item => refKey(item.reference) === refKey(provenance.configurationReference!))?.data
    : null;
  const configuredProvider = savedText(configured?.provider);
  const responseStatus = savedText(receipt?.status);
  const errorKind = savedText(receipt?.error_kind);
  const httpStatus = typeof receipt?.http_status === 'number' && Number.isInteger(receipt.http_status) ? receipt.http_status : null;
  const retryAfterSeconds = typeof receipt?.retry_after_seconds === 'number' && Number.isFinite(receipt.retry_after_seconds) && receipt.retry_after_seconds >= 0 ? receipt.retry_after_seconds : null;
  const responseAt = savedText(receipt?.recordedAt);
  const accepted = provenance?.acceptedOutputReferences?.some(ref => ref.kind === 'report'
    && snapshot.inspection?.acceptedOutputReferences.some(saved => saved.kind === ref.kind && saved.id === ref.id && saved.version === ref.version)
    && snapshot.artifacts.filter(artifact => artifact.id === ref.id && artifact.version === ref.version).length === 1) ?? false;
  const base = { responseStatus, httpStatus, retryAfterSeconds, responseAt, configuredProvider };
  const result = (label: string, summary: string, explanation = summary, badgeStatus: string = attempt.status): AttemptOutcome => ({ label, badgeStatus, summary, explanation, ...base });

  if (accepted) return result('Accepted report saved', attempt.resultSummary?.trim() || 'An accepted report is linked to this exact attempt.', 'This exact attempt produced an accepted report. Acceptance does not itself verify its scientific claims.', 'accepted_report');
  if (httpStatus === 429) {
    const quota = provenance?.explanation;
    const observed = quota?.match(/^Provider reported a (daily|per-minute) token allowance \(HTTP 429\): limit ([\d,]+), used ([\d,]+), remaining ([\d,]+), requested ([\d,]+) for this call\./);
    if (observed) {
      const interval = retryAfterSeconds === null ? 'No retry interval was recorded.' : `The provider advised waiting ${retryAfterSeconds} ${retryAfterSeconds === 1 ? 'second' : 'seconds'} for this request size.`;
      const summary = `${observed[5]} tokens requested; ${observed[4]} remained in the ${observed[1]} allowance when this call was rejected. ${interval}`;
      return result(`${observed[1] === 'daily' ? 'Daily' : 'Per-minute'} token allowance (HTTP 429)`, summary, quota ?? summary, 'provider_rate_limit');
    }
    return result('Provider rate limit (HTTP 429)', `The provider rate-limited this call. ${retryAfterSeconds === null ? 'No retry interval is recorded.' : `Its response advised waiting ${retryAfterSeconds} ${retryAfterSeconds === 1 ? 'second' : 'seconds'} before another request.`} This attempt produced no accepted research report.`, undefined, 'provider_rate_limit');
  }
  if (httpStatus === 413) return result('Request too large (HTTP 413)', 'The provider rejected this request as too large. This attempt produced no accepted research report; any smaller request has its own saved attempt.', undefined, 'provider_rejection');
  if (httpStatus === 400 && errorKind === 'route_used_as_function_name') return result('Search route used as a function name (HTTP 400)', 'The provider rejected a route ID sent as a function name. A route belongs in the route argument of an exposed search function; no host tool or accepted research report resulted from this call.', undefined, 'provider_rejection');
  if (httpStatus === 400 && errorKind === 'unregistered_tool_function') return result('Unregistered function requested (HTTP 400)', 'The provider rejected a function absent from this call’s pinned tool manifest. Choose an exposed function or request a permitted capability; no host tool or accepted research report resulted from this call.', undefined, 'provider_rejection');
  if (httpStatus === 400 && errorKind === 'http_error' && provenance?.explanation.startsWith('Saved provider tool validation: ')) return result('Required tool argument missing (HTTP 400)', provenance.explanation.slice('Saved provider tool validation: '.length), undefined, 'provider_rejection');
  if (httpStatus !== null && httpStatus >= 400) return result(`Provider request rejected (HTTP ${httpStatus})`, `The provider rejected this call with HTTP ${httpStatus}. The exact cause is not established by this status alone; no accepted research report is linked to this attempt.`, undefined, 'provider_rejection');
  if (responseStatus === 'failed' || responseStatus === 'partial') return result('Provider response incomplete', `The saved provider response is ${responseStatus}. No accepted research report is linked to this attempt.`);
  if (responseStatus === 'tool_requested') {
    const tool = attempt.toolCalls[0];
    if (!tool) return result('Model requested a tool step', 'A model tool request was saved, but this snapshot does not identify a completed tool result or accepted research report.', undefined, 'tool_waiting');
    const action = toolActionLabel(tool.name);
    const requested = action === tool.name ? `the ${tool.name} tool` : `the action “${action}”`;
    const state = tool.status.replaceAll('_', ' ');
    if (['rejected', 'failed', 'invalid', 'source_unavailable'].includes(tool.status)) return result(`${action} · ${state}`, `The model requested ${requested}; the saved host tool outcome is ${state}. No accepted research report is linked to this attempt.`, undefined, tool.status === 'source_unavailable' ? 'source_unavailable' : 'tool_rejected');
    if (tool.status === 'result_unaccepted') return result(`${action} · result saved, not accepted`, `A local result for ${requested} was saved, but the task did not accept it. The tool turn remains unresolved; no accepted research report is linked to this attempt.`, undefined, 'tool_recovery_needed');
    if (tool.status === 'acceptance_recorded') return result(`${action} · local recovery accepted`, `The host accepted a recovery record for ${requested}, but the context tool result and next model turn are not saved yet. No accepted research report is linked to this attempt.`, undefined, 'tool_recovery_needed');
    if (tool.status === 'metadata_only') return result(`${action} · metadata only`, `The model requested ${requested}; metadata was saved, not checked source text or an accepted research report.`, undefined, 'metadata_only');
    if (tool.status === 'waiting') return result(`${action} · result not saved`, `The model requested ${requested}, but no matching saved host tool result is linked to this attempt.`, undefined, 'tool_waiting');
    return result(`${action} · ${state}`, `The model requested ${requested}, and the host saved outcome: ${state}. This is a tool step, not an accepted research report from this attempt.`, undefined, 'tool_step_saved');
  }
  if (responseStatus === 'complete') return result('Model response saved', 'A model response was saved, but no accepted research report is linked to this attempt.', undefined, 'response_saved');
  if (attempt.status === 'waiting' && provenance?.explanation.startsWith('Operator paused this prepared model call.')) return result(
    'Model call paused by operator',
    'This prepared call is currently paused by the operator. Its saved attempt still says running, and no provider response is saved for this call. Earlier provider rate limits are separate attempts.',
    provenance.explanation,
    'waiting',
  );
  if (attempt.status === 'waiting' && provenance?.explanation.startsWith('Context preparation needs local repair.')) return result(
    'Context preparation needs local repair',
    'The host could not prepare this model input within its saved context allowance. Review this branch’s exact call budget and repair its context before another attempt. No provider request or response was saved for this attempt.',
    provenance.explanation,
    'waiting',
  );
  if (attempt.status === 'superseded' && provenance?.explanation.startsWith('Context preparation failed locally for this exact call')) return result(
    'Local context preparation failed',
    'This saved call could not fit its required model context. It stopped before a provider request and was later superseded. Review its saved call budget; any later call has a separate attempt.',
    provenance.explanation,
    'superseded',
  );
  if (attempt.status === 'failed' && provenance?.explanation.startsWith('Context preparation failed locally for this exact source-free recovery call')) return result(
    'Local model-input preparation failed',
    'This source-free recovery call exceeded its saved model-input allowance before provider dispatch. Any later recovery would have a separate attempt; no provider failure is inferred.',
    provenance.explanation,
    'failed',
  );
  if (attempt.status === 'superseded') {
    if (provenance?.explanation.includes('before provider dispatch')) return result('Preparation superseded before dispatch', 'This prepared call was replaced before it was sent to the provider. Its successor has a separate saved attempt.');
    return result('Attempt superseded', 'This call is no longer the current task preparation. A provider response is not established by this attempt summary.');
  }
  if (attempt.status === 'uncertain') return result('Provider outcome uncertain', 'The saved records do not confirm whether the provider completed this call. No unseen response or tool action is inferred.');
  if (attempt.resultSummary?.trim()) return result('Saved attempt result', attempt.resultSummary);
  if (attempt.status === 'failed') return result('Attempt failed; cause not recorded here', 'No exact provider response receipt is linked in this snapshot. This attempt has no accepted research report; inspect its saved preparation and tool records for more detail.');
  if (attempt.status === 'running' || attempt.status === 'waiting') return result(`Attempt ${attempt.status}`, `The attempt was ${attempt.status} in this saved snapshot. No final result is inferred.`);
  if (attempt.status === 'completed') return result('Attempt step completed', 'This attempt finished a step, but no accepted research report is linked in this snapshot.');
  return result(`Attempt ${attempt.status}`, 'No accepted research report or specific provider response is linked in this snapshot.');
}
