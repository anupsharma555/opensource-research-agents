/* Gather a real start request or an exact saved follow-up target.
   The form sends only selected peer IDs and supplied evidence; the authenticated
   server owns model access, durable receipts, and target-version validation. */
import { useEffect, useRef, useState } from 'react';
import { invoke, isTauri } from '@tauri-apps/api/core';
import type { ResearchSnapshot } from './generated/contracts';
import type { LiveAgent, LiveReceipt, LiveStartRequest, LiveFollowupRequest } from './live';
import { SavedRecord } from './InspectionDetails';

type Request = LiveStartRequest | LiveFollowupRequest;
type CredentialChoice = 'default' | 'primary' | 'backup';
type Draft = { kind: 'start' | 'followup'; question: string; scope: string; evidenceText: string; selectedAgents: string[]; requiredAgents: string[]; targetKey: string; followupRunId: string | null; credentialChoice?: CredentialChoice; pendingRequest: Request | null };

function requestedCredential(request: Request): string {
  if (request.credentialProfile === 'backup') return 'Backup';
  if (request.credentialProfile === 'primary') return 'Primary';
  return 'parentRunId' in request ? 'Same as earlier run (default)' : 'Primary (default)';
}

function parseDraft(raw: string): Draft {
  const value = JSON.parse(raw) as Draft;
  if (!value || (value.kind !== 'start' && value.kind !== 'followup') || typeof value.question !== 'string' || typeof value.scope !== 'string' || typeof value.evidenceText !== 'string' || !Array.isArray(value.selectedAgents) || !value.selectedAgents.every(id => typeof id === 'string') || typeof value.targetKey !== 'string') throw new Error('Invalid saved draft');
  if (value.pendingRequest != null && (typeof value.pendingRequest !== 'object' || typeof value.pendingRequest.requestId !== 'string')) throw new Error('Invalid pending request identity');
  const choice = value.credentialChoice ?? value.pendingRequest?.credentialProfile ?? 'default';
  if (choice !== 'default' && choice !== 'primary' && choice !== 'backup') throw new Error('Invalid saved credential choice');
  return { ...value, requiredAgents: Array.isArray(value.requiredAgents) ? value.requiredAgents.filter(id => typeof id === 'string' && value.selectedAgents.includes(id)) : [], followupRunId: typeof value.followupRunId === 'string' ? value.followupRunId : null, credentialChoice: choice, pendingRequest: value.pendingRequest ?? null };
}

function storedDraft(workspaceId?: string | null): Draft | null {
  if (!workspaceId || isTauri()) return null;
  try {
    const raw = window.sessionStorage.getItem(`atlas-live-draft:${workspaceId}`);
    return raw === null ? null : parseDraft(raw);
  } catch { return null; }
}

export function LiveRequestForm({ agents, snapshot, runId, workspaceId, parentAgentIds, parentRequiredAgentIds, retrievalRoutes = [], workflowVersion, receipt, receiptAgentIds, receiptRequiredAgentIds = [], submitting, error, errorKind = 'unconfirmed', onSubmit }: {
  agents: LiveAgent[];
  snapshot: ResearchSnapshot | null;
  runId: string | null;
  workspaceId?: string | null;
  parentAgentIds?: string[] | null;
  parentRequiredAgentIds?: string[] | null;
  retrievalRoutes?: string[];
  workflowVersion?: string | null;
  receipt: LiveReceipt | null;
  receiptAgentIds: string[];
  receiptRequiredAgentIds?: string[];
  submitting: boolean;
  error: string | null;
  errorKind?: 'unconfirmed' | 'view_unavailable';
  onSubmit: (request: Request) => Promise<void>;
}) {
  const [saved] = useState(() => storedDraft(workspaceId));
  const [kind, setKind] = useState<'start' | 'followup'>(saved?.kind ?? 'start');
  const [question, setQuestion] = useState(saved?.question ?? '');
  const [scope, setScope] = useState(saved?.scope ?? '');
  const [evidenceText, setEvidenceText] = useState(saved?.evidenceText ?? '');
  const [selectedAgents, setSelectedAgents] = useState<string[]>(() => saved?.selectedAgents ?? (() => {
    const first = agents.find(agent => !agent.availability || agent.availability === 'available' || agent.availability === 'enabled');
    return first ? [first.id] : [];
  })());
  const [requiredAgents, setRequiredAgents] = useState<string[]>(saved?.requiredAgents ?? []);
  const targets = snapshot ? [
    ...snapshot.claims.map(item => ({ id: item.id, version: item.version, label: `Claim ${item.id} · v${item.version}` })),
    ...snapshot.tasks.map(item => ({ id: item.id, version: item.version ?? snapshot.mission.version, label: `Task · ${item.title || item.id} · ${item.version ? `v${item.version}` : `mission v${snapshot.mission.version} (task version unavailable)`} · saved ID ${item.id}` })),
    ...snapshot.artifacts.map(item => ({ id: item.id, version: item.version, label: `${item.kind === 'report' ? 'Report' : item.kind.replaceAll('_', ' ')} · ${item.title || item.id} · v${item.version} · saved ID ${item.id}` })),
    ...snapshot.sources.map(item => ({ id: item.id, version: item.version, label: `Source · ${item.title || item.id} · v${item.version} · saved ID ${item.id}` })),
  ] : [];
  const [targetKey, setTargetKey] = useState(saved?.targetKey ?? '');
  const [followupRunId, setFollowupRunId] = useState<string | null>(saved?.followupRunId ?? null);
  const [credentialChoice, setCredentialChoice] = useState<CredentialChoice>(saved?.credentialChoice ?? 'default');
  const pendingRequestRef = useRef<Request | null>(saved?.pendingRequest ?? null);
  const saveQueue = useRef<Promise<unknown>>(Promise.resolve());
  const [pendingRequest, setPendingRequest] = useState<Request | null>(saved?.pendingRequest ?? null);
  const [draftReady, setDraftReady] = useState(!isTauri() || !workspaceId);
  const [draftLoadError, setDraftLoadError] = useState(false);
  const [loadAttempt, setLoadAttempt] = useState(0);
  const [draftWarning, setDraftWarning] = useState<string | null>(null);
  const [submittedCredential, setSubmittedCredential] = useState<{ requestId: string; label: string } | null>(null);
  const credentialProfilesAvailable = workflowVersion === 'stage4-mission-v1';
  const pendingFollowup = pendingRequest && 'parentRunId' in pendingRequest ? pendingRequest : null;
  const selectedTarget = targets.find(item => `${item.id}:${item.version}` === targetKey);
  const displayedTargetKey = pendingFollowup ? `${pendingFollowup.targetId}:${pendingFollowup.targetVersion}` : targetKey;
  const displayedTargets = pendingFollowup && !targets.some(item => `${item.id}:${item.version}` === displayedTargetKey)
    ? [{ id: pendingFollowup.targetId, version: pendingFollowup.targetVersion, label: `Saved target ${pendingFollowup.targetId} · v${pendingFollowup.targetVersion} (pending retry)` }, ...targets]
    : targets;

  useEffect(() => {
    if (!workspaceId || !isTauri()) return;
    let active = true;
    setDraftReady(false);
    setDraftLoadError(false);
    void invoke<string | null>('load_live_draft', { workspaceId }).then(raw => {
      if (!active) return;
      if (raw !== null) {
        const value = parseDraft(raw);
        setKind(value.kind); setQuestion(value.question); setScope(value.scope); setEvidenceText(value.evidenceText); setSelectedAgents(value.selectedAgents); setRequiredAgents(value.requiredAgents); setTargetKey(value.targetKey); setFollowupRunId(value.followupRunId); setCredentialChoice(value.credentialChoice ?? 'default');
        pendingRequestRef.current = value.pendingRequest; setPendingRequest(value.pendingRequest);
      }
      setDraftReady(true);
    }).catch(() => { if (active) setDraftLoadError(true); });
    return () => { active = false; };
  }, [workspaceId, loadAttempt]);

  useEffect(() => {
    if (!workspaceId || !draftReady) return;
    const draft = JSON.stringify({ kind, question, scope, evidenceText, selectedAgents, requiredAgents, targetKey, followupRunId, credentialChoice, pendingRequest });
    if (isTauri()) saveQueue.current = saveQueue.current.catch(() => {}).then(() => invoke('save_live_draft', { workspaceId, draft })).then(() => setDraftWarning(null)).catch(() => setDraftWarning('This draft could not be saved locally. Keep this window open while revising it.'));
    else try { window.sessionStorage.setItem(`atlas-live-draft:${workspaceId}`, draft); setDraftWarning(null); } catch { setDraftWarning('This draft could not be saved in this browser session.'); }
  }, [workspaceId, draftReady, kind, question, scope, evidenceText, selectedAgents, requiredAgents, targetKey, followupRunId, credentialChoice, pendingRequest]);

  useEffect(() => {
    if (receipt && pendingRequest?.requestId === receipt.requestId) revise();
  }, [receipt?.requestId, pendingRequest?.requestId]);

  function revise() { pendingRequestRef.current = null; setPendingRequest(null); }

  async function submitFrozen(request: Request) {
    if (workspaceId && isTauri()) {
      const draft = JSON.stringify({ kind, question, scope, evidenceText, selectedAgents, requiredAgents, targetKey, followupRunId, credentialChoice, pendingRequest: request });
      try {
        saveQueue.current = saveQueue.current.catch(() => {}).then(() => invoke('save_live_draft', { workspaceId, draft }));
        await saveQueue.current;
        setDraftWarning(null);
      } catch {
        setDraftWarning('This draft could not be saved locally. Keep this window open while revising it.');
        return;
      }
    }
    setSubmittedCredential({ requestId: request.requestId, label: requestedCredential(request) });
    await onSubmit(request);
  }

  async function submit(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!draftReady) return;
    if (pendingRequestRef.current) return submitFrozen(pendingRequestRef.current);
    if (!question.trim() || !selectedAgents.length || (kind === 'start' && !evidenceText.trim() && retrievalRoutes.length === 0) || (kind === 'followup' && followupRunId !== runId)) return;
    const requestId = `request-${globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`}`;
    const base = { requestId, question: question.trim(), scope: scope.trim(), agentIds: selectedAgents as [string, ...string[]],
      ...(requiredAgents.length ? { requiredAgentIds: requiredAgents.filter(id => selectedAgents.includes(id)) } : {}),
      ...(credentialProfilesAvailable && credentialChoice !== 'default' ? { credentialProfile: credentialChoice } : {}) };
    const request: Request | null = kind === 'start'
      ? { ...base, ...(evidenceText.trim() ? { evidenceText: evidenceText.trim() } : {}) }
      : snapshot && runId && selectedTarget
        ? { ...base, parentRunId: runId, parentSnapshotId: snapshot.snapshotId, targetId: selectedTarget.id, targetVersion: selectedTarget.version }
        : null;
    if (!request) return;
    pendingRequestRef.current = request;
    setPendingRequest(request);
    await submitFrozen(request);
  }

  if (!draftReady) return <div className="page-stack live-request">{draftLoadError
    ? <div className="state-card" role="alert"><p>The saved local draft could not be loaded. It has not been changed.</p><button type="button" className="outline-button" onClick={() => setLoadAttempt(count => count + 1)}>Retry loading draft</button></div>
    : <p role="status">Loading the saved local draft…</p>}</div>;

  return <div className="page-stack live-request">
    <div className="section-heading"><h2>Prepare your request</h2><p>Ask a research question, add any instructions, then choose the agents to begin the work.</p></div>
    <form className="panel" onSubmit={submit}>
      <div className="segmented-control" role="group" aria-label="Request type">
        <button type="button" className={kind === 'start' ? 'active' : ''} onClick={() => { setKind('start'); setSelectedAgents([]); setRequiredAgents([]); setFollowupRunId(null); setCredentialChoice('default'); revise(); }}>New research run</button>
        <button type="button" className={kind === 'followup' ? 'active' : ''} disabled={!snapshot || !runId} onClick={() => { setKind('followup'); setFollowupRunId(runId); setTargetKey(''); setSelectedAgents(parentAgentIds ?? []); setRequiredAgents((parentRequiredAgentIds ?? []).filter(id => parentAgentIds?.includes(id))); setCredentialChoice('default'); revise(); }}>Follow up on saved work</button>
      </div>
      <label className="field-label" htmlFor="live-question">Research question or follow-up</label>
      <textarea id="live-question" rows={3} required value={question} onChange={event => { setQuestion(event.target.value); revise(); }} />
      <label className="field-label" htmlFor="live-scope">Additional instructions</label>
      <textarea id="live-scope" rows={3} value={scope} onChange={event => { setScope(event.target.value); revise(); }} />
      {credentialProfilesAvailable && <><label className="field-label" htmlFor="live-credential-profile">Model account profile</label><select id="live-credential-profile" value={credentialChoice} onChange={event => { setCredentialChoice(event.target.value as CredentialChoice); revise(); }}><option value="default">{kind === 'start' ? 'Primary (default)' : 'Same as earlier run (default)'}</option><option value="primary">Primary</option><option value="backup">Backup</option></select><p className="tiny-note">Primary and Backup use the access keys configured for this app. Choose which profile this request should use; Atlas will not switch it automatically.</p></>}
      {kind === 'start' ? <><label className="field-label" htmlFor="live-evidence">Pasted source evidence</label><p className="tiny-note">{retrievalRoutes.length ? 'Optional: agents can also retrieve sources.' : 'Required: this setup cannot retrieve new sources.'}</p><textarea id="live-evidence" rows={7} required={retrievalRoutes.length === 0} value={evidenceText} onChange={event => { setEvidenceText(event.target.value); revise(); }} /><p className="tiny-note">Atlas labels pasted text as material you supplied. Any sources found by agents are recorded separately in Evidence.</p></> : <><label className="field-label" htmlFor="live-target">Work to continue</label><select id="live-target" value={displayedTargetKey} onChange={event => { setTargetKey(event.target.value); revise(); }}><option value="">Choose saved work to continue</option>{displayedTargets.map(item => <option key={`${item.id}:${item.version}`} value={`${item.id}:${item.version}`}>{item.label}</option>)}</select><p className="tiny-note">The earlier run remains available for comparison.{pendingFollowup ? ' Retrying keeps the same saved work and version.' : ''}</p><details className="inline-detail"><summary>Earlier run reference</summary><p>Saved view ID: {pendingFollowup?.parentSnapshotId ?? snapshot?.snapshotId}</p></details>{!pendingRequest && followupRunId !== runId && <div className="state-card" role="alert"><p>This draft belongs to another run, or its earlier run was not recorded. Continuing from the selected run will reset the work, agent and account profile selections.</p><button type="button" className="outline-button" onClick={() => { setFollowupRunId(runId); setTargetKey(''); setSelectedAgents(parentAgentIds ?? []); setRequiredAgents((parentRequiredAgentIds ?? []).filter(id => parentAgentIds?.includes(id))); setCredentialChoice('default'); revise(); }}>Continue from selected run</button></div>}</>}
      <fieldset className="live-agent-picker"><legend>Starting agents</legend><p className="tiny-note">Choose who begins this request. Agents can work independently and collaborate. To require a contribution from an agent before the request is complete, select the option below their name. A follow-up keeps the earlier run’s choices unless you change them.</p>{kind === 'followup' && parentRequiredAgentIds == null && <p className="tiny-note">The earlier run did not record whether any agent’s contribution was required. Choose explicitly for this follow-up.</p>}{agents.length === 0 && <p>No agent is available in this setup.</p>}{agents.map(agent => { const unavailable = agent.availability && agent.availability !== 'available' && agent.availability !== 'enabled'; const configSaved = !!snapshot?.inspection?.records.some(item => item.reference.kind === agent.configurationReference?.kind && item.reference.id === agent.configurationReference?.id && item.reference.version === agent.configurationReference?.version); return <div className="live-agent-option" key={agent.id}><label><input type="checkbox" checked={selectedAgents.includes(agent.id)} disabled={!!unavailable} onChange={event => { setSelectedAgents(current => event.target.checked ? [...current, agent.id] : current.filter(id => id !== agent.id)); if (!event.target.checked) setRequiredAgents(current => current.filter(id => id !== agent.id)); revise(); }} /><span><strong>{agent.name}</strong> · {agent.role}<small>{unavailable ? `Unavailable: ${agent.unavailableReason || agent.availability}` : 'Available for selection'}</small></span></label>{selectedAgents.includes(agent.id) && <label className="live-required-option"><input type="checkbox" aria-label={`Wait for ${agent.name}'s contribution before completing this request`} checked={requiredAgents.includes(agent.id)} onChange={event => { setRequiredAgents(current => event.target.checked ? [...current, agent.id] : current.filter(id => id !== agent.id)); revise(); }} /><span>Wait for {agent.name}'s contribution before completing this request</span></label>}{unavailable && selectedAgents.includes(agent.id) && <button type="button" className="inline-link" onClick={() => { setSelectedAgents(current => current.filter(id => id !== agent.id)); setRequiredAgents(current => current.filter(id => id !== agent.id)); revise(); }}>Remove unavailable {agent.name} from this request</button>}<details className="inline-detail"><summary>Inspect {agent.name}'s model and skills</summary><dl className="detail-list"><div><dt>Role template</dt><dd>{agent.roleTemplateId || 'Not recorded'}</dd></div><div><dt>Selected model</dt><dd>{agent.model || 'Not recorded'}</dd></div><div><dt>Enabled skills</dt><dd>{agent.skillVersion}</dd></div><div><dt>Saved run configuration</dt><dd>{agent.configurationReference ? `${agent.configurationReference.id} · v${agent.configurationReference.version}` : 'No settings for this agent are included in the selected run'}</dd></div></dl>{configSaved && snapshot && agent.configurationReference && <SavedRecord snapshot={snapshot} reference={agent.configurationReference} />}{agent.configurationReference && !configSaved && <p className="tiny-note">The exact agent settings are not included in this saved view. The model and skills shown above come from the current setup.</p>}</details></div>; })}{kind === 'followup' && selectedAgents.filter(id => !agents.some(agent => agent.id === id)).map(id => <p key={id}>Agent {id} from the earlier run is not listed in the current setup. That agent remains selected; the request may be rejected if the agent is unavailable. <button type="button" className="inline-link" onClick={() => { setSelectedAgents(current => current.filter(peer => peer !== id)); setRequiredAgents(current => current.filter(peer => peer !== id)); revise(); }}>Remove from this follow-up</button></p>)}</fieldset>
      <details className="inline-detail"><summary>Research settings</summary><dl className="detail-list"><div><dt>Source retrieval routes</dt><dd>{retrievalRoutes.length ? retrievalRoutes.join(', ') : 'None: use pasted or previously saved evidence.'}</dd></div><div><dt>Workflow version</dt><dd>{workflowVersion || 'Not recorded'}</dd></div></dl></details>
      <button className="primary-button submit-button" type="submit" disabled={submitting || (!pendingRequest && (!question.trim() || !selectedAgents.length || (kind === 'start' && !evidenceText.trim() && retrievalRoutes.length === 0) || (kind === 'followup' && (!selectedTarget || followupRunId !== runId))))}>{submitting ? 'Submitting…' : pendingRequest ? 'Retry same request' : kind === 'start' ? 'Start research' : 'Submit follow-up'}</button>
    </form>
    {error && <div className="state-card" role="alert"><strong>{errorKind === 'view_unavailable' ? 'Saved view unavailable' : 'Request response unconfirmed'}</strong><p>{errorKind === 'view_unavailable' ? 'The service returned the saved receipt below, but Atlas could not load the current run view.' : 'The service did not return a receipt. This request may already have been accepted. Check Saved runs; if needed, Retry same request keeps its saved request ID.'}</p>{errorKind === 'unconfirmed' && pendingRequest && <p>Saved request ID: {pendingRequest.requestId}</p>}<details><summary>Service detail</summary><p>{error}</p></details></div>}
    {draftWarning && <p role="alert">{draftWarning}</p>}
    {receipt && <div className="receipt-card" role="status"><span className="eyebrow">Request confirmation</span><strong>Request accepted for run {receipt.runId}</strong><p>Status when request was accepted: {receipt.status ?? 'queued'}.</p><p>Task {receipt.taskId} · request {receipt.requestId}{receipt.reused ? ' · existing request reused' : ''}</p>{submittedCredential?.requestId === receipt.requestId && <p>Requested model account profile: {submittedCredential.label}.</p>}<p>Starting agents: {receiptAgentIds.map(id => agents.find(agent => agent.id === id)?.name ?? id).join(', ')}. Contributions required before this request is complete: {receiptRequiredAgentIds.length ? receiptRequiredAgentIds.map(id => agents.find(agent => agent.id === id)?.name ?? id).join(', ') : 'none selected'}. Actual work appears in Activity and Details as the run updates.</p></div>}
  </div>;
}
