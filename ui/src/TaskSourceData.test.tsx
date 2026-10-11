import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { TaskSourceData } from './TaskSourceData';
import type { ResearchSnapshot } from './generated/contracts';
import sample from '../public/sample-run.json';

afterEach(cleanup);

function savedInput() {
  const snapshot = structuredClone(sample) as ResearchSnapshot;
  snapshot.inspection = {
    snapshotReference: { kind: 'research_snapshot', id: snapshot.snapshotId, version: 1 }, timingBasis: 'observed_local',
    initialSourceIds: ['A1', 'A2'], acceptedOutputReferences: [], attempts: [], records: [],
    sources: snapshot.sources.slice(0, 2).map((source, index) => ({ sourceId: source.id,
      copyReference: { kind: 'source_copy', id: source.copyId, version: source.version }, encounterReferences: [],
      origin: index === 0 ? 'reused' : 'supplied', ...(index === 0 ? { fromSnapshotId: 'earlier-synthetic-snapshot' } : {}),
    })),
  };
  return snapshot;
}

it('distinguishes inherited evidence from supplied input and opens that exact source in Evidence', async () => {
  const user = userEvent.setup();
  const snapshot = savedInput();
  const open = vi.fn();
  render(<TaskSourceData snapshot={snapshot} onOpenSource={open} />);
  const sources = screen.getByRole('region', { name: 'Sources available at the start' });
  await user.click(within(sources).getByText('View starting sources (2)'));
  expect(within(sources).getByText('Reused from an earlier run')).toBeVisible();
  expect(within(sources).getByText('Supplied with this request')).toBeVisible();
  expect(within(sources).queryByText(snapshot.sources[0].passages[0].text)).not.toBeInTheDocument();
  await user.click(within(sources).getAllByRole('button', { name: 'Read in Evidence →' })[0]);
  expect(open).toHaveBeenCalledWith('A1');
  await user.click(within(sources).getAllByText('Source details')[0]);
  expect(within(sources).getByText('Reused from saved snapshot earlier-synthetic-snapshot')).toBeVisible();
});

it('retains unknown, mismatched and missing starting-source provenance without calling it supplied', async () => {
  const user = userEvent.setup();
  const snapshot = savedInput();
  snapshot.inspection!.sources[0].copyReference.version += 1;
  snapshot.inspection!.sources = snapshot.inspection!.sources.slice(0, 1);
  snapshot.inspection!.initialSourceIds.push('absent', 'absent');
  render(<TaskSourceData snapshot={snapshot} />);
  await user.click(screen.getByText('View starting sources (3)'));
  expect(screen.getAllByText('How this source was added is not recorded')).toHaveLength(3);
  expect(screen.queryByText('Supplied with this request')).not.toBeInTheDocument();
  await user.click(screen.getAllByText('Source details')[2]);
  expect(screen.getByText('This starting source is missing, or more than one saved copy matches it.')).toBeVisible();
});

it('does not mistake later retrievals for sources present at the start', () => {
  const snapshot = savedInput();
  snapshot.inspection!.initialSourceIds = [];
  render(<TaskSourceData snapshot={snapshot} />);
  expect(screen.getByText('No starting material is recorded. Sources found during the run appear in Evidence.')).toBeVisible();
  expect(screen.queryByText(snapshot.sources[0].title)).not.toBeInTheDocument();
});

it('keeps the canonical sample packet and unknown legacy input explicit', async () => {
  const user = userEvent.setup();
  const snapshot = structuredClone(sample) as ResearchSnapshot;
  const view = render(<TaskSourceData snapshot={snapshot} />);
  expect(screen.getByRole('region', { name: 'Source data supplied with this task' })).toBeVisible();
  await user.click(screen.getByText('View supplied source data (4 records)'));
  expect(screen.getByText('3 simulated study abstracts and 1 access notice · real clinical labels')).toBeVisible();
  view.rerender(<TaskSourceData snapshot={{ ...snapshot, snapshotId: 'unknown-snapshot' }} />);
  expect(screen.getByText('The source material supplied at the start was not recorded for this snapshot.')).toBeVisible();
});
