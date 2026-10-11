/* Show only saved provider-returned text from an authenticated local call lookup.
   Reasoning stays separate from answers and delivered peer messages. */
import { useEffect, useRef, useState } from 'react';
import type { ResearchSnapshot } from './generated/contracts';
import { AgentBadge } from './AgentBadge';
import { formatActivityTimestamp } from './ActivityRecordDetails';
import { workStepNumbers } from './attemptLineage';
import { TermHelp } from './TermHelp';
import { getLiveCalls, getLiveReasoning } from './live';
import type { LiveReasoning } from './live';
import './PeerCoordination.css';

type Json = Record<string, unknown>;
const obj = (value: unknown): Json | null => value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Json : null;
const str = (value: unknown): string | null => typeof value === 'string' && value.length > 0 ? value : null;

function reasoningLabel(availability: string) {
  if (availability === 'restricted' || availability === 'returned') return 'Saved model reasoning available';
  if (availability === 'truncated') return 'Partial saved reasoning';
  if (availability === 'absent') return 'No saved reasoning text indicated';
  return 'Reasoning availability unknown';
}

const roleFocus: Record<string, string> = {
  'literature acquisition': 'Find and inspect research literature',
  'evidence preparation synthesis': 'Prepare source evidence and synthesize findings',
  'independent methods reviewer critic': 'Review methods and challenge interpretations',
  'questions hypotheses ranking': 'Frame questions and competing hypotheses',
  'experimental design': 'Plan research designs',
  'data management': 'Organize and check research data',
  analysis: 'Analyze research data',
  'project collaboration planning': 'Plan shared research work',
  'shared research memory completion': 'Track shared context and report completion',
};

const toolAction: Record<string, string> = {
  search_scholarly: 'Search or fetch scholarly records',
  inspect_hit: 'Inspect a saved source hit',
  read_source_window: 'Read a saved source passage',
  fetch_source: 'Fetch a selected source',
  publish_result: 'Submit a research contribution',
  peer_request: 'Ask a peer a question',
  review_peer_answer: 'Review a peer answer',
  read_context_page: 'Read saved context',
  read_context_record: 'Read one saved context record',
  read_source_inventory: 'Review saved source choices',
  select_working_sources: 'Choose sources for the next turn',
};

function actionLabel(name: string) {
  return toolAction[name] ?? 'Use a recorded function';
}

interface SavedCall {
  id: string;
  runId: string;
  attemptId: string;
  agentId: string;
  status: string;
  createdAt: string | null;
  completedAt: string | null;
  availability: string;
  requestedFunctions: string[];
}

function savedCall(raw: unknown, attemptId: string): SavedCall | null {
  const call = obj(raw);
  const id = str(call?.id);
  const runId = str(call?.runId);
  const agentId = str(call?.agentId);
  const toolCalls = obj(call?.response)?.tool_calls;
  if (!id || !runId || !agentId || call?.attemptId !== attemptId) return null;
  return {
    id, runId, attemptId, agentId,
    status: str(call?.status) ?? 'not recorded',
    createdAt: str(call?.createdAt),
    completedAt: str(call?.completedAt),
    availability: str(obj(call?.returnedReasoning)?.availability) ?? 'unknown',
    requestedFunctions: Array.isArray(toolCalls) ? toolCalls.map(item => str(obj(obj(item)?.function)?.name)).filter((name): name is string => !!name) : [],
  };
}

export function ProviderReasoningText({ call }: { call: Pick<SavedCall, 'runId' | 'id' | 'attemptId' | 'agentId' | 'availability' | 'status'> }) {
  const [reasoning, setReasoning] = useState<LiveReasoning | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState(false);
  const [requestNumber, setRequestNumber] = useState(0);
  const canRead = !['prepared', 'superseded'].includes(call.status) && ['restricted', 'truncated', 'returned'].includes(call.availability);
  useEffect(() => {
    if (requestNumber === 0) return;
    let active = true;
    setLoading(true);
    setError(false);
    setReasoning(null);
    getLiveReasoning(call.runId, call.id)
      .then(result => {
        if (!active) return;
        if (result.runId !== call.runId || result.callId !== call.id
          || result.attemptId !== call.attemptId || result.agentId !== call.agentId
          || (result.status === 'available' && (result.origin !== 'provider_returned' || !str(result.text)))) {
          setError(true);
          return;
        }
        setReasoning(result);
      })
      .catch(() => { if (active) setError(true); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [call.runId, call.id, call.attemptId, call.agentId, requestNumber]);

  return <div className="saved-provider-reasoning">
    {canRead && !reasoning && <button type="button" className="inline-link" disabled={loading} onClick={() => setRequestNumber(value => value + 1)}>{loading ? 'Loading saved provider reasoning…' : error ? 'Retry saved provider reasoning' : 'Show saved provider reasoning'}</button>}
    {error && <p role="alert">This call’s saved provider reasoning could not be read. Its availability is not established here.</p>}
    {reasoning?.status === 'available' && reasoning.text ? <div className="agent-reasoning-text">
      <h5>Provider-returned reasoning{reasoning.reason === 'truncated' || call.availability === 'truncated' ? ' · incomplete excerpt' : ''}</h5>
      <pre>{reasoning.text}</pre>
      {reasoning.redacted && <p>Credential-shaped material was redacted in the saved text.</p>}
    </div> : reasoning?.status === 'unavailable' || (!canRead && call.status !== 'prepared' && call.status !== 'superseded') ? <p>No provider reasoning text is available for this call.</p> : null}
  </div>;
}

function CallReasoning({ call, callNumber, agentName }: { call: SavedCall; callNumber: number; agentName: string | null }) {
  return <li className="agent-reasoning-call">
    <div><strong><AgentBadge name={agentName} agentId={call.agentId} /> · Model call {callNumber}</strong></div>
    <p>{call.createdAt ? <time dateTime={call.createdAt}>{formatActivityTimestamp(call.createdAt)}</time> : 'Time unavailable'} · Call {call.status.replaceAll('_', ' ')} · {reasoningLabel(call.availability)}</p>
    <p><strong>Model requested:</strong> {call.requestedFunctions.length ? call.requestedFunctions.map(actionLabel).join('; ') : 'No function request saved in this call'}. The response, if any, is separate in Details.</p>
    <details className="agent-reasoning-identifiers"><summary>Exact saved identifiers and times</summary><dl>
      <div><dt>Call ID</dt><dd>{call.id}</dd></div>
      <div><dt>Attempt ID</dt><dd>{call.attemptId}</dd></div>
      <div><dt>Originating run ID</dt><dd>{call.runId}</dd></div>
      <div><dt>Started</dt><dd>{call.createdAt ?? 'Not recorded'}</dd></div>
      <div><dt>Finished</dt><dd>{call.completedAt ?? 'Not recorded'}</dd></div>
      <div><dt>Exact requested functions</dt><dd>{call.requestedFunctions.join(', ') || 'None recorded'}</dd></div>
    </dl></details>
    <ProviderReasoningText call={call} />
  </li>;
}

function AttemptReasoning({ runId, snapshotId, attempt, attemptNumber, agentName, agentRole, task, selected, earlierRun }: {
  runId: string; snapshotId: string; attempt: ResearchSnapshot['attempts'][number]; attemptNumber: number;
  agentName: string | null; agentRole: string | null; task: ResearchSnapshot['tasks'][number] | undefined;
  selected: boolean; earlierRun: boolean;
}) {
  const [opened, setOpened] = useState(false);
  const disclosure = useRef<HTMLDetailsElement>(null);
  const summary = useRef<HTMLElement>(null);
  const [calls, setCalls] = useState<SavedCall[] | null>(null);
  const [callsSnapshotId, setCallsSnapshotId] = useState<string | null>(null);
  const [error, setError] = useState(false);
  const [requestNumber, setRequestNumber] = useState(0);
  useEffect(() => {
    if (!selected || !disclosure.current) return;
    disclosure.current.open = true;
    setOpened(true);
    summary.current?.focus();
    summary.current?.scrollIntoView?.({ block: 'nearest' });
  }, [selected]);
  useEffect(() => {
    if (!opened) return;
    let active = true;
    setError(false);
    getLiveCalls(runId, attempt.id)
      .then(result => {
        if (!active) return;
        const parsed = result.records.map(item => savedCall(item, attempt.id));
        if (parsed.some(item => item === null || item.agentId !== attempt.agentId)) {
          setError(true);
          return;
        }
        setCalls(parsed as SavedCall[]);
        setCallsSnapshotId(snapshotId);
      })
      .catch(() => { if (active) setError(true); });
    return () => { active = false; };
  }, [opened, runId, attempt.id, snapshotId, requestNumber]);

  return <details ref={disclosure} className="agent-reasoning-attempt" onToggle={event => setOpened(event.currentTarget.open)}>
    <summary ref={summary}><AgentBadge name={agentName} agentId={attempt.agentId} /> · Work step {attemptNumber} · {earlierRun ? 'Earlier run' : 'This run'} · {task?.title ?? 'Task title unavailable'} · <time dateTime={attempt.startedAt}>Started {formatActivityTimestamp(attempt.startedAt)}</time> · {attempt.status.replaceAll('_', ' ')} · {reasoningLabel(attempt.returnedReasoning.availability)}</summary>
    {opened && <div className="agent-reasoning-attempt-body">
      <p><strong>Role focus:</strong> {agentRole ? roleFocus[agentRole.toLowerCase()] ?? agentRole : 'Not recorded for this agent'}.</p>
      <p><strong>Saved task assignment:</strong> <TermHelp term="task assignment" /> {task?.title ?? 'Task title unavailable'}{task ? ` · task ${task.status.replaceAll('_', ' ')}` : ''}.</p>
      <div><strong>Recorded tool actions:</strong> {attempt.toolCalls.length ? <ul className="agent-reasoning-actions">{attempt.toolCalls.map((tool, index) => <li key={`${tool.name}:${index}`}>{actionLabel(tool.name)} · saved outcome: {tool.status.replaceAll('_', ' ')}</li>)}</ul> : <span> No host tool action saved for this attempt.</span>}</div>
      <details className="agent-reasoning-identifiers"><summary>Exact attempt identifiers</summary><dl>
        <div><dt>Attempt ID</dt><dd>{attempt.id}</dd></div>
        <div><dt>Task ID</dt><dd>{attempt.taskId}</dd></div>
        <div><dt>Saved role</dt><dd>{agentRole ?? 'Not recorded'}</dd></div>
        <div><dt>Exact tool names</dt><dd>{attempt.toolCalls.map(tool => tool.name).join(', ') || 'None recorded'}</dd></div>
      </dl></details>
      {calls && callsSnapshotId !== snapshotId && <p role="status">Checking calls for the newer snapshot. The call details below came from the prior snapshot.</p>}
      {error && <><p role="alert">{calls ? 'Call records could not be refreshed for this snapshot; prior saved details remain below.' : 'Call records for this attempt could not be read.'}</p><button type="button" className="inline-link" onClick={() => setRequestNumber(value => value + 1)}>Retry call records</button></>}
      {calls === null ? !error && <p role="status">Loading saved calls…</p>
        : calls.length === 0 ? <p>No model call is saved for this attempt.</p>
          : <ol>{calls.map((call, index) => <CallReasoning key={`${call.runId}:${call.id}`} call={call} callNumber={index + 1} agentName={agentName} />)}</ol>}
    </div>}
  </details>;
}

export function LiveAgentReasoning({ runId, snapshot, selectedAttemptId, earlierAttemptIds = [] }: { runId: string; snapshot: ResearchSnapshot; selectedAttemptId?: string | null; earlierAttemptIds?: readonly string[] }) {
  const overview = useRef<HTMLDetailsElement>(null);
  const lastSelection = useRef<string | null>(null);
  const [agentFilter, setAgentFilter] = useState('all');
  useEffect(() => {
    if (!selectedAttemptId) {
      lastSelection.current = null;
      return;
    }
    if (lastSelection.current === selectedAttemptId) return;
    const selected = snapshot.attempts.find(attempt => attempt.id === selectedAttemptId);
    if (selected && overview.current) {
      overview.current.open = true;
      setAgentFilter(selected.agentId);
      lastSelection.current = selectedAttemptId;
    }
  }, [selectedAttemptId, snapshot.attempts]);
  const chronological = [...snapshot.attempts].sort((a, b) => a.startedAt.localeCompare(b.startedAt) || a.id.localeCompare(b.id));
  const attempts = [...chronological].reverse();
  const visibleAttempts = agentFilter === 'all' ? attempts : attempts.filter(attempt => attempt.agentId === agentFilter);
  const agentIds = [...new Set(attempts.map(attempt => attempt.agentId))];
  const attemptNumbers = workStepNumbers(snapshot, new Set(earlierAttemptIds));
  const savedTextIndicated = attempts.filter(attempt => ['restricted', 'returned', 'truncated'].includes(attempt.returnedReasoning.availability)).length;
  return <section id="agent-reasoning" tabIndex={-1} className="panel agent-reasoning" aria-label="Agent reasoning">
    <details ref={overview} className="agent-reasoning-overview"><summary><span aria-hidden="true">◇ </span><span>Agent reasoning · {attempts.length} saved work step{attempts.length === 1 ? '' : 's'}</span> <small>· {savedTextIndicated} indicate saved provider text</small></summary><div className="agent-reasoning-overview-body">
      <h4>Saved model reasoning by agent and work step <TermHelp term="model reasoning" /></h4>
      <p>A work step is one agent’s saved attempt on a task. It may have prepared a model call without dispatching it. Reasoning here is only text the provider actually returned for a saved call; it is separate from the answer and from messages delivered to peers. Choose an agent and work step, then open its model call.</p>
      {agentIds.length > 1 && <label className="agent-reasoning-filter">Agent <select aria-label="Filter reasoning by agent" value={agentFilter} onChange={event => setAgentFilter(event.target.value)}><option value="all">All agents</option>{agentIds.map(id => <option key={id} value={id}>{snapshot.agents.find(agent => agent.id === id)?.name ?? `Agent identity unavailable (${id})`}</option>)}</select></label>}
      {attempts.length === 0 ? <p>No agent attempt is saved in this view.</p> : visibleAttempts.map(attempt => <AttemptReasoning
        key={`${runId}:${attempt.id}`} runId={runId} snapshotId={snapshot.snapshotId} attempt={attempt} attemptNumber={attemptNumbers.get(attempt.id)!}
        agentName={snapshot.agents.find(agent => agent.id === attempt.agentId)?.name ?? null}
        agentRole={snapshot.agents.find(agent => agent.id === attempt.agentId)?.role ?? null}
        task={snapshot.tasks.find(task => task.id === attempt.taskId)}
        selected={attempt.id === selectedAttemptId}
        earlierRun={earlierAttemptIds.includes(attempt.id)}
      />)}
    </div></details>
  </section>;
}
