import { render, screen, within, cleanup } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { ResearchApp } from './App';
import { describeAttempt, recordedToolAction, showsOriginalRunningRecord } from './attemptOutcome';
import { activityAction, activityOverview, activityScanSummary } from './activityPresentation';
import type { ResearchSnapshot, SavedRecordRef } from './generated/contracts';
import sample from '../public/sample-run.json';

const ref = (kind: string, id: string, version = 1): SavedRecordRef => ({ kind, id, version });

function savedAttempt() {
  const snapshot = structuredClone(sample) as ResearchSnapshot;
  snapshot.proofKind = 'live_mission_pending';
  const attempt = snapshot.attempts.find(item => item.id === 'attempt-a2-failed')!;
  attempt.resultSummary = null;
  attempt.toolCalls = [];
  const call = ref('agent_call', 'exact-call');
  const configuration = ref('agent_configuration', 'exact-configuration');
  snapshot.inspection = {
    snapshotReference: ref('research_snapshot', 'exact-snapshot'),
    timingBasis: 'observed_local', initialSourceIds: [], sources: [], acceptedOutputReferences: [],
    attempts: [{ attemptId: attempt.id, instanceReference: ref('agent_instance', attempt.agentId), configurationReference: configuration, callReferences: [call], inputReferences: [], acceptedOutputReferences: [], explanation: 'Selected model and exact call inputs were saved.' }],
    records: [
      { reference: configuration, label: 'Exact configuration', data: { provider: 'synthetic-provider' } },
      { reference: ref('model_response_receipt', 'exact-response'), label: 'Exact provider response', data: { call_ref: call, attempt_id: attempt.id, status: 'failed', http_status: 429, retry_after_seconds: 29, recordedAt: '2026-10-08T14:03:30Z' } },
    ],
  };
  return { snapshot, attempt };
}

beforeEach(() => { vi.stubGlobal('fetch', vi.fn(async () => Response.json({ records: [] }))); });

type User = ReturnType<typeof userEvent.setup>;
async function openDisclosure(user: User, root: HTMLElement, label: string) {
  const summary = within(root).getByText(label);
  if (!summary.closest('details')?.open) await user.click(summary);
}
async function openReviewStep(user: User, number = 1) {
  await user.click(screen.getByRole('button', { name: 'Activity' }));
  await openDisclosure(user, document.body, 'Browse all saved work steps');
  const timeline = screen.getByRole('group', { name: 'Recorded agent attempts' });
  const button = within(timeline).getByRole('button', { name: new RegExp(`^Open work step ${number}:`) });
  await user.click(button);
  const row = button.closest('li')!;
  expect(within(row).getByRole('region', { name: 'Actual model call records' })).toBeVisible();
  return row;
}
async function expandActivity(user: User, card: HTMLElement) {
  await user.click(within(card).getByRole('button', { name: 'Expand activity details' }));
  expect(within(card).getByRole('region', { name: 'Actual model call records' })).toBeVisible();
}
async function openExactAttempt(user: User, card: HTMLElement) {
  await openDisclosure(user, card, 'Technical records and current task state');
  await user.click(within(card).getByRole('button', { name: 'Open selected attempt in Details →' }));
  return document.getElementById('selected-attempt')!;
}

afterEach(() => { cleanup(); window.localStorage.clear(); vi.restoreAllMocks(); vi.unstubAllGlobals(); });

it('keeps a rejected tool outcome consistent between Activity and task Details', async () => {
  const { snapshot, attempt } = savedAttempt();
  attempt.status = 'completed';
  attempt.toolCalls = [{ name: 'read_search_result', status: 'rejected' }];
  snapshot.attempts = [attempt];
  const receipt = snapshot.inspection!.records.find(item => item.reference.kind === 'model_response_receipt')!.data;
  receipt.status = 'tool_requested';
  receipt.http_status = 200;
  const user = userEvent.setup();
  render(<ResearchApp snapshot={snapshot} live={{ runId: 'synthetic-run', model: 'saved model', runs: [], error: null, form: null, requestedAgentIds: [], onSelectRun: vi.fn() }} />);
  const output = screen.getByRole('region', { name: 'Current saved output' });
  expect(within(output).getByText(/No accepted report yet/)).toBeVisible();
  expect(within(output).queryByRole('heading', { name: 'Saved answer' })).not.toBeInTheDocument();
  expect(within(output).queryByRole('region', { name: 'Accepted output records' })).not.toBeInTheDocument();
  const row = await openReviewStep(user);
  expect(fetch).toHaveBeenCalledWith(`/api/live/calls?runId=synthetic-run&attemptId=${attempt.id}`, expect.objectContaining({ credentials: 'same-origin' }));
  expect(row.querySelector('.agent-step-select')).toHaveTextContent('Tool request rejected');
  expect(row.querySelector('.agent-step-select')).not.toHaveTextContent('Completed');
  await user.click(within(row).getByRole('button', { name: 'Open task in Details →' }));
  const task = document.getElementById('selected-task')!;
  const step = within(task).getByRole('button', { name: /work step 1/ });
  expect(step.querySelector('.status-pill')).toHaveTextContent('tool rejected');
  expect(step.querySelector('.status-pill')).not.toHaveTextContent('completed');
});

it('explains an exact provider rate limit in Activity and the selected Details record', async () => {
  const { snapshot, attempt } = savedAttempt();
  const user = userEvent.setup();
  render(<ResearchApp snapshot={snapshot} live={{ runId: 'synthetic-run', model: 'saved model', runs: [], error: null, form: null, requestedAgentIds: [], onSelectRun: vi.fn() }} />);
  await user.click(screen.getByRole('button', { name: 'Activity' }));
  const card = screen.getByText('synthetic-provider declined the call: rate limit', { selector: '.activity-item-head > strong' }).closest('li')!;
  expect(card.querySelector('.activity-item-head .status-pill')).toHaveTextContent('Rate limit');
  expect(card.querySelector('.system-action-label')).toHaveTextContent('synthetic-provider · for');
  expect(card.querySelector('.activity-item-head .agent-badge')).toBeNull();
  expect(within(card).getByText(/29 seconds/)).toBeVisible();
  await expandActivity(user, card);
  expect(fetch).toHaveBeenCalledWith(`/api/live/calls?runId=synthetic-run&attemptId=${attempt.id}`, expect.objectContaining({ credentials: 'same-origin' }));
  const selected = await openExactAttempt(user, card);
  await openDisclosure(user, selected, 'Exact identity, timing, model and skills');
  expect(within(selected).getByText(new RegExp(`Attempt ${attempt.id} · task ${attempt.taskId}`))).toBeVisible();
  expect(within(selected).getByText(/Provider rate limit \(HTTP 429\)/)).toBeVisible();
  expect(selected.querySelector('.panel-heading .status-pill')).toHaveTextContent('provider rate limit');
  expect(within(selected).getByText('Stored attempt status')).toBeVisible();
  await openDisclosure(user, selected, 'Saved agent, configuration and recovery details');
  const recovery = within(selected).getByText('Saved agent, configuration and recovery details').closest('details')!;
  await openDisclosure(user, recovery, 'Exact configuration');
  expect(within(recovery).getByText('synthetic-provider')).toBeVisible();
});

it.each([
  ['daily', 'Daily token allowance (HTTP 429)'],
  ['per-minute', 'Per-minute token allowance (HTTP 429)'],
])('shows a compact %s token allowance for an exact saved 429', async (allowance, label) => {
  const { snapshot, attempt } = savedAttempt();
  const safe = `Provider reported a ${allowance} token allowance (HTTP 429): limit 200,000, used 195,540, remaining 4,460, requested 5,026 for this call. The provider advised waiting 245 seconds for the rejected request size. This is not a full allowance reset or a promise that a larger or different request will fit. No accepted research report came from this call.`;
  snapshot.inspection!.attempts[0].explanation = safe;
  const receipt = snapshot.inspection!.records.find(item => item.reference.kind === 'model_response_receipt')!.data;
  receipt.retry_after_seconds = 245;
  const row = { id: attempt.id, time: attempt.startedAt, timeLabel: 'Attempt started',
    category: 'agent' as const, title: 'Saved attempt', summary: 'Generic failed request',
    attemptId: attempt.id, agentId: attempt.agentId, status: attempt.status };
  expect(describeAttempt(snapshot, attempt).label).toBe(label);
  expect(activityAction(snapshot, row)).toBe('synthetic-provider declined the call: rate limit');
  expect(activityScanSummary(snapshot, row)).toBe(`5,026 tokens requested; 4,460 remained in the ${allowance} allowance when this call was rejected. synthetic-provider advised waiting 245 seconds for this request size.`);
  expect(describeAttempt(snapshot, attempt).explanation).toBe(safe);

  const user = userEvent.setup();
  render(<ResearchApp snapshot={snapshot} live={{ runId: 'synthetic-run', model: 'saved model', runs: [], error: null,
    form: null, requestedAgentIds: [], onSelectRun: vi.fn() }} />);
  await user.click(screen.getByRole('button', { name: 'Activity' }));
  const card = screen.getByText('synthetic-provider declined the call: rate limit', { selector: '.activity-item-head > strong' }).closest('li')!;
  expect(within(card).getByText(/5,026 tokens requested; 4,460 remained/)).toBeVisible();
  expect(within(card).queryByText(/limit 200,000/)).not.toBeInTheDocument();
  await expandActivity(user, card);
  const selected = await openExactAttempt(user, card);
  await openDisclosure(user, selected, 'Exact identity, timing, model and skills');
  await openDisclosure(user, selected, 'Saved agent, configuration and recovery details');
  expect(selected).toHaveTextContent(label);
  expect(document.getElementById('selected-attempt')).toHaveTextContent('limit 200,000, used 195,540');
  expect(document.getElementById('selected-attempt')).toHaveTextContent('not a full allowance reset');
});

it('explains a schema-verified missing query without treating every HTTP 400 as the same cause', async () => {
  const { snapshot, attempt } = savedAttempt();
  const receipt = snapshot.inspection!.records.find(item => item.reference.kind === 'model_response_receipt')!.data;
  receipt.http_status = 400;
  receipt.error_kind = 'http_error';
  snapshot.inspection!.attempts[0].explanation = "Saved provider tool validation: The provider rejected search_scholarly because required query was missing from the model's proposed tool call. No host tool ran from this rejected call; no accepted report is linked to it.";
  const row = { id: attempt.id, time: attempt.startedAt, timeLabel: 'Attempt started', category: 'agent' as const, title: 'Saved attempt', summary: 'Generic failed request', attemptId: attempt.id, agentId: attempt.agentId, status: attempt.status };
  expect(describeAttempt(snapshot, attempt).label).toBe('Required tool argument missing (HTTP 400)');
  expect(activityAction(snapshot, row)).toBe('Required tool argument missing (HTTP 400)');
  expect(activityScanSummary(snapshot, row)).toMatch(/required query was missing/);
  const user = userEvent.setup();
  render(<ResearchApp snapshot={snapshot} live={{ runId: 'synthetic-run', model: 'saved model', runs: [], error: null, form: null, requestedAgentIds: [], onSelectRun: vi.fn() }} />);
  await user.click(screen.getByRole('button', { name: 'Activity' }));
  const card = screen.getByText('Required tool argument missing (HTTP 400)', { selector: '.activity-item-head > strong' }).closest('li')!;
  expect(within(card).getByText(/required query was missing/)).toBeVisible();
  expect(card.querySelector('.system-action-label')).toHaveTextContent('synthetic-provider · for');
  await expandActivity(user, card);
  expect(card.querySelector('.selected-work-step')).toHaveTextContent('No host tool ran');
  const technical = within(card).getByText('Technical records and current task state').closest('details')!;
  expect(technical).not.toHaveAttribute('open');
  const selected = await openExactAttempt(user, card);
  await openDisclosure(user, selected, 'Exact identity, timing, model and skills');
  expect(within(selected).getByText('Stored attempt status')).toBeVisible();
  expect(within(selected).getByText(new RegExp(`Attempt ${attempt.id} · task ${attempt.taskId}`))).toBeVisible();
  snapshot.inspection!.attempts[0].explanation = 'Selected model and exact call inputs were saved.';
  expect(describeAttempt(snapshot, attempt).summary).toMatch(/exact cause is not established/);
});

it('describes a saved tool-only step in Activity instead of claiming its result summary is missing', async () => {
  const { snapshot, attempt } = savedAttempt();
  snapshot.attempts = [attempt];
  attempt.status = 'completed';
  attempt.toolCalls = [{ name: 'search_scholarly', status: 'completed' }];
  const response = snapshot.inspection!.records.find(item => item.reference.kind === 'model_response_receipt')!.data;
  response.status = 'tool_requested';
  response.http_status = null;
  const user = userEvent.setup();
  render(<ResearchApp snapshot={snapshot} live={{ runId: 'synthetic-run', model: 'saved model', runs: [], error: null, form: null, requestedAgentIds: [], onSelectRun: vi.fn() }} />);
  const row = await openReviewStep(user);
  expect(row.querySelector('.selected-work-step')).toHaveTextContent('The agent searched for publications');
  expect(row.querySelector('.selected-work-step')).toHaveTextContent('search_scholarly');
  expect(within(row).queryByText('No result summary was saved.')).not.toBeInTheDocument();
  expect(within(row).getByRole('region', { name: 'Actual model call records' })).toBeVisible();
  await user.click(screen.getByRole('button', { name: 'Activity' }));
  expect(screen.getByText('Searched for publications', { selector: '.activity-item-head > strong' })).toBeInTheDocument();
});

it('shows a source-window action in plain language while retaining its exact function in Details', async () => {
  const { snapshot, attempt } = savedAttempt();
  snapshot.attempts = [attempt];
  attempt.status = 'completed';
  attempt.toolCalls = [{ name: 'read_source_window', status: 'completed' }];
  snapshot.attempts.push(...Array.from({ length: 8 }, (_, index) => ({
    ...structuredClone(attempt),
    id: `later-source-attempt-${index}`,
    startedAt: new Date(Date.parse(attempt.startedAt) + (index + 1) * 1000).toISOString(),
    toolCalls: [],
  })));
  const response = snapshot.inspection!.records.find(item => item.reference.kind === 'model_response_receipt')!.data;
  response.status = 'tool_requested';
  response.http_status = null;
  const user = userEvent.setup();
  render(<ResearchApp snapshot={snapshot} live={{ runId: 'synthetic-run', model: 'saved model', runs: [], error: null,
    form: null, requestedAgentIds: [], onSelectRun: vi.fn() }} />);
  const row = await openReviewStep(user);
  expect(row.querySelector('.selected-work-step')).toHaveTextContent('The tool retrieved part of a source already saved');
  expect(row.querySelector('.selected-work-step')).toHaveTextContent('read_source_window');
  expect(within(row).getByRole('button', { name: /^Open work step 1: Retrieved a passage from a saved source/ })).toBeVisible();
  await user.click(screen.getByRole('button', { name: 'Details' }));
  await user.click(screen.getByRole('button', { name: 'Work step records' }));
  const selected = document.getElementById('selected-attempt')!;
  const exact = within(selected).getByText('Exact identity, timing, model and skills').closest('details')!;
  await user.click(within(exact).getByText('Exact identity, timing, model and skills'));
  expect(within(exact).getByText(/read_source_window/)).toBeVisible();
});

it('names capability discovery plainly in Activity while keeping the exact function inspectable', async () => {
  const { snapshot, attempt } = savedAttempt();
  snapshot.attempts = [attempt];
  attempt.status = 'completed';
  attempt.toolCalls = [{ name: 'discover_capabilities', status: 'completed' }];
  const response = snapshot.inspection!.records.find(item => item.reference.kind === 'model_response_receipt')!.data;
  response.status = 'tool_requested';
  response.http_status = null;
  vi.stubGlobal('fetch', vi.fn(async () => Response.json({ records: [] })));
  const user = userEvent.setup();
  render(<ResearchApp snapshot={snapshot} live={{ runId: 'synthetic-run', model: 'saved model', runs: [], error: null,
    form: null, requestedAgentIds: [], onSelectRun: vi.fn() }} />);
  const row = await openReviewStep(user);
  expect(within(row).getByRole('button', { name: /^Open work step 1: Looked up the available tools/ })).toBeVisible();
  await user.click(screen.getByRole('button', { name: 'Activity' }));
  expect(screen.getByText('Looked up the available tools', { selector: '.activity-item-head > strong' })).toBeVisible();
  await user.click(screen.getByRole('button', { name: 'Details' }));
  await user.click(screen.getByRole('button', { name: 'Work step records' }));
  const selected = document.getElementById('selected-attempt')!;
  await user.click(within(selected).getByText('Exact identity, timing, model and skills'));
  expect(within(selected).getByText(/discover_capabilities/)).toBeVisible();
});

it('shows an exact current model-capacity wait and eligible time without promising dispatch', async () => {
  const { snapshot, attempt } = savedAttempt();
  snapshot.attempts = [attempt];
  attempt.status = 'waiting';
  attempt.endedAt = null;
  snapshot.inspection!.records = snapshot.inspection!.records.filter(item => item.reference.kind !== 'model_response_receipt');
  const task = snapshot.tasks.find(item => item.id === attempt.taskId)!;
  task.status = 'waiting';
  task.waitingReason = 'Provider quota or resource wait';
  task.waitingUntil = '2026-10-08T14:30:00Z';
  task.waitingOperationReference = ref('capacity_operation', 'exact-wait');
  const user = userEvent.setup();
  render(<ResearchApp snapshot={snapshot} live={{ runId: 'synthetic-run', model: 'saved model', runs: [], error: null,
    form: null, requestedAgentIds: [], onSelectRun: vi.fn() }} />);
  await user.click(screen.getByRole('button', { name: 'Activity' }));
  const card = screen.getByText('Waiting for model capacity', { selector: '.activity-item-head > strong' }).closest('li')!;
  expect(card.querySelector('.activity-item-head .status-pill')).toHaveTextContent('waiting');
  expect(within(card).getByText(/Current task wait: Provider quota or resource wait/)).toHaveTextContent('Next eligible after');
  expect(within(card).getByText(/Current task wait: Provider quota or resource wait/)).toHaveTextContent('dispatch may occur later');
  await expandActivity(user, card);
  await openDisclosure(user, card, 'Technical records and current task state');
  const current = card.querySelector('.step-wait')!;
  expect(current).toHaveTextContent('Current task wait, as of');
  expect(current).toHaveTextContent('Provider quota or resource wait');
  expect(current).toHaveTextContent('Eligible after');
  expect(current).toHaveTextContent('not an explanation of an earlier step');

  task.waitingOperationReference = null;
  const row = { id: attempt.id, time: attempt.startedAt, timeLabel: 'Attempt started', category: 'agent' as const,
    title: 'Attempt waiting', summary: 'The attempt was waiting.', attemptId: attempt.id, agentId: attempt.agentId, status: attempt.status };
  expect(activityAction(snapshot, row)).toBe('Attempt waiting');
  expect(activityScanSummary(snapshot, row)).not.toContain('Next eligible after');
});

it('labels the current operator-held prepared call separately from a provider rate limit', async () => {
  const { snapshot, attempt } = savedAttempt();
  attempt.status = 'waiting';
  attempt.endedAt = null;
  snapshot.inspection!.records = snapshot.inspection!.records.filter(item => item.reference.kind !== 'model_response_receipt');
  snapshot.inspection!.attempts[0].explanation = 'Operator paused this prepared model call. The current task control names this exact call; its saved attempt record remains running, and no provider response is saved for it. Earlier provider rate limits are separate saved attempts.';
  const row = { id: attempt.id, time: attempt.startedAt, timeLabel: 'Attempt started', category: 'agent' as const,
    title: 'Prepared call', summary: 'Original attempt still running', attemptId: attempt.id,
    agentId: attempt.agentId, status: attempt.status };
  const outcome = describeAttempt(snapshot, attempt);
  expect(outcome.label).toBe('Model call paused by operator');
  expect(outcome.summary).toMatch(/Earlier provider rate limits are separate attempts/);
  expect(showsOriginalRunningRecord(snapshot, attempt, outcome)).toBe(true);
  expect(activityAction(snapshot, row)).toBe('Paused model calls at the operator’s request');
  expect(activityScanSummary(snapshot, row)).toMatch(/currently paused by the operator/);

  const user = userEvent.setup();
  render(<ResearchApp snapshot={snapshot} live={{ runId: 'synthetic-run', model: 'saved model', runs: [], error: null,
    form: null, requestedAgentIds: [], onSelectRun: vi.fn() }} />);
  await user.click(screen.getByRole('button', { name: 'Activity' }));
  const card = screen.getByText('Paused model calls at the operator’s request', { selector: '.activity-item-head > strong' }).closest('li')!;
  expect(card.querySelector('.activity-item-head .status-pill')).toHaveTextContent('Paused');
  expect(card.querySelector('.system-action-label')).toHaveTextContent('System · for');
  expect(card.querySelector('.activity-item-head .agent-badge')).toBeNull();
  expect(within(card).getByText(/Earlier provider rate limits are separate attempts/)).toBeVisible();
});

it('shows an exact local context-preparation wait without attributing it to an older attempt or the provider', async () => {
  const { snapshot, attempt } = savedAttempt();
  const older = structuredClone(attempt);
  older.id = 'older-unrelated-attempt';
  older.status = 'superseded';
  older.startedAt = '2026-10-08T13:00:00Z';
  snapshot.attempts = [older, attempt];
  attempt.status = 'waiting';
  attempt.endedAt = null;
  snapshot.inspection!.records = snapshot.inspection!.records.filter(item => item.reference.kind !== 'model_response_receipt');
  snapshot.inspection!.attempts.push({
    ...snapshot.inspection!.attempts[0],
    attemptId: older.id,
    explanation: 'No current task control names this call. Its original running status remains saved.',
  });
  snapshot.inspection!.attempts[0].explanation = 'Context preparation needs local repair. This exact next call has a saved budget and attempt but no model request intent, provider dispatch, or response. The original attempt record remains running; no provider failure is inferred.';
  const task = snapshot.tasks.find(item => item.id === attempt.taskId)!;
  task.status = 'waiting';
  task.waitingReason = 'ContextOverflow: this branch needs local repair';

  expect(describeAttempt(snapshot, attempt).label).toBe('Context preparation needs local repair');
  expect(describeAttempt(snapshot, attempt).summary).toMatch(/No provider request or response was saved/);
  expect(describeAttempt(snapshot, older).label).toBe('Attempt superseded');
  const row = { id: attempt.id, time: attempt.startedAt, timeLabel: 'Attempt started',
    category: 'agent' as const, title: 'Saved attempt', summary: 'Generic pending step',
    attemptId: attempt.id, agentId: attempt.agentId, status: attempt.status };
  expect(activityAction(snapshot, row)).toBe('Context preparation needs local repair');
  expect(activityScanSummary(snapshot, row)).toMatch(/repair its context before another attempt/);

  const user = userEvent.setup();
  render(<ResearchApp snapshot={snapshot} live={{ runId: 'synthetic-run', model: 'saved model', runs: [], error: null,
    form: null, requestedAgentIds: [], onSelectRun: vi.fn() }} />);
  await user.click(screen.getByRole('button', { name: 'Activity' }));
  const card = screen.getByText('Context preparation needs local repair', { selector: '.activity-item-head > strong' }).closest('li')!;
  expect(within(card).getByText(/No provider request or response was saved/)).toBeVisible();
  expect(card.querySelector('.system-action-label')).toHaveTextContent('System · for');
  expect(card.querySelector('.activity-item-head .agent-badge')).toBeNull();
  await expandActivity(user, card);
  await openDisclosure(user, card, 'Technical records and current task state');
  await openDisclosure(user, card, 'Agent settings and recovery');
  expect(within(card).getByText(/This exact next call has a saved budget/)).toBeVisible();
  const selected = await openExactAttempt(user, card);
  await user.click(within(selected).getByText('Exact identity, timing, model and skills'));
  expect(within(selected).getByText('Snapshot attempt state')).toBeVisible();
  expect(within(selected).getByText('waiting (saved attempt record: running)')).toBeVisible();
  expect(within(selected).getByText(/No provider request or response was saved/)).toBeVisible();
});

it('keeps the saved local preparation failure visible after the attempt is superseded', async () => {
  const { snapshot, attempt } = savedAttempt();
  snapshot.attempts = [attempt];
  attempt.status = 'superseded';
  attempt.endedAt = null;
  snapshot.inspection!.records = snapshot.inspection!.records.filter(item => item.reference.kind !== 'model_response_receipt');
  snapshot.inspection!.attempts[0].explanation = 'Context preparation failed locally for this exact call before provider dispatch. A later preparation has its own attempt. The original attempt record and failure-time control and budget remain saved; no provider failure is inferred.';
  const task = snapshot.tasks.find(item => item.id === attempt.taskId)!;
  task.status = 'running';
  task.waitingReason = null;
  expect(describeAttempt(snapshot, attempt).label).toBe('Local context preparation failed');
  const row = { id: attempt.id, time: attempt.startedAt, timeLabel: 'Attempt started',
    category: 'agent' as const, title: 'Saved attempt', summary: 'Generic historical step',
    attemptId: attempt.id, agentId: attempt.agentId, status: attempt.status };
  expect(activityAction(snapshot, row)).toBe('Local context preparation failed');
  expect(activityScanSummary(snapshot, row)).toMatch(/before a provider request/);

  const user = userEvent.setup();
  render(<ResearchApp snapshot={snapshot} live={{ runId: 'synthetic-run', model: 'saved model', runs: [], error: null,
    form: null, requestedAgentIds: [], onSelectRun: vi.fn() }} />);
  await user.click(screen.getByRole('button', { name: 'Activity' }));
  const card = screen.getByText('Local context preparation failed', { selector: '.activity-item-head > strong' }).closest('li')!;
  expect(within(card).getByText(/before a provider request/)).toBeVisible();
  expect(card.querySelector('.system-action-label')).toHaveTextContent('System · for');
  await expandActivity(user, card);
  await openDisclosure(user, card, 'Technical records and current task state');
  await openDisclosure(user, card, 'Agent settings and recovery');
  expect(within(card).getByText(/failure-time control and budget remain saved/)).toBeVisible();
  const selected = await openExactAttempt(user, card);
  await user.click(within(selected).getByText('Exact identity, timing, model and skills'));
  expect(within(selected).getByText('superseded (saved attempt record: running)')).toBeVisible();
  expect(within(selected).getByText(/before a provider request/)).toBeVisible();
});

it('identifies a failed source-free preparation from its exact saved diagnostic', async () => {
  const { snapshot, attempt } = savedAttempt();
  snapshot.attempts = [attempt];
  attempt.status = 'failed';
  snapshot.inspection!.records = snapshot.inspection!.records.filter(item => item.reference.kind !== 'model_response_receipt');
  snapshot.inspection!.attempts[0].explanation = 'Context preparation failed locally for this exact source-free recovery call before provider dispatch. Its saved diagnostic, failed attempt, call and budget identify this failure; a later recovery has its own attempt.';
  const outcome = describeAttempt(snapshot, attempt);
  expect(outcome.label).toBe('Local model-input preparation failed');
  expect(outcome.summary).toMatch(/before provider dispatch/);
  expect(outcome.summary).toMatch(/no provider failure is inferred/);
  const row = { id: attempt.id, time: attempt.startedAt, timeLabel: 'Attempt finished',
    category: 'agent' as const, title: 'Saved attempt', summary: 'Generic failure',
    attemptId: attempt.id, agentId: attempt.agentId, status: attempt.status };
  expect(activityAction(snapshot, row)).toBe('Local model-input preparation failed');
  expect(activityScanSummary(snapshot, row)).toMatch(/source-free recovery call/);

  const user = userEvent.setup();
  render(<ResearchApp snapshot={snapshot} live={{ runId: 'synthetic-run', model: 'saved model', runs: [], error: null,
    form: null, requestedAgentIds: [], onSelectRun: vi.fn() }} />);
  await user.click(screen.getByRole('button', { name: 'Activity' }));
  const card = screen.getByText('Local model-input preparation failed', { selector: '.activity-item-head > strong' }).closest('li')!;
  expect(within(card).getByText(/source-free recovery call exceeded/)).toBeVisible();
  expect(card.querySelector('.system-action-label')).toHaveTextContent('System · for');
  await expandActivity(user, card);
  await openDisclosure(user, card, 'Technical records and current task state');
  await openDisclosure(user, card, 'Agent settings and recovery');
  expect(within(card).getByText(/saved diagnostic, failed attempt, call and budget/)).toBeVisible();
  expect(within(card).queryByText(/saved attempt record: running/)).not.toBeInTheDocument();
  const selected = await openExactAttempt(user, card);
  await user.click(within(selected).getByText('Exact identity, timing, model and skills'));
  expect(within(selected).getByText('Stored attempt status')).toBeVisible();
  expect(within(selected).queryByText(/saved attempt record: running/)).not.toBeInTheDocument();
});

it('shows a saved local tool result awaiting task acceptance instead of a missing result', async () => {
  const { snapshot, attempt } = savedAttempt();
  snapshot.attempts = [attempt];
  attempt.status = 'running';
  attempt.toolCalls = [{ name: 'read_context_record', status: 'result_unaccepted' }];
  const receipt = snapshot.inspection!.records.find(item => item.reference.kind === 'model_response_receipt')!.data;
  receipt.status = 'tool_requested';
  receipt.http_status = null;
  const outcome = describeAttempt(snapshot, attempt);
  expect(outcome.label).toBe('Open an additional saved task record · result saved, not accepted');
  expect(outcome.badgeStatus).toBe('tool_recovery_needed');
  expect(outcome.summary).toMatch(/local result.*saved, but the task did not accept it/);
  expect(recordedToolAction(attempt.toolCalls[0])).toBe('Open an additional saved task record (result saved; task did not accept)');
  const row = { id: attempt.id, time: attempt.startedAt, timeLabel: 'Attempt started',
    category: 'agent' as const, title: 'Saved attempt', summary: 'Generic pending step',
    attemptId: attempt.id, agentId: attempt.agentId, status: attempt.status };
  expect(activityScanSummary(snapshot, row)).toBe('A local tool result was saved, but the task has not accepted it.');

  const user = userEvent.setup();
  render(<ResearchApp snapshot={snapshot} live={{ runId: 'synthetic-run', model: 'saved model', runs: [], error: null,
    form: null, requestedAgentIds: [], onSelectRun: vi.fn() }} />);
  await user.click(screen.getByRole('button', { name: 'Activity' }));
  const card = screen.getByText('Requested: Open an additional saved task record', { selector: '.activity-item-head > strong' }).closest('li')!;
  expect(card.querySelector('.status-pill')).toHaveTextContent('Saved result needs recovery');
  expect(card.querySelector('.status-pill')).toHaveClass('status-tool-recovery-needed');
  expect(within(card).getByText(/local tool result was saved/)).toBeVisible();
  expect(within(card).queryByText(/requested tool result is not saved/)).not.toBeInTheDocument();

  attempt.toolCalls[0].status = 'acceptance_recorded';
  expect(describeAttempt(snapshot, attempt).label).toBe('Open an additional saved task record · local recovery accepted');
  expect(describeAttempt(snapshot, attempt).summary).toMatch(/context tool result and next model turn are not saved yet/);
  expect(activityScanSummary(snapshot, row)).toMatch(/Local recovery was accepted/);
  expect(recordedToolAction(attempt.toolCalls[0])).toBe('Open an additional saved task record (local recovery accepted; continuation pending)');
});

it('labels only exact projected running attempts as snapshot states', () => {
  const { snapshot, attempt } = savedAttempt();
  snapshot.inspection!.records = snapshot.inspection!.records.filter(item => item.reference.kind !== 'model_response_receipt');
  attempt.status = 'completed';
  snapshot.inspection!.attempts[0].explanation = 'An accepted report names this attempt. Its original status and unknown completion time remain in the saved attempt record.';
  expect(showsOriginalRunningRecord(snapshot, attempt, describeAttempt(snapshot, attempt))).toBe(true);
  snapshot.inspection!.attempts[0].explanation = 'Selected model and exact call inputs were saved.';
  expect(showsOriginalRunningRecord(snapshot, attempt, describeAttempt(snapshot, attempt))).toBe(false);
  attempt.status = 'superseded';
  snapshot.inspection!.attempts[0].explanation = 'No current task control names this call. Its original running status, any captured response, and unknown end time remain in saved records.';
  expect(showsOriginalRunningRecord(snapshot, attempt, describeAttempt(snapshot, attempt))).toBe(true);
  snapshot.inspection!.attempts[0].explanation = 'No current task control names another call.';
  expect(showsOriginalRunningRecord(snapshot, attempt, describeAttempt(snapshot, attempt))).toBe(false);
});

it('separates a recorded model turn from a rejected tool and an accepted report', async () => {
  const { snapshot, attempt } = savedAttempt();
  const receipt = snapshot.inspection!.records.find(item => item.reference.kind === 'model_response_receipt')!.data;
  receipt.http_status = null;
  receipt.status = 'tool_requested';
  attempt.status = 'completed';
  attempt.toolCalls = [{ name: 'search_scholarly', status: 'rejected' }];
  const user = userEvent.setup();
  vi.stubGlobal('fetch', vi.fn(async () => Response.json({ records: [] })));
  render(<ResearchApp snapshot={snapshot} live={{ runId: 'synthetic-run', model: 'saved model', runs: [], error: null, form: null, requestedAgentIds: [], onSelectRun: vi.fn() }} />);
  await user.click(screen.getByRole('button', { name: 'Activity' }));
  const card = screen.getByText('Requested: Scholarly source search', { selector: '.activity-item-head > strong' }).closest('li')!;
  expect(card.querySelector('.activity-item-head .status-pill')).toHaveTextContent('Tool request rejected');
  expect(within(card).queryByText('model turn recorded')).not.toBeInTheDocument();
  await expandActivity(user, card);
  expect(card.querySelector('.step-action')).toHaveTextContent('Requested: Scholarly source search');
  await user.click(within(card).getByText('Technical records and current task state'));
  expect(within(card).getByText('Internal tool name:').parentElement).toHaveTextContent('search_scholarly');
  expect(card.querySelector('.step-facts')).toHaveTextContent('Tool request rejected');
  expect(within(card).queryByText('completed attempt')).not.toBeInTheDocument();
  const selected = await openExactAttempt(user, card);
  expect(selected.querySelector(':scope > p')).toHaveTextContent('Scholarly source search · rejected. Tool step only; no accepted report is linked.');
  expect(selected.querySelector('.selected-step-state time')).toHaveAttribute('dateTime', attempt.endedAt || attempt.startedAt);
  expect(selected.querySelector('.tool-stack .status-pill')).toHaveClass('status-rejected');
  const technical = within(selected).getByText('Exact identity, timing, model and skills').closest('details')!;
  expect(technical).not.toHaveAttribute('open');
  const calls = selected.querySelector('.live-call-inspector')!;
  expect(calls.compareDocumentPosition(technical) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  await user.click(within(selected).getByText('Exact identity, timing, model and skills'));
  expect(selected.querySelector('.panel-heading .status-pill')).toHaveTextContent('tool rejected');
  expect(within(selected).getByText('Stored attempt status')).toBeVisible();
  expect(within(selected).getByText('completed')).toBeVisible();

  snapshot.inspection!.attempts[0].acceptedOutputReferences = [ref('report', 'missing-report')];
  snapshot.inspection!.acceptedOutputReferences = [ref('report', 'missing-report')];
  receipt.status = 'complete';
  attempt.toolCalls = [];
  attempt.resultSummary = 'A saved accepted report.';
  const row = { id: attempt.id, time: attempt.startedAt, timeLabel: 'Attempt finished', category: 'agent' as const, title: 'Saved attempt', summary: attempt.resultSummary, attemptId: attempt.id, agentId: attempt.agentId, status: attempt.status };
  expect(activityOverview(snapshot).acceptedReports).toBeNull();
  expect(describeAttempt(snapshot, attempt).badgeStatus).not.toBe('accepted_report');
  expect(activityAction(snapshot, row)).not.toBe('Saved an accepted report');
  const report = snapshot.artifacts[0];
  snapshot.inspection!.attempts[0].acceptedOutputReferences = [ref('report', report.id, report.version)];
  snapshot.inspection!.acceptedOutputReferences = [ref('report', report.id, report.version)];
  expect(describeAttempt(snapshot, attempt).badgeStatus).toBe('accepted_report');
  expect(activityAction(snapshot, row)).toBe('Saved an accepted report');
  expect(describeAttempt(snapshot, attempt).summary).toBe('A saved accepted report.');
});

it('keeps oversized requests, rejected tools, tool progress and superseded preparation distinct', () => {
  const { snapshot, attempt } = savedAttempt();
  const receipt = snapshot.inspection!.records.find(item => item.reference.kind === 'model_response_receipt')!.data;
  receipt.http_status = 413;
  receipt.retry_after_seconds = 57;
  expect(describeAttempt(snapshot, attempt).label).toBe('Request too large (HTTP 413)');
  expect(describeAttempt(snapshot, attempt).summary).not.toMatch(/rate.limit/i);

  receipt.http_status = 400;
  receipt.error_kind = 'route_used_as_function_name';
  expect(describeAttempt(snapshot, attempt).label).toBe('Search route used as a function name (HTTP 400)');
  receipt.error_kind = 'unregistered_tool_function';
  expect(describeAttempt(snapshot, attempt).label).toBe('Unregistered function requested (HTTP 400)');
  receipt.error_kind = 'http_error';
  expect(describeAttempt(snapshot, attempt).summary).toMatch(/exact cause is not established/);

  receipt.http_status = null;
  receipt.status = 'tool_requested';
  attempt.status = 'completed';
  attempt.toolCalls = [{ name: 'search_scholarly', status: 'rejected' }];
  expect(describeAttempt(snapshot, attempt).label).toBe('Scholarly source search · rejected');
  expect(describeAttempt(snapshot, attempt).summary).toMatch(/No accepted research report/);
  attempt.toolCalls[0].status = 'success';
  expect(describeAttempt(snapshot, attempt).summary).toMatch(/tool step, not an accepted research report/);
  attempt.toolCalls[0].status = 'inspected';
  expect(describeAttempt(snapshot, attempt).summary).toMatch(/saved outcome: inspected/);
  expect(describeAttempt(snapshot, attempt).summary).not.toMatch(/a inspected/);
  attempt.toolCalls[0].status = 'metadata_only';
  expect(describeAttempt(snapshot, attempt).summary).toMatch(/not checked source text/);

  attempt.toolCalls = [{ name: 'read_source_window', status: 'completed' }];
  expect(describeAttempt(snapshot, attempt).label).toBe('Read a saved source excerpt · completed');
  expect(describeAttempt(snapshot, attempt).summary).toMatch(/not an accepted research report/);
  expect(recordedToolAction(attempt.toolCalls[0])).toBe('Read a saved source excerpt (completed)');
  attempt.toolCalls[0].status = 'waiting';
  expect(describeAttempt(snapshot, attempt).label).toBe('Read a saved source excerpt · result not saved');
  expect(describeAttempt(snapshot, attempt).summary).toMatch(/no matching saved host tool result/);
  expect(recordedToolAction(attempt.toolCalls[0])).toBe('Read a saved source excerpt (requested; result not saved)');
  attempt.toolCalls[0].status = 'rejected';
  expect(describeAttempt(snapshot, attempt).label).toBe('Read a saved source excerpt · rejected');
  attempt.toolCalls = [{ name: 'read_source_inventory', status: 'metadata_only' }];
  expect(describeAttempt(snapshot, attempt).label).toBe('List saved sources · metadata only');
  expect(describeAttempt(snapshot, attempt).summary).toMatch(/not checked source text/);
  attempt.toolCalls = [{ name: 'custom_reader', status: 'failed' }];
  expect(recordedToolAction(attempt.toolCalls[0])).toBe('custom_reader (failed)');

  snapshot.inspection!.records = snapshot.inspection!.records.filter(item => item.reference.kind !== 'model_response_receipt');
  snapshot.inspection!.attempts[0].explanation = 'This prepared call was replaced before provider dispatch; its successor has a separate saved attempt.';
  attempt.status = 'superseded';
  attempt.toolCalls = [];
  expect(describeAttempt(snapshot, attempt).label).toBe('Preparation superseded before dispatch');
  expect(describeAttempt(snapshot, attempt).summary).toMatch(/before it was sent/);
});

it('uses singular seconds for one saved retry interval', () => {
  const { snapshot, attempt } = savedAttempt();
  const receipt = snapshot.inspection!.records.find(item => item.reference.kind === 'model_response_receipt')!.data;
  receipt.retry_after_seconds = 1;
  expect(describeAttempt(snapshot, attempt).summary).toMatch(/waiting 1 second before/);
});
