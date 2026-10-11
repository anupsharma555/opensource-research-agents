/* Exercise the fixed offline Stage 3 export without contacting a research host.
   Accepted callback records stay inspectable without becoming scientific reports. */
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import App, { ResearchApp } from './App';
import type { ResearchSnapshot } from './generated/contracts';
import snapshotData from '../public/stage3-recorded/snapshot.json';
import sampleData from '../public/sample-run.json';

const snapshot = snapshotData as ResearchSnapshot;
afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.history.replaceState(null, '', '/'); });

describe('recorded Stage 3 entry', () => {
  it('fetches only its fixed snapshot and distinguishes callbacks from research output', async () => {
    window.history.replaceState(null, '', '/?recorded=stage3&path=https://example.test/untrusted.json');
    const user = userEvent.setup();
    const fetcher = vi.fn(async () => Response.json(snapshot));
    vi.stubGlobal('fetch', fetcher);
    render(<App />);
    expect(await screen.findByRole('heading', { name: 'Saved Stage 3 coordination test' })).toBeVisible();
    expect(fetcher).toHaveBeenCalledExactlyOnceWith('/stage3-recorded/snapshot.json', expect.objectContaining({ signal: expect.any(AbortSignal) }));
    expect(screen.getByText('RECORDED OFFLINE TEST')).toBeVisible();
    expect(screen.getByText('3 completed local callbacks · 1 waiting task · mission active.')).toBeVisible();
    const output = screen.getByRole('region', { name: 'Current saved output' });
    expect(within(output).getByRole('heading', { name: 'No scientific report saved' })).toBeVisible();
    expect(within(output).queryByRole('region', { name: 'Saved answer' })).not.toBeInTheDocument();
    expect(within(output).getByText(/They do not form a scientific answer/)).toBeVisible();
    expect(snapshot.artifacts).toEqual([]);
    expect(snapshot.inspection?.acceptedOutputReferences).toEqual([]);
    expect(snapshot.attempts.every(attempt => attempt.selectedModel === null)).toBe(true);
    await user.click(screen.getByRole('button', { name: 'Activity' }));
    await user.click(screen.getByText('Browse all saved work steps'));
    expect(screen.getByText('Browse all saved work steps').closest('details')).toHaveAttribute('open');
    const timeline = screen.getByRole('group', { name: 'Recorded agent attempts' });
    expect(within(timeline).getAllByRole('button')).toHaveLength(3);
    for (const [index, attempt] of [...snapshot.attempts].sort((a, b) => a.startedAt.localeCompare(b.startedAt)).entries()) {
      const row = within(timeline).getByRole('button', { name: new RegExp(`^Open work step ${index + 1}:`) }).closest('.agent-step')!;
      expect(row.querySelector('.agent-step-details')).not.toHaveAttribute('open');
      await user.click(within(row as HTMLElement).getByRole('button', { name: new RegExp(`^Open work step ${index + 1}:`) }));
      expect(within(row as HTMLElement).getByText(attempt.resultSummary!)).toBeVisible();
    }
  });

  it('opens exact synthetic inputs, callback diagnostics and saved steps while peer work remains unanswered', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={snapshot} />);
    const input = screen.getByRole('region', { name: 'Saved synthetic inputs' });
    await user.click(within(input).getByText('Stage3 fixture input'));
    expect(within(input).getByText('Inspect a synthetic evidence packet')).toBeVisible();
    await user.click(screen.getByRole('button', { name: 'Communication' }));
    const unanswered = screen.getByRole('article', { name: 'Peer request stage3-question' });
    expect(within(within(unanswered).getByRole('list', { name: 'Saved request, answer and review' })).getByText('No reply recorded')).toBeVisible();
    const unansweredHistory = within(unanswered).getByText('Delivery history and exact records').closest('details')!;
    expect(unansweredHistory).not.toHaveAttribute('open');
    await user.click(within(unansweredHistory).getByText('Delivery history and exact records'));
    expect(within(unansweredHistory).getByText('Recipient acknowledgment').closest('div')).toHaveTextContent('Recorded');
    await user.click(screen.getByRole('button', { name: /Open exchange 2:/ }));
    const partial = screen.getByRole('article', { name: 'Peer request stage3-partial-review' });
    const partialSteps = within(partial).getByRole('list', { name: 'Saved request, answer and review' });
    expect(within(partialSteps).getByText('Partial answer submitted')).toBeVisible();
    expect(within(partialSteps).getByText('No review recorded')).toBeVisible();
    const partialHistory = within(partial).getByText('Delivery history and exact records').closest('details')!;
    expect(partialHistory).not.toHaveAttribute('open');
    await user.click(within(partialHistory).getByText('Delivery history and exact records'));
    expect(within(partialHistory).getByText('Requester review').closest('div')).toHaveTextContent('No review recorded');
    await user.click(screen.getByRole('button', { name: 'Details' }));
    expect(screen.getByRole('region', { name: 'Retained dissent' })).toHaveTextContent('no study passage to appraise');
    await user.click(screen.getByText(/^Saved work and waits/));
    expect(screen.getByText(/Saved monotonic callback intervals overlap/)).toBeVisible();
    await user.click(screen.getByText('Detailed saved work records'));
    await user.click(screen.getByText(`Saved agent steps (${snapshot.inspection!.steps!.length})`));
    const step = snapshot.inspection!.steps!.find(item => item.action === 'Execute peer-selected work')!;
    const stepHeading = screen.getByText(`Step ${step.sequence} · ${step.actorName} · ${step.action} · ${step.outcome}`);
    await user.click(stepHeading);
    expect(within(stepHeading.closest('details')!).getByText(step.summary)).toBeVisible();
    await user.click(screen.getByText(`Saved coordination records (${snapshot.inspection!.records.length})`));
    const records = screen.getByRole('region', { name: 'Coordination diagnostics' });
    await user.click(within(records).getAllByText('Executor artifact')[0]);
    expect(within(records).getByText('recorded_callback_result')).toBeVisible();
    expect(within(records).getByText('Saved result content is separate from this diagnostic overview')).toBeVisible();
    await user.click(screen.getByText('All work steps in this run'));
    await user.click(screen.getByText(/Task and request records without an agent attempt/));
    expect(screen.getByText(/Resolve the remaining synthetic evidence gap · no attempt recorded/)).toBeVisible();
    await user.click(screen.getByRole('button', { name: 'Work step records' }));
    await user.click(screen.getByText('Exact identity, timing, model and skills'));
    await user.click(screen.getByText('Execution and configuration records'));
    expect(screen.getByRole('region', { name: 'Execution diagnostics' })).toBeVisible();
    expect(screen.queryByText(/private reasoning was not requested/i)).not.toBeInTheDocument();
  });

  it('opens the referenced task and exact request even when Details already shows that task', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={snapshot} />);
    await user.click(screen.getByRole('button', { name: 'Communication' }));
    await user.click(screen.getByRole('button', { name: /Open exchange 2:/ }));
    const partial = screen.getByRole('article', { name: 'Peer request stage3-partial-review' });
    await user.click(within(partial).getByRole('button', { name: 'Open related task →' }));
    const task = document.getElementById('selected-task')!;
    expect(task).toHaveFocus();
    expect(task).toHaveTextContent('Resolve the remaining synthetic evidence gap');
    expect(task).toHaveTextContent('Opened from peer request stage3-partial-review');
    expect(task).toHaveTextContent('No agent attempt is recorded for this task');
    expect(task).toHaveTextContent('Opened from exact work version 1');
    await user.click(screen.getByRole('button', { name: '← Return to Communication' }));
    await user.click(screen.getByRole('button', { name: /Open exchange 2:/ }));
    await user.click(within(screen.getByRole('article', { name: 'Peer request stage3-partial-review' })).getByRole('button', { name: 'Open related task →' }));
    expect(document.getElementById('selected-task')).toHaveFocus();
  });

  it('offers an explicit Stage 3 entry without replacing the Stage 2 entry', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={sampleData as ResearchSnapshot} />);
    await user.click(screen.getByRole('button', { name: 'Previous samples' }));
    expect(screen.getByRole('link', { name: 'Open recorded Stage 3 coordination review' })).toHaveAttribute('href', '?recorded=stage3');
    expect(screen.getByRole('link', { name: 'Open recorded Stage 2 review' })).toHaveAttribute('href', '?recorded=stage2');
  });

  it.each(['live proof', 'unexpected report'] as const)('rejects %s instead of silently displaying it as the callback fixture', async scenario => {
    const invalid = structuredClone(snapshot);
    if (scenario === 'live proof') invalid.proofKind = 'live_model_on_supplied_evidence';
    else invalid.artifacts = (sampleData as ResearchSnapshot).artifacts;
    window.history.replaceState(null, '', '/?recorded=stage3');
    const fetcher = vi.fn(async () => Response.json(invalid));
    vi.stubGlobal('fetch', fetcher);
    render(<App />);
    expect(await screen.findByRole('heading', { name: 'Recorded data unavailable' })).toBeVisible();
    expect(fetcher).toHaveBeenCalledTimes(1);
    expect(screen.queryByRole('region', { name: 'Current saved output' })).not.toBeInTheDocument();
  });
});
