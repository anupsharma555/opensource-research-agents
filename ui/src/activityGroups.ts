/* Group saved activity by its exact assignment and verified run origin.
   Records without an assignment remain run-wide; parent data is never merged. */
import type { ResearchSnapshot } from './generated/contracts';
import type { ActivityItem } from './ActivityRecordDetails';
import { classifyAttemptLineage } from './attemptLineage';

export type ActivityOrigin = 'current' | 'earlier' | 'unknown';
export type ActivityGroup = { key: string; taskId?: string; origin: ActivityOrigin; items: ActivityItem[] };

export function exactActivityParent(snapshot: ResearchSnapshot, parent?: ResearchSnapshot) {
  return snapshot.parentSnapshotId && snapshot.parentSnapshotId === parent?.snapshotId ? parent : undefined;
}

function originResolver(snapshot: ResearchSnapshot, parent?: ResearchSnapshot) {
  const exactParent = exactActivityParent(snapshot, parent);
  const lineage = exactParent ? classifyAttemptLineage(snapshot, exactParent) : undefined;
  const messages = new Map(snapshot.messages.map(record => [record.id, record]));
  const events = new Map(snapshot.events.map(record => [record.id, record]));
  const parentMessages = new Map(exactParent?.messages.map(record => [record.id, record]));
  const parentEvents = new Map(exactParent?.events.map(record => [record.id, record]));
  return (item: ActivityItem): ActivityOrigin => {
    if (!exactParent) return !snapshot.parentSnapshotId ? 'current' : 'unknown';
    if (item.attemptId) return lineage?.get(item.attemptId) === 'inherited' ? 'earlier' : lineage?.has(item.attemptId) ? 'current' : 'unknown';
    const current = item.category === 'communication' ? messages.get(item.id) : events.get(item.id);
    const previous = item.category === 'communication' ? parentMessages.get(item.id) : parentEvents.get(item.id);
    if (!current) return 'unknown';
    if (!previous) return 'current';
    // Compare saved fields, not titles, timestamps alone or agent names.
    const same = Object.keys({ ...current, ...previous }).every(key =>
      JSON.stringify(current[key as keyof typeof current]) === JSON.stringify(previous[key as keyof typeof previous]));
    return same ? 'earlier' : item.category === 'communication' ? 'current' : 'unknown';
  };
}

export function activityOrigin(snapshot: ResearchSnapshot, item: ActivityItem, parent?: ResearchSnapshot): ActivityOrigin {
  return originResolver(snapshot, parent)(item);
}

export function groupActivity(items: ActivityItem[], snapshot: ResearchSnapshot, parent?: ResearchSnapshot): ActivityGroup[] {
  const groups = new Map<string, ActivityGroup>();
  const originFor = originResolver(snapshot, parent);
  for (const item of items) {
    const origin = originFor(item);
    const key = JSON.stringify([origin, item.taskId ?? null]);
    const group = groups.get(key) ?? { key, taskId: item.taskId, origin, items: [] };
    group.items.push(item);
    groups.set(key, group);
  }
  return [...groups.values()].sort((a, b) => ['current', 'unknown', 'earlier'].indexOf(a.origin) - ['current', 'unknown', 'earlier'].indexOf(b.origin));
}

/** Prefer saved creation-order numbers; retain viewer-only labels for older snapshots. */
export function assignTaskNumbers(snapshot: ResearchSnapshot, previous: ReadonlyMap<string, number> = new Map()) {
  const numbers = new Map(previous);
  for (const task of snapshot.tasks) {
    if (task.displayNumber != null) numbers.set(task.id, task.displayNumber);
  }
  const savedNumbers = new Map(snapshot.tasks.filter(task => task.displayNumber != null)
    .map(task => [task.displayNumber!, task.id]));
  for (const [id, number] of numbers) {
    if (savedNumbers.has(number) && savedNumbers.get(number) !== id) numbers.delete(id);
  }
  let next = Math.max(0, ...numbers.values()) + 1;
  for (const id of [...new Set(snapshot.tasks.map(task => task.id))].sort()) {
    if (!numbers.has(id)) numbers.set(id, next++);
  }
  return numbers;
}

export function taskIdentity(snapshot: ResearchSnapshot, taskId: string, numbers = assignTaskNumbers(snapshot)) {
  const number = numbers.get(taskId);
  const agentIds = [...new Set(snapshot.attempts.filter(attempt => attempt.taskId === taskId).map(attempt => attempt.agentId))];
  return { label: number === undefined ? 'Unlisted task' : `Task ${number}`, agentIds };
}
