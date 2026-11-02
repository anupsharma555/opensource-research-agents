/* Review a saved research run through its findings, evidence and agent work.
   Shared views serve the live host and recorded examples; exact records remain
   separate from research conclusions, and sample previews submit no work. */
import { useEffect, useMemo, useRef, useState } from 'react';
import type { CSSProperties, ReactNode } from 'react';
import type { ResearchSnapshot } from './generated/contracts';
import { ContributionTimeline, RecordedWorkTimeline } from './ContributionTimeline';
import { classifyAttemptLineage, workStepNumbers } from './attemptLineage';
import { TaskSourceData } from './TaskSourceData';
import { ReviewCard } from './ReviewCard';
import { ReportOutput } from './ReportOutput';
import { acceptedReport, AttemptInspection, RecordGroup, SnapshotDetails } from './InspectionDetails';
import { ActivityRecordDetails, ActivityTimestamp, formatActivityTimestamp } from './ActivityRecordDetails';
import { useActivityFilters } from './activityFilters';
import type { ActivityItem } from './ActivityRecordDetails';
import { describeLiveAttempt, recordedToolAction, showsOriginalRunningRecord, toolActionLabel } from './attemptOutcome';
import { activityAction, activityOverview, activityScanSummary, hasRecordedIssue } from './activityPresentation';
import { LiveResearchApp } from './LiveResearchApp';
import { LiveCallInspector } from './LiveCallInspector';
import { LiveAgentReasoning } from './LiveAgentReasoning';
import { AgentBadge } from './AgentBadge';
import { SavedAgentSteps } from './SavedAgentSteps';
import { CoordinationWork } from './CoordinationWork';
import { PeerCoordination } from './PeerCoordination';
import { ContributionReview } from './ContributionReview';
import { ExecutionDiagnostics, getReasoningAvailability } from './ExecutionDiagnostics';
import { RecordedText } from './RecordedText';
import { LiveWorkspaceAccess } from './LiveWorkspaceAccess';
import { SavedRuns } from './SavedRuns';
import { SelectedWorkStep } from './SelectedWorkStep';
import { EvidenceBrowser } from './EvidenceBrowser';
import { ReportComparison } from './ReportComparison';
import { TermHelp, AtlasTerms } from './TermHelp';
import { groupActivity, exactActivityParent, taskIdentity, assignTaskNumbers } from './activityGroups';
import { RunStatusSummary } from './RunStatusSummary';
import { RunWorkSummary } from './RunWorkSummary';
import { workGroupLabel } from './WorkOverview';
import { ActivityWorkOverview } from './ActivityWorkOverview';
import { stepPresentation } from './stepPresentation';
import { invoke, isTauri } from '@tauri-apps/api/core';
import type { LiveRunSummary } from './live';
import './styles.css';

export type Screen = 'overview' | 'activity' | 'communication' | 'work' | 'evidence' | 'diagnostics' | 'request' | 'compare' | 'history';
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

function ActivityAttribution({ item, snapshot, compactSummary }: { item: ActivityItem; snapshot: ResearchSnapshot; compactSummary: string }) {
  const attempt = snapshot.attempts.find(record => record.id === item.attemptId);
  if (!attempt) return <p>{item.summary}</p>;
  const task = snapshot.tasks.find(record => record.id === attempt.taskId);
  const outcome = describeLiveAttempt(snapshot, attempt);
  const resultText = attempt.resultSummary || outcome?.summary || 'No result summary was saved.';
  const repeatedDerivedSummary = !attempt.resultSummary && resultText.trim() === compactSummary.trim();
  return <div className="activity-attribution">
    <details className="inline-detail"><summary>Task assignment</summary><p>{task?.title ?? 'Task title unavailable'}</p></details>
    <section><h5>{outcome?.label ?? (attempt.status === 'completed' ? 'Saved result' : 'Saved attempt outcome')}</h5><div className="activity-attribution-meta"><AgentBadge name={snapshot.agents.find(agent => agent.id === attempt.agentId)?.name} agentId={attempt.agentId} /><span>{outcome?.responseStatus === 'tool_requested' ? 'model turn recorded' : `${attempt.status} attempt`}</span><time dateTime={attempt.endedAt || attempt.startedAt}>{formatActivityTimestamp(attempt.endedAt || attempt.startedAt)}</time></div>{!repeatedDerivedSummary && <RecordedText text={resultText} />}</section>
  </div>;
}

const navigation: { id: Screen; label: string; description: string; group: 'current' | 'start' | 'saved' }[] = [
  { id: 'request', label: 'Start or follow up', description: 'Draft a sample request and preview its scope. Nothing is submitted.', group: 'start' },
  { id: 'overview', label: 'Research review', description: 'Start here. Read the input, agent contributions, and saved output in order.', group: 'current' },
  { id: 'communication', label: 'Communication', description: 'Read which named agent requested what from whom, then expand the recorded exchange.', group: 'current' },
  { id: 'evidence', label: 'Evidence', description: 'Select a source or citation to check the saved passage behind an output.', group: 'current' },
  { id: 'compare', label: 'Compare', description: 'Read two saved sample reports side by side and check their different source coverage.', group: 'current' },
  { id: 'activity', label: 'Activity', description: 'Read saved agent actions in time order, or replay the scripted sample. No live agent connection exists.', group: 'current' },
  { id: 'work', label: 'Details', description: 'Choose a task to inspect its exact status, agent attempts, and run metadata.', group: 'current' },
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
  if (status === 'tool_recovery_needed') return 'Result needs recovery';
  return status.replaceAll('_', ' ');
}

function formatTime(value?: string | null) {
  if (!value) return 'Not yet recorded';
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }).format(date);
}

function compareAttemptStart(a: Attempt, b: Attempt) {
  const elapsed = Date.parse(a.startedAt) - Date.parse(b.startedAt);
  return (Number.isFinite(elapsed) ? elapsed : a.startedAt.localeCompare(b.startedAt)) || a.id.localeCompare(b.id);
}

function savedAttemptState(attempt: Attempt, snapshot: ResearchSnapshot) {
  const inspection = snapshot.inspection?.attempts.find(item => item.attemptId === attempt.id);
  return JSON.stringify({ status: attempt.status, endedAt: attempt.endedAt, resultSummary: attempt.resultSummary,
    toolCalls: attempt.toolCalls, returnedReasoning: attempt.returnedReasoning,
    callReferences: inspection?.callReferences, acceptedOutputReferences: inspection?.acceptedOutputReferences,
    explanation: inspection?.explanation });
}

function StatusPill({ status, label }: { status: string; label?: string }) {
  return <span className={`status-pill status-${status.replaceAll('_', '-')}`}>{label || labelStatus(status)}</span>;
}

function InfoTip({ label, description }: { label: string; description: string }) {
  return <span className="info-tip"><button type="button" className="info-tip-trigger" aria-label={`How to use ${label}: ${description}`}>i</button><span className="info-tip-content" role="tooltip"><strong>How to use</strong>{description}</span></span>;
}

function SectionHeading({ eyebrow, title, description, visiblePurpose = false }: { eyebrow: string; title: string; description?: string; visiblePurpose?: boolean }) {
  return (
    <div className="section-heading">
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <div className="heading-with-tip"><h2>{title}</h2>{description && !visiblePurpose && <InfoTip label={title} description={description} />}</div>
        {visiblePurpose && description && <p className="review-card-summary">{description}</p>}
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
  requiredAgentIds?: string[] | null;
  participationPolicyVersion?: number | null;
  onSelectRun: (runId: string) => void;
  lastCheckedAt?: string | null;
  viewerMetrics?: { reads: number; failures: number; cancellations: number; lastDurationMs: number | null; totalDurationMs: number; commands: number; commandErrors: number; lastCommandDurationMs: number | null; mode: 'push' | 'polling' };
  connectionError?: string | null;
  workspaceId?: string | null;
  workspaceLabel?: string | null;
  retrievalRoutes?: string[];
  agentNames?: Record<string, string>;
}

export function ResearchApp({ snapshot, comparisonSnapshot, live, initialScreen = 'overview', onScreenChange }: { snapshot: ResearchSnapshot; comparisonSnapshot?: ResearchSnapshot; live?: LiveView; initialScreen?: Screen; onScreenChange?: (screen: Screen) => void }) {
  const recorded = snapshot.proofKind === 'offline_test';
  useEffect(() => {
    const mode = recorded ? 'recorded' : live ? 'live' : 'sample';
    document.title = `Research Atlas · ${recorded ? 'Recorded offline test' : live ? 'Live local research' : 'Sample prototype'}`;
    if (isTauri()) void invoke('set_atlas_view_title', { mode }).catch(() => {});
  }, [recorded, !!live]);
  const coordinationFixture = recorded && ['stage3-offline-1', 'stage3-contribution-review-1'].includes(snapshot.sampleVersion);
  const attemptLineage = classifyAttemptLineage(snapshot, exactActivityParent(snapshot, comparisonSnapshot));
  const inheritedAttemptIds = new Set(snapshot.attempts.filter(attempt => attemptLineage.get(attempt.id) === 'inherited').map(attempt => attempt.id));
  const currentBranchAttempts = snapshot.attempts.filter(attempt => !inheritedAttemptIds.has(attempt.id)).sort(compareAttemptStart);
  const firstCurrentAttempt = live && currentBranchAttempts.length > 8 ? currentBranchAttempts[currentBranchAttempts.length - 1] : currentBranchAttempts[0];
  const runIdentityKey = live?.runId ?? snapshot.snapshotId;
  const taskNumbering = useRef({ run: runIdentityKey, numbers: assignTaskNumbers(snapshot) });
  if (taskNumbering.current.run !== runIdentityKey) taskNumbering.current = { run: runIdentityKey, numbers: assignTaskNumbers(snapshot) };
  else taskNumbering.current.numbers = assignTaskNumbers(snapshot, taskNumbering.current.numbers);
  const identifyTask = (taskId: string) => taskIdentity(snapshot, taskId, taskNumbering.current.numbers);
  const selectionKey = `${live?.runId ?? snapshot.snapshotId}:${comparisonSnapshot?.snapshotId ?? ''}`;
  const initializedSelectionKey = useRef<string | null>(null);
  const [localScreen, setLocalScreen] = useState<Screen>(initialScreen);
  const screen = onScreenChange ? initialScreen : localScreen;
  const setScreen = onScreenChange ?? setLocalScreen;
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
  const sourceScope = live?.runId || snapshot.snapshotId;
  const [sourceSelection, setSourceSelection] = useState({ scope: sourceScope, sourceId: snapshot.sources[0]?.id ?? '', spanId: null as string | null, explicit: false });
  const sourceSelectionCurrent = sourceSelection.scope === sourceScope;
  const selectedSourceId = sourceSelectionCurrent && (sourceSelection.explicit || sourceSelection.sourceId) ? sourceSelection.sourceId : snapshot.sources[0]?.id ?? '';
  const selectedSpanId = sourceSelectionCurrent ? sourceSelection.spanId : null;
  const [selectedClaimId, setSelectedClaimId] = useState(snapshot.claims[0]?.id ?? '');
  const [selectedAttemptId, setSelectedAttemptId] = useState((live || recorded ? firstCurrentAttempt?.id : undefined) ?? snapshot.attempts[0]?.id ?? '');
  const [includeParentHistory, setIncludeParentHistory] = useState(false);
  const [reviewTimelineOpen, setReviewTimelineOpen] = useState(false);
  const [inspectAttemptId, setInspectAttemptId] = useState<string | undefined>();
  const { order: activityOrder, setOrder: setActivityOrder, filter: activityFilter, setFilter: setActivityFilter, agentId: activityAgentId, setAgentId: setActivityAgentId, search: activitySearch, setSearch: setActivitySearch, attentionOnly: activityAttentionOnly, setAttentionOnly: setActivityAttentionOnly, reset: resetActivityFilters } = useActivityFilters();
  const [activityWorkGroup, setActivityWorkGroup] = useState<{ label: string; attemptIds: string[] } | null>(null);
  const [activityGroupNotice, setActivityGroupNotice] = useState<string | null>(null);
  const [expandedActivityId, setExpandedActivityId] = useState<string | null>(null);
  const [followLatest, setFollowLatest] = useState(false);
  const [followTarget, setFollowTarget] = useState<{ runId: string; snapshotId: string; ids: string[] } | null>(null);
  const seenLiveAttempts = useRef<{ runId: string; snapshotId: string; states: Map<string, string> } | null>(null);
  const [liveAttemptChanges, setLiveAttemptChanges] = useState<{ runId: string; added: string[]; updated: string[] }>({ runId: '', added: [], updated: [] });
  const [jumpToTask, setJumpToTask] = useState(false);
  const [jumpToAttempt, setJumpToAttempt] = useState(false);
  const [jumpToActivityAttempt, setJumpToActivityAttempt] = useState<string | null>(null);
  const [jumpToSource, setJumpToSource] = useState(false);
  const [jumpToReview, setJumpToReview] = useState(false);
  const [reasoningAttemptId, setReasoningAttemptId] = useState<string | null>(null);
  const [taskOrigin, setTaskOrigin] = useState<Screen | null>(null);
  const [taskReference, setTaskReference] = useState<{ requestId?: string; version?: number } | null>(null);
  const [requestKind, setRequestKind] = useState<'new' | 'follow_up'>('follow_up');
  const [requestQuestion, setRequestQuestion] = useState('Find an independent adult comparison before revising the earlier report.');
  const [requestScope, setRequestScope] = useState('Use permitted abstracts and exact passages. Keep the earlier report available while the source check runs.');
  const [requestTarget, setRequestTarget] = useState('report-v1');
  const [receipt, setReceipt] = useState(false);
  const [annotationState, setAnnotationState] = useState<{ scope: string; text: string; notes: { id: number; target: string; text: string }[] }>({ scope: sourceScope, text: '', notes: [] });
  const annotationText = annotationState.scope === sourceScope ? annotationState.text : '';
  const annotations = annotationState.scope === sourceScope ? annotationState.notes : [];

  useEffect(() => {
    if ((!live && !recorded) || initializedSelectionKey.current === selectionKey) return;
    initializedSelectionKey.current = selectionKey;
    setSelectedTaskId(firstCurrentAttempt?.taskId ?? snapshot.tasks[0]?.id ?? '');
    setSelectedReviewTaskId(firstCurrentAttempt?.taskId ?? snapshot.tasks[0]?.id ?? '');
    setSelectedAttemptId(firstCurrentAttempt?.id ?? '');
    setIncludeParentHistory(false);
    setReviewTimelineOpen(false);
    setInspectAttemptId(undefined);
    setReasoningAttemptId(null);
  }, [live, recorded, selectionKey, firstCurrentAttempt?.id, snapshot.tasks]);

  useEffect(() => {
    resetActivityFilters();
    setActivityWorkGroup(null);
    setActivityGroupNotice(null);
    setActivityAttentionOnly(false);
    setExpandedActivityId(null);
    setFollowLatest(false);
    setFollowTarget(null);
  }, [selectionKey, resetActivityFilters]);

  useEffect(() => {
    if (!activityWorkGroup) return;
    const selected = snapshot.attempts.filter(attempt => activityWorkGroup.attemptIds.includes(attempt.id));
    if (selected.some(attempt => workGroupLabel(snapshot, attempt) !== activityWorkGroup.label)) {
      setActivityWorkGroup(null);
      setActivityGroupNotice('A selected step changed its action type. Showing all work steps so you can follow the update.');
      setActivityFilter('agent');
      setActivityAgentId('all');
      setActivitySearch('');
      setActivityAttentionOnly(false);
    }
  }, [snapshot.snapshotId, activityWorkGroup]);

  useEffect(() => {
    if (screen !== 'activity') {
      setFollowLatest(false);
      setFollowTarget(null);
    }
  }, [screen]);

  useEffect(() => {
    if (!live?.runId) return;
    const states = new Map(currentBranchAttempts.map(attempt => [attempt.id, savedAttemptState(attempt, snapshot)]));
    const previous = seenLiveAttempts.current;
    if (!previous || previous.runId !== live.runId) {
      seenLiveAttempts.current = { runId: live.runId, snapshotId: snapshot.snapshotId, states };
      setLiveAttemptChanges({ runId: live.runId, added: [], updated: [] });
      return;
    }
    if (previous.snapshotId === snapshot.snapshotId) return;
    const added = [...states.keys()].filter(id => !previous.states.has(id));
    const updated = [...states].filter(([id, state]) => previous.states.has(id) && previous.states.get(id) !== state).map(([id]) => id);
    seenLiveAttempts.current = { runId: live.runId, snapshotId: snapshot.snapshotId, states };
    if (added.length || updated.length) setLiveAttemptChanges(current => ({
      runId: live.runId!,
      added: [...new Set([...(current.runId === live.runId ? current.added : []), ...added])],
      updated: [...new Set([...(current.runId === live.runId ? current.updated : []), ...updated])]
        .filter(id => !added.includes(id) && !(current.runId === live.runId && current.added.includes(id))),
    }));
    if (followLatest && screen === 'activity' && (added.length || updated.length)) {
      setFollowTarget({ runId: live.runId, snapshotId: snapshot.snapshotId, ids: [...added, ...updated] });
    }
  }, [live?.runId, snapshot.snapshotId, followLatest, screen]);

  useEffect(() => {
    if (!followLatest || screen !== 'activity' || !followTarget || followTarget.runId !== live?.runId
      || followTarget.snapshotId !== snapshot.snapshotId) return;
    const ids = new Set(followTarget.ids);
    const rows = [...document.querySelectorAll<HTMLElement>('.activity-list > li[data-attempt-id]')];
    const latestVisible = rows.reverse().find(row => ids.has(row.dataset.attemptId ?? ''));
    latestVisible?.scrollIntoView?.({ block: 'nearest' });
    setFollowTarget(null);
  }, [followLatest, followTarget, screen, live?.runId, snapshot.snapshotId]);

  useEffect(() => {
    window.scrollTo(0, 0);
  }, [screen]);

  useEffect(() => {
    if (screen !== 'work' || !jumpToTask) return;
    const destination = document.getElementById('selected-task');
    destination?.scrollIntoView?.({ block: 'start' });
    destination?.focus();
    setJumpToTask(false);
  }, [screen, selectedTaskId, jumpToTask]);

  useEffect(() => {
    if (screen !== 'diagnostics' || !jumpToAttempt) return;
    const destination = document.getElementById('selected-attempt');
    destination?.scrollIntoView?.({ block: 'start' });
    destination?.focus();
    setJumpToAttempt(false);
  }, [screen, selectedAttemptId, jumpToAttempt]);

  useEffect(() => {
    if (screen !== 'activity' || !jumpToActivityAttempt) return;
    const row = [...document.querySelectorAll<HTMLElement>('.activity-item[data-attempt-id]')]
      .find(item => item.dataset.attemptId === jumpToActivityAttempt);
    row?.scrollIntoView?.({ block: 'start' });
    row?.querySelector<HTMLButtonElement>('.activity-expand')?.focus();
    setJumpToActivityAttempt(null);
  }, [screen, jumpToActivityAttempt, expandedActivityId]);

  useEffect(() => {
    if (screen !== 'evidence' || !jumpToSource) return;
    const destination = document.getElementById(selectedSourceId && snapshot.sources.some(source => source.id === selectedSourceId) ? 'selected-source-heading' : 'selected-source-panel');
    destination?.scrollIntoView?.({ block: 'start' });
    destination?.focus();
    setJumpToSource(false);
  }, [screen, selectedSourceId, jumpToSource, snapshot.sources]);

  useEffect(() => {
    if (screen !== 'overview' || !jumpToReview) return;
    const destination = document.getElementById('review-contribution');
    destination?.scrollIntoView?.({ block: 'start' });
    destination?.focus();
    setJumpToReview(false);
  }, [screen, selectedAttemptId, selectedReviewTaskId, jumpToReview]);

  const agentsById = useMemo(() => new Map(snapshot.agents.map(agent => [agent.id, agent])), [snapshot.agents]);
  const tasksById = useMemo(() => new Map(snapshot.tasks.map(task => [task.id, task])), [snapshot.tasks]);
  const sourcesById = useMemo(() => new Map(snapshot.sources.map(source => [source.id, source])), [snapshot.sources]);
  const stepNumbers = workStepNumbers(snapshot, inheritedAttemptIds);
  const currentReport = acceptedReport(snapshot);
  const selectedTask = tasksById.get(selectedTaskId);
  const selectedSource = sourcesById.get(selectedSourceId);
  const selectedClaim = snapshot.claims.find(claim => claim.id === selectedClaimId) ?? snapshot.claims[0];
  const selectedAttempt = snapshot.attempts.find(attempt => attempt.id === selectedAttemptId);
  const selectedOutcome = live && selectedAttempt ? describeLiveAttempt(snapshot, selectedAttempt) : null;
  const savedContinuingIds = live?.runs.find(run => run.id === live.runId)?.continuingTaskIds;
  const continuingTasks = live && snapshot.mission.status === 'completed'
    ? [...new Map(snapshot.tasks
      .filter(task => ['queued', 'running', 'waiting'].includes(task.status)
        && (savedContinuingIds == null || savedContinuingIds.includes(task.id)))
      .map(task => [task.id, task])).values()]
    : [];
  const currentTaskCount = snapshot.tasks.filter(task => ['queued', 'running', 'waiting'].includes(task.status)).length;
  const currentSavedTasks = live ? snapshot.tasks.filter(task => ['queued', 'running', 'waiting'].includes(task.status)) : [];
  const activeNavigation = recorded ? navigation.map(item => ({ ...item, label: item.id === 'history' ? live ? 'Saved runs' : 'Previous tasks' : item.label, description: item.id === 'request' ? live ? 'Submit a request to this connected offline test workspace; no model calls occur.' : 'Open the live workspace to start or follow up on a real task.' : item.id === 'history' ? 'Access previous live tasks or the bundled recorded reviews.' : item.id === 'compare' ? 'Compare the immutable parent and follow-up, including their own saved citations.' : item.id === 'activity' ? 'Inspect saved events, attempts and messages. Recorded test times are not live provider latency.' : item.description })) : live ? navigation.map(item => item.id === 'history' ? { ...item, label: 'Saved runs', description: 'Open an actual saved local run.' } : item.id === 'request' ? { ...item, description: 'Submit a real start or exact saved follow-up.' } : item.id === 'activity' ? { ...item, description: 'Read saved agent actions refreshed from the local live host.' } : item.id === 'compare' ? { ...item, description: 'Compare the immutable parent with the current follow-up snapshot.' } : item) : navigation;
  const currentNav = activeNavigation.find(item => item.id === screen) ?? activeNavigation.find(item => item.id === 'work')!;
  const latestMessages = Array.from(
    [...snapshot.messages].sort((a, b) => a.createdAt.localeCompare(b.createdAt))
      .reduce((map, message) => map.set(message.requestId || message.id, message), new Map<string, ResearchSnapshot['messages'][number]>()).values(),
  );
  const activityItems: ActivityItem[] = [
    ...snapshot.events.map(event => ({ id: event.id, time: event.time, timeLabel: 'Event recorded', category: 'event' as const, ...eventDisplay(event, agentsById), taskId: event.taskId || undefined, agentId: event.agentId })),
    ...snapshot.attempts.map(attempt => {
      const outcome = live ? describeLiveAttempt(snapshot, attempt) : null;
      return { id: attempt.id, time: attempt.endedAt || attempt.startedAt, timeLabel: attempt.endedAt ? 'Step finished' : 'Step started', category: 'agent' as const, title: `${agentsById.get(attempt.agentId)?.name || `Agent identity unavailable (${attempt.agentId})`} · ${outcome?.label ?? (attempt.status === 'completed' ? 'saved result' : `${attempt.status} attempt`)}`, summary: attempt.resultSummary || outcome?.summary || 'No result summary was saved.', taskId: attempt.taskId, attemptId: attempt.id, agentId: attempt.agentId, status: attempt.status, badgeStatus: outcome?.badgeStatus };
    }),
    ...snapshot.messages.map(message => {
      const exchange = peerExchange(snapshot.messages.filter(item => (item.requestId || item.id) === (message.requestId || message.id)));
      const from = agentsById.get(message.fromAgentId)?.name || `Agent identity unavailable (${message.fromAgentId})`;
      const to = agentsById.get(message.toAgentId)?.name || `Agent identity unavailable (${message.toAgentId})`;
      const isReply = exchange.deliveredReply?.id === message.id;
      const isAnswer = message.id !== exchange.first.id && ['awaiting_review', 'fulfilled'].includes(message.status);
      return { id: message.id, time: message.createdAt, timeLabel: isReply ? 'Reply delivered' : isAnswer ? 'Answer record saved' : message.id === exchange.first.id ? 'Request recorded' : 'Message recorded', category: 'communication' as const, title: isReply ? `${from} responded to ${to}` : message.id === exchange.first.id ? `${from} asked ${to}` : `${from} → ${to} · ${message.status}`, summary: message.summary, taskId: message.taskId, sourceId: message.sourceId || undefined, fromAgentId: message.fromAgentId, toAgentId: message.toAgentId, status: message.status };
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
    setSourceSelection({ scope: sourceScope, sourceId, spanId: spanId ?? null, explicit: true });
    setJumpToSource(true);
    setScreen('evidence');
  }

  function openReviewActivity(group: string | null = null, attemptId?: string) {
    setActivityWorkGroup(group ? { label: group, attemptIds: snapshot.attempts.filter(attempt => !inheritedAttemptIds.has(attempt.id) && workGroupLabel(snapshot, attempt) === group).map(attempt => attempt.id) } : null);
    setActivityGroupNotice(null);
    setActivityFilter('agent');
    resetActivityFilters();
    setActivityAttentionOnly(false);
    setActivityOrder('time');
    const exactAttempt = attemptId ? snapshot.attempts.find(attempt => attempt.id === attemptId && !inheritedAttemptIds.has(attempt.id) && (!group || workGroupLabel(snapshot, attempt) === group)) : undefined;
    setExpandedActivityId(exactAttempt ? `agent:${exactAttempt.id}` : null);
    setJumpToActivityAttempt(exactAttempt?.id ?? null);
    if (exactAttempt) {
      setSelectedAttemptId(exactAttempt.id);
      setSelectedReviewTaskId(exactAttempt.taskId);
    }
    setScreen('activity');
  }

  function openTask(taskId: string, context?: { requestId?: string; version?: number }) {
    if (screen !== 'work' && screen !== 'diagnostics') setTaskOrigin(screen);
    setTaskReference(context ?? null);
    setSelectedTaskId(taskId);
    const attempts = snapshot.attempts.filter(item => item.taskId === taskId);
    if (attempts.length && !attempts.some(item => item.id === selectedAttemptId)) setSelectedAttemptId(attempts[0].id);
    if (!attempts.length) setSelectedAttemptId('');
    setJumpToTask(true);
    setScreen('work');
  }

  function openClaim(claimId: string) {
    setSelectedClaimId(claimId);
    setScreen('evidence');
  }

  function acknowledgeAttempt(attemptId: string) {
    if (live?.runId) setLiveAttemptChanges(current => current.runId === live.runId
      ? { ...current, added: current.added.filter(id => id !== attemptId), updated: current.updated.filter(id => id !== attemptId) } : current);
  }

  function openAttempt(attemptId: string) {
    acknowledgeAttempt(attemptId);
    setSelectedAttemptId(attemptId);
    setJumpToAttempt(true);
    setScreen('diagnostics');
  }

  function openModelReasoning(attemptId: string) {
    setReasoningAttemptId(attemptId);
    setScreen('communication');
  }

  function toggleReviewCard(section: ReviewSection) {
    setReviewExpanded(current => ({ ...current, [section]: !current[section] }));
  }

  function renderOverview() {
    const savedRequestedNames = live?.runs.find(run => run.id === live.runId)?.requestedAgentNames;
    const requestedPeerName = (id: string) => savedRequestedNames?.[id] || `Name not recorded (${id})`;
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
    const attempts = snapshot.attempts.filter(attempt => attempt.taskId === reviewTask?.id).sort(compareAttemptStart);
    const selectedReviewAttempt = attempts.find(attempt => attempt.id === selectedAttemptId) ?? attempts[0];
    const reviewOutcome = selectedReviewAttempt ? describeLiveAttempt(snapshot, selectedReviewAttempt) : null;
    const condensedReview = (live || recorded) && attempts.length > 8;
    const otherAttempts = attempts.filter(attempt => attempt.id !== selectedReviewAttempt?.id);
    const attemptStatusSummary = [...new Set(attempts.map(attempt => attempt.status))]
      .map(status => `${attempts.filter(attempt => attempt.status === status).length} ${labelStatus(status)}`).join(' · ');
    const attemptOrdinal = selectedReviewAttempt ? stepNumbers.get(selectedReviewAttempt.id) ?? 0 : 0;
    const selectedAgentName = selectedReviewAttempt ? agentsById.get(selectedReviewAttempt.agentId)?.name ?? `Agent identity unavailable (${selectedReviewAttempt.agentId})` : null;
    const contributionTitle = live || recorded ? selectedReviewAttempt ? `${selectedAgentName} · work step ${attemptOrdinal}` : `${reviewTask?.title ?? 'Task'} · request record` : reviewTask?.title ?? 'Task';
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
    const runScopeKnown = !snapshot.parentSnapshotId || Boolean(exactActivityParent(snapshot, comparisonSnapshot));
    const currentAgents = [...new Set(snapshot.attempts.filter(attempt => !inheritedAttemptIds.has(attempt.id)).map(attempt => agentsById.get(attempt.agentId)?.name || attempt.agentId))].join(', ');
    return (
      <div className="page-stack">
        <div className="review-toolbar"><SectionHeading eyebrow={recorded ? 'Saved offline demonstration' : live ? 'Saved live run' : 'Scripted sample'} title="Research review" description="See the original request, saved work and current output together." visiblePurpose /><div className="details-switch"><button className="outline-button" onClick={() => setScreen('communication')}>Next: Communication →</button><button className="outline-button" aria-label={allCollapsed ? 'Expand all cards' : 'Collapse all cards'} onClick={() => setReviewExpanded({ input: allCollapsed, work: allCollapsed, contribution: allCollapsed, output: allCollapsed })}>{allCollapsed ? 'Expand all' : 'Collapse all'}</button></div></div>
        {coordinationFixture && <section className="panel" aria-label="Offline coordination fixture"><h3>Saved Stage 3 coordination test</h3><p>{snapshot.attempts.filter(attempt => attempt.status === 'completed').length} completed local callbacks · {snapshot.tasks.filter(task => task.status === 'waiting').length} waiting task · mission {snapshot.mission.status}.</p><p>These are recorded callback observations, not model inference or scientific findings. The saved peer question remains open; no provider or research service is connected to this view.</p></section>}
        <ReviewCard id="review-input" label="Research task input" controlLabel="input research task" className="panel mission-input" eyebrow="1 · Request" title={snapshot.parentSnapshotId ? 'Original request for this follow-up' : 'Original research request'} expanded={reviewExpanded.input} onToggle={() => toggleReviewCard('input')}><blockquote className="original-research-request">{snapshot.mission.question}</blockquote><details className="inline-detail"><summary>Additional instructions</summary><p>{snapshot.mission.scope || 'No additional instructions saved.'}</p></details>{coordinationFixture ? <p>No study sources were supplied. The synthetic input is inspectable below.</p> : <TaskSourceData snapshot={snapshot} onOpenSource={openSource} />}{coordinationFixture && snapshot.inspection && <RecordGroup snapshot={snapshot} title="Saved synthetic inputs" references={snapshot.inspection.attempts.flatMap(attempt => attempt.inputReferences ?? [])} />}<SnapshotDetails snapshot={snapshot} parentSnapshot={comparisonSnapshot} /></ReviewCard>
        <section className="task-review" aria-label="Task from input to output">
          <ReviewCard id="review-work" label="Agent contributions" controlLabel="agent contributions" eyebrow="2 · Work and exchanges" title={coordinationFixture ? 'Recorded callback activity' : 'What has happened so far'} expanded={reviewExpanded.work} onToggle={() => toggleReviewCard('work')}>
            {live || recorded ? <>
              <RunWorkSummary key={selectionKey} runScopeKnown={runScopeKnown} snapshot={snapshot} earlierAttemptIds={inheritedAttemptIds} taskLabel={id => identifyTask(id).label} onActivity={() => openReviewActivity()} onOpenGroup={openReviewActivity} />
              <details className="inline-detail"><summary>Participating agents and completion requirements</summary><p>{runScopeKnown ? 'Agents with work in this run' : 'Agents named in this saved view'}: {currentAgents || 'None yet.'}</p>{live && <p>Requested starting peers: {live.requestedAgentIds ? live.requestedAgentIds.map(requestedPeerName).join(', ') : 'Not recorded'}. Required contributions: {live.requiredAgentIds?.length ? live.requiredAgentIds.map(requestedPeerName).join(', ') : 'None selected or not recorded'}.</p>}</details>
            </> : <ContributionTimeline snapshot={snapshot} selectedTaskId={reviewTask?.id} onSelect={taskId => { setSelectedReviewTaskId(taskId); setReviewExpanded(current => ({ ...current, contribution: true })); setJumpToReview(true); }} />}
          </ReviewCard>
            {reviewTask && !(live || recorded) && <ReviewCard as="article" id="review-contribution" label={`Review of ${contributionTitle}`} controlLabel="selected contribution" className="panel task-story-detail" eyebrow={`${identifyTask(reviewTask.id).label} · ${inheritedAttemptIds.has(selectedReviewAttempt?.id ?? '') ? 'Earlier run · selected work step' : 'Selected contribution'}`} title={contributionTitle} summary={live || recorded ? `${selectedReviewAttempt ? `Step ${attemptOrdinal} in ${inheritedAttemptIds.has(selectedReviewAttempt.id) ? 'the earlier run' : 'this run'}` : 'No agent attempt recorded'} · ${attempts.length} saved steps` : contributors.join(', ') || 'No agent recorded'} actions={<>{selectedReviewAttempt && <AgentBadge name={agentsById.get(selectedReviewAttempt.agentId)?.name} agentId={selectedReviewAttempt.agentId} />}{selectedReviewAttempt && <span className="subtle-label">Selected work step <TermHelp term="work step" /></span>}<StatusPill status={reviewOutcome?.badgeStatus ?? selectedReviewAttempt?.status ?? reviewTask.status} /></>} expanded={reviewExpanded.contribution} onToggle={() => toggleReviewCard('contribution')}>
              {<>
              <ol className="task-stages">
                <li><div><h4>Purpose</h4><p>{reviewTask.title}</p><p className="stage-context"><strong>Current task status:</strong> {labelStatus(reviewTask.status)} in this saved snapshot. The selected attempt above has its own status and time.</p><p className="stage-context"><strong>Timing expectation:</strong> {reviewTask.latencyClass ? `${reviewTask.latencyClass.replaceAll("_", " ")} · ` : "Not yet estimated · "}{reviewTask.latencyBasis}</p>{reviewTask.status === "waiting" && <p className="stage-context"><strong>Current task wait:</strong> {reviewTask.waitingReason || "Reason not recorded in this snapshot"}</p>}{reviewTask.status === 'waiting' && reviewTask.waitingOperationReference && <p className="stage-context"><strong>Next eligible model attempt:</strong> {reviewTask.waitingUntil ? formatActivityTimestamp(reviewTask.waitingUntil) : 'Time not recorded'}. Dispatch may occur later.</p>}{targetReport && <p className="stage-context">Started from saved report v{targetReport.version}.</p>}</div></li>
                <li><div><h4>Request to another agent</h4><p>{request?.summary || 'No separate agent request was recorded for this task.'}</p>{request && <p className="stage-context">{agentsById.get(request.fromAgentId)?.name || `Agent identity unavailable (${request.fromAgentId})`} asked {agentsById.get(request.toAgentId)?.name || `Agent identity unavailable (${request.toAgentId})`}. Latest request state: {labelStatus(exchange?.requestStatus ?? messages[messages.length - 1].status)}.</p>}{exchange?.deliveredReply && <p className="stage-context"><strong>Saved peer reply · {agentsById.get(exchange.deliveredReply.fromAgentId)?.name || `Agent identity unavailable (${exchange.deliveredReply.fromAgentId})`} → {agentsById.get(exchange.deliveredReply.toAgentId)?.name || `Agent identity unavailable (${exchange.deliveredReply.toAgentId})`}:</strong> {exchange.deliveredReply.summary}</p>}</div></li>
                <li><div><h4>Recorded work</h4>{condensedReview ? <>
                  <p className="stage-context">{attempts.length} saved attempts for this task · {attemptStatusSummary}. The selected attempt is shown here; other saved attempts remain available below and in Details.</p>
                  {selectedReviewAttempt && <div className="selected-attempt-summary">
                    <p><strong>Selected attempt:</strong> {selectedAgentName} · attempt {attemptOrdinal} · {labelStatus(selectedReviewAttempt.status)} · started <time dateTime={selectedReviewAttempt.startedAt}>{formatActivityTimestamp(selectedReviewAttempt.startedAt)}</time>{selectedReviewAttempt.endedAt ? ` · ended ${formatActivityTimestamp(selectedReviewAttempt.endedAt)}` : ' · no end time saved'}</p>
                    <p><strong>Recorded actions:</strong> {selectedReviewAttempt.toolCalls.map(recordedToolAction).join(' → ') || 'No tool action saved.'}</p>
                    {selectedReviewAttempt.resultSummary ? <RecordedText text={selectedReviewAttempt.resultSummary} /> : <p>{live ? describeLiveAttempt(snapshot, selectedReviewAttempt)?.summary || 'No result summary was saved for this selected attempt. Inspect its exact actions and call record in Details.' : 'No result summary was saved for this selected attempt. Inspect its exact actions and call record in Details.'}</p>}
                    <button className="inline-link" onClick={() => openAttempt(selectedReviewAttempt.id)}>Open selected attempt in Details →</button>
                    <AttemptInspection snapshot={snapshot} attemptId={selectedReviewAttempt.id} />
                  </div>}
                  <details className="inline-detail"><summary>Other saved attempts ({otherAttempts.length})</summary><div className="compact-attempt-history">{otherAttempts.map(attempt => <button key={attempt.id} type="button" className="inline-link" onClick={() => setSelectedAttemptId(attempt.id)}>{agentsById.get(attempt.agentId)?.name || `Agent identity unavailable (${attempt.agentId})`} · attempt {attempts.indexOf(attempt) + 1} · {labelStatus(attempt.status)} · started <time dateTime={attempt.startedAt}>{formatActivityTimestamp(attempt.startedAt)}</time> · {attempt.toolCalls.map(recordedToolAction).join(' → ') || 'no tool action saved'} · {attempt.id}</button>)}</div></details>
                </> : attempts.length ? <ol className="work-summary-list">{attempts.map((attempt, index) => <li key={attempt.id}><StatusPill status={attempt.status} /><div><strong>{agentsById.get(attempt.agentId)?.name || `Agent identity unavailable (${attempt.agentId})`} · attempt {index + 1}{inheritedAttemptIds.has(attempt.id) ? ' · parent history' : ''} · started <time dateTime={attempt.startedAt}>{formatActivityTimestamp(attempt.startedAt)}</time>:</strong><RecordedText text={attempt.resultSummary || (live ? describeLiveAttempt(snapshot, attempt)?.summary : null) || 'No result summary was saved.'} />{(live || recorded) && <button className="inline-link" onClick={() => setSelectedAttemptId(attempt.id)}>{selectedAttemptId === attempt.id ? 'Selected attempt' : 'Select this attempt'}</button>}<AttemptInspection snapshot={snapshot} attemptId={attempt.id} /></div></li>)}</ol> : <p>No agent attempt was recorded for this task. It remains a task or request record.</p>}</div></li>
                <li><div><h4>{coordinationFixture ? 'Callback observation' : snapshot.inspection ? 'Accepted contributions for this task' : 'Contribution from selected attempt'}</h4>{coordinationFixture ? <RecordedText text={selectedReviewAttempt?.resultSummary ?? 'No callback attempt was recorded for this task.'} /> : snapshot.inspection ? <RecordGroup snapshot={snapshot} title="Accepted contributions" references={snapshot.inspection.attempts.filter(item => attempts.some(attempt => attempt.id === item.attemptId)).flatMap(item => item.acceptedOutputReferences ?? [])} /> : <RecordedText text={output} />}{sourceResult && output !== sourceResult.text && <blockquote className="story-passage"><strong>{linkedSource?.id} · saved passage</strong>{sourceResult.text}</blockquote>}{revisedClaim && <p className="stage-context">Revised claim v{revisedClaim.version}: {revisedClaim.text}</p>}{linkedSource && <button className="inline-link" onClick={() => openSource(linkedSource.id, sourceResult?.spanId)}>Inspect exact source copy <span aria-hidden="true">→</span></button>}</div></li>
              </ol>
              <details className="inline-detail"><summary><span className="closed-label">Expand task details</span><span className="open-label">Hide task details</span></summary><div className="inline-detail-body"><dl><div><dt>Task ID</dt><dd>{reviewTask.id}</dd></div><div><dt>Parent task</dt><dd>{reviewTask.parentTaskId || 'Mission root'}</dd></div>{reviewTask.firstContributionAt ? <div><dt>First saved contribution</dt><dd>{formatTime(reviewTask.firstContributionAt)}</dd></div> : reviewTask.firstUsefulAt ? <div><dt>First useful result</dt><dd>{formatTime(reviewTask.firstUsefulAt)}</dd></div> : <div><dt>First saved contribution</dt><dd>Not yet recorded</dd></div>}</dl>{attempts.map(attempt => <div className="inline-attempt" key={attempt.id}><strong>{agentsById.get(attempt.agentId)?.name || attempt.agentId} · {attempt.id}</strong><span>{formatTime(attempt.startedAt)} → {formatTime(attempt.endedAt)}</span><span>{attempt.toolCalls.length ? attempt.toolCalls.map(tool => `${tool.name}: ${labelStatus(tool.status)}`).join('; ') : 'No tool call saved'}</span><span>Returned content: {labelStatus(attempt.returnedReasoning.availability)}</span></div>)}<button className="inline-link" onClick={() => openTask(reviewTask.id)}>Open full technical record in Details →</button></div></details>
              </>}
            </ReviewCard>}
        </section>
        <ReviewCard id="review-output" label="Current saved output" controlLabel="saved output" className="hero-card" eyebrow="3 · Output" title={coordinationFixture ? 'No scientific report saved' : 'Current saved output'} actions={<><InfoTip label="Saved output" description={coordinationFixture ? 'Callback observations are operational test records. Inspect their exact saved versions in Details; no scientific report was produced.' : 'Read the reported source results, then the saved interpretation and remaining unknowns. Check the saved mission status and evidence before relying on a result.'} />{continuingTasks.length ? <span className="status-pill status-completed">Requested scope complete</span> : <StatusPill status={snapshot.mission.status} />}</>} expanded={reviewExpanded.output} onToggle={() => toggleReviewCard('output')}>
        {continuingTasks.length > 0 && <p className="output-limit">The requested scope is complete while additional tasks remain open in this saved snapshot. Open their exact task records from the progress strip above.</p>}
          {!coordinationFixture && snapshot.inspection && <p className="output-limit">{currentReport ? 'This exact output version is recorded as accepted. Acceptance records a host decision; scientific validity requires evidence review.' : snapshot.inspection.acceptedOutputReferences.length > 1 ? 'Several independent reports were accepted. No single report was selected as the mission answer; review each contribution and its evidence.' : snapshot.inspection.acceptedOutputReferences.length === 0 ? 'No accepted report yet. Completed work steps remain available above.' : 'The accepted report reference cannot be resolved in this snapshot. Inspect the saved reference below.'}</p>}
          {coordinationFixture ? <p>Accepted callback observations are operational test records. They do not form a scientific answer. The mission remains {snapshot.mission.status}, with waiting work and an unanswered peer request available in Details and Communication.</p> : !currentReport && snapshot.inspection && snapshot.inspection.acceptedOutputReferences.length > 1 ? <div aria-label="Independent accepted reports">{snapshot.artifacts.filter(artifact => snapshot.inspection?.acceptedOutputReferences.some(ref => ref.id === artifact.id && ref.version === artifact.version)).map(report => <section key={`${report.id}-${report.version}`} className="independent-report"><h4>{report.title}</h4><ReportOutput report={report} snapshot={snapshot} onOpenAttempt={openAttempt} sources={snapshot.sources} onOpenSource={openSource} live={!!live || recorded} inspectionSnapshot={snapshot} /></section>)}</div> : !currentReport && snapshot.inspection ? null : <ReportOutput report={currentReport} snapshot={snapshot} onOpenAttempt={openAttempt} sources={snapshot.sources} onOpenSource={openSource} live={!!live || recorded} inspectionSnapshot={snapshot.inspection ? snapshot : undefined} />}
          {!coordinationFixture && snapshot.inspection && snapshot.inspection.acceptedOutputReferences.length > 0 && <RecordGroup snapshot={snapshot} title="Accepted output records" references={snapshot.inspection.acceptedOutputReferences} />}
          <div className="review-work-links"><button className="outline-button" onClick={() => setScreen('evidence')}>Check saved evidence →</button>{snapshot.parentSnapshotId && <button className="outline-button" onClick={() => setScreen('compare')}>Compare with the earlier answer →</button>}</div>
        </ReviewCard>
      </div>
    );
  }

  function renderActivity() {
    const available = sampleMode === 'replaying' || sampleMode === 'paused' ? activityItems.slice(0, replayStep) : activityItems;
    const overview = activityOverview(snapshot);
    const runScopeKnown = !snapshot.parentSnapshotId || Boolean(exactActivityParent(snapshot, comparisonSnapshot));
    const newlySeen = new Set(liveAttemptChanges.runId === live?.runId ? liveAttemptChanges.added : []);
    const recentlyUpdated = new Set(liveAttemptChanges.runId === live?.runId ? liveAttemptChanges.updated : []);
    const currentStepStatus = (item: ActivityItem) => {
      if (!live || !item.attemptId || !item.taskId) return null;
      const task = tasksById.get(item.taskId);
      const attempt = snapshot.attempts.find(candidate => candidate.id === item.attemptId);
      const latest = snapshot.attempts.filter(candidate => candidate.taskId === item.taskId)
        .sort(compareAttemptStart).at(-1);
      if (!task || !attempt || latest?.id !== attempt.id) return null;
      const outcome = describeLiveAttempt(snapshot, attempt);
      if (!['waiting', 'running'].includes(attempt.status) && !showsOriginalRunningRecord(snapshot, attempt, outcome)) return null;
      if (task.status === 'waiting') {
        if (outcome?.badgeStatus === 'provider_rate_limit') return 'Provider wait in saved state';
        if (outcome?.label === 'Context preparation needs local repair') return 'Local repair needed';
        return 'Waiting in saved state';
      }
      if (task.status === 'running') {
        if (outcome?.badgeStatus === 'tool_recovery_needed') return 'Tool recovery pending';
        if (outcome?.badgeStatus === 'tool_waiting') return 'Tool result pending';
        if (!outcome?.responseStatus) return 'Current preparation; dispatch unconfirmed';
        return 'Current task · running';
      }
      return null;
    };
    const search = activitySearch.trim().toLocaleLowerCase();
    const visible = available.filter(item => {
      if (activityWorkGroup) {
        const attempt = snapshot.attempts.find(attempt => attempt.id === item.attemptId);
        if (!attempt || inheritedAttemptIds.has(attempt.id) || workGroupLabel(snapshot, attempt) !== activityWorkGroup.label) return false;
      }
      if (activityFilter !== 'all' && item.category !== activityFilter) return false;
      if (activityAgentId !== 'all' && item.agentId !== activityAgentId && item.fromAgentId !== activityAgentId && item.toAgentId !== activityAgentId) return false;
      if (activityAttentionOnly && !hasRecordedIssue(item)) return false;
      if (!search) return true;
      const actorNames = [item.agentId, item.fromAgentId, item.toAgentId].filter((id): id is string => Boolean(id)).map(id => agentsById.get(id)?.name ?? id);
      const taskTitle = item.taskId ? tasksById.get(item.taskId)?.title ?? '' : '';
      return [item.title, item.summary, item.status ?? '', item.id, taskTitle, ...actorNames].join(' ').toLocaleLowerCase().includes(search);
    }).sort((a, b) => {
      const start = (item: ActivityItem) => snapshot.attempts.find(attempt => attempt.id === item.attemptId)?.startedAt ?? item.time;
      return start(a).localeCompare(start(b)) || a.id.localeCompare(b.id);
    });
    const agentIds = [...new Set(activityItems.flatMap(item => [item.agentId, item.fromAgentId, item.toAgentId].filter((id): id is string => Boolean(id))))];
    const filtered = activityFilter !== 'all' || activityAgentId !== 'all' || Boolean(search) || activityAttentionOnly || Boolean(activityWorkGroup);
    const groups = groupActivity(visible, snapshot, comparisonSnapshot);
    const originByItem = new Map(groups.flatMap(group => group.items.map(item => [`${item.category}:${item.id}`, group.origin] as const)));
    const renderItem = (item: ActivityItem) => { const itemAttempt = snapshot.attempts.find(attempt => attempt.id === item.attemptId); const presentation = itemAttempt ? stepPresentation(snapshot, itemAttempt) : null; const compactSummary = activityScanSummary(snapshot, item); const currentStatus = currentStepStatus(item); const isNew = !!item.attemptId && newlySeen.has(item.attemptId); const isUpdated = !!item.attemptId && recentlyUpdated.has(item.attemptId); return <li key={`${item.category}-${item.id}`} data-attempt-id={item.attemptId} className={`panel activity-item${isNew ? ' activity-new' : ''}${isUpdated ? ' activity-updated' : ''}${currentStatus ? ' activity-current' : ''}`}>
        <ActivityTimestamp item={item} attempt={itemAttempt} id={`activity-time-${item.category}-${item.id}`} />
        <div className="activity-row-content"><div id={`activity-label-${item.category}-${item.id}`} className="activity-item-head">{presentation && presentation.actorKind !== "agent" ? <span className="system-action-label">{presentation.actorKind === "system" ? "System" : presentation.providerName ?? "Model service"} · for {agentsById.get(itemAttempt!.agentId)?.name ?? itemAttempt!.agentId}</span> : (item.agentId || item.fromAgentId) && <AgentBadge name={agentsById.get((item.agentId || item.fromAgentId)!)?.name} agentId={item.agentId || item.fromAgentId} />}<span className={`activity-action-icon activity-kind-${item.category}`} aria-hidden="true">{item.category === 'communication' ? '→' : item.category === 'event' ? '◇' : hasRecordedIssue(item) ? '!' : '↳'}</span><strong>{activityAction(snapshot, item)}</strong>{item.toAgentId && <><span aria-label="to">→</span><AgentBadge name={agentsById.get(item.toAgentId)?.name} agentId={item.toAgentId} /></>}{item.status && <StatusPill status={item.badgeStatus || item.status} label={presentation?.outcomeLabel} />}{currentStatus && <span className="activity-current-label">{currentStatus}</span>}{isNew && <span className="activity-new-label">New since opening</span>}{isUpdated && <span className="activity-new-label">Updated since opening</span>}</div>{activityOrder === 'time' && <p className="activity-row-scope"><span title={item.taskId}>{item.taskId ? tasksById.get(item.taskId)?.title === snapshot.mission.question ? 'Shared request' : identifyTask(item.taskId).label : 'Run-wide'}</span>{item.attemptId && <> · Step {stepNumbers.get(item.attemptId)}</>} · {originByItem.get(`${item.category}:${item.id}`) === 'earlier' ? 'Earlier run' : originByItem.get(`${item.category}:${item.id}`) === 'unknown' ? 'Run origin not confirmed' : 'Selected run'}</p>}<p className="activity-compact-summary">{compactSummary}</p><div className="activity-row-actions"><button type="button" className="activity-expand" aria-expanded={expandedActivityId === `${item.category}:${item.id}`} aria-controls={expandedActivityId === `${item.category}:${item.id}` ? `activity-detail-${item.category}-${item.id}` : undefined} aria-describedby={`activity-label-${item.category}-${item.id} activity-time-${item.category}-${item.id}`} onClick={() => { if (item.attemptId) acknowledgeAttempt(item.attemptId); if (followLatest) setFollowLatest(false); setExpandedActivityId(current => current === `${item.category}:${item.id}` ? null : `${item.category}:${item.id}`); }}>{expandedActivityId === `${item.category}:${item.id}` ? 'Hide activity details' : 'Expand activity details'}</button></div>
          {expandedActivityId === `${item.category}:${item.id}` && <div id={`activity-detail-${item.category}-${item.id}`} className="activity-expanded">
            {itemAttempt ? <SelectedWorkStep snapshot={snapshot} taskId={itemAttempt.taskId} attemptId={itemAttempt.id} runId={live?.runId} onOpenAttempt={openAttempt} onOpenTask={openTask} inspectImmediately /> : <><p>{item.summary}</p><ActivityRecordDetails item={item} snapshot={snapshot} compactSummary={compactSummary} />{item.taskId && <button className="inline-link" onClick={() => openTask(item.taskId!)}>Open related task →</button>}{item.sourceId && <button className="inline-link" onClick={() => openSource(item.sourceId!)}>Read source →</button>}</>}
          </div>}
        </div>
      </li>; };
    return <div className="page-stack">
      <div className="review-toolbar"><SectionHeading eyebrow={recorded ? 'Saved offline activity' : live ? 'Saved live activity' : 'Saved sample trace'} title="Activity" description="Follow saved actions, model calls, errors and waits in time order; expand a step for its exact trace." visiblePurpose /><button className="outline-button" onClick={() => setScreen('work')}>Next: Details →</button></div>
      {live && <section className="activity-current-work panel" aria-label="Current saved work">
        <div className="activity-current-work-heading"><div><h3>{live.connectionError ? 'Last saved work state' : 'Current saved work'}</h3><p>These are task states in the saved snapshot. Viewer checks do not mean research progressed.</p></div><button type="button" className="activity-follow" aria-pressed={followLatest} title="Follow new or updated visible work steps while Activity stays open" onClick={() => { setFollowLatest(value => !value); setFollowTarget(null); }}>Follow latest {followLatest ? 'on' : 'off'}</button></div>
        {currentSavedTasks.length ? <ul>{currentSavedTasks.map(task => <li key={task.id}><StatusPill status={task.status} /><span className="activity-current-task-text"><strong title={`${task.id}: ${task.title}`}>{identifyTask(task.id).agentIds.length ? `${identifyTask(task.id).agentIds.map(id => agentsById.get(id)?.name ?? id).join(' & ')}’s work` : task.title === snapshot.mission.question ? 'Overall research request' : 'Unassigned work'}</strong><small title={task.title}>{task.title === snapshot.mission.question ? 'Part of the shared research request' : task.title}</small>{task.status === 'waiting' && task.waitingReason && <small title={task.waitingReason}>{task.waitingReason}</small>}</span><button type="button" className="inline-link" onClick={() => openTask(task.id)}>Open task →</button></li>)}</ul> : <p>No queued, running or waiting task is included in this snapshot.</p>}
        {(newlySeen.size > 0 || recentlyUpdated.size > 0) && <p role="status">{newlySeen.size} new work step{newlySeen.size === 1 ? '' : 's'}; {recentlyUpdated.size} updated work step{recentlyUpdated.size === 1 ? '' : 's'} since this run was opened in this viewer. Open a marked step to clear its marker.</p>}
      </section>}
      <details className="trace-boundary"><summary>About these saved activity records</summary><strong aria-live="polite">{sampleMode === 'replaying' || sampleMode === 'paused' ? `Replaying saved sample · ${replayStep} of ${activityItems.length} records` : `Saved snapshot · ${formatActivityTimestamp(snapshot.capturedAt)}`}</strong><span>{live ? 'The local host is polled for saved updates. This trace contains recorded actions and summaries, not hidden model chain of thought.' : `No live connection or automatic refresh. ${sampleMode === 'replaying' || sampleMode === 'paused' ? 'This is a timed playback of existing records; no agents are running.' : 'This trace contains saved actions and summaries, not hidden model chain of thought.'}`}</span></details>
      <ActivityWorkOverview snapshot={snapshot} earlierAttemptIds={inheritedAttemptIds} runScopeKnown={runScopeKnown} selectedGroup={activityWorkGroup?.label ?? null} onSelectGroup={group => openReviewActivity(group)} />
      <details className="activity-count-guide"><summary>Saved counts: {overview.attempts} work steps · {overview.issues ?? '—'} with recorded issues</summary><p className="activity-metric-scope"><strong>All tasks in this saved view</strong>{inheritedAttemptIds.size > 0 && <> · Includes {inheritedAttemptIds.size} steps retained from the earlier run.</>}</p><div className="activity-overview" aria-label="Saved activity at a glance">{[
        ['↳', 'Workflow steps', overview.attempts], ['◇', 'Prepared requests', overview.prepared],
        ['↩', 'Saved model responses', overview.receipts], ['▤', 'Accepted reports', overview.acceptedReports],
        ['!', 'Steps with issues', overview.issues], ['⇄', 'Peer requests', overview.peerRequests],
      ].map(([icon, label, value]) => <div className="activity-metric" key={label}><span className="activity-metric-icon" aria-hidden="true">{icon}</span><strong>{value ?? '—'}</strong><span>{label}</span></div>)}</div><p>Workflow steps include local preparation and recovery. Prepared requests are saved model inputs; saved model responses include provider rejections. Neither count is a billing record. Accepted reports and peer requests are separate saved outcomes. A dash means records are unavailable.</p>{snapshot.inspection && <a href="#activity-saved-steps">Inspect all saved step records ({snapshot.inspection.steps?.length ?? 0}) ↓</a>}</details>
      {overview.attempts === 0 && <p className="screen-intro">No saved agent attempts in this snapshot. The requested scope is {labelStatus(snapshot.mission.status)}; queued or waiting tasks remain visible in Details.</p>}
      {!live && !recorded && <div className="replay-controls">{sampleMode === 'replaying' ? <button className="outline-button" onClick={() => setSampleMode('paused')}>Pause replay</button> : sampleMode === 'paused' ? <button className="outline-button" onClick={() => setSampleMode('replaying')}>Resume replay</button> : <button className="outline-button" onClick={replaySample}>Replay saved sample from start</button>}{sampleMode === 'replayed' && <span>Replay complete. The full saved review is available.</span>}</div>}
      {activityGroupNotice && <p className="activity-review-scope" role="status">{activityGroupNotice}</p>}
      {activityWorkGroup && <div className="activity-review-scope"><div><span>Selected work category</span><strong>{activityWorkGroup.label}</strong><p>Showing matching steps in this saved view. Saved counts above still include all tasks.</p></div><button className="outline-button" onClick={() => openReviewActivity()}>Show all work steps</button></div>}
      <div className="activity-filters" role="group" aria-label="Activity filter">{(['all', 'agent', 'communication', 'event'] as const).map(value => <button key={value} className={activityFilter === value ? 'active' : ''} aria-pressed={activityFilter === value} onClick={() => {
        setActivityFilter(value);
        if (activityWorkGroup && value !== 'agent') {
          setActivityWorkGroup(null);
          setActivityGroupNotice('The work-action category was cleared so these saved records remain visible.');
        }
      }}>{value === 'all' ? 'All' : value === 'agent' ? 'Agent work' : value === 'communication' ? 'Messages' : 'Host events'}</button>)}</div>
      <div className="activity-search"><label>Agent <select value={activityAgentId} onChange={event => setActivityAgentId(event.target.value)}><option value="all">All agents and host</option>{agentIds.map(id => <option key={id} value={id}>{agentsById.get(id)?.name ? `Agent ${agentsById.get(id)!.name}` : `Agent identity unavailable (${id})`}</option>)}</select></label><label>Search saved activity <input type="search" value={activitySearch} onChange={event => setActivitySearch(event.target.value)} placeholder="Words in saved records" /></label><button type="button" className={activityAttentionOnly ? 'active activity-attention' : 'activity-attention'} aria-pressed={activityAttentionOnly} onClick={() => setActivityAttentionOnly(!activityAttentionOnly)}>Recorded issues</button><button type="button" className="text-button" onClick={() => { setActivityFilter('all'); setActivityAgentId('all'); setActivitySearch(''); setActivityAttentionOnly(false); setActivityWorkGroup(null); setActivityGroupNotice(null); }}>Clear filters</button><span aria-live="polite">{visible.length} of {available.length} saved records</span></div>
      {visible.length === 0 && <p className="screen-intro">{available.length === 0 ? 'No activity record has been saved for this run yet.' : filtered ? 'No saved activity matches the selected category, agent, recorded-issues setting or search. Clear filters to see all records.' : 'No saved activity is available.'}</p>}
      <div className="activity-filters activity-order" role="group" aria-label="Activity organization"><button type="button" aria-pressed={activityOrder === 'task'} className={activityOrder === 'task' ? 'active' : ''} onClick={() => setActivityOrder('task')}>By task</button><button type="button" aria-pressed={activityOrder === 'time'} className={activityOrder === 'time' ? 'active' : ''} onClick={() => setActivityOrder('time')}>Time order</button></div>
      <p className="activity-order-note">{activityOrder === 'time' ? 'Steps are ordered by start time; messages and system events appear at their saved time. Finish times show where work overlapped.' : 'Grouped by task. Steps in different tasks can overlap in time.'}</p>{activityOrder === 'time' ? <ol className="activity-list">{visible.map(renderItem)}</ol> : <div className="activity-task-groups">{groups.map(group => {
        const task = group.taskId ? (group.origin === 'earlier' ? exactActivityParent(snapshot, comparisonSnapshot)?.tasks.find(task => task.id === group.taskId) : tasksById.get(group.taskId)) : undefined;
        const identity = group.taskId ? identifyTask(group.taskId) : null;
        return <section className={`activity-task-group activity-origin-${group.origin}`} key={group.key} aria-label={`${group.origin === 'earlier' ? 'Earlier run' : group.origin === 'unknown' ? 'Origin not confirmed' : 'Selected run'} · ${identity?.label ?? 'Run-wide activity'}`}>
          <header className="activity-task-heading"><div><span className="eyebrow">{group.origin === 'earlier' ? 'Earlier run · unchanged saved history' : group.origin === 'unknown' ? 'Run origin not confirmed' : 'Selected run'} · {identity?.label ?? 'Run-wide activity'} · {group.items.length} records</span><h3 title={task ? `${task.id}: ${task.title}` : group.taskId}>{task?.title ?? (group.taskId ? 'Task record unavailable' : 'No specific task recorded')}</h3><div className="activity-task-agents">{[...new Set(group.items.flatMap(item => item.agentId ? [item.agentId] : []))].map(id => <AgentBadge key={id} name={agentsById.get(id)?.name} agentId={id} />)}{task && <StatusPill status={task.status} />}</div></div>{task && <button type="button" className="inline-link" onClick={() => openTask(task.id)}>Open task →</button>}</header>
          <ol className="activity-list">{group.items.map(renderItem)}</ol>
        </section>;
      })}</div>}

      {(live || recorded) && <details className="inline-detail review-step-browser" open={reviewTimelineOpen} onToggle={event => { if (event.target === event.currentTarget) setReviewTimelineOpen(event.currentTarget.open); }}><summary>Browse all saved work steps</summary>
                <RecordedWorkTimeline inspectAttemptId={inspectAttemptId} snapshot={snapshot} parentSnapshot={comparisonSnapshot} includeParentHistory={includeParentHistory} onToggleParentHistory={() => setIncludeParentHistory(value => !value)} selectedAttemptId={selectedAttemptId} taskLabel={id => identifyTask(id).label} onSelectAttempt={(attemptId, taskId) => { setSelectedAttemptId(attemptId); setSelectedReviewTaskId(taskId); }} onSelectTask={openTask} renderStepDetails={(attemptId, taskId) => <SelectedWorkStep snapshot={snapshot} runId={live?.runId} taskId={taskId} attemptId={attemptId} onOpenAttempt={openAttempt} onOpenTask={openTask} inspectImmediately />} />
              </details>}
      {snapshot.inspection && <div id="activity-saved-steps"><SavedAgentSteps snapshot={snapshot} /></div>}
    </div>;
  }

  function renderCommunication() {
    const recordedParticipantIds = [...new Set(snapshot.attempts.map(attempt => attempt.agentId))];
    const agentReasoning = live?.runId && !recorded ? <LiveAgentReasoning runId={live.runId} snapshot={snapshot} selectedAttemptId={reasoningAttemptId} earlierAttemptIds={[...inheritedAttemptIds]} /> : null;
    const noDirectedExchange = snapshot.messages.length === 0 && !snapshot.inspection?.records.some(record => record.reference.kind === 'mail_request');
    const emptyExchange = noDirectedExchange && <section className="panel communication-empty"><h3>No peer messages recorded</h3><p>{recordedParticipantIds.length ? `This snapshot contains recorded work by ${recordedParticipantIds.map(id => agentsById.get(id)?.name ?? `Agent identity unavailable (${id})`).join(', ')}. No directed peer request is saved here.` : 'No agent attempt or directed peer request is included in this snapshot.'}</p><button className="inline-link" onClick={() => { setActivityFilter('agent'); setScreen('activity'); }}>See saved agent activity →</button></section>;
    if (snapshot.inspection) return <div className="page-stack">
      <div className="review-toolbar"><SectionHeading eyebrow="Saved coordination" title="Communication" description="See who asked, replied and reviewed; a request alone does not prove an answer." visiblePurpose /><button className="outline-button" onClick={() => setScreen('evidence')}>Next: Evidence →</button></div>
      <PeerCoordination snapshot={snapshot} runId={live?.runId} onOpenTask={openTask} />
      {snapshot.inspection?.records.some(record => record.reference.kind === 'contribution_review_view') && <details className="panel shared-review-disclosure"><summary>Shared feedback and its recorded effects</summary><ContributionReview snapshot={snapshot} /></details>}
      {live?.connectionError && <p className="panel" role="status">This is the last saved snapshot. Exact call and provider reasoning text may be unavailable until the saved workspace is reconnected from the Mac app’s File menu.</p>}
      {agentReasoning}
    </div>;
    const requestIds = [...new Set(snapshot.messages.map(message => message.requestId || message.id))];
    return <div className="page-stack">
      <div className="review-toolbar"><SectionHeading eyebrow={recorded ? 'Saved offline messages' : live ? 'Saved live messages' : 'Saved sample messages'} title="Communication" description="See who asked, replied and reviewed; open a request for its saved delivery and response history." visiblePurpose /><button className="outline-button" onClick={() => setScreen('evidence')}>Next: Evidence →</button></div>
      {requestIds.length > 0 && <p className="screen-intro">Each card follows one request from its ask to its result. The same agent can receive several different requests.</p>}
      {emptyExchange}
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
            <section aria-label={savedResult ? 'Result' : 'Current status'}><h4>{savedResult ? 'Result' : 'Current status'}</h4><div className="communication-person"><span>Assigned to</span><AgentBadge name={agentsById.get(first.toAgentId)?.name} agentId={first.toAgentId} /></div><p>{savedResult?.summary ?? last.summary}</p>{deliveredReply && <p className="communication-pending">Reply: {agentsById.get(deliveredReply.fromAgentId)?.name || deliveredReply.fromAgentId} → {agentsById.get(deliveredReply.toAgentId)?.name || deliveredReply.toAgentId}</p>}{!savedResult && <p className="communication-pending">{requestStatus === 'awaiting_review' ? 'Answer submitted; requester review pending.' : fulfilled ? 'Request marked fulfilled, but no separate reply content was saved.' : 'No completed response has been recorded.'}</p>}</section>
          </div>
          <details className="communication-history inline-detail"><summary><span className="closed-label">Expand communication and contribution</span><span className="open-label">Hide communication and contribution</span></summary><div className="inline-detail-body">
            <h4>Other work on this task</h4><p>These steps share the task. The saved records do not link each step to this particular request.</p>
            <ul className="communication-work">{relatedAttempts.map((attempt, index) => <li key={attempt.id}><StatusPill status={attempt.status} /><div><p><strong>Attempt {index + 1} · {agentsById.get(attempt.agentId)?.name || `Agent name unavailable (${attempt.agentId})`}.</strong> {attempt.resultSummary || 'No summary saved.'}</p><AttemptInspection snapshot={snapshot} attemptId={attempt.id} /></div></li>)}</ul>
            {linkedSource && <p><strong>Source contribution:</strong> {linkedSource.id} · {linkedSource.title}</p>}
            {linkedClaim && <p><strong>Claim revision:</strong> v{linkedClaim.version} · {linkedClaim.text}</p>}
            <h4>Delivery and response history</h4>
            <ol className="communication-status-history">{history.map(message => <li key={message.id}><time dateTime={message.createdAt}>{formatActivityTimestamp(message.createdAt)}</time><StatusPill status={message.status} /><div><p><strong>{agentsById.get(message.fromAgentId)?.name || message.fromAgentId} → {agentsById.get(message.toAgentId)?.name || message.toAgentId}</strong></p><p>{message.summary}</p></div></li>)}</ol>
            <button className="inline-link" onClick={() => openTask(last.taskId)}>Open full technical task record →</button>
          </div></details>
        </article>;
      })}</div>
      {agentReasoning}
    </div>;
  }

  function renderWork() {
    const taskAttempts = snapshot.attempts.filter(attempt => attempt.taskId === selectedTask?.id).sort(compareAttemptStart);
    const focusedTaskAttempt = taskAttempts.find(attempt => attempt.id === selectedAttemptId) ?? taskAttempts[0];
    const fulfilledContribution = snapshot.messages.find(message => message.taskId === selectedTask?.id && message.status === 'fulfilled');
    const relatedRequests = latestMessages.filter(message => message.taskId === selectedTask?.id);
    const relatedRequest = taskReference?.requestId ? relatedRequests.find(message => message.requestId === taskReference.requestId) : relatedRequests[0];
    const sourceRequest = taskReference?.requestId ? snapshot.inspection?.records.filter(record => record.reference.kind === 'mail_request' && record.reference.id === taskReference.requestId).sort((a, b) => b.reference.version - a.reference.version)[0] : undefined;
    return (
      <div className="page-stack">
        <div className="review-toolbar"><SectionHeading eyebrow={live ? 'Saved live records' : 'Saved sample records'} title="Details" description="Inspect exact task requests, versions, timing and waiting records." visiblePurpose /><button className="outline-button" onClick={() => setScreen('overview')}>Return to Research review →</button></div>
        {taskOrigin && <button className="inline-link" onClick={() => setScreen(taskOrigin)}>← Return to {activeNavigation.find(item => item.id === taskOrigin)?.label ?? 'previous view'}</button>}
        <div className="details-switch" role="group" aria-label="Details sections"><button className="active" aria-pressed="true">Task records</button><button onClick={() => setScreen('diagnostics')}>Work step records</button></div>
        <details className="panel run-metadata"><summary title="Open to inspect exact snapshot IDs, versions, and saved time.">Run identifiers and model settings</summary><dl><div><dt>Snapshot</dt><dd>{snapshot.snapshotId}</dd></div><div><dt>Mission</dt><dd>{snapshot.mission.id} · v{snapshot.mission.version}</dd></div>{live ? <><div><dt>Run</dt><dd>{live.runId}</dd></div><div><dt>Parent snapshot</dt><dd>{snapshot.parentSnapshotId || 'None'}</dd></div><div><dt>Model</dt><dd>{live.model}</dd></div><div><dt>Evidence routes enabled</dt><dd>{live.retrievalRoutes?.length ? live.retrievalRoutes.join(', ') : 'Supplied evidence only'}</dd></div></> : <div><dt>Sample packet</dt><dd>v{snapshot.sampleVersion}</dd></div>}<div><dt>Saved</dt><dd>{formatTime(snapshot.capturedAt)}</dd></div></dl><SnapshotDetails snapshot={snapshot} parentSnapshot={comparisonSnapshot} />{recorded ? <p>Saved offline demonstration. These persisted records do not establish live provider execution or scientific validity.</p> : live ? <p>Live output and source origins are identified by this run's saved records. The current snapshot may change as work arrives; earlier snapshots remain available through the follow-up comparison.</p> : <><p>This is a simulated saved snapshot using real clinical labels. No research host, provider, model, sandbox, or live event feed is connected. The separate Groq skill experiment is summarized in <code>docs/development/model-fit.md</code>; its actual synthesis and appraisal are not loaded here.</p><label>Show UI state example <select value={previewState} onChange={event => setPreviewState(event.target.value as PreviewState)}>{previewStates.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label></>}</details>
        <label className="task-picker">Choose a task <select value={selectedTask?.id ?? ''} onChange={event => openTask(event.target.value)}><option value="" disabled>Select a task</option>{snapshot.tasks.map(task => <option key={task.id} value={task.id}>{identifyTask(task.id).label} · {identifyTask(task.id).agentIds.map(id => agentsById.get(id)?.name ?? id).join(', ') || 'Overall request'} · {task.status.replaceAll('_', ' ')}</option>)}</select></label>
        <details className="panel details-work-history"><summary>All work steps in this run</summary>
          <div className="panel-heading"><div><span className="eyebrow">{live || recorded ? 'Agent work steps' : 'Task timeline'}</span><div className="heading-with-tip"><h3>{live || recorded ? 'Who worked and when' : 'What overlapped'}</h3><InfoTip label="Task timeline" description={live || recorded ? 'Each step belongs to one agent and task. Prepared steps may never reach the provider. Work inherited from an earlier run is labeled separately.' : 'Select a task row to inspect its exact record below. Bars show saved attempt times, not a live scheduler.'} /></div></div></div>
          {live || recorded ? <RecordedWorkTimeline inspectAttemptId={inspectAttemptId} snapshot={snapshot} parentSnapshot={comparisonSnapshot} includeParentHistory={includeParentHistory} onToggleParentHistory={() => setIncludeParentHistory(value => !value)} selectedAttemptId={selectedAttemptId} onSelectAttempt={(attemptId, taskId) => { setSelectedAttemptId(attemptId); setSelectedTaskId(taskId); setJumpToTask(true); }} onSelectTask={openTask} /> : <div className="work-grid" aria-label="Task timeline">
            {snapshot.tasks.map((task, index) => (
              <button key={task.id} className={`work-row ${selectedTask?.id === task.id ? 'selected' : ''}`} onClick={() => openTask(task.id)}>
                <span className="work-number">{String(index + 1).padStart(2, '0')}</span>
                <span className="work-description"><strong>{task.title}</strong><small>{task.latencyClass ? `${task.latencyClass.replaceAll('_', ' ')} estimate` : 'Timing not yet estimated'} · {task.activeOrWaiting} · {task.latencyBasis}</small></span>
                <span className="work-track" aria-hidden="true"><span className={`work-bar work-bar-${task.status}`} style={taskBarStyle(task)} /></span>
                <StatusPill status={task.status} />
              </button>
            ))}
          </div>}
        </details>
        <div className="two-column work-detail-grid">
          <section className="panel" id="selected-task" tabIndex={-1}>
            <div className="panel-heading"><div><span className="eyebrow" title={selectedTask?.id}>{selectedTask ? `${identifyTask(selectedTask.id).label} · Selected task` : 'Selected task'}</span><div className="heading-with-tip"><h3>{selectedTask ? (identifyTask(selectedTask.id).agentIds.map(id => agentsById.get(id)?.name ?? id).join(' & ') || 'Overall request') + ' · saved assignment' : 'No task selected'}</h3><InfoTip label="Selected task" description="Read the chosen task's IDs and latest agent request here. Select an attempt below to inspect its tools and returned content." /></div></div>{selectedTask && <StatusPill status={selectedTask.status} />}</div>
            {selectedTask && <>
              <details className="inline-detail"><summary>Exact assignment</summary><p>{selectedTask.title}</p></details>
              {taskReference?.version && <div className="task-contribution"><strong>Opened from exact work version {taskReference.version}</strong><p>The task below shows the current saved assignment{selectedTask.version ? `, version ${selectedTask.version}` : '; its own version is not included in this snapshot'}. The referenced work version is available separately when included.</p>{snapshot.inspection?.records.some(record => record.reference.kind === 'board_proposal' && record.reference.id === selectedTask.id && record.reference.version === taskReference.version) ? <RecordGroup snapshot={snapshot} title="Exact referenced work version" references={[{ kind: 'board_proposal', id: selectedTask.id, version: taskReference.version }]} /> : <p>Exact referenced work version unavailable in this snapshot.</p>}</div>}
              {sourceRequest && <div className="task-contribution"><strong>Opened from peer request {sourceRequest.reference.id} · v{sourceRequest.reference.version}</strong><p>{String(sourceRequest.data.question ?? 'Question unavailable')}</p><p>Expected contribution: {String(sourceRequest.data.expected_artifact ?? 'Not recorded')} · status: {String(sourceRequest.data.status ?? 'Not recorded')}</p></div>}
              <div className="detail-list">
                <div><span>Task</span><strong>{selectedTask.id}</strong></div>
                <div><span>Parent</span><strong>{selectedTask.parentTaskId || 'Mission root'}</strong></div>
                {selectedTask.targetArtifactId && <div><span>Starting artifact</span><strong>{selectedTask.targetArtifactId}</strong></div>}
                {selectedTask.firstContributionAt ? <div><span>First saved contribution</span><strong>{formatTime(selectedTask.firstContributionAt)}</strong></div> : selectedTask.firstUsefulAt ? <div><span>First useful artifact</span><strong>{formatTime(selectedTask.firstUsefulAt)}</strong></div> : <div><span>First saved contribution</span><strong>Not yet recorded</strong></div>}
                <div><span>Current mode</span><strong>{selectedTask.activeOrWaiting}</strong></div>
                {selectedTask.status === "waiting" && <div><span>Waiting reason</span><strong>{selectedTask.waitingReason || "Not recorded in this snapshot"}</strong></div>}
                {selectedTask.status === 'waiting' && selectedTask.waitingOperationReference && <div><span>Next eligible model attempt</span><strong>{selectedTask.waitingUntil ? formatActivityTimestamp(selectedTask.waitingUntil) : 'Time not recorded'}; dispatch may occur later</strong></div>}
              </div>
              <p className="detail-note">{selectedTask.status === 'waiting' ? 'This task is waiting. Inspect its saved request and attempts below for the recorded reason; no absent cause is inferred.' : 'The selected task can be inspected without hiding sibling progress.'}</p>
              {relatedRequest && <div className="task-contribution"><strong>{relatedRequests.length > 1 ? 'One related agent request' : 'Related agent request'} · {relatedRequest.requestId || relatedRequest.id}</strong><p>{relatedRequest.summary}</p><StatusPill status={relatedRequest.status} />{fulfilledContribution && <div className="request-links">{fulfilledContribution.sourceId && <button className="inline-link" onClick={() => openSource(fulfilledContribution.sourceId!)}>Inspect {fulfilledContribution.sourceId}</button>}{fulfilledContribution.claimId && <button className="inline-link" onClick={() => openClaim(fulfilledContribution.claimId!)}>Inspect revised claim</button>}</div>}</div>}
              <h4>Work steps on this task <TermHelp term="work step" /></h4>
              <div className="attempt-stack">
                {taskAttempts.map(attempt => <button key={attempt.id} className={`attempt-item ${focusedTaskAttempt?.id === attempt.id && (live || recorded) ? 'selected' : ''}`} onClick={() => live || recorded ? setSelectedAttemptId(attempt.id) : openAttempt(attempt.id)}><span><strong>{live || recorded ? `${agentsById.get(attempt.agentId)?.name || `Agent identity unavailable (${attempt.agentId})`} · work step ${stepNumbers.get(attempt.id)}` : attempt.id}</strong><small>{live || recorded ? `${attempt.id} · ` : `${agentsById.get(attempt.agentId)?.name || attempt.agentId} · `}{formatTime(attempt.startedAt)}</small></span><StatusPill status={(live || recorded ? describeLiveAttempt(snapshot, attempt)?.badgeStatus : null) ?? attempt.status} /></button>)}
              </div>
              {taskAttempts.length === 0 && <p>No agent attempt is recorded for this task. A saved request does not establish that work started.</p>}
              {(live || recorded) && focusedTaskAttempt && <div className="task-contribution"><strong>{agentsById.get(focusedTaskAttempt.agentId)?.name || `Agent identity unavailable (${focusedTaskAttempt.agentId})`} · selected work step</strong><RecordedText text={focusedTaskAttempt.resultSummary || describeLiveAttempt(snapshot, focusedTaskAttempt)?.summary || 'No result summary saved.'} /><p>Recorded actions: {focusedTaskAttempt.toolCalls.map(recordedToolAction).join(' → ') || 'No tool action saved.'}</p><p>Started {formatActivityTimestamp(focusedTaskAttempt.startedAt)} · {focusedTaskAttempt.endedAt ? `ended ${formatActivityTimestamp(focusedTaskAttempt.endedAt)}` : 'end time not recorded'}</p><button className="inline-link" onClick={() => openAttempt(focusedTaskAttempt.id)}>Open this work step and its calls →</button></div>}
            </>}{!selectedTask && <p>The referenced task is unavailable in this saved run. No other task was substituted.</p>}
          </section>
          <details className="panel assignment-message-records"><summary>Message delivery records</summary><button className="inline-link" onClick={() => setScreen('communication')}>Read the exchanges in Communication →</button>
            <div className="panel-heading"><div><span className="eyebrow">Message status records</span><div className="heading-with-tip"><h3>Agent requests</h3><InfoTip label="Agent request records" description="Use Communication for the plain-language exchange. Here, inspect exact request IDs and whether each request was delivered, acknowledged, or fulfilled." /></div></div></div>
            <div className="request-stack">
              {latestMessages.length === 0 && <p>No peer messages recorded. Task assignments are separate from messages.</p>}
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
          </details>
        </div>
        {snapshot.inspection && <details className="panel"><summary>Detailed saved work records</summary><SavedAgentSteps snapshot={snapshot} /></details>}
        {coordinationFixture && snapshot.inspection && <details className="panel"><summary>Saved coordination records ({snapshot.inspection.records.length})</summary><p>Exact input, callback, receipt, proposal, capacity and trace versions. These records describe the offline test, not research findings.</p><RecordGroup snapshot={snapshot} title="Coordination diagnostics" references={snapshot.inspection.records.map(record => record.reference)} /></details>}
        {snapshot.inspection?.records.some(record => record.reference.kind === 'executor_work' || record.reference.kind === 'board_proposal') && <CoordinationWork snapshot={snapshot} />}
        {snapshot.inspection?.records.some(record => record.reference.kind === 'board_proposal') && <details className="panel technical-disclosure"><summary>Shared work board</summary><PeerCoordination snapshot={snapshot} section="work" onOpenTask={openTask} /></details>}
        <AtlasTerms />
        <details className="panel"><summary>System event records</summary>
          <div className="panel-heading"><div><span className="eyebrow">Saved events</span><div className="heading-with-tip"><h3>Recorded changes and their effects</h3><InfoTip label="Event trail" description="Read saved events in time order, including their recorded agent when available. Use Open task to inspect the related record." /></div></div></div>
          <div className="diagnostic-events">{snapshot.events.map(event => { const display = eventDisplay(event, agentsById); return <article key={event.id}><span className="event-time">{formatTime(event.time)}</span><div><strong>{display.title}</strong><p>{display.summary}</p></div>{event.taskId && <button className="inline-link" onClick={() => openTask(event.taskId!)}>Open task</button>}</article>; })}</div>
        </details>
      </div>
    );
  }

  function renderEvidence() {
    return (
      <div className="page-stack">
        <div className="review-toolbar"><SectionHeading eyebrow="Review" title="Evidence" description="Check saved sources and passages against each claim or answer." visiblePurpose /><button className="outline-button" onClick={() => setScreen('compare')}>Next: Compare →</button></div>
        <EvidenceBrowser snapshot={snapshot} selectedSourceId={selectedSourceId} selectedSpanId={selectedSpanId} onOpenSource={openSource} mode={recorded ? 'recorded' : live ? 'live' : 'sample'} />
        {snapshot.claims.length > 0 && <section className="panel">
          <div className="panel-heading"><div><span className="eyebrow">Claim history</span><div className="heading-with-tip"><h3>Revisions keep their evidence</h3><InfoTip label="Claim history" description="Select a version, read its wording, then open a citation to check the supporting saved passage." /></div></div></div>
          <div className="claim-tabs" role="group" aria-label="Claim versions">
            {snapshot.claims.map(claim => <button key={claim.id} className={selectedClaim?.id === claim.id ? 'active' : ''} onClick={() => setSelectedClaimId(claim.id)}>Version {claim.version}</button>)}
          </div>
          {selectedClaim && <div className="claim-detail"><div><StatusPill status={selectedClaim.disposition} /><span className="subtle-label">{selectedClaim.id}{selectedClaim.supersedesVersion ? ` · supersedes v${selectedClaim.supersedesVersion}` : ''}</span></div><p>{selectedClaim.text}</p>{snapshot.tasks.some(task => task.id === selectedClaim.taskId)
            ? <button className="inline-link" onClick={() => openTask(selectedClaim.taskId)}>Open {identifyTask(selectedClaim.taskId).label} behind this claim →</button>
            : <p className="output-limit">The task linked to this claim is unavailable in this snapshot.</p>}<div className="citation-row">{selectedClaim.supports.map(support => <button key={`${support.sourceId}-${support.spanId}`} className="citation-button" onClick={() => openSource(support.sourceId, support.spanId)}>{support.sourceId} · {support.spanId} <span aria-hidden="true">↗</span></button>)}</div></div>}
        </section>}
        <details className="panel technical-disclosure"><summary>Add a local review note</summary>
          <div className="panel-heading"><div><span className="eyebrow">Reviewer annotation</span><div className="heading-with-tip"><h3>Note a source or claim question</h3><InfoTip label="Reviewer note" description="Select a source and claim version, type a question, then save a local note. It disappears when this view reloads." /></div></div></div>
          <p>Attach a review note to the selected source copy and claim version. Notes are local to this open view.</p>
          <label className="field-label" htmlFor="annotation">Your note about {selectedSource?.id ?? 'the source'} and claim v{selectedClaim?.version ?? '—'}</label>
          <textarea id="annotation" rows={3} value={annotationText} onChange={event => setAnnotationState({ scope: sourceScope, text: event.target.value, notes: annotations })} placeholder="For example: confirm whether the interval uses the same outcome definition." />
          <button className="primary-button annotation-save" disabled={!annotationText.trim()} onClick={() => setAnnotationState({ scope: sourceScope, text: '', notes: [...annotations, { id: annotations.length + 1, target: `${selectedSource?.copyId ?? 'no-copy'} · claim v${selectedClaim?.version ?? '—'}`, text: annotationText.trim() }] })}>Save local {live ? 'review' : 'sample'} note</button>
          {annotations.length > 0 && <div className="annotation-list" aria-label={live ? 'Local review notes' : 'Local sample notes'}>{annotations.map(note => <article key={note.id}><span className="eyebrow">Evidence · {snapshot.mission.id} v{snapshot.mission.version} · {note.target}{live ? '' : ` · sample ${snapshot.sampleVersion}`}</span><p>{note.text}</p></article>)}</div>}
        </details>
      </div>
    );
  }

  function renderDiagnostics() {
    const orderedAttempts = [...snapshot.attempts].sort(compareAttemptStart);
    return (
      <div className="page-stack">
        <div className="review-toolbar"><SectionHeading eyebrow={live ? 'Saved live records' : 'Saved sample records'} title="Details" description="Inspect exact work-step calls, tool results, model settings and recovery records." visiblePurpose /><button className="outline-button" onClick={() => setScreen('overview')}>Return to Research review →</button></div>
        <div className="details-switch" role="group" aria-label="Details sections"><button onClick={() => setScreen('work')}>Task records</button><button className="active" aria-pressed="true">Work step records</button></div>
        <nav className="review-work-links" aria-label="Jump within Details"><span>Jump to</span>{[
          ['details-step-history', 'Work step list'], ['selected-attempt', 'Selected step'],
          ...(snapshot.inspection ? [['details-technical-records', 'All saved technical steps']] : []),
          ['details-operational-events', 'Operational events'],
        ].map(([id, label]) => <button key={id} type="button" className="outline-button" onClick={() => { const destination = document.getElementById(id); destination?.scrollIntoView?.({ block: 'start' }); destination?.focus(); }}>{label} ↓</button>)}</nav>
        <div className="diagnostic-layout">
          <section className="panel" id="details-step-history" tabIndex={-1}>
            <div className="panel-heading"><div><span className="eyebrow">Work step history</span><div className="heading-with-tip"><h3>Inspect a work step <TermHelp term="work step" /></h3><InfoTip label="Attempt history" description="Each task can have several attempts. Select one to see its saved result, tool status, and returned-content availability." /></div></div></div>
            <div className="attempt-stack">
              {orderedAttempts.map(attempt => {
                const outcome = live ? describeLiveAttempt(snapshot, attempt) : null;
                return <button key={attempt.id} className={`attempt-item ${selectedAttempt?.id === attempt.id ? 'selected' : ''}`} onClick={() => openAttempt(attempt.id)}><span><strong>{agentsById.get(attempt.agentId)?.name || `Agent identity unavailable (${attempt.agentId})`} · work step {stepNumbers.get(attempt.id)}{inheritedAttemptIds.has(attempt.id) ? ' · earlier run' : ''}</strong><small><time dateTime={attempt.startedAt}>{formatActivityTimestamp(attempt.startedAt)}</time> · {outcome?.label || labelStatus(attempt.status)}</small></span><StatusPill status={outcome?.badgeStatus || attempt.status} /></button>;
              })}
            </div>
          </section>
          <section className="panel" id="selected-attempt" tabIndex={-1}>
            {selectedAttempt ? <>
              <div className="panel-heading"><div><span className="eyebrow">{identifyTask(selectedAttempt.taskId).label} · {inheritedAttemptIds.has(selectedAttempt.id) ? 'Earlier run' : 'This run'} · work step {stepNumbers.get(selectedAttempt.id)}</span><div className="heading-with-tip"><h3>{agentsById.get(selectedAttempt.agentId)?.name || selectedAttempt.agentId}</h3><InfoTip label="Selected attempt" description="Read this attempt's summary, timing, tools, and returned-content status. Use the related-task link to return to its task record." /></div></div><div className="selected-step-state"><StatusPill status={selectedOutcome?.badgeStatus || selectedAttempt.status} /><time dateTime={selectedAttempt.endedAt || selectedAttempt.startedAt}>{selectedAttempt.endedAt ? 'Finished' : 'Started'} {formatActivityTimestamp(selectedAttempt.endedAt || selectedAttempt.startedAt)}</time></div></div>
              <p>{selectedOutcome && <strong>{selectedOutcome.label}. </strong>}{selectedAttempt.resultSummary || (selectedOutcome?.responseStatus === 'tool_requested' ? 'Tool step only; no accepted report is linked. Inspect the saved call for its exact outcome.' : selectedOutcome?.summary) || 'No result summary was saved for this attempt.'}</p>
              <details className="inline-detail"><summary>{identifyTask(selectedAttempt.taskId).label} · Task assignment</summary><p>{tasksById.get(selectedAttempt.taskId)?.title || 'Task title unavailable in this snapshot.'}</p><TermHelp term="task assignment" /></details>
              <button className="inline-link" onClick={() => openTask(selectedAttempt.taskId)}>Open related task and agent request <span aria-hidden="true">→</span></button>
              <h4>Saved tool outcomes</h4>
              <div className="tool-stack">{selectedAttempt.toolCalls.map((tool, index) => <div key={`${tool.name}-${index}`}><span><strong>{toolActionLabel(tool.name)}</strong></span><StatusPill status={tool.status} /></div>)}{selectedAttempt.toolCalls.length === 0 && <p>No tool call was saved for this attempt.</p>}</div>
              {live ? <p className="saved-reasoning-status"><strong>Provider reasoning:</strong> {['restricted', 'returned'].includes(selectedAttempt.returnedReasoning.availability) ? `Saved text indicated. ${live.connectionError ? 'Reconnect the saved workspace to read the exact call.' : 'Read it from the call below.'}` : selectedAttempt.returnedReasoning.availability === 'truncated' ? 'Partial saved text indicated.' : selectedAttempt.returnedReasoning.availability === 'absent' ? 'None indicated for this step; a waiting or prepared call may have no response.' : 'Availability unknown.'} <button className="inline-link" onClick={() => openModelReasoning(selectedAttempt.id)}>View reasoning history →</button></p> : <><h4>Returned model content</h4><ReasoningView reasoning={selectedAttempt.returnedReasoning} live={recorded} snapshot={snapshot} attemptId={selectedAttempt.id} /><TermHelp term="model reasoning" /></>}
              {live && <LiveCallInspector runId={live.runId} attemptId={selectedAttempt.id} snapshotId={snapshot.snapshotId} snapshot={snapshot} onOpenAttempt={openAttempt} />}
              <details className="inline-detail technical-disclosure"><summary><span>Exact identity, timing, model and skills</span> · configuration and recovery</summary><p>Attempt {selectedAttempt.id} · task {selectedAttempt.taskId}</p><div className="detail-list"><div><span>{showsOriginalRunningRecord(snapshot, selectedAttempt, selectedOutcome) ? 'Snapshot attempt state' : 'Stored attempt status'}</span><strong>{selectedAttempt.status}{showsOriginalRunningRecord(snapshot, selectedAttempt, selectedOutcome) ? ' (saved attempt record: running)' : ''}</strong></div><div><span>Started</span><strong>{formatTime(selectedAttempt.startedAt)}</strong></div><div><span>Ended</span><strong>{formatTime(selectedAttempt.endedAt)}</strong></div><div><span>Selected model</span><strong>{selectedAttempt.selectedModel || (live || recorded ? 'Not recorded' : 'None · scripted fixture')}</strong></div><div><span>Skill versions</span><strong>{selectedAttempt.skillVersions.length ? selectedAttempt.skillVersions.join(', ') : live || recorded ? 'Not recorded' : 'None in this fixture'}</strong></div><div><span>Exact tool functions</span><strong>{selectedAttempt.toolCalls.map(tool => `${tool.name}${tool.provider ? ` (${tool.provider})` : ''}`).join(', ') || 'None recorded'}</strong></div></div><AttemptInspection snapshot={snapshot} attemptId={selectedAttempt.id} />{snapshot.inspection && <details className="inline-detail"><summary>Execution and configuration records</summary><ExecutionDiagnostics snapshot={snapshot} attemptId={selectedAttempt.id} /></details>}</details>
            </> : <p>The referenced attempt is unavailable in this saved run. No other attempt was substituted.</p>}
          </section>
        </div>
        {snapshot.inspection && <div className="panel" id="details-technical-records" tabIndex={-1}><SavedAgentSteps snapshot={snapshot} /></div>}
        <section className="panel" id="details-operational-events" tabIndex={-1}>
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
    if (recorded && !live) return <div className="page-stack"><SectionHeading eyebrow="Saved work" title="Previous tasks" description="Live runs and recorded demonstrations remain separate." /><LiveWorkspaceAccess view="history" /><section className="panel"><h3>{coordinationFixture ? 'Recorded Stage 3 coordination review' : 'Recorded Stage 2 review'}</h3><p>{snapshot.mission.question}</p><button className="outline-button" onClick={() => setScreen('overview')}>Open recorded review</button>{comparisonSnapshot && <button className="outline-button" onClick={() => setScreen('compare')}>Compare with its saved parent</button>}<p><a className="inline-link" href={coordinationFixture ? '?recorded=stage2' : '?recorded=stage3'}>{coordinationFixture ? 'Open recorded Stage 2 review' : 'Open recorded Stage 3 coordination review'}</a></p><p><a className="inline-link" href="/">Open bundled examples</a></p></section></div>;
    if (live) return <div className="page-stack"><SectionHeading eyebrow="Durable local history" title="Find an earlier run" description="Search saved runs by question, date, status and requested peer. Opening a run reads its exact saved snapshot." /><SavedRuns key={live.workspaceId || 'legacy'} runs={live.runs} selectedRunId={live.runId} workspaceId={live.workspaceId} workspaceLabel={live.workspaceLabel} agentNames={live.agentNames} onOpen={id => { live.onSelectRun(id); if (!onScreenChange) setScreen('overview'); }} /><section className="panel" aria-label="Recorded coordination examples"><h3>Recorded coordination examples</h3><p>Inspect saved offline tests of peer communication and contribution review. These examples are separate from your live runs; opening them does not start or stop research.</p><p><a className="inline-link" href={`/?recorded=stage3&review=contributions${live.runId ? `&returnRunId=${encodeURIComponent(live.runId)}` : ''}`}>Open shared contribution review</a></p><p><a className="inline-link" href={`/?recorded=stage3${live.runId ? `&returnRunId=${encodeURIComponent(live.runId)}` : ''}`}>Open recorded Stage 3 coordination review</a></p></section></div>;
    return <div className="page-stack">
      <SectionHeading eyebrow="Bundled test data" title="Previous samples" description="Open the saved sample after clearing the active review, or replay its recorded activity from the beginning." />
      <a className="outline-button" href="?recorded=stage2">Open recorded Stage 2 review</a><a className="outline-button" href="?recorded=stage3">Open recorded Stage 3 coordination review</a><a className="outline-button" href="?recorded=stage3&review=contributions">Open shared contribution review</a><p className="screen-intro">This prototype contains bundled sample snapshots only. It does not store or list actual prior agent runs.</p>
      <article className="panel saved-sample"><div className="panel-heading"><div><span className="eyebrow">Research sample</span><h3>{snapshot.mission.question}</h3></div><StatusPill status={sampleMode === 'cleared' ? 'saved' : 'active'} /></div><p>Saved {formatTime(snapshot.capturedAt)} · {snapshot.tasks.length} task records · {snapshot.agents.length} named agents</p><div className="saved-sample-actions"><button className="primary-button" onClick={loadSample}>Load into active review</button><button className="outline-button" onClick={replaySample}>Replay saved activity</button></div><details className="inline-detail"><summary><span className="closed-label">Expand sample details</span><span className="open-label">Hide sample details</span></summary><div className="inline-detail-body"><p>Snapshot {snapshot.snapshotId}. The comparison baseline is another saved sample snapshot; neither is a live or previously executed provider run.</p></div></details></article>
    </div>;
  }

  function renderCompare() {
    return <div className="page-stack">
      <div className="review-toolbar"><SectionHeading eyebrow="Review" title="Compare" description="Compare the earlier and current answers, each with its own saved sources." visiblePurpose /><button className="outline-button" onClick={() => openReviewActivity()}>Inspect the sequence → Activity</button></div>
      <ReportComparison currentSnapshot={snapshot} parentSnapshot={comparisonSnapshot} mode={live || recorded ? 'followup' : 'sample'} />
      {!live && !recorded && comparisonSnapshot && <section className="panel comparison-explainer"><h3>Interpret the difference</h3><p>The baseline run had only A1 and an open request for another adult comparison. The current run also has A2, so its report can describe mixed evidence. Different source coverage explains this change; the display does not show a better model or a treatment effect.</p><button className="text-button" onClick={() => openClaim('claim-benefit-v2')}>Inspect current claim and sources →</button></section>}
    </div>;
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark" aria-hidden="true">✳</span><div><strong>Research Atlas</strong><small>{recorded ? 'Recorded offline test' : live ? 'Local live research' : 'Review prototype'}</small></div></div>
        <nav aria-label="Primary navigation">{activeNavigation.map((item, index) => {
          const active = screen === item.id || (screen === 'diagnostics' && item.id === 'work');
          const disabled = !live && item.group === 'current' && item.id !== 'overview' && ((sampleMode === 'cleared') || ((sampleMode === 'replaying' || sampleMode === 'paused') && item.id !== 'activity'));
          return <div key={item.id} className="nav-entry">{(index === 0 || activeNavigation[index - 1].group !== item.group) && <div className={`sidebar-section-label ${index > 0 ? 'sidebar-divider' : ''}`}>{item.group === 'current' ? 'Selected run' : item.group === 'start' ? 'Start or follow up' : live ? 'Saved runs' : recorded ? 'Previous tasks' : 'Saved examples'}</div>}{item.id === 'activity' && <div className="sidebar-inspection-label">Inspect execution</div>}<div className={`nav-row ${active ? 'active' : ''}`}><button aria-current={active ? 'page' : undefined} className="nav-item" disabled={disabled} onClick={() => setScreen(item.id)}><span className="nav-indicator" /><strong>{item.label}</strong></button><InfoTip label={`${item.label} view`} description={item.description} /></div></div>;
        })}</nav>
      </aside>
      <div className="workspace">
        <header className="topbar"><div><span className="breadcrumb">{currentNav.label}</span><h1>{screen === 'history' ? (live ? 'Saved runs' : recorded ? 'Previous tasks and recorded examples' : 'Previous samples') : screen === 'request' ? 'Start or follow up' : sampleMode === 'cleared' && screen === 'overview' ? 'No task in the active review' : live || recorded ? 'Selected research run' : screen === 'overview' ? 'One research task, from input to output' : 'Selected research run'}</h1></div><div className="topbar-actions">{recorded && <a className="outline-button" href="/">Back to samples</a>}<span className="sample-tag">{recorded ? 'RECORDED OFFLINE TEST' : live ? snapshot.proofKind === 'live_mission_pending' ? 'LIVE · RUN ACCEPTED' : snapshot.proofKind === 'live_model_with_retrieval' ? 'LIVE · RETRIEVED SOURCES' : snapshot.proofKind === 'live_model_on_supplied_evidence' ? 'LIVE · SUPPLIED EVIDENCE' : 'LIVE · PROOF MODE UNAVAILABLE' : 'SCRIPTED SAMPLE'}</span>{!live && !recorded && sampleMode !== 'cleared' && <button className="outline-button" onClick={clearReview}>Clear review</button>}</div></header>
        {screen !== 'request' && screen !== 'history' && <div className="context-bar"><span className="context-summary">{recorded ? `Saved offline records · ${live ? 'connected test viewer; no model execution' : snapshot.inspection?.timingBasis === 'controlled_clock' ? 'controlled test times, not measured latency' : 'no live updates'}` : live ? `${continuingTasks.length ? 'Requested scope complete; additional work continues' : `Saved run status: ${labelStatus(snapshot.mission.status)}`}${live.connectionError ? ' · current host state unknown' : ''}` : sampleMode === 'cleared' ? 'Active review cleared · bundled sample available in Previous samples' : sampleMode === 'replaying' || sampleMode === 'paused' ? 'Saved sample replay · no live agents or backend' : 'Saved sample · no live updates'}</span>{live && <><span className="live-freshness"><span>Snapshot saved: <time dateTime={snapshot.capturedAt}>{formatActivityTimestamp(snapshot.capturedAt)}</time></span><span>Viewer last checked: {live.lastCheckedAt ? <time dateTime={live.lastCheckedAt}>{formatActivityTimestamp(live.lastCheckedAt)}</time> : 'Not yet checked'}</span></span><details className="technical-run-details"><summary>Technical run details</summary><dl><div><dt>Run ID</dt><dd>{live.runId || 'Unavailable'}</dd></div><div><dt>Snapshot</dt><dd>{snapshot.snapshotId}</dd></div><div><dt>Open task records</dt><dd>{currentTaskCount} · includes the overall request and agent assignments</dd></div>{live.viewerMetrics && <><div><dt>Viewer update channel</dt><dd>{live.viewerMetrics.mode === "push" ? "Host notifications with periodic verification" : "Polling fallback"}</dd></div><div><dt>Viewer reads / failed / cancelled</dt><dd>{live.viewerMetrics.reads} / {live.viewerMetrics.failures} / {live.viewerMetrics.cancellations}</dd></div><div><dt>Viewer commands / response errors</dt><dd>{live.viewerMetrics.commands} / {live.viewerMetrics.commandErrors}</dd></div><div><dt>Last command response</dt><dd>{live.viewerMetrics.lastCommandDurationMs === null ? "Not measured" : `${Math.round(live.viewerMetrics.lastCommandDurationMs)} ms`} · acceptance response, not research completion</dd></div><div><dt>Last viewer read</dt><dd>{live.viewerMetrics.lastDurationMs === null ? "Not measured" : `${Math.round(live.viewerMetrics.lastDurationMs)} ms`}</dd></div></>}<div><dt>Retrieval routes</dt><dd>{live.retrievalRoutes ? live.retrievalRoutes.length ? live.retrievalRoutes.join(', ') : 'Supplied evidence only' : 'Not recorded by this host'}</dd></div></dl></details></>}</div>}
        {screen !== 'request' && screen !== 'history' && sampleMode !== 'cleared' && <RunStatusSummary snapshot={snapshot} runId={live?.runId} run={live?.runs.find(run => run.id === live.runId)} runs={live?.runs} onParent={live?.onSelectRun} waitingReason={live?.runs.find(run => run.id === live.runId)?.waitingReason} disconnected={!!live?.connectionError} onCommunication={screen === 'communication' || screen === 'overview' ? undefined : () => setScreen('communication')} />}
        {(screen === 'request' || screen === 'history') && <div className="workspace-scope">Workspace-wide · {screen === 'history' ? 'Saved runs and examples' : 'Choose a new run or an exact saved follow-up'}</div>}
        {screen !== 'request' && screen !== 'history' && continuingTasks.length > 0 && <div className="scope-continuation" role="status"><strong>Requested scope complete; additional work continues in this saved snapshot.</strong><span>Current task records:</span>{continuingTasks.map(task => <button key={`${task.id}:${task.version ?? 'unknown'}`} type="button" className="inline-link" onClick={() => openTask(task.id, { version: task.version ?? undefined })}>{task.title} · {task.status}{task.version ? ` · v${task.version}` : ' · version not recorded'}</button>)}</div>}
        {live?.connectionError && <div className="live-error live-reconnect-error" role="alert"><span>Viewer refresh failed: {live.connectionError}. Showing the last saved progress; current host state is unknown.</span>{isTauri() && <span className="tiny-note">In the Mac app, choose File → Reconnect saved workspace. This reopens Saved runs without starting research.</span>}</div>}
        {live?.error && <div className="live-error" role="alert">Live status: {live.error}. Inspect the saved snapshot and Details for the affected work.</div>}
        {['activity', 'communication', 'evidence', 'compare', 'work', 'diagnostics'].includes(screen) && <div className="review-return"><button className="inline-link" onClick={() => setScreen('overview')}>← Back to Research review</button><span>Same selected run</span></div>}
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
