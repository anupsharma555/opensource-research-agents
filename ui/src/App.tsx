/* Display a saved research question and let a reviewer follow its evidence trail.
   The React views link findings to source passages, peer requests, task attempts,
   and separate diagnostics. Follow-up receipts and notes are local previews over
   synthetic data; no research host receives a command. */
import { useEffect, useMemo, useState } from 'react';
import type { CSSProperties } from 'react';
import type { ResearchSnapshot } from './generated/contracts';
import './styles.css';

type Screen = 'overview' | 'work' | 'evidence' | 'diagnostics' | 'request' | 'compare';
type PreviewState = 'normal' | 'empty' | 'loading' | 'disconnected' | 'quota' | 'failed' | 'partial';
type Task = ResearchSnapshot['tasks'][number];
type Attempt = ResearchSnapshot['attempts'][number];
type Source = ResearchSnapshot['sources'][number];
type Claim = ResearchSnapshot['claims'][number];

const navigation: { id: Screen; label: string; description: string }[] = [
  { id: 'overview', label: 'Findings', description: 'Answer and progress' },
  { id: 'work', label: 'Work', description: 'Tasks and peer requests' },
  { id: 'evidence', label: 'Evidence', description: 'Sources and claim history' },
  { id: 'diagnostics', label: 'Diagnostics', description: 'Attempts and returned content' },
  { id: 'request', label: 'Start or follow up', description: 'Preview a scoped request' },
  { id: 'compare', label: 'Compare', description: 'Saved sample versions' },
];

const previewStates: { value: PreviewState; label: string }[] = [
  { value: 'normal', label: 'Normal sample' },
  { value: 'empty', label: 'Empty mission' },
  { value: 'loading', label: 'Loading' },
  { value: 'disconnected', label: 'Disconnected' },
  { value: 'quota', label: 'Quota waiting' },
  { value: 'failed', label: 'Failed retrieval' },
  { value: 'partial', label: 'Partial evidence' },
];

function labelStatus(status: string) {
  return status.replaceAll('_', ' ');
}

function formatTime(value?: string | null) {
  if (!value) return 'Not yet recorded';
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? value
    : new Intl.DateTimeFormat('en-US', { month: 'short', day: 'numeric', hour: 'numeric', minute: '2-digit' }).format(date);
}

function StatusPill({ status }: { status: string }) {
  return <span className={`status-pill status-${status.replaceAll('_', '-')}`}>{labelStatus(status)}</span>;
}

function SectionHeading({ eyebrow, title, description }: { eyebrow: string; title: string; description?: string }) {
  return (
    <div className="section-heading">
      <div>
        <span className="eyebrow">{eyebrow}</span>
        <h2>{title}</h2>
        {description && <p>{description}</p>}
      </div>
    </div>
  );
}

function ReasoningView({ reasoning }: { reasoning: Attempt['returnedReasoning'] }) {
  const availability = reasoning.availability;
  return (
    <div className={`reasoning-box reasoning-${availability}`}>
      <strong>{availability === 'returned' ? 'Illustrative returned content' : `Returned content: ${labelStatus(availability)}`}</strong>
      {availability === 'returned' && reasoning.text ? (
        <p>{reasoning.text}</p>
      ) : availability === 'returned' ? (
        <p>The snapshot marks content as returned but does not include its text. Inspect the source record before quoting it.</p>
      ) : availability === 'truncated' ? (
        <p>{reasoning.text || 'The saved fixture contains only a cut-off excerpt.'} This is incomplete; no missing continuation is inferred.</p>
      ) : availability === 'restricted' ? (
        <p>Access is restricted. The reader finding is separate from this diagnostic material.</p>
      ) : (
        <p>No reasoning content was returned in this fixture. A concise rationale is not a substitute for missing provider content.</p>
      )}
      <small>Scripted sample data. No model call was made for this screen.</small>
    </div>
  );
}

function PreviewNotice({ state, capturedAt }: { state: PreviewState; capturedAt: string }) {
  if (state === 'normal') return null;
  const copy: Record<Exclude<PreviewState, 'normal'>, { title: string; body: string; next: string }> = {
    empty: { title: 'No mission in this view', body: 'This is the empty-state preview. The sample run is still available from the state selector.', next: 'Start or preview a scoped request.' },
    loading: { title: 'Loading a saved view', body: 'A live app would request a snapshot and keep its last known cursor visible.', next: 'Wait for the response or inspect the last saved view.' },
    disconnected: { title: 'Connection unavailable', body: `The last sample snapshot was captured ${formatTime(capturedAt)}. Current host state is unknown.`, next: 'Reconnect, then compare the mission ID and cursor before acting.' },
    quota: { title: 'One task is waiting for capacity', body: 'This alternate scripted state shows a queued task. The normal sample methods task waits for source access, not quota.', next: 'Inspect the queued task and its last useful artifact.' },
    failed: { title: 'Retrieval attempt failed', body: 'A permitted methods table was not obtained. The abstract finding remains available with that limitation.', next: 'Inspect the failed attempt and request a different source route.' },
    partial: { title: 'Evidence is partial', body: 'The available abstracts support reported counts, but methods and adverse-event details are unavailable.', next: 'Read supported findings now; keep the specific gap open.' },
  };
  const item = copy[state];
  return (
    <div className={`preview-notice preview-${state}`} role="status">
      <div><strong>{item.title}</strong><p>{item.body}</p></div>
      <span>{item.next}</span>
    </div>
  );
}

export function ResearchApp({ snapshot, comparisonSnapshot }: { snapshot: ResearchSnapshot; comparisonSnapshot?: ResearchSnapshot }) {
  const [screen, setScreen] = useState<Screen>('overview');
  const [previewState, setPreviewState] = useState<PreviewState>('normal');
  const [selectedTaskId, setSelectedTaskId] = useState(snapshot.tasks[0]?.id ?? '');
  const [selectedSourceId, setSelectedSourceId] = useState(snapshot.sources[0]?.id ?? '');
  const [selectedClaimId, setSelectedClaimId] = useState(snapshot.claims[0]?.id ?? '');
  const [selectedAttemptId, setSelectedAttemptId] = useState(snapshot.attempts[0]?.id ?? '');
  const [selectedSpanId, setSelectedSpanId] = useState<string | null>(null);
  const [requestKind, setRequestKind] = useState<'new' | 'follow_up'>('follow_up');
  const [requestQuestion, setRequestQuestion] = useState('Find an independent adult comparison before revising the earlier report.');
  const [requestScope, setRequestScope] = useState('Use permitted abstracts and exact passages. Keep the earlier report available while the source check runs.');
  const [requestTarget, setRequestTarget] = useState('report-v1');
  const [receipt, setReceipt] = useState(false);
  const [annotationText, setAnnotationText] = useState('');
  const [annotations, setAnnotations] = useState<{ id: number; target: string; text: string }[]>([]);

  useEffect(() => {
    window.scrollTo(0, 0);
  }, [screen]);

  const agentsById = useMemo(() => new Map(snapshot.agents.map(agent => [agent.id, agent])), [snapshot.agents]);
  const tasksById = useMemo(() => new Map(snapshot.tasks.map(task => [task.id, task])), [snapshot.tasks]);
  const sourcesById = useMemo(() => new Map(snapshot.sources.map(source => [source.id, source])), [snapshot.sources]);
  const currentReport = snapshot.artifacts.find(artifact => artifact.id === snapshot.mission.reportArtifactId);
  const selectedTask = tasksById.get(selectedTaskId) ?? snapshot.tasks[0];
  const selectedSource = sourcesById.get(selectedSourceId) ?? snapshot.sources[0];
  const selectedClaim = snapshot.claims.find(claim => claim.id === selectedClaimId) ?? snapshot.claims[0];
  const selectedAttempt = snapshot.attempts.find(attempt => attempt.id === selectedAttemptId) ?? snapshot.attempts[0];
  const currentNav = navigation.find(item => item.id === screen)!;
  const latestMessages = Array.from(
    [...snapshot.messages].sort((a, b) => a.createdAt.localeCompare(b.createdAt))
      .reduce((map, message) => map.set(message.requestId || message.id, message), new Map<string, ResearchSnapshot['messages'][number]>()).values(),
  );
  const timelineStart = Math.min(...snapshot.attempts.map(item => new Date(item.startedAt).getTime()));
  const timelineEnd = new Date(snapshot.capturedAt).getTime();

  function taskBarStyle(task: Task): CSSProperties {
    const attempts = snapshot.attempts.filter(item => item.taskId === task.id);
    if (!attempts.length || timelineEnd <= timelineStart) return { left: '0%', width: '4%' };
    const start = Math.min(...attempts.map(item => new Date(item.startedAt).getTime()));
    const finish = Math.max(...attempts.map(item => new Date(item.endedAt || snapshot.capturedAt).getTime()));
    const left = Math.max(0, Math.min(96, ((start - timelineStart) / (timelineEnd - timelineStart)) * 100));
    const width = Math.max(4, Math.min(100 - left, ((finish - start) / (timelineEnd - timelineStart)) * 100));
    return { left: `${left}%`, width: `${width}%` };
  }

  function openSource(sourceId: string, spanId?: string) {
    setSelectedSourceId(sourceId);
    setSelectedSpanId(spanId ?? null);
    setScreen('evidence');
  }

  function openTask(taskId: string) {
    setSelectedTaskId(taskId);
    const attempt = snapshot.attempts.find(item => item.taskId === taskId);
    if (attempt) setSelectedAttemptId(attempt.id);
    setScreen('work');
  }

  function openClaim(claimId: string) {
    setSelectedClaimId(claimId);
    setScreen('evidence');
  }

  function renderOverview() {
    return (
      <div className="page-stack">
        <SectionHeading eyebrow="Mission review" title="What the evidence says" description="A reader-first answer, with the open question and its source trail close by." />
        <div className="hero-card">
          <div className="hero-topline"><span className="eyebrow">Current sample finding</span><StatusPill status={snapshot.mission.status} /></div>
          <h3>{snapshot.mission.question}</h3>
          <p className="hero-answer">{currentReport?.body ?? 'No report has been saved in this sample.'}</p>
          <div className="citation-row">
            {currentReport?.citations.map(citation => (
              <button key={`${citation.sourceId}-${citation.spanId}`} className="citation-button" onClick={() => openSource(citation.sourceId, citation.spanId)}>
                {citation.sourceId} · passage {citation.spanId} <span aria-hidden="true">↗</span>
              </button>
            ))}
          </div>
          <div className="hero-bottom"><span>Saved report v{currentReport?.version ?? '—'}</span><span>Source access: abstracts and an access notice</span></div>
        </div>
        <div className="metric-grid">
          <div className="metric-card"><span className="eyebrow">Evidence reviewed</span><strong>{snapshot.sources.length}</strong><small>source encounters in this scripted packet</small></div>
          <div className="metric-card"><span className="eyebrow">Peer requests</span><strong>{new Set(snapshot.messages.map(message => message.requestId || message.id)).size}</strong><small>{latestMessages.filter(message => message.status !== 'fulfilled').length} still open</small></div>
          <div className="metric-card"><span className="eyebrow">First useful output</span><strong>Abstract summary</strong><small>before the methods follow-up resolves</small></div>
        </div>
        <div className="two-column">
          <section className="panel">
            <div className="panel-heading"><div><span className="eyebrow">Claim lineage</span><h3>Why the answer changed</h3></div><button className="text-button" onClick={() => setScreen('evidence')}>See history <span aria-hidden="true">→</span></button></div>
            <ol className="compact-timeline">
              {snapshot.claims.map(claim => (
                <li key={claim.id}>
                  <span className="timeline-dot" />
                  <div><strong>Version {claim.version}</strong><p>{claim.text}</p><button className="inline-link" onClick={() => openClaim(claim.id)}>Inspect cited evidence</button></div>
                </li>
              ))}
            </ol>
          </section>
          <section className="panel">
            <div className="panel-heading"><div><span className="eyebrow">Still open</span><h3>Next useful work</h3></div><StatusPill status="waiting" /></div>
            <p>The first study's methods and adverse-event details are unavailable in the current packet. An acquisition peer acknowledged the request, but has not fulfilled it.</p>
            <button className="primary-button" onClick={() => openTask('task-methods')}>Inspect waiting task <span aria-hidden="true">→</span></button>
            <div className="limitations"><strong>Material limitation</strong><p>{currentReport?.limitations[0] ?? 'No limitation saved.'}</p></div>
          </section>
        </div>
      </div>
    );
  }

  function renderWork() {
    const taskAttempts = snapshot.attempts.filter(attempt => attempt.taskId === selectedTask?.id);
    const fulfilledContribution = snapshot.messages.find(message => message.taskId === selectedTask?.id && message.status === 'fulfilled');
    return (
      <div className="page-stack">
        <SectionHeading eyebrow="Work and coordination" title="Parallel work, visible handoffs" description="Tasks have separate attempts, direct peer requests, and an explicit waiting state. Durations describe expected work, not deadlines." />
        <section className="panel">
          <div className="panel-heading"><div><span className="eyebrow">Task timeline</span><h3>What overlapped</h3></div><span className="subtle-label">Bars use scripted attempt times · no live scheduler</span></div>
          <div className="work-grid" aria-label="Task timeline">
            {snapshot.tasks.map((task, index) => (
              <button key={task.id} className={`work-row ${selectedTask?.id === task.id ? 'selected' : ''}`} onClick={() => setSelectedTaskId(task.id)}>
                <span className="work-number">{String(index + 1).padStart(2, '0')}</span>
                <span className="work-description"><strong>{task.title}</strong><small>{task.latencyClass.replace('_', ' ')} · {task.activeOrWaiting} · {task.latencyBasis}</small></span>
                <span className="work-track" aria-hidden="true"><span className={`work-bar work-bar-${task.status}`} style={taskBarStyle(task)} /></span>
                <StatusPill status={task.status} />
              </button>
            ))}
          </div>
        </section>
        <div className="two-column work-detail-grid">
          <section className="panel">
            <div className="panel-heading"><div><span className="eyebrow">Selected task</span><h3>{selectedTask?.title ?? 'No task selected'}</h3></div>{selectedTask && <StatusPill status={selectedTask.status} />}</div>
            {selectedTask && <>
              <div className="detail-list">
                <div><span>Task</span><strong>{selectedTask.id}</strong></div>
                <div><span>Parent</span><strong>{selectedTask.parentTaskId || 'Mission root'}</strong></div>
                {selectedTask.targetArtifactId && <div><span>Starting artifact</span><strong>{selectedTask.targetArtifactId}</strong></div>}
                <div><span>First useful artifact</span><strong>{formatTime(selectedTask.firstUsefulAt)}</strong></div>
                <div><span>Current mode</span><strong>{selectedTask.activeOrWaiting}</strong></div>
              </div>
              <p className="detail-note">{selectedTask.status === 'waiting' ? 'This task is waiting for a permitted source route. The answer already supported by abstracts remains available.' : 'The selected task can be inspected without hiding sibling progress.'}</p>
              {fulfilledContribution && <div className="task-contribution"><strong>Recorded contribution</strong><p>{fulfilledContribution.summary}</p><div className="request-links">{fulfilledContribution.sourceId && <button className="inline-link" onClick={() => openSource(fulfilledContribution.sourceId!)}>Inspect {fulfilledContribution.sourceId}</button>}{fulfilledContribution.claimId && <button className="inline-link" onClick={() => openClaim(fulfilledContribution.claimId!)}>Inspect revised claim</button>}</div></div>}
              <h4>Attempts</h4>
              <div className="attempt-stack">
                {taskAttempts.map(attempt => <button key={attempt.id} className="attempt-item" onClick={() => { setSelectedAttemptId(attempt.id); setScreen('diagnostics'); }}><span><strong>{attempt.id}</strong><small>{agentsById.get(attempt.agentId)?.name || attempt.agentId} · {formatTime(attempt.startedAt)}</small></span><StatusPill status={attempt.status} /></button>)}
              </div>
            </>}
          </section>
          <section className="panel">
            <div className="panel-heading"><div><span className="eyebrow">Direct communication</span><h3>Peer requests</h3></div><span className="subtle-label">Requested → delivered → acknowledged → fulfilled</span></div>
            <div className="request-stack">
              {latestMessages.map(message => {
                const history = snapshot.messages.filter(item => (item.requestId || item.id) === (message.requestId || message.id));
                return <article key={message.requestId || message.id} className="request-card">
                  <div className="request-head"><strong>{message.requestId || message.id}</strong><StatusPill status={message.status} /></div>
                  <p>{message.summary}</p>
                  <div className="request-people">{agentsById.get(message.fromAgentId)?.name || message.fromAgentId} <span aria-hidden="true">→</span> {agentsById.get(message.toAgentId)?.name || message.toAgentId}</div>
                  <div className="request-steps" aria-label={`Status history for ${message.requestId || message.id}`}>
                    {['requested', 'delivered', 'acknowledged', 'fulfilled'].map(status => <span key={status} className={history.some(item => item.status === status) ? 'reached' : ''}>{status}</span>)}
                  </div>
                  <div className="request-links"><button className="inline-link" onClick={() => openTask(message.taskId)}>Open task</button>{message.sourceId && <button className="inline-link" onClick={() => openSource(message.sourceId!)}>Source contribution</button>}{message.claimId && <button className="inline-link" onClick={() => openClaim(message.claimId!)}>Claim revision</button>}</div>
                </article>;
              })}
            </div>
          </section>
        </div>
        <section className="panel">
          <div className="panel-heading"><div><span className="eyebrow">Serial event trail</span><h3>Accepted steps and their effect</h3></div><span className="subtle-label">Fixed fixture timestamps</span></div>
          <div className="diagnostic-events">{snapshot.events.map(event => <article key={event.id}><span className="event-time">{formatTime(event.time)}</span><div><strong>{labelStatus(event.kind)}</strong><p>{event.summary}</p></div>{event.taskId && <button className="inline-link" onClick={() => openTask(event.taskId!)}>Open task</button>}</article>)}</div>
        </section>
      </div>
    );
  }

  function renderEvidence() {
    return (
      <div className="page-stack">
        <SectionHeading eyebrow="Evidence and provenance" title="Every source, each claim version" description="Abstract-only facts are useful when labeled. A missing full text cannot supply unreported methods or safety results." />
        <div className="evidence-grid">
          <section className="panel source-list-panel">
            <div className="panel-heading"><div><span className="eyebrow">Complete source list</span><h3>{snapshot.sources.length} saved source records</h3></div></div>
            <div className="source-list">
              {snapshot.sources.map(source => <button key={source.id} className={`source-list-item ${selectedSource?.id === source.id ? 'selected' : ''}`} onClick={() => { setSelectedSourceId(source.id); setSelectedSpanId(null); }}><span className="source-letter">{source.id}</span><span><strong>{source.title}</strong><small>{labelStatus(source.accessState)} · {labelStatus(source.retrievalStatus)}</small></span></button>)}
            </div>
          </section>
          <section className="panel source-detail-panel">
            {selectedSource ? <>
              <div className="panel-heading"><div><span className="eyebrow">Source copy {selectedSource.copyId} · v{selectedSource.version}</span><h3>{selectedSource.title}</h3></div><StatusPill status={selectedSource.accessState} /></div>
              <p className="source-meta">{selectedSource.provider || 'Synthetic packet'}{selectedSource.url ? ` · ${selectedSource.url}` : ''} · {labelStatus(selectedSource.retrievalStatus)}</p>
              <div className="passage-list">
                {selectedSource.passages.map(passage => <article key={passage.spanId} className={`passage ${selectedSpanId === passage.spanId ? 'highlighted' : ''}`}><div><span className="eyebrow">{passage.spanId}</span><span>{passage.locator}</span></div><blockquote>{passage.text}</blockquote></article>)}
                {selectedSource.passages.length === 0 && <p>No retained passage is available. A locator or access notice does not establish missing details.</p>}
              </div>
              <p className="tiny-note">Hash {selectedSource.contentHash || 'not retained'} · Scripted source record, not a live retrieval.</p>
            </> : <p>No source selected.</p>}
          </section>
        </div>
        <section className="panel">
          <div className="panel-heading"><div><span className="eyebrow">Claim history</span><h3>Revisions keep their evidence</h3></div></div>
          <div className="claim-tabs" role="group" aria-label="Claim versions">
            {snapshot.claims.map(claim => <button key={claim.id} className={selectedClaim?.id === claim.id ? 'active' : ''} onClick={() => setSelectedClaimId(claim.id)}>Version {claim.version}</button>)}
          </div>
          {selectedClaim && <div className="claim-detail"><div><StatusPill status={selectedClaim.disposition} /><span className="subtle-label">{selectedClaim.id}{selectedClaim.supersedesVersion ? ` · supersedes v${selectedClaim.supersedesVersion}` : ''}</span></div><p>{selectedClaim.text}</p><div className="citation-row">{selectedClaim.supports.map(support => <button key={`${support.sourceId}-${support.spanId}`} className="citation-button" onClick={() => openSource(support.sourceId, support.spanId)}>{support.sourceId} · {support.spanId} <span aria-hidden="true">↗</span></button>)}</div></div>}
        </section>
        <section className="panel">
          <div className="panel-heading"><div><span className="eyebrow">Reviewer annotation</span><h3>Note a source or claim question</h3></div><span className="subtle-label">Local preview · not saved to a host</span></div>
          <p>Attach a review note to the selected source copy and claim version. Notes disappear when this prototype reloads.</p>
          <label className="field-label" htmlFor="annotation">Your note about {selectedSource?.id ?? 'the source'} and claim v{selectedClaim?.version ?? '—'}</label>
          <textarea id="annotation" rows={3} value={annotationText} onChange={event => setAnnotationText(event.target.value)} placeholder="For example: confirm whether the interval uses the same outcome definition." />
          <button className="primary-button annotation-save" disabled={!annotationText.trim()} onClick={() => { setAnnotations(previous => [...previous, { id: previous.length + 1, target: `${selectedSource?.copyId ?? 'no-copy'} · claim v${selectedClaim?.version ?? '—'}`, text: annotationText.trim() }]); setAnnotationText(''); }}>Save local sample note</button>
          {annotations.length > 0 && <div className="annotation-list" aria-label="Local sample notes">{annotations.map(note => <article key={note.id}><span className="eyebrow">Evidence · {snapshot.mission.id} v{snapshot.mission.version} · {note.target} · sample {snapshot.sampleVersion}</span><p>{note.text}</p></article>)}</div>}
        </section>
      </div>
    );
  }

  function renderDiagnostics() {
    return (
      <div className="page-stack">
        <SectionHeading eyebrow="Diagnostic workspace" title="What the sample attempted" description="Identifiers, tool traces and returned content live here, separate from the reader finding. No provider or model was called for this prototype." />
        <div className="diagnostic-layout">
          <section className="panel">
            <div className="panel-heading"><div><span className="eyebrow">Attempt history</span><h3>Inspect an exact attempt</h3></div></div>
            <div className="attempt-stack">
              {snapshot.attempts.map(attempt => <button key={attempt.id} className={`attempt-item ${selectedAttempt?.id === attempt.id ? 'selected' : ''}`} onClick={() => setSelectedAttemptId(attempt.id)}><span><strong>{attempt.id}</strong><small>{tasksById.get(attempt.taskId)?.title || attempt.taskId}</small></span><StatusPill status={attempt.status} /></button>)}
            </div>
          </section>
          <section className="panel">
            {selectedAttempt ? <>
              <div className="panel-heading"><div><span className="eyebrow">{selectedAttempt.id} · {selectedAttempt.taskId}</span><h3>{agentsById.get(selectedAttempt.agentId)?.name || selectedAttempt.agentId}</h3></div><StatusPill status={selectedAttempt.status} /></div>
              <p>{selectedAttempt.resultSummary || 'No result summary was saved for this attempt.'}</p>
              <div className="detail-list"><div><span>Started</span><strong>{formatTime(selectedAttempt.startedAt)}</strong></div><div><span>Ended</span><strong>{formatTime(selectedAttempt.endedAt)}</strong></div><div><span>Selected model</span><strong>{selectedAttempt.selectedModel || 'None · scripted fixture'}</strong></div><div><span>Skill versions</span><strong>{selectedAttempt.skillVersions.length ? selectedAttempt.skillVersions.join(', ') : 'None in this fixture'}</strong></div></div>
              <h4>Tools and providers</h4>
              <div className="tool-stack">{selectedAttempt.toolCalls.map((tool, index) => <div key={`${tool.name}-${index}`}><span><strong>{tool.name}</strong><small>{tool.provider || 'Local scripted fixture'}</small></span><StatusPill status={tool.status} /></div>)}{selectedAttempt.toolCalls.length === 0 && <p>No tool call was saved.</p>}</div>
              <h4>Returned reasoning availability</h4>
              <ReasoningView reasoning={selectedAttempt.returnedReasoning} />
            </> : <p>No attempt selected.</p>}
          </section>
        </div>
        <section className="panel">
          <div className="panel-heading"><div><span className="eyebrow">Operational events</span><h3>Failures are explicit</h3></div></div>
          <div className="diagnostic-events">{snapshot.diagnostics.map(item => <article key={item.id}><span className="event-time">{formatTime(item.occurredAt)}</span><div><strong>{item.kind} · {labelStatus(item.status)}</strong><p>{item.message}</p></div>{item.taskId && <button className="inline-link" onClick={() => openTask(item.taskId!)}>Task</button>}</article>)}</div>
        </section>
      </div>
    );
  }

  function renderRequest() {
    const targetClaim = snapshot.claims.find(claim => claim.id === requestTarget);
    const targetTask = snapshot.tasks.find(task => task.id === requestTarget);
    const targetArtifact = snapshot.artifacts.find(artifact => artifact.id === requestTarget);
    const targetLabel = targetClaim ? `Claim ${targetClaim.id}, version ${targetClaim.version}` : targetTask ? `Task ${targetTask.id}` : targetArtifact ? `Report ${targetArtifact.id}, version ${targetArtifact.version}` : 'Mission root';
    const hasRecordedPath = requestKind === 'follow_up' && requestTarget === 'report-v1';
    return (
      <div className="page-stack">
        <SectionHeading eyebrow="Scoped request" title="Start from the right place" description="Preview the change before a simulated queue receipt. This form does not send work to a backend." />
        <div className="two-column request-layout">
          <section className="panel">
            <div className="segmented-control" role="group" aria-label="Request type"><button className={requestKind === 'new' ? 'active' : ''} onClick={() => { setRequestKind('new'); setReceipt(false); }}>New mission</button><button className={requestKind === 'follow_up' ? 'active' : ''} onClick={() => { setRequestKind('follow_up'); setReceipt(false); }}>Follow up on exact work</button></div>
            <label className="field-label" htmlFor="request-question">Research question or follow-up</label>
            <textarea id="request-question" rows={4} value={requestQuestion} onChange={event => { setRequestQuestion(event.target.value); setReceipt(false); }} />
            <label className="field-label" htmlFor="request-scope">Scope and constraints</label>
            <textarea id="request-scope" rows={4} value={requestScope} onChange={event => { setRequestScope(event.target.value); setReceipt(false); }} />
            {requestKind === 'follow_up' && <><label className="field-label" htmlFor="request-target">Exact saved target</label><select id="request-target" value={requestTarget} onChange={event => { setRequestTarget(event.target.value); setReceipt(false); }}>{snapshot.claims.map(claim => <option key={claim.id} value={claim.id}>Claim {claim.id} · v{claim.version}</option>)}{snapshot.tasks.map(task => <option key={task.id} value={task.id}>Task {task.id}</option>)}{snapshot.artifacts.filter(artifact => artifact.kind === 'report').map(artifact => <option key={artifact.id} value={artifact.id}>Report {artifact.id} · v{artifact.version}</option>)}</select></>}
            <button className="primary-button submit-button" disabled={!requestQuestion.trim()} onClick={() => setReceipt(true)}>Preview simulated queue receipt <span aria-hidden="true">→</span></button>
          </section>
          <section className="panel preview-panel">
            <span className="eyebrow">Scope-change preview</span><h3>{requestKind === 'new' ? 'A separate mission' : 'A linked follow-up'}</h3>
            <div className="preview-flow"><div><span>Starting point</span><strong>{requestKind === 'new' ? 'No parent artifact' : targetLabel}</strong></div><span aria-hidden="true">→</span><div><span>Proposed work</span><strong>{requestQuestion || 'Enter a question'}</strong></div></div>
            <p><strong>What changes:</strong> {requestKind === 'new' ? 'A new mission would be created with this scope.' : 'A child task would reference the exact selected version; the accepted parent finding stays available.'}</p>
            <p><strong>What stays uncertain:</strong> The requested source may remain unavailable. The reviewer can still inspect supported partial findings.</p>
            {receipt && <div className="receipt-card" role="status"><span className="eyebrow">Simulated receipt · not submitted</span><strong>SIM-QUEUE-001 · queued at this preview point</strong><p>{requestKind === 'new' ? 'New sample mission' : `Child of ${targetLabel}`}. No backend call or model work occurred.</p>{hasRecordedPath ? <><p>The fixed sample then follows task-a2: one retrieval route fails, a second finds A2, and report v2 narrows the claim.</p><button className="inline-link" onClick={() => openTask('task-a2')}>Inspect recorded child task and attempts</button></> : <p>No later attempt or outcome is scripted for this selected target.</p>}</div>}
          </section>
        </div>
      </div>
    );
  }

  function renderCompare() {
    if (!comparisonSnapshot) return <div className="state-card"><h2>Second sample run unavailable</h2><p>A run comparison needs two exact saved snapshots. No comparison is inferred from one view.</p></div>;
    const earlierRun = comparisonSnapshot;
    const earlier = earlierRun.artifacts.find(artifact => artifact.id === earlierRun.mission.reportArtifactId);
    const current = currentReport;
    const comparedRuns = [
      { run: earlierRun, report: earlier, label: 'Baseline sample run' },
      { run: snapshot, report: current, label: 'Current sample run' },
    ];
    return (
      <div className="page-stack">
        <SectionHeading eyebrow="Sample run comparison" title="What changed with another source" description="Two separate scripted mission snapshots share a question but have different source coverage. This is not a model-quality benchmark." />
        <div className="two-column comparison-grid">
          {comparedRuns.map(({ run, report, label }) => <section className="panel comparison-panel" key={run.snapshotId}><span className="eyebrow">{label}</span><h3>Report v{report?.version || '—'}</h3><p className="comparison-meta">{run.mission.id} · {run.sources.length} source{run.sources.length === 1 ? '' : 's'} · saved {formatTime(run.capturedAt)}</p><p>{report?.body || 'No saved report for this run.'}</p><div className="citation-row">{report?.citations.map(citation => <button key={`${citation.sourceId}-${citation.spanId}`} className="citation-button" onClick={() => openSource(citation.sourceId, citation.spanId)}>{citation.sourceId} · {citation.spanId}</button>)}</div><div className="limitations"><strong>Limitations</strong><p>{report?.limitations.join(' ') || 'None saved.'}</p></div></section>)}
        </div>
        <section className="panel comparison-explainer"><h3>Interpret the difference</h3><p>The baseline run had only A1 and an open request for another adult comparison. The current run also has A2, so its report can describe mixed evidence. Different source coverage explains this change; the display does not show a better model or a treatment effect. Unavailable methods remain open.</p><button className="text-button" onClick={() => openClaim('claim-benefit-v2')}>Inspect current claim and sources <span aria-hidden="true">→</span></button></section>
      </div>
    );
  }

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="brand"><span className="brand-mark" aria-hidden="true">✳</span><div><strong>Research Atlas</strong><small>Review prototype</small></div></div>
        <div className="sidebar-section-label">Workspace</div>
        <nav aria-label="Primary navigation">{navigation.map(item => <button key={item.id} aria-current={screen === item.id ? 'page' : undefined} className={`nav-item ${screen === item.id ? 'active' : ''}`} onClick={() => setScreen(item.id)}><span className="nav-indicator" /><span><strong>{item.label}</strong><small>{item.description}</small></span></button>)}</nav>
        <div className="sidebar-footer"><span className="sample-signal" /><strong>Scripted sample only</strong><p>No research host, provider, model, or sandbox is connected.</p></div>
      </aside>
      <div className="workspace">
        <header className="topbar"><div><span className="breadcrumb">Sample mission <span aria-hidden="true">/</span> {currentNav.label}</span><h1>{snapshot.mission.question}</h1></div><div className="topbar-actions"><span className="sample-tag">SAMPLE DATA</span><button className="outline-button" onClick={() => setScreen('request')}>+ New request</button></div></header>
        <div className="context-bar"><div><strong>{snapshot.mission.id}</strong><span>Version {snapshot.mission.version}</span><span>Last saved {formatTime(snapshot.capturedAt)}</span><span>{labelStatus(snapshot.mission.latencyClass)} work · {snapshot.mission.latencyBasis}</span></div><label>Preview a state <select value={previewState} onChange={event => setPreviewState(event.target.value as PreviewState)}>{previewStates.map(item => <option key={item.value} value={item.value}>{item.label}</option>)}</select></label></div>
        <main id="main-content"><PreviewNotice state={previewState} capturedAt={snapshot.capturedAt} />{previewState === 'loading' ? <div className="state-card" role="status"><div className="loading-mark" /><h2>Loading a saved snapshot</h2><p>In this preview, select “Normal sample” to inspect the fixed data. No network request is in progress.</p></div> : previewState === 'empty' ? <div className="state-card"><h2>No mission selected</h2><p>There is no run to review in this preview state. Start from a scoped question or switch back to the sample mission.</p><button className="primary-button" onClick={() => setScreen('request')}>Preview a new mission</button></div> : screen === 'overview' ? renderOverview() : screen === 'work' ? renderWork() : screen === 'evidence' ? renderEvidence() : screen === 'diagnostics' ? renderDiagnostics() : screen === 'request' ? renderRequest() : renderCompare()}</main>
      </div>
    </div>
  );
}

export default function App() {
  const [data, setData] = useState<{ snapshot: ResearchSnapshot; baseline: ResearchSnapshot } | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    const controller = new AbortController();
    Promise.all(['/sample-run.json', '/sample-run-baseline.json'].map(path =>
      fetch(path, { signal: controller.signal }).then(response => {
        if (!response.ok) throw new Error(`Sample data could not be loaded (${response.status}).`);
        return response.json() as Promise<ResearchSnapshot>;
      }),
    ))
      .then(([snapshot, baseline]) => setData({ snapshot, baseline }))
      .catch(cause => { if (!controller.signal.aborted) setError(cause instanceof Error ? cause.message : 'Sample data could not be loaded.'); });
    return () => controller.abort();
  }, []);
  if (error) return <div className="startup-state"><span className="sample-tag">SAMPLE PROTOTYPE</span><h1>Sample data unavailable</h1><p>{error}</p><p>Check that the bundled fixture is present, then relaunch the prototype.</p></div>;
  if (!data) return <div className="startup-state" role="status"><span className="sample-tag">SAMPLE PROTOTYPE</span><h1>Loading the saved sample</h1><p>No live research backend is contacted.</p></div>;
  return <ResearchApp snapshot={data.snapshot} comparisonSnapshot={data.baseline} />;
}
