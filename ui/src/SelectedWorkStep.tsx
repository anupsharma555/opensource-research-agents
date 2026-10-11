/* Present the selected saved workflow step; expanders retain its exact task,
   result and inspection records without treating every step as a model call. */
import { useState } from 'react';
import type { ResearchSnapshot } from './generated/contracts';
import { formatActivityTimestamp } from './ActivityRecordDetails';
import { AttemptInspection, RecordGroup } from './InspectionDetails';
import { RecordedText } from './RecordedText';
import { LiveCallInspector } from './LiveCallInspector';
import './SelectedWorkStep.css';
import { stepPresentation } from './stepPresentation';

export function SelectedWorkStep({ snapshot, taskId, attemptId, runId, onOpenAttempt, onOpenTask, inspectImmediately = false }: {
  snapshot: ResearchSnapshot; taskId: string; attemptId?: string; runId?: string | null;
  onOpenAttempt: (id: string) => void; onOpenTask: (id: string) => void;
  inspectImmediately?: boolean;
}) {
  const [expandedIdentity, setExpandedIdentity] = useState<string | null>(null);
  const identity = `${runId ?? ''}:${taskId}:${attemptId ?? ''}`;
  const expanded = expandedIdentity === identity;
  const task = snapshot.tasks.find(item => item.id === taskId);
  const attempt = snapshot.attempts.find(item => item.id === attemptId && item.taskId === taskId);
  if (!task) return null;
  const detail = snapshot.inspection?.attempts.find(item => item.attemptId === attempt?.id);
  const presentation = attempt ? stepPresentation(snapshot, attempt) : null;
  const result = presentation?.summary || 'No result summary was saved for this selected attempt.';
  return <div className="selected-work-step">
    {attempt ? <>
      <div className="step-facts"><span><strong>{presentation?.actorKind === 'system' ? 'System event' : 'Result'}</strong>{presentation?.outcomeLabel || attempt.status.replaceAll('_', ' ')}</span><span><strong>Started</strong><time dateTime={attempt.startedAt}>{formatActivityTimestamp(attempt.startedAt)}</time></span><span><strong>Finished</strong>{attempt.endedAt ? <time dateTime={attempt.endedAt}>{formatActivityTimestamp(attempt.endedAt)}</time> : 'Not recorded'}</span></div>
      {attempt.toolCalls.length > 0 && <p className="step-action"><strong>Action:</strong> {presentation?.title}</p>}
      {attempt.endedAt && <p className="step-elapsed">Elapsed time includes any preparation, waiting and execution. It is not the tool's execution time.</p>}
      <div className="step-result-preview"><RecordedText text={result} /></div>
      {inspectImmediately ? <div className="step-inspector">{runId
          ? <LiveCallInspector runId={runId} attemptId={attempt.id} snapshotId={snapshot.snapshotId} snapshot={snapshot} onOpenAttempt={onOpenAttempt} />
          : <div className="step-saved-records"><RecordGroup snapshot={snapshot} title="Recorded calls" references={detail?.callReferences ?? []} /><RecordGroup snapshot={snapshot} title="Saved inputs" references={detail?.inputReferences ?? []} /><RecordGroup snapshot={snapshot} title="Accepted outputs" references={detail?.acceptedOutputReferences ?? []} /></div>}</div>
        : <details key={identity} className="inline-detail step-inspector" open={expanded} onToggle={event => { if (event.target === event.currentTarget) setExpandedIdentity(event.currentTarget.open ? identity : null); }}>
        <summary>Inspect this step</summary>
        {expanded && (runId
          ? <LiveCallInspector runId={runId} attemptId={attempt.id} snapshotId={snapshot.snapshotId} snapshot={snapshot} onOpenAttempt={onOpenAttempt} />
          : <div className="step-saved-records">
            <RecordGroup snapshot={snapshot} title="Recorded calls" references={detail?.callReferences ?? []} />
            <RecordGroup snapshot={snapshot} title="Saved inputs" references={detail?.inputReferences ?? []} />
            <RecordGroup snapshot={snapshot} title="Accepted outputs" references={detail?.acceptedOutputReferences ?? []} />
          </div>)}
      </details>}
      <details className="inline-detail"><summary>Technical records and current task state</summary><p><strong>Internal tool name:</strong> {attempt.toolCalls.map(tool => tool.name).join(' → ') || 'No tool action recorded'}</p><AttemptInspection snapshot={snapshot} attemptId={attempt.id} summaryLabel="Agent settings and recovery" configurationOnly />
      <button className="inline-link" onClick={() => onOpenAttempt(attempt.id)}>Open selected attempt in Details →</button>
      {task.status === 'waiting' && <p className="step-wait"><strong>Current task wait, as of {formatActivityTimestamp(snapshot.capturedAt)}:</strong> {task.waitingReason || 'Reason not recorded'}. This is the task’s current state, not an explanation of an earlier step.{task.waitingUntil && <> Eligible after {formatActivityTimestamp(task.waitingUntil)}.</>}</p>}<p><strong>Task timing expectation:</strong> {task.latencyClass?.replaceAll('_', ' ') || 'Not yet estimated'}{task.latencyBasis ? ` · ${task.latencyBasis}` : ''}</p></details>
    </> : <p>No agent attempt was recorded for this task. Its task or request record is still available.</p>}
    <button className="inline-link" onClick={() => onOpenTask(task.id)}>Open task in Details →</button>
  </div>;
}
