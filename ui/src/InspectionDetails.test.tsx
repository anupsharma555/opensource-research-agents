/* Offline UI boundary checks: acceptance and exact parent evidence come from
   saved references, never from the latest attempt or current source contents. */
import { render, screen, within, cleanup, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ResearchApp } from './App';
import { AttemptInspection, SavedRecord } from './InspectionDetails';
import type { ResearchSnapshot, SavedRecordRef } from './generated/contracts';
import sample from '../public/sample-run.json';

const reference = (kind: string, id: string, version = 1): SavedRecordRef => ({ kind, id, version });
function recordedSnapshot(): ResearchSnapshot {
  const data = structuredClone(sample) as ResearchSnapshot;
  const report = data.artifacts.find(item => item.id === data.mission.reportArtifactId)!;
  const attempt = data.attempts.find(item => item.taskId === 'task-a2' && item.status === 'completed')!;
  const instance = reference('agent_instance', 'named-instance');
  const configuration = reference('agent_configuration', 'saved-configuration', 2);
  const encounter = reference('retrieval_encounter', 'fixture-encounter');
  const output = reference('report', report.id, report.version);
  const retainedCopy = data.sources.find(source => source.id === 'A2')!;
  data.proofKind = 'offline_test';
  data.snapshotId = 'recorded-ui-test-child';
  data.parentSnapshotId = 'recorded-ui-test-parent';
  data.inspection = {
    snapshotReference: reference('snapshot', data.snapshotId),
    timingBasis: 'controlled_clock',
    initialSourceIds: ['A1'],
    sources: [{ sourceId: 'A2', copyReference: reference('source_copy', retainedCopy.copyId, retainedCopy.version), encounterReferences: [encounter], origin: 'reused', fromSnapshotId: data.parentSnapshotId }],
    attempts: [{ attemptId: attempt.id, instanceReference: instance, configurationReference: configuration, callReferences: [], inputReferences: [], acceptedOutputReferences: [output], explanation: 'Earlier route failed; this saved attempt recovered the requested comparison.' }],
    acceptedOutputReferences: [output],
    followupTarget: reference('report', 'parent-report', 1),
    records: [
      { reference: instance, label: 'Named acquisition instance', data: { display_name: 'Juniper', role: 'Evidence acquisition', replaced_instance: null } },
      { reference: configuration, label: 'Configuration saved before this attempt', data: { model: 'fixture-model', provider: 'fixture-provider', skill_versions: ['fixture-skill@2'], qualification: 'provisional' } },
      { reference: encounter, label: 'Original discovery receipt', data: { query: 'Recorded comparison query', provider: 'fixture-provider', outcome: 'partial', remaining_results: null } },
      { reference: output, label: 'Accepted report version', data: { body: report.body, acceptance: 'accepted' } },
    ],
  };
  return data;
}

afterEach(() => { cleanup(); window.localStorage.clear(); });

describe('persisted inspection review', () => {
  it('orders work steps by saved time with run-wide numbers and keeps exact selection on refresh', async () => {
    const user = userEvent.setup();
    const data = recordedSnapshot();
    data.proofKind = 'live_mission_pending';
    const template = data.attempts[0];
    const detail = data.inspection!.attempts[0];
    const mainTask = data.tasks.find(task => task.id === template.taskId)!;
    const otherTask = data.tasks.find(task => task.id !== mainTask.id)!;
    mainTask.title = 'Check the full synthetic assignment before interpreting the result.';
    const makeAttempt = (id: string, taskId: string, startedAt: string) => ({
      ...template, id, taskId, startedAt, endedAt: startedAt,
      status: 'completed' as const, resultSummary: `Saved step ${id}.`,
      toolCalls: [{ name: 'search_scholarly', status: 'metadata_only', provider: null }],
    });
    data.attempts = [
      makeAttempt('z-last', mainTask.id, '2026-10-08T14:03:00Z'),
      makeAttempt('b-tie', mainTask.id, '2026-10-08T14:02:00Z'),
      makeAttempt('m-first', mainTask.id, '2026-10-08T14:01:00Z'),
      makeAttempt('b-other', otherTask.id, '2026-10-08T14:00:00Z'),
      makeAttempt('a-tie', mainTask.id, '2026-10-08T14:02:00Z'),
    ];
    data.inspection!.attempts = data.attempts.map(attempt => ({
      ...detail, attemptId: attempt.id,
      callReferences: [reference('model_call', `call-${attempt.id}`)],
      acceptedOutputReferences: [],
    }));
    data.inspection!.records.push(...data.attempts.map(attempt => ({
      reference: reference('model_response_receipt', `response-${attempt.id}`),
      label: 'Saved synthetic response',
      data: { call_ref: reference('model_call', `call-${attempt.id}`), attempt_id: attempt.id, status: 'tool_requested' },
    })));
    const live = { runId: 'synthetic-attempt-history', model: 'saved model', runs: [], error: null,
      form: null, requestedAgentIds: [], onSelectRun: vi.fn() };
    const view = render(<ResearchApp snapshot={data} live={live} />);
    await user.click(screen.getByRole('button', { name: 'Details' }));
    await user.click(screen.getByRole('button', { name: 'Work step records' }));
    const list = screen.getByRole('heading', { name: /Inspect a work step/ }).closest('section')!;
    const buttons = [...list.querySelectorAll<HTMLButtonElement>('.attempt-item')];
    expect(buttons).toHaveLength(5);
    expect(buttons.map(button => button.querySelector('time')?.getAttribute('datetime'))).toEqual([
      '2026-10-08T14:00:00Z', '2026-10-08T14:01:00Z',
      '2026-10-08T14:02:00Z', '2026-10-08T14:02:00Z', '2026-10-08T14:03:00Z',
    ]);
    expect(buttons.map(button => button.querySelector('strong')?.textContent)).toEqual([
      expect.stringContaining('work step 1'), expect.stringContaining('work step 2'),
      expect.stringContaining('work step 3'), expect.stringContaining('work step 4'),
      expect.stringContaining('work step 5'),
    ]);
    expect(buttons.every(button => button.textContent?.includes('Scholarly source search · metadata only'))).toBe(true);
    expect(buttons.every(button => !button.textContent?.includes(mainTask.title))).toBe(true);

    await user.click(buttons[3]);
    const selected = document.getElementById('selected-attempt')!;
    expect(selected.querySelector('.eyebrow')).toHaveTextContent('This run · work step 4');
    await user.click(within(selected).getByText('Exact identity, timing, model and skills'));
    expect(within(selected).getByText(/Attempt b-tie · task/)).toBeVisible();
    await user.click(within(selected).getByText('Task 1 · Task assignment'));
    expect(within(selected).getByText(mainTask.title)).toBeVisible();
    const updated = structuredClone(data);
    updated.snapshotId = 'synthetic-attempt-history-next-view';
    updated.attempts.push(makeAttempt('next-step', mainTask.id, '2026-10-08T14:04:00Z'));
    view.rerender(<ResearchApp snapshot={updated} live={live} />);
    expect(document.getElementById('selected-attempt')?.querySelector('.eyebrow')).toHaveTextContent('This run · work step 4');
    expect(within(document.getElementById('selected-attempt')!).getByText(/Attempt b-tie · task/)).toBeVisible();
    expect(list.querySelectorAll('.attempt-item')).toHaveLength(6);
  });

  it.each(['', null])('lists unopened exact lookups with query %s separately from retained copies', async (query) => {
    const user = userEvent.setup();
    const data = recordedSnapshot();
    const linked = reference('retrieval_hit', 'linked-hit');
    const unopened = reference('retrieval_hit', 'unopened-hit');
    data.inspection!.records.find(record => record.reference.id === 'fixture-encounter')!.data.retrieval_hit = linked;
    data.inspection!.records.push(
      { reference: linked, label: 'Linked search hit', data: { provider: 'synthetic-search', query: 'adult comparison', source: { title: 'Linked lead', original_url: 'https://example.test/linked', access_surface: 'abstract', rights_status: 'permitted', content: 'private raw content' } } },
      { reference: unopened, label: 'Unopened search hit', data: { provider: 'synthetic-search', route: 'pubmed.pmid', query, source: { title: 'Unopened lead', original_url: 'ftp://example.test/unopened', access_surface: 'metadata', rights_status: 'unknown', content: 'private raw content' } } },
    );
    render(<ResearchApp snapshot={data} />);
    await user.click(screen.getByRole('button', { name: 'Evidence' }));
    const discoveryDisclosure = screen.getByText('Search discoveries (2 saved hits) · optional leads').closest('details')!;
    await user.click(within(discoveryDisclosure).getByText('Search discoveries (2 saved hits) · optional leads'));
    expect(discoveryDisclosure).toHaveAttribute('open');
    const discoveries = screen.getByRole('region', { name: 'Search discoveries' });
    expect(within(discoveries).getByText('Unopened lead')).toBeVisible();
    expect(within(discoveries).getByText('ftp://example.test/unopened')).toBeVisible();
    expect(within(discoveries).getByRole('link', { name: 'https://example.test/linked' })).toHaveAttribute('href', 'https://example.test/linked');
    expect(within(discoveries).queryByRole('link', { name: 'ftp://example.test/unopened' })).not.toBeInTheDocument();
    expect(within(discoveries).getByText(/Exact identifier lookup \(pubmed.pmid\); no text query supplied/)).toBeVisible();
    expect(within(discoveries).getByText(/adult comparison/)).toBeVisible();
    expect(within(discoveries).getByText(/no source copy linked in this snapshot/)).toBeVisible();
    expect(within(discoveries).queryByText('private raw content')).not.toBeInTheDocument();
    const scroll = vi.fn();
    document.getElementById('selected-source-heading')!.scrollIntoView = scroll;
    await user.click(within(discoveries).getByRole('button', { name: 'Open exact saved source copy →' }));
    const copy = data.sources.find(source => source.id === 'A2')!;
    const heading = screen.getByRole('heading', { name: copy.title });
    await waitFor(() => expect(heading).toHaveFocus());
    expect(scroll).toHaveBeenCalledWith({ block: 'start' });
    const selectedSource = heading.closest<HTMLElement>('.source-detail-panel')!;
    await user.click(within(selectedSource).getByText('Source origin, copy and discovery records'));
    expect(within(selectedSource).getByText(`Source ID: ${copy.id} · copy ID: ${copy.copyId} · version ${copy.version}`)).toBeVisible();
  });

  it('does not offer a discovery shortcut when its saved copy version does not resolve', async () => {
    const data = recordedSnapshot();
    const hit = reference('retrieval_hit', 'version-mismatch');
    data.inspection!.records.find(record => record.reference.id === 'fixture-encounter')!.data.retrieval_hit = hit;
    data.inspection!.records.push({ reference: hit, label: 'Hit', data: { provider: 'synthetic-search', route: 'pubmed.pmid', source: { title: 'Unresolved copy', original_url: 'https://example.test/hit' } } });
    data.inspection!.sources[0].copyReference.version += 1;
    const user = userEvent.setup();
    render(<ResearchApp snapshot={data} />);
    await user.click(screen.getByRole('button', { name: 'Evidence' }));
    await user.click(screen.getByText('Search discoveries (1 saved hits) · optional leads'));
    const discoveries = screen.getByRole('region', { name: 'Search discoveries' });
    expect(within(discoveries).getByText(/no source copy linked in this snapshot/)).toBeVisible();
    expect(within(discoveries).queryByRole('button', { name: 'Open exact saved source copy →' })).not.toBeInTheDocument();
    expect(within(discoveries).getByText('Search or lookup:').closest('p')).toHaveTextContent('Not recorded');
  });

  it('shows an actual saved wait and leaves an unestimated duration unknown', async () => {
    const user = userEvent.setup();
    const data = recordedSnapshot();
    const task = data.tasks.find(item => item.id === data.attempts[0].taskId)!;
    task.status = 'waiting';
    task.activeOrWaiting = 'waiting';
    task.latencyClass = null;
    task.latencyBasis = 'No saved duration estimate';
    task.waitingReason = 'Provider quota wait';
    render(<ResearchApp snapshot={data} />);
    await user.click(screen.getByRole('button', { name: 'Details' }));
    const selected = document.getElementById('selected-task')!;
    expect(within(selected).getByText('Provider quota wait')).toBeVisible();
    await user.click(screen.getByRole('button', { name: 'Research review' }));
    await user.click(screen.getByRole('button', { name: 'Activity' }));
    await user.click(screen.getByText('Browse all saved work steps'));
    expect(screen.getByText('Browse all saved work steps').closest('details')).toHaveAttribute('open');
    const timeline = screen.getByRole('group', { name: 'Recorded agent attempts' });
    const savedAttempt = data.attempts.find(item => item.taskId === task.id)!;
    const row = [...timeline.querySelectorAll<HTMLElement>('.agent-step')].find(item => item.querySelector('.agent-step-times time')?.getAttribute('datetime') === savedAttempt.startedAt)!;
    await user.click(within(row).getByRole('button', { name: /^Open work step/ }));
    await user.click(within(row).getByText('Technical records and current task state'));
    expect(within(row).getByText(/Not yet estimated · No saved duration estimate/)).toBeVisible();
  });

  it('labels a selected checked source window as an excerpt of the exact copy', async () => {
    const user = userEvent.setup();
    const data = recordedSnapshot();
    data.sources[0].passages.push({ spanId: 'window-checked', locator: 'section=methods; chars=4:12', text: 'Selected method text.' });
    render(<ResearchApp snapshot={data} />);
    await user.click(screen.getByRole('button', { name: 'Evidence' }));
    const source = screen.getByRole('heading', { name: data.sources[0].title }).closest('section')!;
    expect(within(source).getByText(/These excerpts may not include the whole publication/)).toBeVisible();
    const choice = within(source).getByRole('button', { name: /section=methods; chars=4:12/ });
    await user.click(choice);
    const checkedPassage = within(source).getByRole('article', { name: /exact saved text/ });
    expect(within(checkedPassage).getByText('Selected method text.')).toBeVisible();
    await user.click(within(checkedPassage).getByText('Exact passage reference'));
    expect(within(checkedPassage).getByText(/passage window-checked/)).toBeVisible();
  });

  it('resolves a single accepted report record even when its scientific artifact kind differs', () => {
    const data = recordedSnapshot();
    const report = data.artifacts.find(item => item.id === data.mission.reportArtifactId)!;
    report.kind = 'research_contribution';
    render(<ResearchApp snapshot={data} />);
    const output = screen.getByRole('region', { name: 'Current saved output' });
    expect(within(output).getByText(/Together they do not establish benefit/)).toBeVisible();
    expect(within(output).queryByText(/No report has been saved/)).not.toBeInTheDocument();
  });

  it('shows every accepted independent report without choosing a first answer', async () => {
    const data = recordedSnapshot();
    const first = data.artifacts.find(item => item.id === data.mission.reportArtifactId)!;
    const second = { ...first, id: 'independent-report', title: 'Independent review', body: 'A separate interpretation with its own limits.' };
    data.artifacts.push(second);
    data.inspection!.acceptedOutputReferences.push(reference('report', second.id, second.version));
    data.mission.reportArtifactId = null;
    render(<ResearchApp snapshot={data} comparisonSnapshot={recordedSnapshot()} />);
    const output = screen.getByRole('region', { name: 'Current saved output' });
    expect(within(output).getByText(/Several independent reports were accepted/)).toBeVisible();
    expect(within(output).getByText(/A separate interpretation with its own limits/)).toBeVisible();
    expect(within(output).getByText(/Together they do not establish benefit/)).toBeVisible();
    await userEvent.click(screen.getByRole('button', { name: 'Compare' }));
    const followup = screen.getByRole('region', { name: 'This follow-up' });
    expect(within(followup).getByText(/2 answers are saved separately/)).toBeVisible();
    await userEvent.click(within(followup).getByRole('button', { name: 'Read answer' }));
    expect(within(followup).getAllByText(/Saved as a run output/)).toHaveLength(2);
    expect(within(followup).getByText(/A separate interpretation with its own limits/)).toBeVisible();
    expect(within(followup).getByText(/Together they do not establish benefit/)).toBeVisible();
  });

  it('distinguishes an explicitly empty object from a missing saved value', async () => {
    const user = userEvent.setup();
    const data = recordedSnapshot();
    const config = data.inspection!.records.find(item => item.reference.kind === 'agent_configuration')!;
    config.data = { skills: {}, optional_note: null };
    render(<SavedRecord snapshot={data} reference={config.reference} />);
    await user.click(screen.getByText(config.label));
    await user.click(screen.getByText('View skills'));
    expect(screen.getByText('No fields recorded')).toBeVisible();
    expect(screen.getByText('Not recorded')).toBeVisible();
  });

  it('shows exact saved configuration, source reuse and explicit input provenance', async () => {
    const user = userEvent.setup();
    const data = recordedSnapshot();
    render(<ResearchApp snapshot={data} />);
    expect(screen.getByText('RECORDED OFFLINE TEST')).toBeVisible();
    expect(screen.queryByText('SCRIPTED SAMPLE')).not.toBeInTheDocument();
    expect(screen.getByRole('button', { name: 'Start or follow up' })).toBeVisible();
    const input = screen.getByRole('region', { name: 'Research task input' });
    await user.click(within(input).getByText('View starting sources (1)'));
    expect(within(input).getByText(/Adult study A/)).toBeVisible();
    expect(within(input).queryByText(/Adult study B/)).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Activity' }));
    await user.click(screen.getByText('Browse all saved work steps'));
    expect(screen.getByText('Browse all saved work steps').closest('details')).toHaveAttribute('open');
    expect(screen.getByText(/Controlled test clock, not measured provider latency/)).toBeVisible();
    const ordered = [...data.attempts].sort((a, b) => a.startedAt.localeCompare(b.startedAt) || a.id.localeCompare(b.id));
    const ordinal = ordered.findIndex(item => item.id === data.inspection!.attempts[0].attemptId) + 1;
    const timeline = screen.getByRole('group', { name: 'Recorded agent attempts' });
    const contribution = within(timeline).getByRole('button', { name: new RegExp(`^Open work step ${ordinal}:`) }).closest('.agent-step')!;
    await user.click(within(contribution as HTMLElement).getByRole('button', { name: new RegExp(`^Open work step ${ordinal}:`) }));
    await user.click(within(contribution as HTMLElement).getByText('Technical records and current task state'));
    await user.click(within(contribution as HTMLElement).getByText('Agent settings and recovery'));
    const disclosure = within(contribution as HTMLElement).getByText('Configuration saved before this attempt').closest('details')!;
    await user.click(within(disclosure).getByText('Configuration saved before this attempt'));
    expect(within(disclosure).getByText('fixture-model')).toBeVisible();
    expect(within(disclosure).getByText('provisional')).toBeVisible();
    await user.click(screen.getByRole('button', { name: 'Evidence' }));
    await user.click(screen.getByRole('button', { name: /Adult study B/ }));
    await user.click(screen.getByText('Source origin, copy and discovery records'));
    expect(screen.getByText('Reused from an earlier run')).toBeVisible();
    await user.click(screen.getByText('Source copy and discovery receipts'));
    await user.click(screen.getByText('Original discovery receipt'));
    expect(screen.getByText('Recorded comparison query')).toBeVisible();
    expect(screen.getByText('partial')).toBeVisible();
    expect(screen.getAllByText('Not recorded').some(item => item.textContent === 'Not recorded')).toBe(true);
  });

  it('does not present an unaccepted report merely because an attempt completed', () => {
    const data = recordedSnapshot();
    data.inspection!.acceptedOutputReferences = [reference('report', data.mission.reportArtifactId!, 999)];
    render(<ResearchApp snapshot={data} />);
    const output = screen.getByRole('region', { name: 'Current saved output' });
    expect(within(output).getByText(/The accepted report reference cannot be resolved/)).toBeVisible();
    expect(within(output).queryByText(/Together they do not establish benefit/)).not.toBeInTheDocument();
    expect(within(output).getByText('Saved record unavailable')).toBeVisible();
  });

  it('opens the parent citation in place using the parent copy, even when source IDs match', async () => {
    const user = userEvent.setup();
    const child = recordedSnapshot();
    const parent = structuredClone(child);
    parent.snapshotId = child.parentSnapshotId!;
    parent.sources.find(item => item.id === 'A1')!.passages.find(item => item.spanId === 'A1-result')!.text = 'Exact retained parent passage before correction.';
    child.sources.find(item => item.id === 'A1')!.passages.find(item => item.spanId === 'A1-result')!.text = 'Changed child passage after correction.';
    render(<ResearchApp snapshot={child} comparisonSnapshot={parent} />);
    await user.click(screen.getByRole('button', { name: 'Compare' }));
    const earlier = screen.getByRole('region', { name: 'Earlier run' });
    const followup = screen.getByRole('region', { name: 'This follow-up' });
    await user.click(within(earlier).getByRole('button', { name: 'Check cited evidence' }));
    await user.click(within(earlier).getByText('Citation 1'));
    expect(within(earlier).getByText('Exact retained parent passage before correction.')).toBeVisible();
    expect(within(earlier).queryByText('Changed child passage after correction.')).not.toBeInTheDocument();
    await user.click(within(followup).getByRole('button', { name: 'Check cited evidence' }));
    await user.click(within(followup).getByText('Citation 1'));
    expect(within(followup).getByText('Changed child passage after correction.')).toBeVisible();
    expect(within(followup).queryByText('Exact retained parent passage before correction.')).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Compare' })).toBeVisible();
    expect(within(earlier).getByRole('region', { name: 'Earlier run cited evidence' })).toBeVisible();
    expect(within(followup).getByRole('region', { name: 'This follow-up cited evidence' })).toBeVisible();
  });

  it('does not resolve missing configuration versions to another saved version', async () => {
    const user = userEvent.setup();
    const data = recordedSnapshot();
    data.inspection!.attempts[0].configurationReference = { ...data.inspection!.attempts[0].configurationReference!, version: 99 };
    render(<AttemptInspection snapshot={data} attemptId={data.inspection!.attempts[0].attemptId} />);
    await user.click(screen.getByText('Saved agent, configuration and recovery details'));
    const config = screen.getByRole('region', { name: 'Configuration used' });
    await user.click(within(config).getByText('Saved record unavailable'));
    expect(within(config).getByText(/This exact record version was not included/)).toBeVisible();
    expect(screen.queryByText('fixture-model')).not.toBeInTheDocument();
  });
});
