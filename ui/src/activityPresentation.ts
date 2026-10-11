/* Derive a compact Activity scan from saved snapshot records. These labels do
   not treat a completed model turn or tool step as an accepted report. */
import type { ResearchSnapshot } from './generated/contracts';
import type { ActivityItem } from './ActivityRecordDetails';
import { formatActivityTimestamp } from './ActivityRecordDetails';
import { describeLiveAttempt, toolActionLabel } from './attemptOutcome';
import { stepPresentation } from './stepPresentation';

const referenceKey = (ref: { kind: string; id: string; version: number }) => `${ref.kind}:${ref.id}:${ref.version}`;

export function activityOverview(snapshot: ResearchSnapshot) {
  const inspection = snapshot.inspection;
  const receipts = inspection ? new Set(inspection.records.filter(record => record.reference.kind === 'model_response_receipt').map(record => referenceKey(record.reference))).size : null;
  const prepared = inspection ? new Set(inspection.records.filter(record => record.reference.kind === 'model_request_intent').map(record => referenceKey(record.reference))).size : null;
  const acceptedRefs = inspection?.acceptedOutputReferences.filter(ref => ref.kind === 'report') ?? [];
  const acceptedReports = inspection && acceptedRefs.every(ref => snapshot.artifacts.filter(artifact => artifact.id === ref.id && artifact.version === ref.version).length === 1)
    ? new Set(acceptedRefs.map(referenceKey)).size : null;
  const issues = snapshot.attempts.filter(attempt => hasRecordedIssue({
    id: attempt.id, time: attempt.startedAt, timeLabel: '', category: 'agent', title: '', summary: '',
    status: attempt.status, badgeStatus: describeLiveAttempt(snapshot, attempt)?.badgeStatus,
  })).length;
  const savedRequests = inspection?.records.filter(record => record.reference.kind === 'mail_request') ?? [];
  const peerRequests = inspection
    ? new Set(savedRequests.map(record => record.reference.id)).size
    : new Set(snapshot.messages.filter(message => message.status === 'requested').map(message => message.requestId || message.id)).size;
  return { attempts: snapshot.attempts.length, prepared, receipts, acceptedReports, issues, peerRequests };
}

export function hasRecordedIssue(item: ActivityItem) {
  return ['failed', 'waiting', 'uncertain', 'cancelled', 'provider_rate_limit', 'provider_rejection', 'tool_rejected', 'tool_recovery_needed', 'source_unavailable', 'tool_waiting', 'awaiting_review']
    .includes(item.badgeStatus ?? item.status ?? '');
}

export function activityScanSummary(snapshot: ResearchSnapshot, item: ActivityItem): string {
  if (!item.attemptId) return item.summary;
  const attempt = snapshot.attempts.find(record => record.id === item.attemptId);
  if (!attempt) return item.summary;
  const task = snapshot.tasks.find(record => record.id === attempt.taskId);
  if (attempt.status === 'waiting' && task?.status === 'waiting' && task.waitingOperationReference) {
    const reason = task.waitingReason || 'Reason not recorded in this snapshot';
    const next = task.waitingUntil ? ` Next eligible after ${formatActivityTimestamp(task.waitingUntil)}; dispatch may occur later.` : ' No next eligible time is recorded.';
    return `Current task wait: ${reason}.${next}`;
  }
  const outcome = describeLiveAttempt(snapshot, attempt);
  if (outcome && stepPresentation(snapshot, attempt).actorKind !== 'agent') return stepPresentation(snapshot, attempt).summary;
  if (outcome?.label === 'Required tool argument missing (HTTP 400)') return outcome.summary;
  const tool = outcome?.responseStatus === 'tool_requested' ? attempt.toolCalls[0] : undefined;
  if (!tool || toolActionLabel(tool.name) === tool.name) return item.summary;
  if (tool.status === 'metadata_only') return 'Metadata was saved; checked source text is not established.';
  if (tool.status === 'source_unavailable') return 'The requested source was unavailable.';
  if (tool.status === 'rejected') return 'The host rejected this tool step.';
  if (tool.status === 'result_unaccepted') return 'A local tool result was saved, but the task has not accepted it.';
  if (tool.status === 'acceptance_recorded') return 'Local recovery was accepted; the context tool result and next turn are not saved yet.';
  if (['failed', 'invalid'].includes(tool.status)) return 'This tool step did not succeed.';
  if (tool.status === 'waiting') return 'The requested tool result is not saved in this snapshot.';
  return stepPresentation(snapshot, attempt).summary;
}

export function activityAction(snapshot: ResearchSnapshot, item: ActivityItem): string {
  const actorId = item.agentId || item.fromAgentId;
  const actor = actorId ? snapshot.agents.find(agent => agent.id === actorId)?.name ?? `Agent identity unavailable (${actorId})` : null;
  const fallback = actor && item.title.startsWith(`${actor} · `) ? item.title.slice(actor.length + 3)
    : actor && item.title.startsWith(`${actor} `) ? item.title.slice(actor.length + 1) : item.title;
  if (!item.attemptId) return fallback;
  const attempt = snapshot.attempts.find(record => record.id === item.attemptId);
  if (!attempt) return fallback;
  const task = snapshot.tasks.find(record => record.id === attempt.taskId);
  if (attempt.status === 'waiting' && task?.status === 'waiting' && task.waitingOperationReference) return 'Waiting for model capacity';
  const outcome = describeLiveAttempt(snapshot, attempt);
  if (outcome?.badgeStatus === 'accepted_report') return 'Saved an accepted report';
  if (outcome?.badgeStatus === 'provider_rate_limit') return stepPresentation(snapshot, attempt).title;
  if (outcome?.label === 'Required tool argument missing (HTTP 400)') return outcome.label;
  if (outcome?.badgeStatus === 'provider_rejection') return stepPresentation(snapshot, attempt).title;
  const tool = outcome?.responseStatus === 'tool_requested' ? attempt.toolCalls[0] : undefined;
  if (tool) {
    return stepPresentation(snapshot, attempt).title;
  }
  return outcome ? stepPresentation(snapshot, attempt).title : fallback;
}
