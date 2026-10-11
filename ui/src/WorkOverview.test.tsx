/* Exercise grouped overview navigation without losing exact step identities. */
import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import sample from '../public/sample-run.json';
import type { ResearchSnapshot } from './generated/contracts';
import { WorkOverview } from './WorkOverview';

afterEach(cleanup);

function savedRun() {
  const snapshot = structuredClone(sample) as ResearchSnapshot;
  const base = snapshot.attempts[0];
  snapshot.attempts = [
    { ...base, id: 'read-a', startedAt: '2026-10-08T14:01:00Z', endedAt: '2026-10-08T14:02:00Z', toolCalls: [{ name: 'read_source_window', status: 'completed' }] },
    { ...base, id: 'read-b', agentId: snapshot.agents.find(agent => agent.id !== base.agentId)!.id, startedAt: '2026-10-08T14:01:30Z', endedAt: '2026-10-08T14:03:00Z', toolCalls: [{ name: 'read_source_window', status: 'completed' }] },
    { ...base, id: 'rejected-hit', startedAt: '2026-10-08T14:04:00Z', endedAt: '2026-10-08T14:05:00Z', toolCalls: [{ name: 'inspect_hit', status: 'rejected' }] },
    { ...base, id: 'system-unsent', status: 'superseded', startedAt: '2026-10-08T14:05:00Z', endedAt: '2026-10-08T14:10:00Z', toolCalls: [] },
    { ...base, id: 'waiting', status: 'waiting', startedAt: '2026-10-08T14:11:00Z', endedAt: null, toolCalls: [] },
  ];
  return snapshot;
}

it('groups related steps without merging agents or hiding a rejection, then links that group to Activity', async () => {
  const snapshot = savedRun();
  const onOpenActivity = vi.fn();
  const user = userEvent.setup();
  render(<WorkOverview snapshot={snapshot} earlierAttemptIds={new Set()} onOpenActivity={onOpenActivity} />);
  const source = screen.getByText('Source reading and checks').closest('button')!;
  expect(within(source).getByText('3 saved steps · 1 rejected or failed')).toBeVisible();
  expect(source.querySelectorAll('.work-overview-label .agent-badge')).toHaveLength(2);
  expect(source.querySelectorAll('.work-overview-mark')).toHaveLength(3);
  expect(source.querySelectorAll('.mark-failed')).toHaveLength(1);
  await user.hover(source.querySelector<HTMLElement>('[data-attempt-id="rejected-hit"]')!);
  expect(source.querySelector('.work-overview-tooltip')).toHaveTextContent('Tool request rejected');
  await user.click(source);
  expect(onOpenActivity).toHaveBeenLastCalledWith('Source reading and checks');
});

it('excludes inherited steps and leaves unfinished work as a point rather than extending its duration', async () => {
  const snapshot = savedRun();
  render(<WorkOverview snapshot={snapshot} earlierAttemptIds={new Set(['read-a'])} onOpenActivity={vi.fn()} />);
  expect(screen.getByText('2 saved steps · 1 rejected or failed')).toBeVisible();
  const system = screen.getByText('System preparation and pauses').closest('button')!;
  expect(system.querySelector('.work-overview-mark')).toHaveClass('mark-system', 'mark-interval');
  const other = screen.getByText('Other saved work').closest('button')!;
  expect(other.querySelector('.work-overview-mark')).toHaveClass('mark-point');
  expect(other.querySelector('.work-overview-mark')).not.toHaveClass('mark-interval');
  expect(screen.getByText(/bar length is not tool runtime/)).toBeVisible();
});

it('explains a real saved interval on hover and opens that exact step rather than only its category', async () => {
  const snapshot = savedRun();
  const onOpenActivity = vi.fn();
  const user = userEvent.setup();
  render(<WorkOverview snapshot={snapshot} earlierAttemptIds={new Set()} onOpenActivity={onOpenActivity} />);
  const group = screen.getByText('Source reading and checks').closest('button')!;
  const marker = group.querySelector<HTMLElement>('[data-attempt-id="read-b"]')!;
  await user.hover(marker);
  const detail = group.querySelector('.work-overview-tooltip')!;
  expect(within(detail as HTMLElement).getByText(/Retrieved a passage from a saved source/)).toBeVisible();
  expect(within(detail as HTMLElement).getByText(/Passage saved/)).toBeVisible();
  expect(within(detail as HTMLElement).getByText(/Finished .*1 min 30 sec/)).toBeVisible();
  expect(within(detail as HTMLElement).getByText(/may include waits; it is not tool runtime/)).toBeVisible();
  expect(within(detail as HTMLElement).getByText('Whole run at saved view: 24 min 0 sec.')).toBeVisible();
  await user.click(marker);
  expect(onOpenActivity).toHaveBeenLastCalledWith('Source reading and checks', 'read-b');
});

function liveApiRun() {
  const snapshot = savedRun();
  snapshot.proofKind = 'live_mission_pending';
  snapshot.attempts = [snapshot.attempts[0]];
  const attempt = snapshot.attempts[0];
  const call = { kind: 'agent_call', id: 'test-call', version: 1 };
  const config = { kind: 'agent_configuration', id: 'test-config', version: 1 };
  snapshot.inspection = {
    snapshotReference: { kind: 'research_snapshot', id: 'test-snapshot', version: 1 },
    initialSourceIds: [], sources: [], acceptedOutputReferences: [], timingBasis: 'observed_local',
    attempts: [{ attemptId: attempt.id, instanceReference: { kind: 'agent_instance', id: attempt.agentId, version: 1 }, configurationReference: config,
      callReferences: [call], inputReferences: [], acceptedOutputReferences: [], explanation: 'Model input prepared.' }],
    records: [
      { reference: config, label: 'Test configuration', data: { provider: 'groq' } },
      { reference: { kind: 'model_response_receipt', id: 'test-response', version: 1 }, label: 'Test response', data: { call_ref: call, attempt_id: attempt.id, status: 'tool_requested' } },
    ],
  };
  return snapshot;
}

it('explains confirmed Groq use and a rejected API request without equating either with a completed answer', async () => {
  const snapshot = liveApiRun();
  const view = render(<WorkOverview snapshot={snapshot} earlierAttemptIds={new Set()} onOpenActivity={vi.fn()} />);
  const user = userEvent.setup();
  screen.getByRole('button', { name: /Source reading and checks/ }).focus();
  await user.keyboard('{ArrowRight}');
  expect(screen.getByRole('tooltip')).toHaveTextContent('Groq API: model response saved; it requested this action.');
  snapshot.inspection!.records[1].data = { ...snapshot.inspection!.records[1].data, status: 'failed', http_status: 429 };
  view.rerender(<WorkOverview snapshot={snapshot} earlierAttemptIds={new Set()} onOpenActivity={vi.fn()} />);
  screen.getByRole('button', { name: /Groq call errors/ }).focus();
  await user.keyboard('{ArrowRight}');
  expect(screen.getByRole('tooltip')).toHaveTextContent('Groq API: response received (HTTP 429; request rejected).');
  await user.keyboard('{Escape}');
  expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
});

it('keeps a transport failure or generic replacement uncertain and names an unsent call only with local evidence', async () => {
  const snapshot = liveApiRun();
  snapshot.attempts[0].status = 'superseded';
  snapshot.attempts[0].toolCalls = [];
  snapshot.inspection!.records[1].data.status = 'failed';
  const user = userEvent.setup();
  const props = { snapshot, earlierAttemptIds: new Set<string>(), onOpenActivity: vi.fn() };
  const view = render(<WorkOverview {...props} />);
  screen.getByRole('button', { name: /System preparation/ }).focus();
  await user.keyboard('{ArrowRight}');
  expect(screen.getByRole('tooltip')).toHaveTextContent('Groq API: request or response not confirmed in saved records.');
  snapshot.inspection!.records.pop();
  view.rerender(<WorkOverview {...props} />);
  expect(screen.getByRole('tooltip')).toHaveTextContent('request or response not confirmed');
  snapshot.inspection!.attempts[0].explanation = 'This input was superseded before provider dispatch.';
  view.rerender(<WorkOverview {...props} />);
  expect(screen.getByRole('tooltip')).toHaveTextContent('Groq API: not sent; preparation stopped locally.');
});

it('does not resolve ambiguous or wrong-version saved records by taking the first match', async () => {
  const snapshot = liveApiRun();
  const user = userEvent.setup();
  const props = { snapshot, earlierAttemptIds: new Set<string>(), onOpenActivity: vi.fn() };
  snapshot.inspection!.records.push(structuredClone(snapshot.inspection!.records[0]));
  const view = render(<WorkOverview {...props} />);
  screen.getByRole('button', { name: /Source reading and checks/ }).focus();
  await user.keyboard('{ArrowRight}');
  expect(screen.getByRole('tooltip')).toHaveTextContent('Model API: model response saved');
  snapshot.inspection!.records.push(structuredClone(snapshot.inspection!.records[1]));
  view.rerender(<WorkOverview {...props} />);
  expect(screen.getByRole('tooltip')).toHaveTextContent('Model API: request or response not confirmed');
  snapshot.inspection!.records = snapshot.inspection!.records.slice(0, 2);
  snapshot.inspection!.records[1].data.call_ref = { kind: 'agent_call', id: 'test-call', version: 2 };
  view.rerender(<WorkOverview {...props} />);
  expect(screen.getByRole('tooltip')).toHaveTextContent('Groq API: request or response not confirmed');
});

it('offers every bar by arrow key from one category focus stop and keeps unfinished timing honest', async () => {
  const snapshot = savedRun();
  const onOpenActivity = vi.fn();
  const user = userEvent.setup();
  render(<WorkOverview snapshot={snapshot} earlierAttemptIds={new Set()} onOpenActivity={onOpenActivity} />);
  const group = screen.getByText('Source reading and checks').closest('button')!;
  group.focus();
  await user.keyboard('{ArrowRight}{ArrowRight}');
  expect(group.querySelector('.work-overview-tooltip')).toHaveTextContent('Started');
  await user.keyboard('{Enter}');
  expect(onOpenActivity).toHaveBeenLastCalledWith('Source reading and checks', 'read-b');
  const unfinished = screen.getByText('Other saved work').closest('button')!;
  unfinished.focus();
  await user.keyboard('{ArrowRight}');
  expect(unfinished.querySelector('.work-overview-tooltip')).toHaveTextContent('Finish not recorded · no elapsed interval');
});

it('keeps the explanation open when the pointer moves from its bar to the tooltip, without navigating on tooltip clicks', async () => {
  const user = userEvent.setup();
  const onOpenActivity = vi.fn();
  render(<WorkOverview snapshot={savedRun()} earlierAttemptIds={new Set()} onOpenActivity={onOpenActivity} />);
  const group = screen.getByRole('button', { name: /Source reading and checks/ });
  const marker = group.querySelector<HTMLElement>('[data-attempt-id="read-a"]')!;
  await user.hover(marker);
  const tooltip = screen.getByRole('tooltip');
  // This user-event version omits relatedTarget on hover transitions.
  // Supply the browser's actual destination so moving within the row is tested.
  fireEvent.mouseOut(marker, { relatedTarget: tooltip });
  fireEvent.mouseOver(tooltip, { relatedTarget: marker });
  expect(tooltip).toBeVisible();
  fireEvent.click(tooltip);
  expect(onOpenActivity).not.toHaveBeenCalled();
  fireEvent.mouseOut(tooltip, { relatedTarget: document.body });
  expect(screen.queryByRole('tooltip')).not.toBeInTheDocument();
});
