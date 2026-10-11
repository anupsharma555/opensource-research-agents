/* Request authenticated live host state and commands through same-origin routes.
   A launch token in the URL fragment is exchanged once for an HttpOnly session
   cookie before any saved state or provider-backed command is requested. */
import type { LiveState } from './generated/live-state';
import type { LiveStartRequest } from './generated/live-start';
import type { LiveFollowupRequest } from './generated/live-followup';
import type { LiveReceipt } from './generated/live-receipt';
export type { LiveState, LiveAgent, LiveRunSummary } from './generated/live-state';
export type { LiveStartRequest } from './generated/live-start';
export type { LiveFollowupRequest } from './generated/live-followup';
export type { LiveReceipt } from './generated/live-receipt';

let sessionPromise: Promise<void> | null = null;

export function bootstrapLiveSession(): Promise<void> {
  const token = new URLSearchParams(window.location.hash.slice(1)).get('token');
  if (!token) return Promise.resolve();
  if (sessionPromise) return sessionPromise;
  sessionPromise = (async () => {
    const response = await fetch('/api/live/session', { method: 'POST', credentials: 'same-origin', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token }) });
    if (!response.ok) {
      let message = `Local launch session failed (${response.status}).`;
      try {
        const body = await response.json() as { error?: string };
        if (body.error) message = body.error;
      } catch { /* Keep a status-only error if the server did not return JSON. */ }
      throw new Error(message);
    }
    if (new URLSearchParams(window.location.hash.slice(1)).get('token') === token) {
      window.history.replaceState(window.history.state, '', `${window.location.pathname}${window.location.search}`);
    }
  })().finally(() => { sessionPromise = null; });
  return sessionPromise;
}

async function request<T>(path: string, init?: RequestInit): Promise<T> {
  await bootstrapLiveSession();
  const response = await fetch(path, { credentials: 'same-origin', ...init });
  if (!response.ok) {
    let message = `Live service returned ${response.status}.`;
    try {
      const body = await response.json() as { error?: string };
      if (body.error) message = body.error;
    } catch { /* Preserve the HTTP status when the response is not JSON. */ }
    throw new Error(message);
  }
  return response.json() as Promise<T>;
}

export function getLiveState(runId?: string | null): Promise<LiveState> {
  return request<LiveState>(`/api/live/state${runId ? `?runId=${encodeURIComponent(runId)}` : ''}`);
}

export function startLiveRun(body: LiveStartRequest): Promise<LiveReceipt> {
  return request<LiveReceipt>('/api/live/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
}

export function followUpLiveRun(body: LiveFollowupRequest): Promise<LiveReceipt> {
  return request<LiveReceipt>('/api/live/followups', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
}

export function getLiveCalls(runId: string, attemptId: string): Promise<{ records: unknown[] }> {
  return request<{ records: unknown[] }>(`/api/live/calls?runId=${encodeURIComponent(runId)}&attemptId=${encodeURIComponent(attemptId)}`);
}

export interface LiveReasoning {
  runId: string;
  callId: string;
  agentId: string;
  attemptId: string;
  status: 'available' | 'unavailable';
  text?: string | null;
  origin?: 'provider_returned';
  redacted?: boolean;
  reason?: string;
}

export function getLiveReasoning(runId: string, callId: string): Promise<LiveReasoning> {
  return request<LiveReasoning>(`/api/live/reasoning?runId=${encodeURIComponent(runId)}&callId=${encodeURIComponent(callId)}`, { cache: 'no-store' });
}

export type PeerRecordReference = { kind: 'mail_message' | 'mail_answer'; id: string; version: number };

export interface LivePeerRecord {
  runId: string;
  originRunId?: string;
  reference: PeerRecordReference;
  senderId?: string | null;
  recipientId?: string | null;
  requestId?: string | null;
  createdAt?: string | null;
  deliveredAt?: string | null;
  acknowledgedAt?: string | null;
  partial?: boolean | null;
  status: 'available' | 'unavailable';
  content?: unknown;
  contentReference?: { kind: string; id: string; version: number } | null;
  redacted?: boolean;
  reason?: string | null;
}

export async function getLivePeerRecord(runId: string, ref: PeerRecordReference): Promise<LivePeerRecord> {
  const query = new URLSearchParams({ runId, kind: ref.kind, id: ref.id, version: String(ref.version) });
  await bootstrapLiveSession();
  const response = await fetch(`/api/live/peer-record?${query}`, { credentials: 'same-origin', cache: 'no-store' });
  if (!response.ok) throw new Error(`Exact peer content request failed (${response.status}).`);
  return response.json() as Promise<LivePeerRecord>;
}
