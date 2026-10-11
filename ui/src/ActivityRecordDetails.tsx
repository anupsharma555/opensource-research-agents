/* Explain saved activity records using their named agents, work and timing.
   These views describe recorded outcomes and returned summaries without
   inventing missing explanations or treating elapsed time as model latency. */
import type { ResearchSnapshot } from './generated/contracts';
import { AttemptInspection } from './InspectionDetails';
import { describeLiveAttempt, showsOriginalRunningRecord } from './attemptOutcome';

export type ActivityItem = { id: string; time: string; category: 'agent' | 'communication' | 'event'; title: string; summary: string; taskId?: string; attemptId?: string; sourceId?: string; status?: string; badgeStatus?: string; timeLabel: string; agentId?: string | null; fromAgentId?: string; toAgentId?: string };

function dateParts(value: string) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return { date: value, time: 'Time unavailable' };
  return {
    date: new Intl.DateTimeFormat('en-US', { weekday: 'short', month: 'short', day: 'numeric', year: 'numeric' }).format(date),
    time: new Intl.DateTimeFormat('en-US', { hour: 'numeric', minute: '2-digit', second: '2-digit', timeZoneName: 'short' }).format(date),
  };
}

export function formatActivityTimestamp(value: string) {
  const parts = dateParts(value);
  return `${parts.date} · ${parts.time}`;
}

export function ActivityTimestamp({ item, id, attempt }: { item: ActivityItem; id?: string; attempt?: ResearchSnapshot['attempts'][number] }) {
  const parts = dateParts(item.time);
  if (attempt) return <div id={id} className="activity-timestamp"><span>{dateParts(attempt.startedAt).date}</span><time dateTime={attempt.startedAt}><span>Started</span><strong>{dateParts(attempt.startedAt).time}</strong></time>{attempt.endedAt ? <time dateTime={attempt.endedAt}><span>Finished</span><strong>{dateParts(attempt.endedAt).time}</strong>{dateParts(attempt.endedAt).date !== dateParts(attempt.startedAt).date && <span>{dateParts(attempt.endedAt).date}</span>}</time> : <span>No finish saved</span>}</div>;
  return <div id={id} className="activity-timestamp"><span>{item.timeLabel}</span><time dateTime={item.time}><span>{parts.date}</span><strong>{parts.time}</strong></time></div>;
}

const attemptStatus: Record<string, string> = {
  completed: 'This attempt finished. The overall research task may still have other work in progress.',
  failed: 'This attempt did not complete successfully. Other attempts and contributions can continue.',
  running: 'This attempt was still running when the snapshot was saved.',
  waiting: 'This attempt was waiting when the snapshot was saved. Waiting is separate from a completed result.',
  cancelled: 'This attempt was stopped before it completed.',
  uncertain: 'The saved record does not establish whether this attempt completed.',
  superseded: 'This attempt was superseded. Its successor and any provider response need separate saved evidence.',
};
const messageStatus: Record<string, string> = {
  requested: 'One agent asked another agent to do work. No delivery or response is established by this record alone.',
  delivered: 'The request reached its recipient. This does not yet mean the work is complete.',
  acknowledged: 'The recipient acknowledged the request. A completed response is still separate.',
  fulfilled: 'The requested work has a saved contribution. Its source or claim is listed below when available.',
};
const eventKind: Record<string, string> = {
  mission_started: 'The overall research task was opened.',
  parallel_work: 'The run recorded work happening at the same time on related contributions.',
  retrieval_failed: 'A source retrieval route failed. The summary identifies the affected work.',
  critique: 'An agent reviewed an interpretation and recorded a challenge or correction.',
  claim_revision: 'The run saved a revised claim or report after considering additional work.',
};

function elapsed(start: string, end: string) {
  const seconds = Math.round((Date.parse(end) - Date.parse(start)) / 1000);
  if (!Number.isFinite(seconds) || seconds < 0) return 'Not available';
  return `${Math.floor(seconds / 60)} min ${seconds % 60} sec`;
}

export function ActivityRecordDetails({ item, snapshot, compactSummary }: { item: ActivityItem; snapshot: ResearchSnapshot; compactSummary: string }) {
  const task = snapshot.tasks.find(task => task.id === item.taskId);
  const attempt = snapshot.attempts.find(attempt => attempt.id === item.attemptId);
  const message = item.category === 'communication' ? snapshot.messages.find(message => message.id === item.id) : undefined;
  const event = item.category === 'event' ? snapshot.events.find(event => event.id === item.id) : undefined;
  const agent = snapshot.agents.find(agent => agent.id === (attempt?.agentId || event?.agentId));
  const original = message && snapshot.messages.find(candidate => (candidate.requestId || candidate.id) === (message.requestId || message.id) && candidate.status === 'requested');
  const firstMessage = message && snapshot.messages.filter(candidate => (candidate.requestId || candidate.id) === (message.requestId || message.id)).sort((a, b) => a.createdAt.localeCompare(b.createdAt))[0];
  const deliveredReply = message?.status === 'delivered' && firstMessage && message.id !== firstMessage.id && message.fromAgentId === firstMessage.toAgentId && message.toAgentId === firstMessage.fromAgentId;
  const source = snapshot.sources.find(source => source.id === item.sourceId);
  const claim = snapshot.claims.find(claim => claim.id === message?.claimId);
  const name = (id: string) => snapshot.agents.find(agent => agent.id === id)?.name ?? `Agent identity unavailable (${id})`;
  const throughSnapshot = attempt && !attempt.endedAt && (attempt.status === 'running' || attempt.status === 'waiting');
  const end = attempt?.endedAt || (throughSnapshot ? snapshot.capturedAt : undefined);
  const outcome = attempt ? describeLiveAttempt(snapshot, attempt) : null;
  const originalRunningRecord = attempt ? showsOriginalRunningRecord(snapshot, attempt, outcome) : false;
  const attributedSummary = attempt?.resultSummary || outcome?.summary || 'No result summary was saved.';
  const meaning = outcome?.explanation ?? (attempt ? attemptStatus[attempt.status] : deliveredReply ? 'A saved peer reply was delivered back to the requester.' : message ? messageStatus[message.status] : event ? eventKind[event.kind] || event.summary : 'No additional explanation was recorded.');
  const repeatedMeaning = item.category === 'agent' && (compactSummary.trim() === meaning.trim() || (!attempt?.resultSummary && attributedSummary.trim() === meaning.trim()));
  return <div className="activity-record-details">
    <dl className="activity-detail-grid">
      {!repeatedMeaning && <div><dt>What this means</dt><dd>{meaning}</dd></div>}
      {outcome && attempt && <div><dt>{originalRunningRecord ? 'Snapshot attempt state' : 'Stored attempt status'}</dt><dd>{attempt.status}{originalRunningRecord ? ' (saved attempt record: running)' : ''}. This records the model turn or host attempt; the provider, tool and report outcomes are identified separately above.</dd></div>}
      <div><dt>{message ? 'Request between agents' : 'Who acted'}</dt><dd>{message ? `${name(message.fromAgentId)} → ${name(message.toAgentId)}` : agent ? `${agent.name} · ${agent.role}` : attempt?.agentId || event?.agentId || 'Run/host'}</dd></div>
      {task && <>{item.category !== 'agent' && <div><dt>Work toward the research question</dt><dd><strong>{task.title}</strong></dd></div>}<div><dt>Timing expectation</dt><dd>{task.latencyClass ? `${task.latencyClass.replaceAll('_', ' ')} · ` : 'Not yet estimated · '}{task.latencyBasis}</dd></div>{task.status === 'waiting' && <div><dt>Current wait</dt><dd>{task.waitingReason || 'Reason not recorded in this snapshot'}</dd></div>}{task.status === 'waiting' && task.waitingOperationReference && <div><dt>Next eligible model attempt</dt><dd>{task.waitingUntil ? formatActivityTimestamp(task.waitingUntil) : 'Time not recorded'}. Eligibility does not guarantee dispatch at that time.</dd></div>}</>}
      {task?.firstContributionAt && <div><dt>First saved contribution</dt><dd>{formatActivityTimestamp(task.firstContributionAt)}</dd></div>}
      {task && !task.firstContributionAt && task.firstUsefulAt && <div><dt>First useful result in this older snapshot</dt><dd>{formatActivityTimestamp(task.firstUsefulAt)}</dd></div>}
      {original && <div><dt>Original request</dt><dd>{original.summary}</dd></div>}
      {attempt && <><div><dt>Started</dt><dd>{formatActivityTimestamp(attempt.startedAt)}</dd></div><div><dt>{throughSnapshot ? 'Last saved view' : 'Finished'}</dt><dd>{end ? formatActivityTimestamp(end) : 'Finish time not recorded'}</dd></div><div><dt>Recorded time span</dt><dd>{end ? elapsed(attempt.startedAt, end) : 'Not available'}<p>Elapsed time for this attempt, including any waiting; this is not a separate measure of model response time.</p></dd></div></>}
      {outcome?.responseStatus && <div><dt>Saved provider response</dt><dd>{outcome.responseStatus.replaceAll('_', ' ')}{outcome.httpStatus !== null ? ` · HTTP ${outcome.httpStatus}` : ''}{outcome.responseAt ? ` · ${formatActivityTimestamp(outcome.responseAt)}` : ''}</dd></div>}
      {outcome?.configuredProvider && <div><dt>Configured model provider</dt><dd>{outcome.configuredProvider}</dd></div>}
      {outcome?.retryAfterSeconds !== null && outcome?.retryAfterSeconds !== undefined && <div><dt>Provider retry guidance</dt><dd>{outcome.retryAfterSeconds} seconds after its response. This is saved guidance, not proof a later request was sent.</dd></div>}
      {source && <div><dt>Source contribution</dt><dd>{source.id} · {source.title}</dd></div>}
      {claim && <div><dt>Saved claim revision</dt><dd>Version {claim.version}: {claim.text}</dd></div>}
    </dl>
    {attempt && <><div><h4>Tools used</h4>{attempt.toolCalls.length ? <ul className="activity-tool-list">{attempt.toolCalls.map((tool, index) => <li key={`${tool.name}-${index}`}><strong>{tool.name}</strong><span>{tool.status.replaceAll('_', ' ')} · {tool.provider || 'Provider not recorded'}</span></li>)}</ul> : <p>No tool action was recorded for this attempt.</p>}</div><div><h4>Additional saved explanation</h4>{attempt.returnedReasoning.availability === 'returned' && attempt.returnedReasoning.text ? <p>{attempt.returnedReasoning.text}</p> : attempt.returnedReasoning.availability === 'truncated' ? <p>{attempt.returnedReasoning.text || 'Only an incomplete excerpt was recorded.'} The explanation is incomplete.</p> : <p>{attempt.returnedReasoning.availability === 'restricted' ? 'This additional content is restricted; the recorded outcome above remains available.' : 'No additional explanation text was saved for this attempt.'}</p>}</div></>}
    {snapshot.inspection?.timingBasis === 'controlled_clock' && <p>These times use a controlled test clock, not measured agent or provider latency.</p>}
    {attempt && <AttemptInspection snapshot={snapshot} attemptId={attempt.id} />}
    <details className="activity-record-ids"><summary>Technical record references</summary><p>Record: {item.id}{task ? ` · task: ${task.id}` : ''}{message?.requestId ? ` · request: ${message.requestId}` : ''}</p></details>
  </div>;
}
