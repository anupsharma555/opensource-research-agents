/* Outcome language must not turn a pending action or host repair into completed research. */
import { expect, it } from 'vitest';
import type { ResearchSnapshot, SavedRecordRef } from './generated/contracts';
import sample from '../public/sample-run.json';
import { stepPresentation } from './stepPresentation';

const ref = (kind: string, id: string): SavedRecordRef => ({ kind, id, version: 1 });
function recorded() {
  const snapshot = structuredClone(sample) as ResearchSnapshot;
  snapshot.proofKind = 'live_model_with_retrieval';
  const attempt = snapshot.attempts[0];
  attempt.resultSummary = null;
  attempt.toolCalls = [{ name: 'peer_request', status: 'request_saved' }];
  const call = ref('agent_call', 'call-test');
  snapshot.inspection = {
    snapshotReference: ref('research_snapshot', 'view-test'), timingBasis: 'observed_local', initialSourceIds: [], sources: [], acceptedOutputReferences: [],
    attempts: [{ attemptId: attempt.id, instanceReference: ref('agent_instance', attempt.agentId), configurationReference: ref('agent_configuration', 'config-test'), callReferences: [call], inputReferences: [], acceptedOutputReferences: [], explanation: 'Saved exact call.' }],
    records: [
      { reference: ref('model_response_receipt', 'response-test'), label: 'Response', data: { call_ref: call, attempt_id: attempt.id, status: 'tool_requested' } },
      { reference: ref('agent_configuration', 'config-test'), label: 'Configuration', data: { provider: 'groq' } },
    ],
  };
  return { snapshot, attempt };
}

it('describes saving a peer request without inventing delivery, recipient or acceptance', () => {
  const { snapshot, attempt } = recorded();
  const view = stepPresentation(snapshot, attempt);
  expect(view).toMatchObject({ title: 'Requested help from another agent', outcomeLabel: 'Peer request saved', actorKind: 'agent' });
  expect(view.summary).toContain('Delivery, reply and review are recorded separately');
  expect(view.summary).not.toContain('accepted report');
});

it('uses past tense only when the read succeeded and distinguishes a saved read from online retrieval', () => {
  const { snapshot, attempt } = recorded();
  attempt.toolCalls = [{ name: 'read_source_window', status: 'completed' }];
  expect(stepPresentation(snapshot, attempt).summary).toContain('already saved');
  expect(stepPresentation(snapshot, attempt).title).toBe('Retrieved a passage from a saved source');
  attempt.toolCalls[0].status = 'rejected';
  expect(stepPresentation(snapshot, attempt).title).toMatch(/^Requested:/);
  expect(stepPresentation(snapshot, attempt).outcomeLabel).toBe('Tool request rejected');
  attempt.toolCalls[0].status = 'waiting';
  expect(stepPresentation(snapshot, attempt).outcomeLabel).not.toBe('Passage saved');
});

it('names a known model service without borrowing it from another call', () => {
  const { snapshot, attempt } = recorded();
  snapshot.inspection!.records[0].data = { ...snapshot.inspection!.records[0].data, http_status: 429, status: 'failed' };
  expect(stepPresentation(snapshot, attempt)).toMatchObject({ title: 'Groq declined the call: rate limit', actorKind: 'provider' });
  snapshot.inspection!.attempts[0].configurationReference = ref('agent_configuration', 'missing');
  expect(stepPresentation(snapshot, attempt).title).toBe('Model service declined the call: rate limit');
});

it('attributes unsent request replacement to the system without guessing a successor or reason', () => {
  const { snapshot, attempt } = recorded();
  snapshot.inspection!.records = [];
  snapshot.inspection!.attempts[0].explanation = 'This prepared call was replaced before provider dispatch; its successor has a separate saved attempt.';
  attempt.status = 'superseded'; attempt.toolCalls = [];
  expect(stepPresentation(snapshot, attempt)).toMatchObject({ actorKind: 'system', title: 'Replaced an unsent model request', outcomeLabel: 'Not sent to the model' });
  expect(stepPresentation(snapshot, attempt).summary).toContain('must be checked in the saved records');
});

it('shows a linked accepted publication as submitted rather than merely requested', () => {
  const { snapshot, attempt } = recorded();
  const report = snapshot.artifacts[0];
  const output = ref('report', report.id); output.version = report.version;
  snapshot.inspection!.acceptedOutputReferences = [output];
  snapshot.inspection!.attempts[0].acceptedOutputReferences = [output];
  attempt.toolCalls = [{ name: 'publish_result', status: 'published' }];
  expect(stepPresentation(snapshot, attempt)).toMatchObject({ title: 'Submitted a research contribution', outcomeLabel: 'Report accepted' });
  snapshot.inspection!.acceptedOutputReferences = [];
  expect(stepPresentation(snapshot, attempt).outcomeLabel).not.toBe('Report accepted');
});
