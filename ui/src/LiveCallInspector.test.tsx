/* Verify readable call provenance from saved IDs and exact peer replies. */
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import type { ResearchSnapshot } from './generated/contracts';
import { LiveCallInspector } from './LiveCallInspector';
import { getLiveCalls } from './live';
import sample from '../public/sample-run.json';

vi.mock('./live', () => ({ getLiveCalls: vi.fn() }));

const saved = { ...sample,
  agents: [
    { id: 'worker-a', name: 'Sage', role: 'synthesis', status: 'available' },
    { id: 'worker-b', name: 'Mira', role: 'critique', status: 'available' },
  ],
  attempts: [{ ...sample.attempts[0], id: 'attempt-peer', agentId: 'worker-b', taskId: 'peer-task', resultSummary: 'Exact peer assessment', endedAt: '2026-10-08T11:00:00Z' }],
  messages: [
    { ...sample.messages[0], id: 'request-message', requestId: 'req-1', fromAgentId: 'worker-a', toAgentId: 'worker-b', taskId: 'peer-task', status: 'fulfilled', summary: 'Check the answer.' },
    { ...sample.messages[0], id: 'reply-message', requestId: 'req-1', fromAgentId: 'worker-b', toAgentId: 'worker-a', taskId: 'peer-task', status: 'delivered', summary: 'Exact peer assessment' },
  ],
} as ResearchSnapshot;

const baseCall = { id: 'call-1', runId: 'run-1', attemptId: 'attempt-sage', agentId: 'worker-a', status: 'returned', phase: 'response', createdAt: '2026-10-08T12:00:00Z', completedAt: '2026-10-08T12:00:01Z', messages: [], response: { content: 'Sage revised answer', model: 'openai/gpt-oss-120b', tool_calls: [] }, toolResults: [] };

afterEach(() => { cleanup(); vi.resetAllMocks(); });

it('names the author and links only an exact saved peer reply to its attempt', async () => {
  const user = userEvent.setup();
  const open = vi.fn();
  vi.mocked(getLiveCalls).mockResolvedValue({ records: [{ ...baseCall, messages: [{ role: 'user', content: JSON.stringify({ peerRequestId: 'req-1', peerResponse: 'Exact peer assessment' }) }] }] });
  render(<LiveCallInspector runId="run-1" attemptId="attempt-sage" snapshotId="snapshot-1" snapshot={saved} onOpenAttempt={open} />);
  expect(await screen.findByRole('heading', { name: 'Response from Agent Sage' })).toBeInTheDocument();
  expect(screen.getByText('Sage revised answer')).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: 'Agent Mira reply used by Agent Sage' })).toBeInTheDocument();
  expect(screen.getByText('Exact peer assessment')).toBeInTheDocument();
  expect(screen.getByText('Saved peer request req-1')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Open Mira attempt' }));
  expect(open).toHaveBeenCalledWith('attempt-peer');
  expect(screen.getByText('Full raw call JSON')).toBeInTheDocument();
});

it('uses the backend trace for author, exact output path and peer record pointers', async () => {
  const user = userEvent.setup();
  const open = vi.fn();
  vi.mocked(getLiveCalls).mockResolvedValue({ records: [{ ...baseCall, messages: [], trace: {
    basis: 'linked_saved_records',
    author: { agentId: 'worker-a', name: 'Canonical Sage', role: 'synthesis', taskId: 'task-a', attemptId: 'attempt-sage', runId: 'run-1' },
    outputs: [{ authoredByAgentId: 'worker-a', recordPath: 'response.content' }],
    peerInputs: [{ inputRecordPath: 'messages[0].content', peerRequestId: 'req-1', peerResponse: 'Exact peer assessment', linkStatus: 'linked', requestMessageId: 'request-message', responseMessageId: 'reply-message', peerAgentId: 'worker-b', peerAgentName: 'Canonical Mira', sourceAttemptId: 'attempt-peer', sourceAttemptResolution: 'unique' }],
  } }] });
  render(<LiveCallInspector runId="run-1" attemptId="attempt-sage" snapshotId="snapshot-1" snapshot={saved} onOpenAttempt={open} />);
  expect(await screen.findByRole('heading', { name: 'Response from Agent Canonical Sage' })).toBeInTheDocument();
  expect(screen.getByText('Saved output: response.content')).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: 'Agent Canonical Mira reply used by Agent Canonical Sage' })).toBeInTheDocument();
  expect(screen.getByText('Request record request-message · reply record reply-message')).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Open Canonical Mira attempt' }));
  expect(open).toHaveBeenCalledWith('attempt-peer');
});

it('does not infer a peer link from names in unrelated response text', async () => {
  vi.mocked(getLiveCalls).mockResolvedValue({ records: [{ ...baseCall, response: { ...baseCall.response, content: 'Mira is an unrelated name here.' } }] });
  render(<LiveCallInspector runId="run-1" attemptId="attempt-sage" snapshotId="snapshot-1" snapshot={saved} />);
  expect(await screen.findByText('Mira is an unrelated name here.')).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: /reply used by/ })).not.toBeInTheDocument();
});

it('shows a quota wait without inventing a response and links the saved retry', async () => {
  vi.mocked(getLiveCalls).mockResolvedValue({ records: [
    { ...baseCall, status: 'rate_limited', phase: 'quota_wait', response: { error: 'no retry was made' } },
    { ...baseCall, id: 'call-2', retryOfCallId: 'call-1', response: { ...baseCall.response, content: 'Later answer' } },
  ] });
  render(<LiveCallInspector runId="run-1" attemptId="attempt-sage" snapshotId="snapshot-1" snapshot={saved} />);
  const articles = await screen.findAllByRole('article');
  expect(within(articles[0]).getByText('Quota wait: no model response was returned for this call.')).toBeInTheDocument();
  expect(within(articles[0]).queryByRole('heading', { name: 'Response from Agent Sage' })).not.toBeInTheDocument();
  expect(within(articles[0]).getByText('Retried in Call 2; inspect that call for its separate outcome.')).toBeInTheDocument();
  expect(within(articles[1]).getByText('Later answer')).toBeInTheDocument();
});
