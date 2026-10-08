// Check navigation and evidence review in the scripted sample; no host is involved.
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { cleanup } from '@testing-library/react';
import { ResearchApp } from './App';
import type { ResearchSnapshot } from './generated/contracts';
import sample from '../public/sample-run.json';
import baseline from '../public/sample-run-baseline.json';

const snapshot = sample as ResearchSnapshot;

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
});

describe('sample review journeys', () => {
  it('keeps the sample label and follows a finding citation to its exact passage', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={snapshot} />);
    expect(screen.getByText('SAMPLE DATA')).toBeInTheDocument();
    expect(screen.getByText(/Together they do not establish benefit/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /A1 · passage A1-result/ }));
    expect(screen.getByRole('heading', { name: /Adult study A/ })).toBeInTheDocument();
    const passage = screen.getByText(/At six weeks, response occurred in 18 of 40/);
    expect(passage.closest('article')).toHaveClass('highlighted');
  });

  it('shows peer status separately from failed, truncated, and returned attempt content', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={snapshot} />);
    await user.click(screen.getByRole('button', { name: /Work Tasks and peer requests/ }));
    const openRequest = screen.getByText('request-methods').closest('article');
    expect(openRequest).not.toBeNull();
    expect(within(openRequest!.querySelector('.request-head')!).getByText('acknowledged')).toBeInTheDocument();
    expect(within(openRequest!.querySelector('.request-steps')!).getByText('fulfilled')).not.toHaveClass('reached');
    await user.click(screen.getByRole('button', { name: /Seek permitted methods and adverse-event detail/ }));
    await user.click(screen.getByRole('button', { name: /attempt-methods-failed/ }));
    expect(screen.getByText(/Methods table unavailable in this packet/)).toBeInTheDocument();
    expect(screen.getByText(/No reasoning content was returned in this fixture/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /attempt-critique/ }));
    expect(screen.getByText(/This is incomplete; no missing continuation is inferred/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /^attempt-a2 / }));
    expect(screen.getByText(/Illustrative fixture content: A2 is a separate adult/)).toBeInTheDocument();
  });

  it('previews a version-specific follow-up and creates only a simulated receipt', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={snapshot} />);
    await user.click(screen.getByRole('button', { name: /Start or follow up Preview a scoped request/ }));
    await user.selectOptions(screen.getByLabelText('Exact saved target'), 'report-v1');
    await user.clear(screen.getByLabelText('Research question or follow-up'));
    await user.type(screen.getByLabelText('Research question or follow-up'), 'Recheck the earlier report.');
    await user.click(screen.getByRole('button', { name: /Preview simulated queue receipt/ }));
    const receipt = screen.getByRole('status');
    expect(within(receipt).getByText(/SIM-QUEUE-001 · queued/)).toBeInTheDocument();
    expect(within(receipt).getByText(/Child of Report report-v1, version 1/)).toBeInTheDocument();
    expect(within(receipt).getByText(/No backend call or model work occurred/)).toBeInTheDocument();
    await user.click(within(receipt).getByRole('button', { name: /Inspect recorded child task and attempts/ }));
    expect(screen.getByRole('heading', { name: 'Find an independent adult comparison' })).toBeInTheDocument();
    expect(screen.getByText('report-v1')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /attempt-a2-failed/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Inspect revised claim/ })).toBeInTheDocument();
  });

  it('distinguishes disconnected and empty previews from a completed mission', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={snapshot} />);
    await user.selectOptions(screen.getByLabelText('Preview a state'), 'disconnected');
    expect(screen.getByText('Connection unavailable')).toBeInTheDocument();
    expect(screen.getByText(/Current host state is unknown/)).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText('Preview a state'), 'empty');
    expect(screen.getByRole('heading', { name: 'No mission selected' })).toBeInTheDocument();
    expect(screen.getByText(/There is no run to review in this preview state/)).toBeInTheDocument();
  });

  it('keeps a local annotation attached to the visible source and claim version', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={snapshot} />);
    await user.click(screen.getByRole('button', { name: /Evidence Sources and claim history/ }));
    await user.click(screen.getByRole('button', { name: /Adult study B/ }));
    await user.type(screen.getByLabelText(/Your note about A2 and claim v1/), 'Check the outcome definition.');
    await user.click(screen.getByRole('button', { name: 'Save local sample note' }));
    const notes = screen.getByLabelText('Local sample notes');
    expect(within(notes).getByText(/A2-copy-v1 · claim v1 · sample 1.0.1/)).toBeInTheDocument();
    expect(within(notes).getByText('Check the outcome definition.')).toBeInTheDocument();
  });

  it('compares two distinct scripted run snapshots without claiming a model win', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={snapshot} comparisonSnapshot={baseline as ResearchSnapshot} />);
    await user.click(screen.getByRole('button', { name: /Compare Saved sample versions/ }));
    expect(screen.getByText(/mission-fictional-adult-baseline · 1 source/)).toBeInTheDocument();
    expect(screen.getByText(/mission-fictional-adult-review · 4 sources/)).toBeInTheDocument();
    expect(screen.getByText(/Different source coverage explains this change; the display does not show a better model/)).toBeInTheDocument();
  });

  it('allows keyboard navigation into the work view', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={snapshot} />);
    await user.tab();
    expect(screen.getByRole('button', { name: /Findings Answer and progress/ })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole('button', { name: /Work Tasks and peer requests/ })).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(screen.getByRole('heading', { name: 'Parallel work, visible handoffs' })).toBeInTheDocument();
  });
});
