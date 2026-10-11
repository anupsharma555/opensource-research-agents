/* Check that saved board and mailbox records remain distinct in the reader view.
   Synthetic variations exercise incomplete and contradictory records without
   changing the committed offline Stage 3 fixture. */
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { RecordInspection, ResearchSnapshot, SavedRecordRef } from './generated/contracts';
import { PeerCoordination } from './PeerCoordination';
import stage3 from '../public/stage3-recorded/snapshot.json';

const fixture = stage3 as ResearchSnapshot;
const ref = (kind: string, id: string, version = 1): SavedRecordRef => ({ kind, id, version });
const record = (pointer: SavedRecordRef, data: Record<string, unknown>): RecordInspection => ({ reference: pointer, label: `${pointer.kind} · ${pointer.id} · v${pointer.version}`, data });
const copy = () => structuredClone(fixture);
const latestRequest = (snapshot: ResearchSnapshot, id: string) => snapshot.inspection!.records.filter(item => item.reference.kind === 'mail_request' && item.reference.id === id).sort((a, b) => b.reference.version - a.reference.version)[0];

afterEach(() => { cleanup(); vi.unstubAllGlobals(); });

describe('saved peer coordination', () => {
  it('makes absent directed communication explicit despite named agents and work assignments', () => {
    const saved = copy();
    saved.inspection!.records = saved.inspection!.records.filter(item => !['mail_request', 'mail_message', 'mail_answer'].includes(item.reference.kind));
    saved.messages = [];
    render(<PeerCoordination snapshot={saved} />);
    const empty = screen.getByRole('article', { name: 'No peer messages recorded' });
    expect(empty).toHaveTextContent('no saved requests or messages between agents');
    expect(empty).toHaveTextContent('Listing agents in a task does not mean anyone sent a message');
    expect(empty).toHaveTextContent('Model reasoning is separate from messages exchanged with another agent');
    expect(screen.queryByRole('article', { name: /Peer request/ })).not.toBeInTheDocument();
  });

  it('shows a standalone directed message without inventing a linked request or reply', () => {
    const saved = copy();
    saved.inspection!.records = saved.inspection!.records.filter(item => !['mail_request', 'mail_message', 'mail_answer'].includes(item.reference.kind));
    saved.messages = [];
    saved.inspection!.records.push(record(ref('mail_message', 'standalone-message'), {
      sender_ref: ref('agent_instance', saved.agents[0].id),
      recipient_ref: ref('agent_instance', saved.agents[1].id),
      created_at: '2026-10-09T12:00:00Z', delivered_at: null, acknowledged_at: null,
    }));
    render(<PeerCoordination snapshot={saved} />);
    const message = screen.getByRole('article', { name: 'Peer message standalone-message' });
    expect(message).toHaveTextContent('Agent Acquisition peer');
    expect(message).toHaveTextContent('Agent Synthesis peer');
    expect(message).toHaveTextContent('Delivery not recorded');
    expect(message).toHaveTextContent('The message text is not included in this saved view');
    expect(screen.queryByRole('article', { name: 'No peer messages recorded' })).not.toBeInTheDocument();
  });

  it('shows the complete quoted request before any click and distinguishes unavailable message content', () => {
    const saved = copy();
    const original = latestRequest(saved, 'stage3-question');
    const question = 'First exact question line.\nSecond exact question line.';
    saved.inspection!.records.push(record(ref('mail_request', original.reference.id, original.reference.version + 1), {
      ...original.data, question, expected_artifact: 'An exact scoped response with remaining limits.',
    }));
    render(<PeerCoordination snapshot={saved} />);
    const request = screen.getByRole('article', { name: 'Peer request stage3-question' });
    const steps = within(request).getByRole('list', { name: 'Saved request, answer and review' });
    expect(steps).toHaveTextContent('Delivered to Review peer');
    expect(steps).toHaveTextContent('Delivered');
    expect(steps).toHaveTextContent('confirmed receipt');
    expect(steps).toHaveTextContent('No reply recorded');
    const full = request.querySelector('.peer-request-message blockquote')!;
    expect(full.textContent).toBe(question);
    expect(full).toBeVisible();
    expect(request.querySelectorAll('.peer-request-message blockquote')).toHaveLength(1);
    expect(request.querySelector('.peer-request-full')).toBeNull();
    expect(request).toHaveTextContent('The separate message text is not included in this saved view');
    expect(request).toHaveTextContent('Requested result: An exact scoped response with remaining limits.');
  });

  it('names each invited recipient without treating an offer as delivery or ownership', async () => {
    const user = userEvent.setup();
    const saved = copy();
    const proposal = ref('board_proposal', 'a-fast', 1);
    saved.inspection!.records.push(record(ref('agent_instance', 'later-methods-peer'), { display_name: 'Finch', role_id: 'methods_reviewer' }));
    saved.inspection!.records.push(record(ref('board_invitation', 'first-invitation'), {
      proposal_ref: proposal, sender_ref: ref('agent_instance', saved.agents[0].id), recipient_ref: ref('agent_instance', saved.agents[1].id),
      objective: 'Check comparison text', expected_artifact: 'Methods note', status: 'offered', recordedAt: '2026-10-09T14:00:00Z', decision_at: null,
    }));
    saved.inspection!.records.push(record(ref('board_invitation', 'later-invitation'), {
      proposal_ref: proposal, sender_ref: ref('agent_instance', saved.agents[0].id), recipient_ref: ref('agent_instance', 'later-methods-peer'),
      objective: 'Inspect a later method', expected_artifact: 'Methods critique', status: 'accepted', recordedAt: '2026-10-09T14:01:00Z', decision_at: '2026-10-09T14:03:00Z',
    }));
    render(<PeerCoordination snapshot={saved} section="work" />);
    const boardHelp = screen.getByRole('button', { name: 'What does shared work board mean?' });
    await user.hover(boardHelp);
    expect(screen.getByRole('tooltip')).toHaveTextContent('A board entry does not establish that a message was sent');
    const card = screen.getByRole('article', { name: 'Proposal a-fast' });
    const invitations = within(card).getByRole('region', { name: 'Directed invitations' });
    expect(invitations).toHaveTextContent('Finch');
    expect(invitations).toHaveTextContent('Inspect a later method');
    expect(invitations).toHaveTextContent('Check comparison text');
    expect(invitations).toHaveTextContent('neither fact alone proves that work started');
    expect(within(invitations).getAllByRole('time')).toHaveLength(3);
  });

  it('reads the committed partial reply and retained dissent without inventing an accepted study appraisal', async () => {
    const user = userEvent.setup();
    render(<><PeerCoordination snapshot={fixture} /><PeerCoordination snapshot={fixture} section="work" /></>);
    await user.click(screen.getByRole('button', { name: /Open exchange 2:/ }));
    const partial = screen.getByRole('article', { name: 'Peer request stage3-partial-review' });
    const steps = within(partial).getByRole('list', { name: 'Saved request, answer and review' });
    const requestDirection = partial.querySelector('.peer-request-direction')!;
    const answerDirection = partial.querySelector('.peer-request-answer-direction')!;
    expect(requestDirection).toHaveTextContent('Agent Synthesis peer');
    expect(requestDirection).toHaveTextContent('Agent Review peer');
    expect(answerDirection.textContent).toMatch(/Agent Review peer.*submitted for.*Agent Synthesis peer/);
    expect([...answerDirection.children].find(child => child.getAttribute('aria-hidden') === 'true')).toHaveTextContent('→');
    expect(within(steps).getByText('Partial answer submitted')).toBeVisible();
    expect(within(steps).getByText(/synthetic input contains no study passage to appraise/)).toBeVisible();
    expect(within(steps).getByText('No review recorded')).toBeVisible();
    expect(partial.querySelector('.peer-request-full')).toBeNull();
    const proposal = screen.getByRole('article', { name: 'Proposal d-evidence-gap' });
    expect(within(proposal).getByRole('region', { name: 'Retained dissent' })).toHaveTextContent('synthetic input contains no study passage to appraise');
    expect(within(proposal).getByText(/challenged proposal v1/)).toBeInTheDocument();
    expect(fixture.mission.reportArtifactId).toBeNull();
  });

  it('presents a completed proposal action as history and keeps its outcome ID in details', () => {
    render(<PeerCoordination snapshot={fixture} section="work" />);
    const proposal = screen.getByRole('article', { name: 'Proposal a-fast' });
    expect(within(proposal).getByText('Action recorded on this work')).toBeInTheDocument();
    expect(within(proposal).getByText(/A saved result from the local test function/)).toBeInTheDocument();
    expect(within(proposal).getByText('Run the local test function and save its result.')).toBeInTheDocument();
    expect(within(proposal).queryByText('Next action')).not.toBeInTheDocument();
    expect(within(proposal).getByText('Saved work outcome available.')).toBeInTheDocument();
    expect([...proposal.querySelectorAll(':scope > p')].map(item => item.textContent).join(' ')).not.toContain('executor-outcome:');
  });

  it('shows delivery and acknowledgment without turning the original unanswered request into a reviewed answer', async () => {
    const user = userEvent.setup();
    render(<PeerCoordination snapshot={fixture} />);
    const original = screen.getByRole('article', { name: 'Peer request stage3-question' });
    expect(within(original).getByText('Can an independent peer appraise this synthetic callback result?')).toBeVisible();
    const steps = within(original).getByRole('list', { name: 'Saved request, answer and review' });
    expect(within(steps).getByText('No reply recorded')).toBeVisible();
    expect(within(steps).getByText('No review recorded')).toBeVisible();
    expect(within(original).getByText('Delivery history and exact records').closest('details')).not.toHaveAttribute('open');
    await user.click(within(original).getByText('Delivery history and exact records'));
    const history = within(original).getByText('Delivery history and exact records').closest('details')!;
    const fields = within(history).getAllByRole('definition');
    expect(fields[0]).toHaveTextContent('unanswered');
    expect(fields[1]).toHaveTextContent('Recorded');
    expect(fields[2]).toHaveTextContent('Recorded');
    expect(fields[3]).toHaveTextContent('Recorded');
    const times = within(history).getAllByRole('time');
    expect(times).toHaveLength(3);
    expect(times[0]).toHaveAttribute('title', times[0].getAttribute('datetime'));
    expect(times[0].textContent).toMatch(/\d{1,2}:\d{2}:\d{2} (AM|PM)/);
    expect(within(history).getByRole('region', { name: 'Message versions' })).toBeInTheDocument();
  });

  it('keeps a partial answer awaiting review separate from a later requester disposition', () => {
    const saved = copy();
    const original = latestRequest(saved, 'stage3-question');
    const artifact = saved.inspection!.records.find(item => item.reference.kind === 'executor_artifact')!.reference;
    const answerRef = ref('mail_answer', 'synthetic-answer');
    const answer = record(answerRef, { request_id: 'stage3-question', responder_id: saved.agents[2].id, artifact_ref: artifact, partial: true, created_at: '2026-10-09T01:16:00Z' });
    const partial = record(ref('mail_request', 'stage3-question', original.reference.version + 1), { ...original.data, status: 'partial_awaiting_review', answer_refs: [answerRef], answered_at: '2026-10-09T01:16:00Z' });
    saved.inspection!.records.push(answer, partial);
    const { rerender } = render(<PeerCoordination snapshot={saved} />);
    let request = screen.getByRole('article', { name: 'Peer request stage3-question' });
    let steps = within(request).getByRole('list', { name: 'Saved request, answer and review' });
    expect(within(steps).getByText('Partial answer submitted')).toBeVisible();
    expect(within(steps).getByText('No review recorded')).toBeVisible();
    saved.inspection!.records.push(record(ref('mail_request', 'stage3-question', partial.reference.version + 1), { ...partial.data, status: 'partly_answered', reviewer_id: saved.agents[0].id, review: { reason: 'Useful but still limited.', requester_offline: false }, resolved_at: null }));
    rerender(<PeerCoordination snapshot={saved} />);
    request = screen.getByRole('article', { name: 'Peer request stage3-question' });
    steps = within(request).getByRole('list', { name: 'Saved request, answer and review' });
    expect(within(steps).getByText(/Acquisition peer reviewed this reply as partial/)).toBeVisible();
    expect(within(steps).getByText('Useful but still limited.')).toBeVisible();
    expect(within(steps).queryByText('No review recorded')).not.toBeInTheDocument();
  });

  it('offers exact live message and answer reads only for matching projected versions and only after a click', async () => {
    const saved = copy();
    const request = latestRequest(saved, 'stage3-question');
    const requestMessageRef = request.data.message_ref as SavedRecordRef;
    const messageRef = saved.inspection!.records.filter(item => item.reference.kind === 'mail_message' && item.reference.id === requestMessageRef.id)
      .sort((a, b) => b.reference.version - a.reference.version)[0].reference;
    const answerRef = ref('mail_answer', 'exact-answer', 2);
    saved.inspection!.records.push(
      record(answerRef, { request_id: request.reference.id, responder_id: request.data.recipient_id, partial: true, created_at: '2026-10-09T01:16:00Z' }),
      record(ref('mail_request', request.reference.id, request.reference.version + 1), { ...request.data, answer_refs: [answerRef], status: 'partial_awaiting_review' }),
    );
    const response = (reference: SavedRecordRef, content: unknown) => new Response(JSON.stringify({
      runId: 'selected-run', originRunId: 'selected-run', reference, senderId: saved.agents[0].id,
      recipientId: saved.agents[1].id, status: 'available', content, redacted: false, contentReference: reference,
    }), { status: 200, headers: { 'Content-Type': 'application/json' } });
    const fetch = vi.fn().mockResolvedValueOnce(response(messageRef, 'Exact saved message text.'))
      .mockResolvedValueOnce(response(answerRef, { title: 'Exact reply', body: 'Saved answer body.', citations: [], limitations: [] }));
    vi.stubGlobal('fetch', fetch);
    const user = userEvent.setup();
    const view = render(<PeerCoordination snapshot={saved} runId="selected-run" />);
    const card = screen.getByRole('article', { name: 'Peer request stage3-question' });
    expect(fetch).not.toHaveBeenCalled();
    expect(card.querySelector('.peer-request-full')).toBeNull();
    expect(within(card).getByRole('button', { name: 'Expand exact message' }).closest('details')).toBeNull();
    expect(within(card).getByRole('button', { name: 'Expand exact message' })).toBeVisible();
    expect(within(card).getByRole('button', { name: 'Read exact saved answer →' })).toBeVisible();
    expect(fetch).not.toHaveBeenCalled();
    await user.click(within(card).getByRole('button', { name: 'Expand exact message' }));
    expect(await within(card).findByText('Exact saved message text.')).toBeVisible();
    expect(fetch.mock.calls[0][0]).toContain(`kind=mail_message&id=${messageRef.id}&version=${messageRef.version}`);
    await user.click(within(card).getByRole('button', { name: 'Read exact saved answer →' }));
    expect(await within(card).findByRole('heading', { name: 'Exact reply' })).toBeVisible();
    expect(fetch.mock.calls[1][0]).toContain(`kind=mail_answer&id=${answerRef.id}&version=${answerRef.version}`);
    view.rerender(<PeerCoordination snapshot={saved} />);
    expect(screen.queryByRole('button', { name: /Expand exact message|Read exact saved answer/ })).not.toBeInTheDocument();
  });

  it('retains exact proposal-version links and dissent against an earlier version', async () => {
    const user = userEvent.setup();
    const saved = copy();
    const current = saved.inspection!.records.find(item => item.reference.kind === 'board_proposal' && item.reference.id === 'a-fast' && item.reference.version === 2)!;
    const proposalRef = ref('board_proposal', 'a-fast', 1);
    saved.inspection!.records.push(record(ref('board_proposal', 'a-fast', 3), { ...current.data, objective: 'Revised synthetic objective', depends_on: [ref('board_proposal', 'b-slow', 2)], branch_of: ref('board_proposal', 'c-dependent', 1), revision: { reason: 'New saved constraint', evidence_refs: [] } }));
    saved.inspection!.records.push(record(ref('board_dissent', 'synthetic-dissent'), { proposal_ref: proposalRef, actor_ref: ref('agent_instance', saved.agents[2].id), critique: 'The earlier version overstates the callback evidence.', evidence_refs: [] }));
    render(<PeerCoordination snapshot={saved} section="work" />);
    const proposal = screen.getByRole('article', { name: 'Proposal a-fast' });
    expect(within(proposal).getByRole('heading', { name: 'Revised synthetic objective' })).toBeInTheDocument();
    expect(within(proposal).getByText(/New saved constraint/)).toBeInTheDocument();
    expect(within(proposal).getByText('Depends on:').closest('p')).toHaveTextContent('board proposal · b-slow · v2');
    expect(within(proposal).getByText('Alternative to:').closest('p')).toHaveTextContent('board proposal · c-dependent · v1');
    expect(within(proposal).getByRole('button', { name: /version 1/ })).toBeInTheDocument();
    expect(within(proposal).getByText(/challenged proposal v1/)).toHaveTextContent('overstates the callback evidence');
    await user.click(within(proposal).getByRole('button', { name: /version 1/ }));
    expect(within(proposal).getAllByRole('heading', { name: /version 1/ }).length).toBeGreaterThan(0);
  });

  it('marks ambiguous latest versions and missing answer references without inferring a result', () => {
    const saved = copy();
    const current = saved.inspection!.records.find(item => item.reference.kind === 'board_proposal' && item.reference.id === 'a-fast' && item.reference.version === 2)!;
    saved.inspection!.records.push(record(current.reference, { ...current.data, objective: 'Conflicting duplicate' }));
    const original = latestRequest(saved, 'stage3-question');
    saved.inspection!.records.push(record(ref('mail_request', 'stage3-question', original.reference.version + 1), { ...original.data, status: 'partial_awaiting_review', answer_refs: [ref('mail_answer', 'missing-answer')] }));
    render(<><PeerCoordination snapshot={saved} /><PeerCoordination snapshot={saved} section="work" /></>);
    const proposal = screen.getByRole('article', { name: 'Proposal a-fast' });
    expect(within(proposal).getByText(/Latest saved proposal version is ambiguous/)).toBeInTheDocument();
    expect(within(proposal).queryByText('Conflicting duplicate')).not.toBeInTheDocument();
    const request = screen.getByRole('article', { name: 'Peer request stage3-question' });
    const steps = within(request).getByRole('list', { name: 'Saved request, answer and review' });
    expect(within(steps).getByText('Reply could not be verified')).toBeVisible();
    expect(within(steps).getByText('No review recorded')).toBeVisible();
  });

  it('does not label a saved answered status as a verified review when its accepted pointer is malformed', () => {
    const saved = copy();
    const original = latestRequest(saved, 'stage3-question');
    const artifact = saved.inspection!.records.find(item => item.reference.kind === 'executor_artifact')!.reference;
    const answerRef = ref('mail_answer', 'synthetic-reviewed-answer');
    saved.inspection!.records.push(record(answerRef, { request_id: 'stage3-question', responder_id: saved.agents[2].id, artifact_ref: artifact, partial: false }));
    saved.inspection!.records.push(record(ref('mail_request', 'stage3-question', original.reference.version + 1), { ...original.data, status: 'answered', answer_refs: [answerRef], reviewer_id: saved.agents[0].id, review: { reason: 'Appraised the bounded response.' }, accepted_answer_ref: { kind: 'mail_answer', id: answerRef.id, version: 'invalid' } }));
    render(<PeerCoordination snapshot={saved} />);
    const request = screen.getByRole('article', { name: 'Peer request stage3-question' });
    expect(within(request).getByText(/answered · accepted answer link unavailable/)).toBeInTheDocument();
    expect(within(request).getByText(/accepted answer pointer is missing, malformed/)).toBeInTheDocument();
    expect(within(request).getByText(/Saved pointer is malformed/)).toBeInTheDocument();
    expect(within(request).queryByText(/Acquisition peer accepted this reply/)).not.toBeInTheDocument();
  });

  it('does not promote a saved request to delivery when its message belongs to another recipient', () => {
    const saved = copy();
    const request = latestRequest(saved, 'stage3-question');
    const pointer = request.data.message_ref as SavedRecordRef;
    const latestMessage = saved.inspection!.records.filter(item => item.reference.kind === 'mail_message' && item.reference.id === pointer.id)
      .sort((a, b) => b.reference.version - a.reference.version)[0];
    saved.inspection!.records.push(record(ref('mail_message', pointer.id, latestMessage.reference.version + 1), {
      ...latestMessage.data, recipient_ref: ref('agent_instance', 'different-peer'),
    }));
    render(<PeerCoordination snapshot={saved} runId="selected-run" />);
    const card = screen.getByRole('article', { name: 'Peer request stage3-question' });
    const steps = within(card).getByRole('list', { name: 'Saved request, answer and review' });
    expect(steps).toHaveTextContent('Delivery not recorded');
    expect(steps).toHaveTextContent('delivery cannot be confirmed');
    expect(within(card).queryByRole('button', { name: 'Expand exact message' })).not.toBeInTheDocument();
    expect(within(card).getByText(String(request.data.question))).toBeVisible();
  });

  it('does not describe a malformed answer link as an absent reply', () => {
    const saved = copy();
    const original = latestRequest(saved, 'stage3-question');
    saved.inspection!.records.push(record(ref('mail_request', original.reference.id, original.reference.version + 1), {
      ...original.data, answer_refs: [{ kind: 'mail_answer', id: 'broken-answer', version: 'wrong' }],
    }));
    render(<PeerCoordination snapshot={saved} />);
    const card = screen.getByRole('article', { name: 'Peer request stage3-question' });
    const steps = within(card).getByRole('list', { name: 'Saved request, answer and review' });
    expect(steps).toHaveTextContent('Reply could not be verified');
    expect(steps).not.toHaveTextContent('No reply recorded');
    expect(card).toHaveTextContent('Records need checking');
  });

  it('keeps an incomplete saved review decision distinct from no review', () => {
    const saved = copy();
    const original = latestRequest(saved, 'stage3-question');
    saved.inspection!.records.push(record(ref('mail_request', original.reference.id, original.reference.version + 1), {
      ...original.data, status: 'answered', reviewer_id: null, review: null,
    }));
    render(<PeerCoordination snapshot={saved} />);
    const card = screen.getByRole('article', { name: 'Peer request stage3-question' });
    const steps = within(card).getByRole('list', { name: 'Saved request, answer and review' });
    expect(steps).toHaveTextContent('Review could not be confirmed');
    expect(steps).toHaveTextContent('its reviewer or decision is missing');
    expect(steps).not.toHaveTextContent('No review recorded');
  });
});

it('indexes every exchange and keeps a selected exact request separate from its neighbors', async () => {
  const user = userEvent.setup();
  const saved = copy();
  const view = render(<PeerCoordination snapshot={saved} runId="first-run" />);
  const index = screen.getByRole('navigation', { name: 'All saved exchanges' });
  const choices = within(index).getAllByRole('button', { name: /Open exchange/ });
  expect(choices).toHaveLength(2);
  expect(choices[0]).toHaveAttribute('aria-pressed', 'true');
  expect(choices[0]).toHaveTextContent('No reply recorded');
  expect(choices[1]).toHaveTextContent('Reply awaiting review');
  expect(choices[0]).toHaveAccessibleDescription(/Saved .*Can an independent peer appraise this synthetic callback result\? No reply recorded Delivery recorded/);
  expect(choices[1]).toHaveAccessibleDescription(/Saved .*Reply awaiting review Delivery recorded/);
  expect(choices[0].querySelector('time')).toHaveAttribute('datetime', String(latestRequest(saved, 'stage3-question').data.created_at));
  expect(screen.getAllByRole('article', { name: /Peer request / })).toHaveLength(1);
  await user.click(choices[1]);
  expect(screen.getByRole('article', { name: 'Peer request stage3-partial-review' })).toBeVisible();
  expect(screen.queryByRole('article', { name: 'Peer request stage3-question' })).not.toBeInTheDocument();
  const next = copy();
  next.inspection!.records = next.inspection!.records.filter(item => !(item.reference.kind === 'mail_request' && item.reference.id === 'stage3-partial-review'));
  view.rerender(<PeerCoordination snapshot={next} runId="first-run" />);
  expect(screen.getByRole('status')).toHaveTextContent('selected exchange is no longer included');
  expect(screen.queryByRole('article', { name: 'Peer request stage3-question' })).not.toBeInTheDocument();
  view.rerender(<PeerCoordination snapshot={saved} runId="different-run" />);
  expect(screen.getByRole('article', { name: 'Peer request stage3-question' })).toBeVisible();
  expect(screen.queryByRole('status')).not.toBeInTheDocument();
});

it('keeps unknown request time and ambiguous records explicit in the index', () => {
  const saved = copy();
  const current = latestRequest(saved, 'stage3-question');
  current.data.created_at = 'not-a-time';
  const other = latestRequest(saved, 'stage3-partial-review');
  saved.inspection!.records.push(record(other.reference, { ...other.data, question: 'Conflicting request' }));
  render(<PeerCoordination snapshot={saved} />);
  const index = screen.getByRole('navigation', { name: 'All saved exchanges' });
  expect(within(index).getAllByText('Request save time unavailable')).toHaveLength(2);
  expect(within(index).getByText('Latest version is ambiguous')).toBeVisible();
  expect(within(index).getByRole('button', { name: /Request records unclear/ })).toHaveAccessibleDescription('Request save time unavailable Request records unclear Latest version is ambiguous');
  expect(within(index).queryByText('Conflicting request')).not.toBeInTheDocument();
});
