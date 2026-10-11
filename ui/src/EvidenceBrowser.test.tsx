import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { EvidenceBrowser } from './EvidenceBrowser';
import type { ResearchSnapshot } from './generated/contracts';
import sample from '../public/sample-run.json';

afterEach(cleanup);

const snapshot = sample as ResearchSnapshot;

it('leads with saved titles and retained passages while keeping exact copy records expandable', async () => {
  const user = userEvent.setup();
  const open = vi.fn();
  render(<EvidenceBrowser snapshot={snapshot} selectedSourceId="A1" selectedSpanId="A1-result" onOpenSource={open} mode="sample" />);
  const picker = screen.getByRole('region', { name: 'Saved source copies' });
  const source = screen.getByRole('region', { name: 'Selected saved source' });
  expect(within(picker).getByRole('button', { name: /Adult study A.*saved version 3/ })).toHaveAttribute('aria-pressed', 'true');
  expect(within(source).getByRole('heading', { name: snapshot.sources[0].title })).toHaveAttribute('id', 'selected-source-heading');
  expect(within(source).getByText('At six weeks, response occurred in 18 of 40 versus 12 of 40 participants; the risk-difference interval was -4 to 34 percentage points').closest('article')).toHaveClass('highlighted');
  expect(within(source).getByText('Exact cited excerpt')).toBeVisible();
  expect(within(source).getAllByText('Abstract, sentence 3')[0]).toBeVisible();
  const exactReference = within(source).getByText('Source A1 · copy A1-copy-v3 · v3 · passage A1-result');
  expect(exactReference).not.toBeVisible();
  await user.click(within(source).getByText('Exact passage reference'));
  expect(exactReference).toBeVisible();
  await user.click(within(picker).getByRole('button', { name: /Adult study B.*saved version 3/ }));
  expect(open).toHaveBeenCalledWith('A2');
});

it('filters by saved title without losing an already selected source or substituting an unavailable target', async () => {
  const user = userEvent.setup();
  const open = vi.fn();
  const view = render(<EvidenceBrowser snapshot={snapshot} selectedSourceId="A1" selectedSpanId={null} onOpenSource={open} mode="recorded" />);
  await user.type(screen.getByRole('searchbox', { name: 'Find a saved source' }), 'methods table');
  expect(screen.getByText('Showing 1 of 4 saved copies')).toBeInTheDocument();
  expect(screen.getByText('The selected source remains open below, outside this filter.')).toBeInTheDocument();
  expect(screen.getByRole('heading', { name: snapshot.sources[0].title })).toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: /Access notice.*saved version 2/ }));
  expect(open).toHaveBeenCalledWith('A4');

  view.rerender(<EvidenceBrowser snapshot={snapshot} selectedSourceId="missing-copy" selectedSpanId="missing-span" onOpenSource={open} mode="recorded" />);
  expect(within(screen.getByRole('region', { name: 'Selected saved source' })).getByText('The requested source is not available in this snapshot.')).toBeInTheDocument();
  view.rerender(<EvidenceBrowser snapshot={snapshot} selectedSourceId="A1" selectedSpanId="missing-span" onOpenSource={open} mode="recorded" />);
  expect(screen.getByRole('status')).toHaveTextContent('The requested passage (missing-span) is not available in this saved source copy. No other passage was substituted.');
  expect(screen.queryByRole('article', { name: /missing-span/ })).not.toBeInTheDocument();
});

it('keeps an empty source snapshot explicit', () => {
  render(<EvidenceBrowser snapshot={{ ...snapshot, sources: [] }} selectedSourceId="" selectedSpanId={null} onOpenSource={vi.fn()} mode="live" />);
  expect(screen.getByText('No saved source copy is available in this snapshot.')).toBeInTheDocument();
  expect(screen.queryByRole('button', { name: /copy v/ })).not.toBeInTheDocument();
});

it('shows one exact excerpt, explains overlap, keeps reader choices, and opens an exact citation target', async () => {
  const user = userEvent.setup();
  const saved = structuredClone(snapshot);
  const text = 'Synthetic source text contains the population and a later result.';
  saved.sources[0].passages = [
    { spanId: 'window-first', locator: 'text; chars=0:64; chars=0:20', text: text.slice(0, 20) },
    { spanId: 'window-second', locator: 'text; chars=0:64; chars=0:40', text: text.slice(0, 40) },
    { spanId: 'window-third', locator: `text; chars=0:64; chars=40:${text.length}`, text: text.slice(40) },
  ];
  const props = { snapshot: saved, selectedSourceId: 'A1', selectedSpanId: null, onOpenSource: vi.fn(), mode: 'live' as const };
  const view = render(<EvidenceBrowser {...props} />);
  const passageList = screen.getByRole('navigation', { name: 'Retained passages' });
  expect(within(passageList).getByText('Same beginning as excerpt 1, then 20 more characters.')).toBeVisible();
  expect(screen.getByRole('article', { name: 'Excerpt 1 exact saved text' })).toHaveTextContent(text.slice(0, 20));
  expect(screen.queryByRole('article', { name: 'Excerpt 2 exact saved text' })).not.toBeInTheDocument();
  await user.click(within(passageList).getByRole('button', { name: /^Excerpt 2 / }));
  expect(screen.getByRole('article', { name: 'Excerpt 2 exact saved text' })).toHaveTextContent(text.slice(0, 40));
  expect(screen.getAllByRole('article')).toHaveLength(1);
  view.rerender(<EvidenceBrowser {...props} snapshot={structuredClone(saved)} />);
  expect(screen.getByRole('article', { name: 'Excerpt 2 exact saved text' })).toBeVisible();

  view.rerender(<EvidenceBrowser {...props} selectedSpanId="window-third" />);
  expect(screen.getByRole('article', { name: 'Excerpt 3 exact saved text' })).toHaveClass('highlighted');
  expect(screen.queryByRole('article', { name: 'Excerpt 2 exact saved text' })).not.toBeInTheDocument();

  view.rerender(<EvidenceBrowser {...props} selectedSpanId="absent-citation" />);
  expect(screen.getByRole('status')).toHaveTextContent('No other passage was substituted.');
  expect(screen.queryByRole('article')).not.toBeInTheDocument();
  await user.click(screen.getByRole('button', { name: /^Excerpt 1 / }));
  expect(screen.getByRole('article', { name: 'Excerpt 1 exact saved text' })).toBeVisible();
  expect(screen.getByRole('status')).toHaveTextContent('absent-citation');
  expect(screen.queryByText('Exact cited excerpt')).not.toBeInTheDocument();
});

it('opens a requested passage when it arrives and preserves explicit choice across reordered snapshots', async () => {
  const user = userEvent.setup();
  const saved = structuredClone(snapshot);
  const later = { spanId: 'later', locator: 'section=results', text: 'A later retained result.' };
  const props = { snapshot: saved, selectedSourceId: 'A1', selectedSpanId: 'later', onOpenSource: vi.fn(), mode: 'live' as const };
  const view = render(<EvidenceBrowser {...props} />);
  expect(screen.queryByRole('article')).not.toBeInTheDocument();
  saved.sources[0].passages.push(later);
  view.rerender(<EvidenceBrowser {...props} snapshot={structuredClone(saved)} />);
  expect(screen.getByRole('article')).toHaveTextContent(later.text);
  expect(screen.getByRole('article')).toHaveClass('highlighted');
  await user.click(screen.getByRole('button', { name: /^Excerpt 1 / }));
  const chosenText = saved.sources[0].passages[0].text;
  saved.sources[0].passages.reverse();
  view.rerender(<EvidenceBrowser {...props} snapshot={structuredClone(saved)} />);
  expect(screen.getByRole('article')).toHaveTextContent(chosenText);
  expect(screen.getByRole('article')).not.toHaveClass('highlighted');
});

it('does not combine conflicting, incomparable, or contained ranges and retains Unicode offsets', async () => {
  const user = userEvent.setup();
  const saved = structuredClone(snapshot);
  saved.sources[0].passages = [
    { spanId: 'whole', locator: 'text; chars=0:6', text: 'A🧪CDEF' },
    { spanId: 'duplicate', locator: 'text; chars=0:6', text: 'A🧪CDEF' },
    { spanId: 'subset', locator: 'text; chars=1:4', text: '🧪CD' },
    { spanId: 'conflict', locator: 'text; chars=2:6', text: 'XXXX' },
    { spanId: 'other', locator: 'methods; chars=0:6', text: 'ABCDEF' },
    { spanId: 'malformed', locator: 'text; chars=0:99', text: 'Short text' },
  ];
  render(<EvidenceBrowser snapshot={saved} selectedSourceId="A1" selectedSpanId={null} onOpenSource={vi.fn()} mode="live" />);
  expect(screen.getByText('Repeats the same text and location as excerpt 1.')).toBeVisible();
  expect(screen.getByText('A shorter selection from excerpt 1; no new text.')).toBeVisible();
  expect(screen.getByText('Covers part of excerpt 1, but the wording differs. Check both excerpts.')).toBeVisible();
  expect(screen.getByText('No repeated text found at the same saved location.')).toBeVisible();
  expect(screen.getByText('This excerpt’s location cannot be compared with other excerpts.')).toBeVisible();
  await user.click(screen.getByRole('button', { name: /^Excerpt 4 / }));
  expect(screen.getByRole('article')).toHaveTextContent('XXXX');
  expect(screen.getAllByRole('article')).toHaveLength(1);
  expect(screen.getByRole('article')).not.toHaveTextContent('A🧪CDEF');
});

it('keeps the saved origin explicit and does not borrow provenance from another copy version', () => {
  const saved = structuredClone(snapshot);
  saved.inspection = { snapshotReference: { kind: 'research_snapshot', id: saved.snapshotId, version: 1 }, timingBasis: 'observed_local', initialSourceIds: ['A1'], acceptedOutputReferences: [], attempts: [], records: [], sources: [{ sourceId: 'A1', origin: 'reused', copyReference: { kind: 'source_copy', id: saved.sources[0].copyId, version: saved.sources[0].version }, encounterReferences: [], fromSnapshotId: 'earlier-synthetic-snapshot' }] };
  const props = { snapshot: saved, selectedSourceId: 'A1', selectedSpanId: null, onOpenSource: vi.fn(), mode: 'live' as const };
  const view = render(<EvidenceBrowser {...props} />);
  expect(screen.getByText('Reused from an earlier run')).toBeVisible();
  saved.inspection.sources[0].copyReference.version += 1;
  view.rerender(<EvidenceBrowser {...props} snapshot={saved} />);
  expect(screen.getByText('Source origin not recorded')).toBeVisible();
  expect(screen.queryByText('Reused from an earlier run')).not.toBeInTheDocument();
});

it('does not pick an arbitrary copy or passage when an ID is repeated', () => {
  const duplicateSource = structuredClone(snapshot);
  duplicateSource.sources = [snapshot.sources[0], { ...snapshot.sources[0], copyId: 'other-copy', version: 4 }];
  const view = render(<EvidenceBrowser snapshot={duplicateSource} selectedSourceId="A1" selectedSpanId="A1-result" onOpenSource={vi.fn()} mode="live" />);
  expect(screen.getByText('The requested source is ambiguous in this snapshot. No copy was substituted.')).toBeInTheDocument();
  expect(screen.queryByRole('heading', { name: snapshot.sources[0].title })).not.toBeInTheDocument();
  const duplicatePassage = structuredClone(snapshot);
  duplicatePassage.sources[0].passages.push({ ...duplicatePassage.sources[0].passages[1], text: 'Another retained text with the same span ID.' });
  view.rerender(<EvidenceBrowser snapshot={duplicatePassage} selectedSourceId="A1" selectedSpanId="A1-result" onOpenSource={vi.fn()} mode="live" />);
  expect(screen.getByRole('status')).toHaveTextContent('The requested passage (A1-result) is ambiguous in this saved source copy. No other passage was substituted.');
  expect(document.querySelector('.evidence-browser-passage.highlighted')).toBeNull();
});

it('keeps search leads secondary and links only an exact saved copy', async () => {
  const user = userEvent.setup();
  const open = vi.fn();
  const saved = structuredClone(snapshot);
  saved.inspection = {
    snapshotReference: { kind: 'research_snapshot', id: saved.snapshotId, version: 1 },
    timingBasis: 'observed_local', initialSourceIds: [], acceptedOutputReferences: [], attempts: [],
    sources: [{ sourceId: 'A1', origin: 'retrieved', copyReference: { kind: 'source_copy', id: saved.sources[0].copyId, version: saved.sources[0].version }, encounterReferences: [{ kind: 'source_encounter', id: 'encounter-one', version: 1 }] }],
    records: [
      { reference: { kind: 'source_encounter', id: 'encounter-one', version: 1 }, label: 'Encounter', data: { retrieval_hit: { kind: 'retrieval_hit', id: 'hit-one', version: 1 } } },
      { reference: { kind: 'retrieval_hit', id: 'hit-one', version: 1 }, label: 'Hit', data: { provider: 'synthetic-search', query: 'adult comparison', source: { title: 'Linked lead', original_url: 'https://example.test/lead', access_surface: 'abstract', rights_status: 'permitted', content: 'raw source body must stay hidden' } } },
      { reference: { kind: 'retrieval_hit', id: 'hit-two', version: 1 }, label: 'Other hit', data: { provider: 'synthetic-search', query: 'methods', source: { title: 'Unopened lead', original_url: 'ftp://example.test/no-link', access_surface: 'metadata', rights_status: 'unknown' } } },
    ],
  };
  render(<EvidenceBrowser snapshot={saved} selectedSourceId="A1" selectedSpanId={null} onOpenSource={open} mode="live" />);
  const disclosure = screen.getByText('Search discoveries (2 saved hits) · optional leads').closest('details')!;
  expect(disclosure).not.toHaveAttribute('open');
  await user.click(within(disclosure).getByText('Search discoveries (2 saved hits) · optional leads'));
  const discoveries = within(disclosure).getByRole('region', { name: 'Search discoveries' });
  expect(within(discoveries).getByText('Linked lead')).toBeVisible();
  expect(within(discoveries).getByRole('link', { name: 'https://example.test/lead' })).toHaveAttribute('href', 'https://example.test/lead');
  expect(within(discoveries).getByText('ftp://example.test/no-link')).toBeVisible();
  expect(within(discoveries).queryByRole('link', { name: 'ftp://example.test/no-link' })).not.toBeInTheDocument();
  expect(within(discoveries).queryByText('raw source body must stay hidden')).not.toBeInTheDocument();
  expect(within(discoveries).getAllByRole('button', { name: 'Open exact saved source copy →' })).toHaveLength(1);
  await user.click(within(discoveries).getByRole('button', { name: 'Open exact saved source copy →' }));
  expect(open).toHaveBeenCalledWith('A1');
});


it('does not infer that full source text was never saved when no excerpts are included', () => {
  const saved = structuredClone(snapshot);
  saved.sources[0].accessState = 'full_text';
  saved.sources[0].passages = [];
  render(<EvidenceBrowser snapshot={saved} selectedSourceId="A1" selectedSpanId={null} onOpenSource={vi.fn()} mode="live" />);
  expect(screen.getByText('No excerpt text is included in this saved view. Open the source details to check what material was saved.')).toBeVisible();
  expect(screen.queryByText(/No article text was saved/)).not.toBeInTheDocument();
});
