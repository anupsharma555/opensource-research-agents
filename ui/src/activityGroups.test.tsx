/* Verify exact task grouping and saved-parent attribution without inventing history. */
import { describe, expect, it } from 'vitest';
import { activityOrigin, groupActivity, taskIdentity, assignTaskNumbers } from './activityGroups';
import type { ActivityItem } from './ActivityRecordDetails';
import type { ResearchSnapshot } from './generated/contracts';
import parentData from '../public/stage2-recorded/parent.json';
import childData from '../public/stage2-recorded/child.json';
const parent = parentData as ResearchSnapshot;
const child = childData as ResearchSnapshot;
const item = (id: string, taskId?: string): ActivityItem => ({ id, attemptId: id, taskId, category: 'agent', title: 'Work', summary: '', time: '', timeLabel: 'Saved' });

describe('activity assignment boundaries', () => {
  it('uses saved task numbers across updates and reopening, without colliding with legacy labels', () => {
    const snapshot = structuredClone(child);
    snapshot.tasks = snapshot.tasks.slice(0, 2).map((task, i) => ({ ...task, id: i ? 'c-task' : 'b-task', displayNumber: i + 1 }));
    const prior = assignTaskNumbers(snapshot);
    snapshot.tasks.push({ ...snapshot.tasks[0], id: 'a-new-task', displayNumber: 3 });
    expect([...assignTaskNumbers(snapshot, prior)]).toEqual([...assignTaskNumbers(snapshot)]);
    expect(taskIdentity(snapshot, 'b-task', assignTaskNumbers(snapshot)).label).toBe('Task 1');
    snapshot.tasks.push({ ...snapshot.tasks[0], id: 'legacy-task', displayNumber: null });
    expect(taskIdentity(snapshot, 'legacy-task', assignTaskNumbers(snapshot)).label).toBe('Task 4');
    const olderLabels = new Map([['b-task', 2], ['legacy-task', 1]]);
    const updated = assignTaskNumbers(snapshot, olderLabels);
    expect(new Set(updated.values()).size).toBe(updated.size);
    expect(updated.get('b-task')).toBe(1);
  });

  it('separates unchanged earlier work from revised and new work using the exact parent', () => {
    const rows = child.attempts.map(attempt => item(attempt.id, attempt.taskId));
    const groups = groupActivity(rows, child, parent);
    expect(groups.flatMap(group => group.items)).toHaveLength(rows.length);
    expect(activityOrigin(child, item('attempt-a1'), parent)).toBe('earlier');
    expect(activityOrigin(child, item('attempt-a2'), parent)).toBe('current');
    expect(activityOrigin(child, item('attempt-critique'), parent)).toBe('current');
    expect(activityOrigin(child, item('attempt-a1'))).toBe('unknown');
    expect(activityOrigin(child, item('attempt-a1'), { ...parent, snapshotId: 'unrelated' })).toBe('unknown');
  });
  it('keeps duplicated titles separate and never assigns run-wide events by timing', () => {
    const snapshot = structuredClone(child);
    snapshot.parentSnapshotId = null;
    snapshot.tasks[1].title = snapshot.tasks[0].title;
    const rows = [item('one', snapshot.tasks[0].id), item('two', snapshot.tasks[1].id), { ...item('host'), attemptId: undefined, category: 'event' as const }];
    expect(groupActivity(rows, snapshot).map(group => group.taskId)).toEqual([snapshot.tasks[0].id, snapshot.tasks[1].id, undefined]);
    const identity = taskIdentity(snapshot, snapshot.tasks[0].id);
    snapshot.tasks.reverse();
    expect(taskIdentity(snapshot, snapshot.tasks.find(task => task.id === rows[0].taskId)!.id)).toEqual(identity);
    const numbers = assignTaskNumbers(snapshot);
    const previous = taskIdentity(snapshot, snapshot.tasks[0].id, numbers).label;
    snapshot.tasks.push({ ...snapshot.tasks[0], id: 'aaa-newly-recorded-task' });
    expect(taskIdentity(snapshot, snapshot.tasks[0].id, assignTaskNumbers(snapshot, numbers)).label).toBe(previous);
    snapshot.tasks[0].id = 'alpha-same-ending';
    snapshot.tasks[1].id = 'bravo-same-ending';
    expect(taskIdentity(snapshot, snapshot.tasks[0].id).label).not.toBe(taskIdentity(snapshot, snapshot.tasks[1].id).label);
  });
  it('requires saved message/event equality, keeping changed event origin unknown', () => {
    const snapshot = structuredClone(parent);
    snapshot.parentSnapshotId = parent.snapshotId;
    const message = snapshot.messages[0];
    expect(message).toBeDefined();
    {
      const row: ActivityItem = { ...item(message.id, message.taskId), attemptId: undefined, category: 'communication' };
      expect(activityOrigin(snapshot, row, parent)).toBe('earlier');
      message.summary += ' Updated';
      expect(activityOrigin(snapshot, row, parent)).toBe('current');
    }
    const event = snapshot.events[0];
    expect(event).toBeDefined();
    const row: ActivityItem = { ...item(event.id), attemptId: undefined, category: 'event' };
    expect(activityOrigin(snapshot, row, parent)).toBe('earlier');
    event.summary += ' Changed';
    expect(activityOrigin(snapshot, row, parent)).toBe('unknown');
  });
});
