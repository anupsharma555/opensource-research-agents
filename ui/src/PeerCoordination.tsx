/* Read the saved Stage 3 proposal board and peer mailbox without issuing actions.
   Exact record versions establish links; transport receipt, answer submission,
   and requester review stay separate even when their records share a request. */
import { useId, useState } from 'react';
import type { ResearchSnapshot, RecordInspection, SavedRecordRef } from './generated/contracts';
import { AgentBadge } from './AgentBadge';
import { RecordGroup } from './InspectionDetails';
import { formatActivityTimestamp } from './ActivityRecordDetails';
import { PeerRecordReader } from './PeerRecordReader';
import { TermHelp } from './TermHelp';
import './PeerCoordination.css';

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

function RequestTime({ value, id }: { value: string | null; id: string }) {
  const date = value ? new Date(value) : null;
  if (!date || Number.isNaN(date.getTime())) return <span className="peer-exchange-time" id={id}>Request save time unavailable</span>;
  return <span className="peer-exchange-time" id={id}>Saved <time dateTime={value!} title={value!}>{new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }).format(date)}</time></span>;
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
  }).sort((a, b) => {
    if (kind === 'mail_request') {
      const first = string(a.current?.data.created_at);
      const second = string(b.current?.data.created_at);
      if (first && second && first !== second) return first.localeCompare(second);
      if (first && !second) return -1;
      if (second && !first) return 1;
    }
    return a.id.localeCompare(b.id);
  });
}

function Actor({ snapshot, id }: { snapshot: ResearchSnapshot; id: string | null }) {
  if (!id) return <span>Actor not recorded</span>;
  const name = snapshot.agents.find(agent => agent.id === id)?.name
    ?? string(records(snapshot, 'agent_instance').find(item => item.reference.id === id)?.data.display_name);
  return <AgentBadge name={name} agentId={id} />;
}

function ExactRecords({ snapshot, title, refs }: { snapshot: ResearchSnapshot; title: string; refs: SavedRecordRef[] }) {
  const unique = [...new Map(refs.map(ref => [JSON.stringify(ref), ref])).values()];
  const resolved = unique.filter(ref => exact(snapshot, ref));
  const unresolved = unique.filter(ref => !exact(snapshot, ref));
  return <><RecordGroup snapshot={snapshot} title={title} references={resolved} />{unresolved.map(ref => <p key={JSON.stringify(ref)}>Exact record unavailable or ambiguous: {label(ref)}.</p>)}</>;
}

function Proposal({ snapshot, head, onOpenTask }: { snapshot: ResearchSnapshot; head: Head; onOpenTask?: (taskId: string, context?: { requestId?: string; version?: number }) => void }) {
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
  const invitations = heads(snapshot, 'board_invitation').filter(item => reference(item.current?.data.proposal_ref)?.id === proposal.reference.id);
  const dissents = records(snapshot, 'board_dissent').filter(record => reference(record.data.proposal_ref)?.id === proposal.reference.id);
  const fixtureCallback = snapshot.proofKind === 'offline_test' && string(data.expected_artifact) === 'Local callback receipt';
  const expected = fixtureCallback ? 'A saved result from the local test function; no research deliverable is recorded.' : string(data.expected_artifact) ?? 'Not recorded';
  const nextAction = fixtureCallback && string(data.next_action) === 'Run the registered offline callback' ? 'Run the local test function and save its result.' : string(data.next_action) ?? 'Not recorded';
  return <article className="panel peer-proposal" aria-label={`Proposal ${proposal.reference.id}`}>
    <div className="panel-heading"><div><span className="eyebrow">Shared mission work · current v{proposal.reference.version}</span><h3>{string(data.objective) ?? 'Proposal objective not recorded'}</h3></div><span>{string(data.status) ?? 'Status not recorded'}</span></div>
    <p><strong>Posted to shared mission work by:</strong> <Actor snapshot={snapshot} id={actor?.kind === 'agent_instance' ? actor.id : null} />. Other peers could inspect this board item; this record does not establish that every peer received an invitation.</p>
    <p><strong>Current version saved:</strong> <RecordedTime value={string(data.recordedAt)} />. Snapshot saved <RecordedTime value={snapshot.capturedAt} />.</p>
    {onOpenTask && snapshot.tasks.some(task => task.id === proposal.reference.id) && <button className="inline-link" onClick={() => onOpenTask(proposal.reference.id, { version: proposal.reference.version })}>Open task and saved attempts →</button>}
    <dl className="detail-list"><div><dt>Expected contribution</dt><dd>{expected}</dd></div><div><dt>{['open', 'deferred'].includes(string(data.status) ?? '') ? 'Next action' : 'Action recorded on this work'}</dt><dd>{nextAction}</dd></div></dl>
    {dependsOn.length > 0 && <p><strong>Depends on:</strong> {dependsOn.map(label).join('; ')}. These are exact saved proposal versions.</p>}
    {branch && <p><strong>Alternative to:</strong> {label(branch)}.</p>}
    {outcome && <p><strong>Saved work outcome available.</strong> The exact record is expandable below; a work outcome is not a scientific conclusion.</p>}
    {revision && <p><strong>Revision recorded:</strong> {string(revision.reason) ?? 'Reason not recorded'}.</p>}
    {reopen && <p><strong>Reopened after a contradiction:</strong> {string(reopen.reason) ?? 'Reason not recorded'}.</p>}
    {invitations.length > 0 && <section aria-label="Directed invitations"><h4>Who was directly invited</h4><p>An offer names a recipient. Acceptance is a separate saved decision; neither fact alone proves that work started.</p>{invitations.map(invitation => { const invited = invitation.current!; const offer = [...invitation.versions].sort((a, b) => a.reference.version - b.reference.version)[0]; const sender = reference(invited.data.sender_ref); const recipient = reference(invited.data.recipient_ref); return <div key={invitation.id}><p><Actor snapshot={snapshot} id={sender?.id ?? null} /> invited <Actor snapshot={snapshot} id={recipient?.id ?? null} /> to {string(invited.data.objective) ?? 'work with no objective recorded'}; expected result: {string(invited.data.expected_artifact) ?? 'not recorded'}. Decision: {string(invited.data.status) ?? 'not recorded'}{string(invited.data.counter) ? `; proposed change: ${string(invited.data.counter)}` : ''}.</p><p>Offered <RecordedTime value={string(offer.data.recordedAt)} /> · decision <RecordedTime value={string(invited.data.decision_at)} />.</p><details className="inline-detail"><summary>Exact invitation versions</summary><ExactRecords snapshot={snapshot} title="Invitation records" refs={invitation.versions.map(item => item.reference)} /></details></div>; })}</section>}
    {claims.length > 0 && <section aria-label="Recorded work claims"><h4>Who took responsibility</h4>{claims.map(claim => { const claimedVersion = reference(claim.data.proposal_ref); const owner = reference(claim.data.owner_ref); const claimed = claimedVersion ? exact(snapshot, claimedVersion) : null; const claimedActor = reference(claimed?.data.actor_ref); return <div key={JSON.stringify(claim.reference)}><p><Actor snapshot={snapshot} id={owner?.kind === 'agent_instance' ? owner.id : null} /> took responsibility for {claimedVersion && claimed ? <button className="inline-link" onClick={() => { const target = document.getElementById(`proposal-${claimedVersion.id}-v${claimedVersion.version}`); const disclosure = target?.closest('details'); if (disclosure) disclosure.open = true; target?.scrollIntoView?.({ block: 'center' }); }}>{string(claimed.data.objective) ?? 'work title unavailable'}, version {claimedVersion.version}</button> : claimedVersion ? `${string(claimed?.data.objective) ?? 'work title unavailable'}, version ${claimedVersion.version}` : 'an unavailable work version'} <RecordedTime value={string(claim.data.recordedAt)} />. {claimedActor?.id === owner?.id ? 'The author took on their own proposal.' : 'The owner differs from the proposal author.'} This historical claim is not proof of current ownership.</p>{claimedVersion && !claimed && <p>That exact work version is unavailable in this snapshot.</p>}</div>; })}</section>}
    {dissents.length > 0 && <section aria-label="Retained dissent"><h4>Retained dissent</h4>{dissents.map(dissent => { const target = reference(dissent.data.proposal_ref); const author = reference(dissent.data.actor_ref); return <div key={JSON.stringify(dissent.reference)}><p><Actor snapshot={snapshot} id={author?.kind === 'agent_instance' ? author.id : null} /> {target && exact(snapshot, target) ? `challenged proposal v${target.version}` : 'recorded a critique with an unavailable proposal target'}: {string(dissent.data.critique) ?? 'Critique text unavailable.'}</p><details className="inline-detail"><summary>Exact dissent and evidence records</summary><ExactRecords snapshot={snapshot} title="Dissent record" refs={[dissent.reference]} /><ExactRecords snapshot={snapshot} title="Dissent evidence" refs={references(dissent.data.evidence_refs)} /></details></div>; })}</section>}
    <details className="inline-detail"><summary>Exact work versions and relationships</summary>{[...head.versions].sort((a,b) => a.reference.version-b.reference.version).map(version => <section id={`proposal-${version.reference.id}-v${version.reference.version}`} key={version.reference.version}><h4>{string(version.data.objective) ?? 'Work title unavailable'} · version {version.reference.version}</h4><p>Saved <RecordedTime value={string(version.data.recordedAt)} /> · {version.reference.version === proposal.reference.version ? 'latest included version' : 'historical version'}</p><ExactRecords snapshot={snapshot} title={`Work version ${version.reference.version}`} refs={[version.reference]} /></section>)}<ExactRecords snapshot={snapshot} title="Dependencies and alternatives" refs={[...dependsOn, ...prerequisites, ...(branch ? [branch] : [])]} /><ExactRecords snapshot={snapshot} title="Saved outcome and work claims" refs={[...(outcome ? [outcome] : []), ...claims.map(item => item.reference)]} />{revision && <ExactRecords snapshot={snapshot} title="Revision evidence" refs={references(revision.evidence_refs)} />}</details>
  </article>;
}

function requestEvidence(snapshot: ResearchSnapshot, request: RecordInspection) {
  const data = request.data as Data;
  const messageRef = reference(data.message_ref);
  const messageVersions = messageRef?.kind === 'mail_message' ? records(snapshot, 'mail_message').filter(item => item.reference.id === messageRef.id && string(item.data.request_id) === request.reference.id) : [];
  const latestMessageVersion = messageVersions.length ? Math.max(...messageVersions.map(item => item.reference.version)) : null;
  const latestMessages = messageVersions.filter(item => item.reference.version === latestMessageVersion);
  const candidateMessage = latestMessages.length === 1 ? latestMessages[0] : null;
  const message = candidateMessage
    && reference(candidateMessage.data.sender_ref)?.id === string(data.requester_id)
    && reference(candidateMessage.data.recipient_ref)?.id === string(data.recipient_id)
    ? candidateMessage : null;
  const taskRef = reference(message?.data.task_ref);
  const delivery = string(message?.data.delivered_at);
  const acknowledgement = string(message?.data.acknowledged_at);
  const answerRefs = references(data.answer_refs);
  const answerLinkCount = Array.isArray(data.answer_refs) ? data.answer_refs.length : data.answer_refs == null ? 0 : 1;
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
  const actorName = (id: string | null) => snapshot.agents.find(agent => agent.id === id)?.name
    ?? string(records(snapshot, 'agent_instance').find(item => item.reference.id === id)?.data.display_name)
    ?? 'The peer';
  const requesterName = actorName(string(data.requester_id));
  const recipientName = actorName(string(data.recipient_id));
  const question = string(data.question) ?? 'Question not recorded';
  const requestStatus = string(data.status)?.replaceAll('_', ' ') ?? 'Status not recorded';
  const answerStatus = validAnswers.length ? 'Reply recorded' : answerLinkCount ? 'Reply could not be verified' : 'No reply recorded';
  const reviewClaimed = ['answered', 'partly_answered'].includes(string(data.status) ?? '') || Boolean(review || reviewerId);
  const reviewSummary = requesterReviewed
    ? data.status === 'partly_answered' ? `${reviewerName} reviewed this reply as partial` : `${reviewerName} accepted this reply`
    : replacementReviewed ? `${reviewerName} reviewed on behalf of ${requesterName}`
      : reviewClaimed ? 'Review could not be confirmed' : 'No review recorded';
  const reviewExplanation = reviewed && !requesterReviewed && !replacementReviewed
    ? `The record names ${reviewerName}, but does not confirm that they could review on behalf of ${requesterName}.`
    : reviewMetadata && acceptedRequired && !acceptedLinked ? 'The saved decision does not identify a matching answer.'
      : reviewMetadata ? 'The saved decision refers to an answer that is missing or does not match this request.'
        : reviewClaimed ? 'A review is indicated, but its reviewer or decision is missing.'
          : `No saved assessment shows whether ${requesterName} accepted a reply or requested more work.`;
  const statusLabel = requesterReviewed || replacementReviewed ? 'Reviewed'
    : reviewClaimed || answerLinkCount > validAnswers.length ? 'Records need checking'
      : validAnswers.length ? 'Reply awaiting review' : 'No reply recorded';
  return { request, data, messageRef, messageVersions, message, taskRef, delivery, acknowledgement, answerRefs, answerLinkCount, validAnswers, reviewMetadata, accepted, acceptedPresent, acceptedLinked, acceptedRequired, reviewed, requesterReviewed, replacementReviewed, requesterName, recipientName, question, requestStatus, answerStatus, reviewClaimed, reviewSummary, reviewExplanation, statusLabel, reviewReason };
}

function PeerRequest({ snapshot, head, runId, onOpenTask }: { snapshot: ResearchSnapshot; head: Head; runId?: string | null; onOpenTask?: (taskId: string, context?: { requestId?: string; version?: number }) => void }) {
  if (!head.current) return <article className="panel peer-request" aria-label={`Peer request ${head.id}`}><h3>Peer request {head.id}</h3><p>Latest saved request version is ambiguous; its delivery, answer and review states are not inferred.</p></article>;
  const { request, data, messageRef, messageVersions, message, taskRef, delivery, acknowledgement, answerRefs, answerLinkCount, validAnswers, reviewMetadata, accepted, acceptedPresent, acceptedLinked, acceptedRequired, reviewed, requesterReviewed, replacementReviewed, recipientName, question, requestStatus, answerStatus, reviewClaimed, reviewSummary, reviewExplanation, statusLabel, reviewReason } = requestEvidence(snapshot, head.current);
  return <article className="panel peer-request" aria-label={`Peer request ${request.reference.id}`}>
    <div className="panel-heading peer-request-heading">
      <div>
        <h3 className="peer-request-direction"><Actor snapshot={snapshot} id={string(data.requester_id)} /><span className="peer-request-direction-label">asked</span><span aria-hidden="true">→</span><Actor snapshot={snapshot} id={string(data.recipient_id)} /></h3>
        {string(data.created_at) && <p className="peer-request-date">Request saved <time dateTime={string(data.created_at)!}>{formatActivityTimestamp(string(data.created_at)!)}</time></p>}
      </div>
      <span className="peer-request-state">{statusLabel}</span>
    </div>
    <div className="peer-request-message">
      {runId && message
        ? <PeerRecordReader key={`${runId}:${message.reference.id}:${message.reference.version}`} runId={runId} reference={{ kind: 'mail_message', id: message.reference.id, version: message.reference.version }} snapshot={snapshot} requestQuestion={question} />
        : <><p className="peer-message-label">Saved request</p><blockquote className="peer-message-quote">{question}</blockquote><p className="peer-message-note">The separate message text is not included in this saved view.</p></>}
    </div>
    <p className="peer-request-purpose"><strong>Requested result:</strong> {string(data.expected_artifact) ?? 'Expected contribution not recorded'}</p>
    {onOpenTask && taskRef?.kind === 'board_proposal' && <button className="inline-link" onClick={() => onOpenTask(taskRef.id, { requestId: request.reference.id, version: taskRef.version })}>Open related task →</button>}
    <ol className="peer-request-steps" aria-label="Saved request, answer and review">
      <li><span className="peer-request-step-icon" aria-hidden="true">✉</span><div>
        <strong>{delivery ? `Delivered to ${recipientName}` : 'Delivery not recorded'}</strong>
        {delivery && <span> · <time dateTime={delivery} title={delivery}>{formatActivityTimestamp(delivery)}</time></span>}
        <p>{!message ? messageRef ? 'The matching message is missing or inconsistent; delivery cannot be confirmed.' : 'The request is saved, but there is no matching saved message.' : !delivery ? 'The message is saved; these records do not confirm that it reached the recipient.' : null} {acknowledgement ? <>{recipientName} confirmed receipt <time dateTime={acknowledgement} title={acknowledgement}>{formatActivityTimestamp(acknowledgement)}</time>.</> : `No confirmation of receipt from ${recipientName} is recorded.`}</p>
      </div></li>
      <li><span className="peer-request-step-icon" aria-hidden="true">{validAnswers.length ? '↩' : '○'}</span><div>
        <strong>{answerStatus}</strong>
        {!validAnswers.length && <p>{answerLinkCount ? 'A reply is listed, but its saved record is missing or does not match this request.' : `No answer from ${recipientName} is saved with this request.`}</p>}
        {validAnswers.map(item => {
          const artifactRef = reference(item.record?.data.artifact_ref);
          const artifact = artifactRef ? exact(snapshot, artifactRef) : null;
          const content = string(artifact?.data.critique) ?? string(artifact?.data.summary) ?? string(artifact?.data.body);
          const preview = snapshot.messages.find(reply => reply.id === item.ref.id && reply.requestId === request.reference.id && reply.fromAgentId === string(data.recipient_id) && reply.toAgentId === string(data.requester_id));
          const answerTime = string(item.record?.data.created_at);
          return <div className="peer-request-reply" key={JSON.stringify(item.ref)}>
            <p><strong>{item.record?.data.partial === true ? 'Partial answer submitted' : 'Answer submitted'}</strong>{answerTime && <span> · <time dateTime={answerTime} title={answerTime}>{formatActivityTimestamp(answerTime)}</time></span>}</p>
            <span className="peer-request-answer-direction"><Actor snapshot={snapshot} id={string(data.recipient_id)} /><span>submitted for</span><span aria-hidden="true">→</span><Actor snapshot={snapshot} id={string(data.requester_id)} /></span>
            {(content || preview) ? <><p className="peer-message-label">{content ? 'Saved answer text' : 'Reply preview · full text not included'}</p><blockquote className="peer-message-quote">{content ?? preview?.summary}</blockquote></> : <p>The answer is recorded, but its text is not included in this saved view.</p>}
            {runId && <PeerRecordReader key={`${runId}:${item.ref.id}:${item.ref.version}`} runId={runId} reference={{ kind: 'mail_answer', id: item.ref.id, version: item.ref.version }} snapshot={snapshot} />}
          </div>;
        })}
        {validAnswers.length > 0 && validAnswers.length < answerLinkCount && <p>Another listed reply could not be verified from the saved records.</p>}
      </div></li>
      <li><span className="peer-request-step-icon" aria-hidden="true">{requesterReviewed || replacementReviewed ? '✓' : '○'}</span><div>
        <strong>{reviewSummary}</strong>
        {(requesterReviewed || replacementReviewed) && string(data.resolved_at) && <span> · <time dateTime={string(data.resolved_at)!} title={string(data.resolved_at)!}>{formatActivityTimestamp(string(data.resolved_at)!)}</time></span>}
        {(requesterReviewed || replacementReviewed) && reviewReason ? <p className="peer-request-review-preview">{reviewReason}</p>
          : <p>{reviewExplanation} {!reviewClaimed && 'Confirming receipt is not the same as reviewing the answer.'}</p>}
      </div></li>
    </ol>
    <details className="inline-detail peer-request-records"><summary>Delivery history and exact records</summary><div className="inline-detail-body">
      <dl className="detail-list"><div><dt>Saved request status</dt><dd>{requestStatus}{acceptedRequired && !acceptedLinked ? ' · accepted answer link unavailable' : ''}</dd></div><div><dt>Request created</dt><dd><RecordedTime value={string(data.created_at)} /></dd></div><div><dt>Message delivery</dt><dd><RecordedTime value={delivery} /></dd></div><div><dt>Recipient acknowledgment</dt><dd><RecordedTime value={acknowledgement} /></dd></div><div><dt>Answer submission</dt><dd>{answerStatus}{string(data.answered_at) && <> · <time dateTime={string(data.answered_at)!} title={string(data.answered_at)!}>{formatActivityTimestamp(string(data.answered_at)!)}</time></>}</dd></div><div><dt>Requester review</dt><dd>{reviewSummary}. Delivery and answer submission do not by themselves establish review.</dd></div></dl>
      {reviewMetadata && acceptedRequired && !acceptedLinked && <p>Review metadata is saved, but the accepted answer pointer is missing, malformed, or not linked to this request.</p>}
      {acceptedPresent && <p><strong>Accepted answer:</strong> {reviewed && acceptedLinked ? 'Exact saved answer reference available below.' : 'Saved pointer is malformed, unavailable, or not linked to an exact reviewed answer.'}</p>}
      <ExactRecords snapshot={snapshot} title="Request versions" refs={head.versions.map(item => item.reference)} /><ExactRecords snapshot={snapshot} title="Message versions" refs={messageVersions.map(item => item.reference)} /><ExactRecords snapshot={snapshot} title="Answer records" refs={answerRefs} />
      {validAnswers.map(item => { const artifactRef = reference(item.record?.data.artifact_ref); return artifactRef ? <ExactRecords key={JSON.stringify(item.ref)} snapshot={snapshot} title={`Answer artifact · ${item.ref.id}`} refs={[artifactRef]} /> : null; })}
      {accepted && <ExactRecords snapshot={snapshot} title="Accepted answer" refs={[accepted]} />}{messageRef && !exact(snapshot, messageRef) && <p>Original message version {label(messageRef)} is unavailable or ambiguous.</p>}
    </div></details>
  </article>;
}

function StandaloneMessage({ snapshot, head, runId }: { snapshot: ResearchSnapshot; head: Head; runId?: string | null }) {
  if (!head.current) return <article className="panel peer-request" aria-label={`Peer message ${head.id}`}><h4>Peer message</h4><p>Latest saved message version is ambiguous; content and delivery are not inferred.</p></article>;
  const message = head.current;
  const sender = reference(message.data.sender_ref);
  const recipient = reference(message.data.recipient_ref);
  const preview = snapshot.messages.find(item => item.id === head.id && item.fromAgentId === sender?.id && item.toAgentId === recipient?.id);
  return <article className="panel peer-request" aria-label={`Peer message ${head.id}`}>
    <span className="eyebrow">Message between agents</span>
    <div className="peer-request-direction"><Actor snapshot={snapshot} id={sender?.id ?? null} /><span aria-hidden="true">→</span><Actor snapshot={snapshot} id={recipient?.id ?? null} /></div>
    <p><RecordedTime value={string(message.data.created_at)} />. {string(message.data.delivered_at) ? <>Delivered <RecordedTime value={string(message.data.delivered_at)} />.</> : 'Delivery not recorded.'} {string(message.data.acknowledged_at) ? <>Acknowledged <RecordedTime value={string(message.data.acknowledged_at)} />.</> : 'Acknowledgment not recorded.'}</p>
    {preview && <p><strong>Message preview:</strong> {preview.summary} The full message text is not included in this saved view.</p>}
    {!preview && <p>The message text is not included in this saved view. The task assignment cannot tell us what was sent.</p>}
    {runId && <PeerRecordReader key={`${runId}:${message.reference.id}:${message.reference.version}`} runId={runId} reference={{ kind: 'mail_message', id: message.reference.id, version: message.reference.version }} snapshot={snapshot} />}
    <details className="inline-detail peer-request-records"><summary>Exact message record and versions</summary><ExactRecords snapshot={snapshot} title="Message versions" refs={head.versions.map(item => item.reference)} /></details>
  </article>;
}

export function PeerCoordination({ snapshot, section = 'requests', runId, onOpenTask }: { snapshot: ResearchSnapshot; section?: 'requests' | 'work'; runId?: string | null; onOpenTask?: (taskId: string, context?: { requestId?: string; version?: number }) => void }) {
  const selectionId = useId();
  const scope = runId ?? snapshot.mission.id;
  const [selection, setSelection] = useState<{ scope: string; id: string } | null>(null);
  const proposals = heads(snapshot, 'board_proposal');
  const requests = heads(snapshot, 'mail_request');
  const selected = selection?.scope === scope ? requests.find(head => head.id === selection.id) : requests[0];
  const selectedIndex = selected ? requests.indexOf(selected) : -1;
  const messages = heads(snapshot, 'mail_message');
  const unlinkedMessages = messages.filter(head => !requests.some(request => request.id === string(head.current?.data.request_id)));
  return <div className="peer-coordination page-stack" aria-label={section === 'work' ? 'Shared work board' : 'Directed peer exchanges'}>
    {section === 'work' ? <section className="page-stack" aria-label="Work proposals"><h3>Shared work board ({proposals.length}) <TermHelp term="shared work board" /></h3><p className="screen-intro">A shared to-do list for this run. Agents can propose work or take responsibility for it. A board entry is not a message to another agent.</p>{proposals.map(head => <Proposal key={head.id} snapshot={snapshot} head={head} onOpenTask={onOpenTask} />)}{proposals.length === 0 && <p>No proposal record is included in this snapshot.</p>}</section>
      : <section className="page-stack" aria-label="Peer requests"><h3>Requests between agents ({requests.length})</h3><p className="screen-intro">Follow each exchange from request → delivery → reply → review. A saved request does not by itself confirm delivery.</p>{requests.length > 0 && <div className="peer-exchange-browser">
        <nav className="peer-exchange-index" aria-label="All saved exchanges">
          <p className="peer-exchange-index-label">Choose an exchange · {requests.length} saved requests</p>
          <ol>{requests.map((head, index) => {
            const evidence = head.current ? requestEvidence(snapshot, head.current) : null;
            const descriptionId = `${selectionId}-exchange-${index}`;
            return <li key={head.id}><button type="button" className="peer-exchange-select" aria-pressed={selected?.id === head.id} aria-controls={selectionId} aria-describedby={`${descriptionId}-time ${descriptionId}-preview ${descriptionId}-status${evidence ? ` ${descriptionId}-delivery` : ''}`} aria-label={`Open exchange ${index + 1}: ${evidence ? `${evidence.requesterName} → ${evidence.recipientName}` : 'Request records unclear'}`} onClick={() => setSelection({ scope, id: head.id })}>
              <span className="peer-exchange-number">Exchange {index + 1}</span><RequestTime value={string(head.current?.data.created_at)} id={`${descriptionId}-time`} />
              {evidence ? <><span className="peer-exchange-direction"><Actor snapshot={snapshot} id={string(evidence.data.requester_id)} /><span aria-hidden="true">→</span><Actor snapshot={snapshot} id={string(evidence.data.recipient_id)} /></span><span className="peer-exchange-preview" id={`${descriptionId}-preview`}>{evidence.question}</span><span className="peer-exchange-status" id={`${descriptionId}-status`}>{evidence.statusLabel}</span><span className="peer-exchange-delivery" id={`${descriptionId}-delivery`}>{evidence.delivery ? 'Delivery recorded' : 'Delivery not confirmed'}</span></> : <><span id={`${descriptionId}-preview`}>Request records unclear</span><span className="peer-exchange-delivery" id={`${descriptionId}-status`}>Latest version is ambiguous</span></>}
            </button></li>;
          })}</ol>
        </nav>
        <div className="peer-exchange-selected" id={selectionId}>
          {selected ? <><p className="peer-exchange-selected-label">Selected exchange {selectedIndex + 1} of {requests.length} · request, reply and review</p><PeerRequest key={`${scope}:${selected.id}`} snapshot={snapshot} head={selected} runId={runId} onOpenTask={onOpenTask} /></> : <p role="status">The selected exchange is no longer included in this saved view. Choose another exchange.</p>}
        </div>
      </div>}{unlinkedMessages.length > 0 && <section className="page-stack" aria-label="Other saved peer messages"><h4>Other saved peer messages ({unlinkedMessages.length})</h4>{unlinkedMessages.map(head => <StandaloneMessage key={head.id} snapshot={snapshot} head={head} runId={runId} />)}</section>}{requests.length === 0 && messages.length === 0 && snapshot.messages.length === 0 && <article className="panel peer-empty" aria-label="No peer messages recorded"><h4>No peer messages recorded</h4><p>This run has no saved requests or messages between agents. Listing agents in a task does not mean anyone sent a message. Model reasoning is separate from messages exchanged with another agent.</p></article>}</section>}
  </div>;
}
