/* Group saved actions for a compact overview without merging their records.
   Every interval and expanded step keeps its exact identity and outcome. */
import type { ResearchSnapshot } from './generated/contracts';
import { useState } from 'react';
import { AgentBadge } from './AgentBadge';
import { formatActivityTimestamp } from './ActivityRecordDetails';
import { describeLiveAttempt } from './attemptOutcome';
import { stepPresentation } from './stepPresentation';
import { savedDuration } from './savedDuration';
import './WorkOverview.css';

type Attempt = ResearchSnapshot['attempts'][number];

export function workGroupLabel(snapshot: ResearchSnapshot, attempt: Attempt) {
  const presentation = stepPresentation(snapshot, attempt);
  if (presentation.actorKind === 'provider') return `${presentation.providerName ?? 'Model service'} call errors`;
  if (presentation.actorKind === 'system' || attempt.status === 'superseded') return 'System preparation and pauses';
  if (attempt.toolCalls.length !== 1) return 'Other saved work';
  const tool = attempt.toolCalls[0].name;
  if (['read_source_window', 'inspect_hit', 'fetch_source', 'search_scholarly', 'select_working_sources'].includes(tool)) return 'Source reading and checks';
  if (['read_context_record', 'read_context_page', 'read_source_inventory', 'discover_capabilities', 'request_capability', 'list_venues', 'navigate_venue', 'propose_venue'].includes(tool)) return 'Task context and tool lookups';
  if (['peer_request', 'review_peer_answer', 'wait_for_dependencies'].includes(tool)) return 'Peer requests and reviews';
  if (tool === 'publish_result') return 'Research contributions';
  return 'Other saved work';
}

function markerState(snapshot: ResearchSnapshot, attempt: Attempt) {
  const outcome = describeLiveAttempt(snapshot, attempt);
  if (attempt.status === 'failed' || attempt.toolCalls.some(tool => ['rejected', 'failed', 'invalid', 'source_unavailable'].includes(tool.status))
    || ['provider_rate_limit', 'provider_rejection', 'tool_rejected', 'source_unavailable'].includes(outcome?.badgeStatus ?? '')) return 'failed';
  if (stepPresentation(snapshot, attempt).actorKind === 'system' || attempt.status === 'superseded') return 'system';
  return 'recorded';
}

function savedTime(value: string | null | undefined) {
  return value && Number.isFinite(Date.parse(value)) ? formatActivityTimestamp(value) : 'Not recorded';
}

function elapsedSpan(start: string, finish: string | null | undefined) {
  if (!finish) return 'No finish recorded';
  return savedDuration(start, finish) ?? 'Elapsed span unavailable';
}

function modelApiSummary(snapshot: ResearchSnapshot, attempt: Attempt) {
  if (!snapshot.proofKind?.startsWith('live_')) return 'Model API: live use not established by this example.';
  const records = snapshot.inspection?.records ?? [];
  const provenances = snapshot.inspection?.attempts.filter(item => item.attemptId === attempt.id) ?? [];
  if (provenances.length !== 1) return 'Model API: call evidence unavailable or ambiguous.';
  const reference = provenances[0].configurationReference;
  const configurations = reference ? records.filter(item => item.reference.kind === reference.kind
    && item.reference.id === reference.id && item.reference.version === reference.version) : [];
  const provider = configurations.length === 1 && typeof configurations[0].data.provider === 'string' ? configurations[0].data.provider.trim() : '';
  const service = provider.toLowerCase() === 'groq' ? 'Groq' : provider || 'Model';
  const outcome = describeLiveAttempt(snapshot, attempt);
  let evidence = 'request or response not confirmed in saved records.';
  if (outcome?.httpStatus && outcome.httpStatus >= 100 && outcome.httpStatus <= 599) {
    evidence = `response received (HTTP ${outcome.httpStatus}${outcome.httpStatus >= 400 ? '; request rejected' : ''}).`;
  } else if (outcome?.responseStatus === 'tool_requested') evidence = 'model response saved; it requested this action.';
  else if (outcome?.responseStatus === 'complete') evidence = 'model response saved.';
  else if (outcome?.responseStatus === 'partial') evidence = 'partial model response saved.';
  else if (outcome && !outcome.responseStatus && [
    'Preparation superseded before dispatch', 'Context preparation needs local repair',
    'Local context preparation failed', 'Local model-input preparation failed',
  ].includes(outcome.label)) evidence = 'not sent; preparation stopped locally.';
  return `${service} API: ${evidence}`;
}

export function WorkOverview({ snapshot, earlierAttemptIds, onOpenActivity }: {
  snapshot: ResearchSnapshot; earlierAttemptIds: ReadonlySet<string>;
  onOpenActivity: (group: string, attemptId?: string) => void;
}) {
  const [inspected, setInspected] = useState<{ group: string; attemptId: string } | null>(null);
  const attempts = snapshot.attempts.filter(item => !earlierAttemptIds.has(item.id)).sort((a, b) => Date.parse(a.startedAt) - Date.parse(b.startedAt) || a.id.localeCompare(b.id));
  const groups = new Map<string, Attempt[]>();
  for (const attempt of attempts) {
    const label = workGroupLabel(snapshot, attempt);
    groups.set(label, [...(groups.get(label) ?? []), attempt]);
  }
  const times = attempts.flatMap(item => [Date.parse(item.startedAt), item.endedAt && Date.parse(item.endedAt) >= Date.parse(item.startedAt) ? Date.parse(item.endedAt) : NaN]).filter(Number.isFinite);
  const start = times.length ? Math.min(...times) : NaN;
  const end = times.length ? Math.max(...times) : NaN;
  const duration = end - start;
  const scaled = Number.isFinite(duration) && duration > 0;
  const ticks = scaled ? [0, 25, 50, 75, 100] : [];
  const name = (id: string) => snapshot.agents.find(item => item.id === id)?.name;
  const runElapsed = savedDuration(snapshot.mission.startedAt, snapshot.capturedAt);

  if (!attempts.length) return null;
  return <section className="work-overview" aria-label="Grouped work overview">
    <p className="work-overview-guide">Similar actions share a row. Select a row for its steps in Activity. Hover a bar, or focus a row and use the arrow keys, to inspect one saved step; select that bar to open its exact Activity details.</p>
    <div className="work-overview-axis" aria-hidden="true"><span>Recorded actions</span><span>{ticks.map(tick => <small key={tick} style={{ left: `${tick}%` }}>{tick === 0 ? 'Start' : duration < 120000 ? `+${Math.round(duration * tick / 100 / 1000)} sec` : `+${Math.round(duration * tick / 100 / 60000)} min`}</small>)}</span></div>
    {[...groups].map(([label, steps], groupIndex) => {
      const actors = [...new Set(steps.map(item => item.agentId))];
      const issues = steps.filter(item => markerState(snapshot, item) === 'failed').length;
      const selected = inspected?.group === label ? steps.find(item => item.id === inspected.attemptId) : undefined;
      const selectedPresentation = selected && stepPresentation(snapshot, selected);
      const tooltipId = `work-overview-step-${groupIndex}`;
      return <button key={label} type="button" className="work-overview-group" onClick={event => {
        const marker = (event.target as HTMLElement).closest<HTMLElement>('[data-attempt-id]');
        if (marker?.dataset.attemptId) onOpenActivity(label, marker.dataset.attemptId);
        else onOpenActivity(label);
      }} onKeyDown={event => {
        if (event.key === 'Escape') setInspected(null);
        else if (event.key === 'ArrowLeft' || event.key === 'ArrowRight') {
          event.preventDefault();
          const current = selected ? steps.indexOf(selected) : event.key === 'ArrowRight' ? -1 : 0;
          const next = (current + (event.key === 'ArrowRight' ? 1 : -1) + steps.length) % steps.length;
          setInspected({ group: label, attemptId: steps[next].id });
        } else if (selected && (event.key === 'Enter' || event.key === ' ')) {
          event.preventDefault();
          onOpenActivity(label, selected.id);
        }
      }} onBlur={() => setInspected(null)} onMouseOut={event => { if (!(event.relatedTarget instanceof Node) || !event.currentTarget.contains(event.relatedTarget)) setInspected(null); }} aria-describedby={selected ? tooltipId : undefined} aria-label={`${label}: view ${steps.length} ${steps.length === 1 ? 'step' : 'steps'} in Activity`}>
          <span className="work-overview-label"><strong>{label} <span aria-hidden="true">→</span></strong><span>{steps.length} saved {steps.length === 1 ? 'step' : 'steps'}{issues ? ` · ${issues} rejected or failed` : ''}</span><span className="work-overview-agents">{actors.map(id => <AgentBadge key={id} agentId={id} name={name(id)} />)}</span></span>
          <span className="work-overview-track" aria-hidden="true" style={{ height: `${Math.max(32, actors.length * 20)}px` }}>
            {actors.map((id, index) => <span key={id} className="work-overview-lane" style={{ top: `${index * 20 + 2}px` }}><AgentBadge agentId={id} name={name(id)} /></span>)}
            <span className="work-overview-plot">{ticks.map(tick => <i key={tick} className="work-overview-tick" style={{ left: `${tick}%` }} />)}
            {steps.map(attempt => {
              const from = Date.parse(attempt.startedAt);
              const through = attempt.endedAt ? Date.parse(attempt.endedAt) : NaN;
              if (!Number.isFinite(from)) return null;
              const hasInterval = scaled && Number.isFinite(through) && through > from;
              return <i key={attempt.id} data-attempt-id={attempt.id} onMouseEnter={() => setInspected({ group: label, attemptId: attempt.id })} className={`work-overview-mark mark-${markerState(snapshot, attempt)} ${hasInterval ? 'mark-interval' : 'mark-point'}`} style={{ left: `${scaled ? 100 * (from - start) / duration : 0}%`, width: hasInterval ? `${100 * (through - from) / duration}%` : undefined, top: `${actors.indexOf(attempt.agentId) * 20 + 8}px` }} />;
            })}</span>
          </span>
          {selected && selectedPresentation && <span id={tooltipId} className="work-overview-tooltip" role="tooltip" aria-live="polite" onClick={event => { event.preventDefault(); event.stopPropagation(); }}><strong>{selectedPresentation.actorKind === 'system' ? 'System · ' : selectedPresentation.actorKind === 'provider' ? `${selectedPresentation.providerName ?? 'Model service'} · ` : ''}{name(selected.agentId) ?? 'Agent identity unavailable'}</strong><span>{selectedPresentation.title} · {selectedPresentation.outcomeLabel}</span><span className="work-overview-api">{modelApiSummary(snapshot, selected)}</span><span>Started {savedTime(selected.startedAt)}<br />{selected.endedAt ? `Finished ${savedTime(selected.endedAt)} · ${elapsedSpan(selected.startedAt, selected.endedAt)}` : 'Finish not recorded · no elapsed interval'}</span><span>Whole run at saved view: {runElapsed ?? 'elapsed time unavailable'}.</span><small>Elapsed may include waits; it is not tool runtime. Select this bar to open the exact step.</small></span>}
      </button>;
    })}
    <div className="work-overview-legend"><span><i className="mark-recorded" />Saved step interval</span><span><i className="mark-failed" />Rejected or failed</span><span><i className="mark-system" />System preparation or pause</span></div>
    <p className="work-overview-timing">{snapshot.inspection?.timingBasis === 'controlled_clock' && 'Controlled test clock · '}{!times.length && 'Saved times are unavailable. '}Bars use saved start and finish times and may include waits. Dots have no recorded duration. Gaps remain visible; bar length is not tool runtime.</p>
  </section>;
}
