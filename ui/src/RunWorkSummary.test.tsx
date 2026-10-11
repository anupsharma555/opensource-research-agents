/* Preserve separate assignment records without presenting them as separate runs. */
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import sample from '../public/sample-run.json';
import type { ResearchSnapshot } from './generated/contracts';
import { RunWorkSummary } from './RunWorkSummary';

afterEach(cleanup);

it('groups peer work under the shared request while keeping exact assignments accessible', async () => {
  const snapshot = structuredClone(sample) as ResearchSnapshot;
  snapshot.tasks.forEach(task => { task.title = snapshot.mission.question; });
  const user = userEvent.setup();
  render(<RunWorkSummary snapshot={snapshot} earlierAttemptIds={new Set()} taskLabel={id => `Assignment ${id}`} onOpenGroup={vi.fn()} onActivity={vi.fn()} />);
  await user.click(screen.getByText('Agents’ current status and assignments'));
  const first = screen.getAllByRole('article')[0];
  expect(within(first).getByText('Working on the shared request above.')).toBeVisible();
  expect(within(first).getByText(/This assignment record belongs/)).not.toBeVisible();
  await user.click(within(first).getByText('Saved assignment reference'));
  expect(within(first).getByText(/This assignment record belongs/)).toBeVisible();
});

it('identifies the latest-started step honestly when an earlier step finished later', async () => {
  const user = userEvent.setup();
  const snapshot = structuredClone(sample) as ResearchSnapshot;
  const older = { ...snapshot.attempts[0], startedAt: '2026-10-10T10:00:00Z', endedAt: '2026-10-10T12:00:00Z' };
  const later = { ...older, id: 'later-step', startedAt: '2026-10-10T11:00:00Z', endedAt: '2026-10-10T11:05:00Z' };
  snapshot.attempts = [older, later];
  render(<RunWorkSummary snapshot={snapshot} earlierAttemptIds={new Set()} taskLabel={id => id} onOpenGroup={vi.fn()} onActivity={vi.fn()} />);
  await user.click(screen.getByText('Agents’ current status and assignments'));
  const card = screen.getByRole('article');
  expect(within(card).getByText('Most recently started step')).toBeVisible();
  expect(card.querySelector('time')).toHaveAttribute('datetime', later.startedAt);
  expect(screen.getByRole('button', { name: 'Follow activity in time order →' })).toBeVisible();
});
