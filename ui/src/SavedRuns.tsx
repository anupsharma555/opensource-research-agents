/* Search and sort the read-only run index using saved server fields. Unknown
   legacy dates stay unknown; viewer refreshes never become research progress. */
import { useEffect, useMemo, useState } from 'react';
import type { LiveRunSummary } from './live';
import { formatActivityTimestamp } from './ActivityRecordDetails';

type Run = LiveRunSummary & {
  title?: string | null;
  createdAt?: string | null;
  lastProgressAt?: string | null;
  completedAt?: string | null;
  continuingTaskIds?: string[] | null;
  waitingReason?: string | null;
};

const dateValue = (value?: string | null) => value && Number.isFinite(Date.parse(value)) ? Date.parse(value) : null;
const localDate = (value?: string | null) => {
  const time = dateValue(value);
  if (time === null) return null;
  const day = new Date(time);
  return `${day.getFullYear()}-${String(day.getMonth() + 1).padStart(2, '0')}-${String(day.getDate()).padStart(2, '0')}`;
};
const runTitle = (run: Run) => {
  const question = run.question.replace(/\s+/g, ' ').trim();
  const title = run.title?.trim();
  // Expand mechanically shortened prefixes so the distinguishing question is
  // still available to the two-line heading, hover text and screen reader.
  return title && !(title.endsWith('…') && question.startsWith(title.slice(0, -1))) ? title : question;
};
const timestamp = (value?: string | null) => value ? <time dateTime={value} title={value}>{formatActivityTimestamp(value)}</time> : 'Not recorded';
const waitingSummary = (reason: string) => {
  const reasons = [...new Set(reason.split(';').map(value => value.trim()).filter(Boolean))];
  const distinctReason = reasons.join('; ') || reason;
  const hold = reasons.length === 1 && reasons[0].match(/^(Primary|Backup) credential paused by operator$/);
  if (hold) return `Research calls are paused for the ${hold[1]} profile. Saved work is retained.`;
  if (distinctReason.startsWith('ContextOverflow:')) return 'The next model input is too large. Saved work is preserved while this task awaits a context repair.';
  if (distinctReason === 'pending_model has failed external effect') return 'A model step failed. Open the run to inspect its saved response or operation record before deciding how to repair this branch.';
  if (distinctReason === 'pending_tool has failed external effect') return 'A tool step failed. Open the run to inspect its saved tool outcome before deciding how to repair this branch.';
  if (/^(?:StateError|ValueError|KeyError|TypeError): this branch needs local repair$/.test(distinctReason)) return 'The host could not complete a local step. Saved work remains while this branch needs repair.';
  return distinctReason;
};

type Filters = { search: string; status: string; agent: string; date: string; sort: 'created' | 'progress' };
const defaultFilters: Filters = { search: '', status: 'all', agent: 'all', date: '', sort: 'created' };

export function SavedRuns({ runs, selectedRunId, workspaceId, workspaceLabel, agentNames = {}, onOpen }: {
  runs: Run[];
  selectedRunId: string | null;
  workspaceId?: string | null;
  workspaceLabel?: string | null;
  agentNames?: Record<string, string>;
  onOpen: (id: string) => void;
}) {
  const storageKey = workspaceId ? `atlas-saved-runs:${workspaceId}` : null;
  const [filters, setFilters] = useState<Filters>(() => {
    if (!storageKey) return defaultFilters;
    try {
      const value = JSON.parse(window.sessionStorage.getItem(storageKey) || 'null') as Partial<Filters> | null;
      return value && typeof value.search === 'string' && typeof value.date === 'string' && (value.sort === 'created' || value.sort === 'progress')
        ? { ...defaultFilters, ...value } : defaultFilters;
    } catch { return defaultFilters; }
  });
  const { search, status, agent, date, sort } = filters;
  const setFilter = (value: Partial<Filters>) => setFilters(current => ({ ...current, ...value }));
  useEffect(() => {
    if (!storageKey) return;
    try { window.sessionStorage.setItem(storageKey, JSON.stringify(filters)); } catch { /* Filtering still works in memory. */ }
  }, [storageKey, filters]);
  useEffect(() => {
    if (!storageKey) return;
    let position: number;
    try { position = Number(window.sessionStorage.getItem(`${storageKey}:scroll`)); } catch { return; }
    if (!Number.isFinite(position) || position <= 0) return;
    const frame = window.requestAnimationFrame(() => window.scrollTo(0, position));
    return () => window.cancelAnimationFrame(frame);
  }, [storageKey]);
  const openRun = (id: string) => {
    if (storageKey) try { window.sessionStorage.setItem(`${storageKey}:scroll`, String(window.scrollY)); } catch { /* Keep navigation available. */ }
    onOpen(id);
  };
  const allStatuses = [...new Set(runs.map(run => run.status))].sort();
  const allAgents = [...new Set(runs.flatMap(run => run.agentIds ?? []))].sort();
  const name = (id: string, run?: Run) => run
    ? run.requestedAgentNames?.[id] || `Name not recorded (${id})`
    : runs.find(item => item.requestedAgentNames?.[id])?.requestedAgentNames?.[id]
      || agentNames[id] || `Identity unavailable (${id})`;
  const byId = new Map(runs.map(run => [run.id, run]));
  const visible = useMemo(() => runs.filter(run => {
    const query = search.trim().toLocaleLowerCase();
    const matchesText = !query || [run.question, run.title, run.id, run.parentRunId].some(value => value?.toLocaleLowerCase().includes(query));
    const matchesDate = !date || localDate(run.createdAt) === date;
    return matchesText && matchesDate && (status === 'all' || run.status === status) && (agent === 'all' || run.agentIds?.includes(agent));
  }).sort((a, b) => {
    const first = dateValue(sort === 'progress' ? a.lastProgressAt : a.createdAt);
    const second = dateValue(sort === 'progress' ? b.lastProgressAt : b.createdAt);
    if (first === null && second !== null) return 1;
    if (second === null && first !== null) return -1;
    return (second ?? 0) - (first ?? 0) || a.id.localeCompare(b.id);
  }), [runs, search, status, agent, date, sort]);

  return <section className="saved-runs" aria-label="Saved run history">
    <p className="screen-intro">Workspace: {workspaceLabel || 'selected local workspace (label unavailable)'}. These are its saved runs, separate from the recorded examples below. Times use your local timezone; hover a time for its exact saved value.</p>
    <div className="panel saved-run-filters">
      <label>Search question or run <input type="search" value={search} onChange={event => setFilter({ search: event.target.value })} /></label>
      <label>Created date <input type="date" aria-describedby="saved-run-date-hint" value={date} onChange={event => setFilter({ date: event.target.value })} /><span id="saved-run-date-hint">{date ? 'Filtering by this date' : 'Any date — no date filter applied'}</span></label>
      <label>Status <select value={status} onChange={event => setFilter({ status: event.target.value })}><option value="all">All statuses</option>{allStatuses.map(value => <option key={value}>{value}</option>)}</select></label>
      <label>Requested peer <select value={agent} onChange={event => setFilter({ agent: event.target.value })}><option value="all">All peers</option>{allAgents.map(value => <option key={value} value={value}>{name(value)}</option>)}</select></label>
      <label>Sort by <select value={sort} onChange={event => setFilter({ sort: event.target.value as 'created' | 'progress' })}><option value="created">Created, newest first</option><option value="progress">Last saved progress, newest first</option></select></label>
    </div>
    <div className="saved-run-filter-summary"><p className="tiny-note">Showing {visible.length} of {runs.length} saved runs. Unknown historical dates sort last; viewer polling does not change progress time.</p>{(search || date || status !== 'all' || agent !== 'all') && <button className="text-button" onClick={() => setFilter({ search: '', date: '', status: 'all', agent: 'all' })}>Clear filters</button>}</div>
    {runs.length === 0 ? <p>No run has been saved in this workspace yet.</p> : visible.length === 0 ? <p>No saved run matches these filters.</p> : <div className="communication-list">{visible.map(run => {
      const parent = run.parentRunId ? byId.get(run.parentRunId) : null;
      const scopeCompleted = run.status === 'completed' && (run.participationPolicyVersion != null && run.participationPolicyVersion >= 2 || run.continuingTaskIds != null);
      return <article className="panel saved-sample" key={run.id} aria-label={`Saved run ${runTitle(run)}`}>
        <div className="panel-heading"><div><span className="eyebrow">{run.parentRunId ? 'Follow-up run' : 'Original run'} · {scopeCompleted ? 'Requested scope complete' : run.status}</span><h3 title={runTitle(run)}>{runTitle(run)}</h3></div>{selectedRunId === run.id && <span className="status-pill">Open now</span>}</div>
        <dl className="saved-run-times"><div><dt>Created</dt><dd>{timestamp(run.createdAt)}</dd></div><div><dt>Last saved progress</dt><dd>{timestamp(run.lastProgressAt)}</dd></div>{run.completedAt && <div><dt>{scopeCompleted ? 'Requested scope completed' : 'Completed'}</dt><dd>{timestamp(run.completedAt)}</dd></div>}</dl>
        {run.parentRunId && <p>Follows {parent ? <button className="text-button" onClick={() => openRun(parent.id)}>{runTitle(parent)}</button> : 'a parent run not included in this list'}.</p>}
        {scopeCompleted && <p>{run.continuingTaskIds == null ? 'Continuing task status is not recorded in this history row. Open the run to inspect saved tasks.' : run.continuingTaskIds.length ? `${run.continuingTaskIds.length} continuing ${run.continuingTaskIds.length === 1 ? 'task' : 'tasks'} at last saved progress. Open the run to inspect exact task records.` : 'No continuing task is recorded at last saved progress.'}</p>}
        {run.waitingReason && <p>Waiting: {waitingSummary(run.waitingReason)}</p>}
        <p>Requested peers: {run.agentIds?.length ? run.agentIds.map(id => name(id, run)).join(', ') : 'Not recorded in this history row'}. Actual contributors appear in the opened run.</p>
        <details className="inline-detail"><summary>Full question and run details</summary><p>{run.question}</p><p>Run ID: <code>{run.id}</code></p>{run.parentRunId && <p>Parent run ID: <code>{run.parentRunId}</code></p>}{run.workspaceLabel && <p>Workspace: {run.workspaceLabel}</p>}{run.waitingReason && waitingSummary(run.waitingReason) !== run.waitingReason && <p>Saved wait detail: {run.waitingReason}</p>}<p>Required before scope completion: {run.participationPolicyVersion == null ? 'Not recorded for this older run; the requested-peer list does not establish a requirement.' : run.requiredAgentIds?.length ? run.requiredAgentIds.map(id => name(id, run)).join(', ') : 'None selected. Peers may still contribute independently.'}</p></details>
        <button className="outline-button" onClick={() => openRun(run.id)}>Open saved run</button>
      </article>;
    })}</div>}
  </section>;
}
