/* Return from a saved demonstration to the explicitly launched live workspace.
   Navigation opens the requested view; it never starts a research task or host. */
import { useEffect, useState } from 'react';
import { invoke, isTauri } from '@tauri-apps/api/core';

export function LiveWorkspaceAccess({ view }: { view: 'request' | 'history' }) {
  const [error, setError] = useState<string | null>(null);
  const [directory, setDirectory] = useState('');
  const returnRunId = new URLSearchParams(window.location.search).get('returnRunId');
  const runId = returnRunId && returnRunId.length <= 256 ? returnRunId : null;
  const localHost = window.location.protocol === 'http:' && window.location.hostname === '127.0.0.1' && !!window.location.port;
  const bundledNative = isTauri() && (window.location.protocol === 'tauri:' || ((window.location.protocol === 'http:' || window.location.protocol === 'https:') && window.location.hostname === 'tauri.localhost'));
  const liveUrl = `/live?view=${view}${runId ? `&runId=${encodeURIComponent(runId)}` : ''}`;
  useEffect(() => {
    if (bundledNative) void invoke<string | null>('get_live_workspace_selection').then(value => { if (value) setDirectory(value); }).catch(() => {});
  }, [bundledNative]);
  const title = view === 'request' ? 'Start or follow up' : 'Previous live tasks';
  return <section className="panel" aria-label="Live workspace access"><h3>{title}</h3><p>New tasks and saved live runs belong to a selected local workspace. Opening it only reads saved state; it does not submit new research. This recorded review remains available separately.</p>
    {localHost ? <a className="primary-button" href={liveUrl}>Open connected workspace</a> : bundledNative ? <><label className="field-label" htmlFor="saved-workspace-directory">Saved local workspace directory</label><input id="saved-workspace-directory" type="text" value={directory} onChange={event => setDirectory(event.target.value)} placeholder="Choose the directory used when starting the local host" autoComplete="off" /><p className="tiny-note">Atlas saves this directory selection locally. The viewer session and provider credential are not stored in this field.</p><div className="request-links"><button className="primary-button" onClick={async () => {
      setError(null);
      try { await invoke('connect_live_workspace', { stateDir: directory, view, runId }); }
      catch (cause) { setError(typeof cause === 'string' ? cause : 'The live workspace could not be opened.'); }
    }} disabled={!directory.trim()}>Connect selected workspace</button><button className="outline-button" onClick={async () => { setError(null); try { await invoke('open_live_workspace', { view, runId }); } catch (cause) { setError(typeof cause === 'string' ? cause : 'The selected workspace could not be reopened.'); } }}>Retry saved selection</button></div></> : <p>Open the local live app to access these tasks.</p>}
    {error && <p role="alert">{error}</p>}
    <details className="inline-detail"><summary>How to reopen a local live workspace</summary><p>Use the same saved state directory as your existing host:</p><pre>scripts/dev live open --state-dir &lt;saved-state-directory&gt;</pre><p>This reopens the viewer without starting a new research run.</p></details>
  </section>;
}
