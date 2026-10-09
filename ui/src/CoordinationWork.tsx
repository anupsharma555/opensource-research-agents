/* Explain saved coordinator work without scheduling it or promoting diagnostics to findings.
   Every displayed relationship comes from an exact saved reference or identity field. */
import type { ResearchSnapshot, SavedRecordRef } from './generated/contracts';
import { AgentBadge } from './AgentBadge';
import { RecordGroup, referenceKey, referenceLabel } from './InspectionDetails';

type Saved = NonNullable<ResearchSnapshot['inspection']>['records'][number];
type Data = Record<string, unknown>;

function data(value: unknown): Data | undefined {
  return value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Data : undefined;
}

function string(value: unknown): string | undefined {
  return typeof value === 'string' && value.trim() ? value : undefined;
}

function seconds(value: unknown): string | undefined {
  return typeof value === 'number' && Number.isFinite(value) && value >= 0 ? `${value.toFixed(3)} s` : undefined;
}

function reference(value: unknown): SavedRecordRef | undefined {
  const item = data(value);
  return item && typeof item.kind === 'string' && typeof item.id === 'string' && Number.isInteger(item.version) && Number(item.version) > 0
    ? { kind: item.kind, id: item.id, version: Number(item.version) } : undefined;
}

function references(value: unknown): SavedRecordRef[] {
  return Array.isArray(value) ? value.map(reference).filter((item): item is SavedRecordRef => !!item) : [];
}

function latestIncluded(records: Saved[], kind: string): Saved[] {
  const latest = new Map<string, Saved>();
  for (const record of records) {
    if (record.reference.kind !== kind) continue;
    const previous = latest.get(record.reference.id);
    if (!previous || record.reference.version > previous.reference.version) latest.set(record.reference.id, record);
  }
  return [...latest.values()];
}

function exact(records: Saved[], pointer?: SavedRecordRef): Saved | undefined {
  return pointer && records.find(record => referenceKey(record.reference) === referenceKey(pointer));
}

function interval(attempt: ResearchSnapshot['attempts'][number] | undefined) {
  if (!attempt || attempt.status !== 'completed' || !attempt.endedAt) return undefined;
  const start = Date.parse(attempt.startedAt), end = Date.parse(attempt.endedAt);
  return Number.isFinite(start) && Number.isFinite(end) && start < end ? { start, end } : undefined;
}

function callbackProof(records: Saved[], work: Saved[], missionId: string): string | undefined {
  const proof = latestIncluded(records, 'stage3_fixture_proof').find(item => item.data.mission_id === missionId);
  const spans = data(proof?.data.intervals_monotonic_ns);
  const runId = string(proof?.data.run_id);
  if (!spans || !runId || proof?.data.proof !== 'offline_local_callbacks' || proof.data.independent_overlap_observed !== true) return undefined;
  const includedIds = new Set(work.filter(item => data(item.data.spec)?.run_id === runId && data(item.data.spec)?.mission_id === missionId).map(item => string(data(item.data.spec)?.work_id)));
  const intervals = Object.entries(spans).flatMap(([name, value]) => {
    const span = data(value);
    const start = span?.started_ns, end = span?.ended_ns;
    return includedIds.has(name) && typeof start === 'number' && typeof end === 'number' && Number.isFinite(start) && Number.isFinite(end) && start < end
      ? [{ name, start, end }] : [];
  });
  const pairs: string[] = [];
  for (let left = 0; left < intervals.length; left += 1) {
    for (let right = left + 1; right < intervals.length; right += 1) {
      if (Math.max(intervals[left].start, intervals[right].start) < Math.min(intervals[left].end, intervals[right].end))
        pairs.push(`${intervals[left].name} and ${intervals[right].name}`);
    }
  }
  return pairs.length ? `Saved monotonic callback intervals overlap: ${pairs.join('; ')}. This is offline local callback proof, not provider concurrency.` : undefined;
}

function overlapSummary(snapshot: ResearchSnapshot, records: Saved[], work: Saved[]): string {
  const proven = callbackProof(records, work, snapshot.mission.id);
  if (proven) return proven;
  const intervals = work.flatMap(item => {
    if (item.data.phase !== 'completed') return [];
    const spec = data(item.data.spec);
    const attemptId = string(spec?.attempt_id);
    const taskId = reference(spec?.proposal_ref)?.id;
    if (spec?.mission_id !== snapshot.mission.id || !string(spec?.run_id) || !taskId) return [];
    const matches = snapshot.attempts.filter(candidate => candidate.id === attemptId && candidate.agentId === spec.actor_id && candidate.taskId === taskId);
    const span = matches.length === 1 ? interval(matches[0]) : undefined;
    return span ? [{ name: string(spec?.work_id) ?? item.reference.id, ...span }] : [];
  });
  if (intervals.length < 2) return 'Overlap unknown: fewer than two linked completed attempt intervals were included.';
  const pairs: string[] = [];
  for (let left = 0; left < intervals.length; left += 1) {
    for (let right = left + 1; right < intervals.length; right += 1) {
      if (Math.max(intervals[left].start, intervals[right].start) < Math.min(intervals[left].end, intervals[right].end))
        pairs.push(`${intervals[left].name} and ${intervals[right].name}`);
    }
  }
  return pairs.length
    ? `Saved attempt spans overlap: ${pairs.join('; ')}. Attempt spans can include waiting; callback overlap is not established here.`
    : `No overlap appears in the ${intervals.length} included completed attempt spans; callback overlap is unknown.`;
}

function WorkItem({ snapshot, records, board, capacity, traces, work }: {
  snapshot: ResearchSnapshot; records: Saved[]; board: Saved[]; capacity: Saved[]; traces: Saved[]; work: Saved;
}) {
  const spec = data(work.data.spec);
  const pinnedProposal = reference(spec?.proposal_ref);
  const proposal = exact(records, pinnedProposal);
  const currentProposal = pinnedProposal && board.find(item => item.reference.id === pinnedProposal.id);
  const actorId = string(spec?.actor_id);
  const actor = actorId ? latestIncluded(records, 'agent_instance').find(item => item.reference.id === actorId) : undefined;
  const actorName = string(actor?.data.display_name) ?? snapshot.agents.find(agent => agent.id === actorId)?.name;
  const specifiedWorkId = string(spec?.work_id);
  const workId = specifiedWorkId ?? work.reference.id;
  const attemptId = string(spec?.attempt_id);
  const missionId = string(spec?.mission_id);
  const runId = string(spec?.run_id);
  const taskId = pinnedProposal?.kind === 'board_proposal' ? pinnedProposal.id : undefined;
  const validWorkIdentity = missionId === snapshot.mission.id && !!runId && !!taskId && !!specifiedWorkId && !!attemptId && !!actorId;
  const linkedTraces = validWorkIdentity ? traces.filter(item => item.data.mission_id === missionId && item.data.run_id === runId && item.data.task_id === taskId && item.data.attempt_id === attemptId && item.data.instance_id === actorId) : [];
  const receiptPointer = reference(work.data.receipt_ref);
  const receiptCandidate = receiptPointer?.kind === 'executor_receipt' ? exact(records, receiptPointer) : undefined;
  const receipt = validWorkIdentity && receiptCandidate?.data.mission_id === missionId && receiptCandidate.data.work_id === workId ? receiptCandidate : undefined;
  const operationIds = new Set([
    ...linkedTraces.map(item => string(item.data.operation_id)),
    string(receipt?.data.capacity_operation_id),
  ].filter((item): item is string => !!item));
  const matchesCapacity = (item: Saved) => validWorkIdentity && item.data.mission_id === missionId && item.data.run_id === runId && item.data.task_id === taskId && item.data.attempt_id === attemptId;
  const identityMatches = operationIds.size === 0 ? capacity.filter(matchesCapacity) : [];
  const operationId = operationIds.size === 1 ? [...operationIds][0] : undefined;
  const linkedOperation = operationId ? capacity.find(item => item.reference.id === operationId) : identityMatches.length === 1 ? identityMatches[0] : undefined;
  const identityConflict = !!linkedOperation && !matchesCapacity(linkedOperation);
  const operation = identityConflict ? undefined : linkedOperation;
  const dependencies = references(proposal?.data.depends_on);
  const recordedReason = linkedTraces.map(item => string(item.data.reason_summary)).find(Boolean);
  const traceNextAction = linkedTraces.map(item => string(item.data.next_action)).find(Boolean);
  const proposalNextAction = currentProposal?.data.status !== 'completed' ? string(currentProposal?.data.next_action) : undefined;
  const capacityProjected = operation && operation.data.availability !== 'not_projected';
  const acceptedTiming = data(receipt?.data.timing);
  const timing = acceptedTiming ?? (capacityProjected ? operation.data : undefined);

  return <li><details className="inline-detail coordination-work-item">
    <summary>{workId} · {string(work.data.phase) ?? 'phase not recorded'} · {actorName ?? actorId ?? 'actor not recorded'}</summary>
    <div className="inline-detail-body">
      <p><AgentBadge name={actorName} agentId={actorId} /> · {string(proposal?.data.objective) ?? 'Pinned proposal objective unavailable.'}</p>
      <dl className="detail-list">
        <div><dt>Executor phase</dt><dd>{string(work.data.phase) ?? 'Not recorded'}</dd></div>
        <div><dt>Board status</dt><dd>{currentProposal ? `${string(currentProposal.data.status) ?? 'not recorded'} · latest included v${currentProposal.reference.version}` : 'No board status included for this proposal.'}</dd></div>
        <div><dt>Capacity status</dt><dd>{operationIds.size > 1 ? 'Conflicting saved operation IDs; status unresolved.' : identityMatches.length > 1 ? 'Multiple saved capacity records match this task and attempt; status unresolved.' : linkedOperation && !operation ? 'Linked capacity identity is missing or conflicts with this work.' : !operation ? 'No verified capacity record included.' : capacityProjected ? string(operation.data.phase) ?? 'Phase not recorded.' : 'Capacity fields were not projected in this inspection.'}</dd></div>
        {receiptPointer && !receipt && <div><dt>Executor receipt</dt><dd>Recorded pointer is unavailable or does not match this work and mission.</dd></div>}
        {identityMatches.length === 1 && <div><dt>Capacity link basis</dt><dd>Unique saved mission, task and attempt IDs.</dd></div>}
        <div><dt>Saved reason</dt><dd>{recordedReason ?? string(operation?.data.diagnostic) ?? 'No reason was recorded in the included trace or capacity fields.'}</dd></div>
        <div><dt>Next action</dt><dd>{traceNextAction ?? proposalNextAction ?? 'No current next action was recorded for this work.'}</dd></div>
        {capacityProjected && string(operation?.data.wait_reason) && <div><dt>Wait reason</dt><dd>{string(operation?.data.wait_reason)}</dd></div>}
        {capacityProjected && operation?.data.wake_at !== null && operation?.data.wake_at !== undefined && <div><dt>Recorded wake value</dt><dd>{String(operation.data.wake_at)}</dd></div>}
        {timing && seconds(timing.elapsed_seconds) && <div><dt>Saved elapsed time</dt><dd>{seconds(timing.elapsed_seconds)}</dd></div>}
        {timing && seconds(timing.active_seconds) && <div><dt>Saved active time</dt><dd>{seconds(timing.active_seconds)}</dd></div>}
        {timing && seconds(timing.queue_wait_seconds) && <div><dt>Saved queue wait</dt><dd>{seconds(timing.queue_wait_seconds)}</dd></div>}
        {timing && seconds(timing.quota_wait_seconds) && <div><dt>Saved quota wait</dt><dd>{seconds(timing.quota_wait_seconds)}</dd></div>}
        {timing && seconds(timing.provider_wait_seconds) && <div><dt>Saved provider wait</dt><dd>{seconds(timing.provider_wait_seconds)}</dd></div>}
        {timing && seconds(timing.retry_wait_seconds) && <div><dt>Saved retry wait</dt><dd>{seconds(timing.retry_wait_seconds)}</dd></div>}
        {timing && string(timing.peer_wait) && <div><dt>Peer wait</dt><dd>{string(timing.peer_wait) === 'not_measured_by_executor' ? 'Not measured by the executor.' : string(timing.peer_wait)}</dd></div>}
      </dl>
      <p>Dependencies: {proposal ? dependencies.length ? `${dependencies.length} exact saved proposal reference${dependencies.length === 1 ? '' : 's'}.` : 'None recorded on the pinned proposal.' : 'Unknown because the pinned proposal version is unavailable.'}</p>
      {dependencies.map(pointer => <p className="tiny-note" key={referenceKey(pointer)}>{referenceLabel(pointer)} · {exact(records, pointer) ? 'exact version included' : 'exact version unavailable'}</p>)}
      {linkedTraces.map(item => <p key={referenceKey(item.reference)}>{string(item.data.summary) ?? 'Trace summary not recorded.'} · {string(item.data.proof) ?? 'proof not recorded'}</p>)}
      <RecordGroup snapshot={snapshot} title="Saved executor work" references={[work.reference]} />
      <RecordGroup snapshot={snapshot} title="Pinned proposal" references={pinnedProposal ? [pinnedProposal] : []} />
      {currentProposal && (!pinnedProposal || referenceKey(currentProposal.reference) !== referenceKey(pinnedProposal)) && <RecordGroup snapshot={snapshot} title="Latest included proposal" references={[currentProposal.reference]} />}
      <RecordGroup snapshot={snapshot} title="Exact dependencies" references={dependencies} />
      {operation && <RecordGroup snapshot={snapshot} title="Linked capacity record" references={[operation.reference]} />}
      {!!linkedTraces.length && <RecordGroup snapshot={snapshot} title="Saved coordination traces" references={linkedTraces.map(item => item.reference)} />}
      {receiptPointer && <RecordGroup snapshot={snapshot} title={receipt ? 'Verified executor receipt' : 'Unverified receipt pointer'} references={[receiptPointer]} />}
      {reference(work.data.result_ref) && <RecordGroup snapshot={snapshot} title="Executor result" references={[reference(work.data.result_ref)!]} />}
    </div>
  </details></li>;
}

export function CoordinationWork({ snapshot }: { snapshot: ResearchSnapshot }) {
  const records = snapshot.inspection?.records ?? [];
  const work = latestIncluded(records, 'executor_work');
  const board = latestIncluded(records, 'board_proposal');
  const capacity = latestIncluded(records, 'capacity_operation');
  const traces = latestIncluded(records, 'coordination_trace');
  const proof = latestIncluded(records, 'stage3_fixture_proof').find(item => item.data.mission_id === snapshot.mission.id);
  const workProposalIds = new Set(work.map(item => reference(data(item.data.spec)?.proposal_ref)?.id).filter(Boolean));
  const remaining = board.filter(item => !workProposalIds.has(item.reference.id));

  return <details className="inline-detail coordination-work" aria-label="Saved work and waits">
    <summary>Saved work and waits{work.length || remaining.length ? ` (${work.length} executor work, ${remaining.length} other proposals)` : ''}</summary>
    <div className="inline-detail-body">
      <p>Read-only coordinator records. These show saved work and waiting, not a scientific finding or a plan made by this view.</p>
      {!snapshot.inspection ? <p>Coordination inspection was not included in this snapshot.</p> : <>
        <p>{overlapSummary(snapshot, records, work)} {snapshot.inspection.timingBasis === 'controlled_clock' ? 'Times use a controlled test clock.' : 'Times are saved local observations.'}</p>
        {proof && <RecordGroup snapshot={snapshot} title="Saved interval proof" references={[proof.reference]} />}
        {work.length ? <ol className="coordination-work-list">{work.map(item => <WorkItem key={referenceKey(item.reference)} snapshot={snapshot} records={records} board={board} capacity={capacity} traces={traces} work={item} />)}</ol> : <p>No executor work was included in this inspection.</p>}
        {remaining.length > 0 && <section aria-label="Proposals without saved executor work"><h4>Proposals without saved executor work</h4><ul>{remaining.map(item => {
          const dependencies = references(item.data.depends_on);
          return <li key={referenceKey(item.reference)}><details className="inline-detail"><summary>{string(item.data.objective) ?? item.reference.id} · {string(item.data.status) ?? 'status not recorded'}</summary><div className="inline-detail-body">
            <p>Next action: {string(item.data.next_action) ?? 'Not recorded.'}</p>
            <p>Dependencies: {dependencies.length ? dependencies.map(referenceLabel).join('; ') : 'None recorded.'}</p>
            <RecordGroup snapshot={snapshot} title="Saved proposal" references={[item.reference]} />
            {dependencies.length > 0 && <RecordGroup snapshot={snapshot} title="Exact dependencies" references={dependencies} />}
          </div></details></li>;
        })}</ul></section>}
      </>}
    </div>
  </details>;
}
