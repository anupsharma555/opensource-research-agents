import { expect, it } from 'vitest';
import type { ResearchSnapshot, SavedRecordRef } from './generated/contracts';
import { activityAction, activityOverview, activityScanSummary, hasRecordedIssue } from './activityPresentation';
import type { ActivityItem } from './ActivityRecordDetails';
import sample from '../public/sample-run.json';

const ref = (kind: string, id: string, version = 1): SavedRecordRef => ({ kind, id, version });

function savedLiveStep() {
  const snapshot = structuredClone(sample) as ResearchSnapshot;
  snapshot.proofKind = 'live_model_with_retrieval';
  const attempt = snapshot.attempts[0];
  attempt.status = 'completed';
  attempt.resultSummary = null;
  attempt.toolCalls = [{ name: 'search_scholarly', status: 'rejected' }];
  const call = ref('agent_call', 'call-1');
  snapshot.inspection = {
    snapshotReference: ref('research_snapshot', 'snapshot-1'), timingBasis: 'observed_local',
    initialSourceIds: [], sources: [], acceptedOutputReferences: [],
    attempts: [{ attemptId: attempt.id, instanceReference: ref('agent_instance', attempt.agentId), configurationReference: ref('agent_configuration', 'config-1'), callReferences: [call], inputReferences: [], acceptedOutputReferences: [], explanation: 'Exact saved call.' }],
    records: [{ reference: ref('model_response_receipt', 'response-1'), label: 'Saved response', data: { call_ref: call, attempt_id: attempt.id, status: 'tool_requested' } }],
  };
  const item: ActivityItem = { id: attempt.id, time: attempt.startedAt, timeLabel: 'Attempt finished', category: 'agent', title: 'Saved tool step', summary: 'The model requested search_scholarly; the tool was rejected.', attemptId: attempt.id, agentId: attempt.agentId, status: attempt.status, badgeStatus: 'tool_rejected' };
  return { snapshot, attempt, item };
}

it('counts exact report references and unique saved peer requests without equating completed attempts with reports', () => {
  const { snapshot } = savedLiveStep();
  snapshot.messages = [];
  snapshot.inspection!.records.push(
    { reference: ref('mail_request', 'peer-1'), label: 'Request v1', data: {} },
    { reference: ref('mail_request', 'peer-1', 2), label: 'Request v2', data: {} },
    { reference: ref('mail_request', 'peer-2'), label: 'Second request', data: {} },
  );
  expect(activityOverview(snapshot)).toMatchObject({ attempts: snapshot.attempts.length, receipts: 1, acceptedReports: 0, peerRequests: 2 });
  snapshot.inspection!.acceptedOutputReferences = [ref('report', 'missing-report')];
  expect(activityOverview(snapshot).acceptedReports).toBeNull();
  snapshot.inspection!.acceptedOutputReferences = [ref('report', snapshot.artifacts[0].id, snapshot.artifacts[0].version)];
  expect(activityOverview(snapshot).acceptedReports).toBe(1);
});

it('does not count a legacy answer-only message as a peer request', () => {
  const { snapshot } = savedLiveStep();
  snapshot.inspection = undefined;
  snapshot.messages = snapshot.messages.map(message => ({ ...message, status: 'fulfilled' }));
  expect(activityOverview(snapshot).peerRequests).toBe(0);
  snapshot.messages[0].status = 'requested';
  expect(activityOverview(snapshot).peerRequests).toBe(1);
});

it('uses plain known-tool wording, preserves an unknown tool name, and does not invent an ambiguous provider outcome', () => {
  const { snapshot, attempt, item } = savedLiveStep();
  expect(activityAction(snapshot, item)).toBe('Requested: Scholarly source search');
  expect(activityScanSummary(snapshot, item)).not.toContain('search_scholarly');
  expect(hasRecordedIssue(item)).toBe(true);
  attempt.toolCalls[0].status = 'failed';
  expect(activityScanSummary(snapshot, item)).toBe('This tool step did not succeed.');
  attempt.toolCalls = [{ name: 'custom_reader', status: 'rejected' }];
  expect(activityAction(snapshot, item)).toBe('Requested: custom_reader');
  snapshot.inspection!.records.push({ reference: ref('model_response_receipt', 'response-2'), label: 'Another response', data: { call_ref: ref('agent_call', 'call-1'), attempt_id: attempt.id, status: 'failed', http_status: 429 } });
  expect(activityAction(snapshot, item)).not.toMatch(/rate limit/i);
  expect(activityOverview(snapshot).receipts).toBe(2);
});

it('distinguishes saved excerpt reads from pending, metadata-only and rejected source steps', () => {
  const { snapshot, attempt, item } = savedLiveStep();
  attempt.toolCalls = [{ name: 'read_source_window', status: 'completed' }];
  expect(activityAction(snapshot, item)).toBe('Retrieved a passage from a saved source');
  expect(activityScanSummary(snapshot, item)).toBe('The tool retrieved part of a source already saved in this workspace. Inspect the exact passage and next model input to check whether the model received it.');
  attempt.toolCalls[0].status = 'waiting';
  expect(activityAction(snapshot, item)).toBe('Requested: Read a saved source excerpt');
  expect(activityScanSummary(snapshot, item)).toBe('The requested tool result is not saved in this snapshot.');
  attempt.toolCalls[0].status = 'rejected';
  expect(activityScanSummary(snapshot, item)).toBe('The host rejected this tool step.');
  attempt.toolCalls = [{ name: 'read_source_inventory', status: 'metadata_only' }];
  expect(activityAction(snapshot, item)).toBe('Requested: List saved sources');
  expect(activityScanSummary(snapshot, item)).toBe('Metadata was saved; checked source text is not established.');
  attempt.toolCalls = [{ name: 'inspect_hit', status: 'metadata_only' }];
  expect(activityAction(snapshot, item)).toBe('Requested: Inspect a saved search result');
  expect(activityScanSummary(snapshot, item)).not.toMatch(/checked source text was saved/);
});


it('counts prepared requests separately from steps and captured responses, retaining unavailable inspection', () => {
  const { snapshot } = savedLiveStep();
  const intent = { reference: ref('model_request_intent', 'intent-1'), label: 'Prepared input', data: {} };
  snapshot.inspection!.records.push(intent, structuredClone(intent), { ...intent, reference: ref('model_request_intent', 'intent-2') });
  expect(activityOverview(snapshot)).toMatchObject({ prepared: 2, receipts: 1 });
  snapshot.inspection = undefined;
  expect(activityOverview(snapshot)).toMatchObject({ prepared: null, receipts: null, acceptedReports: null });
});
