/* Browse only source copies and passages saved in the selected snapshot.
   Search hits remain discovery leads, separate from inspected evidence. */
import { useState } from 'react';
import type { PassageView, ResearchSnapshot, SourceView } from './generated/contracts';
import { SearchDiscoveries, SourceInspection } from './InspectionDetails';
import { TermHelp } from './TermHelp';
import './EvidenceBrowser.css';

type Props = {
  snapshot: ResearchSnapshot;
  selectedSourceId: string;
  selectedSpanId: string | null;
  onOpenSource: (sourceId: string, spanId?: string) => void;
  mode: 'live' | 'recorded' | 'sample';
};

const accessLabel = (value: SourceView['accessState']) => ({ abstract_only: 'Abstract only', full_text: 'Full-text source', locator_only: 'Reference or access notice', supplied_text: 'Supplied text', navigation_text: 'Web page text', document_text: 'Public web document' })[value];

// Compare only ranges whose saved locator and text lengths agree. A repeated
// range is not another source, and conflicting saved text is never merged.
function locatedPassage(passage: PassageView) {
  const match = /^(.*?)(?:;\s*)?chars=(\d+):(\d+)\s*$/.exec(passage.locator);
  if (!match) return null;
  const start = Number(match[2]), end = Number(match[3]);
  const characters = Array.from(passage.text);
  if (!Number.isSafeInteger(start) || !Number.isSafeInteger(end) || end <= start || end - start !== characters.length) return null;
  return { base: match[1].trim(), start, end, characters };
}

function describePassages(passages: PassageView[]) {
  const locations = passages.map(locatedPassage);
  return passages.map((passage, index) => {
    const location = locations[index];
    const range = location ? `Characters ${(location.start + 1).toLocaleString()}–${location.end.toLocaleString()}` : passage.locator;
    if (!location) return { range, relationship: 'This excerpt’s location cannot be compared with other excerpts.', conflict: false };
    const overlaps = locations.slice(0, index).flatMap((other, previous) => {
      if (!other || other.base !== location.base) return [];
      const start = Math.max(other.start, location.start), end = Math.min(other.end, location.end);
      if (end <= start) return [];
      const matches = location.characters.slice(start - location.start, end - location.start).join('') === other.characters.slice(start - other.start, end - other.start).join('');
      return [{ other, previous, length: end - start, matches }];
    }).sort((a, b) => Number(a.matches) - Number(b.matches) || b.length - a.length);
    const overlap = overlaps[0];
    if (!overlap) return { range, relationship: index === 0 ? 'First saved excerpt.' : 'No repeated text found at the same saved location.', conflict: false };
    const name = `excerpt ${overlap.previous + 1}`;
    if (!overlap.matches) return { range, relationship: `Covers part of ${name}, but the wording differs. Check both excerpts.`, conflict: true };
    const extra = location.end - location.start - overlap.length;
    return { range, relationship: extra > 0
      ? location.start === overlap.other.start && location.end > overlap.other.end
        ? `Same beginning as ${name}, then ${extra.toLocaleString()} more characters.`
        : `${overlap.length.toLocaleString()} characters repeated from ${name}; ${extra.toLocaleString()} more beyond that selection.`
      : location.start === overlap.other.start && location.end === overlap.other.end
        ? `Repeats the same text and location as ${name}.`
        : `A shorter selection from ${name}; no new text.`, conflict: false };
  });
}

function RetainedPassages({ source, selectedSpanId }: { source: SourceView; selectedSpanId: string | null }) {
  const exactMatches = source.passages.filter(passage => passage.spanId === selectedSpanId);
  const requested = exactMatches.length === 1 ? exactMatches[0] : undefined;
  const signature = (passage: PassageView) => JSON.stringify([passage.spanId, passage.locator, passage.text]);
  const [readerChoice, setChoice] = useState<{ index: number; signature: string } | null>(null);
  const defaultIndex = selectedSpanId ? (requested ? source.passages.indexOf(requested) : -1) : 0;
  const relocated = readerChoice ? source.passages.map((passage, index) => ({ index, signature: signature(passage) })).filter(item => item.signature === readerChoice.signature) : [];
  const index = readerChoice
    ? source.passages[readerChoice.index] && signature(source.passages[readerChoice.index]) === readerChoice.signature ? readerChoice.index : relocated.length === 1 ? relocated[0].index : -1
    : defaultIndex;
  const passage = source.passages[index];
  const descriptions = describePassages(source.passages);
  const detail = descriptions[index];
  return <div className="evidence-browser-reading">
    <nav className="evidence-browser-passages" aria-label="Retained passages">
      <h4>Choose an excerpt <span>({source.passages.length})</span></h4>
      <p>An excerpt is a short selection from this source.</p>
      <ol>{source.passages.map((item, index) => <li key={`${item.spanId}:${index}`}>
        <button type="button" aria-label={`Excerpt ${index + 1} · ${descriptions[index].range} · ${descriptions[index].relationship}${requested === item ? ' · Cited in the answer' : ''}`} aria-pressed={passage === item} className={passage === item ? 'selected' : ''} onClick={() => setChoice({ index, signature: signature(item) })}>
          <strong>Excerpt {index + 1}</strong><span>{descriptions[index].range}</span>
          <small className={descriptions[index].conflict ? 'evidence-range-conflict' : ''}>{descriptions[index].relationship}</small>
          {requested === item && <small className="evidence-citation-marker">Cited in the answer</small>}
        </button>
      </li>)}</ol>
    </nav>
    {passage && detail ? <article className={`evidence-browser-passage evidence-browser-reader ${requested === passage ? 'highlighted' : ''}`} aria-label={`Excerpt ${index + 1} exact saved text`}>
      <div className="evidence-reader-heading"><h4>Excerpt {index + 1} <span>of {source.passages.length}</span></h4>{requested === passage && <span className="evidence-citation-marker">Exact cited excerpt</span>}</div>
      <p className="evidence-reader-range">{detail.range}</p>
      <p className={`evidence-reader-relationship ${detail.conflict ? 'evidence-range-conflict' : ''}`}>{detail.relationship}</p>
      <blockquote>{passage.text}</blockquote>
      <details><summary>Exact passage reference</summary><p>{passage.locator}</p><p>Source {source.id} · copy {source.copyId} · v{source.version} · passage {passage.spanId}</p></details>
    </article> : <div className="evidence-browser-reader-empty">{source.passages.length ? 'Choose an excerpt to read its exact saved text.' : 'No excerpt text is included in this saved view. Open the source details to check what material was saved.'}</div>}
  </div>;
}

export function EvidenceBrowser({ snapshot, selectedSourceId, selectedSpanId, onOpenSource, mode }: Props) {
  const [query, setQuery] = useState('');
  const term = query.trim().toLocaleLowerCase();
  const visible = snapshot.sources.filter(source => !term || [source.title, source.provider, source.id, source.copyId]
    .some(value => value?.toLocaleLowerCase().includes(term)));
  const matchingSources = snapshot.sources.filter(source => source.id === selectedSourceId);
  const selectedSource = matchingSources.length === 1 ? matchingSources[0] : undefined;
  const matchingPassages = selectedSource?.passages.filter(passage => passage.spanId === selectedSpanId) ?? [];
  const selectedPassage = matchingPassages.length === 1 ? matchingPassages[0] : undefined;
  const discoveryCount = snapshot.inspection?.records.filter(record => record.reference.kind === 'retrieval_hit').length ?? 0;
  const sourceRecords = selectedSource && snapshot.inspection?.sources.filter(item => item.sourceId === selectedSource.id);
  const provenance = sourceRecords?.length === 1 && sourceRecords[0].copyReference.kind === 'source_copy' && sourceRecords[0].copyReference.id === selectedSource?.copyId && sourceRecords[0].copyReference.version === selectedSource.version ? sourceRecords[0] : undefined;
  const origin = provenance?.origin;
  const originLabel = origin === 'reused' ? 'Reused from an earlier run' : origin === 'supplied' ? 'Supplied with the request' : origin === 'retrieved' ? 'Retrieved during this run' : mode === 'sample' ? 'Scripted sample source' : 'Source origin not recorded';

  return <div className="evidence-browser">
    <section className="panel evidence-browser-picker" aria-label="Saved source copies">
      <div className="evidence-browser-picker-head"><div><span className="eyebrow">1 · Choose a source</span><h3>Saved sources ({snapshot.sources.length}) <TermHelp term="source copy" /></h3></div><label>Find a saved source<input type="search" value={query} onChange={event => setQuery(event.target.value)} placeholder="Title, provider or source ID" /></label></div>
      <p className="evidence-browser-help">Choose a publication or other source, then read the excerpts saved from it. Search results that have not been opened are separate below.</p>
      {snapshot.sources.length > 0 && <><p className="evidence-browser-count" aria-live="polite">Showing {visible.length} of {snapshot.sources.length} saved copies</p>
        {visible.length ? <ul className="evidence-browser-list">{visible.map(source => <li key={`${source.id}:${source.copyId}:${source.version}`}><button type="button" className={source === selectedSource ? 'selected' : ''} aria-pressed={source === selectedSource} onClick={() => onOpenSource(source.id)}><strong>{source.title}</strong><small>{accessLabel(source.accessState)} · saved version {source.version}</small></button></li>)}</ul> : <p>No saved source copy matches this filter. Clear the search to see all saved copies.</p>}
        {selectedSource && !visible.some(source => source.id === selectedSource.id) && <p className="evidence-browser-filter-note">The selected source remains open below, outside this filter.</p>}
      </>}
    </section>

    <section id="selected-source-panel" tabIndex={-1} className="panel source-detail-panel evidence-browser-source" aria-label="Selected saved source">
      {selectedSource ? <>
        <div className="evidence-browser-source-head"><div><span className="eyebrow">2 · Read saved evidence · copy v{selectedSource.version}</span><h3 id="selected-source-heading" tabIndex={-1}>{selectedSource.title}</h3></div><span className="evidence-browser-access">{accessLabel(selectedSource.accessState)}</span></div>
        <p className="evidence-browser-source-meta"><strong>{originLabel}</strong> · {selectedSource.provider || (mode === 'sample' ? 'Sample packet' : 'Provider not recorded')}</p>
        {selectedSpanId && !selectedPassage && <p className="evidence-browser-missing" role="status">The requested passage ({selectedSpanId}) is {matchingPassages.length > 1 ? 'ambiguous' : 'not available'} in this saved source copy. No other passage was substituted.</p>}
        <p className="evidence-browser-scope">Read one excerpt at a time. Repeated wording can come from overlapping selections of the same source. These excerpts may not include the whole publication.</p>
        <RetainedPassages key={`${selectedSource.id}:${selectedSource.copyId}:${selectedSource.version}:${selectedSpanId ?? ''}`} source={selectedSource} selectedSpanId={selectedSpanId} />
        <details className="evidence-browser-records"><summary>Source origin, copy and discovery records</summary><p>Source ID: {selectedSource.id} · copy ID: {selectedSource.copyId} · version {selectedSource.version}</p>{selectedSource.url && <p>Saved URL: {selectedSource.url}</p>}<p>Saved retrieval status or route: {selectedSource.retrievalStatus}</p><p>Content hash: {selectedSource.contentHash || 'not retained'}</p>{provenance ? <SourceInspection snapshot={snapshot} sourceId={selectedSource.id} /> : <p>Source origin and discovery records for this exact copy are unavailable or ambiguous.</p>}<p className="tiny-note">{mode === 'recorded' ? 'Saved offline evidence record.' : mode === 'live' ? 'Source origin and exact copy are shown in saved provenance when available.' : 'Scripted source record, not a live retrieval.'}</p></details>
      </> : <p>{matchingSources.length > 1 ? 'The requested source is ambiguous in this snapshot. No copy was substituted.' : snapshot.sources.length ? 'The requested source is not available in this snapshot.' : 'No saved source copy is available in this snapshot.'}</p>}
    </section>

    {discoveryCount > 0 && <details className="panel evidence-browser-discoveries"><summary>Search discoveries ({discoveryCount} saved hits) · optional leads</summary><p>These saved hits may include items never opened as source copies. They are not evidence for a claim until their exact text is checked.</p><SearchDiscoveries snapshot={snapshot} onOpenSource={sourceId => onOpenSource(sourceId)} /></details>}
  </div>;
}
