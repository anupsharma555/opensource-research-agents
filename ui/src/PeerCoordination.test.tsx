/* Check that saved board and mailbox records remain distinct in the reader view.
   Synthetic variations exercise incomplete and contradictory records without
   changing the committed offline Stage 3 fixture. */
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';
import type { RecordInspection, ResearchSnapshot, SavedRecordRef } from './generated/contracts';
import { PeerCoordination } from './PeerCoordination';
import stage3 from '../public/stage3-recorded/snapshot.json';

const fixture = stage3 as ResearchSnapshot;
const ref = (kind: string, id: string, version = 1): SavedRecordRef => ({ kind, id, version });
const record = (pointer: SavedRecordRef, data: Record<string, unknown>): RecordInspection => ({ reference: pointer, label: `${pointer.kind} · ${pointer.id} · v${pointer.version}`, data });
const copy = () => structuredClone(fixture);
const latestRequest = (snapshot: ResearchSnapshot, id: string) => snapshot.inspection!.records.filter(item => item.reference.kind === 'mail_request' && item.reference.id === id).sort((a, b) => b.reference.version - a.reference.version)[0];

afterEach(() => cleanup());

describe('saved peer coordination', () => {
  it('reads the committed partial reply and retained dissent without inventing an accepted study appraisal', () => {
    render(<PeerCoordination snapshot={fixture} />);
    const partial = screen.getByRole('article', { name: 'Peer request stage3-partial-review' });
    expect(within(partial).getByText(/Partial answer submitted/)).toBeInTheDocument();
    expect(within(partial).getByText(/delivery, acknowledgment, and answer submission are not review/)).toBeInTheDocument();
    const proposal = screen.getByRole('article', { name: 'Proposal d-evidence-gap' });
    expect(within(proposal).getByRole('region', { name: 'Retained dissent' })).toHaveTextContent('synthetic input contains no study passage to appraise');
    expect(within(proposal).getByText(/challenged proposal v1/)).toBeInTheDocument();
    expect(fixture.mission.reportArtifactId).toBeNull();
  });

  it('presents a completed proposal action as history and keeps its outcome ID in details', () => {
    render(<PeerCoordination snapshot={fixture} />);
    const proposal = screen.getByRole('article', { name: 'Proposal a-fast' });
    expect(within(proposal).getByText('Action recorded on this proposal')).toBeInTheDocument();
    expect(within(proposal).queryByText('Next action')).not.toBeInTheDocument();
    expect(within(proposal).getByText('Saved work outcome available.')).toBeInTheDocument();
    expect([...proposal.querySelectorAll(':scope > p')].map(item => item.textContent).join(' ')).not.toContain('executor-outcome:');
  });

  it('shows delivery and acknowledgment without turning the original unanswered request into a reviewed answer', async () => {
    const user = userEvent.setup();
    render(<PeerCoordination snapshot={fixture} />);
    const original = screen.getByRole('article', { name: 'Peer request stage3-question' });
    expect(within(original).getByRole('heading', { name: /Can an independent peer appraise/ })).toBeInTheDocument();
    expect(within(original).getByText(/No answer reference saved/)).toBeInTheDocument();
    expect(within(original).getByText(/delivery, acknowledgment, and answer submission are not review/)).toBeInTheDocument();
    const fields = within(original).getAllByRole('definition');
    expect(fields[0]).toHaveTextContent('Recorded');
    expect(fields[1]).toHaveTextContent('Recorded');
    const times = within(original).getAllByRole('time');
    expect(times).toHaveLength(2);
    expect(times[0]).toHaveAttribute('title', times[0].getAttribute('datetime'));
    expect(times[0].textContent).toMatch(/\d{1,2}:\d{2}:\d{2} (AM|PM)/);
    await user.click(within(original).getByText('Exact request, message and answer records'));
    expect(within(original).getByRole('region', { name: 'Message versions' })).toBeInTheDocument();
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
    expect(within(request).getByText(/Partial answer submitted/)).toBeInTheDocument();
    expect(within(request).getByText(/not yet reviewed it in these records/)).toBeInTheDocument();
    expect(within(request).getByText(/delivery, acknowledgment, and answer submission are not review/)).toBeInTheDocument();
    saved.inspection!.records.push(record(ref('mail_request', 'stage3-question', partial.reference.version + 1), { ...partial.data, status: 'partly_answered', reviewer_id: saved.agents[0].id, review: { reason: 'Useful but still limited.', requester_offline: false }, resolved_at: null }));
    rerender(<PeerCoordination snapshot={saved} />);
    request = screen.getByRole('article', { name: 'Peer request stage3-question' });
    expect(within(request).getByText(/partly answered by Acquisition peer: Useful but still limited/)).toBeInTheDocument();
    expect(within(request).queryByText(/not yet reviewed it in these records/)).not.toBeInTheDocument();
  });

  it('retains exact proposal-version links and dissent against an earlier version', async () => {
    const user = userEvent.setup();
    const saved = copy();
    const current = saved.inspection!.records.find(item => item.reference.kind === 'board_proposal' && item.reference.id === 'a-fast' && item.reference.version === 2)!;
    const proposalRef = ref('board_proposal', 'a-fast', 1);
    saved.inspection!.records.push(record(ref('board_proposal', 'a-fast', 3), { ...current.data, objective: 'Revised synthetic objective', depends_on: [ref('board_proposal', 'b-slow', 2)], branch_of: ref('board_proposal', 'c-dependent', 1), revision: { reason: 'New saved constraint', evidence_refs: [] } }));
    saved.inspection!.records.push(record(ref('board_dissent', 'synthetic-dissent'), { proposal_ref: proposalRef, actor_ref: ref('agent_instance', saved.agents[2].id), critique: 'The earlier version overstates the callback evidence.', evidence_refs: [] }));
    render(<PeerCoordination snapshot={saved} />);
    const proposal = screen.getByRole('article', { name: 'Proposal a-fast' });
    expect(within(proposal).getByRole('heading', { name: 'Revised synthetic objective' })).toBeInTheDocument();
    expect(within(proposal).getByText(/New saved constraint/)).toBeInTheDocument();
    expect(within(proposal).getByText('Depends on:').closest('p')).toHaveTextContent('board proposal · b-slow · v2');
    expect(within(proposal).getByText('Alternative to:').closest('p')).toHaveTextContent('board proposal · c-dependent · v1');
    expect(within(proposal).getByText(/claimed proposal v1/)).toBeInTheDocument();
    expect(within(proposal).getByText(/challenged proposal v1/)).toHaveTextContent('overstates the callback evidence');
    await user.click(within(proposal).getByText('Exact proposal versions and relationships'));
    expect(within(proposal).getByRole('region', { name: 'Prior proposal versions' })).toBeInTheDocument();
  });

  it('marks ambiguous latest versions and missing answer references without inferring a result', () => {
    const saved = copy();
    const current = saved.inspection!.records.find(item => item.reference.kind === 'board_proposal' && item.reference.id === 'a-fast' && item.reference.version === 2)!;
    saved.inspection!.records.push(record(current.reference, { ...current.data, objective: 'Conflicting duplicate' }));
    const original = latestRequest(saved, 'stage3-question');
    saved.inspection!.records.push(record(ref('mail_request', 'stage3-question', original.reference.version + 1), { ...original.data, status: 'partial_awaiting_review', answer_refs: [ref('mail_answer', 'missing-answer')] }));
    render(<PeerCoordination snapshot={saved} />);
    const proposal = screen.getByRole('article', { name: 'Proposal a-fast' });
    expect(within(proposal).getByText(/Latest saved proposal version is ambiguous/)).toBeInTheDocument();
    expect(within(proposal).queryByText('Conflicting duplicate')).not.toBeInTheDocument();
    const request = screen.getByRole('article', { name: 'Peer request stage3-question' });
    expect(within(request).getByText(/Answer reference unavailable or inconsistent/)).toBeInTheDocument();
    expect(within(request).getByText(/delivery, acknowledgment, and answer submission are not review/)).toBeInTheDocument();
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
    expect(within(request).queryByText(/answered by Acquisition peer/)).not.toBeInTheDocument();
  });
});
