/* Exercise the real JSON/fetch/query boundary with synthetic HTTP responses.
   These tests prove cache isolation, cancellation and current data delivery;
   they do not establish provider or native-webview behavior. */
import { waitFor } from '@testing-library/react';
import { afterAll, afterEach, beforeAll, expect, it } from 'vitest';
import { http, HttpResponse, delay } from 'msw';
import { setupServer } from 'msw/node';
import { onlineManager } from '@tanstack/react-query';
import { createLiveQueryClient, emptyMetrics, liveStateOptions } from './liveQuery';
import { getLiveState } from './live';

const packet = (workspaceId = 'workspace-a') => ({ enabled: true, model: 'synthetic', agents: [], runs: [], workspaceId, runId: 'same-run' });
const server = setupServer();
beforeAll(() => server.listen({ onUnhandledRequest: 'error' }));
afterEach(() => { server.resetHandlers(); onlineManager.setOnline(true); });
afterAll(() => server.close());
const scope = { origin: 'http://localhost:3000', session: 'synthetic-session', workspaceId: 'workspace-a' };

it('coalesces concurrent readers and works when Internet connectivity is reported offline', async () => {
  let requests = 0;
  server.use(http.get('*/api/live/state', async () => { requests++; await delay(30); return HttpResponse.json(packet()); }));
  onlineManager.setOnline(false);
  const client = createLiveQueryClient();
  const metrics = emptyMetrics();
  try {
    const options = liveStateOptions(scope, 'same-run', metrics);
    const [a, b] = await Promise.all([client.fetchQuery(options), client.fetchQuery(options)]);
    expect(a).toBe(b);
    expect(requests).toBe(1);
    expect(metrics.reads).toBe(1);
    expect(metrics.lastDurationMs).toBeGreaterThan(0);
    expect(JSON.stringify(metrics)).not.toContain('workspace-a');
  } finally { client.clear(); }
});

it('isolates identical run IDs across workspaces and rejects a changed host identity', async () => {
  let workspace = 'workspace-a';
  server.use(http.get('*/api/live/state', () => HttpResponse.json(packet(workspace))));
  const client = createLiveQueryClient();
  try {
    const a = liveStateOptions(scope, 'same-run');
    expect((await client.fetchQuery(a)).workspaceId).toBe('workspace-a');
    workspace = 'workspace-b';
    await expect(client.fetchQuery(a)).rejects.toMatchObject({ kind: 'workspace_changed' });
    const b = liveStateOptions({ ...scope, workspaceId: workspace }, 'same-run');
    expect(client.getQueryData(b.queryKey)).toBeUndefined();
    expect((await client.fetchQuery(b)).workspaceId).toBe('workspace-b');
  } finally { client.clear(); }
});

it('rejects malformed JSON and incompatible records without caching them', async () => {
  server.use(http.get('*/api/live/state', () => HttpResponse.json({ enabled: 'yes', model: 'synthetic' })));
  await expect(getLiveState()).rejects.toMatchObject({ kind: 'schema' });
  server.use(http.get('*/api/live/state', () => new HttpResponse('{invalid', { headers: { 'Content-Type': 'application/json' } })));
  await expect(getLiveState()).rejects.toMatchObject({ kind: 'schema' });
});

it('does not treat the same snapshot identity as permission to reuse a stale projection', async () => {
  let error: string | null = null;
  server.use(http.get('*/api/live/state', () => HttpResponse.json({ ...packet(), error })));
  const client = createLiveQueryClient();
  try {
    const options = liveStateOptions(scope, 'same-run');
    expect((await client.fetchQuery(options)).error).toBeNull();
    error = 'Source access changed';
    expect((await client.fetchQuery(options)).error).toBe('Source access changed');
  } finally { client.clear(); }
});

it('aborts an obsolete read without issuing a research cancellation command', async () => {
  let started!: () => void;
  const entered = new Promise<void>(resolve => { started = resolve; });
  server.use(http.get('*/api/live/state', async () => { started(); await delay(150); return HttpResponse.json(packet()); }));
  const client = createLiveQueryClient();
  const metrics = emptyMetrics();
  const options = liveStateOptions(scope, 'same-run', metrics);
  try {
    const read = client.fetchQuery(options).catch(() => null);
    await entered;
    await client.cancelQueries({ queryKey: options.queryKey });
    await read;
    expect(client.getQueryData(options.queryKey)).toBeUndefined();
    await waitFor(() => expect(metrics.cancellations).toBe(1));
  } finally { client.clear(); }
});
