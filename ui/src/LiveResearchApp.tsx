/* Connect the local live service to the existing research review screens.
   Keep durable run state fresh by polling its exact run ID, and surface service
   failures without substituting a scripted sample. */
import { useEffect, useRef, useState } from 'react';
import { ResearchApp } from './App';
import { LiveRequestForm } from './LiveRequestForm';
import { followUpLiveRun, getLiveState, startLiveRun } from './live';
import type { LiveFollowupRequest, LiveReceipt, LiveStartRequest, LiveState } from './live';

export function LiveResearchApp() {
  const requestedView = new URLSearchParams(window.location.search).get('view');
  const initialScreen = requestedView === 'request' || requestedView === 'history' ? requestedView : 'overview';
  const [runId, setRunId] = useState<string | null>(() => new URLSearchParams(window.location.search).get('runId'));
  const [state, setState] = useState<LiveState | null>(null);
  const [lastCheckedAt, setLastCheckedAt] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const [receipt, setReceipt] = useState<LiveReceipt | null>(null);
  const [receiptAgentIds, setReceiptAgentIds] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const readGeneration = useRef(0);

  useEffect(() => {
    let active = true;
    let timer: number | undefined;
    async function refresh() {
      const generation = ++readGeneration.current;
      try {
        const next = await getLiveState(runId);
        if (!active || generation !== readGeneration.current) return;
        setState(next);
        setLastCheckedAt(new Date().toISOString());
        setLoadError(null);
        if (!runId && next.runId) {
          setRunId(next.runId);
          const url = new URL(window.location.href);
          url.searchParams.set('runId', next.runId);
          window.history.replaceState(null, '', url);
        }
      } catch (cause) {
        if (active && generation === readGeneration.current) setLoadError(cause instanceof Error ? cause.message : 'Could not load saved live state.');
      } finally {
        if (active) timer = window.setTimeout(() => { void refresh(); }, 1500);
      }
    }
    void refresh();
    return () => { active = false; if (timer !== undefined) window.clearTimeout(timer); };
  }, [runId]);

  async function submit(request: LiveStartRequest | LiveFollowupRequest) {
    readGeneration.current += 1;
    setSubmitting(true);
    setRequestError(null);
    let stateReadGeneration: number | null = null;
    try {
      const next = 'evidenceText' in request ? await startLiveRun(request) : await followUpLiveRun(request);
      setReceipt(next);
      setReceiptAgentIds(request.agentIds);
      setRunId(next.runId);
      const url = new URL(window.location.href);
      url.searchParams.set('runId', next.runId);
      window.history.replaceState(null, '', url);
      const generation = ++readGeneration.current;
      stateReadGeneration = generation;
      const saved = await getLiveState(next.runId);
      if (generation === readGeneration.current) {
        setState(saved);
        setLastCheckedAt(new Date().toISOString());
        setLoadError(null);
      }
    } catch (cause) {
      if (stateReadGeneration === null || stateReadGeneration === readGeneration.current) {
        setRequestError(cause instanceof Error ? cause.message : 'The live request failed.');
      }
    } finally {
      setSubmitting(false);
    }
  }

  function selectRun(id: string) {
    if (id === runId) return;
    readGeneration.current += 1;
    setState(null);
    setLoadError(null);
    setRunId(id);
    setReceipt(null);
    const url = new URL(window.location.href);
    url.searchParams.set('runId', id);
    window.history.replaceState(null, '', url);
  }

  if (!state) return <div className="startup-state" role={loadError ? 'alert' : 'status'}><span className="sample-tag">LIVE RESEARCH</span><h1>{loadError ? 'Live service unavailable' : 'Loading saved live state'}</h1><p>{loadError ?? 'Connecting to the local research host.'}</p></div>;
  if (!state.enabled) return <div className="startup-state" role="alert"><h1>Live research unavailable</h1><p>{state.error || 'The local research host is disabled.'}</p></div>;

  const form = <LiveRequestForm agents={state.agents} snapshot={state.snapshot ?? null} runId={state.runId ?? runId} receipt={receipt} receiptAgentIds={receiptAgentIds} submitting={submitting} error={requestError} onSubmit={submit} />;
  if (!state.snapshot) return <div className="live-empty-shell"><header><span className="sample-tag">LIVE RESEARCH</span><h1>Start a research task</h1><p>Model: {state.model}. Supplied evidence only; fresh web search is unavailable.</p></header>{loadError && <div role="alert">Connection: {loadError}</div>}{state.error && <div role="status">Run status: {state.error}</div>}{form}{state.runs.length > 0 && <section className="panel"><h2>Saved runs</h2>{state.runs.map(run => <button className="outline-button" key={run.id} onClick={() => selectRun(run.id)}>{run.question} · {run.status}</button>)}</section>}</div>;
  return <ResearchApp initialScreen={initialScreen} snapshot={state.snapshot} comparisonSnapshot={state.comparisonSnapshot ?? undefined} live={{ runId: state.runId ?? runId, model: state.model, runs: state.runs, error: state.error ?? null, connectionError: loadError, lastCheckedAt, form, requestedAgentIds: state.runs.find(run => run.id === (state.runId ?? runId))?.agentIds ?? (receipt?.runId === (state.runId ?? runId) ? receiptAgentIds : null), onSelectRun: selectRun }} />;
}
