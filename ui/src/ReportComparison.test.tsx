import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it } from 'vitest';
import type { ResearchSnapshot, SavedRecordRef } from './generated/contracts';
import { ReportComparison } from './ReportComparison';
import sample from '../public/sample-run.json';
import baseline from '../public/sample-run-baseline.json';

const ref = (kind: string, id: string, version = 1): SavedRecordRef => ({ kind, id, version });

function withAccepted(snapshot: ResearchSnapshot, reports: Array<{ id: string; version: number }>) {
  snapshot.inspection = {
    snapshotReference: ref('research_snapshot', snapshot.snapshotId),
    timingBasis: 'controlled_clock', initialSourceIds: [], sources: [], attempts: [], records: [],
    acceptedOutputReferences: reports.map(report => ref('report', report.id, report.version)),
  };
  return snapshot;
}

afterEach(() => cleanup());

it('keeps each report with its own saved passage and renders recorded formatting readably', async () => {
  const user = userEvent.setup();
  const parent = withAccepted(structuredClone(baseline) as ResearchSnapshot, [{ id: 'report-v1', version: 1 }]);
  const current = withAccepted(structuredClone(sample) as ResearchSnapshot, [{ id: 'report-v2', version: 2 }]);
  current.parentSnapshotId = parent.snapshotId;
  parent.sources[0].passages.find(passage => passage.spanId === 'A1-result')!.text = 'EARLIER PASSAGE ONLY';
  current.sources[0].passages.find(passage => passage.spanId === 'A1-result')!.text = 'FOLLOW-UP PASSAGE ONLY';
  current.inspection!.sources = [{
    sourceId: 'A1', copyReference: ref('source_copy', current.sources[0].copyId, current.sources[0].version),
    encounterReferences: [], origin: 'reused', fromSnapshotId: parent.snapshotId,
  }];
  current.artifacts.find(report => report.id === 'report-v2')!.body = '# Findings\n\nA **revised reading** with saved evidence.\n\n- First point\n- Second point\n\nA later paragraph records uncertainty.';

  const { container, rerender } = render(<ReportComparison currentSnapshot={current} parentSnapshot={parent} />);
  const earlier = screen.getByRole('region', { name: 'Earlier run' });
  const followup = screen.getByRole('region', { name: 'This follow-up' });
  expect(screen.getByText(/earlier run is the work this follow-up continues/)).toBeVisible();
  expect(within(earlier).getByText('Run started')).toBeVisible();
  expect(within(followup).getByText('View saved')).toBeVisible();
  expect(within(followup).getByText(/4 saved source copies/)).not.toBeVisible();
  expect(within(followup).getByText('1 answer available')).toBeVisible();
  expect(followup.querySelector('.report-comparison-body')).toBeNull();
  await user.click(within(followup).getByRole('button', { name: 'Read answer' }));
  await user.click(within(earlier).getByRole('button', { name: 'Read answer' }));
  const reportBody = followup.querySelector('.report-comparison-body') as HTMLElement;
  expect(within(reportBody).getByRole('heading', { name: 'Findings', level: 4 })).toBeVisible();
  expect(within(followup).getByText('revised reading', { selector: 'strong' })).toBeVisible();
  expect(within(reportBody).getByRole('list')).toBeVisible();
  expect(within(reportBody).getAllByRole('listitem')).toHaveLength(2);
  expect(within(reportBody).getByText('A later paragraph records uncertainty.', { selector: 'p' })).toBeVisible();
  expect(within(followup).getByRole('heading', { name: 'Limitations stated in the answer' })).toBeVisible();
  expect(screen.getByRole('button', { name: 'What does earlier run mean?' })).toBeVisible();
  expect(within(followup).getByRole('button', { name: 'What does accepted report mean?' })).toBeVisible();
  expect(within(earlier).getByRole('button', { name: 'What does accepted report mean?' })).toBeVisible();
  expect(container.textContent).not.toContain('**revised reading**');
  expect(container.textContent).not.toContain('# Findings');
  expect(within(followup).queryByText('Early abstract reading')).not.toBeInTheDocument();
  expect(within(earlier).getByText(/Report report-v1 · version 1/)).not.toBeVisible();

  await user.click(within(earlier).getByRole('button', { name: 'Check cited evidence' }));
  await user.click(within(followup).getByRole('button', { name: 'Check cited evidence' }));
  await user.click(within(earlier).getByText('Citation 1'));
  await user.click(within(followup).getByText('Citation 1'));
  expect(within(earlier).getByText('EARLIER PASSAGE ONLY')).toBeVisible();
  expect(within(earlier).queryByText('FOLLOW-UP PASSAGE ONLY')).not.toBeInTheDocument();
  expect(within(followup).getByText('FOLLOW-UP PASSAGE ONLY')).toBeVisible();
  expect(within(followup).queryByText('EARLIER PASSAGE ONLY')).not.toBeInTheDocument();
  expect(within(followup).getByText(/This source was reused from earlier work/)).toBeVisible();
  current.inspection!.sources[0].copyReference.id = 'unrelated-copy';
  rerender(<ReportComparison currentSnapshot={current} parentSnapshot={parent} />);
  expect(within(followup).queryByText(/This source was reused from earlier work/)).not.toBeInTheDocument();
  expect(within(followup).getAllByText(/This is the exact excerpt saved with this answer/).length).toBeGreaterThan(0);
});

it('does not borrow an earlier passage when the follow-up citation is missing or ambiguous', async () => {
  const user = userEvent.setup();
  const parent = withAccepted(structuredClone(baseline) as ResearchSnapshot, [{ id: 'report-v1', version: 1 }]);
  const current = withAccepted(structuredClone(sample) as ResearchSnapshot, [{ id: 'report-v2', version: 2 }]);
  current.parentSnapshotId = parent.snapshotId;
  current.sources = current.sources.filter(source => source.id !== 'A1');
  current.artifacts.find(report => report.id === 'report-v2')!.citations = [{ sourceId: 'A1', spanId: 'A1-result' }];
  const view = render(<ReportComparison currentSnapshot={current} parentSnapshot={parent} />);
  const followup = screen.getByRole('region', { name: 'This follow-up' });
  await user.click(within(followup).getByRole('button', { name: 'Check cited evidence' }));
  await user.click(within(followup).getByText('Citation 1'));
  expect(within(followup).getByText(/This citation cannot be shown: source unavailable/)).toBeVisible();
  expect(within(followup).getByText(/The other run’s sources are not substituted/)).toBeVisible();

  current.sources = [structuredClone(parent.sources[0]), structuredClone(parent.sources[0])];
  view.rerender(<ReportComparison currentSnapshot={current} parentSnapshot={parent} />);
  expect(within(followup).getByText(/This citation cannot be shown: source identity unclear/)).toBeVisible();
});

it('shows an exactly accepted report whose saved artifact kind is research_contribution', async () => {
  const user = userEvent.setup();
  const current = withAccepted(structuredClone(sample) as ResearchSnapshot, [{ id: 'report-v2', version: 2 }]);
  const report = current.artifacts.find(item => item.id === 'report-v2')!;
  report.kind = 'research_contribution';
  current.sources.find(source => source.id === 'A1')!.passages.find(passage => passage.spanId === 'A1-result')!.text = 'EXACT CURRENT PASSAGE';
  const view = render(<ReportComparison currentSnapshot={current} mode="sample" />);
  const column = screen.getByRole('region', { name: 'Current sample run' });
  await user.click(within(column).getByRole('button', { name: 'Check cited evidence' }));
  expect(within(column).getByText('Revised evidence summary')).toBeVisible();
  expect(within(column).getByText('Saved as a run output')).toBeVisible();
  await user.click(within(column).getByText('Citation 1'));
  expect(within(column).getByText('EXACT CURRENT PASSAGE')).toBeVisible();
  current.inspection!.acceptedOutputReferences[0].version = 99;
  view.rerender(<ReportComparison currentSnapshot={current} mode="sample" />);
  expect(within(column).queryByText('Revised evidence summary')).not.toBeInTheDocument();
  expect(within(column).getByText(/exact saved text is missing from this view/)).toBeVisible();
});

it('keeps multiple accepted outputs separate and makes absent or unresolved reports explicit', async () => {
  const user = userEvent.setup();
  const current = withAccepted(structuredClone(sample) as ResearchSnapshot, [
    { id: 'report-v1', version: 1 }, { id: 'report-v2', version: 2 }, { id: 'missing-report', version: 1 },
  ]);
  render(<ReportComparison currentSnapshot={current} mode="sample" />);
  const column = screen.getByRole('region', { name: 'Current sample run' });
  await user.click(within(column).getByRole('button', { name: 'Read answer' }));
  expect(within(column).getByText('Early abstract reading')).toBeVisible();
  expect(within(column).getByText('Revised evidence summary')).toBeVisible();
  expect(within(column).getByText(/No single main answer is identified/)).toBeVisible();
  expect(within(column).getByText(/exact saved text is missing from this view/)).toBeVisible();
  expect(screen.getByText('No baseline sample is available to compare.')).toBeVisible();
});

it('shows loading, failed and absent parent states while preserving the current report', async () => {
  const user = userEvent.setup();
  const current = withAccepted(structuredClone(sample) as ResearchSnapshot, [{ id: 'report-v2', version: 2 }]);
  current.parentSnapshotId = 'exact-parent-not-loaded';
  const view = render(<ReportComparison currentSnapshot={current} parentState="loading" />);
  expect(screen.getByRole('status')).toHaveTextContent('Loading the earlier run’s saved work');
  await user.click(within(screen.getByRole('region', { name: 'This follow-up' })).getByRole('button', { name: 'Read answer' }));
  expect(screen.getByRole('region', { name: 'This follow-up' })).toHaveTextContent('Revised evidence summary');
  view.rerender(<ReportComparison currentSnapshot={current} parentState="error" />);
  expect(screen.getByText(/earlier run’s saved work could not be loaded/)).toBeVisible();
  view.rerender(<ReportComparison currentSnapshot={current} />);
  expect(screen.getByText('The earlier run’s saved work is not available here.')).toBeVisible();
  const wrong = structuredClone(baseline) as ResearchSnapshot;
  view.rerender(<ReportComparison currentSnapshot={current} parentSnapshot={wrong} />);
  expect(screen.getByText(/does not match the version linked by this follow-up/)).toBeVisible();
  expect(screen.getByRole('region', { name: 'Earlier run' })).not.toHaveTextContent(wrong.artifacts[0].body);
  current.parentSnapshotId = null;
  view.rerender(<ReportComparison currentSnapshot={current} />);
  expect(screen.getByRole('region', { name: 'This run' })).toHaveTextContent('Revised evidence summary');
});

it('does not call uninspected report artifacts accepted or hide additional saved outputs', async () => {
  const user = userEvent.setup();
  const current = structuredClone(sample) as ResearchSnapshot;
  current.inspection = undefined;
  render(<ReportComparison currentSnapshot={current} mode="sample" />);
  const column = screen.getByRole('region', { name: 'Current sample run' });
  expect(within(column).getByText(/records do not confirm whether it was accepted/)).toBeVisible();
  await user.click(within(column).getByRole('button', { name: 'Read answer' }));
  expect(within(column).getByText('Early abstract reading')).toBeVisible();
  expect(within(column).getByText('Revised evidence summary')).toBeVisible();
  expect(within(column).getAllByText(/Saved text · output status unknown/)).toHaveLength(2);
});


it('separates request, instructions and saved times without inventing a report timestamp', async () => {
  const user = userEvent.setup();
  const current = withAccepted(structuredClone(sample) as ResearchSnapshot, [{ id: 'report-v2', version: 2 }]);
  current.mission.question = 'Compare the two published trial abstracts.';
  current.mission.scope = 'Limit the answer to the abstract text; distinguish study populations.';
  current.mission.startedAt = '2026-01-01T12:00:00Z';
  current.capturedAt = '2026-01-01T13:00:00Z';
  render(<ReportComparison currentSnapshot={current} mode="sample" />);
  const column = screen.getByRole('region', { name: 'Current sample run' });
  expect(within(column).queryByText(current.mission.question)).not.toBeInTheDocument();
  await user.click(within(column).getByRole('button', { name: 'Read request' }));
  expect(within(column).getByText(current.mission.question)).toBeVisible();
  expect(within(column).getByText(current.mission.scope)).toBeVisible();
  expect(column.querySelector('time[datetime="2026-01-01T12:00:00Z"]')).toBeVisible();
  expect(column.querySelector('time[datetime="2026-01-01T13:00:00Z"]')).toBeVisible();
  expect(within(column).queryByText(/Answer saved at/)).not.toBeInTheDocument();
  await user.click(within(column).getByRole('button', { name: 'Read answer' }));
  await user.click(within(column).getByText('Answer record details'));
  expect(within(column).getByText(/answer’s own save time is unavailable/)).toBeVisible();
});

it('distinguishes no result, inaccessible result and unknown result without implying a stopped run is active', () => {
  const current = withAccepted(structuredClone(sample) as ResearchSnapshot, []);
  current.mission.status = 'completed';
  const view = render(<ReportComparison currentSnapshot={current} mode="sample" />);
  const column = screen.getByRole('region', { name: 'Current sample run' });
  expect(within(column).getByText('No answer available to compare')).toBeVisible();
  expect(within(column).getByText('No answer is recorded as an output in this saved view.')).toBeVisible();
  expect(within(column).getByText(/run is marked completed. That status does not supply/)).toBeVisible();
  expect(within(column).queryByText(/still be in progress/)).not.toBeInTheDocument();
  current.inspection!.acceptedOutputReferences = [ref('report', 'absent-report')];
  view.rerender(<ReportComparison currentSnapshot={current} mode="sample" />);
  expect(within(column).getByText('Saved answer unavailable')).toBeVisible();
  expect(within(column).getByText(/exact saved text is missing/)).toBeVisible();
  current.inspection = undefined;
  current.artifacts = [];
  current.mission.status = 'cancelled';
  view.rerender(<ReportComparison currentSnapshot={current} mode="sample" />);
  expect(within(column).getByText(/whether one was produced is unknown/)).toBeVisible();
  expect(within(column).getByText('This run is marked cancelled.')).toBeVisible();
  expect(within(column).queryByText(/The answer text is saved/)).not.toBeInTheDocument();
});

it('shows unfinished comparison before long requests and offers direct, independently scoped detail controls', async () => {
  const user = userEvent.setup();
  const parent = withAccepted(structuredClone(baseline) as ResearchSnapshot, [{ id: 'report-v1', version: 1 }]);
  const current = withAccepted(structuredClone(sample) as ResearchSnapshot, []);
  current.parentSnapshotId = parent.snapshotId;
  render(<ReportComparison currentSnapshot={current} parentSnapshot={parent} />);
  const earlier = screen.getByRole('region', { name: 'Earlier run' });
  const followup = screen.getByRole('region', { name: 'This follow-up' });
  expect(within(earlier).getByText('1 answer available')).toBeVisible();
  expect(within(followup).getByText('No answer available to compare')).toBeVisible();
  expect(within(followup).getByRole('button', { name: 'Read answer' })).toBeDisabled();
  expect(within(followup).getByRole('button', { name: 'Check cited evidence' })).toBeDisabled();
  expect(screen.queryByText(/A later answer is not necessarily better/)).not.toBeInTheDocument();
  await user.click(within(earlier).getByRole('button', { name: 'Read answer' }));
  expect(within(earlier).getByRole('region', { name: 'Earlier run answers' })).toBeVisible();
  expect(within(earlier).queryByText(parent.mission.question)).not.toBeInTheDocument();
  await user.click(within(followup).getByRole('button', { name: 'Read request' }));
  expect(within(followup).getByRole('region', { name: 'This follow-up request' })).toHaveTextContent(current.mission.question);
  expect(within(earlier).getByRole('region', { name: 'Earlier run answers' })).toBeVisible();
  await user.click(within(earlier).getByRole('button', { name: 'Read answer' }));
  expect(within(earlier).queryByRole('region', { name: 'Earlier run answers' })).not.toBeInTheDocument();
});

it('distinguishes source references written in the answer from checkable links and unavailable saved links', async () => {
  const user = userEvent.setup();
  const current = withAccepted(structuredClone(sample) as ResearchSnapshot, [{ id: 'report-v2', version: 2 }]);
  const report = current.artifacts.find(item => item.id === 'report-v2')!;
  report.body = 'Citations: an author and source ID are written in this answer.';
  report.citations = [];
  const view = render(<ReportComparison currentSnapshot={current} mode="sample" />);
  const column = screen.getByRole('region', { name: 'Current sample run' });
  expect(within(column).getByText(/0 checkable source links/)).toBeVisible();
  expect(within(column).getByText('Source references written in the answer are not linked here to exact saved passages.')).toBeVisible();
  expect(within(column).queryByText(/saved links unavailable/)).not.toBeInTheDocument();
  await user.click(within(column).getByRole('button', { name: 'Read answer' }));
  expect(within(column).getByText(report.body)).toBeVisible();
  await user.click(within(column).getByRole('button', { name: 'Check cited evidence' }));
  expect(within(column).getByText(/References written in the answer may still name sources/)).toBeVisible();
  report.citations = [{ sourceId: 'A1', spanId: 'missing-passage' }];
  view.rerender(<ReportComparison currentSnapshot={current} mode="sample" />);
  expect(within(column).getByText(/0 checkable source links · 1 saved link unavailable/)).toBeVisible();
  expect(within(column).queryByText(/Source references written in the answer are not linked here/)).not.toBeInTheDocument();
  expect(within(column).getByText('excerpt unavailable')).toBeVisible();
  report.citations = [{ sourceId: 'A1', spanId: 'A1-result' }];
  view.rerender(<ReportComparison currentSnapshot={current} mode="sample" />);
  expect(within(column).getByText(/1 checkable source link/)).toBeVisible();
  expect(within(column).queryByText(/saved link unavailable/)).not.toBeInTheDocument();
});
