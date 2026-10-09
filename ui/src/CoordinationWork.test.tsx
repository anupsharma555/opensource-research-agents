import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';
import type { ResearchSnapshot, SavedRecordRef } from './generated/contracts';
import { CoordinationWork } from './CoordinationWork';
import stage3 from '../public/stage3-recorded/snapshot.json';

const ref = (kind: string, id: string, version: number): SavedRecordRef => ({ kind, id, version });
const saved = stage3 as ResearchSnapshot;

afterEach(() => cleanup());

describe('saved coordinator work', () => {
  it('shows exact dependencies and observed interval overlap from the recorded offline fixture', async () => {
    const user = userEvent.setup();
    render(<CoordinationWork snapshot={saved} />);
    const panel = screen.getByRole('group', { name: 'Saved work and waits' });
    await user.click(within(panel).getByText('Saved work and waits (3 executor work, 1 other proposals)'));
    expect(within(panel).getByText(/Saved monotonic callback intervals overlap:/)).toHaveTextContent('a-fast and b-slow');
    expect(within(panel).getByText(/Saved monotonic callback intervals overlap:/)).toHaveTextContent('not provider concurrency');

    const dependent = within(panel).getByText(/c-dependent · completed/).closest('details')!;
    await user.click(within(dependent).getByText(/c-dependent · completed/));
    expect(within(dependent).getByText(/Inspect the synthetic input for c-dependent/)).toBeVisible();
    expect(within(dependent).getByText('board_proposal · a-fast · v1 · exact version included')).toBeVisible();
    expect(within(dependent).getAllByText('completed').length).toBeGreaterThan(0);
    expect(within(dependent).getByText('Not measured by the executor.')).toBeVisible();
    const dependencies = within(dependent).getByRole('region', { name: 'Exact dependencies' });
    await user.click(within(dependencies).getByText('Board proposal'));
    expect(within(dependencies).getByText('Inspect the synthetic input for a-fast')).toBeVisible();

    const other = within(panel).getByRole('region', { name: 'Proposals without saved executor work' });
    await user.click(within(other).getByText(/Resolve the remaining synthetic evidence gap · deferred/));
    expect(within(other).getByText(/Find a permitted exact source version/)).toBeVisible();
    expect(within(panel).getByText(/Read-only coordinator records/)).toBeVisible();
  });

  it('keeps waiting, uncertain, missing exact dependencies, and absent proof explicit', async () => {
    const user = userEvent.setup();
    const snapshot = structuredClone(saved);
    const actorId = 'agent-synthetic';
    const missingDependency = ref('board_proposal', 'missing-dependency', 3);
    const proposal = (id: string) => ref('board_proposal', id, 1);
    const work = (id: string, version: number, phase: string) => ({
      reference: ref('executor_work', `work-${id}`, version), label: `Work ${id}`, data: {
        phase, spec: { mission_id: snapshot.mission.id, run_id: 'run-synthetic', work_id: id, actor_id: actorId, attempt_id: `attempt-${id}`, proposal_ref: proposal(id) },
      },
    });
    snapshot.attempts = [];
    snapshot.inspection!.records = [
      { reference: ref('agent_instance', actorId, 1), label: 'Synthetic peer', data: { display_name: 'Synthetic peer' } },
      { reference: proposal('waiting'), label: 'Waiting proposal v1', data: { objective: 'Review a saved input', depends_on: [missingDependency], status: 'open' } },
      { reference: ref('board_proposal', 'waiting', 2), label: 'Waiting proposal v2', data: { status: 'open', next_action: 'Wait for the missing input.' } },
      { reference: proposal('uncertain'), label: 'Uncertain proposal', data: { objective: 'Check a callback', depends_on: [], status: 'open' } },
      work('waiting', 1, 'pending'), work('uncertain', 1, 'pending'),
      { reference: ref('coordination_trace', 'trace-uncertain', 1), label: 'Uncertain trace', data: { mission_id: snapshot.mission.id, run_id: 'run-synthetic', task_id: 'uncertain', attempt_id: 'attempt-uncertain', instance_id: actorId, operation_id: 'cap-uncertain' } },
      { reference: ref('capacity_operation', 'cap-waiting', 2), label: 'Waiting capacity', data: { mission_id: snapshot.mission.id, run_id: 'run-synthetic', task_id: 'waiting', attempt_id: 'attempt-waiting', phase: 'waiting', wait_reason: 'quota', diagnostic: 'Quota wait recorded.', wake_at: '2026-01-01T00:02:00Z' } },
      { reference: ref('capacity_operation', 'cap-uncertain', 3), label: 'Uncertain capacity', data: { mission_id: snapshot.mission.id, run_id: 'run-synthetic', task_id: 'uncertain', attempt_id: 'attempt-uncertain', phase: 'uncertain', diagnostic: 'External outcome is unknown.' } },
    ];
    render(<CoordinationWork snapshot={snapshot} />);
    const panel = screen.getByRole('group', { name: 'Saved work and waits' });
    await user.click(within(panel).getByText(/Saved work and waits \(2 executor work/));
    expect(within(panel).getByText(/Overlap unknown: fewer than two/)).toBeVisible();

    const waiting = within(panel).getByText('waiting · pending · Synthetic peer').closest('details')!;
    await user.click(within(waiting).getByText('waiting · pending · Synthetic peer'));
    expect(within(waiting).getByText('quota')).toBeVisible();
    expect(within(waiting).getByText('Quota wait recorded.')).toBeVisible();
    expect(within(waiting).getByText('Wait for the missing input.')).toBeVisible();
    expect(within(waiting).getByText('Unique saved mission, task and attempt IDs.')).toBeVisible();
    expect(within(waiting).getByText('board_proposal · missing-dependency · v3 · exact version unavailable')).toBeVisible();
    const deps = within(waiting).getByRole('region', { name: 'Exact dependencies' });
    await user.click(within(deps).getByText('Saved record unavailable'));
    expect(within(deps).getByText('This exact record version was not included in the saved inspection.')).toBeVisible();

    const uncertain = within(panel).getByText('uncertain · pending · Synthetic peer').closest('details')!;
    await user.click(within(uncertain).getByText('uncertain · pending · Synthetic peer'));
    expect(within(uncertain).getByText('External outcome is unknown.')).toBeVisible();
    expect(within(uncertain).getByText('No current next action was recorded for this work.')).toBeVisible();
  });

  it('does not invent coordination state when inspection is absent', async () => {
    const user = userEvent.setup();
    const snapshot = structuredClone(saved);
    snapshot.inspection = null;
    render(<CoordinationWork snapshot={snapshot} />);
    await user.click(screen.getByText('Saved work and waits'));
    expect(screen.getByText('Coordination inspection was not included in this snapshot.')).toBeVisible();
    expect(screen.queryByText(/Saved callback attempt intervals overlap:/)).not.toBeInTheDocument();
  });

  it('rejects another work item’s receipt and trace even when their IDs are pinned or task-shaped', async () => {
    const user = userEvent.setup();
    const snapshot = structuredClone(saved);
    snapshot.inspection!.records = snapshot.inspection!.records.filter(item => item.reference.kind !== 'stage3_fixture_proof');
    const work = snapshot.inspection!.records.find(item => item.reference.kind === 'executor_work' && item.data.spec && (item.data.spec as { work_id: string }).work_id === 'a-fast' && item.reference.version === 2)!;
    const other = snapshot.inspection!.records.find(item => item.reference.kind === 'executor_work' && item.data.spec && (item.data.spec as { work_id: string }).work_id === 'b-slow' && item.reference.version === 2)!;
    work.data.receipt_ref = other.data.receipt_ref;
    const aSpec = work.data.spec as { actor_id: string; attempt_id: string; run_id: string };
    snapshot.inspection!.records = snapshot.inspection!.records.filter(item => item.reference.kind !== 'coordination_trace' || item.data.task_id !== 'a-fast');
    snapshot.inspection!.records.push({ reference: ref('coordination_trace', 'mislinked-trace', 1), label: 'Wrong run trace', data: { mission_id: snapshot.mission.id, run_id: 'different-run', task_id: 'a-fast', attempt_id: aSpec.attempt_id, instance_id: aSpec.actor_id, operation_id: 'wrong-capacity', reason_summary: 'False linked reason.' } });
    snapshot.inspection!.records.push({ reference: ref('coordination_trace', 'missing-mission-trace', 1), label: 'Missing mission trace', data: { run_id: aSpec.run_id, task_id: 'a-fast', attempt_id: aSpec.attempt_id, instance_id: aSpec.actor_id, reason_summary: 'False reason with missing mission.' } });
    snapshot.inspection!.records.push({ reference: ref('capacity_operation', 'wrong-capacity', 1), label: 'Wrong capacity', data: { mission_id: snapshot.mission.id, run_id: 'different-run', task_id: 'a-fast', attempt_id: aSpec.attempt_id, phase: 'waiting', diagnostic: 'False linked wait.' } });
    const ownCapacity = snapshot.inspection!.records.find(item => item.reference.kind === 'capacity_operation' && item.data.task_id === 'a-fast')!;
    delete ownCapacity.data.run_id;
    const view = render(<CoordinationWork snapshot={snapshot} />);
    await user.click(screen.getByText(/Saved work and waits \(3 executor work/));
    const card = screen.getByText(/a-fast · completed/).closest('details')!;
    await user.click(within(card).getByText(/a-fast · completed/));
    expect(within(card).getByText('Recorded pointer is unavailable or does not match this work and mission.')).toBeVisible();
    expect(within(card).getByText('No verified capacity record included.')).toBeVisible();
    expect(within(card).queryByText('False linked reason.')).not.toBeInTheDocument();
    expect(within(card).queryByText('False reason with missing mission.')).not.toBeInTheDocument();
    expect(within(card).queryByText('False linked wait.')).not.toBeInTheDocument();
    expect(within(card).queryByText('Saved elapsed time')).not.toBeInTheDocument();
    work.data.receipt_ref = other.data.result_ref;
    view.rerender(<CoordinationWork snapshot={snapshot} />);
    expect(within(card).getByText('Recorded pointer is unavailable or does not match this work and mission.')).toBeVisible();
    expect(within(card).getByRole('region', { name: 'Unverified receipt pointer' })).toBeVisible();
  });

  it('does not call overlapping attempts linked when their saved task IDs differ', async () => {
    const user = userEvent.setup();
    const snapshot = structuredClone(saved);
    snapshot.inspection!.records = snapshot.inspection!.records.filter(item => item.reference.kind !== 'stage3_fixture_proof');
    snapshot.attempts.find(item => item.taskId === 'a-fast')!.taskId = 'another-task';
    snapshot.attempts.find(item => item.taskId === 'b-slow')!.taskId = 'another-task';
    render(<CoordinationWork snapshot={snapshot} />);
    await user.click(screen.getByText(/Saved work and waits \(3 executor work/));
    expect(screen.getByText(/Overlap unknown: fewer than two linked completed attempt intervals/)).toBeVisible();
    expect(screen.queryByText(/Saved attempt spans overlap:/)).not.toBeInTheDocument();
  });
});
