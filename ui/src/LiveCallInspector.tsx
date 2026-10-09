/* Render saved calls plainly; new records use the host's exact trace links. */
import { useEffect, useState } from 'react';
import { AgentBadge } from './AgentBadge';
import type { ResearchSnapshot } from './generated/contracts';
import { getLiveCalls } from './live';

type Json = Record<string, unknown>;
const obj = (value: unknown): Json | null => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Json : null;
const str = (value: unknown): string | null => typeof value === 'string' && value.length > 0 ? value : null;

function linkedPeers(call: Json, snapshot: ResearchSnapshot) {
  const requester = str(call.agentId);
  if (!requester || !Array.isArray(call.messages)) return [];
  const links: Array<{ requestId: string; peerId: string; requesterId: string; peerName: string | null; requesterName: string | null; reply: string; attemptId: string | null }> = [];
  for (const value of call.messages) {
    const message = obj(value);
    if (message?.role !== 'user' || typeof message.content !== 'string') continue;
    let envelope: Json | null = null;
    try { envelope = obj(JSON.parse(message.content)); } catch { /* Ordinary text is not linkage. */ }
    const id = str(envelope?.peerRequestId);
    const reply = str(envelope?.peerResponse);
    if (!id || !reply || links.some(link => link.requestId === id)) continue;
    const requests = snapshot.messages.filter(item => item.requestId === id && item.fromAgentId === requester && item.toAgentId !== requester && item.status === 'fulfilled');
    if (requests.length !== 1) continue;
    const request = requests[0];
    const delivered = snapshot.messages.filter(item => item.requestId === id && item.status === 'delivered' && item.fromAgentId === request.toAgentId && item.toAgentId === requester && item.taskId === request.taskId && item.summary === reply);
    if (delivered.length !== 1) continue;
    const callTime = Date.parse(str(call.createdAt) ?? '');
    const attempts = snapshot.attempts.filter(item => {
      const ended = Date.parse(item.endedAt ?? '');
      return item.taskId === request.taskId && item.agentId === request.toAgentId
        && item.resultSummary === reply && Number.isFinite(ended)
        && Number.isFinite(callTime) && ended <= callTime;
    });
    links.push({ requestId: id, reply, peerId: request.toAgentId, requesterId: requester,
      peerName: snapshot.agents.find(item => item.id === request.toAgentId)?.name ?? null,
      requesterName: snapshot.agents.find(item => item.id === requester)?.name ?? null,
      attemptId: attempts.length === 1 ? attempts[0].id : null });
  }
  return links;
}

export function LiveCallInspector({ runId, attemptId, snapshotId, snapshot, onOpenAttempt }: {
  runId: string | null; attemptId: string; snapshotId: string; snapshot: ResearchSnapshot;
  onOpenAttempt?: (attemptId: string) => void;
}) {
  const [records, setRecords] = useState<unknown[] | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    if (!runId) return;
    let active = true;
    setRecords(null);
    setError(null);
    getLiveCalls(runId, attemptId)
      .then(result => { if (active) setRecords(result.records); })
      .catch(cause => { if (active) setError(cause instanceof Error ? cause.message : 'Call records unavailable.'); });
    return () => { active = false; };
  }, [runId, attemptId, snapshotId]);
  return <section className="live-call-inspector" aria-label="Actual model call records">
    <h4>Actual model call records</h4>
    <p>Saved inputs, responses and tool results are shown below. Inherited attempts retain their originating run. Private model reasoning is unavailable.</p>
    {error ? <p role="alert">{error}</p> : records === null ? <p role="status">Loading call records…</p> : records.length === 0 ? <p>No call record has been saved for this attempt.</p> : records.map((raw, index) => {
      const call = obj(raw);
      const response = obj(call?.response);
      const trace = obj(call?.trace);
      const canonical = trace?.basis === 'linked_saved_records';
      const traceAuthor = canonical ? obj(trace.author) : null;
      const authorId = str(traceAuthor?.agentId) ?? str(call?.agentId);
      const authorName = canonical ? str(traceAuthor?.name)
        : snapshot.agents.find(item => item.id === authorId)?.name ?? null;
      const outputs = canonical && Array.isArray(trace.outputs) ? trace.outputs.map(obj).filter(item => item !== null) : null;
      const contentPath = outputs?.find(item => item.recordPath === 'response.content' && item.authoredByAgentId === authorId);
      const toolsPath = outputs?.find(item => item.recordPath === 'response.tool_calls' && item.authoredByAgentId === authorId);
      const content = !canonical || contentPath ? str(response?.content) : null;
      const results = Array.isArray(call?.toolResults) ? call.toolResults : [];
      const requested = (!canonical || toolsPath) && Array.isArray(response?.tool_calls) ? response.tool_calls : [];
      const tracedPeers = canonical && Array.isArray(trace.peerInputs) ? trace.peerInputs.map(obj).filter(item => item !== null) : [];
      const retryIndex = str(call?.id) ? records.findIndex(item => obj(item)?.retryOfCallId === call?.id) : -1;
      return <article className="panel live-call-card" key={str(call?.id) ?? index}>
        <h5>Call {index + 1} · <AgentBadge name={authorName} agentId={authorId} /></h5>
        <dl className="detail-list"><div><dt>Status</dt><dd>{str(call?.status) ?? 'Not recorded'}{str(call?.phase) ? ` · ${call?.phase}` : ''}</dd></div><div><dt>Model</dt><dd>{str(response?.model) ?? 'No model response recorded'}</dd></div><div><dt>Started</dt><dd>{str(call?.createdAt) ?? 'Not recorded'}</dd></div><div><dt>Finished</dt><dd>{str(call?.completedAt) ?? 'Not recorded'}</dd></div><div><dt>Originating run</dt><dd>{str(traceAuthor?.runId) ?? str(call?.runId) ?? 'Not recorded'}</dd></div>{canonical && <><div><dt>Author record</dt><dd>{str(traceAuthor?.agentId) ?? 'Unknown'} · {str(traceAuthor?.role) ?? 'role not recorded'}</dd></div><div><dt>Task / attempt</dt><dd>{str(traceAuthor?.taskId) ?? 'Unknown'} / {str(traceAuthor?.attemptId) ?? 'Unknown'}</dd></div></>}</dl>
        {call?.status === 'rate_limited' ? <p>Quota wait: no model response was returned for this call.</p> : content ? <div><h6>Response from <AgentBadge name={authorName} agentId={authorId} /></h6>{contentPath && <p>Saved output: {str(contentPath.recordPath)}</p>}<pre>{content}</pre></div> : <p>No final text response was linked for this call.</p>}
        {requested.length > 0 && <div><h6>Requested functions</h6>{toolsPath && <p>Saved output: {str(toolsPath.recordPath)}</p>}{requested.map((item, toolIndex) => <p key={toolIndex}>{str(obj(obj(item)?.function)?.name) ?? 'Unnamed function'} · {str(obj(obj(item)?.function)?.arguments) ?? 'Arguments not recorded'}</p>)}</div>}
        {results.length > 0 && <div><h6>Host tool results</h6>{results.map((item, toolIndex) => <div key={toolIndex}><strong>Tool result {toolIndex + 1}</strong><pre>{str(obj(item)?.content) ?? JSON.stringify(item, null, 2)}</pre></div>)}</div>}
        {canonical ? tracedPeers.map((link, linkIndex) => link.linkStatus === 'linked' ? <div className="live-peer-link" key={str(link.peerRequestId) ?? linkIndex}>
          <h6><AgentBadge name={str(link.peerAgentName)} agentId={str(link.peerAgentId)} /> reply used by <AgentBadge name={authorName} agentId={authorId} /></h6>
          <p>Saved peer request {str(link.peerRequestId) ?? 'not recorded'} · input {str(link.inputRecordPath) ?? 'not recorded'}</p>
          <p>Request record {str(link.requestMessageId) ?? 'not recorded'} · reply record {str(link.responseMessageId) ?? 'not recorded'}</p>
          <blockquote>{str(link.peerResponse) ?? 'No reply text recorded.'}</blockquote>
          {link.sourceAttemptResolution === 'unique' && str(link.sourceAttemptId) && onOpenAttempt ? <button type="button" className="inline-link" onClick={() => onOpenAttempt(str(link.sourceAttemptId)!)}>Open {str(link.peerAgentName) ?? 'peer'} attempt</button> : <p>Peer attempt: {str(link.sourceAttemptResolution) ?? 'unknown'}.</p>}
        </div> : <p key={str(link.peerRequestId) ?? linkIndex}>Peer input {str(link.peerRequestId) ?? 'unknown'}: saved relationship {str(link.linkStatus) ?? 'unknown'}; no author or attempt is inferred.</p>)
          : call && linkedPeers(call, snapshot).map(link => <div className="live-peer-link" key={link.requestId}><h6><AgentBadge name={link.peerName} agentId={link.peerId} /> reply used by <AgentBadge name={link.requesterName} agentId={link.requesterId} /></h6><p>Saved peer request {link.requestId}</p><blockquote>{link.reply}</blockquote>{link.attemptId && onOpenAttempt ? <button type="button" className="inline-link" onClick={() => onOpenAttempt(link.attemptId!)}>Open {link.peerName ?? 'peer'} attempt</button> : !link.attemptId ? <p>Peer attempt cannot be identified uniquely from these saved records.</p> : null}</div>)}
        {retryIndex >= 0 && <p>Retried in Call {retryIndex + 1}; inspect that call for its separate outcome.</p>}
        {str(call?.retryOfCallId) && <p>This call retries saved call {str(call?.retryOfCallId)}.</p>}
        <details className="inline-detail"><summary>Full raw call JSON</summary><pre>{JSON.stringify(raw, null, 2)}</pre></details>
      </article>;
    })}
  </section>;
}
