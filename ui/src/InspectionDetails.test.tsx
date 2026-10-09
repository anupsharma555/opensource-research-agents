/* Offline UI boundary checks: acceptance and exact parent evidence come from
   saved references, never from the latest attempt or current source contents. */
import { render, screen, within, cleanup } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';
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
  data.proofKind = 'offline_test';
  data.snapshotId = 'recorded-ui-test-child';
  data.parentSnapshotId = 'recorded-ui-test-parent';
  data.inspection = {
    snapshotReference: reference('snapshot', data.snapshotId),
    timingBasis: 'controlled_clock',
    initialSourceIds: ['A1'],
    sources: [{ sourceId: 'A2', copyReference: reference('source_copy', 'saved-copy'), encounterReferences: [encounter], origin: 'reused', fromSnapshotId: data.parentSnapshotId }],
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
    expect(screen.getByText('Controlled test clock · not measured latency')).toBeVisible();
    const input = screen.getByRole('region', { name: 'Research task input' });
    await user.click(within(input).getByText('View supplied source data (1 records)'));
    expect(within(input).getByText(/Adult study A/)).toBeVisible();
    expect(within(input).queryByText(/Adult study B/)).not.toBeInTheDocument();
    await user.click(within(screen.getByRole('group', { name: 'Recorded agent attempts' })).getByRole('button', { name: /Acquisition peer; completed; Find an independent adult comparison/ }));
    const contribution = screen.getByRole('article', { name: /Review of Acquisition peer/ });
    const disclosure = within(contribution).getByText('Configuration saved before this attempt').closest('details')!;
    const attemptDisclosure = disclosure.parentElement!.closest('details')!;
    await user.click(within(attemptDisclosure).getByText('Saved agent, configuration and recovery details'));
    await user.click(within(disclosure).getByText('Configuration saved before this attempt'));
    expect(within(disclosure).getByText('fixture-model')).toBeVisible();
    expect(within(disclosure).getByText('provisional')).toBeVisible();
    await user.click(screen.getByRole('button', { name: 'Evidence' }));
    await user.click(screen.getByRole('button', { name: /Adult study B/ }));
    expect(screen.getByText('Reused saved evidence')).toBeVisible();
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
    expect(within(output).getByText(/No accepted report resolves/)).toBeVisible();
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
    const parentCard = screen.getByText('Immutable parent snapshot').closest('section')!;
    await user.click(within(parentCard).getByText(/A1 · A1-result · expand cited passage/));
    expect(within(parentCard).getByText('Exact retained parent passage before correction.')).toBeVisible();
    expect(within(parentCard).queryByText('Changed child passage after correction.')).not.toBeInTheDocument();
    expect(screen.getByRole('heading', { name: 'Compare' })).toBeVisible();
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
