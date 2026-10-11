/* A short map of saved work before the chronological, exact-record Activity list. */
import type { ResearchSnapshot } from './generated/contracts';
import { workGroupLabel } from './WorkOverview';
import './ActivityWorkOverview.css';

const explanations: Record<string, string> = {
  'Source reading and checks': 'Searches, saved results and source passages',
  'Task context and tool lookups': 'Saved task information and available tools',
  'Peer requests and reviews': 'Requests to peers and their reviews',
  'System preparation and pauses': 'Prepared or paused work, not new research findings',
  'Research contributions': 'Submitted work; inspect each result for acceptance',
};

export function ActivityWorkOverview({ snapshot, earlierAttemptIds, runScopeKnown, selectedGroup, onSelectGroup }: {
  snapshot: ResearchSnapshot;
  earlierAttemptIds: ReadonlySet<string>;
  runScopeKnown: boolean;
  selectedGroup: string | null;
  onSelectGroup: (group: string) => void;
}) {
  const attempts = snapshot.attempts.filter(attempt => !earlierAttemptIds.has(attempt.id));
  if (!attempts.length) return null;
  const groups = new Map<string, number>();
  for (const attempt of attempts) {
    const label = workGroupLabel(snapshot, attempt);
    groups.set(label, (groups.get(label) ?? 0) + 1);
  }
  return <section className="activity-work-overview" aria-label="Saved work by action">
    <div className="activity-work-overview-heading"><h3>Where the saved work went</h3><p>{runScopeKnown ? `${attempts.length} saved steps in this run` : `${attempts.length} saved steps in this view; some may belong to an earlier run`}. These categories organize saved actions; they do not measure active tool time or prove a research result.</p></div>
    <div className="activity-work-overview-grid">{[...groups].map(([label, count]) => <button key={label} type="button" className="activity-work-category" aria-pressed={selectedGroup === label} aria-label={`${label}: show ${count} saved ${count === 1 ? 'step' : 'steps'} in time order`} onClick={() => onSelectGroup(label)}>
      <span className="activity-work-category-head"><strong>{label}</strong><b>{count}</b></span>
      <span className="activity-work-category-explanation">{explanations[label] ?? 'Other saved work actions'}</span>
      <span className="activity-work-category-meter" aria-hidden="true"><i style={{ width: `${100 * count / attempts.length}%` }} /></span>
      <span className="activity-work-category-action">Show exact steps in time order ↓</span>
    </button>)}</div>
    <p className="activity-work-overview-boundary">Messages and host events remain available in the Activity filters below. Every saved step remains in the exact chronological list.</p>
  </section>;
}
