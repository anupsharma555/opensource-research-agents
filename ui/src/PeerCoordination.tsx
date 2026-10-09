/* Read the saved Stage 3 proposal board and peer mailbox without issuing actions.
   Exact record versions establish links; transport receipt, answer submission,
   and requester review stay separate even when their records share a request. */
import type { ResearchSnapshot, RecordInspection, SavedRecordRef } from './generated/contracts';
import { AgentBadge } from './AgentBadge';
import { RecordGroup } from './InspectionDetails';
import { formatActivityTimestamp } from './ActivityRecordDetails';

type Data = Record<string, unknown>;
type Head = { id: string; current?: RecordInspection; versions: RecordInspection[]; ambiguous: boolean };
const object = (value: unknown): Data | null => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Data : null;
const string = (value: unknown): string | null => typeof value === 'string' && value.length > 0 ? value : null;
const reference = (value: unknown): SavedRecordRef | null => {
  const data = object(value);
  return data && string(data.kind) && string(data.id) && Number.isInteger(data.version) && Number(data.version) > 0
    ? { kind: data.kind as string, id: data.id as string, version: data.version as number } : null;
};
const references = (value: unknown) => Array.isArray(value) ? value.map(reference).filter((item): item is SavedRecordRef => item !== null) : [];
const same = (a: SavedRecordRef, b: SavedRecordRef) => a.kind === b.kind && a.id === b.id && a.version === b.version;
const label = (ref: SavedRecordRef) => `${ref.kind.replaceAll('_', ' ')} · ${ref.id} · v${ref.version}`;

function RecordedTime({ value }: { value: string | null }) {
  if (!value) return <>Not recorded</>;
  return <>Recorded <time dateTime={value} title={value}>{formatActivityTimestamp(value)}</time></>;
}

function records(snapshot: ResearchSnapshot, kind: string) {
  return snapshot.inspection?.records.filter(record => record.reference.kind === kind) ?? [];
}

function exact(snapshot: ResearchSnapshot, ref: SavedRecordRef) {
  const matches = snapshot.inspection?.records.filter(record => same(record.reference, ref)) ?? [];
  return matches.length === 1 ? matches[0] : null;
}

function heads(snapshot: ResearchSnapshot, kind: string): Head[] {
  const byId = new Map<string, RecordInspection[]>();
  for (const record of records(snapshot, kind)) byId.set(record.reference.id, [...(byId.get(record.reference.id) ?? []), record]);
  return [...byId].map(([id, versions]) => {
    const latestVersion = Math.max(...versions.map(record => record.reference.version));
    const latest = versions.filter(record => record.reference.version === latestVersion);
    return { id, current: latest.length === 1 ? latest[0] : undefined, versions, ambiguous: latest.length !== 1 };
  }).sort((a, b) => a.id.localeCompare(b.id));
}

function Actor({ snapshot, id }: { snapshot: ResearchSnapshot; id: string | null }) {
  if (!id) return <span>Actor not recorded</span>;
  return <AgentBadge name={snapshot.agents.find(agent => agent.id === id)?.name} agentId={id} />;
}

function ExactRecords({ snapshot, title, refs }: { snapshot: ResearchSnapshot; title: string; refs: SavedRecordRef[] }) {
  const unique = [...new Map(refs.map(ref => [JSON.stringify(ref), ref])).values()];
  const resolved = unique.filter(ref => exact(snapshot, ref));
  const unresolved = unique.filter(ref => !exact(snapshot, ref));
  return <><RecordGroup snapshot={snapshot} title={title} references={resolved} />{unresolved.map(ref => <p key={JSON.stringify(ref)}>Exact record unavailable or ambiguous: {label(ref)}.</p>)}</>;
}

function Proposal({ snapshot, head }: { snapshot: ResearchSnapshot; head: Head }) {
  if (!head.current) return <article className="panel peer-proposal" aria-label={`Proposal ${head.id}`}><h3>Proposal {head.id}</h3><p>Latest saved proposal version is ambiguous; no current wording or status is inferred.</p><details className="inline-detail"><summary>Saved version references</summary><p>{head.versions.map(item => label(item.reference)).join('; ')}</p></details></article>;
  const proposal = head.current;
  const data = proposal.data as Data;
  const actor = reference(data.actor_ref);
  const dependsOn = references(data.depends_on);
  const branch = reference(data.branch_of);
  const prerequisites = references(data.prerequisite_refs);
  const outcome = reference(data.outcome_ref);
  const revision = object(data.revision);
  const reopen = object(data.reopen);
  const claims = records(snapshot, 'board_claim').filter(record => reference(record.data.proposal_ref)?.id === proposal.reference.id);
  const dissents = records(snapshot, 'board_dissent').filter(record => reference(record.data.proposal_ref)?.id === proposal.reference.id);
  const history = head.versions.filter(item => item.reference.version !== proposal.reference.version);
  return <article className="panel peer-proposal" aria-label={`Proposal ${proposal.reference.id}`}>
    <div className="panel-heading"><div><span className="eyebrow">Saved work proposal · v{proposal.reference.version}</span><h3>{string(data.objective) ?? 'Proposal objective not recorded'}</h3></div><span>{string(data.status) ?? 'Status not recorded'}</span></div>
    <p><strong>Proposed by:</strong> <Actor snapshot={snapshot} id={actor?.kind === 'agent_instance' ? actor.id : null} /></p>
    <dl className="detail-list"><div><dt>Expected contribution</dt><dd>{string(data.expected_artifact) ?? 'Not recorded'}</dd></div><div><dt>{['open', 'deferred'].includes(string(data.status) ?? '') ? 'Next action' : 'Action recorded on this proposal'}</dt><dd>{string(data.next_action) ?? 'Not recorded'}</dd></div></dl>
    {dependsOn.length > 0 && <p><strong>Depends on:</strong> {dependsOn.map(label).join('; ')}. These are exact saved proposal versions.</p>}
    {branch && <p><strong>Alternative to:</strong> {label(branch)}.</p>}
    {outcome && <p><strong>Saved work outcome available.</strong> The exact record is expandable below; a work outcome is not a scientific conclusion.</p>}
    {revision && <p><strong>Revision recorded:</strong> {string(revision.reason) ?? 'Reason not recorded'}.</p>}
    {reopen && <p><strong>Reopened after a contradiction:</strong> {string(reopen.reason) ?? 'Reason not recorded'}.</p>}
    {claims.length > 0 && <section aria-label="Recorded work claims"><h4>Recorded work claims</h4>{claims.map(claim => { const claimedVersion = reference(claim.data.proposal_ref); const owner = reference(claim.data.owner_ref); return <p key={JSON.stringify(claim.reference)}><Actor snapshot={snapshot} id={owner?.kind === 'agent_instance' ? owner.id : null} /> claimed {claimedVersion ? `proposal v${claimedVersion.version}` : 'an unavailable proposal version'}. This records temporary ownership, not a current lease or scientific claim.</p>; })}</section>}
    {dissents.length > 0 && <section aria-label="Retained dissent"><h4>Retained dissent</h4>{dissents.map(dissent => { const target = reference(dissent.data.proposal_ref); const author = reference(dissent.data.actor_ref); return <div key={JSON.stringify(dissent.reference)}><p><Actor snapshot={snapshot} id={author?.kind === 'agent_instance' ? author.id : null} /> {target && exact(snapshot, target) ? `challenged proposal v${target.version}` : 'recorded a critique with an unavailable proposal target'}: {string(dissent.data.critique) ?? 'Critique text unavailable.'}</p><details className="inline-detail"><summary>Exact dissent and evidence records</summary><ExactRecords snapshot={snapshot} title="Dissent record" refs={[dissent.reference]} /><ExactRecords snapshot={snapshot} title="Dissent evidence" refs={references(dissent.data.evidence_refs)} /></details></div>; })}</section>}
    <details className="inline-detail"><summary>Exact proposal versions and relationships</summary><ExactRecords snapshot={snapshot} title="Current proposal" refs={[proposal.reference]} /><ExactRecords snapshot={snapshot} title="Prior proposal versions" refs={history.map(item => item.reference)} /><ExactRecords snapshot={snapshot} title="Dependencies and alternatives" refs={[...dependsOn, ...prerequisites, ...(branch ? [branch] : [])]} /><ExactRecords snapshot={snapshot} title="Saved outcome and work claims" refs={[...(outcome ? [outcome] : []), ...claims.map(item => item.reference)]} />{revision && <ExactRecords snapshot={snapshot} title="Revision evidence" refs={references(revision.evidence_refs)} />}</details>
  </article>;
}

function PeerRequest({ snapshot, head }: { snapshot: ResearchSnapshot; head: Head }) {
  if (!head.current) return <article className="panel peer-request" aria-label={`Peer request ${head.id}`}><h3>Peer request {head.id}</h3><p>Latest saved request version is ambiguous; its delivery, answer and review states are not inferred.</p></article>;
  const request = head.current;
  const data = request.data as Data;
  const messageRef = reference(data.message_ref);
  const messageVersions = messageRef ? records(snapshot, 'mail_message').filter(item => item.reference.id === messageRef.id && string(item.data.request_id) === request.reference.id) : [];
  const latestMessageVersion = messageVersions.length ? Math.max(...messageVersions.map(item => item.reference.version)) : null;
  const latestMessages = messageVersions.filter(item => item.reference.version === latestMessageVersion);
  const message = latestMessages.length === 1 ? latestMessages[0] : null;
  const delivery = string(message?.data.delivered_at) ?? string(data.delivered_at);
  const acknowledgement = string(message?.data.acknowledged_at);
  const answerRefs = references(data.answer_refs);
  const answers = answerRefs.map(ref => ({ ref, record: ref.kind === 'mail_answer' ? exact(snapshot, ref) : null }));
  const validAnswers = answers.filter(item => item.record && string(item.record.data.request_id) === request.reference.id && string(item.record.data.responder_id) === string(data.recipient_id));
  const review = object(data.review);
  const reviewerId = string(data.reviewer_id);
  const reviewReason = string(review?.reason);
  const reviewMetadata = Boolean(['answered', 'partly_answered'].includes(string(data.status) ?? '') && reviewerId && reviewReason);
  const accepted = reference(data.accepted_answer_ref);
  const acceptedPresent = data.accepted_answer_ref !== null && data.accepted_answer_ref !== undefined;
  const acceptedLinked = accepted && validAnswers.some(item => same(item.ref, accepted));
  const acceptedRequired = data.status === 'answered';
  const reviewed = reviewMetadata && validAnswers.length > 0 && (!acceptedRequired || Boolean(acceptedLinked));
  const requesterReviewed = reviewed && reviewerId === string(data.requester_id);
  const availability = reference(review?.availability_ref);
  const availabilityRecord = availability?.kind === 'agent_availability' ? exact(snapshot, availability) : null;
  const replacementReviewed = reviewed && !requesterReviewed && review?.requester_offline === true && review?.independence_checked === true
    && availabilityRecord && string(availabilityRecord.data.instance_id) === string(data.requester_id)
    && availabilityRecord.data.status === 'offline' && availabilityRecord.data.recorded_by === 'host';
  const reviewerName = reviewerId ? snapshot.agents.find(agent => agent.id === reviewerId)?.name ?? `Agent identity unavailable (${reviewerId})` : 'Reviewer unavailable';
  return <article className="panel peer-request" aria-label={`Peer request ${request.reference.id}`}>
    <div className="panel-heading"><div><span className="eyebrow">Saved peer request · v{request.reference.version}</span><h3>{string(data.question) ?? 'Question not recorded'}</h3></div><span>{string(data.status)?.replaceAll('_', ' ') ?? 'Status not recorded'}{acceptedRequired && !acceptedLinked ? ' · accepted answer link unavailable' : ''}</span></div>
    <p><Actor snapshot={snapshot} id={string(data.requester_id)} /> asked <Actor snapshot={snapshot} id={string(data.recipient_id)} /> for {string(data.expected_artifact) ?? 'an expected contribution not recorded'}.</p>
    <dl className="detail-list">
      <div><dt>Message delivery</dt><dd><RecordedTime value={delivery} /></dd></div>
      <div><dt>Recipient acknowledgment</dt><dd><RecordedTime value={acknowledgement} /></dd></div>
      <div><dt>Answer submission</dt><dd>{validAnswers.length ? validAnswers.map(item => item.record?.data.partial === true ? 'Partial answer submitted' : 'Answer submitted').join('; ') : answerRefs.length ? 'Answer reference unavailable or inconsistent; content not inferred' : 'No answer reference saved'}{string(data.answered_at) && <> · <time dateTime={string(data.answered_at)!} title={string(data.answered_at)!}>{formatActivityTimestamp(string(data.answered_at)!)}</time></>}</dd></div>
      <div><dt>Requester review</dt><dd>{requesterReviewed ? `${string(data.status)?.replaceAll('_', ' ')} by ${reviewerName}: ${reviewReason}` : replacementReviewed ? `Saved replacement review by ${reviewerName}: ${reviewReason}; the original requester did not review this answer.` : reviewed ? 'Review metadata names another actor, but the saved replacement conditions are not established here.' : reviewMetadata && acceptedRequired && !acceptedLinked ? 'Review metadata is saved, but the accepted answer pointer is missing, malformed, or not linked to this request.' : reviewMetadata ? 'Review metadata is saved, but its exact answer record is unavailable or inconsistent.' : 'Not recorded; delivery, acknowledgment, and answer submission are not review'}</dd></div>
    </dl>
    {validAnswers.map(item => <div key={JSON.stringify(item.ref)}><p><strong>{item.record?.data.partial === true ? 'Partial answer' : 'Submitted answer'}:</strong> the requester has {requesterReviewed ? 'a separate review record' : replacementReviewed ? 'an authorized replacement review record' : 'not yet reviewed it in these records'}.</p><details className="inline-detail"><summary>Exact answer artifact</summary><ExactRecords snapshot={snapshot} title="Answer artifact" refs={reference(item.record?.data.artifact_ref) ? [reference(item.record?.data.artifact_ref)!] : []} /></details></div>)}
    {acceptedPresent && <p><strong>Accepted answer:</strong> {reviewed && acceptedLinked ? 'Exact saved answer reference available in the records below.' : 'Saved pointer is malformed, unavailable, or not linked to an exact reviewed answer.'}</p>}
    <details className="inline-detail"><summary>Exact request, message and answer records</summary><ExactRecords snapshot={snapshot} title="Request versions" refs={head.versions.map(item => item.reference)} /><ExactRecords snapshot={snapshot} title="Message versions" refs={messageVersions.map(item => item.reference)} /><ExactRecords snapshot={snapshot} title="Answer records" refs={answerRefs} />{accepted && <ExactRecords snapshot={snapshot} title="Accepted answer" refs={[accepted]} />}{messageRef && !exact(snapshot, messageRef) && <p>Original message version {label(messageRef)} is unavailable or ambiguous.</p>}</details>
  </article>;
}

export function PeerCoordination({ snapshot }: { snapshot: ResearchSnapshot }) {
  const proposals = heads(snapshot, 'board_proposal');
  const requests = heads(snapshot, 'mail_request');
  return <div className="peer-coordination page-stack" aria-label="Saved peer coordination">
    <div className="section-heading"><div><span className="eyebrow">Saved coordination records</span><h2>Peer coordination</h2></div></div>
    <p className="screen-intro">Proposal versions describe work on the board. Message delivery, acknowledgment, answer submission, and requester review are separate saved facts.</p>
    <section className="page-stack" aria-label="Work proposals"><h3>Work proposals ({proposals.length})</h3>{proposals.map(head => <Proposal key={head.id} snapshot={snapshot} head={head} />)}{proposals.length === 0 && <p>No proposal record is included in this snapshot.</p>}</section>
    <section className="page-stack" aria-label="Peer requests"><h3>Peer requests ({requests.length})</h3>{requests.map(head => <PeerRequest key={head.id} snapshot={snapshot} head={head} />)}{requests.length === 0 && <p>No peer request record is included in this snapshot.</p>}</section>
  </div>;
}
