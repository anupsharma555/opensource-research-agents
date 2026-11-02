/* Subscribe to content-free host invalidations and retain polling as a fallback.
   A reconnect always refreshes the complete authorized projection. Bursts are
   coalesced, and an update during a read schedules a subsequent read. */
import { useEffect, useState } from 'react';
import type { QueryClient, QueryKey } from '@tanstack/react-query';

export function useLiveUpdates(client: QueryClient, key: QueryKey, enabled: boolean) {
  const [mode, setMode] = useState<'push' | 'polling'>('polling');
  const serializedKey = JSON.stringify(key);
  useEffect(() => {
    if (!enabled || typeof EventSource === 'undefined') return;
    const queryKey: QueryKey = JSON.parse(serializedKey);
    let active = true;
    let busy = false;
    let dirty = false;
    let timer: ReturnType<typeof setTimeout> | undefined;
    let reconnectTimer: ReturnType<typeof setTimeout> | undefined;
    let events: EventSource | null = null;
    const refresh = async () => {
      if (busy || !active) return;
      busy = true;
      while (dirty && active) {
        dirty = false;
        await client.invalidateQueries({ queryKey, exact: true }, { cancelRefetch: false });
      }
      busy = false;
    };
    const changed = () => {
      dirty = true;
      if (timer !== undefined) clearTimeout(timer);
      timer = setTimeout(() => { void refresh(); }, 75);
    };
    const connect = () => {
      if (!active) return;
      const source = new EventSource('/api/live/updates', { withCredentials: true });
      events = source;
      source.onopen = () => { if (active) setMode('push'); };
      source.onerror = () => {
        if (!active) return;
        setMode('polling');
        // Chromium closes streams after some transient HTTP errors (e.g. 503).
        // Only advertised hosts reach this hook. Reopen CLOSED streams; leave
        // CONNECTING streams to the browser's own reconnect implementation.
        if (source.readyState === EventSource.CLOSED && reconnectTimer === undefined) {
          reconnectTimer = setTimeout(() => { reconnectTimer = undefined; connect(); }, 2000);
        }
      };
      source.addEventListener('invalidate', changed);
    };
    connect();
    const visible = () => { if (document.visibilityState === 'visible') changed(); };
    document.addEventListener('visibilitychange', visible);
    window.addEventListener('focus', changed);
    return () => {
      active = false;
      if (timer !== undefined) clearTimeout(timer);
      if (reconnectTimer !== undefined) clearTimeout(reconnectTimer);
      events?.close();
      document.removeEventListener('visibilitychange', visible);
      window.removeEventListener('focus', changed);
    };
  }, [client, serializedKey, enabled]);
  return enabled ? mode : 'polling';
}
