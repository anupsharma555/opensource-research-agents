import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { PeerRecordReader } from './PeerRecordReader';
import type { LivePeerRecord, PeerRecordReference } from './live';
import type { ResearchSnapshot } from './generated/contracts';
import sample from '../public/sample-run.json';

const snapshot = sample as ResearchSnapshot;
const message: PeerRecordReference = { kind: 'mail_message', id: 'message-one', version: 3 };
const available = (content: unknown, reference = message, runId = 'run-one'): LivePeerRecord => ({
  runId, originRunId: runId, reference, senderId: snapshot.agents[0].id, recipientId: snapshot.agents[1].id,
  requestId: 'request-one', createdAt: '2026-10-09T12:00:00Z', status: 'available', content,
  contentReference: { kind: 'mail_message', id: reference.id, version: reference.version }, redacted: false,
});
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });

afterEach(() => { cleanup(); vi.unstubAllGlobals(); window.history.replaceState(null, '', '/'); });

it('reads an exact message only on click, without cache, and preserves multiline content and direction', async () => {
  const fetch = vi.fn().mockResolvedValue(json({ ...available('First exact line.\nSecond exact line.'), deliveredAt: '2026-10-09T12:01:00Z', acknowledgedAt: '2026-10-09T12:02:00Z', redacted: true }));
  vi.stubGlobal('fetch', fetch);
  render(<PeerRecordReader runId="run-one" reference={message} snapshot={snapshot} />);
  expect(fetch).not.toHaveBeenCalled();
  await userEvent.setup().click(screen.getByRole('button', { name: 'Read exact saved message →' }));
  expect(fetch).toHaveBeenCalledWith('/api/live/peer-record?runId=run-one&kind=mail_message&id=message-one&version=3', expect.objectContaining({ credentials: 'same-origin', cache: 'no-store' }));
  const content = await screen.findByText(/First exact line\. Second exact line\./);
  expect(content).toHaveClass('peer-record-text');
  expect(content.textContent).toBe('First exact line.\nSecond exact line.');
  expect(screen.getByText(/Synthesis peer → Acquisition peer/)).toBeVisible();
  expect(screen.getByText(/Delivered/)).toHaveTextContent('Acknowledged');
  expect(screen.getByText('Secret values were redacted from this saved content.')).toBeVisible();
});

it('distinguishes saved scalar and null content from missing content', async () => {
  const fetch = vi.fn().mockResolvedValueOnce(json(available(0)))
    .mockResolvedValueOnce(json(available(null, message, 'run-two')));
  vi.stubGlobal('fetch', fetch);
  const view = render(<PeerRecordReader runId="run-one" reference={message} snapshot={snapshot} />);
  const user = userEvent.setup();
  await user.click(screen.getByRole('button', { name: 'Read exact saved message →' }));
  expect(await screen.findByText('0')).toBeVisible();
  view.rerender(<PeerRecordReader runId="run-two" reference={message} snapshot={snapshot} />);
  await user.click(screen.getByRole('button', { name: 'Read exact saved message →' }));
  expect(await screen.findByText('The record explicitly contains no value (null).')).toBeVisible();
  expect(screen.queryByText('0')).not.toBeInTheDocument();
});

it('clears previously read content when a newer snapshot of the same run is selected', async () => {
  const fetch = vi.fn().mockResolvedValueOnce(json(available('Earlier snapshot text')))
    .mockResolvedValueOnce(json(available('New snapshot text')));
  vi.stubGlobal('fetch', fetch);
  const user = userEvent.setup();
  const view = render(<PeerRecordReader runId="run-one" reference={message} snapshot={snapshot} />);
  await user.click(screen.getByRole('button', { name: 'Read exact saved message →' }));
  expect(await screen.findByText('Earlier snapshot text')).toBeVisible();
  view.rerender(<PeerRecordReader runId="run-one" reference={message} snapshot={{ ...snapshot, snapshotId: 'next-snapshot' }} />);
  expect(screen.queryByText('Earlier snapshot text')).not.toBeInTheDocument();
  expect(fetch).toHaveBeenCalledTimes(1);
  await user.click(screen.getByRole('button', { name: 'Read exact saved message →' }));
  expect(await screen.findByText('New snapshot text')).toBeVisible();
});

it('shows a saved answer as a report with citations and limitations, with raw structure optional', async () => {
  const answer: PeerRecordReference = { kind: 'mail_answer', id: 'answer-one', version: 2 };
  const content = { title: 'Bounded reply', body: 'First paragraph.\n\nSecond paragraph.', citations: [{ sourceId: 'A1', spanId: 'A1-result' }], limitations: ['Full text unavailable.'] };
  vi.stubGlobal('fetch', vi.fn().mockResolvedValue(json({ ...available(content, answer), partial: true })));
  render(<PeerRecordReader runId="run-one" reference={answer} snapshot={snapshot} />);
  await userEvent.setup().click(screen.getByRole('button', { name: 'Read exact saved answer →' }));
  expect(await screen.findByRole('heading', { name: 'Bounded reply' })).toBeVisible();
  expect(screen.getByText('First paragraph.')).toBeVisible();
  expect(screen.getByText('Second paragraph.')).toBeVisible();
  expect(screen.getByText('Saved citations (1)')).toBeVisible();
  expect(screen.getByText('Full text unavailable.')).toBeVisible();
  expect(screen.getByText(/partial answer/)).toBeVisible();
  expect(screen.getByText('Exact structured answer').closest('details')).not.toHaveAttribute('open');
});

it('withholds a mismatched response and ignores a late response from another selected run', async () => {
  let resolveOld!: (value: Response) => void;
  const fetch = vi.fn().mockImplementationOnce(() => new Promise<Response>(resolve => { resolveOld = resolve; }))
    .mockResolvedValueOnce(json(available('Current run content', { ...message, version: 4 }, 'run-two')));
  vi.stubGlobal('fetch', fetch);
  const user = userEvent.setup();
  const view = render(<PeerRecordReader runId="run-one" reference={message} snapshot={snapshot} />);
  await user.click(screen.getByRole('button', { name: 'Read exact saved message →' }));
  view.rerender(<PeerRecordReader runId="run-two" reference={{ ...message, version: 4 }} snapshot={snapshot} />);
  resolveOld(json(available('Old run content')));
  expect(screen.queryByText('Old run content')).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Read exact saved message →' }));
  expect(await screen.findByText('Current run content')).toBeVisible();
  fetch.mockResolvedValueOnce(json(available('Wrong reference content', message, 'run-two')));
  await user.click(screen.getByRole('button', { name: 'Read exact saved message →' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('did not match the selected run and exact record');
  expect(screen.queryByText('Wrong reference content')).not.toBeInTheDocument();
});

it('labels unavailable and authorization failures, then permits retry without exposing error text', async () => {
  const fetch = vi.fn()
    .mockResolvedValueOnce(json({ ...available(null), status: 'unavailable', reason: 'content_unavailable' }))
    .mockResolvedValueOnce(json({ error: 'private host detail' }, 401))
    .mockResolvedValueOnce(json(available('Saved after retry')));
  vi.stubGlobal('fetch', fetch);
  const user = userEvent.setup();
  render(<PeerRecordReader runId="run-one" reference={message} snapshot={snapshot} />);
  const button = screen.getByRole('button', { name: 'Read exact saved message →' });
  await user.click(button);
  expect(await screen.findByText('The exact record is saved, but its content is unavailable.')).toBeVisible();
  await user.click(button);
  expect(await screen.findByRole('alert')).toHaveTextContent('Access to this saved content was denied');
  expect(screen.queryByText('private host detail')).not.toBeInTheDocument();
  await user.click(button);
  expect(await screen.findByText('Saved after retry')).toBeVisible();
});

it('expands the exact message in the request location without presenting the saved question as the delivered payload', async () => {
  const user = userEvent.setup();
  const fetch = vi.fn().mockResolvedValue(json(available({ question: 'Different exact message.\nSecond line.', context: 'Additional saved message context.' })));
  vi.stubGlobal('fetch', fetch);
  const view = render(<PeerRecordReader runId="run-one" reference={message} snapshot={snapshot} requestQuestion="Saved request wording." />);
  expect(screen.getByText('Saved request wording.')).toBeVisible();
  expect(screen.getByText('Saved request')).toBeVisible();
  expect(fetch).not.toHaveBeenCalled();
  await user.click(screen.getByRole('button', { name: 'Expand exact message' }));
  const quote = await screen.findByText(/Different exact message\. Second line\./);
  expect(quote.tagName).toBe('BLOCKQUOTE');
  expect(quote.textContent).toBe('Different exact message.\nSecond line.');
  expect(screen.getByText('Additional saved message context.')).toBeVisible();
  expect(screen.queryByText('Saved request wording.')).not.toBeInTheDocument();
  expect(screen.getByText('Exact saved message')).toBeVisible();
  await user.click(screen.getByRole('button', { name: 'Collapse exact message', expanded: true }));
  expect(screen.getByText('Saved request wording.')).toBeVisible();
  expect(screen.queryByText('Exact saved message')).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Expand exact message', expanded: false }));
  expect(fetch).toHaveBeenCalledTimes(1);
  view.rerender(<PeerRecordReader runId="run-one" reference={message} snapshot={{ ...snapshot, snapshotId: 'next-snapshot' }} requestQuestion="Next saved request." />);
  expect(screen.getByText('Next saved request.')).toBeVisible();
  expect(screen.queryByText(/Different exact message/)).not.toBeInTheDocument();
  expect(screen.getByRole('button', { name: 'Expand exact message', expanded: false })).toBeVisible();
});

it('retains the saved request when exact content is denied and allows an inline retry', async () => {
  const fetch = vi.fn().mockResolvedValueOnce(json({ error: 'private diagnostic' }, 403))
    .mockResolvedValueOnce(json(available('Verified saved message.')));
  vi.stubGlobal('fetch', fetch);
  const user = userEvent.setup();
  render(<PeerRecordReader runId="run-one" reference={message} snapshot={snapshot} requestQuestion="Original question." />);
  await user.click(screen.getByRole('button', { name: 'Expand exact message' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('Access to this saved content was denied');
  expect(screen.getByText('Original question.')).toBeVisible();
  expect(screen.queryByText('private diagnostic')).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: 'Retry exact message' }));
  expect(await screen.findByText('Verified saved message.')).toBeVisible();
  expect(screen.queryByText('Original question.')).not.toBeInTheDocument();
});
