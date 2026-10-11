/* Inspect immutable records supplied by the backend without reconstructing state.
   Exact references resolve only within this snapshot; absent records remain
   explicit. Findings and accepted outputs are separate from these disclosures. */
import type { ResearchSnapshot, SavedRecordRef } from './generated/contracts';
import { useState } from 'react';

type Snapshot = { snapshot: ResearchSnapshot };
export const referenceKey = (ref: SavedRecordRef) => `${ref.kind}:${ref.id}:${ref.version}`;
export const referenceLabel = (ref: SavedRecordRef) => `${ref.kind} · ${ref.id} · v${ref.version}`;

function discoveryQuery(data?: Record<string, unknown>) {
  if (typeof data?.query === 'string' && data.query.trim()) return data.query;
  if ((data?.query === '' || data?.query === null) && ['pubmed.pmid', 'pubmed.abstract', 'crossref.doi'].includes(String(data.route))) {
    return `Exact identifier lookup (${data.route}); no text query supplied`;
  }
  return 'Not recorded';
}

function fieldLabel(key: string) {
  const words = key.replace(/([a-z])([A-Z])/g, '$1 $2').replaceAll('_', ' ');
  return words.charAt(0).toUpperCase() + words.slice(1);
}

function asReference(value: unknown): SavedRecordRef | undefined {
  if (!value || typeof value !== 'object' || Object.keys(value).length !== 3) return undefined;
  const ref = value as Partial<SavedRecordRef>;
  return typeof ref.kind === 'string' && typeof ref.id === 'string' && Number.isInteger(ref.version) && Number(ref.version) > 0 ? ref as SavedRecordRef : undefined;
}

function SavedValue({ value, snapshot, trail }: Snapshot & { value: unknown; trail: string[] }) {
  if (value === null || value === undefined) return <span className="inspection-missing">Not recorded</span>;
  if (typeof value === 'boolean') return <span>{value ? 'Yes' : 'No'}</span>;
  if (Array.isArray(value)) return value.length ? <ol className="inspection-values">{value.map((item, index) => <li key={index}><SavedValue snapshot={snapshot} trail={trail} value={item} /></li>)}</ol> : <span>None recorded</span>;
  const pointer = asReference(value);
  if (pointer) return trail.includes(referenceKey(pointer)) ? <span>Already shown above: {referenceLabel(pointer)}</span> : <SavedRecord snapshot={snapshot} reference={pointer} trail={trail} />;
  if (typeof value === 'object' && Object.keys(value).length === 0) return <span>No fields recorded</span>;
  if (typeof value === 'object') return <dl className="inspection-fields">{Object.entries(value).map(([key, item]) => <div key={key}><dt>{fieldLabel(key)}</dt><dd>{item !== null && typeof item === 'object' && !asReference(item) ? <details><summary>View {fieldLabel(key).toLowerCase()}</summary><SavedValue snapshot={snapshot} trail={trail} value={item} /></details> : <SavedValue snapshot={snapshot} trail={trail} value={item} />}</dd></div>)}</dl>;
  return <span className="inspection-text">{String(value) || 'Not recorded'}</span>;
}

export function SavedRecord({ snapshot, reference, trail = [] }: Snapshot & { reference: SavedRecordRef; trail?: string[] }) {
  const [expanded, setExpanded] = useState(false);
  const record = snapshot.inspection?.records.find(item => referenceKey(item.reference) === referenceKey(reference));
  const defaultLabel = `${reference.kind.replaceAll('_', ' ')} · ${reference.id} · v${reference.version}`;
  const label = record?.label === defaultLabel ? `${fieldLabel(reference.kind)}${typeof record.data.display_name === 'string' ? ` · ${record.data.display_name}` : typeof record.data.model_id === 'string' ? ` · ${record.data.model_id}` : typeof record.data.title === 'string' ? ` · ${record.data.title}` : ''}` : record?.label || 'Saved record unavailable';
  return <details className="saved-record" onToggle={event => { if (event.target === event.currentTarget) setExpanded(event.currentTarget.open); }}><summary>{label}</summary>{expanded && <><p className="tiny-note">{referenceLabel(reference)}</p>{record ? <SavedValue snapshot={snapshot} trail={[...trail, referenceKey(reference)]} value={record.data} /> : <p>This exact record version was not included in the saved inspection.</p>}</>}</details>;
}

export function RecordGroup({ snapshot, title, references }: Snapshot & { title: string; references: SavedRecordRef[] }) {
  const unique = [...new Map(references.map(ref => [referenceKey(ref), ref])).values()];
  return <section className="inspection-group" aria-label={title}><h4>{title}</h4>{unique.length ? unique.map(ref => <SavedRecord key={referenceKey(ref)} snapshot={snapshot} reference={ref} />) : <p>No {title.toLowerCase()} recorded.</p>}</section>;
}

export function AttemptInspection({ snapshot, attemptId, summaryLabel = 'Saved agent, configuration and recovery details', configurationOnly = false }: Snapshot & { attemptId: string; summaryLabel?: string; configurationOnly?: boolean }) {
  const detail = snapshot.inspection?.attempts.find(item => item.attemptId === attemptId);
  if (!detail) return <details className="inline-detail inspection-detail"><summary>{summaryLabel}</summary><p className="inspection-missing">Saved identity and configuration inspection is unavailable for this attempt.</p></details>;
  return <details className="inline-detail inspection-detail"><summary>{summaryLabel}</summary><p>{detail.explanation}</p>
    <RecordGroup snapshot={snapshot} title="Agent instance" references={[detail.instanceReference]} />
    <RecordGroup snapshot={snapshot} title="Configuration used" references={detail.configurationReference ? [detail.configurationReference] : []} />
    {!configurationOnly && <><RecordGroup snapshot={snapshot} title="Recorded calls" references={detail.callReferences ?? []} />
    <RecordGroup snapshot={snapshot} title="Saved inputs" references={detail.inputReferences ?? []} />
    <RecordGroup snapshot={snapshot} title="Accepted outputs" references={detail.acceptedOutputReferences ?? []} /></>}
  </details>;
}

export function SourceInspection({ snapshot, sourceId }: Snapshot & { sourceId: string }) {
  const detail = snapshot.inspection?.sources.find(item => item.sourceId === sourceId);
  if (!detail) return <p className="inspection-missing">Saved source origin and retrieval receipts are not included in this snapshot.</p>;
  return <div className="source-inspection"><p><strong>{detail.origin === 'reused' ? 'Reused saved evidence' : detail.origin === 'retrieved' ? 'Retrieved during this task' : 'Supplied evidence'}</strong>{detail.fromSnapshotId && <> · from snapshot {detail.fromSnapshotId}</>}</p>
    <details className="inline-detail"><summary>Source copy and discovery receipts</summary>
      {detail.encounterReferences.map(ref => {
        const encounter = snapshot.inspection?.records.find(item => referenceKey(item.reference) === referenceKey(ref));
        const hitRef = asReference(encounter?.data.retrieval_hit);
        const hit = hitRef && snapshot.inspection?.records.find(item => referenceKey(item.reference) === referenceKey(hitRef));
        return hitRef ? <div className="discovery-summary" key={referenceKey(ref)}><dl className="inspection-fields"><div><dt>Search or lookup</dt><dd>{discoveryQuery(hit?.data)}</dd></div><div><dt>Provider</dt><dd>{typeof hit?.data.provider === 'string' ? hit.data.provider : 'Not recorded'}</dd></div></dl><SavedRecord snapshot={snapshot} reference={hitRef} /></div> : null;
      })}
      <SavedRecord snapshot={snapshot} reference={detail.copyReference} />
      <RecordGroup snapshot={snapshot} title="Discovery encounters" references={detail.encounterReferences} />
    </details>
  </div>;
}

export function SearchDiscoveries({ snapshot, onOpenSource }: Snapshot & { onOpenSource: (sourceId: string) => void }) {
  const inspection = snapshot.inspection;
  const hits = inspection?.records.filter(record => record.reference.kind === 'retrieval_hit') ?? [];
  if (!inspection || hits.length === 0) return null;
  const recordsByKey = new Map(inspection.records.map(record => [referenceKey(record.reference), record]));
  const copiesByHit = new Map<string, Set<string>>();
  for (const source of inspection.sources) {
    const copies = snapshot.sources.filter(copy => copy.id === source.sourceId);
    if (source.copyReference.kind !== 'source_copy' || copies.length !== 1
      || copies[0].copyId !== source.copyReference.id || copies[0].version !== source.copyReference.version) continue;
    for (const encounterRef of source.encounterReferences) {
      const hitRef = asReference(recordsByKey.get(referenceKey(encounterRef))?.data.retrieval_hit);
      if (hitRef) {
        const key = referenceKey(hitRef);
        copiesByHit.set(key, (copiesByHit.get(key) ?? new Set<string>()).add(source.sourceId));
      }
    }
  }
  const savedText = (value: unknown) => typeof value === 'string' && value.trim() ? value : 'Not recorded';
  const safeLink = (value: unknown) => {
    if (typeof value !== 'string' || !/^https?:\/\//i.test(value)) return null;
    try {
      const url = new URL(value);
      return ['http:', 'https:'].includes(url.protocol) && url.hostname && !url.username && !url.password ? value : null;
    } catch { return null; }
  };
  return <section className="panel discovery-results" aria-label="Search discoveries">
    <h3>Search discoveries ({hits.length} saved hits)</h3>
    <p>These are search leads, including hits that were never retained as source copies. They are not a count of independently checked studies or evidence for a claim.</p>
    <div className="discovery-list">{hits.map(hit => {
      const source = hit.data.source && typeof hit.data.source === 'object' && !Array.isArray(hit.data.source) ? hit.data.source as Record<string, unknown> : {};
      const linkedCopies = copiesByHit.get(referenceKey(hit.reference));
      const copyId = linkedCopies?.size === 1 ? [...linkedCopies][0] : undefined;
      const url = safeLink(source.original_url);
      return <article key={referenceKey(hit.reference)} className="discovery-hit">
        <h4>{savedText(source.title)}</h4>
        <p><strong>URL:</strong> {url ? <a href={url} target="_blank" rel="noopener noreferrer">{url}</a> : savedText(source.original_url)}</p>
        <p><strong>Provider:</strong> {savedText(hit.data.provider)} · <strong>Search or lookup:</strong> {discoveryQuery(hit.data)}</p>
        <p><strong>Saved hit status:</strong> {savedText(source.access_surface)} access · {savedText(source.rights_status)} rights · {copyId ? 'exact source copy linked in this snapshot' : 'no source copy linked in this snapshot'}</p>
        <p className="tiny-note">Hit {hit.reference.id} · v{hit.reference.version}</p>
        {copyId && <button className="inline-link" type="button" onClick={() => onOpenSource(copyId)}>Open exact saved source copy →</button>}
      </article>;
    })}</div>
  </section>;
}

export function CitationDetails({ snapshot, sourceId, spanId }: Snapshot & { sourceId: string; spanId: string }) {
  const sources = snapshot.sources.filter(item => item.id === sourceId);
  const source = sources.length === 1 ? sources[0] : undefined;
  const passage = source?.passages.find(item => item.spanId === spanId);
  return <details className="inline-detail citation-detail"><summary>{sourceId} · {spanId} · expand cited passage</summary><p className="tiny-note">Snapshot {snapshot.snapshotId}{source && ` · copy ${source.copyId} · v${source.version}`}{spanId.startsWith('window-') ? ' · checked source window excerpt' : ''}</p>
    {passage ? <><p>{source?.title} · {passage.locator}</p><blockquote>{passage.text}</blockquote></> : <p>The exact cited passage is unavailable or ambiguous in this snapshot.</p>}
    <SourceInspection snapshot={snapshot} sourceId={sourceId} />
  </details>;
}

export function acceptedReport(snapshot: ResearchSnapshot) {
  if (snapshot.proofKind === 'offline_test' && !snapshot.inspection) return undefined;
  return snapshot.artifacts.find(artifact => artifact.id === snapshot.mission.reportArtifactId && (!snapshot.inspection || snapshot.inspection.acceptedOutputReferences.some(ref => ref.id === artifact.id && ref.version === artifact.version && ref.kind === 'report')));
}

export function SnapshotDetails({ snapshot, parentSnapshot }: Snapshot & { parentSnapshot?: ResearchSnapshot }) {
  const inspection = snapshot.inspection;
  if (!inspection) return <details className="inline-detail inspection-detail"><summary>Saved inspection details</summary><p className="inspection-missing">Versioned inspection records are not included in this snapshot.</p></details>;
  return <details className="inline-detail inspection-detail"><summary>Saved snapshot and follow-up details</summary><p>{inspection.timingBasis === 'controlled_clock' ? 'Times use a controlled test clock; they do not measure live agent or provider latency.' : 'Times were recorded locally; elapsed time may include waiting.'}</p><p>Parent snapshot: {snapshot.parentSnapshotId || 'None recorded'}</p>
    {inspection.followupTarget && <RecordGroup snapshot={parentSnapshot && parentSnapshot.snapshotId === snapshot.parentSnapshotId ? parentSnapshot : snapshot} title="Exact follow-up target" references={[inspection.followupTarget]} />}
    <p>Saved snapshot: {referenceLabel(inspection.snapshotReference)}</p>
    <RecordGroup snapshot={snapshot} title="Accepted outputs" references={inspection.acceptedOutputReferences} />
  </details>;
}
