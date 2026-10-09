/* Explain saved peer contributions and the event coverage of derived summaries.
   This component renders the backend's read projection; it never dispatches work,
   infers scientific correctness, or treats missing peer statements as rejection. */
import type { RecordInspection, ResearchSnapshot, SavedRecordRef } from './generated/contracts';
import { RecordGroup } from './InspectionDetails';

type Data = Record<string, unknown>;
const object = (value: unknown): Data | null => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Data : null;
const text = (value: unknown): string | null => typeof value === 'string' && value.length > 0 ? value : null;
const array = (value: unknown): unknown[] => Array.isArray(value) ? value : [];
const count = (value: unknown): number | null => typeof value === 'number' && Number.isInteger(value) && value >= 0 ? value : null;
const pointer = (value: unknown): SavedRecordRef | null => {
  const data = object(value);
  return data && text(data.kind) && text(data.id) && count(data.version) !== null && Number(data.version) > 0
    ? { kind: data.kind as string, id: data.id as string, version: data.version as number } : null;
};

function actorName(snapshot: ResearchSnapshot, value: unknown): string {
  const ref = pointer(value);
  return snapshot.agents.find(agent => agent.id === ref?.id)?.name ?? 'Peer identity unavailable';
}

function projectedRecords(context: Data): { ref: SavedRecordRef; data: Data; availability: string }[] {
  const found = new Map<string, { ref: SavedRecordRef; data: Data; availability: string }>();
  for (const entry of array(context.entries)) {
    for (const value of array(object(entry)?.records)) {
      const row = object(value), ref = pointer(row?.ref), data = object(row?.data);
      if (row && ref && data) found.set(JSON.stringify(ref), { ref, data, availability: text(row.availability) ?? 'unknown' });
    }
  }
  return [...found.values()];
}

function Explanation({ snapshot, value, boundaryLabel }: { snapshot: ResearchSnapshot; value: unknown; boundaryLabel?: string }) {
  const item = object(value);
  if (!item) return <p>Explanation metadata is unavailable.</p>;
  const ref = pointer(item.reference);
  const scoped = count(item.newer_scoped_event_count), other = count(item.newer_unclassified_event_count);
  const missing = [...array(item.missing_source_refs), ...array(item.missing_event_refs)];
  const gaps = array(item.known_gaps).map(object).filter((gap): gap is Data => gap !== null);
  return <article className="panel" aria-label={`Saved explanation ${ref?.id ?? 'unavailable'}`}>
    <h4>{item.level === 'agent' ? 'Agent explanation' : item.level === 'group' ? 'Group explanation' : 'Run explanation'}{boundaryLabel && ` · ${boundaryLabel}`}</h4>
    <p>{text(item.summary) ?? 'No explanation text is available.'}</p>
    {item.coverage_status === 'declared' ? <>
      <p>{count(item.through_event_sequence) !== null ? `Author-declared event boundary: saved event ${item.through_event_sequence}.` : 'The author declared an explicit set of saved events.'} This is the author’s stated reading boundary.</p>
      <p>The cited source set is not proven complete by this event boundary.</p>
      {count(item.observed_event_sequence) !== null && <p>This comparison inspected the event store through event {count(item.observed_event_sequence)}.</p>}
      <p>{item.generation_state === 'partial' ? 'Saved as a partial explanation.' : item.generation_state === 'complete' ? 'Generation finished for the stated scope; the research mission may still be open.' : 'Generation status is unavailable.'}</p>
    </> : <p>Coverage unknown: this older explanation did not record an event boundary.</p>}
    {scoped !== null && scoped > 0 && <p>{scoped} later event{scoped === 1 ? ' is' : 's are'} available in the declared scope. This explanation predates that activity; its relevance still needs review.</p>}
    {other !== null && other > 0 && <p>{other} later event{other === 1 ? ' has' : 's have'} not been classified against this explanation’s scope.</p>}
    {missing.length > 0 && <p>{missing.length} exact saved record{missing.length === 1 ? ' is' : 's are'} unavailable. Its content is not inferred.</p>}
    {gaps.length > 0 && <ul>{gaps.map((gap, index) => <li key={index}>{gap.kind === 'omitted' ? 'Detail intentionally omitted' : gap.kind === 'delayed' ? 'Detail still pending' : 'Detail unavailable'}: {text(gap.reason_code)?.replaceAll('_', ' ') ?? 'Reason not recorded'}.</li>)}</ul>}
    {ref && <RecordGroup snapshot={snapshot} title="Exact explanation and contributing records" references={[ref]} />}
  </article>;
}

function Review({ snapshot, record }: { snapshot: ResearchSnapshot; record: RecordInspection }) {
  const context = object(record.data.proposal_context);
  const rows = context ? projectedRecords(context) : [];
  const critiques = rows.filter(item => item.ref.kind === 'board_dissent');
  const uptake = rows.filter(item => item.ref.kind === 'peer_update_uptake');
  const messages = rows.filter(item => item.ref.kind === 'mail_message' && item.data.request_id === null && item.data.message_role !== 'clarification');
  const clarifications = [...new Map(rows.filter(item => item.ref.kind === 'mail_message' && item.data.message_role === 'clarification').map(item => [item.ref.id, item])).values()];
  const unknown = [...new Set(messages.filter(message => !uptake.some(item => pointer(item.data.update_ref)?.id === message.ref.id)).map(message => message.ref.id))];
  const summaries = array(record.data.summary_views);
  const scopes = summaries.map(item => JSON.stringify(object(item)?.scope));
  const boundaries = summaries.map(item => count(object(item)?.through_event_sequence));
  const comparable = scopes.length > 1 && scopes.every(scope => scope === scopes[0] && scope !== undefined && scope !== 'null') && boundaries.every(value => value !== null);
  const earliest = comparable ? Math.min(...boundaries as number[]) : null;
  const latest = comparable ? Math.max(...boundaries as number[]) : null;
  return <div className="page-stack">
    <section className="panel" aria-label="Shared contribution review">
      <h3>How peers used shared work</h3>
      <p>{text(record.data.purpose) ?? 'These saved peer statements describe handling of shared work, not scientific correctness.'}</p>
      <p>The viewing peer is {actorName(snapshot, record.data.late_peer_ref)}. Earlier discussion can be discovered without sending another message or waking every agent.</p>
      {!context && <p>The shared discussion view is unavailable.</p>}
      {context && <p>Discussion captured through event {count(context.through) ?? 'unknown'}.{context.has_more === true ? ' More event pages are available; this view is incomplete.' : ' This saved page has no further events within its declared boundary.'}</p>}
      {critiques.map(item => <article key={JSON.stringify(item.ref)}><h4>Shared critique</h4><p>{actorName(snapshot, item.data.actor_ref)}: {text(item.data.critique) ?? 'Critique text unavailable; inspect its exact record.'}</p><RecordGroup snapshot={snapshot} title="Exact critique and evidence" references={[item.ref]} /></article>)}
      {uptake.map(item => <article key={JSON.stringify(item.ref)} aria-label={`Peer statement ${item.ref.id}`}><h4>{actorName(snapshot, item.data.actor_ref)} — {text(item.data.disposition)?.replaceAll('_', ' ') ?? 'handling not recorded'}</h4><p>{text(item.data.statement) ?? 'No explanation was saved.'}</p><p>This is a peer’s statement. Following the linked change does not by itself establish that the change was scientifically correct.</p><RecordGroup snapshot={snapshot} title="Exact update, statement and linked effect" references={[item.ref]} /></article>)}
      {unknown.map(id => {
        const message = messages.filter(item => item.ref.id === id).sort((a, b) => b.ref.version - a.ref.version)[0];
        const receipt = text(message?.data.acknowledged_at) ? 'Acknowledgment recorded' : text(message?.data.delivered_at) ? 'Delivery recorded' : 'Delivery not recorded';
        return <article key={id}><p>Update {id}: {receipt}; scientific use unknown. No handling statement is recorded.</p><RecordGroup snapshot={snapshot} title="Exact update and receipt state" references={[message.ref]} /></article>;
      })}
      {clarifications.map(item => <article key={item.ref.id}><h4>Clarification of an existing peer request</h4><p>Sending this clarification does not answer or close the original request. Inspect the linked request for its saved state.</p><RecordGroup snapshot={snapshot} title="Exact clarification and original request" references={[item.ref, ...(pointer(item.data.clarifies_request_ref) ? [pointer(item.data.clarifies_request_ref)!] : [])]} /></article>)}
      <p>Statements are optional. Missing information does not mean an update was ignored, rejected or scientifically resolved.</p>
      <RecordGroup snapshot={snapshot} title="Saved discussion projection" references={[record.reference]} />
    </section>
    <section className="page-stack" aria-label="Explanation coverage"><h3>What the saved explanations cover</h3><p>Explanations are derived from records. They are separate from research findings and provider-returned reasoning.</p>{summaries.map((item, index) => <Explanation key={index} snapshot={snapshot} value={item} boundaryLabel={comparable && earliest !== latest ? boundaries[index] === earliest ? 'earlier event boundary' : boundaries[index] === latest ? 'later event boundary' : undefined : undefined} />)}</section>
  </div>;
}

export function ContributionReview({ snapshot }: { snapshot: ResearchSnapshot }) {
  const records = snapshot.inspection?.records.filter(item => item.reference.kind === 'contribution_review_view') ?? [];
  if (!records.length) return null;
  return <section className="page-stack" aria-label="Contribution and explanation review">{records.map(record => <Review key={JSON.stringify(record.reference)} snapshot={snapshot} record={record} />)}</section>;
}
