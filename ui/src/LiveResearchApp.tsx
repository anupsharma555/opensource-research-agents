/* Connect the local live service to the existing research review screens.
   Keep durable run state fresh by polling its exact run ID, and surface service
   failures without substituting a scripted sample. */
import { useCallback, useEffect, useRef, useState } from 'react';
import { ResearchApp } from './App';
import type { Screen } from './App';
import { LiveRequestForm } from './LiveRequestForm';
import { SavedRuns } from './SavedRuns';
import { followUpLiveRun, getLiveState, startLiveRun } from './live';
import type { LiveFollowupRequest, LiveReceipt, LiveStartRequest, LiveState } from './live';

const liveViews = new Set<Screen>(['overview', 'activity', 'communication', 'work', 'evidence', 'diagnostics', 'request', 'compare', 'history']);

function selectedView(): Screen {
  const value = new URLSearchParams(window.location.search).get('view');
  return liveViews.has(value as Screen) ? value as Screen : 'overview';
}

export function LiveResearchApp() {
  const initialScreen = selectedView();
  const [runId, setRunId] = useState<string | null>(() => new URLSearchParams(window.location.search).get('runId'));
  const [state, setState] = useState<LiveState | null>(null);
  const [lastCheckedAt, setLastCheckedAt] = useState<string | null>(null);
  const [loadError, setLoadError] = useState<string | null>(null);
  const [requestError, setRequestError] = useState<string | null>(null);
  const [requestErrorKind, setRequestErrorKind] = useState<'unconfirmed' | 'view_unavailable'>('unconfirmed');
  const [receipt, setReceipt] = useState<LiveReceipt | null>(null);
  const [receiptAgentIds, setReceiptAgentIds] = useState<string[]>([]);
  const [receiptRequiredAgentIds, setReceiptRequiredAgentIds] = useState<string[]>([]);
  const [submitting, setSubmitting] = useState(false);
  const readGeneration = useRef(0);
  const syncScreen = useCallback((screen: Screen) => {
    const url = new URL(window.location.href);
    if (url.searchParams.get('view') === screen) return;
    url.searchParams.set('view', screen);
    window.history.replaceState(null, '', url);
  }, []);

  useEffect(() => { syncScreen(initialScreen); }, [initialScreen, syncScreen]);

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
    setRequestErrorKind('unconfirmed');
    setReceipt(null);
    let stateReadGeneration: number | null = null;
    try {
      const next = 'parentRunId' in request ? await followUpLiveRun(request) : await startLiveRun(request);
      setReceipt(next);
      setReceiptAgentIds(request.agentIds);
      setReceiptRequiredAgentIds(request.requiredAgentIds ?? []);
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
        setRequestErrorKind(stateReadGeneration === null ? 'unconfirmed' : 'view_unavailable');
        setRequestError(cause instanceof Error ? cause.message : 'Could not read the service response.');
      }
    } finally {
      setSubmitting(false);
    }
  }

  function selectRun(id: string) {
    const url = new URL(window.location.href);
    url.searchParams.set('view', 'overview');
    url.searchParams.set('runId', id);
    window.history.replaceState(null, '', url);
    if (id === runId) return;
    readGeneration.current += 1;
    setState(null);
    setLoadError(null);
    setRunId(id);
    setReceipt(null);
  }

  if (!state) return <div className="startup-state" role={loadError ? 'alert' : 'status'}><span className="sample-tag">LIVE RESEARCH</span><h1>{loadError ? 'Live service unavailable' : 'Loading saved live state'}</h1><p>{loadError ?? 'Connecting to the local research host.'}</p></div>;
  if (!state.enabled) return <div className="startup-state" role="alert"><h1>Live research unavailable</h1><p>{state.error || 'The local research host is disabled.'}</p></div>;

  const agentNames = Object.fromEntries([...state.agents, ...(state.snapshot?.agents ?? [])].map(agent => [agent.id, agent.name]));
  const selectedRun = state.runs.find(run => run.id === (state.runId ?? runId));
  const form = <LiveRequestForm key={state.workspaceId || 'unselected'} agents={state.agents} snapshot={state.snapshot ?? null} runId={state.runId ?? runId} workspaceId={state.workspaceId} parentAgentIds={selectedRun?.agentIds ?? null} parentRequiredAgentIds={selectedRun?.requiredAgentIds ?? null} retrievalRoutes={state.retrievalRoutes ?? []} workflowVersion={state.workflowVersion} receipt={receipt} receiptAgentIds={receiptAgentIds} receiptRequiredAgentIds={receiptRequiredAgentIds} submitting={submitting} error={requestError} errorKind={requestErrorKind} onSubmit={submit} />;
  if (!state.snapshot) return <div className="live-empty-shell"><header><span className="sample-tag">LIVE RESEARCH</span><h1>{initialScreen === 'history' ? 'Saved runs' : 'Start a research task'}</h1><p>Model: {state.model}. {state.retrievalRoutes?.length ? `Enabled retrieval: ${state.retrievalRoutes.join(', ')}.` : 'Supplied evidence only; fresh web search is unavailable.'}</p></header>{loadError && <div role="alert">Connection: {loadError}</div>}{state.error && <div role="status">Run status: {state.error}</div>}{initialScreen === 'history' ? <SavedRuns key={state.workspaceId || 'legacy'} runs={state.runs} selectedRunId={runId} workspaceId={state.workspaceId} workspaceLabel={state.workspaceLabel} agentNames={agentNames} onOpen={selectRun} /> : form}{initialScreen !== 'history' && state.runs.length > 0 && <SavedRuns key={state.workspaceId || 'legacy'} runs={state.runs} selectedRunId={runId} workspaceId={state.workspaceId} workspaceLabel={state.workspaceLabel} agentNames={agentNames} onOpen={selectRun} />}</div>;
  return <ResearchApp initialScreen={initialScreen} onScreenChange={syncScreen} snapshot={state.snapshot} comparisonSnapshot={state.comparisonSnapshot ?? undefined} live={{ runId: state.runId ?? runId, model: state.model, runs: state.runs, error: state.error ?? null, connectionError: loadError, lastCheckedAt, form, workspaceId: state.workspaceId, workspaceLabel: state.workspaceLabel, retrievalRoutes: state.retrievalRoutes, agentNames, requestedAgentIds: selectedRun?.agentIds ?? (receipt?.runId === (state.runId ?? runId) ? receiptAgentIds : null), requiredAgentIds: selectedRun?.requiredAgentIds ?? (receipt?.runId === (state.runId ?? runId) ? receiptRequiredAgentIds : null), participationPolicyVersion: selectedRun?.participationPolicyVersion ?? null, onSelectRun: selectRun }} />;
}
