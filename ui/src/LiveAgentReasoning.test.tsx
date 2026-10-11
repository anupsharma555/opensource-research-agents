/* Check the authenticated reasoning disclosure using synthetic saved call data. */
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { ResearchApp } from './App';
import { LiveAgentReasoning } from './LiveAgentReasoning';
import { getLiveCalls, getLiveReasoning } from './live';
import type { ResearchSnapshot } from './generated/contracts';
import sample from '../public/sample-run.json';

vi.mock('./live', () => ({ getLiveCalls: vi.fn(), getLiveReasoning: vi.fn() }));

const saved = structuredClone(sample) as ResearchSnapshot;
saved.agents[0].role = 'evidence preparation synthesis';
saved.attempts = [{ ...saved.attempts[0], id: 'attempt-a', agentId: saved.agents[0].id,
  returnedReasoning: { availability: 'restricted' }, toolCalls: [{ name: 'search_scholarly', status: 'completed' }] }];
const agentId = saved.attempts[0].agentId;
const call = { id: 'call-a', runId: 'run-a', attemptId: 'attempt-a', agentId,
  status: 'returned', createdAt: '2026-10-08T12:00:00Z', completedAt: '2026-10-08T12:00:01Z',
  response: { content: 'Synthetic final answer', tool_calls: [{ id: 'tool-a', function: { name: 'search_scholarly', arguments: 'Private synthetic arguments' } }] },
  returnedReasoning: { availability: 'restricted' } };

afterEach(() => { cleanup(); vi.resetAllMocks(); });

it('puts actual provider-returned text under its named call, apart from answers and peer messages', async () => {
  const user = userEvent.setup();
  const parentCall = { ...call, runId: 'run-parent' };
  vi.mocked(getLiveCalls).mockResolvedValue({ records: [parentCall, { ...parentCall, id: 'call-b', response: { content: null, tool_calls: [{ id: 'tool-b', function: { name: 'inspect_hit', arguments: '{}' } }] }, returnedReasoning: { availability: 'absent' } }] });
  vi.mocked(getLiveReasoning).mockResolvedValue({ runId: 'run-parent', callId: 'call-a', attemptId: 'attempt-a', agentId,
    status: 'available', origin: 'provider_returned', text: 'Synthetic provider-only reasoning.', redacted: false });
  render(<LiveAgentReasoning runId="run-a" snapshot={saved} />);
  const panel = screen.getByRole('region', { name: 'Agent reasoning' });
  const outer = within(panel).getByText('Agent reasoning · 1 saved work step').closest('details')!;
  expect(outer).not.toHaveAttribute('open');
  expect(getLiveCalls).not.toHaveBeenCalled();
  await user.click(within(panel).getByText('Agent reasoning · 1 saved work step'));
  expect(outer).toHaveAttribute('open');
  expect(within(panel).getByText(/separate from the answer and from messages delivered to peers/)).toBeVisible();
  const reasoningHelp = within(panel).getByRole('button', { name: 'What does model reasoning mean?' });
  await user.hover(reasoningHelp);
  expect(screen.getByRole('tooltip')).toHaveTextContent('actually returned by the selected model');
  expect(getLiveCalls).not.toHaveBeenCalled();
  const attemptSummary = within(panel).getByText(/Work step 1/);
  expect(attemptSummary).toHaveTextContent('Saved model reasoning available');
  expect(attemptSummary).not.toHaveTextContent('attempt-a');
  await user.click(attemptSummary);
  expect(getLiveCalls).toHaveBeenCalledWith('run-a', 'attempt-a');
  const attemptIdentifiers = within(panel).getByText('Exact attempt identifiers').closest('details')!;
  expect(attemptIdentifiers).toHaveTextContent('attempt-a');
  expect(within(panel).getByText(/Role focus:/).closest('p')).toHaveTextContent('Prepare source evidence and synthesize findings');
  expect(within(panel).getByText(/Saved task assignment:/).closest('p')).toHaveTextContent(saved.tasks.find(task => task.id === saved.attempts[0].taskId)!.title);
  const assignmentHelp = within(panel).getByRole('button', { name: 'What does task assignment mean?' });
  await user.unhover(reasoningHelp);
  await user.hover(assignmentHelp);
  expect(screen.getByRole('tooltip')).toHaveTextContent('does not establish that the first agent sent it a message');
  expect(within(panel).getByText(/Search or fetch scholarly records · saved outcome: completed/)).toBeInTheDocument();
  const firstCall = (await within(panel).findByText(/Model call 1/)).closest('li')!;
  expect(within(firstCall).getByText(/Model call 1/)).not.toHaveTextContent('call-a');
  expect(within(firstCall).getByText(/Oct 8/)).toBeInTheDocument();
  expect(within(firstCall).getByText(/Call returned/)).toBeInTheDocument();
  expect(within(firstCall).getByText('Exact saved identifiers and times')).toBeInTheDocument();
  expect(within(firstCall).getByText(/Saved model reasoning available/)).toBeInTheDocument();
  expect(within(firstCall).getByText(/Model requested:/).closest('p')).toHaveTextContent('Search or fetch scholarly records');
  await user.click(within(firstCall).getByText('Exact saved identifiers and times'));
  expect(within(firstCall).getByText('call-a')).toBeInTheDocument();
  expect(within(firstCall).getByText('search_scholarly')).toBeInTheDocument();
  expect(within(panel).getByText(/Inspect a saved source hit/)).toBeInTheDocument();
  expect(getLiveReasoning).not.toHaveBeenCalled();
  expect(within(panel).queryByText('Synthetic final answer')).not.toBeInTheDocument();
  expect(within(panel).queryByText('Private synthetic arguments')).not.toBeInTheDocument();
  expect(within(panel).getAllByText('No provider reasoning text is available for this call.')).toHaveLength(1);
  await user.click(within(panel).getByRole('button', { name: 'Show saved provider reasoning' }));
  expect(await within(panel).findByText('Synthetic provider-only reasoning.')).toBeInTheDocument();
  expect(getLiveReasoning).toHaveBeenCalledWith('run-parent', 'call-a');
  expect(within(panel).queryByText('Synthetic final answer')).not.toBeInTheDocument();
});

it('keeps a saved unavailable status separate from actual provider text', async () => {
  const user = userEvent.setup();
  vi.mocked(getLiveCalls).mockResolvedValue({ records: [call] });
  vi.mocked(getLiveReasoning).mockResolvedValue({ runId: 'run-a', callId: 'call-a', attemptId: 'attempt-a', agentId,
    status: 'unavailable', reason: 'saved_reasoning_unavailable', text: 'Not an available provider excerpt.' });
  render(<LiveAgentReasoning runId="run-a" snapshot={saved} />);
  await user.click(screen.getByText('Agent reasoning · 1 saved work step'));
  await user.click(screen.getByText(/Work step 1/));
  await screen.findByText(/Model call 1/);
  await user.click(screen.getByRole('button', { name: 'Show saved provider reasoning' }));
  expect(await screen.findByText('No provider reasoning text is available for this call.')).toBeInTheDocument();
  expect(screen.queryByText('Not an available provider excerpt.')).not.toBeInTheDocument();
});

it('labels a saved excerpt incomplete and rejects text from a mismatched call identity', async () => {
  const user = userEvent.setup();
  vi.mocked(getLiveCalls).mockResolvedValue({ records: [{ ...call, returnedReasoning: { availability: 'truncated' } }] });
  vi.mocked(getLiveReasoning)
    .mockResolvedValueOnce({ runId: 'other-run', callId: 'call-a', attemptId: 'attempt-a', agentId,
      status: 'available', origin: 'provider_returned', text: 'Wrong run text.' })
    .mockResolvedValueOnce({ runId: 'run-a', callId: 'call-a', attemptId: 'attempt-a', agentId,
      status: 'available', origin: 'provider_returned', reason: 'truncated', text: 'Synthetic incomplete excerpt.', redacted: true });
  render(<LiveAgentReasoning runId="run-a" snapshot={saved} />);
  await user.click(screen.getByText('Agent reasoning · 1 saved work step'));
  await user.click(screen.getByText(/Work step 1/));
  expect(screen.getByText(/Partial saved reasoning/)).toBeInTheDocument();
  await screen.findByText(/Model call 1/);
  await user.click(screen.getByRole('button', { name: 'Show saved provider reasoning' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('could not be read');
  expect(screen.queryByText('Wrong run text.')).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Retry saved provider reasoning' }));
  expect(await screen.findByText('Synthetic incomplete excerpt.')).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: 'Provider-returned reasoning · incomplete excerpt' })).toBeInTheDocument();
  expect(screen.getByText(/Credential-shaped material was redacted/)).toBeInTheDocument();
});

it('uses chronological attempt numbers while keeping the newest work first', async () => {
  const user = userEvent.setup();
  const twoAttempts = structuredClone(saved);
  twoAttempts.attempts = [
    { ...saved.attempts[0], id: 'later-attempt', startedAt: '2026-10-09T12:00:00Z', returnedReasoning: { availability: 'absent' } },
    { ...saved.attempts[0], id: 'earlier-attempt', startedAt: '2026-10-08T12:00:00Z' },
  ];
  render(<LiveAgentReasoning runId="run-a" snapshot={twoAttempts} />);
  await user.click(screen.getByText('Agent reasoning · 2 saved work steps'));
  const summaries = screen.getAllByText(/Work step [12]/);
  expect(summaries[0]).toHaveTextContent('Work step 2');
  expect(summaries[0]).toHaveTextContent('No saved reasoning text indicated');
  expect(summaries[0]).not.toHaveTextContent('later-attempt');
  expect(summaries[1]).toHaveTextContent('Work step 1');
  expect(summaries[1]).toHaveTextContent('Saved model reasoning available');
});

it('keeps a historical saved agent name instead of substituting a new roster default', async () => {
  const user = userEvent.setup();
  const historical = structuredClone(saved);
  historical.agents[0].name = 'Mira';
  render(<LiveAgentReasoning runId="run-a" snapshot={historical} />);
  await user.click(screen.getByText('Agent reasoning · 1 saved work step'));
  expect(screen.getByText(/Work step 1/)).toHaveTextContent('Agent Mira');
  expect(screen.queryByText('Agent Mxra')).not.toBeInTheDocument();
});

it('keeps an explicitly opened call readable during a same-run snapshot check and clears it on a run change', async () => {
  const user = userEvent.setup();
  vi.mocked(getLiveCalls).mockResolvedValueOnce({ records: [call] })
    .mockImplementationOnce(() => new Promise(() => {}))
    .mockImplementationOnce(() => new Promise(() => {}));
  vi.mocked(getLiveReasoning).mockResolvedValue({ runId: 'run-a', callId: 'call-a', attemptId: 'attempt-a', agentId,
    status: 'available', origin: 'provider_returned', text: 'Synthetic exact text.', redacted: false });
  const view = render(<LiveAgentReasoning runId="run-a" snapshot={saved} />);
  await user.click(screen.getByText('Agent reasoning · 1 saved work step'));
  await user.click(screen.getByText(/Work step 1/));
  await screen.findByText(/Model call 1/);
  await user.click(screen.getByRole('button', { name: 'Show saved provider reasoning' }));
  expect(await screen.findByText('Synthetic exact text.')).toBeVisible();

  view.rerender(<LiveAgentReasoning runId="run-a" snapshot={{ ...saved, snapshotId: 'newer-snapshot' }} />);
  expect(screen.getByText('Synthetic exact text.')).toBeVisible();
  expect(screen.getByText(/prior snapshot/)).toBeVisible();

  view.rerender(<LiveAgentReasoning runId="run-other" snapshot={{ ...saved, snapshotId: 'other-run-snapshot' }} />);
  expect(screen.queryByText('Synthetic exact text.')).not.toBeInTheDocument();
});

it('shows reasoning only in live Communication, not in bundled sample review', async () => {
  const user = userEvent.setup();
  const view = render(<ResearchApp snapshot={saved} />);
  await user.click(screen.getByRole('button', { name: 'Communication' }));
  expect(screen.queryByRole('region', { name: 'Agent reasoning' })).not.toBeInTheDocument();
  view.unmount();
  render(<ResearchApp snapshot={saved} live={{ runId: 'run-a', model: 'saved model', runs: [], error: null,
    form: null, requestedAgentIds: [], onSelectRun: vi.fn() }} />);
  await user.click(screen.getByRole('button', { name: 'Communication' }));
  expect(screen.getByRole('region', { name: 'Agent reasoning' })).toBeInTheDocument();
  expect(getLiveReasoning).not.toHaveBeenCalled();
});

it('keeps an empty reasoning section folded without trying to fetch calls', async () => {
  const user = userEvent.setup();
  const empty = structuredClone(saved);
  empty.attempts = [];
  render(<LiveAgentReasoning runId="run-a" snapshot={empty} />);
  const summary = screen.getByText('Agent reasoning · 0 saved work steps');
  expect(summary.closest('details')).not.toHaveAttribute('open');
  expect(getLiveCalls).not.toHaveBeenCalled();
  await user.click(summary);
  expect(screen.getByText('No agent attempt is saved in this view.')).toBeVisible();
  expect(getLiveCalls).not.toHaveBeenCalled();
});

it('opens and focuses a selected earlier-run work step without fetching private text', async () => {
  const user = userEvent.setup();
  const twoAgents = structuredClone(saved);
  twoAgents.attempts.push({ ...saved.attempts[0], id: 'attempt-other', agentId: twoAgents.agents[1].id,
    startedAt: '2026-10-09T13:00:00Z', returnedReasoning: { availability: 'absent' } });
  vi.mocked(getLiveCalls).mockResolvedValue({ records: [call] });
  render(<LiveAgentReasoning runId="run-a" snapshot={twoAgents} selectedAttemptId="attempt-a" earlierAttemptIds={['attempt-a']} />);
  const panel = screen.getByRole('region', { name: 'Agent reasoning' });
  expect(panel).toHaveAttribute('id', 'agent-reasoning');
  expect(within(panel).getByText('Agent reasoning · 2 saved work steps').closest('details')).toHaveAttribute('open');
  const selected = within(panel).getByText(/Work step 1/);
  expect(selected).toHaveTextContent('Earlier run');
  expect(selected).toHaveTextContent(twoAgents.tasks.find(task => task.id === saved.attempts[0].taskId)!.title);
  expect(selected.closest('details')).toHaveAttribute('open');
  expect(selected).toHaveFocus();
  expect(within(panel).queryByText(/Work step 2/)).not.toBeInTheDocument();
  expect(getLiveCalls).toHaveBeenCalledWith('run-a', 'attempt-a');
  expect(getLiveReasoning).not.toHaveBeenCalled();
  await user.selectOptions(within(panel).getByRole('combobox', { name: 'Filter reasoning by agent' }), twoAgents.agents[1].id);
  expect(within(panel).getByText(/Work step 1/)).toHaveTextContent('This run');
  expect(within(panel).queryByText(/Earlier run/)).not.toBeInTheDocument();
});

it('opens the same work step from Details in Communication without fetching provider text', async () => {
  const user = userEvent.setup();
  vi.mocked(getLiveCalls).mockResolvedValue({ records: [call] });
  render(<ResearchApp snapshot={saved} live={{ runId: 'run-a', model: 'saved model', runs: [], error: null,
    form: null, requestedAgentIds: [], onSelectRun: vi.fn() }} />);
  await user.click(screen.getByRole('button', { name: 'Details' }));
  await user.click(screen.getByRole('button', { name: 'Work step records' }));
  await user.click(screen.getByRole('button', { name: 'View reasoning history →' }));
  expect(screen.getByRole('button', { name: 'Communication' })).toHaveAttribute('aria-current', 'page');
  const panel = screen.getByRole('region', { name: 'Agent reasoning' });
  const selected = within(panel).getByText(/Work step 1/);
  expect(selected.closest('details')).toHaveAttribute('open');
  expect(selected).toHaveFocus();
  expect(getLiveCalls).toHaveBeenCalledWith('run-a', 'attempt-a');
  expect(getLiveReasoning).not.toHaveBeenCalled();
});
