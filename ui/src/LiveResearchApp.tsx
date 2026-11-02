/* Compose the live viewer from scoped server data, URL navigation and explicit
   command outcomes. The backend remains authoritative for research state. */
import { useCallback, useEffect, useMemo, useReducer, useRef, useState } from 'react';
import { BrowserRouter, useSearchParams } from 'react-router';
import { QueryClientProvider, useQuery, useQueryClient } from '@tanstack/react-query';
import { ResearchApp } from './App';
import type { Screen } from './App';
import { LiveRequestForm } from './LiveRequestForm';
import { SavedRuns } from './SavedRuns';
import { followUpLiveRun, startLiveRun, LiveServiceError } from './live';
import type { LiveFollowupRequest, LiveStartRequest } from './live';
import { createLiveQueryClient, emptyMetrics, liveStateOptions } from './liveQuery';
import { initialSubmission, submissionReducer } from './liveSubmission';
import { useLiveUpdates } from './useLiveUpdates';

const liveViews = new Set<Screen>(['overview', 'activity', 'communication', 'work', 'evidence', 'diagnostics', 'request', 'compare', 'history']);

export function LiveResearchApp() {
  const [client] = useState(createLiveQueryClient);
  const [session] = useState(() => crypto.randomUUID());
  useEffect(() => () => { client.clear(); }, [client]);
  return <QueryClientProvider client={client}><BrowserRouter><LiveSession session={session} /></BrowserRouter></QueryClientProvider>;
}

function LiveSession({ session }: { session: string }) {
  const client = useQueryClient();
  const [params, setParams] = useSearchParams();
  const runId = params.get('runId');
  const view = params.get('view');
  const initialScreen: Screen = liveViews.has(view as Screen) ? view as Screen : 'overview';
  const [workspaceId, setWorkspaceId] = useState<string | null>(null);
  const [metrics] = useState(emptyMetrics);
  const [blockedError, setBlockedError] = useState<Error | null>(null);
  const options = useMemo(() => liveStateOptions({ origin: window.location.origin, session, workspaceId }, runId, metrics), [session, workspaceId, runId, metrics]);
  const [submission, dispatch] = useReducer(submissionReducer, initialSubmission);
  const commandGeneration = useRef(0);
  const mode = useLiveUpdates(client, options.queryKey, workspaceId !== null && metrics.updatesSupported && !blockedError);
  const query = useQuery({ ...options, enabled: !blockedError, refetchInterval: q => blockedError || q.state.error instanceof LiveServiceError && [401, 403, 409].includes(q.state.error.status) ? false : mode === 'push' ? 5000 : 1500, refetchOnMount: 'always' });
  const identityError = query.error instanceof LiveServiceError && [401, 403, 409].includes(query.error.status);
  const state = blockedError || identityError ? null : query.data ?? null;
  const loadError = blockedError?.message ?? query.error?.message ?? null;
  const lastCheckedAt = query.dataUpdatedAt ? new Date(query.dataUpdatedAt).toISOString() : null;
  const { submitting, receipt, error: requestError, errorKind: requestErrorKind, agentIds: receiptAgentIds, requiredAgentIds: receiptRequiredAgentIds } = submission;

  useEffect(() => {
    if (!identityError || !query.error) return;
    setBlockedError(query.error);
    commandGeneration.current += 1;
    client.clear();
  }, [client, identityError, query.error]);

  useEffect(() => {
    if (view !== initialScreen) setParams(current => { current.set('view', initialScreen); return current; }, { replace: true });
  }, [view, initialScreen, setParams]);

  // Bind the discovery response to its workspace key before subsequent reads.
  useEffect(() => {
    if (!state || workspaceId !== null || !state.workspaceId) return;
    const next = liveStateOptions({ origin: window.location.origin, session, workspaceId: state.workspaceId }, runId, metrics);
    client.setQueryData(next.queryKey, state, { updatedAt: query.dataUpdatedAt });
    setWorkspaceId(state.workspaceId);
  }, [client, state, workspaceId, session, runId, metrics, query.dataUpdatedAt]);

  useEffect(() => {
    if (!state || runId || !state.runId) return;
    const next = liveStateOptions({ origin: window.location.origin, session, workspaceId }, state.runId, metrics);
    client.setQueryData(next.queryKey, state, { updatedAt: query.dataUpdatedAt });
    setParams(current => { current.set('runId', state.runId!); return current; }, { replace: true });
  }, [client, state, runId, session, workspaceId, metrics, query.dataUpdatedAt, setParams]);

  const syncScreen = useCallback((screen: Screen) => {
    setParams(current => { if (current.get('view') === screen) return current; const next = new URLSearchParams(current); next.set('view', screen); return next; });
  }, [setParams]);

  async function submit(request: LiveStartRequest | LiveFollowupRequest) {
    const generation = ++commandGeneration.current;
    dispatch({ type: 'begin' });
    await client.cancelQueries({ queryKey: ['atlas-live'] });
    const commandStarted = performance.now();
    let accepted = false;
    metrics.commands += 1;
    try {
      const next = 'parentRunId' in request ? await followUpLiveRun(request) : await startLiveRun(request);
      accepted = true;
      metrics.lastCommandDurationMs = performance.now() - commandStarted;
      if (generation !== commandGeneration.current) return;
      dispatch({ type: 'accepted', receipt: next, agentIds: request.agentIds, requiredAgentIds: request.requiredAgentIds ?? [] });
      const savedOptions = liveStateOptions({ origin: window.location.origin, session, workspaceId }, next.runId, metrics);
      const saved = await client.fetchQuery(savedOptions);
      if (generation !== commandGeneration.current) return;
      client.setQueryData(savedOptions.queryKey, saved);
      setParams(current => { current.set('runId', next.runId); return current; }, { replace: true });
    } catch (cause) {
      if (!accepted) {
        metrics.commandErrors += 1;
        metrics.lastCommandDurationMs = performance.now() - commandStarted;
      }
      if (generation === commandGeneration.current) dispatch({ type: 'failed', message: cause instanceof Error ? cause.message : 'Could not read the service response.' });
    } finally { if (generation === commandGeneration.current) dispatch({ type: 'settled' }); }
  }

  function selectRun(id: string) {
    commandGeneration.current += 1;
    dispatch({ type: 'reset' });
    setParams({ view: 'overview', runId: id });
  }

  if (!state) return <div className="startup-state" role={loadError ? 'alert' : 'status'}><span className="sample-tag">LIVE RESEARCH</span><h1>{loadError ? 'Live service unavailable' : 'Loading saved live state'}</h1><p>{loadError ?? 'Connecting to the local research host.'}</p></div>;
  if (!state.enabled) return <div className="startup-state" role="alert"><h1>Live research unavailable</h1><p>{state.error || 'The local research host is disabled.'}</p></div>;

  const agentNames = Object.fromEntries([...state.agents, ...(state.snapshot?.agents ?? [])].map(agent => [agent.id, agent.name]));
  const selectedRun = state.runs.find(run => run.id === (state.runId ?? runId));
  const form = <LiveRequestForm key={state.workspaceId || 'unselected'} agents={state.agents} snapshot={state.snapshot ?? null} runId={state.runId ?? runId} workspaceId={state.workspaceId} parentAgentIds={selectedRun?.agentIds ?? null} parentRequiredAgentIds={selectedRun?.requiredAgentIds ?? null} retrievalRoutes={state.retrievalRoutes ?? []} workflowVersion={state.workflowVersion} receipt={receipt} receiptAgentIds={receiptAgentIds} receiptRequiredAgentIds={receiptRequiredAgentIds} submitting={submitting} error={requestError} errorKind={requestErrorKind} onSubmit={submit} />;
  if (!state.snapshot) return <div className="live-empty-shell"><header><span className="sample-tag">LIVE RESEARCH</span><h1>{initialScreen === 'history' ? 'Saved runs' : 'Start a research task'}</h1><p>Model: {state.model}. {state.retrievalRoutes?.length ? `Enabled retrieval: ${state.retrievalRoutes.join(', ')}.` : 'Supplied evidence only; fresh web search is unavailable.'}</p></header>{loadError && <div role="alert">Connection: {loadError}</div>}{state.error && <div role="status">Run status: {state.error}</div>}{initialScreen === 'history' ? <SavedRuns key={state.workspaceId || 'legacy'} runs={state.runs} selectedRunId={runId} workspaceId={state.workspaceId} workspaceLabel={state.workspaceLabel} agentNames={agentNames} onOpen={selectRun} /> : form}{initialScreen !== 'history' && state.runs.length > 0 && <SavedRuns key={state.workspaceId || 'legacy'} runs={state.runs} selectedRunId={runId} workspaceId={state.workspaceId} workspaceLabel={state.workspaceLabel} agentNames={agentNames} onOpen={selectRun} />}</div>;
  return <ResearchApp initialScreen={initialScreen} onScreenChange={syncScreen} snapshot={state.snapshot} comparisonSnapshot={state.comparisonSnapshot ?? undefined} live={{ runId: state.runId ?? runId, model: state.model, runs: state.runs, error: state.error ?? null, connectionError: loadError, lastCheckedAt, viewerMetrics: { ...metrics, mode }, form, workspaceId: state.workspaceId, workspaceLabel: state.workspaceLabel, retrievalRoutes: state.retrievalRoutes, agentNames, requestedAgentIds: selectedRun?.agentIds ?? (receipt?.runId === (state.runId ?? runId) ? receiptAgentIds : null), requiredAgentIds: selectedRun?.requiredAgentIds ?? (receipt?.runId === (state.runId ?? runId) ? receiptRequiredAgentIds : null), participationPolicyVersion: selectedRun?.participationPolicyVersion ?? null, onSelectRun: selectRun }} />;
}
