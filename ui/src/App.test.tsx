/* Check whether a reviewer can navigate the saved research sample.
   These browser-like tests follow a finding to its cited passage, inspect peer
   requests and attempts, preview follow-up work, and use the keyboard. They use
   fixed data and do not contact a research backend. */
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
  window.localStorage.clear();
});

describe('sample review journeys', () => {
  it('shows one research task from input through agent work to the saved output on one screen', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={snapshot} comparisonSnapshot={baseline as ResearchSnapshot} />);
    const input = screen.getByRole('region', { name: 'Research task input' });
    expect(within(input).getByText(/Simulated abstracts with real clinical labels/)).toBeInTheDocument();
    expect(within(input).getByRole('heading', { name: /adults with depression.*cognitive behavioural therapy/ })).toBeInTheDocument();
    const review = screen.getByRole('region', { name: 'Task from input to output' });
    const contribution = within(review).getByRole('article', { name: 'Review of Find an independent adult comparison' });
    expect(within(contribution).getByText('Acquisition peer')).toBeInTheDocument();
    expect(within(contribution).getByText('Find an independent adult attention-control study at six weeks.')).toBeInTheDocument();
    expect(within(contribution).getByText(/First route returned no accessible adult comparison/)).toBeInTheDocument();
    expect(within(contribution).getByText(/A2 supplied a separate study and narrowed the claim/)).toBeInTheDocument();
    expect(within(contribution).getByText(/20 of 50 versus 21 of 50/)).toBeInTheDocument();
    await user.click(within(review).getByRole('button', { name: /Seek permitted methods and adverse-event detail.*waiting/i }));
    expect(within(review).getByText(/No final output yet. Methods table unavailable/)).toBeInTheDocument();
    expect(within(review).queryByText(/No separate agent request was recorded/)).not.toBeInTheDocument();
    const output = screen.getByRole('region', { name: 'Current saved output' });
    expect(within(output).getByText(/Together they do not establish benefit/)).toBeInTheDocument();
    expect(within(output).getByText(/Saved limitations/)).toBeInTheDocument();
    expect(screen.getByText(/Saved sample · no live updates/)).toBeInTheDocument();
  });

  it('keeps the sample label and follows a finding citation to its exact passage', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={snapshot} />);
    expect(screen.getByText('SCRIPTED SAMPLE')).toBeInTheDocument();
    expect(screen.getByText(/Together they do not establish benefit/)).toBeInTheDocument();
    await user.click(screen.getByText('Sources and supporting passages (2 references)'));
    await user.click(screen.getByRole('button', { name: 'Open passage 1 →' }));
    expect(screen.getByRole('heading', { name: /Adult study A/ })).toBeInTheDocument();
    const passage = screen.getByText(/At six weeks, response occurred in 18 of 40/);
    expect(passage.closest('article')).toHaveClass('highlighted');
  });

  it('folds review cards independently and reopens the selected contribution underneath', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={snapshot} />);
    const input = screen.getByRole('region', { name: 'Research task input' });
    await user.click(within(input).getByText('View supplied source data (4 records)'));
    await user.click(within(input).getByText('Adult study A · cognitive behavioural therapy vs attention control'));
    const sourceText = within(input).getByText(/80 adults with depression, 40 received cognitive behavioural therapy/);
    await user.click(screen.getByRole('button', { name: 'Collapse input research task' }));
    expect(within(input).getByRole('heading', { name: /adults with depression/ })).toBeVisible();
    expect(sourceText).not.toBeVisible();
    const output = screen.getByRole('region', { name: 'Current saved output' });
    const answer = within(output).getByText(/Together they do not establish benefit/);
    expect(answer).toBeVisible();
    await user.click(screen.getByRole('button', { name: 'Collapse all cards' }));
    expect(answer).not.toBeVisible();
    expect(within(output).getByRole('heading', { name: 'Current saved output' })).toBeVisible();
    await user.click(screen.getByRole('button', { name: 'Expand agent contributions' }));
    await user.click(screen.getByRole('button', { name: /^Challenge the early benefit reading;/ }));
    const contribution = screen.getByRole('article', { name: 'Review of Challenge the early benefit reading' });
    expect(within(contribution).getByText('Bounded review of interval and comparator.')).toBeVisible();
    expect(answer).not.toBeVisible();
    await user.click(screen.getByRole('button', { name: 'Expand input research task' }));
    expect(sourceText).toBeVisible();
  });

  it('keeps reported results, interpretation and unknowns together with independent result disclosure', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={snapshot} />);
    const output = screen.getByRole('region', { name: 'Current saved output' });
    await user.click(within(output).getByText('Sources and supporting passages (2 references)'));
    const results = within(output).getByRole('table', { name: 'Sources and supporting passages' });
    expect(within(results).getByText(/At six weeks, response occurred in 18 of 40/)).toBeVisible();
    const interpretation = within(output).getByRole('region', { name: 'Saved answer' });
    const answer = within(interpretation).getByText(/Together they do not establish benefit/);
    expect(answer).toBeVisible();
    expect(within(output).getByRole('region', { name: 'Saved limitations' })).toBeVisible();
    expect(within(results).queryByText(/Together they do not establish benefit/)).not.toBeInTheDocument();
    await user.click(within(output).getByText('Sources and supporting passages (2 references)'));
    expect(results).not.toBeVisible();
    expect(answer).toBeVisible();
  });

  it('uses saved attempt times to show overlapping contributions and waiting separately', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={snapshot} />);
    const timeline = screen.getByRole('group', { name: 'Work toward this answer' });
    expect(within(timeline).getByRole('button', { name: /Inspect first adult abstract; Acquisition peer; completed; completed: \+1 min to \+4 min/ })).toBeInTheDocument();
    expect(within(timeline).getByRole('button', { name: /Find an independent adult comparison; Acquisition peer; completed; failed: \+2 min to \+3 min; completed: \+3 min to \+6 min/ })).toBeInTheDocument();
    expect(within(timeline).getByRole('button', { name: /Seek permitted methods.*failed: \+13 min to \+15 min; waiting: \+16 min to \+24 min/ })).toBeInTheDocument();
    await user.click(within(timeline).getByRole('button', { name: /^Synthesize adult six-week evidence/ }));
    const contribution = screen.getByRole('article', { name: 'Review of Synthesize adult six-week evidence' });
    expect(within(contribution).getByText('Synthesis peer')).toBeInTheDocument();
    expect(within(contribution).getByText('Revised the answer after critique; retained supported counts and uncertainty.')).toBeInTheDocument();
    expect(timeline.compareDocumentPosition(contribution) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy();
  });

  it('shows the actual supplied source packet and does not infer inputs for unknown snapshots', async () => {
    const user = userEvent.setup();
    const { unmount } = render(<ResearchApp snapshot={snapshot} />);
    const sources = screen.getByRole('region', { name: 'Source data supplied with this task' });
    expect(within(sources).getByText('3 simulated study abstracts and 1 access notice · real clinical labels')).toBeInTheDocument();
    await user.click(within(sources).getByText('View supplied source data (4 records)'));
    await user.click(within(sources).getByText('Adult study A · cognitive behavioural therapy vs attention control'));
    expect(within(sources).getByText(/80 adults with depression, 40 received cognitive behavioural therapy/)).toBeVisible();
    await user.click(within(sources).getByText('Adolescent waitlist study · different question'));
    expect(within(sources).getByText(/60 adolescents with generalized anxiety disorder compared cognitive behavioural therapy/)).toBeVisible();
    await user.click(within(sources).getByText('Access notice · methods table unavailable'));
    expect(within(sources).getByText(/Sample publisher access notice. The full text and methods table/)).toBeVisible();
    unmount();
    render(<ResearchApp snapshot={{ ...snapshot, snapshotId: 'unknown-snapshot' }} />);
    expect(screen.getByText(/source material supplied at the start was not recorded/)).toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Source data supplied with this task' })).not.toBeInTheDocument();
  });

  it('shows peer status separately from failed, truncated, and returned attempt content', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={snapshot} />);
    await user.click(screen.getByRole('button', { name: 'Details' }));
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
    await user.click(screen.getByRole('button', { name: 'Start or follow up' }));
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
    await user.click(screen.getByRole('button', { name: 'Details' }));
    await user.click(screen.getByText('Run metadata'));
    await user.selectOptions(screen.getByLabelText('Show UI state example'), 'disconnected');
    expect(screen.getByText('Connection unavailable')).toBeInTheDocument();
    expect(screen.getByText(/Current host state is unknown/)).toBeInTheDocument();
    await user.selectOptions(screen.getByLabelText('Show UI state example'), 'empty');
    expect(screen.getByRole('heading', { name: 'No mission selected' })).toBeInTheDocument();
    expect(screen.getByText(/There is no run to review in this preview state/)).toBeInTheDocument();
  });

  it('keeps a local annotation attached to the visible source and claim version', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={snapshot} />);
    await user.click(screen.getByRole('button', { name: 'Evidence' }));
    await user.click(screen.getByRole('button', { name: /Adult study B/ }));
    await user.type(screen.getByLabelText(/Your note about A2 and claim v1/), 'Check the outcome definition.');
    await user.click(screen.getByRole('button', { name: 'Save local sample note' }));
    const notes = screen.getByLabelText('Local sample notes');
    expect(within(notes).getByText(/A2-copy-v3 · claim v1 · sample 1\.0\.3/)).toBeInTheDocument();
    expect(within(notes).getByText('Check the outcome definition.')).toBeInTheDocument();
  });

  it('compares two distinct scripted run snapshots without claiming a model win', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={snapshot} comparisonSnapshot={baseline as ResearchSnapshot} />);
    await user.click(screen.getByRole('button', { name: 'Compare' }));
    expect(screen.getByText(/mission-fictional-adult-baseline · 1 source/)).toBeInTheDocument();
    expect(screen.getByText(/mission-fictional-adult-review · 4 sources/)).toBeInTheDocument();
    expect(screen.getByText(/Different source coverage explains this change; the display does not show a better model/)).toBeInTheDocument();
  });

  it('explains agent requests and replies in the Communication tab', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={snapshot} />);
    await user.click(screen.getByRole('button', { name: 'Communication' }));
    const complete = screen.getByRole('heading', { name: 'Find an independent adult comparison' }).closest('article');
    expect(complete).not.toBeNull();
    expect(within(complete!).getByText('Requested by')).toBeInTheDocument();
    expect(within(complete!).getByText('Assigned to')).toBeInTheDocument();
    expect(within(complete!).getByText('Agent Synthesis peer')).toBeInTheDocument();
    expect(within(complete!).getByText('Agent Acquisition peer')).toBeInTheDocument();
    expect(within(complete!).getAllByText('Find an independent adult attention-control study at six weeks.').length).toBeGreaterThan(0);
    expect(within(complete!).getAllByText('A2 supplied a separate study and narrowed the claim.').length).toBeGreaterThan(0);
    await user.click(within(complete!).getByText(/Expand communication and contribution/));
    expect(within(complete!).getByText(/Acquisition peer accepted the question/)).toBeVisible();
    expect(within(complete!).getByText(/Found a separate adult study with no clear difference/)).toBeVisible();
    const waiting = screen.getByRole('heading', { name: 'Seek permitted methods and adverse-event detail' }).closest('article');
    expect(within(waiting!).getByText(/No completed response has been recorded/)).toBeInTheDocument();
  });

  it('labels the Activity view as saved and filters recorded agent work', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={snapshot} />);
    await user.click(screen.getByRole('button', { name: 'Activity' }));
    expect(screen.getByText(/No live connection or automatic refresh/)).toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Agent work' })).toHaveAttribute('aria-pressed', 'true');
    const failedAttempt = screen.getByText(/First route returned no accessible adult comparison/).closest('li');
    expect(failedAttempt).not.toBeNull();
    const timestamp = failedAttempt!.querySelector('time');
    expect(timestamp).toHaveAttribute('datetime', '2026-10-08T14:03:00Z');
    expect(timestamp).toHaveTextContent('2026');
    expect(timestamp).toHaveTextContent(/\d:\d{2}:\d{2} (AM|PM) [A-Z]/);
    await user.click(within(failedAttempt!).getByText('Expand activity details'));
    expect(within(failedAttempt!).getByText(/This attempt did not complete successfully/)).toBeVisible();
    expect(within(failedAttempt!).getByText('Read unavailable scripted route')).toBeVisible();
    expect(within(failedAttempt!).getByText('1 min 0 sec')).toBeVisible();
    await user.click(screen.getByRole('button', { name: 'Messages' }));
    expect(screen.getAllByText('Find an independent adult attention-control study at six weeks.')[0]).toBeVisible();
    expect(screen.queryByText(/First route returned no accessible adult comparison/)).not.toBeInTheDocument();
  });

  it('combines actor, category and text filters using saved sender and recipient IDs', async () => {
    const saved = structuredClone(snapshot);
    const template = saved.messages[0];
    saved.messages = [
      { ...template, id: 'asymmetric-request', requestId: 'asymmetric', fromAgentId: 'agent-synthesis', toAgentId: 'agent-acquisition', summary: 'Please inspect asymmetric evidence.', createdAt: '2026-10-08T14:31:00Z' },
      { ...template, id: 'asymmetric-reply', requestId: 'asymmetric', fromAgentId: 'agent-acquisition', toAgentId: 'agent-synthesis', summary: 'Asymmetric evidence was inspected.', createdAt: '2026-10-08T14:32:00Z' },
    ];
    saved.events = [{ ...saved.events[0], id: 'host-only', agentId: undefined, summary: 'Host stored an asymmetric receipt.' }];
    const user = userEvent.setup();
    render(<ResearchApp snapshot={saved} />);
    await user.click(screen.getByRole('button', { name: 'Activity' }));
    await user.click(screen.getByRole('button', { name: 'Messages' }));
    await user.selectOptions(screen.getByRole('combobox', { name: 'Agent' }), 'agent-acquisition');
    expect(screen.getAllByText('Please inspect asymmetric evidence.').length).toBeGreaterThan(0);
    expect(screen.getAllByText('Asymmetric evidence was inspected.').length).toBeGreaterThan(0);
    await user.type(screen.getByRole('searchbox', { name: 'Search saved activity' }), 'Please inspect');
    expect(screen.getAllByText('Please inspect asymmetric evidence.').length).toBeGreaterThan(0);
    expect(screen.queryByText('Asymmetric evidence was inspected.')).not.toBeInTheDocument();
    expect(screen.getByText(/^1 of \d+ saved records$/)).toBeInTheDocument();
    await user.clear(screen.getByRole('searchbox', { name: 'Search saved activity' }));
    await user.selectOptions(screen.getByRole('combobox', { name: 'Agent' }), 'agent-critique');
    expect(screen.getByText(/No saved activity matches these filters/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Host events' }));
    expect(screen.queryByText('Host stored an asymmetric receipt.')).not.toBeInTheDocument();
    await user.selectOptions(screen.getByRole('combobox', { name: 'Agent' }), 'all');
    expect(screen.getByText('Host stored an asymmetric receipt.')).toBeInTheDocument();
    expect(screen.getByText(/^1 of \d+ saved records$/)).toBeInTheDocument();
  });

  it('separates saved assignment, actor and time from a formatted result body', async () => {
    const saved = structuredClone(snapshot);
    saved.attempts[0].resultSummary = 'Observed **5 of 20** eligible records. **Citations:** `A1:result`. **Limitations:** Methods unavailable.';
    const user = userEvent.setup();
    render(<ResearchApp snapshot={saved} />);
    await user.click(screen.getByRole('button', { name: 'Activity' }));
    const card = screen.getByText('5 of 20', { selector: 'strong' }).closest('li')!;
    expect(within(card).getByRole('heading', { name: 'Task assignment' })).toBeInTheDocument();
    expect(within(card).getByRole('heading', { name: 'Saved result' })).toBeInTheDocument();
    expect(within(card).getAllByText('Agent Acquisition peer').length).toBeGreaterThan(0);
    expect(within(card).getByRole('region', { name: 'Citations' })).toHaveTextContent('A1:result');
    expect(within(card).getByRole('region', { name: 'Limitations' })).toHaveTextContent('Methods unavailable.');
    expect([...card.querySelectorAll('time')].some(time => time.getAttribute('datetime') === saved.attempts[0].endedAt)).toBe(true);
  });

  it('allows keyboard navigation into the Activity view and help', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={snapshot} />);
    await user.tab();
    expect(screen.getByRole('button', { name: 'Start or follow up' })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole('button', { name: /How to use Start or follow up view/ })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole('button', { name: 'Research review' })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole('button', { name: /How to use Research review view/ })).toHaveFocus();
    await user.tab();
    expect(screen.getByRole('button', { name: 'Activity' })).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(screen.getByRole('heading', { name: 'Activity' })).toBeInTheDocument();
  });

  it('clears the local active review, loads the saved sample, and replays its trace', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={snapshot} />);
    const nav = screen.getByRole('navigation', { name: 'Primary navigation' });
    expect(within(nav).getByText('Current task')).toBeInTheDocument();
    expect(within(nav).getAllByText('Start or follow up')).toHaveLength(2);
    expect(within(nav).getByText('Saved examples')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Clear review' }));
    expect(screen.getByRole('heading', { name: 'Active review cleared' })).toBeInTheDocument();
    expect(within(nav).getByRole('button', { name: 'Communication' })).toBeDisabled();
    await user.click(screen.getByRole('button', { name: 'Open Previous samples' }));
    expect(screen.getByText(/does not store or list actual prior agent runs/)).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Replay saved activity' }));
    expect(screen.getByText(/Replaying saved sample · 0 of/)).toBeInTheDocument();
    expect(screen.getByText(/no agents are running/)).toBeInTheDocument();
    expect(await screen.findByText('Sample mission opened.')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Clear review' }));
    await user.click(within(nav).getByRole('button', { name: 'Previous samples' }));
    await user.click(screen.getByRole('button', { name: 'Load into active review' }));
    expect(screen.getByRole('region', { name: 'Research task input' })).toBeInTheDocument();
  });
});
