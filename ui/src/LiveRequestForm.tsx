/* Gather a real start request or an exact saved follow-up target.
   The form sends only selected peer IDs and supplied evidence; the authenticated
   server owns model access, durable receipts, and target-version validation. */
import { useRef, useState } from 'react';
import type { ResearchSnapshot } from './generated/contracts';
import type { LiveAgent, LiveReceipt, LiveStartRequest, LiveFollowupRequest } from './live';

type Request = LiveStartRequest | LiveFollowupRequest;

export function LiveRequestForm({ agents, snapshot, runId, receipt, receiptAgentIds, submitting, error, onSubmit }: {
  agents: LiveAgent[];
  snapshot: ResearchSnapshot | null;
  runId: string | null;
  receipt: LiveReceipt | null;
  receiptAgentIds: string[];
  submitting: boolean;
  error: string | null;
  onSubmit: (request: Request) => Promise<void>;
}) {
  const [kind, setKind] = useState<'start' | 'followup'>('start');
  const [question, setQuestion] = useState('');
  const [scope, setScope] = useState('');
  const [evidenceText, setEvidenceText] = useState('');
  const [selectedAgents, setSelectedAgents] = useState<string[]>(agents[0] ? [agents[0].id] : []);
  const targets = snapshot ? [
    ...snapshot.claims.map(item => ({ id: item.id, version: item.version, label: `Claim ${item.id} · v${item.version}` })),
    ...snapshot.tasks.map(item => ({ id: item.id, version: snapshot.mission.version, label: `Task ${item.id} · mission v${snapshot.mission.version}` })),
    ...snapshot.artifacts.map(item => ({ id: item.id, version: item.version, label: `${item.kind} ${item.id} · v${item.version}` })),
    ...snapshot.sources.map(item => ({ id: item.id, version: item.version, label: `Source ${item.id} · v${item.version}` })),
  ] : [];
  const [targetKey, setTargetKey] = useState('');
  const pendingRequestRef = useRef<Request | null>(null);
  const [pendingRequest, setPendingRequest] = useState<Request | null>(null);
  const pendingFollowup = pendingRequest && 'parentRunId' in pendingRequest ? pendingRequest : null;
  const selectedTarget = targets.find(item => `${item.id}:${item.version}` === targetKey) ?? targets[0];
  const displayedTargetKey = pendingFollowup ? `${pendingFollowup.targetId}:${pendingFollowup.targetVersion}` : selectedTarget ? `${selectedTarget.id}:${selectedTarget.version}` : '';
  const displayedTargets = pendingFollowup && !targets.some(item => `${item.id}:${item.version}` === displayedTargetKey)
    ? [{ id: pendingFollowup.targetId, version: pendingFollowup.targetVersion, label: `Saved target ${pendingFollowup.targetId} · v${pendingFollowup.targetVersion} (pending retry)` }, ...targets]
    : targets;

  function revise() { pendingRequestRef.current = null; setPendingRequest(null); }

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (pendingRequestRef.current) return onSubmit(pendingRequestRef.current);
    if (!question.trim() || !selectedAgents.length || (kind === 'start' && !evidenceText.trim())) return;
    const requestId = `request-${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`}`;
    const base = { requestId, question: question.trim(), scope: scope.trim(), agentIds: selectedAgents as [string, ...string[]] };
    const request: Request | null = kind === 'start'
      ? { ...base, evidenceText: evidenceText.trim() }
      : snapshot && runId && selectedTarget
        ? { ...base, parentRunId: runId, parentSnapshotId: snapshot.snapshotId, targetId: selectedTarget.id, targetVersion: selectedTarget.version }
        : null;
    if (!request) return;
    pendingRequestRef.current = request;
    setPendingRequest(request);
    await onSubmit(request);
  }

  return <div className="page-stack live-request">
    <div className="section-heading"><span className="eyebrow">Live research</span><h2>Start or follow up</h2><p>Agents work from pasted or saved evidence. Fresh web search is unavailable in this local slice.</p></div>
    <form className="panel" onSubmit={submit}>
      <div className="segmented-control" role="group" aria-label="Request type">
        <button type="button" className={kind === 'start' ? 'active' : ''} onClick={() => { setKind('start'); revise(); }}>New mission</button>
        <button type="button" className={kind === 'followup' ? 'active' : ''} disabled={!snapshot || !runId} onClick={() => { setKind('followup'); revise(); }}>Follow up on exact work</button>
      </div>
      <label className="field-label" htmlFor="live-question">Research question or follow-up</label>
      <textarea id="live-question" rows={3} required value={question} onChange={event => { setQuestion(event.target.value); revise(); }} />
      <label className="field-label" htmlFor="live-scope">Scope and constraints</label>
      <textarea id="live-scope" rows={3} value={scope} onChange={event => { setScope(event.target.value); revise(); }} />
      {kind === 'start' ? <><label className="field-label" htmlFor="live-evidence">Pasted source evidence</label><textarea id="live-evidence" rows={7} required value={evidenceText} onChange={event => { setEvidenceText(event.target.value); revise(); }} /><p className="tiny-note">This text is supplied evidence. The app does not retrieve or verify a web source.</p></> : <><label className="field-label" htmlFor="live-target">Exact saved target</label><select id="live-target" value={displayedTargetKey} onChange={event => { setTargetKey(event.target.value); revise(); }}>{displayedTargets.map(item => <option key={`${item.id}:${item.version}`} value={`${item.id}:${item.version}`}>{item.label}</option>)}</select><p className="tiny-note">Parent snapshot {pendingFollowup?.parentSnapshotId ?? snapshot?.snapshotId}{pendingFollowup ? ' · pending retry remains pinned to this saved parent' : ''}; the saved parent is retained for comparison.</p></>}
      <fieldset className="live-agent-picker"><legend>Participating peers requested</legend>{agents.map(agent => <label key={agent.id}><input type="checkbox" checked={selectedAgents.includes(agent.id)} onChange={event => { setSelectedAgents(current => event.target.checked ? [...current, agent.id] : current.filter(id => id !== agent.id)); revise(); }} /><span><strong>{agent.name}</strong> · {agent.role} <small>skill {agent.skillVersion}</small></span></label>)}</fieldset>
      <button className="primary-button submit-button" type="submit" disabled={submitting || (!pendingRequest && (!question.trim() || !selectedAgents.length || (kind === 'start' && !evidenceText.trim()) || (kind === 'followup' && !selectedTarget)))}>{submitting ? 'Submitting…' : pendingRequest ? 'Retry same request' : kind === 'start' ? 'Start real run' : 'Submit exact follow-up'}</button>
    </form>
    {error && <div className="state-card" role="alert"><strong>Request failed</strong><p>{error}</p></div>}
    {receipt && <div className="receipt-card" role="status"><span className="eyebrow">Saved service receipt</span><strong>{receipt.runId} · {receipt.status ?? 'queued'}</strong><p>Task {receipt.taskId} · request {receipt.requestId}{receipt.reused ? ' · existing request reused' : ''}</p><p>Requested peers: {receiptAgentIds.map(id => agents.find(agent => agent.id === id)?.name ?? id).join(', ')}. Actual work appears in Activity and Details as the run updates.</p></div>}
  </div>;
}
