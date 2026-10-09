/* Keep revised work visible without turning unchanged history into new work. */
import { describe, expect, it } from 'vitest';
import { classifyAttemptLineage } from './attemptLineage';
import type { ResearchSnapshot } from './generated/contracts';
import parentData from '../public/stage2-recorded/parent.json';
import childData from '../public/stage2-recorded/child.json';

const parent = parentData as ResearchSnapshot;
const child = childData as ResearchSnapshot;

describe('attempt lineage', () => {
  it('uses exact saved versions in the actual recorded exports, including linked older versions', () => {
    const lineage = classifyAttemptLineage(child, parent);
    expect(lineage.get('attempt-a1')).toBe('inherited');
    expect(lineage.get('attempt-a2')).toBe('revised');
    expect(lineage.get('attempt-synthesis')).toBe('revised');
    expect(lineage.get('attempt-critique')).toBe('new');
    // Record ordering is not authority; the child also carries the older v1 records.
    const reordered = structuredClone(child);
    reordered.inspection!.records.reverse();
    expect(classifyAttemptLineage(reordered, parent)).toEqual(lineage);
  });

  it('keeps older unchanged snapshots inherited despite capture time and call-record changes', () => {
    const before = structuredClone(parent);
    before.inspection!.records = [];
    const after = structuredClone(before);
    after.capturedAt = '2026-10-08T20:00:00Z';
    after.inspection!.attempts[0].callReferences = [{ kind: 'call', id: 'other-projection-call', version: 1 }];
    expect(classifyAttemptLineage(after, before).get('attempt-a1')).toBe('inherited');
  });

  it.each(['status', 'result', 'input', 'configuration', 'model', 'skill'] as const)('detects an older snapshot with changed %s', field => {
    const before = structuredClone(parent);
    before.inspection!.records = [];
    const after = structuredClone(before);
    const attempt = after.attempts.find(item => item.id === 'attempt-a1')!;
    const context = after.inspection!.attempts.find(item => item.attemptId === attempt.id)!;
    if (field === 'status') attempt.status = 'failed';
    if (field === 'result') attempt.resultSummary = 'Updated saved result.';
    if (field === 'input') context.inputReferences = [{ kind: 'source_copy', id: 'additional-input', version: 1 }];
    if (field === 'configuration') context.configurationReference = { ...context.configurationReference!, version: 2 };
    if (field === 'model') attempt.selectedModel = 'fixture-model';
    if (field === 'skill') attempt.skillVersions = ['fixture-skill@2'];
    expect(classifyAttemptLineage(after, before).get(attempt.id)).toBe('revised');
  });
});
