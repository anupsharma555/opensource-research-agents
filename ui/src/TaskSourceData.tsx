/* Distinguish sources available at the start from later discoveries.
   Exact provenance controls supplied/reused labels; sample packets retain their
   canonical input text. Opening these disclosures does not retrieve new text. */
import type { ResearchSnapshot } from './generated/contracts';
import { SourceInspection } from './InspectionDetails';
import './TaskSourceData.css';
import scenario from '../../benchmarks/v1/scenarios/syn-abstract-disagreement.json';

const packetSnapshots = new Set(['syn-abstract-disagreement-snapshot-v2', 'syn-abstract-disagreement-baseline-snapshot']);

export function TaskSourceData({ snapshot, onOpenSource }: { snapshot: ResearchSnapshot; onOpenSource?: (sourceId: string) => void }) {
  if (snapshot.inspection) {
    const initialIds = [...new Set(snapshot.inspection.initialSourceIds)];
    return <section className="task-source-data" aria-label="Sources available at the start">
      <h4>Sources available at the start</h4>
      <p>{initialIds.length ? 'Starting material for this request. The labels show whether it was supplied, reused from earlier work, or retrieved.' : 'No starting material is recorded. Sources found during the run appear in Evidence.'}</p>
      {initialIds.length > 0 && <details className="inline-detail"><summary>View starting sources ({initialIds.length})</summary><div className="input-source-list">{initialIds.map(id => {
        const matches = snapshot.sources.filter(source => source.id === id);
        const source = matches.length === 1 ? matches[0] : undefined;
        const provenance = snapshot.inspection!.sources.filter(item => item.sourceId === id);
        const detail = provenance.length === 1 && source && provenance[0].copyReference.kind === 'source_copy' && provenance[0].copyReference.id === source.copyId && provenance[0].copyReference.version === source.version ? provenance[0] : undefined;
        const origin = detail?.origin === 'reused' ? 'Reused from an earlier run' : detail?.origin === 'supplied' ? 'Supplied with this request' : detail?.origin === 'retrieved' ? 'Retrieved during this run' : 'How this source was added is not recorded';
        return <article className="starting-source-card" key={id}>
          <div><strong>{source?.title || id}</strong><p className="starting-source-origin">{origin}</p></div>
          {source && onOpenSource && <button type="button" className="secondary-button" onClick={() => onOpenSource(source.id)}>Read in Evidence →</button>}
          <details className="starting-source-reference"><summary>Source details</summary>
            {source ? <><p>Saved copy {source.copyId} · v{source.version} · {source.accessState.replaceAll('_', ' ')}</p>{detail?.fromSnapshotId && <p>Reused from saved snapshot {detail.fromSnapshotId}</p>}
              {!onOpenSource && <details><summary>Read retained source text ({source.passages.length} excerpts)</summary>{source.passages.map((passage, index) => <div key={`${passage.spanId}:${index}`}><p>{passage.locator}</p><blockquote>{passage.text}</blockquote></div>)}{!source.passages.length && <p>No passage text retained.</p>}</details>}
              {detail && <SourceInspection snapshot={snapshot} sourceId={id} />}
            </> : <p>This starting source is missing, or more than one saved copy matches it.</p>}
          </details>
        </article>;
      })}</div></details>}
    </section>;
  }
  if (snapshot.proofKind === 'live_model_on_supplied_evidence') return <section className="task-source-data" aria-label="Source data supplied with this task"><h4>Saved source evidence</h4><p>{snapshot.sources.length} source record{snapshot.sources.length === 1 ? '' : 's'} retained from supplied evidence. Fresh web search is unavailable.</p><details className="inline-detail"><summary><span className="closed-label">View saved source data ({snapshot.sources.length} records)</span><span className="open-label">Hide saved source data</span></summary><div className="input-source-list">{snapshot.sources.map(source => <details className="input-source-record" key={source.id}><summary><span className="source-letter">{source.id}</span><strong>{source.title}</strong></summary><div className="input-source-text">{source.passages.map(passage => <p key={passage.spanId}>{passage.text}</p>)}<small>Saved source copy {source.copyId} · v{source.version}</small></div></details>)}</div></details></section>;
  const knownPacket = packetSnapshots.has(snapshot.snapshotId) && snapshot.sampleVersion === scenario.scenario_version;
  if (!knownPacket) return <div className="task-source-data"><h4>Source data</h4><p>The source material supplied at the start was not recorded for this snapshot.</p></div>;
  const inputs = scenario.sources.filter(source => scenario.initial_source_ids.includes(source.source_id));
  const abstracts = inputs.filter(source => source.surface === 'published_abstract').length;
  const notices = inputs.filter(source => source.surface === 'access_notice').length;
  return <section className="task-source-data" aria-label="Source data supplied with this task">
    <h4>Source data supplied with this task</h4>
    <p>{abstracts} simulated study abstracts and {notices} access notice · real clinical labels</p>
    <details className="inline-detail"><summary><span className="closed-label">View supplied source data ({inputs.length} records)</span><span className="open-label">Hide supplied source data</span></summary>
      <div className="input-source-list">{inputs.map(source => {
        const savedSource = snapshot.sources.find(item => item.id === source.source_id && item.contentHash === source.body_sha256);
        return <details className="input-source-record" key={source.source_id}><summary><span className="source-letter">{source.source_id}</span><span><strong>{savedSource?.title || `Source ${source.source_id}`}</strong><small>{source.surface === 'access_notice' ? 'Access notice · full text unavailable' : 'Simulated abstract · supplied at start'}</small></span></summary><div className="input-source-text"><p>{source.body}</p><small>Simulated source text · version {source.source_version}</small></div></details>;
      })}</div>
    </details>
  </section>;
}
