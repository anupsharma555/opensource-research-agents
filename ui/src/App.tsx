/* Display a saved research question and let a reviewer follow its evidence trail.
   The React views link findings to source passages, peer requests, task attempts,
   and separate diagnostics. Follow-up receipts and notes are local previews over
   simulated data; no research host receives a command. */
import { useEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties, ReactNode } from 'react';
import type { ResearchSnapshot } from './generated/contracts';
import { ContributionTimeline, RecordedWorkTimeline } from './ContributionTimeline';
import { classifyAttemptLineage } from './attemptLineage';
import { TaskSourceData } from './TaskSourceData';
import { ReviewCard } from './ReviewCard';
import { ReportOutput } from './ReportOutput';
import { acceptedReport, AttemptInspection, CitationDetails, RecordGroup, SnapshotDetails, SourceInspection } from './InspectionDetails';
import { ActivityRecordDetails, ActivityTimestamp, formatActivityTimestamp } from './ActivityRecordDetails';
import type { ActivityItem } from './ActivityRecordDetails';
import { LiveResearchApp } from './LiveResearchApp';
import { LiveCallInspector } from './LiveCallInspector';
import { AgentBadge } from './AgentBadge';
import { SavedAgentSteps } from './SavedAgentSteps';
import { CoordinationWork } from './CoordinationWork';
import { PeerCoordination } from './PeerCoordination';
import { ContributionReview } from './ContributionReview';
import { ExecutionDiagnostics, getReasoningAvailability } from './ExecutionDiagnostics';
import { RecordedText } from './RecordedText';
import { LiveWorkspaceAccess } from './LiveWorkspaceAccess';
import type { LiveRunSummary } from './live';
import './styles.css';

type Screen = 'overview' | 'activity' | 'communication' | 'work' | 'evidence' | 'diagnostics' | 'request' | 'compare' | 'history';
type SampleMode = 'loaded' | 'cleared' | 'replaying' | 'paused' | 'replayed';
type ReviewSection = 'input' | 'work' | 'contribution' | 'output';
type PreviewState = 'normal' | 'empty' | 'loading' | 'disconnected' | 'quota' | 'failed' | 'partial';
type Task = ResearchSnapshot['tasks'][number];
type Attempt = ResearchSnapshot['attempts'][number];
type Source = ResearchSnapshot['sources'][number];
type Claim = ResearchSnapshot['claims'][number];
type Message = ResearchSnapshot['messages'][number];

function peerExchange(history: Message[]) {
  const ordered = [...history].sort((a, b) => a.createdAt.localeCompare(b.createdAt));
  const first = ordered[0];
  const last = ordered[ordered.length - 1];
  const fulfilled = ordered.find(message => message.status === 'fulfilled');
  const deliveredReply = [...ordered].reverse().find(message => message.status === 'delivered' && message.fromAgentId === first.toAgentId && message.toAgentId === first.fromAgentId);
  const savedResult = fulfilled ? deliveredReply ?? [...ordered].reverse().find(message => message.status === 'fulfilled' && message.id !== first.id) : undefined;
  return { ordered, first, last, fulfilled, deliveredReply, savedResult, requestStatus: fulfilled ? 'fulfilled' : last.status };
}

function eventDisplay(event: ResearchSnapshot['events'][number], agents: Map<string, ResearchSnapshot['agents'][number]>) {
  const actor = event.agentId ? agents.get(event.agentId)?.name ?? `Agent identity unavailable (${event.agentId})` : 'Run/host';
  const summary = event.agentId && /^Agent work started\.?$/i.test(event.summary)
    ? `${actor} started work.`
    : event.agentId && /^Agent work completed\.?$/i.test(event.summary)
      ? `${actor} completed work.`
      : event.agentId && /^Agent result published for review\.?$/i.test(event.summary)
        ? `${actor} published a result for review.`
        : event.agentId ? `${actor}: ${event.summary}` : event.summary;
  return { title: `${actor} · ${labelStatus(event.kind)}`, summary };
}

function ActivityAttribution({ item, snapshot }: { item: ActivityItem; snapshot: ResearchSnapshot }) {
  const attempt = snapshot.attempts.find(record => record.id === item.attemptId);
  if (!attempt) return <p>{item.summary}</p>;
  const name = (id: string) => snapshot.agents.find(agent => agent.id === id)?.name ?? `Agent identity unavailable (${id})`;
  const task = snapshot.tasks.find(record => record.id === attempt.taskId);
  const incoming = snapshot.messages.find(message => message.taskId === attempt.taskId && message.toAgentId === attempt.agentId && message.fromAgentId !== attempt.agentId);
  const exchange = incoming ? peerExchange(snapshot.messages.filter(message => (message.requestId || message.id) === (incoming.requestId || incoming.id))) : undefined;
  const deliveredReply = exchange?.deliveredReply;
  const matchingAttempts = deliveredReply ? snapshot.attempts.filter(candidate => candidate.taskId === attempt.taskId && candidate.agentId === deliveredReply.fromAgentId && candidate.status === 'completed' && candidate.resultSummary?.trim() === deliveredReply.summary.trim() && candidate.endedAt && Date.parse(candidate.endedAt) <= Date.parse(deliveredReply.createdAt)) : [];
  const linkedReply = matchingAttempts.length === 1 && matchingAttempts[0].id === attempt.id ? deliveredReply : undefined;
  const replyRepeatsAttempt = linkedReply?.summary.trim() === attempt.resultSummary?.trim();
  return <div className="activity-attribution">
    <section><h5>Task assignment</h5><p>{task?.title ?? 'Task title unavailable'}</p></section>
    {exchange && <section><h5>Peer request</h5><div className="activity-attribution-meta"><AgentBadge name={snapshot.agents.find(agent => agent.id === exchange.first.fromAgentId)?.name} agentId={exchange.first.fromAgentId} /><span>asked {name(exchange.first.toAgentId)}</span><time dateTime={exchange.first.createdAt}>{formatActivityTimestamp(exchange.first.createdAt)}</time></div><p>{exchange.first.summary}</p></section>}
    <section><h5>{attempt.status === 'completed' ? 'Saved result' : 'Saved attempt outcome'}</h5><div className="activity-attribution-meta"><AgentBadge name={snapshot.agents.find(agent => agent.id === attempt.agentId)?.name} agentId={attempt.agentId} /><span>{attempt.status} attempt</span><time dateTime={attempt.endedAt || attempt.startedAt}>{formatActivityTimestamp(attempt.endedAt || attempt.startedAt)}</time></div><RecordedText text={attempt.resultSummary || 'No result summary was saved.'} /></section>
    {linkedReply && <section><h5>Delivered peer reply</h5><div className="activity-attribution-meta"><AgentBadge name={snapshot.agents.find(agent => agent.id === linkedReply.fromAgentId)?.name} agentId={linkedReply.fromAgentId} /><span>responded to {name(linkedReply.toAgentId)}</span><time dateTime={linkedReply.createdAt}>{formatActivityTimestamp(linkedReply.createdAt)}</time></div>{replyRepeatsAttempt ? <p>Reply text matches the saved result above.</p> : <RecordedText text={linkedReply.summary} />}</section>}
    {deliveredReply && !linkedReply && <p>The peer reply is a separate saved message; this attempt is not its uniquely verified match.</p>}
  </div>;
}

const navigation: { id: Screen; label: string; description: string; group: 'current' | 'start' | 'saved' }[] = [
  { id: 'request', label: 'Start or follow up', description: 'Draft a sample request and preview its scope. Nothing is submitted.', group: 'start' },
  { id: 'overview', label: 'Research review', description: 'Start here. Read the input, agent contributions, and saved output in order.', group: 'current' },
  { id: 'activity', label: 'Activity', description: 'Read saved agent actions in time order, or replay the scripted sample. No live agent connection exists.', group: 'current' },
  { id: 'communication', label: 'Communication', description: 'Read which named agent requested what from whom, then expand the recorded exchange.', group: 'current' },
  { id: 'evidence', label: 'Evidence', description: 'Select a source or citation to check the saved passage behind an output.', group: 'current' },
  { id: 'work', label: 'Details', description: 'Choose a task to inspect its exact status, agent attempts, and run metadata.', group: 'current' },
  { id: 'compare', label: 'Compare', description: 'Read two saved sample reports side by side and check their different source coverage.', group: 'current' },
  { id: 'history', label: 'Previous samples', description: 'Load the bundled sample again after clearing the active review.', group: 'saved' },
];

const previewStates: { value: PreviewState; label: string }[] = [
  { value: 'normal', label: 'Normal sample' },
  { value: 'empty', label: 'Empty mission' },
  { value: 'loading', label: 'Loading' },
  { value: 'disconnected', label: 'Disconnected' },
  { value: 'quota', label: 'Quota waiting' },
  { value: 'failed', label: 'Failed retrieval' },
  { value: 'partial', label: 'Partial evidence' },
];

function labelStatus(status: string) {
  return status.replaceAll('_', ' ');
}

function formatTime(value?: string | null) {
  if (!value) return 'Not yet recorded';
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }).format(date);
}

function StatusPill({ status }: { status: string }) {
  return <span className={`status-pill status-${status.replaceAll('_', '-')}`}>{labelStatus(status)}</span>;
}

function InfoTip({ label, description }: { label: string; description: string }) {
  return <span className="info-tip"><button type="button" className="info-tip-trigger" aria-label={`How to use ${label}: ${description}`}>i</button><span className="info-tip-content" role="tooltip"><strong>How to use</strong>{description}</span></span>;
}

function SectionHeading({ eyebrow, title, description }: { eyebrow: string; title: string; description?: string }) {
  return (
    <div className="section-heading">
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <div className="heading-with-tip"><h2>{title}</h2>{description && <InfoTip label={title} description={description} />}</div>
      </div>
    </div>
  );
}

function ReasoningView({ reasoning, live = false, snapshot, attemptId }: { reasoning: Attempt['returnedReasoning']; live?: boolean; snapshot?: ResearchSnapshot; attemptId?: string }) {
  const availability = reasoning.availability;
  const saved = snapshot && attemptId ? getReasoningAvailability(snapshot, attemptId) : undefined;
  const requestDescription = saved?.request === null || !saved
    ? 'The included records do not establish whether reasoning content was requested.'
    : saved.requestBasis === 'saved_call'
      ? `The saved call ${saved.request ? 'requested' : 'disabled'} reasoning content.`
      : `The saved configuration ${saved.request ? 'enables' : 'disables'} reasoning content; a configuration alone does not prove a call was sent.`;
  return (
    <div className={`reasoning-box reasoning-${availability}`}>
      <strong>{live ? `Provider-returned reasoning: ${labelStatus(availability)}` : availability === 'returned' ? 'Illustrative returned content' : `Returned content: ${labelStatus(availability)}`}</strong>
      {availability === 'returned' && reasoning.text ? (
        <p>{reasoning.text}</p>
      ) : availability === 'returned' ? (
        <p>The snapshot marks content as returned but does not include its text. Inspect the source record before quoting it.</p>
      ) : availability === 'truncated' ? (
        <p>{reasoning.text || (live ? 'Only a cut-off excerpt was saved.' : 'The saved fixture contains only a cut-off excerpt.')} This is incomplete; no missing continuation is inferred.</p>
      ) : availability === 'restricted' ? (
        <p>Access is restricted. The reader finding is separate from this diagnostic material.</p>
      ) : (
        <p>{live ? 'No reasoning text is included in this attempt field. Check the saved request and response status separately; absence here does not establish that none was requested or returned.' : 'No reasoning content was returned in this fixture. A concise rationale is not a substitute for missing provider content.'}</p>
      )}
      {live && <p>{requestDescription}</p>}
      <small>{live ? 'Only content included in the authorized saved view can appear here. Restricted records remain separate from findings and peer-review summaries.' : 'Scripted sample data. No model call was made for this screen.'}</small>
    </div>
  );
}

function PreviewNotice({ state, capturedAt }: { state: PreviewState; capturedAt: string }) {
  if (state === 'normal') return null;
  const copy: Record<Exclude<PreviewState, 'normal'>, { title: string; body: string; next: string }> = {
    empty: { title: 'No mission in this view', body: 'This is the empty-state preview. The sample run is still available from the state selector.', next: 'Start or preview a scoped request.' },
    loading: { title: 'Loading a saved view', body: 'A live app would request a snapshot and keep its last known cursor visible.', next: 'Wait for the response or inspect the last saved view.' },
    disconnected: { title: 'Connection unavailable', body: `The last sample snapshot was captured ${formatTime(capturedAt)}. Current host state is unknown.`, next: 'Reconnect, then compare the mission ID and cursor before acting.' },
    quota: { title: 'One task is waiting for capacity', body: 'This alternate scripted state shows a queued task. The normal sample methods task waits for source access, not quota.', next: 'Inspect the queued task and its last useful artifact.' },
    failed: { title: 'Retrieval attempt failed', body: 'A permitted methods table was not obtained. The abstract finding remains available with that limitation.', next: 'Inspect the failed attempt and request a different source route.' },
    partial: { title: 'Evidence is partial', body: 'The available abstracts support reported counts, but methods and adverse-event details are unavailable.', next: 'Read supported findings now; keep the specific gap open.' },
  };
  const item = copy[state];
  return (
    <div className={`preview-notice preview-${state}`} role="status">
      <div><strong>{item.title}</strong><p>{item.body}</p></div>
      <span>{item.next}</span>
    </div>
  );
}

interface LiveView {
  runId: string | null;
  model: string;
  runs: LiveRunSummary[];
  error: string | null;
  form: ReactNode;
  requestedAgentIds: string[] | null;
  onSelectRun: (runId: string) => void;
  lastCheckedAt?: string | null;
  connectionError?: string | null;
}

export function ResearchApp({ snapshot, comparisonSnapshot, live, initialScreen = 'overview' }: { snapshot: ResearchSnapshot; comparisonSnapshot?: ResearchSnapshot; live?: LiveView; initialScreen?: 'overview' | 'request' | 'history' }) {
  const recorded = snapshot.proofKind === 'offline_test';
  const coordinationFixture = recorded && ['stage3-offline-1', 'stage3-contribution-review-1'].includes(snapshot.sampleVersion);
  const attemptLineage = classifyAttemptLineage(snapshot, comparisonSnapshot);
  const inheritedAttemptIds = new Set(snapshot.attempts.filter(attempt => attemptLineage.get(attempt.id) === 'inherited').map(attempt => attempt.id));
  const firstCurrentAttempt = snapshot.attempts.filter(attempt => !inheritedAttemptIds.has(attempt.id)).sort((a, b) => a.startedAt.localeCompare(b.startedAt))[0];
  const selectionKey = `${live?.runId ?? snapshot.snapshotId}:${comparisonSnapshot?.snapshotId ?? ''}`;
  const initializedSelectionKey = useRef<string | null>(null);
  const [screen, setScreen] = useState<Screen>(initialScreen);
  const [reviewExpanded, setReviewExpanded] = useState<Record<ReviewSection, boolean>>({ input: true, work: true, contribution: true, output: true });
  const [sampleMode, setSampleMode] = useState<SampleMode>(() => !live && !recorded && window.localStorage.getItem('ora-active-sample') === 'cleared' ? 'cleared' : 'loaded');
  const [replayStep, setReplayStep] = useState(0);
  const [previewState, setPreviewState] = useState<PreviewState>('normal');
  const [selectedTaskId, setSelectedTaskId] = useState((live || recorded ? firstCurrentAttempt?.taskId : undefined) ?? snapshot.tasks[0]?.id ?? '');
  const [selectedReviewTaskId, setSelectedReviewTaskId] = useState(
    (live || recorded ? firstCurrentAttempt?.taskId : undefined)
      ?? snapshot.tasks.find(task => task.status === 'completed' && snapshot.messages.some(message => message.taskId === task.id && message.status === 'fulfilled'))?.id
      ?? snapshot.tasks.find(task => task.parentTaskId)?.id ?? '',
  );
  const [selectedSourceId, setSelectedSourceId] = useState(snapshot.sources[0]?.id ?? '');
  const [selectedClaimId, setSelectedClaimId] = useState(snapshot.claims[0]?.id ?? '');
  const [selectedAttemptId, setSelectedAttemptId] = useState((live || recorded ? firstCurrentAttempt?.id : undefined) ?? snapshot.attempts[0]?.id ?? '');
  const [includeParentHistory, setIncludeParentHistory] = useState(false);
  const [selectedSpanId, setSelectedSpanId] = useState<string | null>(null);
  const [activityFilter, setActivityFilter] = useState<'all' | ActivityItem['category']>('agent');
  const [activityAgentId, setActivityAgentId] = useState('all');
  const [activitySearch, setActivitySearch] = useState('');
  const [jumpToTask, setJumpToTask] = useState(false);
  const [requestKind, setRequestKind] = useState<'new' | 'follow_up'>('follow_up');
  const [requestQuestion, setRequestQuestion] = useState('Find an independent adult comparison before revising the earlier report.');
  const [requestScope, setRequestScope] = useState('Use permitted abstracts and exact passages. Keep the earlier report available while the source check runs.');
  const [requestTarget, setRequestTarget] = useState('report-v1');
  const [receipt, setReceipt] = useState(false);
  const [annotationText, setAnnotationText] = useState('');
  const [annotations, setAnnotations] = useState<{ id: number; target: string; text: string }[]>([]);

  useEffect(() => {
    if ((!live && !recorded) || !firstCurrentAttempt || initializedSelectionKey.current === selectionKey) return;
    initializedSelectionKey.current = selectionKey;
    setSelectedTaskId(firstCurrentAttempt.taskId);
    setSelectedReviewTaskId(firstCurrentAttempt.taskId);
    setSelectedAttemptId(firstCurrentAttempt.id);
    setIncludeParentHistory(false);
  }, [live, recorded, selectionKey, firstCurrentAttempt?.id]);

  useEffect(() => {
    setActivityAgentId('all');
    setActivitySearch('');
  }, [selectionKey]);

  useEffect(() => {
    window.scrollTo(0, 0);
  }, [screen]);

  useEffect(() => {
    if (screen !== 'work' || !jumpToTask) return;
    document.getElementById('selected-task')?.scrollIntoView?.({ block: 'start' });
    setJumpToTask(false);
  }, [screen, selectedTaskId, jumpToTask]);

  const agentsById = useMemo(() => new Map(snapshot.agents.map(agent => [agent.id, agent])), [snapshot.agents]);
  const tasksById = useMemo(() => new Map(snapshot.tasks.map(task => [task.id, task])), [snapshot.tasks]);
  const sourcesById = useMemo(() => new Map(snapshot.sources.map(source => [source.id, source])), [snapshot.sources]);
  const currentReport = acceptedReport(snapshot);
  const selectedTask = tasksById.get(selectedTaskId) ?? snapshot.tasks[0];
  const selectedSource = sourcesById.get(selectedSourceId);
  const selectedClaim = snapshot.claims.find(claim => claim.id === selectedClaimId) ?? snapshot.claims[0];
  const selectedAttempt = snapshot.attempts.find(attempt => attempt.id === selectedAttemptId) ?? snapshot.attempts[0];
  const activeNavigation = recorded ? navigation.map(item => ({ ...item, label: item.id === 'history' ? 'Previous tasks' : item.label, description: item.id === 'request' ? 'Open the live workspace to start or follow up on a real task.' : item.id === 'history' ? 'Access previous live tasks or the bundled recorded reviews.' : item.id === 'compare' ? 'Compare the immutable parent and follow-up, including their own saved citations.' : item.id === 'activity' ? 'Inspect saved events, attempts and messages. Recorded test times are not live provider latency.' : item.description })) : live ? navigation.map(item => item.id === 'history' ? { ...item, label: 'Saved runs', description: 'Open an actual saved local run.' } : item.id === 'request' ? { ...item, description: 'Submit a real start or exact saved follow-up.' } : item.id === 'activity' ? { ...item, description: 'Read saved agent actions refreshed from the local live host.' } : item.id === 'compare' ? { ...item, description: 'Compare the immutable parent with the current follow-up snapshot.' } : item) : navigation;
  const currentNav = activeNavigation.find(item => item.id === screen) ?? activeNavigation.find(item => item.id === 'work')!;
  const latestMessages = Array.from(
    [...snapshot.messages].sort((a, b) => a.createdAt.localeCompare(b.createdAt))
      .reduce((map, message) => map.set(message.requestId || message.id, message), new Map<string, ResearchSnapshot['messages'][number]>()).values(),
  );
  const activityItems: ActivityItem[] = [
    ...snapshot.events.map(event => ({ id: event.id, time: event.time, timeLabel: 'Event recorded', category: 'event' as const, ...eventDisplay(event, agentsById), taskId: event.taskId || undefined, agentId: event.agentId })),
    ...snapshot.attempts.map(attempt => ({ id: attempt.id, time: attempt.endedAt || attempt.startedAt, timeLabel: attempt.endedAt ? 'Attempt finished' : 'Attempt started', category: 'agent' as const, title: `${agentsById.get(attempt.agentId)?.name || `Agent identity unavailable (${attempt.agentId})`} · ${attempt.status === 'completed' ? 'saved result' : `${attempt.status} attempt`}`, summary: attempt.resultSummary || 'No result summary was saved.', taskId: attempt.taskId, attemptId: attempt.id, agentId: attempt.agentId, status: attempt.status })),
    ...snapshot.messages.map(message => {
      const exchange = peerExchange(snapshot.messages.filter(item => (item.requestId || item.id) === (message.requestId || message.id)));
      const from = agentsById.get(message.fromAgentId)?.name || `Agent identity unavailable (${message.fromAgentId})`;
      const to = agentsById.get(message.toAgentId)?.name || `Agent identity unavailable (${message.toAgentId})`;
      const isReply = exchange.deliveredReply?.id === message.id;
      return { id: message.id, time: message.createdAt, timeLabel: isReply ? 'Reply delivered' : 'Request recorded', category: 'communication' as const, title: isReply ? `${from} responded to ${to}` : message.id === exchange.first.id ? `${from} asked ${to}` : `${from} → ${to} · ${message.status}`, summary: message.summary, taskId: message.taskId, sourceId: message.sourceId || undefined, fromAgentId: message.fromAgentId, toAgentId: message.toAgentId, status: message.status };
    }),
  ].sort((a, b) => a.time.localeCompare(b.time) || a.id.localeCompare(b.id));
  const timelineStart = Math.min(...snapshot.attempts.map(item => new Date(item.startedAt).getTime()));
  const timelineEnd = new Date(snapshot.capturedAt).getTime();

  useEffect(() => {
    if (sampleMode !== 'replaying') return;
    if (replayStep >= activityItems.length) {
      setSampleMode('replayed');
      return;
    }
    const timer = window.setTimeout(() => setReplayStep(step => step + 1), 750);
    return () => window.clearTimeout(timer);
  }, [sampleMode, replayStep, activityItems.length]);

  function clearReview() {
    window.localStorage.setItem('ora-active-sample', 'cleared');
    setSampleMode('cleared');
    setReplayStep(0);
    setScreen('overview');
  }

  function loadSample() {
    window.localStorage.setItem('ora-active-sample', 'loaded');
    setSampleMode('loaded');
    setScreen('overview');
  }

  function replaySample() {
    window.localStorage.setItem('ora-active-sample', 'loaded');
    setReplayStep(0);
    setActivityFilter('all');
    setSampleMode('replaying');
    setScreen('activity');
  }

  function taskBarStyle(task: Task): CSSProperties {
    const attempts = snapshot.attempts.filter(item => item.taskId === task.id);
    if (!attempts.length || timelineEnd <= timelineStart) return { left: '0%', width: '4%' };
    const start = Math.min(...attempts.map(item => new Date(item.startedAt).getTime()));
    const finish = Math.max(...attempts.map(item => new Date(item.endedAt || snapshot.capturedAt).getTime()));
    const left = Math.max(0, Math.min(96, ((start - timelineStart) / (timelineEnd - timelineStart)) * 100));
    const width = Math.max(4, Math.min(100 - left, ((finish - start) / (timelineEnd - timelineStart)) * 100));
    return { left: `${left}%`, width: `${width}%` };
  }

  function openSource(sourceId: string, spanId?: string) {
    setSelectedSourceId(sourceId);
    setSelectedSpanId(spanId ?? null);
    setScreen('evidence');
  }

  function openTask(taskId: string) {
    setSelectedTaskId(taskId);
    const attempt = snapshot.attempts.find(item => item.taskId === taskId);
    if (attempt) setSelectedAttemptId(attempt.id);
    setJumpToTask(true);
    setScreen('work');
  }

  function openClaim(claimId: string) {
    setSelectedClaimId(claimId);
    setScreen('evidence');
  }

  function openAttempt(attemptId: string) {
    setSelectedAttemptId(attemptId);
    setScreen('diagnostics');
  }

  function toggleReviewCard(section: ReviewSection) {
    setReviewExpanded(current => ({ ...current, [section]: !current[section] }));
  }

  function renderOverview() {
    const reviewTasks = snapshot.tasks;
    const reviewTask = reviewTasks.find(task => task.id === selectedReviewTaskId) ?? reviewTasks[0];
    const messages = snapshot.messages.filter(message => message.taskId === reviewTask?.id || ((live || recorded) && tasksById.get(message.taskId)?.parentTaskId === reviewTask?.id));
    const requestIds = [...new Set(messages.map(message => message.requestId || message.id))];
    const exchange = requestIds.length ? peerExchange(messages.filter(message => (message.requestId || message.id) === requestIds[requestIds.length - 1])) : undefined;
    const request = live || recorded ? exchange?.first : messages.find(message => message.status === 'requested');
    const fulfilled = live || recorded ? exchange?.savedResult : [...messages].reverse().find(message => message.status === 'fulfilled');
    const linkedSourceId = [...messages].reverse().find(message => message.sourceId)?.sourceId || (!live && reviewTask?.id === 'task-a1' ? 'A1' : undefined);
    const linkedSource = linkedSourceId ? sourcesById.get(linkedSourceId) : undefined;
    const sourceResult = snapshot.inspection ? undefined : linkedSource?.passages.find(passage => passage.spanId.toLowerCase().endsWith('-result')) ?? linkedSource?.passages[0];
    const attempts = snapshot.attempts.filter(attempt => attempt.taskId === reviewTask?.id);
    const selectedReviewAttempt = attempts.find(attempt => attempt.id === selectedAttemptId) ?? attempts[0];
    const branchTaskAttempts = attempts.filter(attempt => inheritedAttemptIds.has(selectedReviewAttempt?.id ?? '') === inheritedAttemptIds.has(attempt.id));
    const attemptOrdinal = selectedReviewAttempt ? branchTaskAttempts.slice().sort((a, b) => a.startedAt.localeCompare(b.startedAt)).findIndex(attempt => attempt.id === selectedReviewAttempt.id) + 1 : 0;
    const selectedAgentName = selectedReviewAttempt ? agentsById.get(selectedReviewAttempt.agentId)?.name ?? `Agent identity unavailable (${selectedReviewAttempt.agentId})` : null;
    const contributionTitle = live || recorded ? selectedReviewAttempt ? `${selectedAgentName} · ${attemptLineage.get(selectedReviewAttempt.id) === 'revised' ? 'updated' : attemptOrdinal > 1 ? 'resumed' : 'initial'} attempt` : `${reviewTask?.title ?? 'Task'} · request record` : reviewTask?.title ?? 'Task';
    const contributors = [...new Set(attempts.map(attempt => agentsById.get(attempt.agentId)?.name || attempt.agentId))];
    const completed = [...attempts].reverse().find(attempt => attempt.status === 'completed');
    const failed = [...attempts].reverse().find(attempt => attempt.status === 'failed');
    const latestAttempt = attempts[attempts.length - 1];
    const targetReport = snapshot.inspection ? undefined : snapshot.artifacts.find(artifact => artifact.id === reviewTask?.targetArtifactId);
    const revisedClaim = snapshot.claims.find(claim => claim.id === fulfilled?.claimId);
    const output = live || recorded ? (fulfilled?.fromAgentId === selectedReviewAttempt?.agentId ? fulfilled?.summary : selectedReviewAttempt?.resultSummary) || fulfilled?.summary || 'No result summary was saved for this attempt.' : snapshot.inspection ? 'Accepted contributions are identified by the saved references below.' : !live && reviewTask?.id === 'task-a1' && sourceResult
      ? sourceResult.text
      : fulfilled?.summary
        || (reviewTask?.status === 'waiting' ? `No final output yet. ${failed?.resultSummary || latestAttempt?.resultSummary || 'The request remains open.'}`
          : reviewTask?.status === 'running' ? `No final output yet. ${latestAttempt?.resultSummary || 'Work is still in progress.'}`
            : completed?.resultSummary || 'No output was saved.');
    const allCollapsed = !Object.values(reviewExpanded).some(Boolean);
    const currentAgents = [...new Set(snapshot.attempts.filter(attempt => !inheritedAttemptIds.has(attempt.id)).map(attempt => agentsById.get(attempt.agentId)?.name || attempt.agentId))].join(', ');
    return (
      <div className="page-stack">
        <div className="review-toolbar"><SectionHeading eyebrow={recorded ? 'Saved offline demonstration' : live ? 'Saved live run' : 'Scripted sample'} title="Research review" description="Read input, agent contributions, and output on this page. Use each card's arrow to fold its details while keeping its heading visible. Collapse all for an overview, then expand the sections you want to focus on." /><button className="outline-button" aria-label={allCollapsed ? 'Expand all cards' : 'Collapse all cards'} onClick={() => setReviewExpanded({ input: allCollapsed, work: allCollapsed, contribution: allCollapsed, output: allCollapsed })}>{allCollapsed ? 'Expand all' : 'Collapse all'}</button></div>
        {coordinationFixture && <section className="panel" aria-label="Offline coordination fixture"><h3>Saved Stage 3 coordination test</h3><p>{snapshot.attempts.filter(attempt => attempt.status === 'completed').length} completed local callbacks · {snapshot.tasks.filter(task => task.status === 'waiting').length} waiting task · mission {snapshot.mission.status}.</p><p>These are recorded callback observations, not model inference or scientific findings. The saved peer question remains open; no provider or research service is connected to this view.</p></section>}
        <ReviewCard id="review-input" label="Research task input" controlLabel="input research task" className="panel mission-input" eyebrow="1 · Input research task" title={snapshot.mission.question} expanded={reviewExpanded.input} onToggle={() => toggleReviewCard('input')} actions={<InfoTip label="Research task input" description="Read the question, scope, and source material supplied with it. Expand the source data to read each original text. Use the card arrow to keep just the question visible." />}><p><strong>Scope:</strong> {snapshot.mission.scope}</p>{coordinationFixture ? <p>No study sources were supplied. The synthetic input is inspectable below.</p> : <TaskSourceData snapshot={snapshot} />}{coordinationFixture && snapshot.inspection && <RecordGroup snapshot={snapshot} title="Saved synthetic inputs" references={snapshot.inspection.attempts.flatMap(attempt => attempt.inputReferences ?? [])} />}<SnapshotDetails snapshot={snapshot} parentSnapshot={comparisonSnapshot} /></ReviewCard>
        <section className="task-review" aria-label="Task from input to output">
          <ReviewCard id="review-work" label="Agent contributions" controlLabel="agent contributions" eyebrow="2 · Agent work" title={coordinationFixture ? 'Recorded callback activity' : 'Work toward this answer'} summary={coordinationFixture ? `${snapshot.attempts.length} callback attempts · ${snapshot.agents.length} named peers` : live || recorded ? `${snapshot.attempts.filter(attempt => !inheritedAttemptIds.has(attempt.id)).length} current-branch attempts · ${snapshot.agents.length} available peers` : `${snapshot.agents.length} named agents · ${reviewTasks.length} contributions`} expanded={reviewExpanded.work} onToggle={() => toggleReviewCard('work')} actions={<InfoTip label="Agent work" description="Read saved attempt times and select one for its recorded tools and result. Earlier parent work can be included without hiding the gap between branches." />}><p className="story-context">{live || recorded ? <>{live && <><strong>Requested peers:</strong> {live.requestedAgentIds ? live.requestedAgentIds.map(id => agentsById.get(id)?.name ?? id).join(', ') : 'Not included in this saved snapshot.'} </>}<strong>Agents with current-branch attempts:</strong> {currentAgents || 'None yet.'}</> : 'All contributions answer the research question above. Aligned bars show work happening at the same time.'}</p><div className="contribution-panel">{live || recorded ? <RecordedWorkTimeline snapshot={snapshot} parentSnapshot={comparisonSnapshot} includeParentHistory={includeParentHistory} onToggleParentHistory={() => setIncludeParentHistory(value => !value)} selectedAttemptId={selectedAttemptId} onSelectAttempt={(attemptId, taskId) => { setSelectedAttemptId(attemptId); setSelectedReviewTaskId(taskId); setReviewExpanded(current => ({ ...current, contribution: true })); }} onSelectTask={taskId => { setSelectedReviewTaskId(taskId); setReviewExpanded(current => ({ ...current, contribution: true })); }} /> : <ContributionTimeline snapshot={snapshot} selectedTaskId={reviewTask?.id} onSelect={taskId => { setSelectedReviewTaskId(taskId); setReviewExpanded(current => ({ ...current, contribution: true })); }} />}</div></ReviewCard>
            {reviewTask && <ReviewCard as="article" id="review-contribution" label={`Review of ${contributionTitle}`} controlLabel="selected contribution" className="panel task-story-detail" eyebrow={live || recorded ? inheritedAttemptIds.has(selectedReviewAttempt?.id ?? '') ? 'Selected parent-history attempt' : 'Selected current-branch attempt' : 'Contribution to this answer'} title={contributionTitle} summary={live || recorded ? `${reviewTask.title} · ${selectedReviewAttempt ? `attempt ${attemptOrdinal} on this task` : 'no agent attempt recorded'}` : contributors.join(', ') || 'No agent recorded'} actions={<>{selectedReviewAttempt && <AgentBadge name={agentsById.get(selectedReviewAttempt.agentId)?.name} agentId={selectedReviewAttempt.agentId} />}<StatusPill status={selectedReviewAttempt?.status ?? reviewTask.status} /></>} expanded={reviewExpanded.contribution} onToggle={() => toggleReviewCard('contribution')}>
              <ol className="task-stages">
                <li><div><h4>Purpose</h4><p>{reviewTask.latencyBasis}</p>{targetReport && <p className="stage-context">Started from saved report v{targetReport.version}.</p>}</div></li>
                <li><div><h4>Request to another agent</h4><p>{request?.summary || 'No separate agent request was recorded for this task.'}</p>{request && <p className="stage-context">{agentsById.get(request.fromAgentId)?.name || `Agent identity unavailable (${request.fromAgentId})`} asked {agentsById.get(request.toAgentId)?.name || `Agent identity unavailable (${request.toAgentId})`}. Latest request state: {labelStatus(exchange?.requestStatus ?? messages[messages.length - 1].status)}.</p>}{exchange?.deliveredReply && <p className="stage-context"><strong>Saved peer reply · {agentsById.get(exchange.deliveredReply.fromAgentId)?.name || `Agent identity unavailable (${exchange.deliveredReply.fromAgentId})`} → {agentsById.get(exchange.deliveredReply.toAgentId)?.name || `Agent identity unavailable (${exchange.deliveredReply.toAgentId})`}:</strong> {exchange.deliveredReply.summary}</p>}</div></li>
                <li><div><h4>Recorded work</h4>{attempts.length ? <ol className="work-summary-list">{attempts.map(attempt => <li key={attempt.id}><StatusPill status={attempt.status} /><div><strong>{agentsById.get(attempt.agentId)?.name || `Agent identity unavailable (${attempt.agentId})`} · {attemptLineage.get(attempt.id) === 'revised' ? 'updated' : attempts.filter(item => inheritedAttemptIds.has(item.id) === inheritedAttemptIds.has(attempt.id) && item.startedAt <= attempt.startedAt).length === 1 ? 'initial' : 'resumed'} attempt{inheritedAttemptIds.has(attempt.id) ? ' · parent history' : ''}:</strong><RecordedText text={attempt.resultSummary || 'No result summary was saved.'} />{(live || recorded) && <button className="inline-link" onClick={() => setSelectedAttemptId(attempt.id)}>{selectedAttemptId === attempt.id ? 'Selected attempt' : 'Select this attempt'}</button>}<AttemptInspection snapshot={snapshot} attemptId={attempt.id} /></div></li>)}</ol> : <p>No agent attempt was recorded for this task. It remains a task or request record.</p>}</div></li>
                <li><div><h4>{coordinationFixture ? 'Callback observation' : 'Contribution from selected attempt'}</h4>{coordinationFixture ? <RecordedText text={selectedReviewAttempt?.resultSummary ?? 'No callback attempt was recorded for this task.'} /> : snapshot.inspection ? <RecordGroup snapshot={snapshot} title="Accepted contributions" references={snapshot.inspection.attempts.filter(item => attempts.some(attempt => attempt.id === item.attemptId)).flatMap(item => item.acceptedOutputReferences ?? [])} /> : <RecordedText text={output} />}{sourceResult && output !== sourceResult.text && <blockquote className="story-passage"><strong>{linkedSource?.id} · saved passage</strong>{sourceResult.text}</blockquote>}{revisedClaim && <p className="stage-context">Revised claim v{revisedClaim.version}: {revisedClaim.text}</p>}{linkedSource && <button className="inline-link" onClick={() => openSource(linkedSource.id, sourceResult?.spanId)}>Inspect exact source copy <span aria-hidden="true">→</span></button>}</div></li>
              </ol>
              <details className="inline-detail"><summary><span className="closed-label">Expand task details</span><span className="open-label">Hide task details</span></summary><div className="inline-detail-body"><dl><div><dt>Task ID</dt><dd>{reviewTask.id}</dd></div><div><dt>Parent task</dt><dd>{reviewTask.parentTaskId || 'Mission root'}</dd></div><div><dt>First useful result</dt><dd>{formatTime(reviewTask.firstUsefulAt)}</dd></div></dl>{attempts.map(attempt => <div className="inline-attempt" key={attempt.id}><strong>{agentsById.get(attempt.agentId)?.name || attempt.agentId} · {attempt.id}</strong><span>{formatTime(attempt.startedAt)} → {formatTime(attempt.endedAt)}</span><span>{attempt.toolCalls.length ? attempt.toolCalls.map(tool => `${tool.name}: ${labelStatus(tool.status)}`).join('; ') : 'No tool call saved'}</span><span>Returned content: {labelStatus(attempt.returnedReasoning.availability)}</span></div>)}<button className="inline-link" onClick={() => openTask(reviewTask.id)}>Open full technical record in Details →</button></div></details>
            </ReviewCard>}
        </section>
        <ReviewCard id="review-output" label="Current saved output" controlLabel="saved output" className="hero-card" eyebrow="3 · Output" title={coordinationFixture ? 'No scientific report saved' : 'Current saved output'} actions={<><InfoTip label="Saved output" description={coordinationFixture ? 'Callback observations are operational test records. Inspect their exact saved versions in Details; no scientific report was produced.' : 'Read the reported source results, then the saved interpretation and remaining unknowns. Check the saved mission status and evidence before relying on a result.'} /><StatusPill status={snapshot.mission.status} /></>} expanded={reviewExpanded.output} onToggle={() => toggleReviewCard('output')}>
          {!coordinationFixture && snapshot.inspection && <p className="output-limit">{currentReport ? 'This exact output version is recorded as accepted. Acceptance records a host decision; scientific validity requires evidence review.' : 'No accepted report resolves in this saved snapshot. Completed attempts do not establish an accepted answer.'}</p>}
          {coordinationFixture ? <p>Accepted callback observations are operational test records. They do not form a scientific answer. The mission remains {snapshot.mission.status}, with waiting work and an unanswered peer request available in Details and Communication.</p> : <ReportOutput report={currentReport} snapshot={snapshot} onOpenAttempt={openAttempt} sources={snapshot.sources} onOpenSource={openSource} live={!!live || recorded} inspectionSnapshot={snapshot.inspection ? snapshot : undefined} />}
          {!coordinationFixture && snapshot.inspection && <RecordGroup snapshot={snapshot} title="Accepted output records" references={snapshot.inspection.acceptedOutputReferences} />}
        </ReviewCard>
      </div>
    );
  }

  function renderActivity() {
    const available = sampleMode === 'replaying' || sampleMode === 'paused' ? activityItems.slice(0, replayStep) : activityItems;
    const search = activitySearch.trim().toLocaleLowerCase();
    const visible = available.filter(item => {
      if (activityFilter !== 'all' && item.category !== activityFilter) return false;
      if (activityAgentId !== 'all' && item.agentId !== activityAgentId && item.fromAgentId !== activityAgentId && item.toAgentId !== activityAgentId) return false;
      if (!search) return true;
      const actorNames = [item.agentId, item.fromAgentId, item.toAgentId].filter((id): id is string => Boolean(id)).map(id => agentsById.get(id)?.name ?? id);
      const taskTitle = item.taskId ? tasksById.get(item.taskId)?.title ?? '' : '';
      return [item.title, item.summary, item.status ?? '', item.id, taskTitle, ...actorNames].join(' ').toLocaleLowerCase().includes(search);
    });
    const agentIds = [...new Set(activityItems.flatMap(item => [item.agentId, item.fromAgentId, item.toAgentId].filter((id): id is string => Boolean(id))))];
    return <div className="page-stack">
      <SectionHeading eyebrow={recorded ? 'Saved offline activity' : live ? 'Saved live activity' : 'Saved sample trace'} title="Activity" description="Read recorded actions in time order. Each time says whether it marks a start, finish, message, or event. Expand activity details for the named agent, purpose, status explanation, tools, and elapsed time." />
      <div className="trace-boundary"><strong aria-live="polite">{sampleMode === 'replaying' || sampleMode === 'paused' ? `Replaying saved sample · ${replayStep} of ${activityItems.length} records` : `Saved snapshot · ${formatActivityTimestamp(snapshot.capturedAt)}`}</strong><span>{live ? 'The local host is polled for saved updates. This trace contains recorded actions and summaries, not hidden model chain of thought.' : `No live connection or automatic refresh. ${sampleMode === 'replaying' || sampleMode === 'paused' ? 'This is a timed playback of existing records; no agents are running.' : 'This trace contains saved actions and summaries, not hidden model chain of thought.'}`}</span></div>
      {snapshot.inspection && <SavedAgentSteps snapshot={snapshot} />}
      {!live && !recorded && <div className="replay-controls">{sampleMode === 'replaying' ? <button className="outline-button" onClick={() => setSampleMode('paused')}>Pause replay</button> : sampleMode === 'paused' ? <button className="outline-button" onClick={() => setSampleMode('replaying')}>Resume replay</button> : <button className="outline-button" onClick={replaySample}>Replay saved sample from start</button>}{sampleMode === 'replayed' && <span>Replay complete. The full saved review is available.</span>}</div>}
      <div className="activity-filters" role="group" aria-label="Activity filter">{(['all', 'agent', 'communication', 'event'] as const).map(value => <button key={value} className={activityFilter === value ? 'active' : ''} aria-pressed={activityFilter === value} onClick={() => setActivityFilter(value)}>{value === 'all' ? 'All' : value === 'agent' ? 'Agent work' : value === 'communication' ? 'Messages' : 'Host events'}</button>)}</div>
      <div className="activity-search"><label>Agent <select value={activityAgentId} onChange={event => setActivityAgentId(event.target.value)}><option value="all">All agents and host</option>{agentIds.map(id => <option key={id} value={id}>{agentsById.get(id)?.name ? `Agent ${agentsById.get(id)!.name}` : `Agent identity unavailable (${id})`}</option>)}</select></label><label>Search saved activity <input type="search" value={activitySearch} onChange={event => setActivitySearch(event.target.value)} placeholder="Words in saved records" /></label><span aria-live="polite">{visible.length} of {available.length} saved records</span></div>
      {visible.length === 0 && <p className="screen-intro">{available.length === 0 ? 'No activity record has been saved for this run yet.' : 'No saved activity matches these filters. Change the agent, category, or search text.'}</p>}
      <ol className="activity-list">{visible.map(item => <li key={`${item.category}-${item.id}`} className="panel activity-item">
        <ActivityTimestamp item={item} />
        <div><div className="activity-item-head">{(item.agentId || item.fromAgentId) && <AgentBadge name={agentsById.get((item.agentId || item.fromAgentId)!)?.name} agentId={item.agentId || item.fromAgentId} />}<strong>{(() => { const id = item.agentId || item.fromAgentId; if (!id) return item.title; const actor = agentsById.get(id)?.name ?? `Agent identity unavailable (${id})`; return item.title.startsWith(`${actor} · `) ? item.title.slice(actor.length + 3) : item.title.startsWith(`${actor} `) ? item.title.slice(actor.length + 1) : item.title; })()}</strong>{item.status && <StatusPill status={item.status} />}</div>{item.category === 'agent' ? <ActivityAttribution item={item} snapshot={snapshot} /> : <p>{item.summary}</p>}<small>{item.category === 'agent' ? 'Saved attempt summary' : item.category === 'communication' ? 'Saved agent message' : 'Saved host event'}</small>
          <details className="inline-detail"><summary><span className="closed-label">Expand activity details</span><span className="open-label">Hide activity details</span></summary><div className="inline-detail-body">
            <ActivityRecordDetails item={item} snapshot={snapshot} />
            {sampleMode !== 'replaying' && sampleMode !== 'paused' && <div className="request-links">{item.attemptId ? <button className="inline-link" onClick={() => openAttempt(item.attemptId!)}>Open full attempt record →</button> : item.taskId ? <button className="inline-link" onClick={() => openTask(item.taskId!)}>Open full task record →</button> : null}{item.sourceId && <button className="inline-link" onClick={() => openSource(item.sourceId!)}>Open source →</button>}</div>}
          </div></details>
        </div>
      </li>)}</ol>
    </div>;
  }

  function renderCommunication() {
    if (snapshot.inspection?.records.some(record => record.reference.kind === 'mail_request' || record.reference.kind === 'board_proposal')) return <div className="page-stack">
      <SectionHeading eyebrow="Saved coordination" title="Communication" description="Inspect exact proposals, retained critiques and peer requests. Delivery and acknowledgment do not establish that an answer was reviewed." />
      <PeerCoordination snapshot={snapshot} />
      <ContributionReview snapshot={snapshot} />
    </div>;
    const requestIds = [...new Set(snapshot.messages.map(message => message.requestId || message.id))];
    return <div className="page-stack">
      <SectionHeading eyebrow={recorded ? 'Saved offline messages' : live ? 'Saved live messages' : 'Saved sample messages'} title="Communication" description="Each card is one separate request. Read its request and result side by side on a wide window. Expand the card to see how the assigned agent handled that particular request, followed by delivery and response history." />
      <p className="screen-intro">Each card follows one request from its ask to its result. The same agent can receive several different requests.</p>
      {requestIds.length === 0 && <p className="screen-intro">No peer request or reply has been saved for this run yet.</p>}
      <div className="communication-list">{requestIds.map(requestId => {
        const { ordered: history, first, last, fulfilled, deliveredReply, savedResult, requestStatus } = peerExchange(snapshot.messages.filter(message => (message.requestId || message.id) === requestId));
        const relatedTask = tasksById.get(first.taskId);
        const relatedAttempts = snapshot.attempts.filter(attempt => attempt.taskId === first.taskId);
        const linkedSourceId = savedResult?.sourceId ?? last.sourceId;
        const linkedSource = linkedSourceId ? sourcesById.get(linkedSourceId) : undefined;
        const linkedClaimId = savedResult?.claimId ?? last.claimId;
        const linkedClaim = linkedClaimId ? snapshot.claims.find(claim => claim.id === linkedClaimId) : undefined;
        return <article className="panel communication-thread" key={requestId}>
          <div className="panel-heading"><div><span className="eyebrow">Separate request</span><h3>{relatedTask?.title || 'Task request'}</h3></div><StatusPill status={requestStatus} /></div>
          <div className="communication-flow">
            <section aria-label="Request"><h4>Request</h4><div className="communication-person"><span>Requested by</span><AgentBadge name={agentsById.get(first.fromAgentId)?.name} agentId={first.fromAgentId} /></div><p>{first.summary}</p></section>
            <span className="communication-arrow" aria-hidden="true">→</span>
            <section aria-label={savedResult ? 'Result' : 'Current status'}><h4>{savedResult ? 'Result' : 'Current status'}</h4><div className="communication-person"><span>Assigned to</span><AgentBadge name={agentsById.get(first.toAgentId)?.name} agentId={first.toAgentId} /></div><p>{savedResult?.summary ?? last.summary}</p>{deliveredReply && <p className="communication-pending">Reply: {agentsById.get(deliveredReply.fromAgentId)?.name || deliveredReply.fromAgentId} → {agentsById.get(deliveredReply.toAgentId)?.name || deliveredReply.toAgentId}</p>}{!savedResult && <p className="communication-pending">{fulfilled ? 'Request marked fulfilled, but no separate reply content was saved.' : 'No completed response has been recorded.'}</p>}</section>
          </div>
          <details className="communication-history inline-detail"><summary><span className="closed-label">Expand communication and contribution</span><span className="open-label">Hide communication and contribution</span></summary><div className="inline-detail-body">
            <h4>Recorded work for this request</h4>
            <ul className="communication-work">{relatedAttempts.map((attempt, index) => <li key={attempt.id}><StatusPill status={attempt.status} /><div><p><strong>Attempt {index + 1} · {agentsById.get(attempt.agentId)?.name || `Agent name unavailable (${attempt.agentId})`}.</strong> {attempt.resultSummary || 'No summary saved.'}</p><AttemptInspection snapshot={snapshot} attemptId={attempt.id} /></div></li>)}</ul>
            {linkedSource && <p><strong>Source contribution:</strong> {linkedSource.id} · {linkedSource.title}</p>}
            {linkedClaim && <p><strong>Claim revision:</strong> v{linkedClaim.version} · {linkedClaim.text}</p>}
            <h4>Delivery and response history</h4>
            <ol className="communication-status-history">{history.map(message => <li key={message.id}><time dateTime={message.createdAt}>{formatActivityTimestamp(message.createdAt)}</time><StatusPill status={message.status} /><div><p><strong>{agentsById.get(message.fromAgentId)?.name || message.fromAgentId} → {agentsById.get(message.toAgentId)?.name || message.toAgentId}</strong></p><p>{message.summary}</p></div></li>)}</ol>
            <button className="inline-link" onClick={() => openTask(last.taskId)}>Open full technical task record →</button>
          </div></details>
        </article>;
      })}</div>
    </div>;
  }

  function renderWork() {
    const taskAttempts = snapshot.attempts.filter(attempt => attempt.taskId === selectedTask?.id);
    const focusedTaskAttempt = taskAttempts.find(attempt => attempt.id === selectedAttemptId) ?? taskAttempts[0];
    const fulfilledContribution = snapshot.messages.find(message => message.taskId === selectedTask?.id && message.status === 'fulfilled');
    const relatedRequest = latestMessages.find(message => message.taskId === selectedTask?.id);
    return (
      <div className="page-stack">
        <SectionHeading eyebrow={live ? 'Saved live records' : 'Saved sample records'} title="Details" description="Use Task records to inspect exact task IDs, timing, and message states. Use Attempt records to inspect tools and returned-content availability." />
        <div className="details-switch" role="group" aria-label="Details sections"><button className="active" aria-pressed="true">Task records</button><button onClick={() => setScreen('diagnostics')}>Attempt records</button></div>
        <details className="panel run-metadata"><summary title="Open to inspect exact snapshot IDs, versions, and saved time.">Run metadata</summary><dl><div><dt>Snapshot</dt><dd>{snapshot.snapshotId}</dd></div><div><dt>Mission</dt><dd>{snapshot.mission.id} · v{snapshot.mission.version}</dd></div>{live ? <><div><dt>Run</dt><dd>{live.runId}</dd></div><div><dt>Parent snapshot</dt><dd>{snapshot.parentSnapshotId || 'None'}</dd></div><div><dt>Model</dt><dd>{live.model}</dd></div><div><dt>Evidence mode</dt><dd>Supplied or saved evidence; no web search adapter</dd></div></> : <div><dt>Sample packet</dt><dd>v{snapshot.sampleVersion}</dd></div>}<div><dt>Saved</dt><dd>{formatTime(snapshot.capturedAt)}</dd></div></dl><SnapshotDetails snapshot={snapshot} parentSnapshot={comparisonSnapshot} />{recorded ? <p>Saved offline demonstration. These persisted records do not establish live provider execution or scientific validity.</p> : live ? <p>Live model output on supplied evidence. The current snapshot may change as saved work arrives; earlier snapshots remain available through the follow-up comparison.</p> : <><p>This is a simulated saved snapshot using real clinical labels. No research host, provider, model, sandbox, or live event feed is connected. The separate Groq skill experiment is summarized in <code>docs/development/model-fit.md</code>; its actual synthesis and appraisal are not loaded here.</p><label>Show UI state example <select value={previewState} onChange={event => setPreviewState(event.target.value as PreviewState)}>{previewStates.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label></>}</details>
        {snapshot.inspection && <SavedAgentSteps snapshot={snapshot} />}
        {coordinationFixture && snapshot.inspection && <details className="panel"><summary>Saved coordination records ({snapshot.inspection.records.length})</summary><p>Exact input, callback, receipt, proposal, capacity and trace versions. These records describe the offline test, not research findings.</p><RecordGroup snapshot={snapshot} title="Coordination diagnostics" references={snapshot.inspection.records.map(record => record.reference)} /></details>}
        {snapshot.inspection?.records.some(record => record.reference.kind === 'executor_work' || record.reference.kind === 'board_proposal') && <CoordinationWork snapshot={snapshot} />}
        <section className="panel">
          <div className="panel-heading"><div><span className="eyebrow">{live || recorded ? 'Agent attempt timeline' : 'Task timeline'}</span><div className="heading-with-tip"><h3>{live || recorded ? 'Who worked and when' : 'What overlapped'}</h3><InfoTip label="Task timeline" description={live || recorded ? 'Each bar is an actual saved attempt by the named agent. Task and request records without attempts are listed separately.' : 'Select a task row to inspect its exact record below. Bars show saved attempt times, not a live scheduler.'} /></div></div></div>
          {live || recorded ? <RecordedWorkTimeline snapshot={snapshot} parentSnapshot={comparisonSnapshot} includeParentHistory={includeParentHistory} onToggleParentHistory={() => setIncludeParentHistory(value => !value)} selectedAttemptId={selectedAttemptId} onSelectAttempt={(attemptId, taskId) => { setSelectedAttemptId(attemptId); setSelectedTaskId(taskId); }} onSelectTask={taskId => setSelectedTaskId(taskId)} /> : <div className="work-grid" aria-label="Task timeline">
            {snapshot.tasks.map((task, index) => (
              <button key={task.id} className={`work-row ${selectedTask?.id === task.id ? 'selected' : ''}`} onClick={() => setSelectedTaskId(task.id)}>
                <span className="work-number">{String(index + 1).padStart(2, '0')}</span>
                <span className="work-description"><strong>{task.title}</strong><small>{task.latencyClass.replace('_', ' ')} · {task.activeOrWaiting} · {task.latencyBasis}</small></span>
                <span className="work-track" aria-hidden="true"><span className={`work-bar work-bar-${task.status}`} style={taskBarStyle(task)} /></span>
                <StatusPill status={task.status} />
              </button>
            ))}
          </div>}
        </section>
        <div className="two-column work-detail-grid">
          <section className="panel" id="selected-task">
            <div className="panel-heading"><div><span className="eyebrow">Selected task</span><div className="heading-with-tip"><h3>{selectedTask?.title ?? 'No task selected'}</h3><InfoTip label="Selected task" description="Read the chosen task's IDs and latest agent request here. Select an attempt below to inspect its tools and returned content." /></div></div>{selectedTask && <StatusPill status={selectedTask.status} />}</div>
            {selectedTask && <>
              <div className="detail-list">
                <div><span>Task</span><strong>{selectedTask.id}</strong></div>
                <div><span>Parent</span><strong>{selectedTask.parentTaskId || 'Mission root'}</strong></div>
                {selectedTask.targetArtifactId && <div><span>Starting artifact</span><strong>{selectedTask.targetArtifactId}</strong></div>}
                <div><span>First useful artifact</span><strong>{formatTime(selectedTask.firstUsefulAt)}</strong></div>
                <div><span>Current mode</span><strong>{selectedTask.activeOrWaiting}</strong></div>
              </div>
              <p className="detail-note">{selectedTask.status === 'waiting' ? 'This task is waiting for a permitted source route. The answer already supported by abstracts remains available.' : 'The selected task can be inspected without hiding sibling progress.'}</p>
              {relatedRequest && <div className="task-contribution"><strong>Related agent request · {relatedRequest.requestId || relatedRequest.id}</strong><p>{relatedRequest.summary}</p><StatusPill status={relatedRequest.status} />{fulfilledContribution && <div className="request-links">{fulfilledContribution.sourceId && <button className="inline-link" onClick={() => openSource(fulfilledContribution.sourceId!)}>Inspect {fulfilledContribution.sourceId}</button>}{fulfilledContribution.claimId && <button className="inline-link" onClick={() => openClaim(fulfilledContribution.claimId!)}>Inspect revised claim</button>}</div>}</div>}
              <h4>Attempts</h4>
              <div className="attempt-stack">
                {taskAttempts.map((attempt, index) => <button key={attempt.id} className={`attempt-item ${focusedTaskAttempt?.id === attempt.id && (live || recorded) ? 'selected' : ''}`} onClick={() => live || recorded ? setSelectedAttemptId(attempt.id) : openAttempt(attempt.id)}><span><strong>{live || recorded ? `${agentsById.get(attempt.agentId)?.name || `Agent identity unavailable (${attempt.agentId})`} · attempt ${index + 1}` : attempt.id}</strong><small>{live || recorded ? `${attempt.id} · ` : `${agentsById.get(attempt.agentId)?.name || attempt.agentId} · `}{formatTime(attempt.startedAt)}</small></span><StatusPill status={attempt.status} /></button>)}
              </div>
              {(live || recorded) && focusedTaskAttempt && <div className="task-contribution"><strong>{agentsById.get(focusedTaskAttempt.agentId)?.name || `Agent identity unavailable (${focusedTaskAttempt.agentId})`} · selected actual attempt</strong><p>{focusedTaskAttempt.resultSummary || 'No result summary saved.'}</p><p>Recorded actions: {focusedTaskAttempt.toolCalls.map(tool => `${tool.name} (${labelStatus(tool.status)})`).join(' → ') || 'No tool action saved.'}</p><p>Started {formatActivityTimestamp(focusedTaskAttempt.startedAt)} · {focusedTaskAttempt.endedAt ? `ended ${formatActivityTimestamp(focusedTaskAttempt.endedAt)}` : 'end time not recorded'}</p><button className="inline-link" onClick={() => openAttempt(focusedTaskAttempt.id)}>Open full attempt record and calls →</button></div>}
            </>}
          </section>
          <section className="panel">
            <div className="panel-heading"><div><span className="eyebrow">Message status records</span><div className="heading-with-tip"><h3>Agent requests</h3><InfoTip label="Agent request records" description="Use Communication for the plain-language exchange. Here, inspect exact request IDs and whether each request was delivered, acknowledged, or fulfilled." /></div></div></div>
            <div className="request-stack">
              {latestMessages.map(message => {
                const history = snapshot.messages.filter(item => (item.requestId || item.id) === (message.requestId || message.id));
                return <article key={message.requestId || message.id} className="request-card">
                  <div className="request-head"><strong>{message.requestId || message.id}</strong><StatusPill status={message.status} /></div>
                  <p>{message.summary}</p>
                  <div className="request-people">{agentsById.get(message.fromAgentId)?.name || message.fromAgentId} <span aria-hidden="true">→</span> {agentsById.get(message.toAgentId)?.name || message.toAgentId}</div>
                  <div className="request-steps" aria-label={`Status history for ${message.requestId || message.id}`}>
                    {['requested', 'delivered', 'acknowledged', 'fulfilled'].map(status => <span key={status} className={history.some(item => item.status === status) ? 'reached' : ''}>{status}</span>)}
                  </div>
                  <div className="request-links"><button className="inline-link" onClick={() => openTask(message.taskId)}>Open task</button>{message.sourceId && <button className="inline-link" onClick={() => openSource(message.sourceId!)}>Source contribution</button>}{message.claimId && <button className="inline-link" onClick={() => openClaim(message.claimId!)}>Claim revision</button>}</div>
                </article>;
              })}
            </div>
          </section>
        </div>
        <section className="panel">
          <div className="panel-heading"><div><span className="eyebrow">Serial event trail</span><div className="heading-with-tip"><h3>Accepted steps and their effect</h3><InfoTip label="Event trail" description="Read saved events in time order, including their recorded agent when available. Use Open task to inspect the related record." /></div></div></div>
          <div className="diagnostic-events">{snapshot.events.map(event => { const display = eventDisplay(event, agentsById); return <article key={event.id}><span className="event-time">{formatTime(event.time)}</span><div><strong>{display.title}</strong><p>{display.summary}</p></div>{event.taskId && <button className="inline-link" onClick={() => openTask(event.taskId!)}>Open task</button>}</article>; })}</div>
        </section>
      </div>
    );
  }

  function renderEvidence() {
    return (
      <div className="page-stack">
        <SectionHeading eyebrow="Review" title="Evidence" description="Select a source, read its saved passages, and open a claim version to check the exact citations behind it." />
        <div className="evidence-grid">
          <section className="panel source-list-panel">
            <div className="panel-heading"><div><span className="eyebrow">Complete source list</span><div className="heading-with-tip"><h3>{snapshot.sources.length} saved source records</h3><InfoTip label="Source list" description="Select a source to view the exact saved copy, access state, and retained passages." /></div></div></div>
            <div className="source-list">
              {snapshot.sources.map(source => <button key={source.id} className={`source-list-item ${selectedSource?.id === source.id ? 'selected' : ''}`} onClick={() => { setSelectedSourceId(source.id); setSelectedSpanId(null); }}><span className="source-letter">{source.id}</span><span><strong>{source.title}</strong><small>{labelStatus(source.accessState)} · {labelStatus(source.retrievalStatus)}</small></span></button>)}
            </div>
          </section>
          <section className="panel source-detail-panel">
            {selectedSource ? <>
              <div className="panel-heading"><div><span className="eyebrow">Source copy {selectedSource.copyId} · v{selectedSource.version}</span><div className="heading-with-tip"><h3>{selectedSource.title}</h3><InfoTip label="Source copy" description="Read the passage and locator together. A highlighted passage is the one opened from a citation; check its exact copy version before relying on it." /></div></div><StatusPill status={selectedSource.accessState} /></div>
              <p className="source-meta">{selectedSource.provider || (recorded ? 'Provider not recorded' : live ? 'Supplied evidence' : 'Sample packet')}{selectedSource.url ? ` · ${selectedSource.url}` : ''} · {labelStatus(selectedSource.retrievalStatus)}</p>
              <div className="passage-list">
                {selectedSource.passages.map(passage => <article key={passage.spanId} className={`passage ${selectedSpanId === passage.spanId ? 'highlighted' : ''}`}><div><span className="eyebrow">{passage.spanId}</span><span>{passage.locator}</span></div><blockquote>{passage.text}</blockquote></article>)}
                {selectedSource.passages.length === 0 && <p>No retained passage is available. A locator or access notice does not establish missing details.</p>}
              </div>
              <SourceInspection snapshot={snapshot} sourceId={selectedSource.id} /><p className="tiny-note">Hash {selectedSource.contentHash || 'not retained'} · {recorded ? 'Saved offline evidence record.' : live ? 'Saved supplied evidence; no live web retrieval.' : 'Scripted source record, not a live retrieval.'}</p>
            </> : <p>The requested source is not available in this snapshot.</p>}
          </section>
        </div>
        <section className="panel">
          <div className="panel-heading"><div><span className="eyebrow">Claim history</span><div className="heading-with-tip"><h3>Revisions keep their evidence</h3><InfoTip label="Claim history" description="Select a version, read its wording, then open a citation to check the supporting saved passage." /></div></div></div>
          <div className="claim-tabs" role="group" aria-label="Claim versions">
            {snapshot.claims.map(claim => <button key={claim.id} className={selectedClaim?.id === claim.id ? 'active' : ''} onClick={() => setSelectedClaimId(claim.id)}>Version {claim.version}</button>)}
          </div>
          {selectedClaim && <div className="claim-detail"><div><StatusPill status={selectedClaim.disposition} /><span className="subtle-label">{selectedClaim.id}{selectedClaim.supersedesVersion ? ` · supersedes v${selectedClaim.supersedesVersion}` : ''}</span></div><p>{selectedClaim.text}</p><div className="citation-row">{selectedClaim.supports.map(support => <button key={`${support.sourceId}-${support.spanId}`} className="citation-button" onClick={() => openSource(support.sourceId, support.spanId)}>{support.sourceId} · {support.spanId} <span aria-hidden="true">↗</span></button>)}</div></div>}
        </section>
        <section className="panel">
          <div className="panel-heading"><div><span className="eyebrow">Reviewer annotation</span><div className="heading-with-tip"><h3>Note a source or claim question</h3><InfoTip label="Reviewer note" description="Select a source and claim version, type a question, then save a local note. It disappears when this view reloads." /></div></div></div>
          <p>Attach a review note to the selected source copy and claim version. Notes are local to this open view.</p>
          <label className="field-label" htmlFor="annotation">Your note about {selectedSource?.id ?? 'the source'} and claim v{selectedClaim?.version ?? '—'}</label>
          <textarea id="annotation" rows={3} value={annotationText} onChange={event => setAnnotationText(event.target.value)} placeholder="For example: confirm whether the interval uses the same outcome definition." />
          <button className="primary-button annotation-save" disabled={!annotationText.trim()} onClick={() => { setAnnotations(previous => [...previous, { id: previous.length + 1, target: `${selectedSource?.copyId ?? 'no-copy'} · claim v${selectedClaim?.version ?? '—'}`, text: annotationText.trim() }]); setAnnotationText(''); }}>Save local {live ? 'review' : 'sample'} note</button>
          {annotations.length > 0 && <div className="annotation-list" aria-label={live ? 'Local review notes' : 'Local sample notes'}>{annotations.map(note => <article key={note.id}><span className="eyebrow">Evidence · {snapshot.mission.id} v{snapshot.mission.version} · {note.target}{live ? '' : ` · sample ${snapshot.sampleVersion}`}</span><p>{note.text}</p></article>)}</div>}
        </section>
      </div>
    );
  }

  function renderDiagnostics() {
    return (
      <div className="page-stack">
        <SectionHeading eyebrow={live ? 'Saved live records' : 'Saved sample records'} title="Details" description="Choose an attempt to inspect exact timing, tool status, faults, and returned-content availability." />
        <div className="details-switch" role="group" aria-label="Details sections"><button onClick={() => setScreen('work')}>Task records</button><button className="active" aria-pressed="true">Attempt records</button></div>
        {snapshot.inspection && <SavedAgentSteps snapshot={snapshot} />}
        <div className="diagnostic-layout">
          <section className="panel">
            <div className="panel-heading"><div><span className="eyebrow">Attempt history</span><div className="heading-with-tip"><h3>Inspect an exact attempt</h3><InfoTip label="Attempt history" description="Each task can have several attempts. Select one to see its saved result, tool status, and returned-content availability." /></div></div></div>
            <div className="attempt-stack">
              {snapshot.attempts.map(attempt => <button key={attempt.id} className={`attempt-item ${selectedAttempt?.id === attempt.id ? 'selected' : ''}`} onClick={() => setSelectedAttemptId(attempt.id)}><span><strong>{attempt.id}</strong><small>{tasksById.get(attempt.taskId)?.title || attempt.taskId}</small></span><StatusPill status={attempt.status} /></button>)}
            </div>
          </section>
          <section className="panel">
            {selectedAttempt ? <>
              <div className="panel-heading"><div><span className="eyebrow">{selectedAttempt.id} · {selectedAttempt.taskId}</span><div className="heading-with-tip"><h3>{agentsById.get(selectedAttempt.agentId)?.name || selectedAttempt.agentId}</h3><InfoTip label="Selected attempt" description="Read this attempt's summary, timing, tools, and returned-content status. Use the related-task link to return to its task record." /></div></div><StatusPill status={selectedAttempt.status} /></div>
              <p>{selectedAttempt.resultSummary || 'No result summary was saved for this attempt.'}</p>
              <button className="inline-link" onClick={() => openTask(selectedAttempt.taskId)}>Open related task and agent request <span aria-hidden="true">→</span></button>
              <div className="detail-list"><div><span>Started</span><strong>{formatTime(selectedAttempt.startedAt)}</strong></div><div><span>Ended</span><strong>{formatTime(selectedAttempt.endedAt)}</strong></div><div><span>Selected model</span><strong>{selectedAttempt.selectedModel || (live || recorded ? 'Not recorded' : 'None · scripted fixture')}</strong></div><div><span>Skill versions</span><strong>{selectedAttempt.skillVersions.length ? selectedAttempt.skillVersions.join(', ') : live || recorded ? 'Not recorded' : 'None in this fixture'}</strong></div></div>
              <AttemptInspection snapshot={snapshot} attemptId={selectedAttempt.id} />
              {snapshot.inspection && <ExecutionDiagnostics snapshot={snapshot} attemptId={selectedAttempt.id} />}<h4>{live || recorded ? 'Tools called in this attempt' : 'Tools and providers'}</h4>
              <div className="tool-stack">{selectedAttempt.toolCalls.map((tool, index) => <div key={`${tool.name}-${index}`}><span><strong>{tool.name}</strong><small>{tool.provider || (recorded ? 'Provider not recorded' : live ? 'Local host' : 'Local scripted fixture')}</small></span><StatusPill status={tool.status} /></div>)}{selectedAttempt.toolCalls.length === 0 && <p>No tool call was saved for this attempt.</p>}</div>
              <div className="heading-with-tip"><h4>{live || recorded ? 'Provider-returned reasoning' : 'Returned model content'}</h4><InfoTip label={live || recorded ? 'Provider-returned reasoning' : 'Returned model content'} description="Request configuration, response availability and access permission are separate facts. Missing text here does not establish that reasoning was never requested." /></div>
              <ReasoningView reasoning={selectedAttempt.returnedReasoning} live={!!live || recorded} snapshot={snapshot} attemptId={selectedAttempt.id} />
              {live && <LiveCallInspector runId={live.runId} attemptId={selectedAttempt.id} snapshotId={snapshot.snapshotId} snapshot={snapshot} onOpenAttempt={openAttempt} />}
            </> : <p>No attempt selected.</p>}
          </section>
        </div>
        <section className="panel">
          <div className="panel-heading"><div><span className="eyebrow">Operational events</span><div className="heading-with-tip"><h3>Failures are explicit</h3><InfoTip label="Operational events" description="Scan faults and waiting states here, then use a Task link to inspect the affected work. These are not scientific findings." /></div></div></div>
          <div className="diagnostic-events">{snapshot.diagnostics.map(item => <article key={item.id}><span className="event-time">{formatTime(item.occurredAt)}</span><div><strong>{item.kind} · {labelStatus(item.status)}</strong><p>{item.message}</p></div>{item.taskId && <button className="inline-link" onClick={() => openTask(item.taskId!)}>Task</button>}</article>)}</div>
        </section>
      </div>
    );
  }

  function renderRequest() {
    if (live) return live.form;
    if (recorded) return <div className="page-stack"><SectionHeading eyebrow="Tasks" title="Start or follow up" description="Choose the live workspace for a new task or a follow-up on saved work." /><LiveWorkspaceAccess view="request" /></div>;
    const targetClaim = snapshot.claims.find(claim => claim.id === requestTarget);
    const targetTask = snapshot.tasks.find(task => task.id === requestTarget);
    const targetArtifact = snapshot.artifacts.find(artifact => artifact.id === requestTarget);
    const targetLabel = targetClaim ? `Claim ${targetClaim.id}, version ${targetClaim.version}` : targetTask ? `Task ${targetTask.id}` : targetArtifact ? `Report ${targetArtifact.id}, version ${targetArtifact.version}` : 'Mission root';
    const hasRecordedPath = requestKind === 'follow_up' && requestTarget === 'report-v1';
    return (
      <div className="page-stack">
        <SectionHeading eyebrow="Review" title="Request preview" description="Draft a new question or follow up on an exact saved version. This form only simulates a queue receipt." />
        <div className="two-column request-layout">
          <section className="panel">
            <div className="heading-with-tip"><h3>Draft a request</h3><InfoTip label="Draft a request" description="Choose a new mission or an exact saved claim, task, or report. Nothing is sent to a backend." /></div>
            <div className="segmented-control" role="group" aria-label="Request type"><button className={requestKind === 'new' ? 'active' : ''} onClick={() => { setRequestKind('new'); setReceipt(false); }}>New mission</button><button className={requestKind === 'follow_up' ? 'active' : ''} onClick={() => { setRequestKind('follow_up'); setReceipt(false); }}>Follow up on exact work</button></div>
            <label className="field-label" htmlFor="request-question">Research question or follow-up</label>
            <textarea id="request-question" rows={4} value={requestQuestion} onChange={event => { setRequestQuestion(event.target.value); setReceipt(false); }} />
            <label className="field-label" htmlFor="request-scope">Scope and constraints</label>
            <textarea id="request-scope" rows={4} value={requestScope} onChange={event => { setRequestScope(event.target.value); setReceipt(false); }} />
            {requestKind === 'follow_up' && <><label className="field-label" htmlFor="request-target">Exact saved target</label><select id="request-target" value={requestTarget} onChange={event => { setRequestTarget(event.target.value); setReceipt(false); }}>{snapshot.claims.map(claim => <option key={claim.id} value={claim.id}>Claim {claim.id} · v{claim.version}</option>)}{snapshot.tasks.map(task => <option key={task.id} value={task.id}>Task {task.id}</option>)}{snapshot.artifacts.filter(artifact => artifact.kind === 'report').map(artifact => <option key={artifact.id} value={artifact.id}>Report {artifact.id} · v{artifact.version}</option>)}</select></>}
            <button className="primary-button submit-button" disabled={!requestQuestion.trim()} onClick={() => setReceipt(true)}>Preview simulated queue receipt <span aria-hidden="true">→</span></button>
          </section>
          <section className="panel preview-panel">
            <span className="eyebrow">Scope-change preview</span><div className="heading-with-tip"><h3>{requestKind === 'new' ? 'A separate mission' : 'A linked follow-up'}</h3><InfoTip label="Scope preview" description="Check the proposed starting point and question, then preview a simulated receipt. No actual task is queued." /></div>
            <div className="preview-flow"><div><span>Starting point</span><strong>{requestKind === 'new' ? 'No parent artifact' : targetLabel}</strong></div><span aria-hidden="true">→</span><div><span>Proposed work</span><strong>{requestQuestion || 'Enter a question'}</strong></div></div>
            <p><strong>What changes:</strong> {requestKind === 'new' ? 'A new mission would be created with this scope.' : 'A child task would reference the exact selected version; the accepted parent finding stays available.'}</p>
            <p><strong>What stays uncertain:</strong> The requested source may remain unavailable. The reviewer can still inspect supported partial findings.</p>
            {receipt && <div className="receipt-card" role="status"><span className="eyebrow">Simulated receipt · not submitted</span><strong>SIM-QUEUE-001 · queued at this preview point</strong><p>{requestKind === 'new' ? 'New sample mission' : `Child of ${targetLabel}`}. No backend call or model work occurred.</p>{hasRecordedPath ? <><p>The fixed sample then follows task-a2: one retrieval route fails, a second finds A2, and report v2 narrows the claim.</p><button className="inline-link" onClick={() => openTask('task-a2')}>Inspect recorded child task and attempts</button></> : <p>No later attempt or outcome is scripted for this selected target.</p>}</div>}
          </section>
        </div>
      </div>
    );
  }

  function renderHistory() {
    if (recorded) return <div className="page-stack"><SectionHeading eyebrow="Saved work" title="Previous tasks" description="Live runs and recorded demonstrations remain separate." /><LiveWorkspaceAccess view="history" /><section className="panel"><h3>{coordinationFixture ? 'Recorded Stage 3 coordination review' : 'Recorded Stage 2 review'}</h3><p>{snapshot.mission.question}</p><button className="outline-button" onClick={() => setScreen('overview')}>Open recorded review</button>{comparisonSnapshot && <button className="outline-button" onClick={() => setScreen('compare')}>Compare with its saved parent</button>}<p><a className="inline-link" href={coordinationFixture ? '?recorded=stage2' : '?recorded=stage3'}>{coordinationFixture ? 'Open recorded Stage 2 review' : 'Open recorded Stage 3 coordination review'}</a></p><p><a className="inline-link" href="/">Open bundled examples</a></p></section></div>;
    if (live) return <div className="page-stack"><SectionHeading eyebrow="Durable local history" title="Saved runs" description="Select a saved run to inspect its current authoritative snapshot." />{live.runs.length === 0 && <p className="screen-intro">No run has been saved yet.</p>}<div className="communication-list">{live.runs.map(run => <article className="panel saved-sample" key={run.id}><h3>{run.question}</h3><p>{run.id} · {run.status}{run.parentRunId ? ` · follows ${run.parentRunId}` : ''}</p><button className="outline-button" onClick={() => live.onSelectRun(run.id)}>Open saved run</button></article>)}</div><section className="panel" aria-label="Recorded coordination examples"><h3>Recorded coordination examples</h3><p>Inspect saved offline tests of peer communication and contribution review. These examples are separate from your live runs; opening them does not start or stop research.</p><p><a className="inline-link" href="/?recorded=stage3&review=contributions">Open shared contribution review</a></p><p><a className="inline-link" href="/?recorded=stage3">Open recorded Stage 3 coordination review</a></p></section></div>;
    return <div className="page-stack">
      <SectionHeading eyebrow="Bundled test data" title="Previous samples" description="Open the saved sample after clearing the active review, or replay its recorded activity from the beginning." />
      <a className="outline-button" href="?recorded=stage2">Open recorded Stage 2 review</a><a className="outline-button" href="?recorded=stage3">Open recorded Stage 3 coordination review</a><a className="outline-button" href="?recorded=stage3&review=contributions">Open shared contribution review</a><p className="screen-intro">This prototype contains bundled sample snapshots only. It does not store or list actual prior agent runs.</p>
      <article className="panel saved-sample"><div className="panel-heading"><div><span className="eyebrow">Research sample</span><h3>{snapshot.mission.question}</h3></div><StatusPill status={sampleMode === 'cleared' ? 'saved' : 'active'} /></div><p>Saved {formatTime(snapshot.capturedAt)} · {snapshot.tasks.length} task records · {snapshot.agents.length} named agents</p><div className="saved-sample-actions"><button className="primary-button" onClick={loadSample}>Load into active review</button><button className="outline-button" onClick={replaySample}>Replay saved activity</button></div><details className="inline-detail"><summary><span className="closed-label">Expand sample details</span><span className="open-label">Hide sample details</span></summary><div className="inline-detail-body"><p>Snapshot {snapshot.snapshotId}. The comparison baseline is another saved sample snapshot; neither is a live or previously executed provider run.</p></div></details></article>
    </div>;
  }

  function renderCompare() {
    if (!comparisonSnapshot) return <div className="state-card"><h2>{live || recorded ? 'No parent snapshot to compare' : 'Second sample run unavailable'}</h2><p>A comparison needs two exact saved snapshots. No difference is inferred from one view.</p></div>;
    const earlierRun = comparisonSnapshot;
    const earlier = acceptedReport(earlierRun);
    const current = currentReport;
    const comparedRuns = [
      { run: earlierRun, report: earlier, label: live || recorded ? 'Immutable parent snapshot' : 'Baseline sample run' },
      { run: snapshot, report: current, label: live || recorded ? 'Current follow-up snapshot' : 'Current sample run' },
    ];
    return (
      <div className="page-stack">
        <SectionHeading eyebrow="Review" title="Compare" description="Read the baseline and current reports side by side, then compare source counts, citations, and limitations before interpreting the change." />
        <div className="two-column comparison-grid">
          {comparedRuns.map(({ run, report, label }) => <section className="panel comparison-panel" key={run.snapshotId}><span className="eyebrow">{label}</span><div className="heading-with-tip"><h3>Report v{report?.version || '—'}</h3><InfoTip label={label} description="Compare exact saved snapshots, source coverage, and limits before interpreting the report difference." /></div><p className="comparison-meta">{run.mission.id} · {run.sources.length} source{run.sources.length === 1 ? '' : 's'} · saved {formatTime(run.capturedAt)} · snapshot {run.snapshotId}</p><p>{report?.body || 'No saved report for this run.'}</p><div className="comparison-citations">{report?.citations.map(citation => <CitationDetails key={`${citation.sourceId}-${citation.spanId}`} snapshot={run} sourceId={citation.sourceId} spanId={citation.spanId} />)}</div><div className="limitations"><strong>Limitations</strong><p>{report?.limitations.join(' ') || 'None saved.'}</p></div></section>)}
        </div>
        {live || recorded ? <section className="panel comparison-explainer"><h3>Interpret the difference</h3><p>The left snapshot is the saved parent baseline. The right snapshot is the current follow-up state. Compare exact wording and citations; a version change alone does not establish scientific improvement.</p></section> : <section className="panel comparison-explainer"><div className="heading-with-tip"><h3>Interpret the difference</h3><InfoTip label="Comparison" description="Read this explanation, then open the current claim and sources. Added source coverage explains the scripted change; it is not a model-quality result." /></div><p>The baseline run had only A1 and an open request for another adult comparison. The current run also has A2, so its report can describe mixed evidence. Different source coverage explains this change; the display does not show a better model or a treatment effect. Unavailable methods remain open.</p><button className="text-button" onClick={() => openClaim('claim-benefit-v2')}>Inspect current claim and sources <span aria-hidden="true">→</span></button></section>}
      </div>
    );
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark" aria-hidden="true">✳</span><div><strong>Research Atlas</strong><small>{recorded ? 'Saved research review' : live ? 'Local live research' : 'Review prototype'}</small></div></div>
        <nav aria-label="Primary navigation">{activeNavigation.map((item, index) => {
          const active = screen === item.id || (screen === 'diagnostics' && item.id === 'work');
          const disabled = !live && item.group === 'current' && item.id !== 'overview' && ((sampleMode === 'cleared') || ((sampleMode === 'replaying' || sampleMode === 'paused') && item.id !== 'activity'));
          return <div key={item.id} className="nav-entry">{(index === 0 || activeNavigation[index - 1].group !== item.group) && <div className={`sidebar-section-label ${index > 0 ? 'sidebar-divider' : ''}`}>{item.group === 'current' ? 'Current task' : item.group === 'start' ? 'Start or follow up' : live ? 'Saved runs' : recorded ? 'Previous tasks' : 'Saved examples'}</div>}<div className={`nav-row ${active ? 'active' : ''}`}><button aria-current={active ? 'page' : undefined} className="nav-item" disabled={disabled} onClick={() => setScreen(item.id)}><span className="nav-indicator" /><strong>{item.label}</strong></button><InfoTip label={`${item.label} view`} description={item.description} /></div></div>;
        })}</nav>
      </aside>
      <div className="workspace">
        <header className="topbar"><div><span className="breadcrumb">{currentNav.label}</span><h1>{sampleMode === 'cleared' && screen === 'overview' ? 'No task in the active review' : screen === 'overview' ? 'One research task, from input to output' : snapshot.mission.question}</h1></div><div className="topbar-actions">{recorded && <a className="outline-button" href="/">Back to samples</a>}<span className="sample-tag">{recorded ? 'RECORDED OFFLINE TEST' : live ? 'LIVE · SUPPLIED EVIDENCE' : 'SCRIPTED SAMPLE'}</span>{!live && !recorded && sampleMode !== 'cleared' && <button className="outline-button" onClick={clearReview}>Clear review</button>}</div></header>
        <div className="context-bar"><span className="context-summary">{recorded ? `Saved offline records · ${snapshot.inspection?.timingBasis === 'controlled_clock' ? 'controlled test times, not measured latency' : 'no live updates'}` : live ? `Run ${live.runId ?? 'unknown'} · ${snapshot.mission.status} · no web search adapter` : sampleMode === 'cleared' ? 'Active review cleared · bundled sample available in Previous samples' : sampleMode === 'replaying' || sampleMode === 'paused' ? 'Saved sample replay · no live agents or backend' : 'Saved sample · no live updates'}</span>{live && <span className="live-freshness"><span>Last saved progress: <time dateTime={snapshot.capturedAt}>{formatActivityTimestamp(snapshot.capturedAt)}</time></span><span>Viewer last checked: {live.lastCheckedAt ? <time dateTime={live.lastCheckedAt}>{formatActivityTimestamp(live.lastCheckedAt)}</time> : 'Not yet checked'}</span></span>}</div>
        {live?.connectionError && <div className="live-error" role="alert">Viewer refresh failed: {live.connectionError}. Showing the last saved progress; current host state is unknown.</div>}
        {live?.error && <div className="live-error" role="alert">Live status: {live.error}. Inspect the saved snapshot and Details for the affected work.</div>}
        <main id="main-content">{!live && sampleMode === 'cleared' && screen !== 'history' && screen !== 'request' ? <div className="state-card"><h2>Active review cleared</h2><p>The bundled sample is available under Previous samples. Clearing only changes this local view.</p><button className="primary-button" onClick={() => setScreen('history')}>Open Previous samples</button></div> : !live && (sampleMode === 'replaying' || sampleMode === 'paused') && screen !== 'activity' && screen !== 'history' && screen !== 'request' ? <div className="state-card"><h2>Sample replay in progress</h2><p>{replayStep} of {activityItems.length} saved records shown. No agents are running.</p><button className="primary-button" onClick={() => setScreen('activity')}>Watch Activity</button></div> : <>{!live && <PreviewNotice state={previewState} capturedAt={snapshot.capturedAt} />}{!live && previewState === 'loading' ? <div className="state-card" role="status"><div className="loading-mark" /><h2>Loading a saved snapshot</h2><p>In this preview, select “Normal sample” to inspect the fixed data. No network request is in progress.</p></div> : !live && previewState === 'empty' ? <div className="state-card"><h2>No mission selected</h2><p>There is no run to review in this preview state. Start from a scoped question or switch back to the sample mission.</p><button className="primary-button" onClick={() => setScreen('request')}>Preview a new mission</button></div> : screen === 'overview' ? renderOverview() : screen === 'activity' ? renderActivity() : screen === 'communication' ? renderCommunication() : screen === 'work' ? renderWork() : screen === 'evidence' ? renderEvidence() : screen === 'diagnostics' ? renderDiagnostics() : screen === 'request' ? renderRequest() : screen === 'history' ? renderHistory() : renderCompare()}</>}</main>
      </div>
    </div>
  );
}

export default function App() {
  if (window.location.pathname === '/live') return <LiveResearchApp />;
  const parameters = new URLSearchParams(window.location.search);
  const recorded = parameters.get('recorded');
  return <SampleApp recorded={recorded === 'stage2' || recorded === 'stage3' ? recorded : undefined} contributionReview={recorded === 'stage3' && parameters.get('review') === 'contributions'} />;
}

function SampleApp({ recorded, contributionReview = false }: { recorded?: 'stage2' | 'stage3'; contributionReview?: boolean }) {
  const [data, setData] = useState<{ snapshot: ResearchSnapshot; baseline?: ResearchSnapshot } | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    const paths = recorded === 'stage3' ? [contributionReview ? '/stage3-contributions/snapshot.json' : '/stage3-recorded/snapshot.json'] : recorded === 'stage2' ? ['/stage2-recorded/child.json', '/stage2-recorded/parent.json'] : ['/sample-run.json', '/sample-run-baseline.json'];
    Promise.all(paths.map(path =>
      fetch(path, { signal: controller.signal }).then(response => {
        if (!response.ok) throw new Error(`Sample data could not be loaded (${response.status}).`);
        return response.json() as Promise<ResearchSnapshot>;
      }),
    ))
      .then(([snapshot, baseline]) => {
        if (recorded === 'stage2' && (snapshot.proofKind !== 'offline_test' || baseline?.proofKind !== 'offline_test' || !snapshot.inspection || !baseline.inspection || snapshot.parentSnapshotId !== baseline.snapshotId)) throw new Error('The recorded parent and child do not form a valid offline inspection pair.');
        if (recorded === 'stage3' && (snapshot.proofKind !== 'offline_test' || snapshot.sampleVersion !== (contributionReview ? 'stage3-contribution-review-1' : 'stage3-offline-1') || !snapshot.inspection || snapshot.artifacts.length || snapshot.claims.length || snapshot.mission.reportArtifactId != null || snapshot.inspection.acceptedOutputReferences.length)) throw new Error('The Stage 3 fixture must contain offline coordination records without a scientific report.');
        setData({ snapshot, baseline });
      })
      .catch(cause => { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : 'Sample data could not be loaded.'); });
    return () => controller.abort();
  }, [recorded, contributionReview]);
  if (error) return <div className="startup-state"><span className="sample-tag">{recorded ? 'RECORDED OFFLINE TEST' : 'SAMPLE PROTOTYPE'}</span><h1>{recorded ? 'Recorded data unavailable' : 'Sample data unavailable'}</h1><p>{error}</p><p>Check that the bundled fixture is present, then relaunch the prototype.</p></div>;
  if (!data) return <div className="startup-state" role="status"><span className="sample-tag">{recorded ? 'RECORDED OFFLINE TEST' : 'SAMPLE PROTOTYPE'}</span><h1>{recorded ? `Loading the saved Stage ${recorded === 'stage3' ? '3' : '2'} review` : 'Loading the saved sample'}</h1><p>No live research backend is contacted.</p></div>;
  return <ResearchApp snapshot={data.snapshot} comparisonSnapshot={data.baseline} />;
}
