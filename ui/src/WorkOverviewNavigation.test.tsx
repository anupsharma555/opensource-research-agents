/* Follow the overview into matching chronological activity without losing run scope. */
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import sample from '../public/sample-run.json';
import type { ResearchSnapshot } from './generated/contracts';
import { ResearchApp } from './App';

afterEach(() => { cleanup(); window.history.replaceState(null, '', '/'); });

it('keeps overview bars compact and opens only matching steps in Activity, with exact details and a return path', async () => {
  const snapshot = structuredClone(sample) as ResearchSnapshot;
  snapshot.proofKind = 'live_mission_pending';
  snapshot.events = [];
  snapshot.messages = [];
  const base = snapshot.attempts[0];
  snapshot.attempts = [
    { ...base, id: 'source-first', toolCalls: [{ name: 'read_source_window', status: 'completed' }], startedAt: '2026-10-08T14:01:00Z', endedAt: '2026-10-08T14:02:00Z', resultSummary: 'Saved the first checked source passage.' },
    { ...base, id: 'peer-request', toolCalls: [{ name: 'peer_request', status: 'request_saved' }], startedAt: '2026-10-08T14:02:00Z', endedAt: '2026-10-08T14:03:00Z' },
    { ...base, id: 'source-second', agentId: snapshot.agents.find(agent => agent.id !== base.agentId)!.id, toolCalls: [{ name: 'read_source_window', status: 'completed' }], startedAt: '2026-10-08T14:03:00Z', endedAt: '2026-10-08T14:04:00Z' },
  ];
  const user = userEvent.setup();
  render(<ResearchApp snapshot={snapshot} live={{ runId: 'overview-navigation', model: 'saved-model', runs: [], error: null, form: null, requestedAgentIds: [], onSelectRun: vi.fn() }} />);
  const overview = screen.getByRole('region', { name: 'Grouped work overview' });
  expect(overview.querySelectorAll('.work-overview-mark')).toHaveLength(3);
  expect(screen.queryByText('Browse all saved work steps')).not.toBeInTheDocument();
  await user.click(within(overview).getByRole('button', { name: 'Source reading and checks: view 2 steps in Activity' }));
  expect(screen.getByRole('heading', { name: 'Activity' })).toBeVisible();
  expect(screen.getByText('Selected work category')).toBeVisible();
  const rows = [...document.querySelectorAll<HTMLElement>('.activity-item')];
  expect(rows.map(row => row.dataset.attemptId)).toEqual(['source-first', 'source-second']);
  await user.click(within(rows[0]).getByRole('button', { name: 'Expand activity details' }));
  expect(within(rows[0]).getAllByText('Saved the first checked source passage.').some(node => node.closest('.selected-work-step'))).toBe(true);
  await user.click(screen.getByRole('button', { name: 'Show all work steps' }));
  expect([...document.querySelectorAll<HTMLElement>('.activity-item')].map(row => row.dataset.attemptId)).toEqual(['source-first', 'peer-request', 'source-second']);
  await user.click(screen.getByRole('button', { name: 'Research review' }));
  await user.click(screen.getByRole('button', { name: 'Peer requests and reviews: view 1 step in Activity' }));
  expect([...document.querySelectorAll<HTMLElement>('.activity-item')].map(row => row.dataset.attemptId)).toEqual(['peer-request']);
  await user.click(screen.getByRole('button', { name: '← Back to Research review' }));
  expect(screen.getByRole('region', { name: 'Grouped work overview' })).toBeVisible();
});

it('opens a selected time bar directly into its exact expanded Activity step and keeps the category map usable', async () => {
  const snapshot = structuredClone(sample) as ResearchSnapshot;
  snapshot.proofKind = 'live_mission_pending';
  snapshot.events = [];
  snapshot.messages = [];
  const base = snapshot.attempts[0];
  snapshot.attempts = [
    { ...base, id: 'first-read', toolCalls: [{ name: 'read_source_window', status: 'completed' }], startedAt: '2026-10-08T14:01:00Z', endedAt: '2026-10-08T14:02:00Z' },
    { ...base, id: 'second-read', toolCalls: [{ name: 'read_source_window', status: 'completed' }], startedAt: '2026-10-08T14:03:00Z', endedAt: '2026-10-08T14:04:00Z' },
    { ...base, id: 'peer-request', toolCalls: [{ name: 'peer_request', status: 'request_saved' }], startedAt: '2026-10-08T14:05:00Z', endedAt: '2026-10-08T14:06:00Z' },
  ];
  const user = userEvent.setup();
  render(<ResearchApp snapshot={snapshot} live={{ runId: 'bar-navigation', model: 'saved-model', runs: [], error: null, form: null, requestedAgentIds: [], onSelectRun: vi.fn() }} />);
  const overview = screen.getByRole('region', { name: 'Grouped work overview' });
  await user.click(overview.querySelector<HTMLElement>('[data-attempt-id="second-read"]')!);
  expect(screen.getByRole('heading', { name: 'Activity' })).toBeVisible();
  const selected = document.querySelector<HTMLElement>('.activity-item[data-attempt-id="second-read"]')!;
  expect(within(selected).getByRole('button', { name: 'Hide activity details' })).toHaveFocus();
  expect(within(selected).getByText('Retrieved a passage from a saved source')).toBeVisible();
  expect(document.querySelectorAll('.activity-item')).toHaveLength(2);
  const categories = screen.getByRole('region', { name: 'Saved work by action' });
  await user.click(within(categories).getByRole('button', { name: 'Peer requests and reviews: show 1 saved step in time order' }));
  expect([...document.querySelectorAll<HTMLElement>('.activity-item')].map(row => row.dataset.attemptId)).toEqual(['peer-request']);
  await user.click(screen.getByRole('button', { name: 'Show all work steps' }));
  expect([...document.querySelectorAll<HTMLElement>('.activity-item')].map(row => row.dataset.attemptId)).toEqual(['first-read', 'second-read', 'peer-request']);
});

it('labels uncertain origin when the exact earlier-run snapshot is unavailable', () => {
  const snapshot = structuredClone(sample) as ResearchSnapshot;
  snapshot.proofKind = 'live_mission_pending';
  snapshot.parentSnapshotId = 'missing-exact-parent';
  render(<ResearchApp snapshot={snapshot} live={{ runId: 'unresolved-origin', model: 'saved-model', runs: [], error: null, form: null, requestedAgentIds: [], onSelectRun: vi.fn() }} />);
  expect(screen.getByText(`${snapshot.attempts.length} saved steps in this saved view`)).toBeVisible();
  expect(screen.getByText(/Some steps may be inherited; their run origin is not confirmed/)).toBeVisible();
  expect(screen.queryByText(/saved steps across this run/)).not.toBeInTheDocument();
  expect(screen.getByText(/Agents named in this saved view/)).toBeInTheDocument();
  expect(screen.queryByText(/Agents with work in this run/)).not.toBeInTheDocument();
});

it('clears a saved-work category when switching to messages instead of silently hiding those records', async () => {
  const user = userEvent.setup();
  render(<ResearchApp snapshot={structuredClone(sample) as ResearchSnapshot} initialScreen="activity" />);
  const categories = screen.getByRole('region', { name: 'Saved work by action' });
  await user.click(within(categories).getAllByRole('button')[0]);
  expect(screen.getByText('Selected work category')).toBeVisible();
  await user.click(screen.getByRole('button', { name: 'Messages' }));
  expect(screen.queryByText('Selected work category')).not.toBeInTheDocument();
  expect(screen.getByText(/work-action category was cleared/)).toBeVisible();
  expect(document.querySelectorAll('.activity-item').length).toBeGreaterThan(0);
});

it('keeps step and issue counts scannable while folding the secondary technical count grid', async () => {
  const user = userEvent.setup();
  render(<ResearchApp snapshot={structuredClone(sample) as ResearchSnapshot} initialScreen="activity" />);
  const counts = screen.getByText(/Saved counts: .* work steps .* with recorded issues/).closest('details')!;
  expect(counts).not.toHaveAttribute('open');
  expect(screen.getByRole('region', { name: 'Saved work by action' })).toBeVisible();
  await user.click(within(counts).getByText(/Saved counts:/));
  expect(counts).toHaveAttribute('open');
  expect(within(counts).getByText('Saved model responses')).toBeVisible();
  expect(within(counts).getByText('Peer requests')).toBeVisible();
});

it('keeps an updated step visible when it changes action type during the same run', async () => {
  const snapshot = structuredClone(sample) as ResearchSnapshot;
  snapshot.proofKind = 'live_mission_pending';
  snapshot.events = [];
  snapshot.messages = [];
  snapshot.attempts = [{ ...snapshot.attempts[0], id: 'changing-step', status: 'running', endedAt: null, toolCalls: [] }];
  const live = { runId: 'changing-group', model: 'saved-model', runs: [], error: null, form: null, requestedAgentIds: [], onSelectRun: vi.fn() };
  const user = userEvent.setup();
  const { rerender } = render(<ResearchApp snapshot={snapshot} live={live} />);
  await user.click(screen.getByRole('button', { name: 'Other saved work: view 1 step in Activity' }));
  const updated = structuredClone(snapshot);
  updated.snapshotId = 'after-source-read';
  updated.attempts[0] = { ...updated.attempts[0], status: 'completed', endedAt: updated.capturedAt, toolCalls: [{ name: 'read_source_window', status: 'completed' }] };
  rerender(<ResearchApp snapshot={updated} live={live} />);
  expect(await screen.findByText(/A selected step changed its action type/)).toBeVisible();
  const rows = [...document.querySelectorAll<HTMLElement>('.activity-item')];
  expect(rows.map(row => row.dataset.attemptId)).toEqual(['changing-step']);
  expect(within(rows[0]).getByText('Passage saved')).toBeVisible();
  expect(within(rows[0]).getByText('Updated since opening')).toBeVisible();
});
