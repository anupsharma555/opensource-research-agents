/* Selected-step inspection is explicit, local to its identity, and never repeats the overview. */
import { act, cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { SelectedWorkStep } from './SelectedWorkStep';
import { getLiveCalls, getLiveReasoning } from './live';
import type { ResearchSnapshot } from './generated/contracts';
import sample from '../public/sample-run.json';

vi.mock('./live', () => ({ getLiveCalls: vi.fn(), getLiveReasoning: vi.fn() }));
const snapshot = structuredClone(sample) as ResearchSnapshot;
const attempt = snapshot.attempts[0];
const props = { snapshot, taskId: attempt.taskId, attemptId: attempt.id, onOpenTask: vi.fn(), onOpenAttempt: vi.fn() };
afterEach(() => { cleanup(); vi.resetAllMocks(); });

it('shows the summary once and reads exact live calls only after Inspect this step opens', async () => {
  vi.mocked(getLiveCalls).mockResolvedValue({ records: [] });
  const user = userEvent.setup();
  render(<SelectedWorkStep {...props} runId="run-one" />);
  expect(getLiveCalls).not.toHaveBeenCalled();
  expect(getLiveReasoning).not.toHaveBeenCalled();
  expect(screen.getAllByText(attempt.resultSummary!)).toHaveLength(1);
  await user.click(screen.getByText('Inspect this step'));
  expect(getLiveCalls).toHaveBeenCalledExactlyOnceWith('run-one', attempt.id);
  expect(screen.getByRole('region', { name: 'Actual model call records' })).toBeVisible();
  expect(screen.getAllByText(attempt.resultSummary!)).toHaveLength(1);
  expect(screen.getByText('Agent settings and recovery').closest('details')).not.toHaveAttribute('open');
  expect(getLiveReasoning).not.toHaveBeenCalled();
  await user.click(screen.getByText('Technical records and current task state'));
  await user.click(screen.getByRole('button', { name: 'Open selected attempt in Details →' }));
  expect(props.onOpenAttempt).toHaveBeenCalledWith(attempt.id);
  await user.click(screen.getByRole('button', { name: 'Open task in Details →' }));
  expect(props.onOpenTask).toHaveBeenCalledWith(attempt.taskId);
});

it('opens exact records directly when its parent step has already been expanded', async () => {
  vi.mocked(getLiveCalls).mockResolvedValue({ records: [] });
  render(<SelectedWorkStep {...props} runId="run-one" inspectImmediately />);
  expect(await screen.findByRole('region', { name: 'Actual model call records' })).toBeVisible();
  expect(screen.queryByText('Inspect this step')).not.toBeInTheDocument();
  expect(getLiveCalls).toHaveBeenCalledExactlyOnceWith('run-one', attempt.id);
  expect(getLiveReasoning).not.toHaveBeenCalled();
  expect(screen.queryByText(snapshot.tasks[0].title)).not.toBeInTheDocument();
});

it('closes inspection on run change and ignores a late previous-run response', async () => {
  let resolveOld!: (value: { records: unknown[] }) => void;
  vi.mocked(getLiveCalls).mockImplementationOnce(() => new Promise(resolve => { resolveOld = resolve; })).mockResolvedValueOnce({ records: [] });
  const user = userEvent.setup();
  const view = render(<SelectedWorkStep {...props} runId="old-run" />);
  await user.click(screen.getByText('Inspect this step'));
  view.rerender(<SelectedWorkStep {...props} runId="new-run" />);
  expect(screen.getByText('Inspect this step').closest('details')).not.toHaveAttribute('open');
  expect(getLiveCalls).toHaveBeenCalledTimes(1);
  await act(async () => { resolveOld({ records: [{ response: { content: 'Stale previous run' } }] }); });
  expect(screen.queryByText('Stale previous run')).not.toBeInTheDocument();
  await user.click(screen.getByText('Inspect this step'));
  expect(getLiveCalls).toHaveBeenLastCalledWith('new-run', attempt.id);
});

it('exposes recorded call/input/output groups directly and rejects an unrelated selected attempt', async () => {
  const user = userEvent.setup();
  const view = render(<SelectedWorkStep {...props} />);
  await user.click(screen.getByText('Inspect this step'));
  expect(screen.getByRole('region', { name: 'Recorded calls' })).toBeVisible();
  expect(screen.getByRole('region', { name: 'Saved inputs' })).toBeVisible();
  expect(getLiveCalls).not.toHaveBeenCalled();
  const otherTask = snapshot.tasks.find(task => task.id !== attempt.taskId)!;
  view.rerender(<SelectedWorkStep {...props} taskId={otherTask.id} runId="run-one" />);
  expect(screen.queryByText('Inspect this step')).not.toBeInTheDocument();
  expect(screen.getByText(/No agent attempt was recorded/)).toBeVisible();
  expect(getLiveCalls).not.toHaveBeenCalled();
});
