/* Check whether a reviewer can navigate the saved research sample.
   These browser-like tests follow a finding to its cited passage, inspect peer
   requests and attempts, preview follow-up work, and use the keyboard. They use
   fixed data and do not contact a research backend. */
import { render, screen, waitFor, within } from '@testing-library/react';
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
  it('opens the exact task behind an evidence claim and labels a missing task honestly', async () => {
    const user = userEvent.setup();
    const data = structuredClone(snapshot);
    data.claims = [data.claims[0]];
    const claim = data.claims[0];
    const task = data.tasks.find(item => item.id === claim.taskId)!;
    task.displayNumber = 7;
    const view = render(<ResearchApp snapshot={data} initialScreen="evidence" />);
    await user.click(screen.getByRole('button', { name: 'Open Task 7 behind this claim →' }));
    const selected = document.getElementById('selected-task')!;
    expect(within(selected).getByText('Exact assignment').closest('details')).not.toHaveAttribute('open');
    await user.click(within(selected).getByText('Exact assignment'));
    expect(within(selected).getByText(task.title, { selector: 'p' })).toBeVisible();
    expect(within(selected).getByText(task.id)).toBeVisible();
    expect(selected).toHaveTextContent('Task 7');
    await user.click(screen.getByRole('button', { name: 'Evidence' }));
    const missing = structuredClone(data);
    missing.claims[0].taskId = 'missing-claim-task';
    view.rerender(<ResearchApp snapshot={missing} initialScreen="evidence" />);
    expect(screen.getByText('The task linked to this claim is unavailable in this snapshot.')).toBeVisible();
    expect(screen.queryByRole('button', { name: /behind this claim/ })).not.toBeInTheDocument();
  });

  it('uses this run’s saved peer name rather than the current roster in Research review', async () => {
    const historical = structuredClone(snapshot);
    const alias = 'agent-literature-acquisition';
    const live = {
      runId: 'historical-run', model: 'saved model', error: null, form: null, requestedAgentIds: [alias],
      requiredAgentIds: [alias], agentNames: { [alias]: 'Nova' }, onSelectRun: vi.fn(),
      runs: [{ id: 'historical-run', question: historical.mission.question, status: 'completed',
        agentIds: [alias], requestedAgentNames: { [alias]: 'Literature acquisition' } }],
    };
    const view = render(<ResearchApp snapshot={historical} live={live} />);
    await userEvent.click(screen.getByText('Participating agents and completion requirements'));
    const peers = screen.getByText(/Requested starting peers: Literature acquisition/).closest('p')!;
    expect(peers).toHaveTextContent('Requested starting peers: Literature acquisition');
    expect(peers).toHaveTextContent('Required contributions: Literature acquisition');
    expect(peers).not.toHaveTextContent('Nova');

    view.rerender(<ResearchApp snapshot={historical} live={{ ...live, runs: [{
      id: 'historical-run', question: historical.mission.question, status: 'completed', agentIds: [alias],
    }] }} />);
    expect(screen.getByText(/Requested starting peers: Name not recorded/).closest('p')).toHaveTextContent(`Name not recorded (${alias})`);
    expect(screen.getByText(/Requested starting peers: Name not recorded/).closest('p')).not.toHaveTextContent('Nova');
  });

  it('selects an available source after a run change while preserving an explicit missing citation target', async () => {
    const user = userEvent.setup();
    const first = structuredClone(snapshot);
    const second = structuredClone(snapshot);
    second.snapshotId = 'other-run-snapshot';
    second.sources = [second.sources[0]];
    second.claims[0].supports = [{ sourceId: 'missing-exact-source', spanId: 'missing-span' }];
    const live = (runId: string) => ({ runId, model: 'saved model', runs: [], error: null, form: null,
      requestedAgentIds: [], onSelectRun: vi.fn() });
    const view = render(<ResearchApp snapshot={first} live={live('first-run')} />);
    await user.click(screen.getByRole('button', { name: 'Evidence' }));
    const oldSource = within(screen.getByRole('region', { name: 'Saved source copies' }))
      .getByRole('button', { name: new RegExp(first.sources[1].title) });
    await user.click(oldSource);
    expect(within(document.getElementById('selected-source-panel')!).getByRole('heading', { name: first.sources[1].title })).toBeInTheDocument();
    expect(oldSource).toHaveAttribute('aria-pressed', 'true');

    view.rerender(<ResearchApp snapshot={second} live={live('second-run')} />);
    const detail = document.getElementById('selected-source-panel')!;
    expect(within(detail).getByRole('heading', { name: second.sources[0].title })).toBeInTheDocument();
    expect(within(detail).queryByText('The requested source is not available in this snapshot.')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /missing-exact-source.*missing-span/ }));
    expect(within(detail).getByText('The requested source is not available in this snapshot.')).toBeInTheDocument();
    const refreshed = { ...second, snapshotId: 'other-run-refreshed' };
    view.rerender(<ResearchApp snapshot={refreshed} live={live('second-run')} />);
    expect(within(document.getElementById('selected-source-panel')!).getByText('The requested source is not available in this snapshot.')).toBeInTheDocument();
  });

  it('selects the new run’s saved task when it has no agent attempt yet', async () => {
    const user = userEvent.setup();
    const first = structuredClone(snapshot);
    const second = structuredClone(snapshot);
    second.snapshotId = 'waiting-run-snapshot';
    second.attempts = [];
    second.tasks = [second.tasks.find(task => task.id === 'task-a2')!];
    second.tasks[0].status = 'queued';
    const live = (runId: string) => ({ runId, model: 'saved model', runs: [], error: null, form: null,
      requestedAgentIds: [], onSelectRun: vi.fn() });
    const view = render(<ResearchApp snapshot={first} live={live('finished-run')} />);
    await user.click(screen.getByRole('button', { name: 'Details' }));
    view.rerender(<ResearchApp snapshot={second} live={live('waiting-run')} />);
    const selected = document.getElementById('selected-task')!;
    await user.click(within(selected).getByText('Exact assignment'));
    expect(within(selected).getByText(second.tasks[0].title, { selector: 'p' })).toBeVisible();
    expect(within(selected).getByText(second.tasks[0].id)).toBeVisible();
    expect(within(document.getElementById('selected-task')!).getByText('No agent attempt is recorded for this task. A saved request does not establish that work started.')).toBeInTheDocument();
  });

  it('shows an empty Evidence state, then selects the first copy saved in that same run', async () => {
    const user = userEvent.setup();
    const empty = structuredClone(snapshot);
    empty.sources = [];
    const live = { runId: 'growing-run', model: 'saved model', runs: [], error: null, form: null,
      requestedAgentIds: [], onSelectRun: vi.fn() };
    const view = render(<ResearchApp snapshot={empty} live={live} />);
    await user.click(screen.getByRole('button', { name: 'Evidence' }));
    expect(within(document.getElementById('selected-source-panel')!).getByText('No saved source copy is available in this snapshot.')).toBeInTheDocument();
    const withCopy = { ...empty, snapshotId: 'growing-run-with-copy', sources: [snapshot.sources[0]] };
    view.rerender(<ResearchApp snapshot={withCopy} live={live} />);
    expect(within(document.getElementById('selected-source-panel')!).getByRole('heading', { name: snapshot.sources[0].title })).toBeInTheDocument();
  });

  it('does not carry a local Evidence note or unsent annotation into another run', async () => {
    const user = userEvent.setup();
    const first = structuredClone(snapshot);
    const second = structuredClone(snapshot);
    second.snapshotId = 'second-note-run';
    const live = (runId: string) => ({ runId, model: 'saved model', runs: [], error: null, form: null,
      requestedAgentIds: [], onSelectRun: vi.fn() });
    const view = render(<ResearchApp snapshot={first} live={live('note-run-a')} />);
    await user.click(screen.getByRole('button', { name: 'Evidence' }));
    const note = screen.getByLabelText(/Your note about/);
    await user.type(note, 'Check the original interval.');
    await user.click(screen.getByRole('button', { name: 'Save local review note' }));
    expect(screen.getByText('Check the original interval.')).toBeInTheDocument();
    await user.type(note, 'Unsent note for run A.');

    view.rerender(<ResearchApp snapshot={second} live={live('note-run-b')} />);
    expect(screen.getByLabelText(/Your note about/)).toHaveValue('');
    expect(screen.queryByText('Check the original interval.')).not.toBeInTheDocument();
    expect(screen.queryByDisplayValue('Unsent note for run A.')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Save local review note' })).toBeDisabled();
  });

  it('keeps a long live retry history compact without hiding the selected attempt or saved output', async () => {
    const user = userEvent.setup();
    const saved = structuredClone(snapshot);
    saved.proofKind = 'live_mission_pending';
    const task = saved.tasks.find(item => item.id === 'task-a2')!;
    task.status = 'waiting';
    task.waitingReason = 'Provider quota wait';
    const template = saved.attempts.find(item => item.taskId === task.id)!;
    saved.attempts = Array.from({ length: 18 }, (_, index) => ({
      ...template,
      id: `saved-retry-${index + 1}`,
      status: (index === 17 ? 'completed' : 'failed') as 'completed' | 'failed',
      resultSummary: index === 17 ? 'Final saved bounded result.' : undefined,
      toolCalls: [{ name: 'search_catalog', status: index === 17 ? 'completed' : 'failed' }],
      startedAt: `2026-10-08T14:${String(index).padStart(2, '0')}:00Z`,
      endedAt: `2026-10-08T14:${String(index).padStart(2, '0')}:30Z`,
    }));
    render(<ResearchApp snapshot={saved} live={{ runId: 'long-run', model: 'saved model', runs: [], error: null, form: null, requestedAgentIds: [], onSelectRun: vi.fn() }} />);
    expect(screen.getByText('18 saved steps across this run')).toBeVisible();
    expect(screen.getByRole('region', { name: 'Grouped work overview' })).toBeVisible();
    expect(screen.queryByText('Browse all saved work steps')).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Activity' }));
    await user.click(screen.getByText('Browse all saved work steps'));
    expect(screen.getByText('Browse all saved work steps').closest('details')).toHaveAttribute('open');
    const timeline = screen.getByRole('group', { name: 'Recorded agent attempts' });
    expect(timeline.querySelectorAll('.agent-step')).toHaveLength(18);
    expect([...timeline.querySelectorAll('.agent-step-details')].every(row => !row.hasAttribute('open'))).toBe(true);
    await user.click(within(timeline).getByRole('button', { name: /^Open work step 18:/ }));
    const latest = within(timeline).getByRole('button', { name: /^Open work step 18:/ }).closest('.agent-step')!;
    expect(within(latest as HTMLElement).getByText('Result').parentElement).toHaveTextContent('Tool result saved');
    expect(within(latest as HTMLElement).getAllByText('Final saved bounded result.')[0]).toBeVisible();
    await user.click(within(latest as HTMLElement).getByText('Technical records and current task state'));
    expect(within(latest as HTMLElement).getByText(/Current task wait, as of/).closest('p')).toHaveTextContent('Provider quota wait');
    await user.click(within(timeline).getByRole('button', { name: /^Open work step 1:/ }));
    const first = within(timeline).getByRole('button', { name: /^Open work step 1:/ }).closest('.agent-step')!;
    expect(within(first as HTMLElement).getByText('No result summary is saved for this step.')).toBeVisible();
    await user.click(within(first as HTMLElement).getByText('Technical records and current task state'));
    await user.click(within(first as HTMLElement).getByRole('button', { name: 'Open selected attempt in Details →' }));
    const selected = document.getElementById('selected-attempt')!;
    expect(selected.querySelector('.eyebrow')).toHaveTextContent('This run · work step 1');
    await user.click(within(selected).getByText('Exact identity, timing, model and skills'));
    expect(within(selected).getByText('Attempt saved-retry-1 · task task-a2')).toBeVisible();
  });

  it('shows selected task work in saved start order with neutral numbers and exact times', async () => {
    const user = userEvent.setup();
    const saved = structuredClone(snapshot);
    saved.proofKind = 'live_mission_pending';
    const template = saved.attempts.find(item => item.taskId === 'task-a2')!;
    saved.attempts = [
      ['attempt-z-late', '2026-10-08T14:06:00Z'],
      ['attempt-b-tie', '2026-10-08T14:04:00Z'],
      ['attempt-m-first', '2026-10-08T14:02:00Z'],
      ['attempt-a-tie', '2026-10-08T14:04:00Z'],
    ].map(([id, startedAt]) => ({ ...template, id, startedAt, endedAt: startedAt, resultSummary: `Saved action ${id}.` }));
    render(<ResearchApp snapshot={saved} live={{ runId: 'scrambled-run', model: 'saved model', runs: [], error: null, form: null, requestedAgentIds: [], onSelectRun: vi.fn() }} />);

    await user.click(screen.getByRole('button', { name: 'Activity' }));
    await user.click(screen.getByText('Browse all saved work steps'));
    expect(screen.getByText('Browse all saved work steps').closest('details')).toHaveAttribute('open');
    const timeline = screen.getByRole('group', { name: 'Recorded agent attempts' });
    const rows = [...timeline.querySelectorAll('.agent-step')];
    expect(rows).toHaveLength(4);
    expect(rows.map(row => row.querySelector('time')?.getAttribute('datetime'))).toEqual([
      '2026-10-08T14:02:00Z', '2026-10-08T14:04:00Z',
      '2026-10-08T14:04:00Z', '2026-10-08T14:06:00Z',
    ]);
    await user.click(within(rows[0] as HTMLElement).getByRole('button'));
    expect(within(rows[0] as HTMLElement).getAllByText('Saved action attempt-m-first.')[0]).toBeVisible();
    await user.click(within(rows[2] as HTMLElement).getByRole('button'));
    expect(within(rows[2] as HTMLElement).getByText('Saved action attempt-b-tie.')).toBeVisible();
  });

  it('links continuing saved tasks when the requested live scope is complete', async () => {
    const user = userEvent.setup();
    const saved = structuredClone(snapshot);
    saved.proofKind = 'live_mission_pending';
    saved.mission.status = 'completed';
    const methods = saved.tasks.find(task => task.id === 'task-methods')!;
    methods.version = 3;
    const live = { runId: 'run-1', model: 'saved model', runs: [{ id: 'run-1', question: 'Review', status: 'completed', continuingTaskIds: ['task-methods', 'task-sibling'], participationPolicyVersion: 2 }], error: null, form: null, requestedAgentIds: [], requiredAgentIds: [], participationPolicyVersion: 2, onSelectRun: vi.fn() };
    const view = render(<ResearchApp snapshot={saved} live={live} />);
    expect(screen.getByText(/Requested scope complete; additional work continues/, { selector: '.context-summary' })).toBeInTheDocument();
    await user.click(screen.getByText('Technical run details'));
    expect(within(screen.getByRole('region', { name: 'This run at a glance' })).getByText('run-1')).toBeVisible();
    expect(screen.getByText('Requested scope complete; additional work continues in this saved snapshot.')).toBeInTheDocument();
    expect(within(screen.getByRole('region', { name: 'Current saved output' })).getByText('Requested scope complete')).toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: /Seek permitted methods and adverse-event detail · waiting · v3/ }));
    const selected = document.getElementById('selected-task')!;
    await user.click(within(selected).getByText('Exact assignment'));
    expect(within(selected).getByText('Seek permitted methods and adverse-event detail', { selector: 'p' })).toBeVisible();
    expect(within(selected).getByText('task-methods')).toBeVisible();
    expect(screen.getByText('Opened from exact work version 3')).toBeInTheDocument();
    view.unmount();

    saved.tasks = saved.tasks.map(task => ({ ...task, status: 'completed' as const }));
    saved.tasks.find(task => task.id === 'task-methods')!.status = 'queued';
    live.runs[0].continuingTaskIds = [];
    render(<ResearchApp snapshot={saved} live={live} />);
    expect(screen.queryByText(/additional work continues/)).not.toBeInTheDocument();
  });

  it('shows one research task from input through agent work to the saved output on one screen', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={snapshot} comparisonSnapshot={baseline as ResearchSnapshot} />);
    const input = screen.getByRole('region', { name: 'Research task input' });
    expect(within(input).getByText(/Simulated abstracts with real clinical labels/)).toBeInTheDocument();
    expect(within(input).getByRole('heading', { name: 'Original research request' })).toBeInTheDocument();
    expect(input.querySelector('.original-research-request')).toHaveTextContent(/adults with depression.*cognitive behavioural therapy/);
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
    expect(within(input).getByRole('heading', { name: 'Original research request' })).toBeVisible();
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
    expect(within(contribution).getByText('Timing expectation:').closest('p')).toHaveTextContent('Bounded review of interval and comparator.');
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
    const selectSavedAttempt = async (id: string) => {
      const startedAt = snapshot.attempts.find(attempt => attempt.id === id)!.startedAt;
      const list = screen.getByRole('heading', { name: /Inspect a work step/ }).closest('section')!;
      const button = [...list.querySelectorAll<HTMLButtonElement>('.attempt-item')]
        .find(item => item.querySelector('time')?.getAttribute('datetime') === startedAt);
      expect(button).toBeDefined();
      await user.click(button!);
      const selected = document.getElementById('selected-attempt')!;
      const exact = within(selected).getByText('Exact identity, timing, model and skills').closest('details')!;
      if (!exact.open) await user.click(within(exact).getByText('Exact identity, timing, model and skills'));
      expect(within(exact).getByText(new RegExp(`Attempt ${id} · task`))).toBeVisible();
    };
    await selectSavedAttempt('attempt-critique');
    expect(screen.getByText(/This is incomplete; no missing continuation is inferred/)).toBeInTheDocument();
    await selectSavedAttempt('attempt-a2');
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
    const selected = document.getElementById('selected-task')!;
    await user.click(within(selected).getByText('Exact assignment'));
    expect(within(selected).getByText('Find an independent adult comparison', { selector: 'p' })).toBeVisible();
    expect(within(selected).getByText('task-a2')).toBeVisible();
    expect(screen.getByText('report-v1')).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /attempt-a2-failed/ })).toBeInTheDocument();
    expect(screen.getByRole('button', { name: /Inspect revised claim/ })).toBeInTheDocument();
  });

  it('distinguishes disconnected and empty previews from a completed mission', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={snapshot} />);
    await user.click(screen.getByRole('button', { name: 'Details' }));
    await user.click(screen.getByText('Run identifiers and model settings'));
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
    const earlier = screen.getByRole('region', { name: 'Earlier sample run' });
    const current = screen.getByRole('region', { name: 'Current sample run' });
    const earlierIds = within(earlier).getByText('Run and saved-view details').closest('details')!;
    const currentIds = within(current).getByText('Run and saved-view details').closest('details')!;
    await user.click(within(earlierIds).getByText('Run and saved-view details'));
    await user.click(within(currentIds).getByText('Run and saved-view details'));
    expect(within(earlierIds).getByText(/1 saved source copy/)).toBeVisible();
    expect(within(currentIds).getByText(/4 saved source copies/)).toBeVisible();
    expect(within(earlierIds).getByText(/mission-fictional-adult-baseline/)).toBeVisible();
    expect(within(currentIds).getByText(/mission-fictional-adult-review/)).toBeVisible();
    expect(screen.getByText(/A later answer is not necessarily better/)).toBeInTheDocument();
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
    expect(timestamp).toHaveAttribute('datetime', '2026-10-08T14:02:00Z');
    expect(failedAttempt).toHaveTextContent('Oct 8, 2026');
    expect(timestamp).toHaveTextContent(/\d:\d{2}:\d{2} (AM|PM) [A-Z]/);
    await user.click(within(failedAttempt!).getByText('Expand activity details'));
    expect(within(failedAttempt!).getByText('Result').parentElement).toHaveTextContent('failed');
    expect([...failedAttempt!.querySelectorAll('time')].some(time => time.getAttribute('datetime') === '2026-10-08T14:03:00Z')).toBe(true);
    expect(within(failedAttempt!).getByText(/First route returned no accessible adult comparison/, { selector: '.step-result-preview *' })).toBeVisible();
    await user.click(screen.getByRole('button', { name: 'Messages' }));
    expect(screen.getAllByText('Find an independent adult attention-control study at six weeks.')[0]).toBeVisible();
    expect(screen.queryByText(/First route returned no accessible adult comparison/)).not.toBeInTheDocument();
  });

  it('keeps one exact Activity row expanded and restores the chronological scan after filtering', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={snapshot} />);
    await user.click(screen.getByRole('button', { name: 'Activity' }));
    const first = screen.getAllByRole('button', { name: 'Expand activity details' })[0];
    const firstRow = first.closest('li')!;
    const firstAttemptId = firstRow.dataset.attemptId!;
    expect(first).toHaveAttribute('aria-expanded', 'false');
    expect(first).not.toHaveAttribute('aria-controls');
    for (const id of first.getAttribute('aria-describedby')!.split(' ')) expect(document.getElementById(id)?.textContent).toBeTruthy();
    expect(within(firstRow).queryByText('Technical records and current task state', { selector: 'summary' })).not.toBeInTheDocument();
    await user.click(first);
    expect(within(firstRow).getByText('Technical records and current task state', { selector: 'summary' })).toBeVisible();
    const openControl = within(firstRow).getByRole('button', { name: 'Hide activity details' });
    expect(document.getElementById(openControl.getAttribute('aria-controls')!)).not.toBeNull();
    const second = screen.getAllByRole('button', { name: 'Expand activity details' })[0];
    await user.click(second);
    expect(within(firstRow).queryByText('Technical records and current task state', { selector: 'summary' })).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Recorded issues' }));
    expect(screen.getByRole('button', { name: 'Recorded issues' })).toHaveAttribute('aria-pressed', 'true');
    await user.click(screen.getByRole('button', { name: 'Clear filters' }));
    expect(screen.getByRole('button', { name: 'All' })).toHaveAttribute('aria-pressed', 'true');
    expect(screen.getByRole('button', { name: 'Recorded issues' })).toHaveAttribute('aria-pressed', 'false');
    await user.click(screen.getByRole('button', { name: 'Hide activity details' }));
    expect(screen.getAllByRole('button', { name: 'Expand activity details' }).length).toBe(snapshot.events.length + snapshot.attempts.length + snapshot.messages.length);
    await user.click(screen.getByRole('button', { name: 'Agent work' }));
    const restored = document.querySelector<HTMLElement>(`[data-attempt-id="${firstAttemptId}"]`)!;
    await user.click(within(restored).getByRole('button', { name: 'Expand activity details' }));
    await user.click(within(restored).getByText('Technical records and current task state'));
    await user.click(within(restored).getByRole('button', { name: 'Open selected attempt in Details →' }));
    const firstAttempt = snapshot.attempts.find(attempt => attempt.id === firstAttemptId)!;
    const selected = document.getElementById('selected-attempt')!;
    expect(selected.querySelector('.eyebrow')).toHaveTextContent(/This run · work step \d+/);
    await user.click(within(selected).getByText('Exact identity, timing, model and skills'));
    expect(within(selected).getByText(new RegExp(`Attempt ${firstAttempt.id} · task`))).toBeVisible();
  });

  it('marks only a newly saved live work step and opens its exact record in one click', async () => {
    const user = userEvent.setup();
    const first = structuredClone(snapshot);
    first.proofKind = 'live_mission_pending';
    const live = { runId: 'progress-run', model: 'saved model', runs: [], error: null, form: null,
      requestedAgentIds: [], onSelectRun: vi.fn() };
    const view = render(<ResearchApp snapshot={first} live={live} />);
    await user.click(screen.getByRole('button', { name: 'Activity' }));
    expect(screen.getByRole('region', { name: 'Current saved work' })).toHaveTextContent('Viewer checks do not mean research progressed');
    expect(screen.queryByText('New since opening')).not.toBeInTheDocument();

    const later = structuredClone(first);
    later.snapshotId = 'progress-run-next-snapshot';
    const newAttempt = { ...later.attempts.find(attempt => attempt.id === 'attempt-methods-wait')!,
      id: 'attempt-new', startedAt: '2026-10-08T15:30:00Z', endedAt: null,
      toolCalls: [...later.attempts.find(attempt => attempt.id === 'attempt-methods-wait')!.toolCalls] };
    later.attempts.push(newAttempt);
    later.tasks.find(task => task.id === newAttempt.taskId)!.attemptIds.push(newAttempt.id);
    view.rerender(<ResearchApp snapshot={later} live={live} />);
    const marker = await screen.findByText('New since opening');
    const row = marker.closest('li')!;
    expect(within(row).getByText('Waiting in saved state')).toBeVisible();
    expect(screen.getByText(/1 new work step; 0 updated work steps since this run was opened/)).toBeVisible();
    await user.click(within(row).getByRole('button', { name: 'Expand activity details' }));
    await user.click(within(row).getByText('Technical records and current task state'));
    await user.click(within(row).getByRole('button', { name: 'Open selected attempt in Details →' }));
    const selected = document.getElementById('selected-attempt')!;
    await user.click(within(selected).getByText('Exact identity, timing, model and skills'));
    expect(within(selected).getByText(/Attempt attempt-new · task/)).toBeVisible();
    await user.click(screen.getByRole('button', { name: 'Activity' }));
    expect(screen.queryByText('New since opening')).not.toBeInTheDocument();

    const revised = structuredClone(later);
    revised.snapshotId = 'progress-run-tool-result';
    revised.attempts.find(attempt => attempt.id === 'attempt-new')!.toolCalls.push({ name: 'inspect_hit', status: 'rejected' });
    view.rerender(<ResearchApp snapshot={revised} live={live} />);
    const updated = await screen.findByText('Updated since opening');
    expect(within(updated.closest('li')!).getByRole('button', { name: /activity details/ })).toBeVisible();
    view.rerender(<ResearchApp snapshot={revised} live={{ ...live, runId: 'another-run' }} />);
    expect(screen.queryByText('Updated since opening')).not.toBeInTheDocument();
  });

  it('follows only newly changed visible work after opting in, and stops on leaving Activity', async () => {
    const user = userEvent.setup();
    const previousScroll = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollIntoView');
    const scrolled: HTMLElement[] = [];
    Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', {
      configurable: true, value: function(this: HTMLElement) { scrolled.push(this); },
    });
    try {
      const first = structuredClone(snapshot);
      first.proofKind = 'live_mission_pending';
      const live = { runId: 'follow-run', model: 'saved model', runs: [], error: null, form: null,
        requestedAgentIds: [], onSelectRun: vi.fn() };
      const view = render(<ResearchApp snapshot={first} live={live} />);
      await user.click(screen.getByRole('button', { name: 'Activity' }));
      const control = screen.getByRole('button', { name: 'Follow latest off' });
      expect(control).toHaveAttribute('aria-pressed', 'false');
      expect(scrolled).toHaveLength(0);
      const taskTitle = first.tasks.find(task => task.status === 'waiting')!.title;
      expect(screen.getByTitle(taskTitle)).toHaveTextContent(taskTitle);
      await user.click(control);
      expect(screen.getByRole('button', { name: 'Follow latest on' })).toHaveAttribute('aria-pressed', 'true');
      expect(scrolled).toHaveLength(0);
      await user.click(screen.getByRole('button', { name: 'Messages' }));

      const hiddenUpdate = structuredClone(first);
      hiddenUpdate.snapshotId = 'follow-run-hidden-update';
      const waiting = first.attempts.find(attempt => attempt.id === 'attempt-methods-wait')!;
      hiddenUpdate.attempts.push({ ...waiting, id: 'attempt-follow-hidden', startedAt: '2026-10-08T16:00:00Z', endedAt: null });
      view.rerender(<ResearchApp snapshot={hiddenUpdate} live={live} />);
      expect(scrolled).toHaveLength(0);
      await user.click(screen.getByRole('button', { name: 'Agent work' }));
      expect(scrolled).toHaveLength(0);

      const visibleUpdate = structuredClone(hiddenUpdate);
      visibleUpdate.snapshotId = 'follow-run-visible-update';
      visibleUpdate.attempts.push({ ...waiting, id: 'attempt-follow-visible', startedAt: '2026-10-08T16:01:00Z', endedAt: null });
      view.rerender(<ResearchApp snapshot={visibleUpdate} live={live} />);
      await waitFor(() => expect(scrolled).toHaveLength(1));
      expect(scrolled[0]).toHaveAttribute('data-attempt-id', 'attempt-follow-visible');
      await user.click(screen.getByRole('button', { name: 'Details' }));
      await user.click(screen.getByRole('button', { name: 'Activity' }));
      expect(screen.getByRole('button', { name: 'Follow latest off' })).toHaveAttribute('aria-pressed', 'false');
      await user.click(screen.getByRole('button', { name: 'Follow latest off' }));
      view.rerender(<ResearchApp snapshot={visibleUpdate} live={{ ...live, runId: 'different-run' }} />);
      expect(screen.getByRole('button', { name: 'Follow latest off' })).toHaveAttribute('aria-pressed', 'false');
      expect(scrolled).toHaveLength(1);
    } finally {
      if (previousScroll) Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', previousScroll);
      else Reflect.deleteProperty(HTMLElement.prototype, 'scrollIntoView');
    }
  });

  it('names recorded participants without claiming a peer exchange when Communication is empty', async () => {
    const user = userEvent.setup();
    const saved = structuredClone(snapshot);
    saved.messages = [];
    render(<ResearchApp snapshot={saved} />);
    await user.click(screen.getByRole('button', { name: 'Communication' }));
    const empty = screen.getByRole('heading', { name: 'No peer messages recorded' }).closest('section')!;
    expect(within(empty).getByText(/This snapshot contains recorded work by/)).toHaveTextContent('Acquisition');
    expect(within(empty).getByText(/No directed peer request is saved here/)).toBeVisible();
    await user.click(within(empty).getByRole('button', { name: 'See saved agent activity →' }));
    expect(screen.getByRole('heading', { name: 'Activity' })).toBeVisible();
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
    expect(screen.getByText(/No saved activity matches the selected category/)).toBeInTheDocument();
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
    const card = screen.getByText(/Observed \*\*5 of 20\*\*/, { selector: '.activity-compact-summary' }).closest('li')!;
    expect(within(card).queryByText('Task assignment', { selector: 'summary' })).not.toBeInTheDocument();
    await user.click(within(card).getByRole('button', { name: 'Expand activity details' }));
    expect(within(card).getByText('Technical records and current task state', { selector: 'summary' })).toBeInTheDocument();
    expect(within(card).queryByText('Work toward the research question')).not.toBeInTheDocument();
    const result = card.querySelector<HTMLElement>('.step-result-preview')!;
    expect(result).toHaveTextContent('5 of 20');
    expect(within(result).getByRole('region', { name: 'Citations' })).toHaveTextContent('A1:result');
    expect(within(result).getByRole('region', { name: 'Limitations' })).toHaveTextContent('Methods unavailable.');
    expect([...card.querySelectorAll('time')].some(time => time.getAttribute('datetime') === saved.attempts[0].endedAt)).toBe(true);
  });

  it('allows keyboard navigation into the Communication view and help', async () => {
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
    expect(screen.getByRole('button', { name: 'Communication' })).toHaveFocus();
    await user.keyboard('{Enter}');
    expect(screen.getByRole('heading', { name: 'Communication' })).toBeInTheDocument();
  });

  it('clears the local active review, loads the saved sample, and replays its trace', async () => {
    const user = userEvent.setup();
    render(<ResearchApp snapshot={snapshot} />);
    const nav = screen.getByRole('navigation', { name: 'Primary navigation' });
    expect(within(nav).getByText('Selected run')).toBeInTheDocument();
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

it('orders a task’s work steps by saved time and keeps the timeline step number', async () => {
  const user = userEvent.setup();
  const data = structuredClone(snapshot);
  const template = data.attempts[0];
  data.attempts = [
    { ...template, id: 'step-later', startedAt: '2026-10-10T12:02:00Z', endedAt: '2026-10-10T12:03:00Z' },
    { ...template, id: 'step-first', startedAt: '2026-10-10T12:00:00Z', endedAt: '2026-10-10T12:01:00Z' },
  ];
  render(<ResearchApp snapshot={data} live={{ runId: 'ordered-step-run', model: 'saved model', runs: [], error: null,
    form: null, requestedAgentIds: [], onSelectRun: vi.fn() }} />);
  await user.click(screen.getByRole('button', { name: 'Details' }));
  const task = document.getElementById('selected-task')!;
  const buttons = [...task.querySelectorAll<HTMLButtonElement>('.attempt-item')];
  expect(buttons).toHaveLength(2);
  expect(buttons[0]).toHaveTextContent('work step 1');
  expect(buttons[0]).toHaveTextContent('step-first');
  expect(buttons[1]).toHaveTextContent('work step 2');
  expect(buttons[1]).toHaveTextContent('step-later');
});

it('keeps selected-run identity across tabs and workspace screens separate', async () => {
  const user = userEvent.setup();
  const select = vi.fn();
  const saved = structuredClone(snapshot);
  saved.parentSnapshotId = 'earlier-snapshot';
  const run = { id: 'run-selected', title: 'Independent abstract review', question: saved.mission.question,
    status: 'waiting', createdAt: '2026-10-01T12:30:00Z', parentRunId: 'run-earlier' };
  render(<ResearchApp snapshot={saved} live={{ runId: run.id, runs: [run], model: 'saved model', error: null, form: <p>Draft input</p>, requestedAgentIds: [], onSelectRun: select }} />);
  for (const name of ['Activity', 'Communication', 'Evidence', 'Details', 'Compare']) {
    await user.click(screen.getByRole('button', { name }));
    const strip = screen.getByRole('region', { name: 'This run at a glance' });
    expect(within(strip).getByText(run.title)).toBeVisible();
    expect(within(strip).getByText('Run 1 · Selected run')).toBeVisible();
    expect(strip.querySelector('time')).toHaveAttribute('datetime', run.createdAt);
    expect(within(strip).getByText(run.id, { selector: 'code' })).toBeVisible();
  }
  await user.click(screen.getByRole('button', { name: 'Open earlier run →' }));
  expect(select).toHaveBeenCalledWith('run-earlier');
  for (const name of ['Saved runs', 'Start or follow up']) {
    await user.click(screen.getByRole('button', { name }));
    expect(screen.getByText(/Workspace-wide/)).toBeVisible();
    expect(screen.queryByRole('region', { name: 'This run at a glance' })).not.toBeInTheDocument();
  }
});

it('offers direct Details section jumps without replacing the selected exact work step', async () => {
  const user = userEvent.setup();
  render(<ResearchApp snapshot={snapshot} initialScreen="diagnostics" />);
  const step = snapshot.attempts[1];
  const history = document.getElementById('details-step-history')!;
  await user.click(within(history).getAllByRole('button').find(button => button.textContent?.includes('work step 2'))!);
  const jumps = screen.getByRole('navigation', { name: 'Jump within Details' });
  await user.click(within(jumps).getByRole('button', { name: 'Selected step ↓' }));
  expect(document.getElementById('selected-attempt')).toHaveFocus();
  expect(document.getElementById('selected-attempt')).toHaveTextContent(`Attempt ${step.id}`);
  await user.click(within(jumps).getByRole('button', { name: 'Operational events ↓' }));
  expect(document.getElementById('details-operational-events')).toHaveFocus();
});

it('explains each review tab in one line and offers optional next-view controls without changing the saved run', async () => {
  const user = userEvent.setup();
  render(<ResearchApp snapshot={snapshot} comparisonSnapshot={baseline as ResearchSnapshot} />);
  expect(screen.getByText('See the original request, saved work and current output together.')).toBeVisible();
  const nav = screen.getByRole('navigation', { name: 'Primary navigation' });
  expect(within(nav).getAllByRole('button').filter(button => button.classList.contains('nav-item')).map(button => button.textContent)).toEqual(['Start or follow up', 'Research review', 'Communication', 'Evidence', 'Compare', 'Activity', 'Details', 'Previous samples']);
  await user.click(screen.getByRole('button', { name: 'Next: Communication →' }));
  expect(screen.getByRole('button', { name: '← Back to Research review' })).toBeVisible();
  expect(screen.getByText(/See who asked, replied and reviewed/)).toBeVisible();
  await user.click(screen.getByRole('button', { name: 'Next: Evidence →' }));
  expect(screen.getByText('Check saved sources and passages against each claim or answer.')).toBeVisible();
  await user.click(screen.getByRole('button', { name: 'Next: Compare →' }));
  expect(screen.getByText('Compare the earlier and current answers, each with its own saved sources.')).toBeVisible();
  await user.click(screen.getByRole('button', { name: 'Inspect the sequence → Activity' }));
  expect(screen.getByText(/Follow saved actions, model calls, errors and waits in time order/)).toBeVisible();
  await user.click(screen.getByRole('button', { name: 'Next: Details →' }));
  expect(screen.getByText('Inspect exact task requests, versions, timing and waiting records.')).toBeVisible();
  await user.click(screen.getByRole('button', { name: 'Work step records' }));
  expect(screen.getByText('Inspect exact work-step calls, tool results, model settings and recovery records.')).toBeVisible();
  await user.click(screen.getByRole('button', { name: 'Return to Research review →' }));
  expect(screen.getByRole('heading', { name: 'Research review' })).toBeVisible();
});

it('groups by exact task and preserves expanded step and filters when switching to time order', async () => {
  const user = userEvent.setup();
  render(<ResearchApp snapshot={snapshot} />);
  await user.click(screen.getByRole('button', { name: 'Activity' }));
  expect(screen.getByRole('button', { name: 'Time order' })).toHaveAttribute('aria-pressed', 'true');
  await user.click(screen.getByRole('button', { name: 'By task' }));
  expect(screen.getByRole('button', { name: 'By task' })).toHaveAttribute('aria-pressed', 'true');
  const rows = [...document.querySelectorAll('.activity-list > li')];
  expect(rows.length).toBe(snapshot.attempts.length);
  const row = rows[0] as HTMLElement;
  const attemptId = row.dataset.attemptId!;
  await user.click(within(row).getByRole('button', { name: 'Expand activity details' }));
  await user.click(screen.getByRole('button', { name: 'Time order' }));
  const preserved = document.querySelector(`[data-attempt-id="${attemptId}"]`)! as HTMLElement;
  expect(within(preserved).getByRole('button', { name: 'Hide activity details' })).toHaveAttribute('aria-expanded', 'true');
  expect(screen.getByRole('button', { name: 'Agent work' })).toHaveAttribute('aria-pressed', 'true');
  expect(document.querySelectorAll('.activity-list > li')).toHaveLength(rows.length);
  await user.click(within(preserved).getByText('Technical records and current task state'));
  await user.click(within(preserved).getByRole('button', { name: 'Open selected attempt in Details →' }));
  const selected = document.getElementById('selected-attempt')!;
  await user.click(within(selected).getByText('Exact identity, timing, model and skills'));
  expect(within(selected).getByText(new RegExp(`Attempt ${attemptId} · task`))).toBeVisible();
});
