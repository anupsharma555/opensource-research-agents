/* Put each named agent's contribution on the same saved time scale.
   Separate attempt bars preserve retries and gaps; waiting is shown distinctly
   from work. The timeline selects details on the current review page. */
import type { ResearchSnapshot } from './generated/contracts';
import { formatActivityTimestamp } from './ActivityRecordDetails';
import { AgentBadge } from './AgentBadge';
import { classifyAttemptLineage } from './attemptLineage';

type Props = {
  snapshot: ResearchSnapshot;
  selectedTaskId?: string;
  onSelect: (taskId: string) => void;
};

function elapsedLabel(milliseconds: number) {
  if (milliseconds <= 0) return 'Start';
  const minutes = Math.floor(milliseconds / 60000);
  if (minutes === 0) return '<1 min';
  return milliseconds % 60000 === 0 ? `+${minutes} min` : `+${minutes}–${minutes + 1} min`;
}

function minuteTicks(duration: number) {
  if (!Number.isFinite(duration) || duration <= 0) return [];
  const wholeMinutes = Math.floor(duration / 60000);
  if (wholeMinutes === 0) return [{ label: 'Start', left: 0 }, { label: '<1 min', left: 100 }];
  const step = Math.max(1, Math.ceil(wholeMinutes / 4));
  const marks = [...new Set([0, ...Array.from({ length: Math.floor(wholeMinutes / step) }, (_, index) => (index + 1) * step), wholeMinutes])];
  return marks.map(minute => ({ label: minute === 0 ? 'Start' : `+${minute} min`, left: 100 * minute * 60000 / duration }));
}

function TimeAxis({ ticks, title }: { ticks: ReturnType<typeof minuteTicks>; title: string }) {
  return <div className="contribution-axis" aria-hidden="true"><span>{title}</span><div>{ticks.map(tick => <span key={tick.label} className={tick.left === 0 ? 'at-start' : tick.left === 100 ? 'at-end' : ''} style={{ left: `${tick.left}%` }}>{tick.label}</span>)}</div></div>;
}

export function ContributionTimeline({ snapshot, selectedTaskId, onSelect }: Props) {
  const start = Date.parse(snapshot.mission.startedAt);
  const end = Date.parse(snapshot.capturedAt);
  const duration = end - start;
  const validScale = Number.isFinite(duration) && duration > 0;
  const axisTicks = minuteTicks(duration);
  const rows = snapshot.tasks.map(task => {
    const attempts = snapshot.attempts.filter(attempt => attempt.taskId === task.id);
    const names = [...new Set(attempts.map(attempt => snapshot.agents.find(agent => agent.id === attempt.agentId)?.name || attempt.agentId))];
    const segments = attempts.flatMap(attempt => {
      const from = Date.parse(attempt.startedAt);
      const throughSnapshot = !attempt.endedAt && (attempt.status === 'running' || attempt.status === 'waiting');
      const to = attempt.endedAt ? Date.parse(attempt.endedAt) : throughSnapshot ? end : NaN;
      if (!validScale || !Number.isFinite(from) || !Number.isFinite(to) || to < from || from > end || to < start) return [];
      return [{ attempt, from: Math.max(start, from), to: Math.min(end, to), throughSnapshot }];
    }).sort((a, b) => a.from - b.from);
    return { task, names, segments };
  }).sort((a, b) => (a.segments[0]?.from ?? Infinity) - (b.segments[0]?.from ?? Infinity));

  return <div className="contribution-timeline" role="group" aria-label="Work toward this answer">
    <TimeAxis ticks={axisTicks} title="Agent contribution" />
    {rows.map(({ task, names, segments }) => {
      const timing = segments.map(segment => `${segment.attempt.status.replaceAll('_', ' ')}: ${elapsedLabel(segment.from - start)} to ${elapsedLabel(segment.to - start)}${segment.to - segment.from < 60000 ? ' (<1 min attempt)' : ''}${segment.throughSnapshot ? ' (through saved snapshot)' : ''}`).join('; ');
      return <button key={task.id} type="button" className={`contribution-row ${selectedTaskId === task.id ? 'selected' : ''}`} aria-pressed={selectedTaskId === task.id} aria-label={`${task.title}; ${names.join(', ') || 'No agent recorded'}; ${task.status}; ${timing || 'Timing unavailable'}`} onClick={() => onSelect(task.id)}>
        <span className="contribution-label"><strong>{task.title}</strong><small>{names.join(', ') || 'No agent recorded'} · {task.status.replaceAll('_', ' ')}</small></span>
        <span className="contribution-track" aria-hidden="true">{axisTicks.slice(1).map(tick => <i className="contribution-tick" key={tick.label} style={{ left: `${tick.left}%` }} />)}{segments.length ? segments.map(({ attempt, from, to, throughSnapshot }) => <span key={attempt.id} className={`contribution-segment segment-${attempt.status}`} style={{ left: `${100 * (from - start) / duration}%`, width: `${100 * (to - from) / duration}%` }} title={`${attempt.status}: ${elapsedLabel(from - start)} to ${elapsedLabel(to - start)}${to - from < 60000 ? ' · <1 min attempt' : ''}${throughSnapshot ? ' · through saved snapshot' : ''}`} />) : <span className="timing-unavailable">Timing unavailable</span>}</span>
      </button>;
    })}
    <div className="contribution-legend"><span><i className="legend-work" />Recorded work</span><span><i className="legend-failed" />Failed attempt</span><span><i className="legend-waiting" />Waiting</span><small>{snapshot.inspection?.timingBasis === 'controlled_clock' ? 'Controlled test clock · not measured latency' : 'Time since research began · ends at saved snapshot'}</small></div>
  </div>;
}

export function RecordedWorkTimeline({ snapshot, parentSnapshot, includeParentHistory, onToggleParentHistory, selectedAttemptId, onSelectAttempt, onSelectTask }: {
  snapshot: ResearchSnapshot;
  parentSnapshot?: ResearchSnapshot;
  includeParentHistory: boolean;
  onToggleParentHistory: () => void;
  selectedAttemptId?: string;
  onSelectAttempt: (attemptId: string, taskId: string) => void;
  onSelectTask: (taskId: string) => void;
}) {
  const lineage = classifyAttemptLineage(snapshot, parentSnapshot);
  const parentTaskIds = new Set(parentSnapshot?.tasks.map(item => item.id) ?? []);
  const parentEventIds = new Set(parentSnapshot?.events.map(item => item.id) ?? []);
  const currentAttempts = snapshot.attempts.filter(item => lineage.get(item.id) !== 'inherited');
  const parentAttempts = snapshot.attempts.filter(item => lineage.get(item.id) === 'inherited');
  const visibleAttempts = (includeParentHistory ? snapshot.attempts : currentAttempts)
    .slice().sort((a, b) => a.startedAt.localeCompare(b.startedAt) || a.id.localeCompare(b.id));
  const visibleEvents = includeParentHistory ? snapshot.events : snapshot.events.filter(item => !parentEventIds.has(item.id));
  const starts = [...visibleAttempts.map(item => Date.parse(item.startedAt)), ...visibleEvents.map(item => Date.parse(item.time))].filter(Number.isFinite);
  const finishes = [...visibleAttempts.map(item => Date.parse(item.endedAt || (item.status === 'running' || item.status === 'waiting' ? snapshot.capturedAt : ''))), ...visibleEvents.map(item => Date.parse(item.time))].filter(Number.isFinite);
  const start = starts.length ? Math.min(...starts) : NaN;
  const end = finishes.length ? Math.max(...finishes) : NaN;
  const duration = end - start;
  const ticks = minuteTicks(duration);
  const noAttemptTasks = snapshot.tasks.filter(task => !snapshot.attempts.some(item => item.taskId === task.id) && (includeParentHistory || !parentTaskIds.has(task.id)));
  const parentTimes = [...parentAttempts.map(item => Date.parse(item.endedAt || item.startedAt)), ...(parentSnapshot?.events.map(item => Date.parse(item.time)) ?? [])].filter(Number.isFinite);
  const currentTimes = [...currentAttempts.map(item => Date.parse(item.startedAt)), ...snapshot.events.filter(item => !parentEventIds.has(item.id)).map(item => Date.parse(item.time))].filter(Number.isFinite);
  const gapStart = parentTimes.length ? Math.max(...parentTimes) : NaN;
  const gapEnd = currentTimes.length ? Math.min(...currentTimes) : NaN;
  const hasGap = includeParentHistory && Number.isFinite(gapStart) && Number.isFinite(gapEnd) && gapEnd > gapStart;

  return <div className="recorded-work-timeline">
    {parentSnapshot && <button className="outline-button" aria-pressed={includeParentHistory} onClick={onToggleParentHistory}>{includeParentHistory ? 'Hide parent history' : `Include parent history (${parentAttempts.length} attempt${parentAttempts.length === 1 ? '' : 's'})`}</button>}
    <p className="screen-intro">{includeParentHistory ? 'Showing current work alongside unchanged parent attempts.' : parentSnapshot ? 'Showing new and updated attempts in this follow-up. Unchanged parent attempts can be included.' : 'Showing recorded attempts in this run.'} Updated attempts keep their saved identity and timestamps; the Details records retain exact times.</p>
    {hasGap && <p className="timeline-gap">No recorded work in this branch between {formatActivityTimestamp(new Date(gapStart).toISOString())} and {formatActivityTimestamp(new Date(gapEnd).toISOString())}.</p>}
    <div className="contribution-timeline" role="group" aria-label="Recorded agent attempts">
      <TimeAxis ticks={ticks} title="Recorded attempt" />
      {visibleAttempts.map((attempt, index) => {
        const agent = snapshot.agents.find(item => item.id === attempt.agentId);
        const name = agent?.name ?? `Agent identity unavailable (${attempt.agentId})`;
        const task = snapshot.tasks.find(item => item.id === attempt.taskId);
        const tools = attempt.toolCalls.map(item => `${item.name} (${item.status.replaceAll('_', ' ')})`).join(' → ');
        const from = Date.parse(attempt.startedAt);
        const to = Date.parse(attempt.endedAt || (attempt.status === 'running' || attempt.status === 'waiting' ? snapshot.capturedAt : ''));
        const validBar = Number.isFinite(duration) && duration > 0 && Number.isFinite(from) && Number.isFinite(to) && to >= from && from >= start && to <= end;
        return <button key={attempt.id} className={`contribution-row recorded-attempt-row ${selectedAttemptId === attempt.id ? 'selected' : ''}`} aria-pressed={selectedAttemptId === attempt.id} aria-label={`Attempt ${index + 1}; ${name}; ${attempt.status}; ${task?.title ?? 'Task title unavailable'}; ${tools || 'No tool action saved'}; ${attempt.resultSummary || 'No result summary saved'}`} onClick={() => onSelectAttempt(attempt.id, attempt.taskId)}>
          <span className="contribution-label"><AgentBadge name={agent?.name} agentId={attempt.agentId} /><span className="attempt-assignment"><strong>Attempt {index + 1} · Assignment:</strong> {task?.title ?? 'Task title unavailable'}{lineage.get(attempt.id) === 'inherited' ? ' · parent history' : lineage.get(attempt.id) === 'revised' ? ' · updated since parent' : ''}</span><span className="attempt-actions"><strong>Recorded actions:</strong> {tools || 'No tool action saved'}</span><span className="attempt-response-label">{attempt.status === 'completed' ? `Saved response from ${name}` : `Saved attempt outcome from ${name}`}</span><span className="attempt-result-preview" title={attempt.resultSummary || ''}>{attempt.resultSummary || 'No result summary saved.'}</span></span>
          <span className="contribution-track" aria-hidden="true">{ticks.slice(1).map(tick => <i className="contribution-tick" key={tick.label} style={{ left: `${tick.left}%` }} />)}{validBar ? <span className={`contribution-segment segment-${attempt.status}`} style={{ left: `${100 * (from - start) / duration}%`, width: `${100 * (to - from) / duration}%` }} title={`${formatActivityTimestamp(attempt.startedAt)} to ${formatActivityTimestamp(attempt.endedAt || snapshot.capturedAt)} · ${elapsedLabel(to - from)} recorded elapsed span (may include waits)`} /> : <span className="timing-unavailable">Timing unavailable</span>}</span>
        </button>;
      })}
      {visibleAttempts.length === 0 && <p className="screen-intro">No agent attempt was recorded for this branch yet.</p>}
      <div className="contribution-legend"><span><i className="legend-work" />Recorded elapsed span (may include waits)</span><span><i className="legend-failed" />Failed attempt</span><span><i className="legend-waiting" />Waiting</span><small>{snapshot.inspection?.timingBasis === 'controlled_clock' ? 'Controlled test clock · not measured latency' : 'Whole-minute visual scale · exact times in selected records'}</small></div>
    </div>
    {noAttemptTasks.length > 0 && <details className="inline-detail request-only-records"><summary>Task and request records without an agent attempt ({noAttemptTasks.length})</summary><div className="inline-detail-body">{noAttemptTasks.map(task => <button className="inline-link" key={task.id} onClick={() => onSelectTask(task.id)}>{task.title} · no attempt recorded{parentTaskIds.has(task.id) ? ' · parent history' : ''}</button>)}</div></details>}
  </div>;
}
