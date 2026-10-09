/* Lead with the saved answer, then expose its exact reference and work records.
   Recorded citations, text-only references, producer pointers, and peer replies
   retain separate labels so the view does not invent authorship or support. */
import type { ResearchSnapshot } from './generated/contracts';
import { useState } from 'react';
import { AgentBadge } from './AgentBadge';
import { CitationDetails } from './InspectionDetails';
import { ReportMarkdown } from './ReportMarkdown';
import { linkedPeerExchanges, reportProducer, reportReferences } from './reportProvenance';

type Props = {
  report?: ResearchSnapshot['artifacts'][number];
  sources: ResearchSnapshot['sources'];
  onOpenSource: (sourceId: string, spanId: string) => void;
  live?: boolean;
  inspectionSnapshot?: ResearchSnapshot;
  snapshot?: ResearchSnapshot;
  onOpenAttempt?: (attemptId: string) => void;
};

export function ReportOutput({ report, sources, onOpenSource, live = false, inspectionSnapshot, snapshot, onOpenAttempt }: Props) {
  const [detailsOpen, setDetailsOpen] = useState(false);
  const references = report ? reportReferences(report, sources) : [];
  const producer = report ? reportProducer(report, snapshot) : { kind: 'absent' as const };
  const peerExchanges = snapshot && producer.attempt?.status === 'completed' ? linkedPeerExchanges(snapshot, producer.attempt) : [];
  const agentName = (id: string) => snapshot?.agents.find(agent => agent.id === id)?.name;
  const recordedCount = references.filter(reference => reference.basis === 'recorded').length;
  const textCount = references.length - recordedCount;
  return <div className="report-output">
    <section className="output-interpretation" aria-label="Saved answer">
      <h4>Saved answer</h4>
      {report ? <div className="hero-answer"><ReportMarkdown text={report.body} sourceReferences={references.map((reference, index) => reference.passage ? { sourceId: reference.sourceId, spanId: reference.spanId, label: String(index + 1) } : null).filter((reference): reference is { sourceId: string; spanId: string; label: string } => reference !== null)} onOpenSourceReference={onOpenSource} /></div>
        : <p className="hero-answer">{live ? 'No report has been saved for this run yet.' : 'No report has been saved in this sample.'}</p>}
      {report && snapshot && <div className="report-attribution" aria-label="Report producer record">
        {producer.kind === 'recorded' ? <><p><strong>{producer.attempt.status === 'completed' ? 'Answer by' : 'Recorded producer attempt'}</strong> <AgentBadge name={agentName(producer.attempt.agentId)} agentId={producer.attempt.agentId} />{producer.attempt.status !== 'completed' && ` · ${producer.attempt.status}`}</p>{onOpenAttempt && <button className="inline-link" onClick={() => onOpenAttempt(producer.attempt.id)}>Open producer attempt →</button>}</>
          : producer.kind === 'legacy_match' ? <><p><strong>Matching saved contribution</strong> <AgentBadge name={agentName(producer.attempt.agentId)} agentId={producer.attempt.agentId} /></p>{onOpenAttempt && <button className="inline-link" onClick={() => onOpenAttempt(producer.attempt.id)}>Open matching attempt →</button>}</>
            : <p>{producer.kind === 'ambiguous' ? 'Producer not recorded; multiple completed attempts have text identical to this answer.' : producer.kind === 'unresolved' ? 'The saved producer attempt pointer does not resolve in this snapshot.' : 'Report producer not recorded in this snapshot.'}</p>}
      </div>}
    </section>

    {report && <details className="inline-detail output-results">
      <summary>Sources and supporting passages ({references.length} reference{references.length === 1 ? '' : 's'})</summary>
      <p className="output-limit">Saved citations: {recordedCount} · references found in saved text: {textCount}. Check the passages to assess support.</p>
      {references.length ? <div className="result-table-scroll"><table aria-label="Sources and supporting passages"><thead><tr><th scope="col">Saved source</th><th scope="col">Saved passage</th><th scope="col">Reference basis</th></tr></thead><tbody>{references.map((reference, index) => <tr key={`${reference.sourceId}-${reference.spanId}`}>
        <th scope="row"><strong>{reference.source?.title || 'Source title unavailable'}</strong>{reference.passage && <button className="inline-link" onClick={() => onOpenSource(reference.sourceId, reference.spanId)}>Open passage {index + 1} →</button>}<details className="inline-detail"><summary>Reference IDs</summary><p>{reference.sourceId} · {reference.spanId}</p></details></th>
        <td>{reference.passage ? <><span>{reference.passage.locator}</span><p>{reference.passage.text}</p></> : 'Exact passage unavailable or ambiguous in this snapshot.'}</td>
        <td>{reference.basis === 'recorded' ? 'Saved citation' : 'Reference found in saved text'}</td>
      </tr>)}</tbody></table></div> : <p className="output-limit">No source reference was recorded or identified in this answer. Other available sources are not automatically evidence for it.</p>}
      {inspectionSnapshot && report.citations.map(citation => <CitationDetails key={`${citation.sourceId}-${citation.spanId}`} snapshot={inspectionSnapshot} sourceId={citation.sourceId} spanId={citation.spanId} />)}
    </details>}

    {report && peerExchanges.length > 0 && <details className="inline-detail report-peer-exchanges"><summary>Related peer exchanges ({peerExchanges.length})</summary>{producer.kind === 'legacy_match' && <p>Linked to the task of the matching saved contribution; this older report has no explicit producer pointer.</p>}<p>Each request and reply has its own scope; this does not establish a review of the whole answer.</p>{peerExchanges.map(exchange => <article key={exchange.request.requestId || exchange.request.id}>
      <p><AgentBadge name={agentName(exchange.request.fromAgentId)} agentId={exchange.request.fromAgentId} /> asked <AgentBadge name={agentName(exchange.request.toAgentId)} agentId={exchange.request.toAgentId} /></p>
      <h5>Request scope</h5><ReportMarkdown text={exchange.request.summary} />
      <h5>Saved reply</h5><ReportMarkdown text={exchange.reply.summary} />
      {onOpenAttempt && <button className="inline-link" onClick={() => onOpenAttempt(exchange.attempt.id)}>Open peer attempt →</button>}
      <details className="inline-detail"><summary>Saved exchange IDs and times</summary><p>Request {exchange.request.requestId || exchange.request.id} · {exchange.request.createdAt}</p><p>Reply {exchange.reply.id} · {exchange.reply.createdAt}</p><p>Attempt {exchange.attempt.id}</p></details>
    </article>)}</details>}

    {report?.limitations.length ? <section className="output-uncertainty" aria-label="Saved limitations"><h4>Saved limitations</h4>{report.limitations.map((limitation, index) => <ReportMarkdown key={index} text={limitation} />)}</section> : null}
    {report && <details className="inline-detail report-details" onToggle={event => { if (event.target === event.currentTarget) setDetailsOpen(event.currentTarget.open); }}><summary>Report details</summary><dl>
      <div><dt>Saved report version</dt><dd>v{report.version}</dd></div>
      <div><dt>Proof kind</dt><dd>{snapshot?.proofKind ?? 'Not recorded in this view'}</dd></div>
      <div><dt>Producer attempt ID</dt><dd>{report.producerAttemptId ?? 'Not recorded'}</dd></div>
      <div><dt>Producer attribution</dt><dd>{producer.kind === 'recorded' ? 'Explicit saved producer pointer.' : producer.kind === 'legacy_match' ? 'One completed attempt has text identical to this saved answer; this legacy match is not an authorship pointer.' : producer.kind === 'ambiguous' ? 'Several completed attempts have identical text; no producer is inferred.' : producer.kind === 'unresolved' ? 'Saved producer pointer could not be resolved in this snapshot.' : 'Not recorded.'}</dd></div>
      <div><dt>Separate limitations</dt><dd>{report.limitations.length ? `${report.limitations.length} saved` : 'Not separately recorded in this artifact; check the answer and sources for stated uncertainty.'}</dd></div>
    </dl>{detailsOpen && <><h5>Raw saved body</h5><pre>{report.body}</pre></>}</details>}
  </div>;
}
