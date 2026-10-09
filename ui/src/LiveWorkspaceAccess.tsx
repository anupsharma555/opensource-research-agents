/* Return from a saved demonstration to the explicitly launched live workspace.
   Navigation opens the requested view; it never starts a research task or host. */
import { useState } from 'react';
import { invoke, isTauri } from '@tauri-apps/api/core';

export function LiveWorkspaceAccess({ view }: { view: 'request' | 'history' }) {
  const [error, setError] = useState<string | null>(null);
  const localHost = window.location.protocol === 'http:' && window.location.hostname === '127.0.0.1' && !!window.location.port;
  const title = view === 'request' ? 'Start or follow up' : 'Previous live tasks';
  return <section className="panel" aria-label="Live workspace access"><h3>{title}</h3><p>New tasks and saved live runs are in the live workspace. Opening it does not submit a task. This recorded review remains available separately.</p>
    {localHost ? <a className="primary-button" href={`/live?view=${view}`}>Open live workspace</a> : isTauri() ? <button className="primary-button" onClick={async () => {
      setError(null);
      try { await invoke('open_live_workspace', { view }); }
      catch (cause) { setError(typeof cause === 'string' ? cause : 'The live workspace could not be opened.'); }
    }}>Open live workspace</button> : <p>Open the local live app to access these tasks.</p>}
    {error && <p role="alert">{error}</p>}
    <details className="inline-detail"><summary>How to reopen a local live workspace</summary><p>Use the same saved state directory as your existing host:</p><pre>scripts/dev live open --state-dir &lt;saved-state-directory&gt;</pre><p>This reopens the viewer without starting a new research run.</p></details>
  </section>;
}
