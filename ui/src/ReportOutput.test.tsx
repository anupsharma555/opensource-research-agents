/* Verify that saved answers lead while citations and attribution keep their
   exact provenance and cannot imply unrecorded support or peer review. */
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it, vi } from 'vitest';
import type { ResearchSnapshot } from './generated/contracts';
import { ReportOutput } from './ReportOutput';
import { reportReferences } from './reportProvenance';
import sample from '../public/sample-run.json';

const fixture = sample as ResearchSnapshot;
const savedReport = fixture.artifacts.find(artifact => artifact.id === fixture.mission.reportArtifactId)!;

afterEach(() => cleanup());

describe('saved report view', () => {
  it('shows the saved answer first, then expandable source references and downstream limitations', async () => {
    const user = userEvent.setup();
    const openSource = vi.fn();
    const { container } = render(<ReportOutput report={savedReport} sources={fixture.sources} onOpenSource={openSource} />);
    expect(container.querySelector('.report-output')?.firstElementChild).toHaveAttribute('aria-label', 'Saved answer');
    expect(within(screen.getByRole('region', { name: 'Saved answer' })).getByText(/Together they do not establish benefit/)).toBeVisible();
    const disclosure = screen.getByText('Sources and supporting passages (2 references)').closest('details')!;
    expect(disclosure).not.toHaveAttribute('open');
    expect(screen.getByRole('region', { name: 'Saved limitations' })).toHaveTextContent('methods table');
    await user.click(within(disclosure).getByText('Sources and supporting passages (2 references)'));
    const table = within(disclosure).getByRole('table', { name: 'Sources and supporting passages' });
    expect(within(table).getAllByText('Saved citation')).toHaveLength(2);
    expect(within(disclosure).getByText(/Check the passages to assess support/)).toBeInTheDocument();
    await user.click(within(table).getByRole('button', { name: 'Open passage 1 →' }));
    expect(openSource).toHaveBeenCalledWith('A1', 'A1-result');
  });

  it('separates uniquely resolved IDs in saved text from structured citations', async () => {
    const user = userEvent.setup();
    const report = { ...savedReport, citations: [{ sourceId: 'A1', spanId: 'A1-result' }], body: 'Check A1::A1-result and [A2::A2-result]. A4 was also supplied but is not cited.' };
    const openSource = vi.fn();
    render(<ReportOutput report={report} sources={fixture.sources} onOpenSource={openSource} />);
    const disclosure = screen.getByText('Sources and supporting passages (2 references)').closest('details')!;
    await user.click(within(disclosure).getByText('Sources and supporting passages (2 references)'));
    const table = within(disclosure).getByRole('table', { name: 'Sources and supporting passages' });
    expect(within(table).getByText('Saved citation')).toBeInTheDocument();
    expect(within(table).getByText('Reference found in saved text')).toBeInTheDocument();
    expect(within(table).queryByText(fixture.sources.find(source => source.id === 'A4')!.title)).not.toBeInTheDocument();
    await user.click(screen.getByRole('button', { name: 'Open cited passage 1' }));
    expect(openSource).toHaveBeenCalledWith('A1', 'A1-result');
    expect(report.citations).toEqual([{ sourceId: 'A1', spanId: 'A1-result' }]);
  });

  it('does not recover ambiguous text tokens or turn an unavailable citation into a passage link', async () => {
    const user = userEvent.setup();
    const report = { ...savedReport, citations: [{ sourceId: 'A1', spanId: 'missing' }], body: 'The text says [A1], [A1-result], and [A1::A1-result].', limitations: [] };
    const sources = [...fixture.sources, { ...fixture.sources[0], copyId: 'duplicate-copy' }];
    expect(reportReferences(report, sources)).toHaveLength(1);
    expect(reportReferences({ ...report, citations: [], body: '[A3] [A1-result] [A1:A1-result]' }, fixture.sources)).toHaveLength(0);
    expect(reportReferences({ ...report, citations: [], body: 'A2::A2-result.' }, fixture.sources)).toMatchObject([{ sourceId: 'A2', spanId: 'A2-result', basis: 'saved_text' }]);
    expect(reportReferences({ ...report, citations: [], body: 'A2::A2-result-extra' }, fixture.sources)).toHaveLength(0);
    render(<ReportOutput report={report} sources={sources} onOpenSource={vi.fn()} />);
    await user.click(screen.getByText('Sources and supporting passages (1 reference)'));
    expect(screen.getByText('Exact passage unavailable or ambiguous in this snapshot.')).toBeInTheDocument();
    expect(screen.queryByRole('button', { name: /Open passage/ })).not.toBeInTheDocument();
    expect(screen.queryByRole('region', { name: 'Saved limitations' })).not.toBeInTheDocument();
    const details = screen.getByText('Report details').closest('details')!;
    await user.click(within(details).getByText('Report details'));
    expect(within(details).getByText(/Not separately recorded in this artifact/)).toBeInTheDocument();
    expect(within(details).getByText('v2')).toBeInTheDocument();
    expect(within(details).getByText(report.body, { selector: 'pre' })).toBeInTheDocument();
  });

  it('uses an explicit producer pointer and only a uniquely linked peer request and reply', async () => {
    const user = userEvent.setup();
    const saved = structuredClone(fixture);
    const report = { ...savedReport, producerAttemptId: 'attempt-synthesis' };
    const request = { ...saved.messages[0], id: 'scope-request', requestId: 'scope-check', taskId: 'task-critique', fromAgentId: 'agent-synthesis', toAgentId: 'agent-critique', status: 'fulfilled' as const, summary: 'Check only the interval interpretation.', createdAt: '2026-10-08T14:06:00Z' };
    const reply = { ...request, id: 'scope-reply', fromAgentId: 'agent-critique', toAgentId: 'agent-synthesis', status: 'delivered' as const, summary: saved.attempts.find(attempt => attempt.id === 'attempt-critique')!.resultSummary!, createdAt: '2026-10-08T14:09:10Z' };
    saved.messages = [request, reply];
    const openAttempt = vi.fn();
    const { rerender } = render(<ReportOutput report={report} sources={saved.sources} snapshot={saved} onOpenSource={() => {}} onOpenAttempt={openAttempt} />);
    const attribution = screen.getByLabelText('Report producer record');
    expect(within(attribution).getByText('Agent Synthesis peer')).toBeInTheDocument();
    await user.click(within(attribution).getByRole('button', { name: 'Open producer attempt →' }));
    expect(openAttempt).toHaveBeenCalledWith('attempt-synthesis');
    await user.click(screen.getByText('Report details'));
    expect(screen.getByText('attempt-synthesis', { selector: 'dd' })).toBeInTheDocument();
    const peers = screen.getByText('Related peer exchanges (1)').closest('details')!;
    expect(peers).not.toHaveAttribute('open');
    await user.click(within(peers).getByText('Related peer exchanges (1)'));
    expect(within(peers).getByText('Check only the interval interpretation.')).toBeInTheDocument();
    expect(within(peers).getByText(reply.summary)).toBeInTheDocument();
    expect(within(peers).getByText(/does not establish a review of the whole answer/)).toBeInTheDocument();
    await user.click(within(peers).getByRole('button', { name: 'Open peer attempt →' }));
    expect(openAttempt).toHaveBeenCalledWith('attempt-critique');
    const legacy = { ...report, producerAttemptId: undefined, body: saved.attempts.find(attempt => attempt.id === 'attempt-synthesis')!.resultSummary! };
    rerender(<ReportOutput report={legacy} sources={saved.sources} snapshot={saved} onOpenSource={() => {}} onOpenAttempt={openAttempt} />);
    expect(screen.getByText('Matching saved contribution')).toBeVisible();
    expect(screen.getByText(/Linked to the task of the matching saved contribution/)).toBeVisible();
    expect(screen.getByRole('button', { name: 'Open peer attempt →' })).toBeVisible();
    rerender(<ReportOutput report={report} sources={saved.sources} snapshot={{ ...saved, messages: [{ ...request, status: 'requested' }, reply, { ...reply, id: 'wrong-fulfillment', status: 'fulfilled' }] }} onOpenSource={() => {}} />);
    expect(screen.queryByText('Related peer exchanges (1)')).not.toBeInTheDocument();
    rerender(<ReportOutput report={report} sources={saved.sources} snapshot={{ ...saved, messages: [request, { ...reply, createdAt: '2026-10-08T14:12:01Z' }] }} onOpenSource={() => {}} onOpenAttempt={openAttempt} />);
    expect(screen.queryByText('Related peer exchanges (1)')).not.toBeInTheDocument();
    rerender(<ReportOutput report={report} sources={saved.sources} snapshot={{ ...saved, messages: [request, { ...reply, summary: 'Different text from the saved attempt.' }] }} onOpenSource={() => {}} onOpenAttempt={openAttempt} />);
    expect(screen.queryByText('Related peer exchanges (1)')).not.toBeInTheDocument();
  });

  it('qualifies unique legacy text matching and rejects ambiguous author matches', () => {
    const saved = structuredClone(fixture);
    const attempt = saved.attempts.find(item => item.id === 'attempt-synthesis')!;
    const report = { ...savedReport, body: attempt.resultSummary! };
    const { rerender } = render(<ReportOutput report={report} sources={saved.sources} snapshot={saved} onOpenSource={() => {}} />);
    expect(screen.getByText('Matching saved contribution')).toBeInTheDocument();
    expect(screen.getByText('Agent Synthesis peer')).toBeInTheDocument();
    const details = screen.getByText('Report details').closest('details')!;
    details.open = true;
    expect(within(details).getByText(/legacy match is not an authorship pointer/)).toBeInTheDocument();
    rerender(<ReportOutput report={report} sources={saved.sources} snapshot={{ ...saved, attempts: [...saved.attempts, { ...attempt, id: 'same-text-attempt' }] }} onOpenSource={() => {}} />);
    expect(screen.getByText(/multiple completed attempts have text identical/)).toBeInTheDocument();
    expect(screen.queryByText('Matching saved contribution')).not.toBeInTheDocument();
  });
});
