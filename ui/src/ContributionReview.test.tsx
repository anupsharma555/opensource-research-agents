/* Check human-readable contribution and explanation views using a generated
   saved fixture; review interactions must never initiate agent work. */
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ResearchSnapshot } from './generated/contracts';
import App, { ResearchApp } from './App';
import { ContributionReview } from './ContributionReview';
import fixture from '../public/stage3-contributions/snapshot.json';
import sample from '../public/sample-run.json';

const snapshot = fixture as ResearchSnapshot;
afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.history.replaceState(null, '', '/'); });

describe('shared contribution review', () => {
  it('distinguishes optional uptake, unknown use and older explanation coverage', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={snapshot} />);
    await user.click(screen.getByRole('button', { name: 'Communication' }));
    const sharedFeedback = screen.getByText('Shared feedback and its recorded effects').closest('details')!;
    expect(sharedFeedback).not.toHaveAttribute('open');
    await user.click(within(sharedFeedback).getByText('Shared feedback and its recorded effects'));
    const contribution = screen.getByRole('region', { name: 'Shared contribution review' });
    expect(within(contribution).getByText(/Follow-up review peer.*Earlier discussion/)).toBeVisible();
    expect(within(contribution).getByText(/challenged/)).toBeVisible();
    expect(within(contribution).getByText(/Acknowledgment recorded; scientific use unknown/)).toBeVisible();
    expect(within(contribution).getByText(/No handling statement is recorded/)).toBeVisible();
    expect(within(contribution).getByText(/does not mean an update was ignored/)).toBeVisible();
    const explanations = screen.getByRole('region', { name: 'Explanation coverage' });
    expect(within(explanations).getByText(/later event.*available in the declared scope/)).toBeVisible();
    expect(within(explanations).getAllByText(/cited source set is not proven complete/)).toHaveLength(2);
    expect(within(explanations).getByText(/Detail intentionally omitted/)).toBeVisible();
    expect(within(explanations).getByText(/Detail still pending/)).toBeVisible();
    expect(within(explanations).getAllByText(/author’s stated reading boundary/)).toHaveLength(2);
    expect(within(explanations).getByRole('heading', { name: 'Run explanation · earlier event boundary' })).toBeVisible();
    expect(within(explanations).getByRole('heading', { name: 'Run explanation · later event boundary' })).toBeVisible();
    expect(screen.queryByRole('region', { name: 'Saved answer' })).not.toBeInTheDocument();
  });

  it('shows unknown coverage for an older summary without inventing freshness', () => {
    const old = structuredClone(snapshot);
    const review = old.inspection!.records.find(r => r.reference.kind === 'contribution_review_view')!;
    review.data.summary_views = [{ level: 'run', summary: 'An older saved explanation.', coverage_status: 'unknown' }];
    render(<ContributionReview snapshot={old} />);
    expect(screen.getByText(/Coverage unknown/)).toBeVisible();
    expect(screen.queryByText(/Author-declared event boundary/)).not.toBeInTheDocument();
  });

  it('keeps clarification messages separate from ordinary update uptake', () => {
    const clarified = structuredClone(snapshot);
    const review = clarified.inspection!.records.find(r => r.reference.kind === 'contribution_review_view')!;
    const context = review.data.proposal_context as { entries: { records: { ref: { kind: string; id: string }; data: Record<string, unknown> }[] }[] };
    for (const entry of context.entries) for (const item of entry.records) {
      if (item.ref.kind === 'mail_message' && item.ref.id === 'contribution-unassessed') item.data.message_role = 'clarification';
    }
    render(<ContributionReview snapshot={clarified} />);
    expect(screen.getByRole('heading', { name: 'Clarification of an existing peer request' })).toBeVisible();
    expect(screen.getByText(/does not answer or close the original request/)).toBeVisible();
    expect(screen.queryByText(/Update contribution-unassessed:/)).not.toBeInTheDocument();
  });

  it('loads only the fixed contribution fixture when requested', async () => {
    window.history.replaceState(null, '', '/?recorded=stage3&review=contributions&path=https://example.test/not-a-fixture');
    const fetcher = vi.fn(async () => Response.json(snapshot));
    vi.stubGlobal('fetch', fetcher);
    render(<App />);
    expect(await screen.findByRole('heading', { name: 'Saved Stage 3 coordination test' })).toBeVisible();
    expect(fetcher).toHaveBeenCalledExactlyOnceWith('/stage3-contributions/snapshot.json', expect.objectContaining({ signal: expect.any(AbortSignal) }));
  });

  it('makes the added review discoverable without replacing existing samples', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={sample as ResearchSnapshot} />);
    await user.click(screen.getByRole('button', { name: 'Previous samples' }));
    expect(screen.getByRole('link', { name: 'Open shared contribution review' })).toHaveAttribute('href', '?recorded=stage3&review=contributions');
    expect(screen.getByRole('link', { name: 'Open recorded Stage 3 coordination review' })).toHaveAttribute('href', '?recorded=stage3');
  });
});
