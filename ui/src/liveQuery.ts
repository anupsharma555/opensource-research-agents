/* Own one viewer's in-memory server data and content-free read measurements.
   Workspace and session boundaries are explicit; no provider calls or payloads
   are persisted here. A viewer read can be cancelled without cancelling work. */
import { QueryClient, queryOptions } from '@tanstack/react-query';
import { getLiveState, LiveServiceError } from './live';

export type LiveScope = { origin: string; session: string; workspaceId: string | null };
export type ReadMetrics = { reads: number; failures: number; cancellations: number; lastDurationMs: number | null; totalDurationMs: number; updatesSupported: boolean; commands: number; commandErrors: number; lastCommandDurationMs: number | null };
export const emptyMetrics = (): ReadMetrics => ({ reads: 0, failures: 0, cancellations: 0, lastDurationMs: null, totalDurationMs: 0, updatesSupported: false, commands: 0, commandErrors: 0, lastCommandDurationMs: null });

export function createLiveQueryClient() {
  return new QueryClient({ defaultOptions: {
    queries: { retry: false, networkMode: 'always', staleTime: 0, gcTime: 60000, refetchOnWindowFocus: false, refetchOnReconnect: false },
    mutations: { retry: false, networkMode: 'always' },
  } });
}

export function liveStateOptions(scope: LiveScope, runId: string | null, metrics?: ReadMetrics) {
  return queryOptions({
    queryKey: ['atlas-live', scope.origin, scope.session, scope.workspaceId, runId] as const,
    queryFn: async ({ signal }) => {
      const started = performance.now();
      if (metrics) metrics.reads += 1;
      try {
        const state = await getLiveState(runId, signal, response => {
          if (metrics) metrics.updatesSupported = response.headers.get('X-Atlas-Updates') === 'sqlite-invalidation-v1';
        });
        if (scope.workspaceId !== null && state.workspaceId !== scope.workspaceId) {
          throw new LiveServiceError('The connected workspace changed. Reopen the selected workspace.', 409, 'workspace_changed');
        }
        if (runId && state.runId !== runId) throw new LiveServiceError('The host returned a different run.', 409, 'identity');
        return state;
      } catch (error) {
        if (metrics) {
          if (signal.aborted) metrics.cancellations += 1;
          else metrics.failures += 1;
        }
        throw error;
      } finally {
        if (metrics) {
          metrics.lastDurationMs = performance.now() - started;
          metrics.totalDurationMs += metrics.lastDurationMs;
        }
      }
    },
  });
}
