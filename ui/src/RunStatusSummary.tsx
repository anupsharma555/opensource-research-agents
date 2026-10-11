/* Keep the selected run's recorded work, messages and report status consistent
   across tabs. Missing inspection evidence stays unknown rather than zero. */
import { useEffect, useRef } from 'react';
import type { ResearchSnapshot } from './generated/contracts';
import { activityOverview } from './activityPresentation';
import { TermHelp } from './TermHelp';
import type { LiveRunSummary } from './live';
import { formatActivityTimestamp } from './ActivityRecordDetails';
import { savedDuration } from './savedDuration';
import './RunStatusSummary.css';

export function RunStatusSummary({ snapshot, waitingReason, disconnected = false, onCommunication, run, runId, runs, onParent }: {
  run?: LiveRunSummary; runs?: LiveRunSummary[]; runId?: string | null; onParent?: (id: string) => void;
  snapshot: ResearchSnapshot; waitingReason?: string | null; disconnected?: boolean; onCommunication?: () => void;
}) {
  const headerRef = useRef<HTMLElement>(null);
  useEffect(() => {
    const header = headerRef.current;
    if (!header) return;
    const root = document.documentElement;
    const previous = root.style.getPropertyValue('--atlas-run-header-height');
    const measure = () => root.style.setProperty('--atlas-run-header-height', `${Math.ceil(header.getBoundingClientRect().height)}px`);
    measure();
    const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(measure);
    observer?.observe(header);
    window.addEventListener('resize', measure);
    return () => {
      observer?.disconnect();
      window.removeEventListener('resize', measure);
      if (previous) root.style.setProperty('--atlas-run-header-height', previous);
      else root.style.removeProperty('--atlas-run-header-height');
    };
  }, []);
  const overview = activityOverview(snapshot);
  const messages = snapshot.inspection?.records.filter(record => record.reference.kind === 'mail_message') ?? [];
  const noMessages = snapshot.messages.length === 0 && messages.length === 0 && overview.peerRequests === 0;
  const paused = !!waitingReason?.trim() && waitingReason.split(';').filter(Boolean).every(reason => /^(Primary|Backup) credential paused by operator$/.test(reason.trim()));
  const callState = paused ? 'Model calls paused' : null;
  const createdAt = run?.createdAt && Number.isFinite(Date.parse(run.createdAt)) ? run.createdAt : null;
  const savedAt = Number.isFinite(Date.parse(snapshot.capturedAt)) ? snapshot.capturedAt : null;
  const runStart = run ? createdAt : snapshot.mission.startedAt;
  const completedAt = run?.status === 'completed' && run.completedAt && savedAt && Date.parse(run.completedAt) <= Date.parse(savedAt)
    && savedDuration(runStart, run.completedAt) !== null ? run.completedAt : null;
  const duration = savedDuration(runStart, completedAt || savedAt);
  const date = createdAt || savedAt;
  const identity = runId || snapshot.snapshotId;
  const orderedRuns = runs?.length && runs.every(item => item.createdAt && Number.isFinite(Date.parse(item.createdAt)))
    ? [...runs].sort((a, b) => Date.parse(a.createdAt!) - Date.parse(b.createdAt!) || a.id.localeCompare(b.id)) : [];
  const runNumber = orderedRuns.findIndex(item => item.id === runId) + 1;
  const title = run?.title?.trim() || run?.question || snapshot.mission.question;
  return <section ref={headerRef} className="run-progress-summary" aria-label="This run at a glance">
    <div className="selected-run-identity">
      <span className="selected-run-label">{runNumber ? `Run ${runNumber} · Selected run` : 'Selected run'}</span>
      <span className="selected-run-title-label">{run?.title?.trim() ? 'Run title' : 'Research request'}</span>
      <strong className="selected-run-title" title={title}>{title}</strong>
      <span className="selected-run-time">{date ? <>{createdAt ? 'Run started: ' : 'Snapshot saved: '}<time dateTime={date}>{formatActivityTimestamp(date)}</time></> : 'Time unavailable'} · <span>{disconnected ? 'Saved view · connection unavailable' : callState || snapshot.mission.status.replaceAll('_', ' ')}</span></span>
      {duration && <span className="selected-run-duration">{completedAt ? 'Run duration' : 'Run elapsed to saved view'}: <strong>{duration}</strong> · includes pauses</span>}
      <div className="selected-run-reference"><span>{runId ? 'Run ID' : 'Snapshot ID'}</span><code>{identity}</code></div>
      <details className="run-number-help"><summary>About run and task numbers</summary><span>A run is the saved research request and its continuing work. Tasks are assignments within it; steps are individual saved actions. A pause or later step does not create a new run. Run numbers follow the saved history’s creation order. {snapshot.tasks.every(task => task.displayNumber != null) ? 'Task numbers follow saved task creation order and remain the same after reopening this run.' : 'Older task records without saved numbers use labels for this open view; those labels may differ after reopening.'} These display numbers do not replace exact saved IDs.</span></details>
      {snapshot.parentSnapshotId && <span>Follow-up on an earlier run <TermHelp term="earlier run" /> {run?.parentRunId && onParent && <button type="button" className="inline-link" onClick={() => onParent(run.parentRunId!)}>Open earlier run →</button>}</span>}
    </div>
    {onCommunication && <button type="button" className="run-communication-button" onClick={onCommunication}>{noMessages ? 'No peer messages recorded' : 'Open recorded peer communication'} <span aria-hidden="true">→</span></button>}
    <span>{overview.acceptedReports === null ? 'Report acceptance not fully available' : overview.acceptedReports === 0 ? 'No accepted report yet' : `${overview.acceptedReports} accepted report${overview.acceptedReports === 1 ? '' : 's'}`} <TermHelp term="accepted report" /></span>
  </section>;
}
