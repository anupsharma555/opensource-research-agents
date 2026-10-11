/* Compare two saved views without treating source counts or report versions as
   scientific improvement. Every citation resolves within its own snapshot. */
import { useId, useState } from 'react';
import type { ResearchSnapshot } from './generated/contracts';
import { ReportMarkdown } from './ReportMarkdown';
import { TermHelp } from './TermHelp';
import './ReportComparison.css';

type Snapshot = ResearchSnapshot;
type Report = Snapshot['artifacts'][number];
type Citation = Report['citations'][number];

export type ReportComparisonProps = {
  currentSnapshot: Snapshot;
  parentSnapshot?: Snapshot | null;
  mode?: 'followup' | 'sample';
  parentState?: 'ready' | 'loading' | 'error';
};

function savedTime(value: string) {
  const date = new Date(value);
  return Number.isNaN(date.getTime())
    ? 'Time unavailable'
    : new Intl.DateTimeFormat('en-US', {
      month: 'short', day: 'numeric', year: 'numeric', hour: 'numeric', minute: '2-digit', timeZoneName: 'short',
    }).format(date);
}

function reportSelection(snapshot: Snapshot) {
  const issues: string[] = [];
  const reports: Report[] = [];
  const accepted = snapshot.inspection?.acceptedOutputReferences.filter(ref => ref.kind === 'report');
  if (accepted) {
    const seen = new Set<string>();
    for (const ref of accepted) {
      const key = JSON.stringify([ref.id, ref.version]);
      if (seen.has(key)) {
        issues.push('The same saved answer is listed more than once in the records; it is shown once here.');
        continue;
      }
      seen.add(key);
      const matches = snapshot.artifacts.filter(item => item.id === ref.id && item.version === ref.version);
      if (matches.length === 1) reports.push(matches[0]);
      else issues.push(matches.length ? 'An answer is recorded, but more than one text matches its saved version. None was chosen.' : 'An answer is recorded, but its exact saved text is missing from this view.');
    }
  } else {
    const candidates = snapshot.artifacts.filter(item => item.kind === 'report' || item.id === snapshot.mission.reportArtifactId);
    const counts = new Map<string, number>();
    const seen = new Set<string>();
    for (const report of candidates) {
      const key = JSON.stringify([report.id, report.version]);
      counts.set(key, (counts.get(key) ?? 0) + 1);
    }
    for (const report of candidates) {
      const key = JSON.stringify([report.id, report.version]);
      if (seen.has(key)) continue;
      seen.add(key);
      if (counts.get(key) === 1) reports.push(report);
      else {
        issues.push('More than one answer has the same saved ID and version. None was chosen.');
      }
    }
  }
  return { reports, issues, acceptanceVerified: Boolean(accepted) };
}

function citedPassage(snapshot: Snapshot, citation: Citation) {
  const sources = snapshot.sources.filter(source => source.id === citation.sourceId);
  if (sources.length !== 1) return { source: undefined, passage: undefined, reason: sources.length ? 'source identity unclear' : 'source unavailable' };
  const passages = sources[0].passages.filter(passage => passage.spanId === citation.spanId);
  return {
    source: sources[0],
    passage: passages.length === 1 ? passages[0] : undefined,
    reason: passages.length === 1 ? null : passages.length ? 'more than one matching excerpt' : 'excerpt unavailable',
  };
}

function CitationCard({ snapshot, citation, index }: { snapshot: Snapshot; citation: Citation; index: number }) {
  const { source, passage, reason } = citedPassage(snapshot, citation);
  const origins = snapshot.inspection?.sources.filter(item => item.sourceId === citation.sourceId
    && source && item.copyReference.kind === 'source_copy'
    && item.copyReference.id === source.copyId && item.copyReference.version === source.version) ?? [];
  const reused = origins.length === 1 && origins[0].origin === 'reused';
  return <li className="report-comparison-citation">
    <details>
      <summary><span>Citation {index + 1}</span><strong>{source?.title ?? 'Source missing or unclear'}</strong><small>{passage?.locator ?? reason}</small></summary>
      {passage ? <><p>{reused ? 'This source was reused from earlier work.' : 'This is the exact excerpt saved with this answer.'} Check whether this text supports the answer.</p><blockquote>{passage.text}</blockquote></>
        : <p>This citation cannot be shown: {reason}. The other run’s sources are not substituted.</p>}
      <details className="report-comparison-technical"><summary>Exact source and passage details</summary><p>Source {citation.sourceId} · passage {citation.spanId}</p>{source && <p>Copy {source.copyId} · version {source.version}</p>}{reused && origins[0].fromSnapshotId && <p>Reused from snapshot {origins[0].fromSnapshotId}</p>}</details>
    </details>
  </li>;
}

function ReportCard({ snapshot, report, accepted, view }: { snapshot: Snapshot; report: Report; accepted: boolean; view: 'answer' | 'evidence' }) {
  const citedCopies = new Set(report.citations.map(citation => citation.sourceId)).size;
  return <article className="report-comparison-report">
    <div className="report-comparison-report-heading"><div><span className="eyebrow">{accepted ? <>Saved as a run output<TermHelp term="accepted report" /></> : 'Saved text · output status unknown'}</span><h4>{report.title}</h4></div><span className="report-comparison-version">Version {report.version}</span></div>

    {view === 'answer' && <><div className="report-comparison-body"><ReportMarkdown text={report.body} /></div>
    <div className="report-comparison-limits"><h5>Limitations stated in the answer</h5>{report.limitations.length
      ? report.limitations.map((limitation, index) => <ReportMarkdown key={index} text={limitation} />)
      : <p>No separate limitations were saved. Read the answer and its sources for qualifications.</p>}</div></>}
    {view === 'evidence' && <section className="report-comparison-citations" aria-label={`Citations for ${report.title}`}><h5>Check the cited evidence ({report.citations.length})</h5>{report.citations.length
      ? <ol>{report.citations.map((citation, index) => <CitationCard key={`${citation.sourceId}:${citation.spanId}:${index}`} snapshot={snapshot} citation={citation} index={index} />)}</ol>
      : <p>No source links were saved with this answer. References written in the answer may still name sources, but they are not linked here to exact saved passages.</p>}</section>}
    <details className="report-comparison-technical"><summary>Answer record details</summary><p>{report.citations.length} citations to {citedCopies} saved source IDs. These are not counts of independent studies.</p><p>Report {report.id} · version {report.version}</p><p>Snapshot {snapshot.snapshotId} · mission {snapshot.mission.id}</p><p>The answer’s own save time is unavailable here. The date above belongs to the saved view, not necessarily to the answer.</p></details>
  </article>;
}

function SnapshotColumn({ snapshot, label }: { snapshot?: Snapshot | null; label: string }) {
  const detailId = useId();
  const [view, setView] = useState<'request' | 'answer' | 'evidence' | null>(null);
  if (!snapshot) return <section className="report-comparison-column report-comparison-empty" aria-label={label}><span className="eyebrow">{label}</span><h3>Saved work unavailable</h3><p>The exact saved version of this run is not available to compare.</p></section>;
  const selection = reportSelection(snapshot);
  const savedLinks = selection.reports.flatMap(report => report.citations);
  const checkableLinks = savedLinks.filter(citation => citedPassage(snapshot, citation).passage).length;
  const unavailableLinks = savedLinks.length - checkableLinks;
  const states = { active: 'Run open', waiting: 'Waiting', completed: 'Marked completed', cancelled: 'Cancelled' };
  const stateExplanation = snapshot.mission.status === 'active' ? 'This run was still active when this view was saved.'
    : snapshot.mission.status === 'waiting' ? 'This run was waiting when this view was saved.'
      : snapshot.mission.status === 'cancelled' ? 'This run is marked cancelled.'
        : 'This run is marked completed. That status does not supply the missing answer text.';
  return <section className="report-comparison-column" aria-label={label}>
    <header><div className="report-comparison-run-heading"><h3>{label}</h3><span className="report-comparison-state">{states[snapshot.mission.status]}</span></div>
      <dl className="report-comparison-times"><div><dt>Run started</dt><dd><time dateTime={snapshot.mission.startedAt}>{savedTime(snapshot.mission.startedAt)}</time></dd></div><div><dt>View saved</dt><dd><time dateTime={snapshot.capturedAt}>{savedTime(snapshot.capturedAt)}</time></dd></div></dl>
      {selection.reports.length > 0 && <p className="report-comparison-availability"><strong>{selection.reports.length === 1 ? '1 answer available' : `${selection.reports.length} answers available`}</strong><span>{selection.acceptanceVerified ? 'Recorded as run output' : 'Output status not verified'} · {checkableLinks} checkable source {checkableLinks === 1 ? 'link' : 'links'}{unavailableLinks > 0 && ` · ${unavailableLinks} saved ${unavailableLinks === 1 ? 'link unavailable' : 'links unavailable'}`}</span>{savedLinks.length === 0 && <span>Source references written in the answer are not linked here to exact saved passages.</span>}</p>}
    </header>
    {!selection.acceptanceVerified && selection.reports.length > 0 && <p className="report-comparison-caveat">The answer text is saved, but the records do not confirm whether it was accepted as an output of this run.</p>}
    {selection.reports.length > 1 && <p className="report-comparison-caveat">{selection.reports.length} answers are saved separately. No single main answer is identified here.</p>}
    {selection.issues.map((issue, index) => <p className="report-comparison-caveat" key={index}>{issue}</p>)}
    {!selection.reports.length && <div className="report-comparison-no-report"><h4>{selection.issues.length ? 'Saved answer unavailable' : 'No answer available to compare'}</h4><p>{selection.issues.length ? 'The exact answer must be available before it can be compared.' : selection.acceptanceVerified ? 'No answer is recorded as an output in this saved view.' : 'No answer text is included in this saved view; whether one was produced is unknown.'}</p><p>{stateExplanation}</p></div>}
    <div className="report-comparison-actions" role="group" aria-label={`Read ${label.toLowerCase()} details`}>
      {(['request', 'answer', 'evidence'] as const).map(kind => <button key={kind} type="button" aria-controls={detailId} aria-expanded={view === kind} disabled={kind !== 'request' && selection.reports.length === 0} onClick={() => setView(view === kind ? null : kind)}>{kind === 'request' ? 'Read request' : kind === 'answer' ? 'Read answer' : 'Check cited evidence'}</button>)}
    </div>
    <div id={detailId} className="report-comparison-selected">
      {view === 'request' && <section className="report-comparison-question" aria-label={`${label} request`}><h4>{label} · saved request</h4><blockquote>{snapshot.mission.question || 'The original request was not saved.'}</blockquote>{snapshot.mission.scope && <><h5>Additional instructions for this run</h5><p>{snapshot.mission.scope}</p></>}</section>}
      {(view === 'answer' || view === 'evidence') && selection.reports.length > 0 && <section aria-label={`${label} ${view === 'answer' ? 'answers' : 'cited evidence'}`}><h4 className="report-comparison-detail-heading">{label} · {view === 'answer' ? 'saved answer' : 'cited evidence'}</h4><div className="report-comparison-report-list">{selection.reports.map(report => <ReportCard key={`${report.id}:${report.version}`} snapshot={snapshot} report={report} accepted={selection.acceptanceVerified} view={view} />)}</div></section>}
    </div>
    <details className="report-comparison-technical"><summary>Run and saved-view details</summary><p>Snapshot {snapshot.snapshotId} · mission {snapshot.mission.id}</p><p>{snapshot.sources.length} saved source {snapshot.sources.length === 1 ? 'copy' : 'copies'} · {selection.reports.length} {selection.reports.length === 1 ? 'answer' : 'answers'} shown</p>{snapshot.parentSnapshotId && <p>Saved earlier version {snapshot.parentSnapshotId}</p>}</details>
  </section>;
}

export function ReportComparison({ currentSnapshot, parentSnapshot, mode = 'followup', parentState = 'ready' }: ReportComparisonProps) {
  const exactParent = !currentSnapshot.parentSnapshotId || parentSnapshot?.snapshotId === currentSnapshot.parentSnapshotId;
  const parent = parentState === 'ready' && exactParent ? parentSnapshot : null;
  const paired = Boolean(parent);
  const parentLabel = mode === 'sample' ? 'Earlier sample run' : 'Earlier run';
  const currentLabel = mode === 'sample' ? 'Current sample run' : currentSnapshot.parentSnapshotId ? 'This follow-up' : 'This run';
  const notice = parentState === 'loading' ? 'Loading the earlier run’s saved work. The current run remains readable below.'
    : parentState === 'error' ? 'The earlier run’s saved work could not be loaded. The current run remains readable below.'
      : parentSnapshot && !exactParent ? 'The loaded earlier run does not match the version linked by this follow-up. It is not used for this comparison.'
        : !parentSnapshot ? mode === 'sample' ? 'No baseline sample is available to compare.' : 'The earlier run’s saved work is not available here.'
          : mode === 'followup' && !currentSnapshot.parentSnapshotId ? 'No saved link confirms that these runs belong together. They are displayed separately for inspection.'
            : null;
  return <div className="page-stack report-comparison">
    <header className="report-comparison-intro"><p>{mode === 'followup' && paired && currentSnapshot.parentSnapshotId ? 'The earlier run is the work this follow-up continues. ' : ''}Check answer availability on each side, then open a request, answer or its cited evidence.</p></header>
    <div className="report-comparison-path" aria-label="Comparison order"><span>{parentLabel}{mode === 'followup' && paired && currentSnapshot.parentSnapshotId && <TermHelp term="earlier run" />}</span><span aria-hidden="true">→</span><span>{currentLabel}</span></div>
    {notice && <div className="report-comparison-notice" role={parentState === 'loading' ? 'status' : undefined}>{notice}</div>}
    <div className="report-comparison-grid"><SnapshotColumn key={`parent:${parent?.mission.id ?? "unavailable"}`} snapshot={parent} label={parentLabel} /><SnapshotColumn key={`current:${currentSnapshot.mission.id}`} snapshot={currentSnapshot} label={currentLabel} /></div>
    {paired && reportSelection(parent!).reports.length > 0 && reportSelection(currentSnapshot).reports.length > 0 && <p className="report-comparison-footer">A later answer is not necessarily better. Compare what changed, whether the cited text supports it, and what remains uncertain.</p>}
  </div>;
}
