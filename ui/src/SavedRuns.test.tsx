import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { SavedRuns } from './SavedRuns';
import type { LiveRunSummary } from './live';

afterEach(cleanup);

it('explains an operator pause once while retaining each peer’s saved wait detail', () => {
  const reason = 'Backup credential paused by operator; Backup credential paused by operator';
  render(<SavedRuns runs={[
    { id: 'held', question: 'Two-peer review', status: 'active', waitingReason: reason },
    { id: 'mixed', question: 'Other work', status: 'active', waitingReason: 'Backup credential paused by operator; Awaiting peer reply' },
  ]} selectedRunId={null} onOpen={vi.fn()} />);
  const held = screen.getByRole('article', { name: 'Saved run Two-peer review' });
  expect(within(held).getByText('Waiting: Research calls are paused for the Backup profile. Saved work is retained.')).toBeInTheDocument();
  expect(within(held).getByText(`Saved wait detail: ${reason}`)).toBeInTheDocument();
  expect(within(screen.getByRole('article', { name: 'Saved run Other work' })).getByText(/Awaiting peer reply/)).toBeInTheDocument();
});

it('shows a repeated quota-binding wait once in the run list while retaining the exact saved detail', () => {
  const reason = 'Saved Backup quota binding changed; this run needs an explicit transition before more model work';
  const saved = `${reason}; ${reason}`;
  render(<SavedRuns runs={[{ id: 'held', question: 'Independent review', status: 'waiting', waitingReason: saved }]}
    selectedRunId={null} onOpen={vi.fn()} />);
  const run = screen.getByRole('article', { name: 'Saved run Independent review' });
  expect(within(run).getByText(`Waiting: ${reason}`)).toBeInTheDocument();
  expect(within(run).getByText(`Saved wait detail: ${saved}`)).toBeInTheDocument();
});

it('keeps the distinguishing question available when saved titles share a shortened prefix', () => {
  const prefix = 'Find the published trial abstract. ';
  const question = `${prefix}Does it report an allocation seed?`;
  render(<SavedRuns runs={[
    { id: 'first', title: `${prefix.trim()}…`, question, status: 'active' },
    { id: 'second', title: 'Comparator check', question: `${prefix}Which treatments were compared?`, status: 'completed' },
  ]} selectedRunId={null} onOpen={vi.fn()} />);
  expect(screen.getByRole('heading', { name: question })).toHaveAttribute('title', question);
  expect(screen.getByRole('heading', { name: 'Comparator check' })).toBeInTheDocument();
});

it('finds duplicate questions by saved time, status, peer and parent without inventing legacy dates', async () => {
  const user = userEvent.setup();
  const open = vi.fn();
  const runs: LiveRunSummary[] = [
    { id: 'parent', question: 'Same question?', status: 'completed', agentIds: ['sage'], requestedAgentNames: { sage: 'Historical Sage' }, participationPolicyVersion: 2, continuingTaskIds: ['task-independent'], createdAt: '2026-10-08T12:00:00Z', lastProgressAt: '2026-10-08T13:00:00Z' },
    { id: 'followup', question: 'Same question?', status: 'waiting', agentIds: ['mira'], requiredAgentIds: ['mira'], participationPolicyVersion: 1, requestedAgentNames: { mira: 'Mira' }, parentRunId: 'parent', createdAt: '2026-10-09T12:00:00Z', lastProgressAt: '2026-10-09T12:02:00Z', waitingReason: 'Awaiting peer reply' },
    { id: 'legacy', question: 'Older question', status: 'running', agentIds: [] },
  ];
  const props = { runs, selectedRunId: 'parent', workspaceId: 'history-test', workspaceLabel: 'Local review', agentNames: { sage: 'Current peer label' }, onOpen: open };
  window.sessionStorage.removeItem('atlas-saved-runs:history-test');
  const view = render(<SavedRuns {...props} />);
  expect(screen.getByText(/Workspace: Local review/)).toBeInTheDocument();
  expect(screen.getAllByText(/Historical Sage/).length).toBeGreaterThan(0);
  const cards = screen.getAllByRole('article');
  expect(within(cards[0]).getByText(/Awaiting peer reply/)).toBeInTheDocument();
  expect(within(cards[0]).getByText(/Required before scope completion: Mira/)).toBeInTheDocument();
  expect(within(cards[0]).getByRole('button', { name: 'Same question?' })).toBeInTheDocument();
  expect(within(cards[1]).getByText(/Requested scope complete/)).toBeInTheDocument();
  expect(within(cards[1]).getByText(/1 continuing task at last saved progress/)).toBeInTheDocument();
  expect(within(cards[2]).getAllByText(/Not recorded/).length).toBeGreaterThan(0);
  expect(within(cards[2]).getByText(/Completion requirements were not recorded|Not recorded for this older run/)).toBeInTheDocument();
  await user.selectOptions(screen.getByLabelText('Status'), 'waiting');
  expect(screen.getAllByRole('article')).toHaveLength(1);
  view.unmount();
  render(<SavedRuns {...props} />);
  expect(screen.getByLabelText('Status')).toHaveValue('waiting');
  await user.selectOptions(screen.getByLabelText('Status'), 'all');
  await user.selectOptions(screen.getByLabelText('Requested peer'), 'sage');
  expect(screen.getAllByRole('article')).toHaveLength(1);
  await user.click(screen.getByRole('button', { name: 'Open saved run' }));
  expect(open).toHaveBeenCalledWith('parent');
  await user.selectOptions(screen.getByLabelText('Requested peer'), 'all');
  await user.type(screen.getByLabelText('Search question or run'), 'Older');
  expect(screen.getAllByRole('article')).toHaveLength(1);
  expect(open).toHaveBeenCalledTimes(1);
  window.sessionStorage.removeItem('atlas-saved-runs:history-test');
});

it('makes an empty date filter explicit and clears filters without opening a run', async () => {
  const user = userEvent.setup();
  const open = vi.fn();
  render(<SavedRuns runs={[
    { id: 'first', question: 'Earlier run', status: 'completed', createdAt: '2026-10-08T12:00:00Z' },
    { id: 'second', question: 'Later run', status: 'active', createdAt: '2026-10-09T12:00:00Z' },
  ]} selectedRunId={null} onOpen={open} />);
  expect(screen.getByText('Any date — no date filter applied')).toBeInTheDocument();
  const date = screen.getByLabelText('Created date', { exact: false });
  // Native date inputs can display a date placeholder despite their empty value.
  expect(date).toHaveValue('');
  fireEvent.change(date, { target: { value: '2026-10-09' } });
  expect(screen.getAllByRole('article')).toHaveLength(1);
  expect(screen.getByRole('heading', { name: 'Later run' })).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Clear filters' }));
  expect(date).toHaveValue('');
  expect(screen.getAllByRole('article')).toHaveLength(2);
  expect(open).not.toHaveBeenCalled();
});

it('explains a generic saved model failure and preserves each run’s own historical peer name', () => {
  const alias = 'agent-literature-acquisition';
  render(<SavedRuns runs={[
    { id: 'saved-peer', question: 'Historical task', status: 'active', agentIds: [alias],
      requestedAgentNames: { [alias]: 'Literature acquisition' },
      waitingReason: 'pending_model has failed external effect' },
    { id: 'older-unknown', question: 'Older task', status: 'active', agentIds: [alias],
      waitingReason: 'StateError: this branch needs local repair' },
  ]} selectedRunId={null} agentNames={{ [alias]: 'Nova' }} onOpen={vi.fn()} />);
  const saved = screen.getByRole('article', { name: 'Saved run Historical task' });
  expect(within(saved).getByText(/Requested peers: Literature acquisition/)).toBeInTheDocument();
  expect(within(saved).getByText(/Waiting: A model step failed/)).toBeInTheDocument();
  expect(within(saved).getByText('Saved wait detail: pending_model has failed external effect')).toBeInTheDocument();
  expect(within(saved).queryByText(/HTTP 400/)).not.toBeInTheDocument();
  const unknown = screen.getByRole('article', { name: 'Saved run Older task' });
  expect(within(unknown).getByText(/Requested peers: Name not recorded/)).toBeInTheDocument();
  expect(within(unknown).getByText(/Waiting: The host could not complete a local step/)).toBeInTheDocument();
  expect(within(unknown).getByText('Saved wait detail: StateError: this branch needs local repair')).toBeInTheDocument();
  expect(within(unknown).queryByText(/Nova/)).not.toBeInTheDocument();
});
