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

afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.history.replaceState(null, '', '/'); });

describe('generated persisted workflow', () => {
  it('retains task navigation in the recorded review without pretending to submit live work', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={child} comparisonSnapshot={parent} />);
    const nav = screen.getByRole('navigation', { name: 'Primary navigation' });
    expect(within(nav).getAllByRole('button').filter(button => button.classList.contains('nav-item')).map(button => button.textContent)).toEqual(['Start or follow up', 'Research review', 'Activity', 'Communication', 'Evidence', 'Details', 'Compare', 'Previous tasks']);
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

  it('follows the real saved configuration, retrieval chain, recovery and parent target', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={child} comparisonSnapshot={parent} />);
    const input = screen.getByRole('region', { name: 'Research task input' });
    await user.click(within(input).getByText('Saved snapshot and follow-up details'));
    const target = within(input).getByRole('region', { name: 'Exact follow-up target' });
    await user.click(within(target).getByText('Report · Early abstract reading'));
    expect(within(target).getByText(parent.artifacts[0].body)).toBeVisible();
    await user.click(within(screen.getByRole('group', { name: 'Recorded agent attempts' })).getByRole('button', { name: /Finch; completed; Find an independent adult comparison/ }));
    const contribution = screen.getByRole('article', { name: /Review of Finch/ });
    expect(within(contribution).getByText(/First route returned no accessible adult comparison/)).toBeVisible();
    const configuration = within(contribution).getAllByRole('region', { name: 'Configuration used', hidden: true })[0];
    await user.click(within(contribution).getAllByText('Saved agent, configuration and recovery details')[0]);
    await user.click(within(configuration).getByText(/Agent configuration · /));
    expect(within(configuration).getByText('groq')).toBeVisible();
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
    const exchange = screen.getByRole('heading', { name: 'Find an independent adult comparison' }).closest('article')!;
    await user.click(within(exchange).getByText('Expand communication and contribution'));
    expect(within(exchange).getAllByText('Sage → Finch')[0]).toBeVisible();
    await user.click(screen.getByRole('button', { name: 'Activity' }));
    const waitingAttempt = screen.getByText('waiting', { selector: '.activity-item-head .status-pill' }).closest('li');
    expect(waitingAttempt).not.toBeNull();
    expect(within(waitingAttempt!).getByText('Seek permitted methods and adverse-event detail', { selector: '.activity-attribution p' })).toBeVisible();
    expect(within(waitingAttempt!).getByText(/Waiting for a permitted alternative source/)).toBeVisible();
  });

  it('shows revised Finch and Sage attempts by default while unchanged A1 stays in parent history', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={child} comparisonSnapshot={parent} />);
    const timeline = screen.getByRole('group', { name: 'Recorded agent attempts' });
    expect(within(timeline).getAllByRole('button')).toHaveLength(7);
    expect(screen.getByText(/7 current-branch attempts/)).toBeVisible();
    const revisedFinch = within(timeline).getByRole('button', { name: /Finch; completed; Find an independent adult comparison/ });
    const revisedSage = within(timeline).getByRole('button', { name: /Sage; completed;/ });
    expect(revisedFinch).toHaveTextContent('updated since parent');
    expect(revisedSage).toHaveTextContent('updated since parent');
    const a1Result = parent.attempts.find(item => item.id === 'attempt-a1')!.resultSummary!;
    expect(within(timeline).queryByText(a1Result)).not.toBeInTheDocument();
    await user.click(revisedSage);
    const contribution = screen.getByRole('article', { name: 'Review of Sage · updated attempt' });
    expect(within(contribution).getByText('Selected current-branch attempt')).toBeVisible();
    // The bar retains 14:04–14:12; it is not moved to the parent's capture time.
    const bar = revisedSage.querySelector<HTMLElement>('.contribution-segment')!;
    const start = Math.min(...child.attempts.filter(item => item.id !== 'attempt-a1').map(item => Date.parse(item.startedAt)), ...child.events.filter(item => !parent.events.some(previous => previous.id === item.id)).map(item => Date.parse(item.time)));
    const span = Date.parse(child.capturedAt) - start;
    expect(parseFloat(bar.style.left)).toBeCloseTo(100 * (Date.parse('2026-10-08T14:04:00Z') - start) / span);
    expect(parseFloat(bar.style.width)).toBeCloseTo(100 * 8 * 60000 / span);
    await user.click(screen.getByRole('button', { name: 'Include parent history (1 attempt)' }));
    expect(within(timeline).getAllByRole('button')).toHaveLength(8);
    expect(within(timeline).getByText(a1Result)).toBeVisible();
    expect(within(timeline).getAllByText(/updated since parent/)).toHaveLength(2);
  });
});
