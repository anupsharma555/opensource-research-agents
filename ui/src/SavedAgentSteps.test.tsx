import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';
import type { ResearchSnapshot, SavedRecordRef } from './generated/contracts';
import { SavedAgentSteps } from './SavedAgentSteps';
import sample from '../public/sample-run.json';

const ref = (kind: string, id: string, version: number): SavedRecordRef => ({ kind, id, version });

function snapshotWithSteps(): ResearchSnapshot {
  const snapshot = structuredClone(sample) as ResearchSnapshot;
  const olderCopy = ref('source_copy', 'saved-source', 2);
  const newerCopy = ref('source_copy', 'saved-source', 3);
  const peerReply = ref('peer_reply', 'checked-reply', 1);
  snapshot.proofKind = 'offline_test';
  snapshot.inspection = {
    snapshotReference: ref('snapshot', snapshot.snapshotId, 1),
    initialSourceIds: [],
    sources: [],
    attempts: [],
    records: [
      { reference: olderCopy, label: 'Saved copy v2', data: { text: 'Exact older passage.' } },
      { reference: newerCopy, label: 'Saved copy v3', data: { text: 'Newer passage must not appear.' } },
      { reference: peerReply, label: 'Saved peer reply', data: { text: 'Reviewer checked the denominator.' } },
    ],
    acceptedOutputReferences: [],
    timingBasis: 'controlled_clock',
    steps: [
      {
        id: 'step-prepared', sequence: 1, recordedAt: '2026-01-01T00:00:00Z',
        actorName: 'Sage', action: 'Prepared evidence packet',
        summary: 'Selected the saved source copy for review.', outcome: 'prepared',
        inputReferences: [olderCopy], outputReferences: [],
        reasonSummary: null, nextAction: 'Ask Mira to check the denominator.',
        proof: 'Offline fixture record',
      },
      {
        id: 'step-reply', sequence: 2, recordedAt: '2026-01-01T00:01:00Z',
        actorName: 'Mira', action: 'Delivered arithmetic check',
        summary: 'Checked the two denominators.', outcome: 'reply delivered',
        inputReferences: [olderCopy], outputReferences: [peerReply],
        reasonSummary: 'Compared five included records with both saved totals.',
        nextAction: null, proof: 'Offline fixture record',
      },
    ],
  };
  return snapshot;
}

afterEach(() => cleanup());

describe('saved agent steps', () => {
  it('shows two recorded actions, missing explanation, and only exact referenced versions', async () => {
    const user = userEvent.setup();
    render(<SavedAgentSteps snapshot={snapshotWithSteps()} />);
    await user.click(screen.getByText('Saved agent steps (2)'));
    expect(screen.getByText(/Recorded offline test; times and actions are fixture evidence/)).toBeVisible();
    const firstStep = screen.getByText(/Step 1 · Sage · Prepared evidence packet · prepared/).closest('details')!;
    await user.click(within(firstStep).getByText(/Step 1 · Sage · Prepared evidence packet · prepared/));
    expect(screen.getByText('No saved explanation was recorded for this step.')).toBeVisible();
    expect(screen.getByText('Ask Mira to check the denominator.')).toBeVisible();
    expect(within(firstStep).getByText(/controlled test time/)).toBeVisible();
    const inputs = within(firstStep).getByRole('region', { name: 'Exact saved inputs' });
    await user.click(within(inputs).getByText('Saved copy v2'));
    expect(within(inputs).getByText('Exact older passage.')).toBeVisible();
    expect(screen.queryByText('Newer passage must not appear.')).not.toBeInTheDocument();
    const secondStep = screen.getByText(/Step 2 · Mira · Delivered arithmetic check · reply delivered/).closest('details')!;
    await user.click(within(secondStep).getByText(/Step 2 · Mira · Delivered arithmetic check · reply delivered/));
    expect(screen.getByText('Compared five included records with both saved totals.')).toBeVisible();
    expect(screen.getByText('No next action was recorded.')).toBeVisible();
    const outputs = within(secondStep).getByRole('region', { name: 'Exact saved outputs' });
    await user.click(within(outputs).getByText('Saved peer reply'));
    expect(within(outputs).getByText('Reviewer checked the denominator.')).toBeVisible();
  });

  it('keeps absent and empty step data explicit and labels live proof separately', async () => {
    const user = userEvent.setup();
    const absent = structuredClone(sample) as ResearchSnapshot;
    const view = render(<SavedAgentSteps snapshot={absent} />);
    await user.click(screen.getByText('Saved agent steps'));
    expect(screen.getByText('Step inspection was not included in this snapshot.')).toBeVisible();

    const empty = snapshotWithSteps();
    empty.proofKind = 'live_model_on_supplied_evidence';
    empty.inspection!.steps = [];
    view.rerender(<SavedAgentSteps snapshot={empty} />);
    expect(screen.getByText('No saved agent steps were included in this snapshot.')).toBeVisible();
    expect(screen.getByText(/Live model work on supplied evidence/)).toBeVisible();
  });
});
