/* Keep empty, partial and disconnected run summaries truthful across tabs. */
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import sample from '../public/sample-run.json';
import type { ResearchSnapshot } from './generated/contracts';
import { RunStatusSummary } from './RunStatusSummary';
import { workStepNumbers } from './attemptLineage';
import { formatActivityTimestamp } from './ActivityRecordDetails';

afterEach(cleanup);
const snapshot = () => {
  const data = structuredClone(sample) as ResearchSnapshot;
  data.messages = [];
  data.inspection = { snapshotReference: { kind: 'snapshot', id: data.snapshotId, version: 1 },
    acceptedOutputReferences: [], records: [], attempts: [], sources: [], initialSourceIds: [], timingBasis: 'controlled_clock' };
  return data;
};

it('does not turn assignments or old artifacts into messages or an accepted report', async () => {
  const data = snapshot();
  data.tasks[0].title = 'Ask another peer to review this answer';
  const open = vi.fn();
  render(<RunStatusSummary snapshot={data} waitingReason="Backup credential paused by operator" onCommunication={open} />);
  expect(screen.getByText('Model calls paused')).toBeVisible();
  expect(screen.getByText('No accepted report yet')).toBeVisible();
  await userEvent.click(screen.getByRole('button', { name: /No peer messages recorded/ }));
  expect(open).toHaveBeenCalledOnce();
});

it('keeps unavailable acceptance and disconnected host state distinct from zero or a pause', () => {
  const data = snapshot();
  data.inspection = undefined;
  render(<RunStatusSummary snapshot={data} waitingReason="" disconnected onCommunication={vi.fn()} />);
  expect(screen.getByText('Saved view · connection unavailable')).toBeVisible();
  expect(screen.getByText(/Report acceptance not fully available/)).toBeVisible();
  expect(screen.queryByText('Model calls paused')).not.toBeInTheDocument();
  expect(screen.queryByText('No accepted report yet')).not.toBeInTheDocument();
});

it('recognizes saved mail outside the projected messages list and unresolved accepted references', () => {
  const data = snapshot();
  data.inspection!.records.push({ reference: { kind: 'mail_message', id: 'message-a', version: 1 }, label: 'Saved mail', data: {} });
  data.inspection!.acceptedOutputReferences.push({ kind: 'report', id: 'missing-report', version: 1 });
  render(<RunStatusSummary snapshot={data} onCommunication={vi.fn()} />);
  expect(screen.getByRole('button', { name: /Open recorded peer communication/ })).toBeVisible();
  expect(screen.getByText(/Report acceptance not fully available/)).toBeVisible();
});

it('keeps current and earlier step numbers stable across tab filters and toggles', () => {
  const data = snapshot();
  const template = data.attempts[0];
  data.attempts = [
    { ...template, id: 'later', startedAt: '2026-10-10T12:02:00Z' },
    { ...template, id: 'earlier', startedAt: '2026-10-09T12:00:00Z' },
    { ...template, id: 'first', startedAt: '2026-10-10T12:01:00Z' },
  ];
  expect([...workStepNumbers(data, new Set(['earlier']))]).toEqual([['earlier', 1], ['first', 1], ['later', 2]]);
});

it('uses unfiltered saved creation order for run numbers and labels missing creation time honestly', () => {
  const data = snapshot();
  const earlier = { id: 'earlier', question: 'Earlier question', status: 'completed', createdAt: '2026-10-01T10:00:00Z' };
  const current = { id: 'current', question: 'Current question', status: 'waiting', createdAt: '2026-10-02T10:00:00Z' };
  const props = { snapshot: data, run: current, runId: current.id, onCommunication: vi.fn() };
  const view = render(<RunStatusSummary {...props} runs={[current, earlier]} />);
  expect(screen.getByText('Run 2 · Selected run')).toBeVisible();
  expect(screen.getByText('Run ID')).toBeVisible();
  expect(screen.getByText('current')).toBeVisible();
  expect(screen.getByText(formatActivityTimestamp(current.createdAt))).toBeVisible();
  expect(screen.getByText('current').closest('details')).toBeNull();
  view.rerender(<RunStatusSummary {...props} runs={[earlier, current]} />);
  expect(screen.getByText('Run 2 · Selected run')).toBeVisible();
  view.rerender(<RunStatusSummary {...props} run={{ ...current, createdAt: null }} runs={[earlier, { ...current, createdAt: null }]} />);
  expect(screen.queryByText('Run 2 · Selected run')).not.toBeInTheDocument();
  expect(screen.getByText(/Snapshot saved/)).toBeVisible();
  view.rerender(<RunStatusSummary {...props} run={{ ...current, createdAt: 'invalid-date' }} runs={[{ ...current, createdAt: 'invalid-date' }]} />);
  expect(screen.getByText(/Snapshot saved/)).toBeVisible();
  expect(screen.queryByText(/Invalid Date/)).not.toBeInTheDocument();
  expect(screen.queryByText(/Run elapsed/)).not.toBeInTheDocument();
});

it('stops elapsed run time at the saved view, includes pauses, and uses completion only when established', () => {
  const data = snapshot();
  data.capturedAt = '2026-10-08T20:00:00Z';
  const run = { id: 'test-run', question: 'Test question', status: 'waiting', createdAt: '2026-10-08T14:00:00Z' };
  const props = { snapshot: data, run, runId: run.id, onCommunication: vi.fn() };
  const view = render(<RunStatusSummary {...props} />);
  expect(screen.getByText('6 hr 0 min 0 sec').parentElement).toHaveTextContent('Run elapsed to saved view: 6 hr 0 min 0 sec · includes pauses');
  view.rerender(<RunStatusSummary {...props} run={{ ...run, status: 'completed', completedAt: '2026-10-08T14:30:00Z' }} />);
  expect(screen.getByText('30 min 0 sec').parentElement).toHaveTextContent('Run duration: 30 min 0 sec');
  view.rerender(<RunStatusSummary {...props} run={{ ...run, status: 'completed', completedAt: '2026-10-09T14:30:00Z' }} />);
  expect(screen.getByText('6 hr 0 min 0 sec').parentElement).toHaveTextContent('Run elapsed to saved view');
  view.rerender(<RunStatusSummary {...props} run={{ ...run, createdAt: '2026-10-09T14:30:00Z' }} />);
  expect(screen.queryByText(/Run elapsed to saved view|Run duration/)).not.toBeInTheDocument();
});
