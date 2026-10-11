import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import sample from '../public/sample-run.json';
import type { ResearchSnapshot } from './generated/contracts';
import { ActivityWorkOverview } from './ActivityWorkOverview';

afterEach(cleanup);

it('maps the same scoped saved attempts to clear actions without counting inherited work or claiming research results', async () => {
  const snapshot = structuredClone(sample) as ResearchSnapshot;
  const base = snapshot.attempts[0];
  snapshot.attempts = [
    { ...base, id: 'earlier', toolCalls: [{ name: 'read_source_window', status: 'completed' }] },
    { ...base, id: 'source', toolCalls: [{ name: 'read_source_window', status: 'completed' }] },
    { ...base, id: 'context', toolCalls: [{ name: 'read_context_page', status: 'completed' }] },
    { ...base, id: 'peer', toolCalls: [{ name: 'peer_request', status: 'request_saved' }] },
  ];
  const onSelectGroup = vi.fn();
  const user = userEvent.setup();
  render(<ActivityWorkOverview snapshot={snapshot} earlierAttemptIds={new Set(['earlier'])} runScopeKnown selectedGroup={null} onSelectGroup={onSelectGroup} />);
  const overview = screen.getByRole('region', { name: 'Saved work by action' });
  expect(within(overview).getByText('3 saved steps in this run', { exact: false })).toBeVisible();
  expect(within(overview).getAllByRole('button')).toHaveLength(3);
  expect(within(overview).getByText(/Messages and host events remain available/)).toBeVisible();
  await user.click(within(overview).getByRole('button', { name: 'Peer requests and reviews: show 1 saved step in time order' }));
  expect(onSelectGroup).toHaveBeenCalledWith('Peer requests and reviews');
});

it('labels unresolved earlier-run origin without assigning every saved step to the selected run', () => {
  const snapshot = structuredClone(sample) as ResearchSnapshot;
  snapshot.parentSnapshotId = 'unavailable-parent';
  render(<ActivityWorkOverview snapshot={snapshot} earlierAttemptIds={new Set()} runScopeKnown={false} selectedGroup={null} onSelectGroup={vi.fn()} />);
  expect(screen.getByText(/some may belong to an earlier run/)).toBeVisible();
  expect(screen.queryByText(/saved steps in this run/)).not.toBeInTheDocument();
});
