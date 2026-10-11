/* Distinguish unchanged parent attempts from work added or revised in a follow-up.
   Exact saved attempt versions take precedence over projection metadata. Older
   snapshots fall back to their recorded state and input/configuration references. */
import type { AttemptView, ResearchSnapshot, SavedRecordRef } from './generated/contracts';

export type AttemptLineage = 'new' | 'revised' | 'inherited';

function attemptState(attempt: AttemptView) {
  return JSON.stringify({
    id: attempt.id, agentId: attempt.agentId, taskId: attempt.taskId,
    status: attempt.status, startedAt: attempt.startedAt, endedAt: attempt.endedAt ?? null,
    resultSummary: attempt.resultSummary ?? null, selectedModel: attempt.selectedModel ?? null,
    skillVersions: attempt.skillVersions ?? [],
    toolCalls: (attempt.toolCalls ?? []).map(tool => [tool.name, tool.provider ?? null, tool.status]),
    returnedContent: [attempt.returnedReasoning?.availability, attempt.returnedReasoning?.text ?? null],
  });
}

function savedVersion(snapshot: ResearchSnapshot, attempt: AttemptView) {
  // Inspection can also contain older versions reached through linked records.
  // Resolve by the displayed attempt's data, never by record ordering or max version.
  const state = attemptState(attempt);
  const versions = new Set(snapshot.inspection?.records.filter(record =>
    record.reference.kind === 'attempt' && record.reference.id === attempt.id
    && attemptState(record.data as unknown as AttemptView) === state,
  ).map(record => record.reference.version));
  return versions.size === 1 ? [...versions][0] : undefined;
}

const referenceKey = (reference?: SavedRecordRef | null) => reference
  ? `${reference.kind}:${reference.id}:${reference.version}` : null;

function attemptContext(snapshot: ResearchSnapshot, attemptId: string) {
  const context = snapshot.inspection?.attempts.find(item => item.attemptId === attemptId);
  return JSON.stringify({
    instance: referenceKey(context?.instanceReference),
    configuration: referenceKey(context?.configurationReference),
    inputs: (context?.inputReferences ?? []).map(referenceKey).sort(),
    outputs: (context?.acceptedOutputReferences ?? []).map(referenceKey).sort(),
  });
}

export function classifyAttemptLineage(snapshot: ResearchSnapshot, parent?: ResearchSnapshot): Map<string, AttemptLineage> {
  const parents = new Map(parent?.attempts.map(attempt => [attempt.id, attempt]));
  return new Map(snapshot.attempts.map(attempt => {
    const previous = parents.get(attempt.id);
    if (!parent || !previous) return [attempt.id, 'new'];
    const currentVersion = savedVersion(snapshot, attempt);
    const parentVersion = savedVersion(parent, previous);
    const unchanged = currentVersion !== undefined && parentVersion !== undefined
      ? currentVersion === parentVersion
      : attemptState(attempt) === attemptState(previous)
        && attemptContext(snapshot, attempt.id) === attemptContext(parent, previous.id);
    return [attempt.id, unchanged ? 'inherited' : 'revised'];
  }));
}

/** Keep step numbers stable across filters, tabs and the earlier-run toggle. */
export function workStepNumbers(snapshot: ResearchSnapshot, earlierIds: ReadonlySet<string> = new Set()): Map<string, number> {
  let current = 0;
  let earlier = 0;
  const ordered = [...snapshot.attempts].sort((a, b) => {
    const elapsed = Date.parse(a.startedAt) - Date.parse(b.startedAt);
    return (Number.isFinite(elapsed) ? elapsed : a.startedAt.localeCompare(b.startedAt)) || a.id.localeCompare(b.id);
  });
  return new Map(ordered.map(attempt => [attempt.id, earlierIds.has(attempt.id) ? ++earlier : ++current]));
}
