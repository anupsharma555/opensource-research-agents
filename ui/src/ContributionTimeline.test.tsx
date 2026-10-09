/* Verify that the visual contribution scale uses whole-minute marks while
   saved attempt positions still reflect their exact timestamps. */
import { render, screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { ContributionTimeline, RecordedWorkTimeline } from './ContributionTimeline';
import type { ResearchSnapshot } from './generated/contracts';
import sample from '../public/sample-run.json';

describe('contribution timeline minute scale', () => {
  it('places whole-minute ticks accurately and calls a short attempt less than one minute', () => {
    const fixture = sample as ResearchSnapshot;
    const task = fixture.tasks[0];
    const attempt = fixture.attempts[0];
    const snapshot: ResearchSnapshot = {
      ...fixture,
      mission: { ...fixture.mission, startedAt: '2026-10-08T14:00:00Z' },
      capturedAt: '2026-10-08T14:02:30Z',
      tasks: [{ ...task, attemptIds: [attempt.id] }],
      attempts: [{ ...attempt, taskId: task.id, startedAt: '2026-10-08T14:00:10Z', endedAt: '2026-10-08T14:00:40Z', status: 'completed' }],
    };
    render(<ContributionTimeline snapshot={snapshot} onSelect={() => {}} />);
    const timeline = screen.getByRole('group', { name: 'Work toward this answer' });
    const axis = timeline.querySelector<HTMLElement>('.contribution-axis')!;
    expect(within(axis).getByText('Start')).toBeInTheDocument();
    expect(within(axis).getByText('+1 min')).toHaveStyle({ left: '40%' });
    expect(within(axis).getByText('+2 min')).toHaveStyle({ left: '80%' });
    expect(axis.textContent).not.toMatch(/\d+\.\d+ min/);
    const row = within(timeline).getByRole('button', { name: /completed: <1 min to <1 min \(<1 min attempt\)/ });
    const bar = row.querySelector<HTMLElement>('.contribution-segment')!;
    expect(parseFloat(bar.style.left)).toBeCloseTo(100 * 10 / 150);
    expect(parseFloat(bar.style.width)).toBeCloseTo(20);
    expect(bar.title).toContain('<1 min attempt');
  });

  it('labels a saved attempt with its agent, assignment, actions and unchanged result summary', () => {
    const fixture = sample as ResearchSnapshot;
    const attempt = fixture.attempts[0];
    const saved = { ...fixture, attempts: [attempt] };
    render(<RecordedWorkTimeline snapshot={saved} includeParentHistory={false} onToggleParentHistory={() => {}} onSelectAttempt={() => {}} onSelectTask={() => {}} />);
    const row = screen.getByRole('button', { name: new RegExp(`Attempt 1; .*; ${attempt.status};`) });
    const agentName = fixture.agents.find(agent => agent.id === attempt.agentId)?.name;
    expect(within(row).getByText(`Agent ${agentName}`)).toBeInTheDocument();
    expect(within(row).getByText(/Attempt 1 · Assignment:/)).toBeInTheDocument();
    expect(within(row).getByText(/Recorded actions:/)).toBeInTheDocument();
    expect(within(row).getByText(new RegExp(`Saved ${attempt.status === 'completed' ? 'response' : 'attempt outcome'} from`))).toBeInTheDocument();
    expect(row.querySelector('.attempt-result-preview')).toHaveTextContent(attempt.resultSummary || 'No result summary saved.');
  });

  it('does not stretch completed recorded work across an unrelated mission or snapshot gap', () => {
    const fixture = sample as ResearchSnapshot;
    const attempt = { ...fixture.attempts[0], status: 'completed' as const, startedAt: '2026-10-08T14:10:10Z', endedAt: '2026-10-08T14:10:40Z' };
    const saved = { ...fixture, capturedAt: '2026-10-08T15:00:00Z', attempts: [attempt], events: [] };
    const { container } = render(<RecordedWorkTimeline snapshot={saved} includeParentHistory={false} onToggleParentHistory={() => {}} onSelectAttempt={() => {}} onSelectTask={() => {}} />);
    const bar = container.querySelector<HTMLElement>('.contribution-segment')!;
    expect(bar).toHaveStyle({ left: '0%', width: '100%' });
    expect(within(container).getByText('<1 min')).toBeInTheDocument();
  });
});
