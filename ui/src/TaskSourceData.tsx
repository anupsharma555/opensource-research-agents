/* Show the public source packet supplied with the scripted research question.
   The canonical scenario owns its initial source IDs and complete source text.
   Unknown snapshots stay explicit about missing input provenance. */
import type { ResearchSnapshot } from './generated/contracts';
import { SourceInspection } from './InspectionDetails';
import scenario from '../../benchmarks/v1/scenarios/syn-abstract-disagreement.json';

const packetSnapshots = new Set(['syn-abstract-disagreement-snapshot-v2', 'syn-abstract-disagreement-baseline-snapshot']);

export function TaskSourceData({ snapshot }: { snapshot: ResearchSnapshot }) {
  if (snapshot.inspection?.initialSourceIds.length === 0) return <section className="task-source-data" aria-label="Source data supplied with this task"><h4>Source data supplied with this task</h4><p>No source records are identified as supplied input. Evidence acquired during the task is available with its contributions.</p></section>;
  if (snapshot.inspection) return <section className="task-source-data" aria-label="Source data supplied with this task"><h4>Source data supplied with this task</h4>
    <p>{snapshot.inspection.initialSourceIds.length} source records identified as input. Sources discovered later are listed with their contributions.</p>
    <details className="inline-detail"><summary>View supplied source data ({snapshot.inspection.initialSourceIds.length} records)</summary><div className="input-source-list">{snapshot.inspection.initialSourceIds.map(id => {
      const matches = snapshot.sources.filter(source => source.id === id);
      const source = matches.length === 1 ? matches[0] : undefined;
      return <details className="input-source-record" key={id}><summary>{source?.title || id}</summary>{source ? <><p>Saved copy {source.copyId} · v{source.version} · {source.accessState.replaceAll('_', ' ')}</p>{source.passages.map(passage => <div key={passage.spanId}><p>{passage.locator}</p><blockquote>{passage.text}</blockquote></div>)}{!source.passages.length && <p>No passage text retained.</p>}</> : <p>The supplied source is unavailable or ambiguous in this snapshot.</p>}<SourceInspection snapshot={snapshot} sourceId={id} /></details>;
    })}</div></details>
  </section>;
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
