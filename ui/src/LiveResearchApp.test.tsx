/* Verify live HTTP requests and review updates without invoking a model.
   Mocked service responses exercise token bootstrap, start and follow-up
   submissions, polling, call inspection, and immutable comparison. */
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { StrictMode } from 'react';
import type { ResearchSnapshot } from './generated/contracts';
import { LiveResearchApp } from './LiveResearchApp';
import { LiveRequestForm } from './LiveRequestForm';
import { ResearchApp } from './App';
import type { LiveStartRequest, LiveFollowupRequest } from './live';
import sample from '../public/sample-run.json';

const fixture = sample as ResearchSnapshot;
const agents = [
  { id: 'agent-synthesis', name: 'Sage', role: 'Evidence synthesis', skillVersion: 'synthesis-v1' },
  { id: 'agent-critique', name: 'Mira', role: 'Independent critique', skillVersion: 'critique-v1' },
];

function snapshot(version = 1): ResearchSnapshot {
  const source = { ...fixture.sources[0], id: 'source-live', title: 'Pasted observation', copyId: 'source-copy-1', passages: [{ spanId: 'span-live', locator: 'user-pasted text', text: 'Observed response was 8 of 20.' }] };
  const report = { ...fixture.artifacts[0], id: `report-live-v${version}`, kind: 'report', version, body: version === 1 ? 'Initial answer from supplied observation.' : 'Revised answer after independent critique.', citations: [{ sourceId: source.id, spanId: 'span-live' }] };
  const task = { ...fixture.tasks[0], id: 'task-live', title: 'Analyze supplied observation', parentTaskId: null, status: 'completed' as const, attemptIds: ['attempt-live'], targetArtifactId: null };
  const attempt = { ...fixture.attempts[0], id: 'attempt-live', taskId: task.id, agentId: agents[0].id, status: 'completed' as const, resultSummary: 'Sage analyzed the pasted observation.', returnedReasoning: { availability: 'absent' as const, text: null }, toolCalls: [] };
  const peerTask = { ...fixture.tasks[0], id: 'task-peer', title: 'Critique the initial answer', parentTaskId: 'task-live', status: 'completed' as const, attemptIds: ['attempt-peer'], targetArtifactId: 'report-live-v1' };
  const peerAttempt = { ...attempt, id: 'attempt-peer', taskId: peerTask.id, agentId: agents[1].id, resultSummary: 'Mira found a missing limitation.' };
  const message = { ...fixture.messages[0], id: 'message-live', requestId: 'peer-request', taskId: peerTask.id, fromAgentId: agents[0].id, toAgentId: agents[1].id, status: 'fulfilled' as const, summary: 'Mira returned a critique of the initial answer.', sourceId: source.id, claimId: 'claim-live' };
  return {
    ...fixture,
    agents: agents.map(agent => ({ id: agent.id, name: agent.name, role: agent.role, status: 'available' })),
    artifacts: [report],
    attempts: version === 1 ? [attempt] : [attempt, peerAttempt],
    claims: [{ ...fixture.claims[0], id: 'claim-live', taskId: task.id, version, text: version === 1 ? 'Initial claim' : 'Revised claim', supports: [{ sourceId: source.id, spanId: 'span-live' }] }],
    diagnostics: [],
    events: [{ ...fixture.events[0], id: 'event-live', taskId: task.id, summary: 'Live run saved.' }],
    messages: version === 1 ? [] : [message],
    mission: { ...fixture.mission, id: 'mission-live', question: 'What does the pasted observation show?', scope: 'Use only supplied evidence.', reportArtifactId: report.id, version, status: 'active' },
    sampleVersion: 'live-1',
    proofKind: 'live_model_on_supplied_evidence',
    snapshotId: `snapshot-live-v${version}`,
    parentSnapshotId: version === 1 ? null : 'snapshot-live-v1',
    sources: [source],
    tasks: version === 1 ? [task] : [task, peerTask],
  };
}

function liveState(current: ResearchSnapshot | null, comparison: ResearchSnapshot | null = null) {
  return { enabled: true, model: 'openai/gpt-oss-120b', agents, runs: current ? [{ id: current.mission.version === 1 ? 'run-1' : 'run-2', question: current.mission.question, status: 'running', parentRunId: current.mission.version === 1 ? null : 'run-1' }] : [], snapshot: current, comparisonSnapshot: comparison, runId: current ? current.mission.version === 1 ? 'run-1' : 'run-2' : null, error: null };
}

function mockService(initial: ResearchSnapshot | null) {
  let current = initial;
  let comparison: ResearchSnapshot | null = null;
  let sessionCalls = 0;
  const requests: Array<{ path: string; body: Record<string, unknown> }> = [];
  const fetchMock = vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
    const path = String(input);
    if (path === '/api/live/session') { sessionCalls += 1; return Response.json({ authenticated: true }); }
    if (path.startsWith('/api/live/state')) return Response.json(liveState(current, comparison));
    if (path === '/api/live/start') {
      requests.push({ path, body: JSON.parse(String(init?.body)) as Record<string, unknown> });
      current = snapshot(1);
      return Response.json({ requestId: requests.at(-1)!.body.requestId, runId: 'run-1', taskId: 'task-live', status: 'queued', reused: false }, { status: 202 });
    }
    if (path === '/api/live/followups') {
      requests.push({ path, body: JSON.parse(String(init?.body)) as Record<string, unknown> });
      comparison = snapshot(1);
      current = snapshot(2);
      return Response.json({ requestId: requests.at(-1)!.body.requestId, runId: 'run-2', taskId: 'task-peer', status: 'queued', reused: false }, { status: 202 });
    }
    if (path.startsWith('/api/live/calls')) return Response.json({ records: [{ model: 'openai/gpt-oss-120b', messages: [{ role: 'user', content: 'Read supplied evidence' }], content: 'Initial answer', usage: { total_tokens: 20 } }] });
    throw new Error(`Unexpected request ${path}`);
  });
  vi.stubGlobal('fetch', fetchMock);
  return { requests, fetchMock, getSessionCalls: () => sessionCalls, update: (next: ResearchSnapshot) => { current = next; } };
}

function activityCard(agent: string, action: string): HTMLElement {
  const matches = [...document.querySelectorAll<HTMLElement>('.activity-item')].filter(item =>
    item.querySelector('.activity-item-head .agent-badge')?.textContent?.includes(`Agent ${agent}`)
      && item.querySelector('.activity-item-head > strong')?.textContent === action,
  );
  expect(matches).toHaveLength(1);
  return matches[0];
}

afterEach(() => {
  cleanup();
  vi.useRealTimers();
  vi.unstubAllGlobals();
  window.history.replaceState(null, '', '/');
});

describe('live review', () => {
  it('offers the recorded contribution review separately from saved live runs', async () => {
    window.history.replaceState(null, '', '/live?view=history');
    const service = mockService(snapshot(1));
    render(<LiveResearchApp />);
    const examples = await screen.findByRole('region', { name: 'Recorded coordination examples' });
    expect(within(examples).getByRole('link', { name: 'Open shared contribution review' })).toHaveAttribute('href', '/?recorded=stage3&review=contributions');
    expect(within(examples).getByText(/separate from your live runs/)).toBeVisible();
    expect(screen.getByRole('button', { name: 'Open saved run' })).toBeVisible();
    expect(service.requests).toHaveLength(0);
  });

  it.each([['request', 'Start or follow up'], ['history', 'Saved runs']])('opens the requested %s view without submitting work', async (view, title) => {
    window.history.replaceState(null, '', `/live?view=${view}`);
    const service = mockService(snapshot(1));
    render(<LiveResearchApp />);
    expect(await screen.findByRole('heading', { name: title })).toBeVisible();
    expect(service.requests).toHaveLength(0);
  });

  it('does not attach a delivered peer reply to an earlier failed retry', async () => {
    const saved = snapshot(2);
    const base = saved.attempts[1];
    const failed = { ...base, id: 'peer-failed', status: 'failed' as const, startedAt: '2026-10-08T14:00:00Z', endedAt: '2026-10-08T14:00:03Z', resultSummary: 'Peer answer after retry.' };
    const completed = { ...base, id: 'peer-completed', status: 'completed' as const, startedAt: '2026-10-08T14:00:04Z', endedAt: '2026-10-08T14:00:08Z', resultSummary: 'Peer answer after retry.' };
    saved.attempts = [failed, completed];
    saved.tasks[1].attemptIds = [failed.id, completed.id];
    saved.capturedAt = '2026-10-08T14:00:10Z';
    const ask = { ...saved.messages[0], id: 'ask', requestId: 'peer-retry', taskId: 'task-peer', fromAgentId: 'agent-synthesis', toAgentId: 'agent-critique', status: 'fulfilled' as const, summary: 'Please retry the review.', createdAt: '2026-10-08T13:59:59Z' };
    const reply = { ...ask, id: 'reply', fromAgentId: 'agent-critique', toAgentId: 'agent-synthesis', status: 'delivered' as const, summary: completed.resultSummary, createdAt: '2026-10-08T14:00:09Z' };
    saved.messages = [ask, reply];
    const user = userEvent.setup();
    render(<ResearchApp snapshot={saved} live={{ runId: 'run-retry', model: 'test-model', runs: [], error: null, form: null, requestedAgentIds: null, onSelectRun: () => {} }} />);
    await user.click(screen.getByRole('button', { name: 'Activity' }));
    const failedCard = activityCard('Mira', 'failed attempt');
    expect(within(failedCard).queryByText(/Mira responded to Sage/)).not.toBeInTheDocument();
    expect(within(failedCard).getByText(/not its uniquely verified match/)).toBeInTheDocument();
    const completedCard = activityCard('Mira', 'saved result');
    expect(within(completedCard).getByRole('heading', { name: 'Delivered peer reply' })).toBeInTheDocument();
    expect(within(completedCard).getByText('responded to Sage')).toBeInTheDocument();
    expect([...completedCard.querySelectorAll('.activity-attribution p')].filter(item => item.textContent?.includes(reply.summary))).toHaveLength(1);
    await user.click(screen.getByRole('button', { name: 'Messages' }));
    const replyCard = activityCard('Mira', 'responded to Sage');
    expect(within(replyCard).getByText(reply.summary)).toBeInTheDocument();
  });

  it('shows Sage, Mira, then Sage as current attempts with traceable request and reply', async () => {
    const parent = snapshot(1);
    const prior = { ...parent.attempts[0], id: 'attempt-parent', startedAt: '2026-10-08T14:00:00Z', endedAt: '2026-10-08T14:00:10Z' };
    parent.attempts = [prior];
    parent.capturedAt = '2026-10-08T14:00:11Z';
    parent.events = [{ ...parent.events[0], id: 'event-parent', time: '2026-10-08T14:00:10Z', agentId: 'agent-synthesis', summary: 'Agent work completed.' }];
    const current = snapshot(2);
    const sageFirst = { ...prior, id: 'attempt-sage-ask', startedAt: '2026-10-08T14:30:00Z', endedAt: '2026-10-08T14:30:05Z', resultSummary: 'Sage requested an independent check.', toolCalls: [{ name: 'source_read', status: 'completed', provider: null }, { name: 'ask_peer', status: 'completed', provider: null }] };
    const mira = { ...current.attempts[1], id: 'attempt-mira-check', startedAt: '2026-10-08T14:30:05Z', endedAt: '2026-10-08T14:30:13Z', resultSummary: 'The inference exceeds the supplied evidence.', toolCalls: [{ name: 'source_read', status: 'completed', provider: null }, { name: 'publish_result', status: 'completed', provider: null }] };
    const sageResumed = { ...sageFirst, id: 'attempt-sage-resumed', startedAt: '2026-10-08T14:30:13Z', endedAt: '2026-10-08T14:30:33Z', resultSummary: 'Sage revised the answer after Mira replied.', toolCalls: [] };
    current.attempts = [prior, sageFirst, mira, sageResumed];
    current.tasks[0].attemptIds = [prior.id, sageFirst.id, sageResumed.id];
    current.tasks[1].attemptIds = [mira.id];
    current.tasks.push({ ...current.tasks[0], id: 'task-aggregate', title: 'Aggregate request record', attemptIds: [] });
    current.mission.startedAt = '2026-10-08T14:00:00Z';
    current.capturedAt = '2026-10-08T14:30:34Z';
    current.events = [
      ...parent.events,
      { ...parent.events[0], id: 'event-sage-start', time: '2026-10-08T14:30:00Z', summary: 'Agent work started.' },
      { ...parent.events[0], id: 'event-mira-result', time: '2026-10-08T14:30:13Z', agentId: 'agent-critique', summary: 'Agent result published for review.' },
      { ...parent.events[0], id: 'event-sage-done', time: '2026-10-08T14:30:34Z', summary: 'Agent work completed.' },
    ];
    const ask = { ...current.messages[0], id: 'message-ask', requestId: 'request-peer', taskId: 'task-peer', fromAgentId: 'agent-synthesis', toAgentId: 'agent-critique', status: 'fulfilled' as const, summary: 'Please verify the inference.', createdAt: '2026-10-08T14:30:04Z' };
    const reply = { ...ask, id: 'message-reply', fromAgentId: 'agent-critique', toAgentId: 'agent-synthesis', status: 'delivered' as const, summary: 'The inference exceeds the supplied evidence.', createdAt: '2026-10-08T14:30:13Z' };
    current.messages = [ask, reply];
    const user = userEvent.setup();
    render(<ResearchApp snapshot={current} comparisonSnapshot={parent} live={{ runId: 'run-current', model: 'test-model', runs: [], error: null, form: null, requestedAgentIds: ['agent-synthesis'], onSelectRun: () => {} }} />);
    const reviewTimeline = screen.getByRole('group', { name: 'Recorded agent attempts' });
    expect(within(reviewTimeline).getAllByRole('button').map(button => button.querySelector('.contribution-label .agent-badge span:last-child')?.textContent)).toEqual(['Agent Sage', 'Agent Mira', 'Agent Sage']);
    expect(within(reviewTimeline).getByText('<1 min')).toBeInTheDocument();
    expect(within(reviewTimeline).queryByText('+30 min')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Include parent history (1 attempt)' }));
    expect(within(reviewTimeline).getAllByRole('button')).toHaveLength(4);
    expect(screen.getByText(/No recorded work in this branch between/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Hide parent history' }));
    await user.click(within(reviewTimeline).getByRole('button', { name: /Mira; completed/ }));
    const miraCard = screen.getByRole('article', { name: 'Review of Mira · initial attempt' });
    expect(within(miraCard).getByText('Please verify the inference.')).toBeInTheDocument();
    expect(within(miraCard).getAllByText(/The inference exceeds the supplied evidence/).length).toBeGreaterThan(0);
    await user.click(within(reviewTimeline).getByRole('button', { name: /Sage; completed;.*No tool action saved/ }));
    expect(screen.getByRole('article', { name: 'Review of Sage · resumed attempt' })).toHaveTextContent('Sage revised the answer after Mira replied.');
    await user.click(screen.getByRole('button', { name: 'Details' }));
    expect(within(screen.getByRole('group', { name: 'Recorded agent attempts' })).getAllByRole('button')).toHaveLength(3);
    await user.click(screen.getByText('Task and request records without an agent attempt (1)'));
    expect(screen.getByText(/Aggregate request record · no attempt recorded/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Activity' }));
    await user.click(screen.getByRole('button', { name: 'Host events' }));
    expect(screen.getByText('Mira published a result for review.')).toBeInTheDocument();
    expect(screen.getAllByText('Sage completed work.').length).toBeGreaterThan(0);
    await user.click(screen.getByRole('button', { name: 'Agent work' }));
    const miraActivity = activityCard('Mira', 'saved result');
    expect([...miraActivity.querySelectorAll('.activity-attribution p')].filter(item => item.textContent?.includes('The inference exceeds the supplied evidence.'))).toHaveLength(1);
    expect(within(miraActivity).getByRole('heading', { name: 'Peer request' })).toBeInTheDocument();
    expect(within(miraActivity).getByText('asked Mira')).toBeInTheDocument();
    expect(screen.getAllByText('Please verify the inference.').length).toBeGreaterThan(0);
    expect(within(miraActivity).getByRole('heading', { name: 'Delivered peer reply' })).toBeInTheDocument();
    expect(within(miraActivity).getByText('responded to Sage')).toBeInTheDocument();
    expect(screen.getAllByText('The inference exceeds the supplied evidence.').length).toBeGreaterThan(0);
    expect(screen.queryByText('Mira · Please verify the inference.')).not.toBeInTheDocument();
  });

  it('shows the delivered peer answer when the original request is fulfilled', async () => {
    const saved = snapshot(2);
    const original = { ...saved.messages[0], id: 'request-message', requestId: 'peer-request', status: 'fulfilled' as const, fromAgentId: 'agent-synthesis', toAgentId: 'agent-critique', summary: 'Please critique the supplied observation.', createdAt: '2026-10-08T14:00:00Z' };
    const reply = { ...original, id: 'reply-message', status: 'delivered' as const, fromAgentId: 'agent-critique', toAgentId: 'agent-synthesis', summary: 'Mira identified an unsupported inference.', createdAt: '2026-10-08T14:01:00Z' };
    const pending = { ...original, id: 'pending-message', requestId: 'pending-request', status: 'acknowledged' as const, summary: 'A separate critique request was acknowledged.', createdAt: '2026-10-08T14:02:00Z' };
    saved.messages = [original, reply, pending];
    mockService(saved);
    const user = userEvent.setup();
    render(<LiveResearchApp />);
    await screen.findByText('Revised answer after independent critique.');
    await user.click(screen.getByRole('button', { name: 'Communication' }));
    const result = screen.getByRole('region', { name: 'Result' });
    expect(within(result).getByText('Mira identified an unsupported inference.')).toBeInTheDocument();
    expect(within(result).getByText('Reply: Mira → Sage')).toBeInTheDocument();
    expect(result.closest('article')?.querySelector('.panel-heading')).toHaveTextContent('fulfilled');
    const waiting = screen.getByRole('region', { name: 'Current status' });
    expect(waiting.closest('article')?.querySelector('.panel-heading')).toHaveTextContent('acknowledged');
    expect(within(waiting).getByText('A separate critique request was acknowledged.')).toBeInTheDocument();
    expect(within(waiting).getByText('No completed response has been recorded.')).toBeInTheDocument();
  });

  it('keeps the newer command refresh when an older same-run poll returns late', async () => {
    window.history.replaceState(null, '', '/live?runId=run-1');
    const original = snapshot(1);
    const updated = snapshot(1);
    updated.snapshotId = 'snapshot-after-command';
    updated.artifacts[0].body = 'Newly saved answer from the command.';
    let stateReads = 0;
    let resolveOldPoll: ((response: Response) => void) | null = null;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      const path = String(input);
      if (path.startsWith('/api/live/state')) {
        stateReads += 1;
        if (stateReads === 2) return new Promise<Response>(resolve => { resolveOldPoll = resolve; });
        return Response.json(liveState(stateReads === 1 ? original : updated));
      }
      if (path === '/api/live/start') return Response.json({ requestId: 'request-reused', runId: 'run-1', taskId: 'task-live', status: 'queued', reused: true }, { status: 202 });
      throw new Error(`Unexpected request ${path}`);
    }));
    const user = userEvent.setup();
    render(<LiveResearchApp />);
    await screen.findByText('Initial answer from supplied observation.');
    await waitFor(() => expect(stateReads).toBe(2), { timeout: 3000 });
    await user.click(screen.getByRole('button', { name: 'Start or follow up' }));
    await user.type(screen.getByLabelText('Research question or follow-up'), 'Review new evidence.');
    await user.type(screen.getByLabelText('Pasted source evidence'), 'New supplied note.');
    await user.click(screen.getByRole('button', { name: 'Start real run' }));
    await user.click(screen.getByRole('button', { name: 'Research review' }));
    expect(await screen.findByText('Newly saved answer from the command.')).toBeInTheDocument();
    const checkedAfterCommand = document.querySelectorAll('.live-freshness time')[1]?.getAttribute('datetime');
    expect(checkedAfterCommand).toBeTruthy();
    expect(document.querySelectorAll('.live-freshness time')[0]).toHaveAttribute('datetime', updated.capturedAt);
    await new Promise(resolve => setTimeout(resolve, 5));
    await act(async () => { resolveOldPoll!(Response.json(liveState(original))); });
    expect(screen.getByText('Newly saved answer from the command.')).toBeInTheDocument();
    expect(document.querySelectorAll('.live-freshness time')[1]).toHaveAttribute('datetime', checkedAfterCommand);
    expect(document.querySelectorAll('.live-freshness time')[0]).toHaveAttribute('datetime', updated.capturedAt);
  });

  it('keeps the last successful check separate from a later connection failure', async () => {
    window.history.replaceState(null, '', '/live?runId=run-1');
    const current = snapshot(1);
    let stateReads = 0;
    vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
      if (String(input).startsWith('/api/live/state')) {
        stateReads += 1;
        if (stateReads === 1) return Response.json(liveState(current));
        throw new Error('Connection lost');
      }
      throw new Error(`Unexpected request ${input}`);
    }));
    render(<LiveResearchApp />);
    await screen.findByText('Initial answer from supplied observation.');
    const checked = document.querySelectorAll('.live-freshness time')[1]?.getAttribute('datetime');
    expect(checked).toBeTruthy();
    expect(document.querySelectorAll('.live-freshness time')[0]).toHaveAttribute('datetime', current.capturedAt);
    expect(await screen.findByText(/Viewer refresh failed: Connection lost/, {}, { timeout: 3000 })).toBeInTheDocument();
    expect(document.querySelectorAll('.live-freshness time')[1]).toHaveAttribute('datetime', checked);
    expect(document.querySelectorAll('.live-freshness time')[0]).toHaveAttribute('datetime', current.capturedAt);
  });

  it('waits for each state read to finish before polling again and stops after unmount', async () => {
    vi.useFakeTimers();
    const resolveReads: Array<(response: Response) => void> = [];
    const fetchMock = vi.fn(() => new Promise<Response>(resolve => { resolveReads.push(resolve); }));
    vi.stubGlobal('fetch', fetchMock);
    const view = render(<LiveResearchApp />);
    await act(async () => { await Promise.resolve(); });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(4500); });
    expect(fetchMock).toHaveBeenCalledTimes(1);

    await act(async () => { resolveReads[0](Response.json(liveState(null))); });
    await act(async () => { await vi.advanceTimersByTimeAsync(1499); });
    expect(fetchMock).toHaveBeenCalledTimes(1);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(fetchMock).toHaveBeenCalledTimes(2);

    view.unmount();
    await act(async () => { resolveReads[1](Response.json(liveState(null))); });
    await act(async () => { await vi.advanceTimersByTimeAsync(4500); });
    expect(fetchMock).toHaveBeenCalledTimes(2);
  });

  it('retries the exact frozen follow-up after the visible run advances', async () => {
    const user = userEvent.setup();
    const onSubmit = vi.fn(async (_request: LiveStartRequest | LiveFollowupRequest) => {});
    const firstProps = { agents, snapshot: snapshot(1), runId: 'run-1', receipt: null, receiptAgentIds: [], submitting: false, error: null, onSubmit };
    const { rerender } = render(<LiveRequestForm {...firstProps} />);
    await user.click(screen.getByRole('button', { name: 'Follow up on exact work' }));
    await user.type(screen.getByLabelText('Research question or follow-up'), 'Critique the saved report.');
    await user.selectOptions(screen.getByLabelText('Exact saved target'), 'report-live-v1:1');
    await user.click(screen.getByRole('button', { name: 'Submit exact follow-up' }));
    const firstRequest = onSubmit.mock.calls[0][0];
    expect(firstRequest).toMatchObject({ parentRunId: 'run-1', parentSnapshotId: 'snapshot-live-v1', targetId: 'report-live-v1', targetVersion: 1 });

    rerender(<LiveRequestForm {...firstProps} snapshot={snapshot(2)} runId="run-2" error="Previous response uncertain" />);
    expect(screen.getByText(/Parent snapshot snapshot-live-v1 · pending retry remains pinned/)).toBeInTheDocument();
    expect(screen.getByLabelText('Exact saved target')).toHaveValue('report-live-v1:1');
    await user.click(screen.getByRole('button', { name: 'Retry same request' }));
    expect(onSubmit).toHaveBeenCalledTimes(2);
    expect(onSubmit.mock.calls[1][0]).toEqual(firstRequest);

    await user.type(screen.getByLabelText('Research question or follow-up'), ' Recheck.');
    await user.selectOptions(screen.getByLabelText('Exact saved target'), 'report-live-v2:2');
    await user.click(screen.getByRole('button', { name: 'Submit exact follow-up' }));
    expect(onSubmit.mock.calls[2][0]).toMatchObject({ parentRunId: 'run-2', parentSnapshotId: 'snapshot-live-v2', targetId: 'report-live-v2', targetVersion: 2 });
    expect(onSubmit.mock.calls[2][0].requestId).not.toBe(firstRequest.requestId);
  });

  it('exchanges the launch token once before state reads, including in Strict Mode', async () => {
    window.history.replaceState(null, '', '/live#token=one-time-launch');
    const service = mockService(null);
    render(<StrictMode><LiveResearchApp /></StrictMode>);
    await screen.findByRole('heading', { name: 'Start a research task' });
    expect(service.getSessionCalls()).toBe(1);
    expect(service.fetchMock.mock.calls[0][0]).toBe('/api/live/session');
    expect(JSON.parse(String(service.fetchMock.mock.calls[0][1]?.body))).toEqual({ token: 'one-time-launch' });
    expect(window.location.hash).toBe('');
  });

  it('shows a live connection error without loading a scripted sample', async () => {
    vi.stubGlobal('fetch', vi.fn(async () => Response.json({ error: 'Session required' }, { status: 401 })));
    render(<LiveResearchApp />);
    expect(await screen.findByRole('heading', { name: 'Live service unavailable' })).toBeInTheDocument();
    expect(screen.getByText('Session required')).toBeInTheDocument();
    expect(screen.queryByText('SCRIPTED SAMPLE')).not.toBeInTheDocument();
  });

  it('submits pasted evidence and selected peers, then refreshes real records across views', async () => {
    const service = mockService(null);
    const user = userEvent.setup();
    render(<LiveResearchApp />);
    await screen.findByRole('heading', { name: 'Start a research task' });
    await user.type(screen.getByLabelText('Research question or follow-up'), 'What does this observation show?');
    await user.type(screen.getByLabelText('Scope and constraints'), 'Use the supplied text.');
    await user.type(screen.getByLabelText('Pasted source evidence'), 'Observed response was 8 of 20.');
    await user.click(screen.getByRole('checkbox', { name: /Mira/ }));
    await user.click(screen.getByRole('button', { name: 'Start real run' }));
    await screen.findByText('Initial answer from supplied observation.');
    expect(service.requests[0].path).toBe('/api/live/start');
    expect(service.requests[0].body).toMatchObject({ question: 'What does this observation show?', scope: 'Use the supplied text.', evidenceText: 'Observed response was 8 of 20.', agentIds: ['agent-synthesis', 'agent-critique'] });
    expect(screen.getByText('LIVE · SUPPLIED EVIDENCE')).toBeInTheDocument();
    expect(screen.queryByText('SCRIPTED SAMPLE')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Activity' }));
    expect(activityCard('Sage', 'saved result')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Evidence' }));
    expect(screen.getByText('Observed response was 8 of 20.')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Details' }));
    await user.click(screen.getByRole('button', { name: 'Attempt records' }));
    expect(await screen.findByText(/Read supplied evidence/)).toBeInTheDocument();
    expect(service.fetchMock).toHaveBeenCalledWith(expect.stringContaining('/api/live/calls?runId=run-1&attemptId=attempt-live'), expect.objectContaining({ credentials: 'same-origin' }));
    const refreshed = snapshot(1);
    refreshed.snapshotId = 'snapshot-live-refreshed';
    refreshed.artifacts[0].body = 'Saved answer refreshed after another model call.';
    service.update(refreshed);
    await user.click(screen.getByRole('button', { name: 'Research review' }));
    expect(await screen.findByText('Saved answer refreshed after another model call.', {}, { timeout: 3000 })).toBeInTheDocument();
  });

  it('submits an exact saved target and compares the immutable parent with new peer work', async () => {
    const service = mockService(snapshot(1));
    const user = userEvent.setup();
    render(<LiveResearchApp />);
    await screen.findByText('Initial answer from supplied observation.');
    await waitFor(() => expect(window.location.search).toBe('?runId=run-1'));
    await user.click(screen.getByRole('button', { name: 'Start or follow up' }));
    await user.click(screen.getByRole('button', { name: 'Follow up on exact work' }));
    await user.type(screen.getByLabelText('Research question or follow-up'), 'Critique this report.');
    await user.selectOptions(screen.getByLabelText('Exact saved target'), 'report-live-v1:1');
    await user.click(screen.getByRole('button', { name: 'Submit exact follow-up' }));
    await waitFor(() => expect(service.requests).toHaveLength(1));
    expect(service.requests[0]).toMatchObject({ path: '/api/live/followups', body: { parentRunId: 'run-1', parentSnapshotId: 'snapshot-live-v1', targetId: 'report-live-v1', targetVersion: 1, question: 'Critique this report.', agentIds: ['agent-synthesis'] } });
    await user.click(screen.getByRole('button', { name: 'Compare' }));
    expect(await screen.findByText('Revised answer after independent critique.')).toBeInTheDocument();
    expect(window.location.search).toBe('?runId=run-2');
    const comparison = screen.getByRole('heading', { name: 'Compare' }).closest<HTMLElement>('.page-stack')!;
    expect(within(comparison).getByText('Initial answer from supplied observation.')).toBeInTheDocument();
    expect(within(comparison).getByText('Immutable parent snapshot')).toBeInTheDocument();
    expect(within(comparison).getByText('Current follow-up snapshot')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Communication' }));
    expect(screen.getAllByText('Mira returned a critique of the initial answer.').length).toBeGreaterThan(0);
    await user.click(screen.getByRole('button', { name: 'Evidence' }));
    expect(screen.getByText('Revised claim')).toBeInTheDocument();
  });
});
