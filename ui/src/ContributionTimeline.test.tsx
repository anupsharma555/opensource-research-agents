import { fireEvent, render, screen, within } from '@testing-library/react';
import { describe, expect, it, vi } from 'vitest';
import { ContributionTimeline, RecordedWorkTimeline } from './ContributionTimeline';
import type { ResearchSnapshot } from './generated/contracts';
import sample from '../public/sample-run.json';

const fixture = () => structuredClone(sample) as ResearchSnapshot;
const required = {
  includeParentHistory: false,
  onToggleParentHistory: () => {},
  onSelectAttempt: () => {},
  onSelectTask: () => {},
};

function savedReadTimeline() {
  const saved = fixture();
  const template = saved.attempts[0];
  saved.proofKind = 'live_mission_pending';
  saved.messages = [];
  saved.events = [];
  saved.attempts = Array.from({ length: 6 }, (_, index) => ({
    ...template,
    id: 'source-read-' + (index + 1),
    status: 'completed' as const,
    startedAt: '2026-10-08T14:0' + index + ':00Z',
    endedAt: '2026-10-08T14:0' + index + ':20Z',
    toolCalls: [{ name: 'read_source_window', status: 'completed' as const }],
    resultSummary: undefined,
  }));
  saved.inspection = {
    snapshotReference: { kind: 'research_snapshot', id: saved.snapshotId, version: 1 },
    timingBasis: 'observed_local', initialSourceIds: [], sources: [], acceptedOutputReferences: [],
    records: saved.attempts.map((attempt, index) => ({
      reference: { kind: 'model_response_receipt', id: 'response-' + index, version: 1 },
      label: 'Saved response',
      data: { call_ref: { kind: 'agent_call', id: 'call-' + index, version: 1 }, attempt_id: attempt.id, status: 'tool_requested' },
    })),
    attempts: saved.attempts.map((attempt, index) => ({
      attemptId: attempt.id,
      instanceReference: { kind: 'agent_instance', id: attempt.agentId, version: 1 },
      callReferences: [{ kind: 'agent_call', id: 'call-' + index, version: 1 }],
      explanation: 'Exact saved source read.',
    })),
  };
  return saved;
}

describe('contribution timeline', () => {
  it('retains the sample task scale and positions a short saved attempt', () => {
    const saved = fixture();
    const task = saved.tasks[0];
    const attempt = saved.attempts[0];
    saved.mission.startedAt = '2026-10-08T14:00:00Z';
    saved.capturedAt = '2026-10-08T14:02:30Z';
    saved.tasks = [{ ...task, attemptIds: [attempt.id] }];
    saved.attempts = [{ ...attempt, taskId: task.id, startedAt: '2026-10-08T14:00:10Z', endedAt: '2026-10-08T14:00:40Z', status: 'completed' }];
    render(<ContributionTimeline snapshot={saved} onSelect={() => {}} />);
    const timeline = screen.getByRole('group', { name: 'Work toward this answer' });
    expect(within(timeline.querySelector<HTMLElement>('.contribution-axis')!).getByText('+1 min')).toHaveStyle({ left: '40%' });
    const bar = timeline.querySelector<HTMLElement>('.contribution-segment')!;
    expect(parseFloat(bar.style.left)).toBeCloseTo(100 * 10 / 150);
    expect(parseFloat(bar.style.width)).toBeCloseTo(20);
  });
});

describe('recorded work reading flow', () => {
  it('starts in time order with prominent actor, task label, concrete outcome, and start to finish', () => {
    const saved = savedReadTimeline();
    const secondActor = saved.agents.find(agent => agent.id !== saved.attempts[0].agentId)!;
    saved.attempts[1] = { ...saved.attempts[1], agentId: secondActor.id };
    const onSelectAttempt = vi.fn();
    const { container } = render(<RecordedWorkTimeline snapshot={saved} {...required} taskLabel={() => 'Task 3'} onSelectAttempt={onSelectAttempt} />);
    expect(screen.getByRole('button', { name: 'Time order' })).toHaveAttribute('aria-pressed', 'true');
    expect(container.querySelector('.agent-chronology')).toBeInTheDocument();
    expect(container.querySelector('.agent-chronology .agent-step-track')).toBeVisible();
    expect(container.querySelector('.agent-time-axis')).toBeVisible();
    expect(container.querySelectorAll('.agent-step-day')).toHaveLength(1);
    const rows = within(container).getAllByRole('button', { name: /^Open work step/ });
    expect(rows).toHaveLength(6);
    expect(rows[0]).toHaveTextContent('Task 3 · step 1');
    expect(rows[0]).toHaveTextContent('Retrieved a passage from a saved source');
    expect(rows[0]).toHaveTextContent('Passage saved');
    expect(rows[1]).toHaveTextContent(secondActor.name);
    expect(rows[0].closest('.agent-step')).toHaveTextContent(/Started .*Finished .*Elapsed 20s/s);
    expect(rows[0].closest('.agent-step')).not.toHaveTextContent(saved.tasks[0].title);
    fireEvent.click(rows[1]);
    expect(onSelectAttempt).toHaveBeenCalledWith(saved.attempts[1].id, saved.attempts[1].taskId);
  });

  it('offers By agent without changing exact step numbers or collapsing across another actor', () => {
    const saved = savedReadTimeline();
    const secondActor = saved.agents.find(agent => agent.id !== saved.attempts[0].agentId)!;
    saved.attempts[1] = { ...saved.attempts[1], agentId: secondActor.id };
    const { container } = render(<RecordedWorkTimeline snapshot={saved} {...required} selectedAttemptId="source-read-3" />);
    expect(container.querySelectorAll('.agent-step-group')).toHaveLength(1);
    fireEvent.click(within(container).getByRole('button', { name: 'By agent' }));
    expect(within(container).getByRole('button', { name: 'By agent' })).toHaveAttribute('aria-pressed', 'true');
    expect(within(container).getByRole('region', { name: secondActor.name + ' saved steps' })).toBeInTheDocument();
    expect(container.querySelectorAll('.agent-step-group')).toHaveLength(1);
    expect(within(container).getByRole('button', { name: /^Open work step 3:/ })).toHaveAttribute('aria-pressed', 'true');
  });

  it('expands one selected step inline and mounts the exact inspector only after opening', () => {
    const saved = savedReadTimeline();
    saved.attempts = [saved.attempts[0]];
    const onSelectAttempt = vi.fn();
    const renderStepDetails = vi.fn((id: string) => <p>Exact call for {id}</p>);
    const { container } = render(<RecordedWorkTimeline snapshot={saved} {...required} onSelectAttempt={onSelectAttempt} renderStepDetails={renderStepDetails} selectedAttemptId={saved.attempts[0].id} />);
    const disclosure = container.querySelector<HTMLDetailsElement>('.agent-step-details')!;
    expect(disclosure).not.toHaveAttribute('open');
    expect(renderStepDetails).not.toHaveBeenCalled();
    fireEvent.click(within(disclosure).getByText('Inspect this step'));
    expect(onSelectAttempt).toHaveBeenCalledWith(saved.attempts[0].id, saved.attempts[0].taskId);
    expect(disclosure).toHaveAttribute('open');
    expect(within(disclosure).getByText('Exact call for source-read-1')).toBeVisible();
    expect(renderStepDetails).toHaveBeenCalledTimes(1);
    fireEvent.click(within(container).getByRole('button', { name: /^Open work step 1:/ }));
    expect(disclosure).toHaveAttribute('open');
    fireEvent.click(within(disclosure).getByText('Inspect this step'));
    expect(disclosure).not.toHaveAttribute('open');
  });

  it('keeps a concrete fallback outcome, actions, exact reference and one folded assignment', () => {
    const saved = fixture();
    const attempt = { ...saved.attempts[0], toolCalls: [
      { name: 'read_source_inventory', status: 'waiting' },
      { name: 'read_source_window', status: 'completed' },
      { name: 'custom_reader', status: 'rejected' },
    ] };
    saved.attempts = [attempt];
    const { container } = render(<RecordedWorkTimeline snapshot={saved} {...required} selectedAttemptId={attempt.id} />);
    const assignment = container.querySelector<HTMLDetailsElement>('.timeline-task-assignment')!;
    expect(assignment).not.toHaveAttribute('open');
    expect(assignment).toHaveTextContent(saved.tasks.find(task => task.id === attempt.taskId)!.title);
    const details = container.querySelector<HTMLDetailsElement>('.agent-step-details')!;
    fireEvent.click(within(details).getByText('Inspect this step'));
    expect(details).toHaveTextContent('List saved sources (requested; result not saved)');
    expect(details).toHaveTextContent('Read a saved source excerpt (completed)');
    expect(details).toHaveTextContent('custom_reader (rejected)');
    expect(details).toHaveTextContent(attempt.id);
    expect(details).not.toHaveTextContent('Assignment:');
  });

  it('labels a long saved bar as elapsed attempt time, not tool runtime, and leaves a missing end as a point', () => {
    const saved = fixture();
    const template = saved.attempts[0];
    saved.events = [];
    saved.messages = [];
    saved.attempts = [
      { ...template, id: 'long-step', startedAt: '2026-10-11T13:34:26Z', endedAt: '2026-10-11T15:13:58Z' },
      { ...template, id: 'open-step', status: 'superseded', startedAt: '2026-10-11T15:15:00Z', endedAt: undefined },
    ];
    const { container } = render(<RecordedWorkTimeline snapshot={saved} {...required} />);
    const longStep = within(container).getByRole('button', { name: /^Open work step 1:/ }).closest('.agent-step')!;
    expect(longStep).toHaveTextContent('Elapsed 1h 39m 32s · includes waits or gaps; not tool runtime');
    expect(longStep.querySelector('.agent-step-bar')).toBeVisible();
    const chronologicalBar = longStep.querySelector<HTMLElement>('.agent-step-bar')!;
    expect(parseFloat(chronologicalBar.style.left)).toBe(0);
    expect(parseFloat(chronologicalBar.style.width)).toBeCloseTo(100 * (Date.parse(saved.attempts[0].endedAt!) - Date.parse(saved.attempts[0].startedAt)) / (Date.parse(saved.attempts[1].startedAt) - Date.parse(saved.attempts[0].startedAt)));
    const chronologicalOpen = within(container).getByRole('button', { name: /^Open work step 2:/ }).closest('.agent-step')!;
    expect(chronologicalOpen.querySelector('.agent-step-point')).toBeVisible();
    expect(chronologicalOpen.querySelector('.agent-step-bar')).not.toBeInTheDocument();
    fireEvent.click(within(container).getByRole('button', { name: 'By agent' }));
    const longStepByAgent = within(container).getByRole('button', { name: /^Open work step 1:/ }).closest('.agent-step')!;
    expect(longStepByAgent.querySelector('.agent-step-bar')).toBeInTheDocument();
    const openStep = within(container).getByRole('button', { name: /^Open work step 2:/ }).closest('.agent-step')!;
    expect(openStep.querySelector('.agent-step-point')).toBeInTheDocument();
    expect(openStep.querySelector('.agent-step-bar')).not.toBeInTheDocument();
    expect(openStep).toHaveTextContent('Finish time not recorded');
  });

  it('keeps rejected tool and local preparation failures visually distinct from a completed model turn', () => {
    const saved = savedReadTimeline();
    const rejected = { ...saved.attempts[0], toolCalls: [{ name: 'peer_request', status: 'rejected' }] };
    const local = { ...saved.attempts[1], status: 'superseded' as const, endedAt: undefined, toolCalls: [] };
    saved.attempts = [rejected, local];
    const call = { kind: 'agent_call', id: 'call-1', version: 1 };
    saved.inspection!.records.push({ reference: { kind: 'model_response_receipt', id: 'response-local', version: 1 }, label: 'Saved local response', data: { call_ref: call, attempt_id: local.id, status: 'partial' } });
    saved.inspection!.attempts[1]!.explanation = 'Context preparation failed locally for this exact call before provider dispatch.';
    const { container } = render(<RecordedWorkTimeline snapshot={saved} {...required} />);
    fireEvent.click(within(container).getByRole('button', { name: 'By agent' }));
    const rejectedRow = within(container).getByRole('button', { name: /Requested: Request peer assistance; Tool request rejected/ });
    expect(rejectedRow.querySelector('.agent-step-icon')).toHaveTextContent('!');
    expect(rejectedRow.closest('.agent-step')!.querySelector('.agent-step-bar')).toHaveClass('state-failed');
    const localRow = within(container).getByRole('button', { name: /Local context preparation failed|Local preparation failed/ });
    expect(localRow.querySelector('.agent-step-icon')).toHaveTextContent('!');
    expect(localRow.closest('.agent-step')!.querySelector('.agent-step-point')).toHaveClass('state-failed');
  });

  it('preserves an exact selected earlier-run step and refuses a mismatched parent', () => {
    const saved = fixture();
    const old = { ...saved.attempts[0], id: 'earlier-step' };
    const current = { ...saved.attempts[0], id: 'current-step', startedAt: '2026-10-08T14:10:00Z' };
    const parent = { ...saved, snapshotId: 'exact-parent', attempts: [old] };
    saved.snapshotId = 'selected-child';
    saved.parentSnapshotId = parent.snapshotId;
    saved.attempts = [old, current];
    const { container, rerender } = render(<RecordedWorkTimeline snapshot={saved} parentSnapshot={parent} {...required} selectedAttemptId={old.id} />);
    expect(within(container).getAllByRole('button', { name: /^Open work step 1:/ })).toHaveLength(2);
    expect(within(container).getByRole('button', { name: /earlier run; started/ })).toHaveAttribute('aria-pressed', 'true');
    rerender(<RecordedWorkTimeline snapshot={saved} parentSnapshot={{ ...parent, snapshotId: 'wrong-parent' }} {...required} />);
    expect(within(container).queryByRole('button', { name: /Include work from earlier run/ })).not.toBeInTheDocument();
    expect(within(container).getByRole('button', { name: /^Open work step 2:/ })).toBeInTheDocument();
    expect(container).not.toHaveTextContent('earlier run');
  });
});

describe('read grouping fidelity', () => {
  it('folds adjacent successful source and context reads, preserving exact rows and bars', () => {
    const saved = savedReadTimeline();
    saved.attempts[3] = { ...saved.attempts[3], toolCalls: [{ name: 'read_context_record', status: 'completed' }] };
    const { container } = render(<RecordedWorkTimeline snapshot={saved} {...required} selectedAttemptId="source-read-2" />);
    const chronologicalGroups = container.querySelectorAll<HTMLDetailsElement>('.agent-step-group > details');
    expect(chronologicalGroups[1]).not.toHaveAttribute('open');
    expect(chronologicalGroups[1].querySelectorAll('summary .agent-step-bar')).toHaveLength(2);
    expect(chronologicalGroups[1].querySelector('summary .agent-step-track')).toBeVisible();
    fireEvent.click(within(container).getByRole('button', { name: 'By agent' }));
    const groups = container.querySelectorAll<HTMLDetailsElement>('.agent-step-group > details');
    expect(groups).toHaveLength(2);
    expect(groups[0]).toHaveAttribute('open');
    expect(within(groups[0]).getByText(/Read saved source excerpts · 3 steps/)).toBeVisible();
    expect(groups[0].querySelectorAll('summary .agent-step-bar')).toHaveLength(3);
    expect(within(groups[0]).getByRole('button', { name: /^Open work step 2:/ })).toHaveAttribute('aria-pressed', 'true');
    expect(within(container).getByRole('button', { name: /Read a list of saved task records|Opened a saved task record/ })).toBeInTheDocument();
  });

  it('does not fold across a message, event, other actor, changed task, failure, or waiting step', () => {
    const scenarios = ['message', 'event', 'actor', 'task', 'failure', 'waiting'] as const;
    for (const scenario of scenarios) {
      const saved = savedReadTimeline();
      saved.attempts = saved.attempts.slice(0, 2);
      if (scenario === 'message') saved.messages = [{ ...fixture().messages[0], createdAt: '2026-10-08T14:00:30Z' }];
      if (scenario === 'event') saved.events = [{ ...fixture().events[0], time: '2026-10-08T14:00:30Z' }];
      if (scenario === 'actor') saved.attempts[1] = { ...saved.attempts[1], agentId: saved.agents.find(agent => agent.id !== saved.attempts[0].agentId)!.id };
      if (scenario === 'task') saved.attempts[1] = { ...saved.attempts[1], taskId: saved.tasks.find(task => task.id !== saved.attempts[0].taskId)!.id };
      if (scenario === 'failure') saved.attempts[1] = { ...saved.attempts[1], toolCalls: [{ name: 'read_source_window', status: 'rejected' }] };
      if (scenario === 'waiting') saved.attempts[1] = { ...saved.attempts[1], status: 'waiting', endedAt: undefined, toolCalls: [{ name: 'read_source_window', status: 'waiting' }] };
      const { container, unmount } = render(<RecordedWorkTimeline snapshot={saved} {...required} />);
      expect(container.querySelectorAll('.agent-step-group'), scenario).toHaveLength(0);
      expect(within(container).getAllByRole('button', { name: /^Open work step/ }), scenario).toHaveLength(2);
      unmount();
    }
  });

  it('does not combine reads from an exact earlier run with the current task version', () => {
    const saved = savedReadTimeline();
    const parent = { ...saved, snapshotId: 'source-parent', attempts: saved.attempts.slice(0, 2) };
    saved.snapshotId = 'source-child';
    saved.parentSnapshotId = parent.snapshotId;
    const { container } = render(<RecordedWorkTimeline snapshot={saved} parentSnapshot={parent} includeParentHistory={true} onToggleParentHistory={() => {}} onSelectAttempt={() => {}} onSelectTask={() => {}} />);
    const groups = container.querySelectorAll('.agent-step-group');
    expect(groups).toHaveLength(2);
    expect(groups[0]).toHaveTextContent('earlier run');
    expect(groups[1]).not.toHaveTextContent('earlier run');
    expect(within(container).getAllByRole('button', { name: /^Open work step 1:/ })).toHaveLength(2);
  });
});
