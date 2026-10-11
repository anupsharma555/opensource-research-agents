/* Put each named agent's contribution on the same saved time scale.
   Separate attempt bars preserve retries and gaps; waiting is shown distinctly
   from work. The timeline selects details on the current review page. */
import { useEffect, useRef, useState, type ReactNode } from 'react';
import type { ResearchSnapshot } from './generated/contracts';
import { formatActivityTimestamp } from './ActivityRecordDetails';
import { AgentBadge } from './AgentBadge';
import { TermHelp } from './TermHelp';
import { classifyAttemptLineage, workStepNumbers } from './attemptLineage';
import { assignTaskNumbers, exactActivityParent, taskIdentity } from './activityGroups';
import { describeLiveAttempt, recordedToolAction } from './attemptOutcome';
import { stepPresentation } from './stepPresentation';
import './ContributionTimeline.css';

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

function compactTime(value: string, includeDate: boolean) {
  const date = new Date(value);
  if (!Number.isFinite(date.getTime())) return 'Time unavailable';
  return new Intl.DateTimeFormat(undefined, includeDate
    ? { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }
    : { hour: 'numeric', minute: '2-digit' }).format(date);
}

const stepStateLabels: Record<string, string> = {
  tool_recovery_needed: 'result needs recovery', tool_waiting: 'tool result pending',
  provider_rate_limit: 'rate limited', provider_rejection: 'provider rejected',
  accepted_report: 'report accepted', tool_step_saved: 'tool step saved',
  response_saved: 'reply saved', metadata_only: 'metadata only',
  tool_rejected: 'tool rejected', source_unavailable: 'source unavailable',
};

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

type SavedAttempt = ResearchSnapshot['attempts'][number];

function SourceReadGroup({ attemptIds, selectedAttemptId, summary, summaryLabel, children }: {
  attemptIds: string[];
  selectedAttemptId?: string;
  summary: ReactNode;
  summaryLabel: string;
  children: ReactNode;
}) {
  const details = useRef<HTMLDetailsElement>(null);
  const selectedInside = Boolean(selectedAttemptId && attemptIds.includes(selectedAttemptId));
  useEffect(() => {
    if (selectedInside && details.current) details.current.open = true;
  }, [selectedInside, selectedAttemptId]);
  return <li className="agent-step-group"><details ref={details}><summary className="agent-step-group-summary" aria-label={summaryLabel}>{summary}</summary><ol className="agent-step-group-children">{children}</ol></details></li>;
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

export function RecordedWorkTimeline({ snapshot, parentSnapshot, includeParentHistory, onToggleParentHistory, selectedAttemptId, inspectAttemptId, onSelectAttempt, onSelectTask, renderStepDetails, taskLabel }: {
  snapshot: ResearchSnapshot;
  parentSnapshot?: ResearchSnapshot;
  includeParentHistory: boolean;
  onToggleParentHistory: () => void;
  selectedAttemptId?: string;
  inspectAttemptId?: string;
  onSelectAttempt: (attemptId: string, taskId: string) => void;
  onSelectTask: (taskId: string) => void;
  renderStepDetails?: (attemptId: string, taskId: string) => ReactNode;
  taskLabel?: (taskId: string) => string;
}) {
  const [order, setOrder] = useState<'time' | 'agent'>('time');
  const [expandedAttemptId, setExpandedAttemptId] = useState<string | null>(null);
  useEffect(() => { if (inspectAttemptId) setExpandedAttemptId(inspectAttemptId); }, [inspectAttemptId]);
  const exactParent = exactActivityParent(snapshot, parentSnapshot);
  const lineage = classifyAttemptLineage(snapshot, exactParent);
  const parentTaskIds = new Set(exactParent?.tasks.map(item => item.id) ?? []);
  const parentAttempts = snapshot.attempts.filter(item => lineage.get(item.id) === 'inherited');
  const selectedParentVisible = !includeParentHistory && parentAttempts.some(item => item.id === selectedAttemptId);
  const visibleAttempts = (includeParentHistory ? snapshot.attempts : snapshot.attempts.filter(item => lineage.get(item.id) !== 'inherited' || item.id === selectedAttemptId))
    .slice().sort((a, b) => a.startedAt.localeCompare(b.startedAt) || a.id.localeCompare(b.id));
  const observedTimes = visibleAttempts.flatMap(item => {
    const begun = Date.parse(item.startedAt);
    const ended = item.endedAt ? Date.parse(item.endedAt) : NaN;
    return [begun, ended >= begun ? ended : NaN];
  }).filter(Number.isFinite);
  const start = observedTimes.length ? Math.min(...observedTimes) : NaN;
  const end = observedTimes.length ? Math.max(...observedTimes) : NaN;
  const duration = end - start;
  const ticks = minuteTicks(duration);
  const noAttemptTasks = snapshot.tasks.filter(task => !snapshot.attempts.some(item => item.taskId === task.id) && (includeParentHistory || !parentTaskIds.has(task.id)));
  const stepNumbers = workStepNumbers(snapshot, new Set(parentAttempts.map(attempt => attempt.id)));
  const fallbackTaskNumbers = assignTaskNumbers(snapshot);
  const name = (id: string) => snapshot.agents.find(agent => agent.id === id)?.name ?? id;
  const labelTask = (id: string) => taskLabel?.(id) ?? taskIdentity(snapshot, id, fallbackTaskNumbers).label;
  const selectedAttempt = snapshot.attempts.find(item => item.id === selectedAttemptId);
  const selectedTask = selectedAttempt && snapshot.tasks.find(item => item.id === selectedAttempt.taskId);
  const interruptionTimes = [
    ...snapshot.messages.map(item => Date.parse(item.createdAt)),
    ...snapshot.events.map(item => Date.parse(item.time)),
  ].filter(Number.isFinite);

  function readGroupKey(attempt: SavedAttempt) {
    const outcome = describeLiveAttempt(snapshot, attempt);
    const tool = attempt.toolCalls.length === 1 ? attempt.toolCalls[0] : null;
    if (attempt.status !== 'completed' || !tool || !['read_source_window', 'read_context_record', 'read_context_page'].includes(tool.name)
      || tool.status !== 'completed' || outcome?.responseStatus !== 'tool_requested'
      || outcome.badgeStatus !== 'tool_step_saved') return null;
    return [attempt.agentId, attempt.taskId, lineage.get(attempt.id), tool.name].join('\u0000');
  }

  // Group only adjacent steps in the full time order. A peer message, saved event,
  // another agent's step, or an overlapping interval keeps its own boundary.
  const groups: SavedAttempt[][] = [];
  for (const attempt of visibleAttempts) {
    const previousGroup = groups[groups.length - 1];
    const previous = previousGroup?.[previousGroup.length - 1];
    const key = readGroupKey(attempt);
    const previousEnd = previous?.endedAt ? Date.parse(previous.endedAt) : NaN;
    const nextStart = Date.parse(attempt.startedAt);
    const noEventBetween = previous && Number.isFinite(nextStart)
      && !interruptionTimes.some(time => time > Date.parse(previous.startedAt) && time <= nextStart);
    if (previous && key && key === readGroupKey(previous) && Number.isFinite(previousEnd)
      && Number.isFinite(nextStart) && previousEnd <= nextStart && noEventBetween) previousGroup.push(attempt);
    else groups.push([attempt]);
  }

  function trackMarks(attempts: SavedAttempt[]) {
    return <span className="agent-step-track" aria-hidden="true">{ticks.slice(1).map(tick => <i className="agent-step-tick" key={tick.label} style={{ left: String(tick.left) + '%' }} />)}{attempts.map(attempt => {
      const from = Date.parse(attempt.startedAt);
      const to = attempt.endedAt ? Date.parse(attempt.endedAt) : NaN;
      if (!Number.isFinite(from)) return <span key={attempt.id} className="timing-unavailable">Start time unavailable</span>;
      const point = Number.isFinite(duration) && duration > 0 ? 100 * (from - start) / duration : 0;
      const bar = Number.isFinite(to) && to > from && duration > 0;
      return bar
        ? <i key={attempt.id} className="agent-step-bar state-completed" style={{ left: String(point) + '%', width: String(100 * (to - from) / duration) + '%' }} />
        : <i key={attempt.id} className="agent-step-point state-completed" style={{ left: String(point) + '%' }} />;
    })}</span>;
  }

  function attemptRow(attempt: SavedAttempt) {
    const task = snapshot.tasks.find(item => item.id === attempt.taskId);
    const number = stepNumbers.get(attempt.id) ?? 1;
    const outcome = describeLiveAttempt(snapshot, attempt);
    const presentation = stepPresentation(snapshot, attempt);
    const from = Date.parse(attempt.startedAt);
    const to = attempt.endedAt ? Date.parse(attempt.endedAt) : NaN;
    const point = Number.isFinite(duration) && duration > 0 && Number.isFinite(from) ? 100 * (from - start) / duration : 0;
    const hasEnd = Number.isFinite(to) && to >= from;
    const bar = hasEnd && duration > 0 && to > from;
    const localFailure = outcome?.label === 'Local context preparation failed' || outcome?.label === 'Local model-input preparation failed';
    const problem = localFailure || ['provider_rejection', 'provider_rate_limit', 'tool_rejected', 'source_unavailable'].includes(outcome?.badgeStatus ?? '') || attempt.status === 'failed';
    const markerState = problem ? 'failed' : outcome?.badgeStatus === 'tool_recovery_needed' ? 'waiting' : attempt.status;
    const icon = outcome?.badgeStatus === 'accepted_report' ? '✓' : problem || outcome?.badgeStatus === 'tool_recovery_needed' ? '!' : attempt.status === 'superseded' ? '↷' : attempt.toolCalls.length ? '↗' : '•';
    const elapsed = hasEnd ? elapsedSpan(from, to) : null;
    const expanded = expandedAttemptId === attempt.id;
    return <li key={attempt.id} className="agent-step">
      <div className="agent-step-main">
        <button type="button" className={'agent-step-select' + (selectedAttemptId === attempt.id ? ' selected' : '')} aria-pressed={selectedAttemptId === attempt.id} onClick={() => { setExpandedAttemptId(attempt.id); onSelectAttempt(attempt.id, attempt.taskId); }} aria-label={'Open work step ' + number + ': ' + presentation.title + '; ' + presentation.outcomeLabel + (lineage.get(attempt.id) === 'inherited' ? '; earlier run' : '') + '; started ' + formatActivityTimestamp(attempt.startedAt)}>
          <span className="agent-step-icon" aria-hidden="true">{icon}</span>
          <span className="agent-step-text"><span className="agent-step-identity">{presentation.actorKind === 'agent' ? <AgentBadge name={name(attempt.agentId)} agentId={attempt.agentId} /> : <span className="system-action-label">{presentation.actorKind === 'system' ? 'System' : presentation.providerName ?? 'Model service'} · for {name(attempt.agentId)}</span>}<span>{task?.title === snapshot.mission.question ? 'Shared request' : labelTask(attempt.taskId)} · step {number}{lineage.get(attempt.id) === 'inherited' ? ' · earlier run' : lineage.get(attempt.id) === 'revised' ? ' · updated' : ''}</span></span><strong>{presentation.title}</strong><small>{presentation.outcomeLabel}</small></span>
        </button>
        <span className="agent-step-track" aria-hidden="true">{ticks.slice(1).map(tick => <i className="agent-step-tick" key={tick.label} style={{ left: String(tick.left) + '%' }} />)}{Number.isFinite(from) ? bar ? <i className={'agent-step-bar state-' + markerState} style={{ left: String(point) + '%', width: String(100 * (to - from) / duration) + '%' }} /> : <i className={'agent-step-point state-' + markerState} style={{ left: String(point) + '%' }} /> : <span className="timing-unavailable">Start time unavailable</span>}</span>
      </div>
      <p className="agent-step-times"><span>Started <time dateTime={attempt.startedAt}>{formatActivityTimestamp(attempt.startedAt)}</time></span><span>{hasEnd ? <>Finished <time dateTime={attempt.endedAt!}>{formatActivityTimestamp(attempt.endedAt!)}</time></> : 'Finish time not recorded'}</span>{elapsed && <span>Elapsed {elapsed}{to - from >= 5 * 60_000 ? ' · includes waits or gaps; not tool runtime' : ''}</span>}</p>
      <details className="agent-step-details" open={expanded}>
        <summary onClick={event => {
          event.preventDefault();
          setExpandedAttemptId(expanded ? null : attempt.id);
          if (!expanded) onSelectAttempt(attempt.id, attempt.taskId);
        }}>Inspect this step</summary>
        {expanded && <div className="agent-step-detail-body">
          {renderStepDetails ? renderStepDetails(attempt.id, attempt.taskId) : <>
            <p><strong>Saved outcome:</strong> {attempt.resultSummary || presentation.summary}</p>
            <p><strong>Recorded actions:</strong> {attempt.toolCalls.length ? attempt.toolCalls.map(recordedToolAction).join(' → ') : 'No tool action saved.'}</p>
            <details><summary>Exact step reference</summary><p>{attempt.id} · {attempt.taskId}</p></details>
          </>}
        </div>}
      </details>
    </li>;
  }

  function renderedGroup(group: SavedAttempt[]) {
    if (group.length === 1) return attemptRow(group[0]);
    const first = group[0];
    const last = group[group.length - 1];
    const firstNumber = stepNumbers.get(first.id) ?? 1;
    const lastNumber = stepNumbers.get(last.id) ?? firstNumber;
    const tool = first.toolCalls[0]?.name;
    const groupTitle = tool === 'read_source_window' ? 'Read saved source excerpts'
      : tool === 'read_context_record' ? 'Read saved task records' : 'Read saved task pages';
    return <SourceReadGroup key={first.id} attemptIds={group.map(item => item.id)} selectedAttemptId={selectedAttemptId}
      summaryLabel={groupTitle + '; ' + group.length + ' saved steps; first started ' + formatActivityTimestamp(first.startedAt) + '; last finished ' + formatActivityTimestamp(last.endedAt ?? last.startedAt) + '. Expand to inspect each exact step.'}
      summary={<><span className="agent-step-group-label"><strong><AgentBadge name={name(first.agentId)} agentId={first.agentId} /> {groupTitle} · {group.length} steps</strong><small>{labelTask(first.taskId)} · steps {firstNumber}–{lastNumber} · {formatActivityTimestamp(first.startedAt)} to {formatActivityTimestamp(last.endedAt ?? last.startedAt)}{lineage.get(first.id) === 'inherited' ? ' · earlier run' : ''}</small></span>{trackMarks(group)}</>}>
      {group.map(attemptRow)}
    </SourceReadGroup>;
  }

  const agentIds = [...new Set(groups.map(group => group[0].agentId))];
  const dayKey = (value: string) => {
    const date = new Date(value);
    return Number.isFinite(date.getTime()) ? new Intl.DateTimeFormat(undefined, { year: 'numeric', month: 'long', day: 'numeric' }).format(date) : 'Date unavailable';
  };
  let previousDay = '';
  let previousOrigin = '';
  const chronological = groups.flatMap(group => {
    const date = dayKey(group[0].startedAt);
    const origin = lineage.get(group[0].id) === 'inherited' ? 'Earlier run' : 'Selected run';
    const boundary = exactParent && origin !== previousOrigin
      ? [<li className="agent-step-boundary" key={'origin:' + group[0].id}><strong>{origin}</strong></li>] : [];
    const heading = date !== previousDay ? [<li className="agent-step-day" key={'day:' + group[0].id}><h4>{date}</h4></li>] : [];
    previousDay = date;
    previousOrigin = origin;
    return [...boundary, ...heading, renderedGroup(group)];
  });

  return <div className="recorded-work-timeline">
    {exactParent && <button className="outline-button" aria-pressed={includeParentHistory} onClick={onToggleParentHistory}>{includeParentHistory ? 'Hide work from the earlier run' : 'Include work from the earlier run (' + parentAttempts.length + (parentAttempts.length === 1 ? ' step)' : ' steps)')}</button>}
    {exactParent && <TermHelp term="earlier run" />}
    {selectedParentVisible && <p className="agent-lanes-intro">The selected earlier-run step remains visible while other parent history is hidden.</p>}
    {selectedTask && <details className="timeline-task-assignment"><summary>Saved assignment for {name(selectedAttempt!.agentId)}</summary><p>{labelTask(selectedTask.id)} · {selectedTask.id}</p><p>{selectedTask.title}</p><button type="button" className="inline-link" onClick={() => onSelectTask(selectedTask.id)}>Open task record →</button></details>}
    <div className="timeline-order" role="group" aria-label="Work step organization"><button type="button" aria-pressed={order === 'time'} className={order === 'time' ? 'active' : ''} onClick={() => setOrder('time')}>Time order</button><button type="button" aria-pressed={order === 'agent'} className={order === 'agent' ? 'active' : ''} onClick={() => setOrder('agent')}>By agent</button></div>
    <p className="agent-lanes-intro"><TermHelp term="work step" /> {visibleAttempts.length} saved step{visibleAttempts.length === 1 ? '' : 's'} · {agentIds.length} agent{agentIds.length === 1 ? '' : 's'}. Bars show each step’s saved start-to-finish interval on the same time scale. They include waits or gaps, not just tool execution. A dot marks a step without a recorded duration.</p>
    <div className="agent-lanes" role="group" aria-label="Recorded agent attempts">
      {Number.isFinite(duration) && duration > 0 && <div className="agent-time-axis" aria-hidden="true"><span>Elapsed from first saved step</span><span>Start <span>→</span> {elapsedLabel(duration)}</span></div>}
      {order === 'time'
        ? <ol className="agent-chronology">{chronological}</ol>
        : agentIds.map(agentId => <section className="agent-lane" key={agentId} aria-label={name(agentId) + ' saved steps'}><h4><AgentBadge name={name(agentId)} agentId={agentId} /><span>{groups.filter(group => group[0].agentId === agentId).reduce((count, group) => count + group.length, 0)} steps</span></h4><ol>{groups.filter(group => group[0].agentId === agentId).map(renderedGroup)}</ol></section>)}
      {visibleAttempts.length === 0 && <p className="screen-intro">No agent attempt was recorded for this branch yet.</p>}
      <p className="agent-lanes-note">Adjacent successful reads can be folded without removing their exact steps. {snapshot.inspection?.timingBasis === 'controlled_clock' ? 'Controlled test clock, not measured provider latency.' : 'Saved attempt times do not explain every interval; inspect a step for its recorded outcome.'}</p>
    </div>
    {noAttemptTasks.length > 0 && <details className="inline-detail request-only-records"><summary>Task and request records without an agent attempt ({noAttemptTasks.length})</summary><div className="inline-detail-body">{noAttemptTasks.map(task => <button className="inline-link" key={task.id} onClick={() => onSelectTask(task.id)}>{task.title} · no attempt recorded{parentTaskIds.has(task.id) ? ' · earlier run' : ''}</button>)}</div></details>}
  </div>;
}

function elapsedSpan(from: number, to: number): string | null {
  if (!Number.isFinite(from) || !Number.isFinite(to) || to < from) return null;
  let seconds = Math.floor((to - from) / 1000);
  const hours = Math.floor(seconds / 3600);
  seconds %= 3600;
  const minutes = Math.floor(seconds / 60);
  seconds %= 60;
  return [hours && String(hours) + 'h', minutes && String(minutes) + 'm', String(seconds) + 's'].filter(Boolean).join(' ');
}
