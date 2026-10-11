/* Exercise the generated persisted workflow and its fixed offline entry point.
   These tests make no provider calls; they read the demo's paired projections. */
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App, { ResearchApp } from './App';
import type { ResearchSnapshot } from './generated/contracts';
import parentData from '../public/stage2-recorded/parent.json';
import childData from '../public/stage2-recorded/child.json';

const parent = parentData as ResearchSnapshot;
const child = childData as ResearchSnapshot;

function timelineStep(attemptId: string) {
  const timeline = screen.getByRole('group', { name: 'Recorded agent attempts' });
  const attempt = child.attempts.find(item => item.id === attemptId) ?? parent.attempts.find(item => item.id === attemptId);
  expect(attempt).toBeDefined();
  const step = [...timeline.querySelectorAll<HTMLElement>('.agent-step')].find(item =>
    item.querySelector('.agent-step-times time')?.getAttribute('datetime') === attempt?.startedAt);
  expect(step).toBeDefined();
  return step!;
}

afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.history.replaceState(null, '', '/'); });

describe('generated persisted workflow', () => {
  it('retains task navigation in the recorded review without pretending to submit live work', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={child} comparisonSnapshot={parent} />);
    const nav = screen.getByRole('navigation', { name: 'Primary navigation' });
    expect(within(nav).getAllByRole('button').filter(button => button.classList.contains('nav-item')).map(button => button.textContent)).toEqual(['Start or follow up', 'Research review', 'Communication', 'Evidence', 'Compare', 'Activity', 'Details', 'Previous tasks']);
    await user.click(within(nav).getByRole('button', { name: 'Start or follow up' }));
    expect(screen.getByRole('region', { name: 'Live workspace access' })).toBeVisible();
    expect(screen.queryByRole('button', { name: /Preview simulated queue receipt/ })).not.toBeInTheDocument();
    await user.click(within(nav).getByRole('button', { name: 'Previous tasks' }));
    expect(screen.getByRole('heading', { name: 'Previous live tasks' })).toBeVisible();
    await user.click(screen.getByRole('button', { name: 'Open recorded review' }));
    expect(screen.getByRole('region', { name: 'Current saved output' })).toBeVisible();
  });

  it('loads only the fixed offline projection paths and shows the accepted child version', async () => {
    window.history.replaceState(null, '', '/?recorded=stage2&path=https://example.test/ignored');
    const fetcher = vi.fn(async (path: string) => Response.json(path.endsWith('/child.json') ? child : parent));
    vi.stubGlobal('fetch', fetcher);
    render(<App />);
    const output = await screen.findByRole('region', { name: 'Current saved output' });
    expect(within(output).getByText(child.artifacts[0].body)).toBeVisible();
    expect(within(output).queryByText(parent.artifacts[0].body)).not.toBeInTheDocument();
    expect(fetcher.mock.calls.map(call => call[0])).toEqual(['/stage2-recorded/child.json', '/stage2-recorded/parent.json']);
    expect(screen.getByText('RECORDED OFFLINE TEST')).toBeVisible();
  });

  it('rejects a mismatched parent without falling back to unrelated sample data', async () => {
    window.history.replaceState(null, '', '/?recorded=stage2');
    vi.stubGlobal('fetch', vi.fn(async (path: string) => Response.json(path.endsWith('/child.json') ? child : { ...parent, snapshotId: 'unrelated-parent' })));
    render(<App />);
    expect(await screen.findByRole('heading', { name: 'Recorded data unavailable' })).toBeVisible();
    expect(screen.queryByRole('region', { name: 'Current saved output' })).not.toBeInTheDocument();
  });

  it('follows the saved configuration, retrieval chain and parent target without inferring a peer request', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={child} comparisonSnapshot={parent} />);
    const input = screen.getByRole('region', { name: 'Research task input' });
    await user.click(within(input).getByText('Saved snapshot and follow-up details'));
    const target = within(input).getByRole('region', { name: 'Exact follow-up target' });
    await user.click(within(target).getByText('Report · Early abstract reading'));
    expect(within(target).getByText(parent.artifacts[0].body)).toBeVisible();
    await user.click(screen.getByRole('button', { name: 'Activity' }));
    await user.click(screen.getByText('Browse all saved work steps'));
    expect(screen.getByText('Browse all saved work steps').closest('details')).toHaveAttribute('open');
    await user.click(within(timelineStep('attempt-a2-failed')).getByRole('button', { name: /^Open work step/ }));
    const contribution = timelineStep('attempt-a2-failed');
    expect(within(contribution.querySelector('.step-result-preview') as HTMLElement).getByText(/First route returned no accessible adult comparison/)).toBeVisible();
    await user.click(within(contribution).getByText('Technical records and current task state'));
    await user.click(within(contribution).getByText('Agent settings and recovery'));
    const configuration = within(contribution).getByRole('region', { name: 'Configuration used' });
    await user.click(within(configuration).getByText(/Agent configuration · /));
    expect(within(configuration).getByText('groq')).toBeVisible();
    await user.click(screen.getByRole('button', { name: 'Research review' }));
    const output = screen.getByRole('region', { name: 'Current saved output' });
    await user.click(within(output).getByText(/Sources and supporting passages/));
    await user.click(within(output).getByText('A1 · A1-result · expand cited passage'));
    expect(within(output).getByText('Reused saved evidence')).toBeVisible();
    const citation = within(output).getByText('A1 · A1-result · expand cited passage').closest('details')!;
    await user.click(within(citation).getByText('Source copy and discovery receipts'));
    expect(within(citation).getByText('cognitive behavioural therapy depression six weeks')).toBeVisible();
    await user.click(within(citation).getByText('Retrieval hit'));
    const hit = within(citation).getByText('Retrieval hit').closest('details')!;
    await user.click(within(hit).getByText('Retrieval delivery'));
    expect(within(hit).getByText('View envelope')).toBeVisible();
    await user.click(screen.getByRole('button', { name: 'Communication' }));
    expect(screen.getByRole('heading', { name: 'Requests between agents (0)' })).toBeVisible();
    expect(screen.queryByRole('heading', { name: 'Find an independent adult comparison' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Activity' }));
    const waitingAttempt = screen.getByText('waiting', { selector: '.activity-item-head .status-pill' }).closest('li');
    expect(waitingAttempt).not.toBeNull();
    await user.click(within(waitingAttempt!).getByRole('button', { name: 'Expand activity details' }));
    const waitingTask = child.attempts.find(item => item.id === waitingAttempt!.dataset.attemptId)!.taskId;
    expect(waitingAttempt!.querySelector('.activity-row-scope span')).toHaveAttribute('title', waitingTask);
    await user.click(within(waitingAttempt!).getByText('Technical records and current task state'));
    expect(within(waitingAttempt!).getAllByText(/Waiting for a permitted alternative source/).length).toBeGreaterThan(0);
  });

  it('shows revised Finch and Sage attempts by default while unchanged A1 stays in parent history', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={child} comparisonSnapshot={parent} />);
    await user.click(screen.getByRole('button', { name: 'Activity' }));
    await user.click(screen.getByText('Browse all saved work steps'));
    expect(screen.getByText('Browse all saved work steps').closest('details')).toHaveAttribute('open');
    const timeline = screen.getByRole('group', { name: 'Recorded agent attempts' });
    expect(within(timeline).getAllByRole('button')).toHaveLength(7);
    expect(screen.getByText(/7 saved steps ·/)).toBeVisible();
    const revisedFinch = timelineStep('attempt-a2');
    const revisedSage = timelineStep('attempt-synthesis');
    expect(revisedFinch).toHaveTextContent('updated');
    expect(revisedSage).toHaveTextContent('updated');
    const a1Result = parent.attempts.find(item => item.id === 'attempt-a1')!.resultSummary!;
    expect(within(timeline).queryByText(a1Result)).not.toBeInTheDocument();
    await user.click(within(revisedSage).getByRole('button', { name: /^Open work step/ }));
    expect(within(revisedSage).getByText(child.attempts.find(item => item.id === 'attempt-synthesis')!.resultSummary!)).toBeVisible();
    expect(revisedSage.querySelector('.agent-step-details')).toHaveAttribute('open');
    // The shared scale uses attempt timestamps, not the later snapshot or unrelated events.
    await user.click(screen.getByRole('button', { name: 'By agent' }));
    const bar = timelineStep('attempt-synthesis').querySelector<HTMLElement>('.agent-step-bar')!;
    const shown = child.attempts.filter(item => item.id !== 'attempt-a1');
    const start = Math.min(...shown.map(item => Date.parse(item.startedAt)));
    const end = Math.max(...shown.map(item => Date.parse(item.endedAt || item.startedAt)));
    const span = end - start;
    expect(parseFloat(bar.style.left)).toBeCloseTo(100 * (Date.parse('2026-10-08T14:04:00Z') - start) / span);
    expect(parseFloat(bar.style.width)).toBeCloseTo(100 * 8 * 60000 / span);
    await user.click(screen.getByRole('button', { name: /Include work from the earlier run \(1 steps?\)/ }));
    expect(timeline.querySelectorAll('.agent-step-select')).toHaveLength(8);
    const parentStep = timelineStep('attempt-a1');
    await user.click(within(parentStep).getByRole('button', { name: /^Open work step/ }));
    expect(within(parentStep).getByText(a1Result)).toBeVisible();
    expect(timelineStep('attempt-a2')).toHaveTextContent('updated');
    expect(timelineStep('attempt-synthesis')).toHaveTextContent('updated');
  });

  it('opens the exact task record for a selected task without an attempt', async () => {
    const user = userEvent.setup();
    const saved = structuredClone(child);
    saved.tasks.push({ ...saved.tasks[0], id: 'task-unstarted', title: 'Unstarted review request', status: 'queued', attemptIds: [] });
    render(<ResearchApp snapshot={saved} comparisonSnapshot={parent} />);
    await user.click(screen.getByRole('button', { name: 'Activity' }));
    await user.click(screen.getByText('Browse all saved work steps'));
    expect(screen.getByText('Browse all saved work steps').closest('details')).toHaveAttribute('open');
    await user.click(screen.getByText('Task and request records without an agent attempt (1)'));
    await user.click(screen.getByRole('button', { name: /Unstarted review request · no attempt recorded/ }));
    const selected = document.getElementById('selected-task')!;
    expect(within(selected).getByText(/No agent attempt is recorded for this task/)).toBeVisible();
    expect(selected).toHaveTextContent('task-unstarted');
  });
});
