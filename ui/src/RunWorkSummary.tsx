/* Summarize the saved work in this run before opening individual steps.
   Counts and latest actions describe records, not scientific progress. */
import type { ResearchSnapshot } from './generated/contracts';
import { stepPresentation } from './stepPresentation';
import { formatActivityTimestamp } from './ActivityRecordDetails';
import { AgentBadge } from './AgentBadge';
import { WorkOverview } from './WorkOverview';
import './RunWorkSummary.css';

export function RunWorkSummary({ snapshot, earlierAttemptIds, taskLabel, onActivity, onOpenGroup, runScopeKnown = !snapshot.parentSnapshotId }: {
  snapshot: ResearchSnapshot; earlierAttemptIds: ReadonlySet<string>; taskLabel: (id: string) => string;
  onActivity: () => void;
  onOpenGroup: (group: string) => void;
  runScopeKnown?: boolean;
}) {
  const attempts = snapshot.attempts.filter(item => !earlierAttemptIds.has(item.id));
  const tasks = snapshot.tasks.filter(task => attempts.some(attempt => attempt.taskId === task.id));

  return <div className="run-work-summary">
    <p className="run-work-count"><strong>{attempts.length} saved steps {runScopeKnown ? 'across this run' : 'in this saved view'}</strong> · {new Set(attempts.map(item => item.agentId)).size} agents</p>
    {!runScopeKnown && <p className="shared-request-explainer">The exact earlier-run snapshot is unavailable. Some steps may be inherited; their run origin is not confirmed.</p>}
    <WorkOverview snapshot={snapshot} earlierAttemptIds={earlierAttemptIds} onOpenActivity={onOpenGroup} />
    {tasks.length > 0 && <details className="inline-detail agent-work-status"><summary>Agents’ current status and assignments</summary><div className="review-task-cards">{tasks.map(task => {
      const steps = attempts.filter(item => item.taskId === task.id).sort((a, b) => a.startedAt.localeCompare(b.startedAt) || a.id.localeCompare(b.id));
      const latest = steps.at(-1)!;
      const actors = [...new Set(steps.map(item => item.agentId))];
      const presentation = stepPresentation(snapshot, latest);
      return <article key={task.id} className="review-task-card">
        <header><strong className="review-task-title">{actors.map((id, index) => <span key={id}>{index > 0 && ' & '}<AgentBadge name={snapshot.agents.find(item => item.id === id)?.name} agentId={id} /></span>)}’s work</strong><span>{task.status.replaceAll('_', ' ')}</span></header>
        {task.title === snapshot.mission.question
          ? <p className="review-task-purpose">Working on the shared request above.</p>
          : <p className="review-task-purpose">{task.title.length > 180 ? `${task.title.slice(0, 180)}…` : task.title}</p>}
        <p><strong>Most recently started step</strong><br />{presentation.title}</p>
        <time className="tiny-note" dateTime={latest.startedAt}>Started {formatActivityTimestamp(latest.startedAt)}</time>
        {task.waitingReason && <p className="review-task-wait"><strong>Now:</strong> {task.waitingReason}</p>}
        {task.title !== snapshot.mission.question && task.title.length > 180 && <details className="inline-detail"><summary>Full task assignment</summary><p>{task.title}</p></details>}
        <details className="inline-detail"><summary>Saved assignment reference</summary><p>{taskLabel(task.id)} · {task.id}. {runScopeKnown ? 'This assignment record belongs to the same research run.' : 'Its run origin needs the exact earlier-run snapshot.'}</p></details>
        <footer><span>{steps.length} saved steps</span></footer>
      </article>;
    })}</div></details>}
    {!tasks.length && <p>No agent work step has been saved for this run yet.</p>}
    <div className="review-work-links"><button className="outline-button" onClick={onActivity}>Follow activity in time order →</button></div>
  </div>;
}
