/* Show backend-saved agent steps as inspection, separate from research findings.
   Actions and explanations are displayed as recorded; exact references resolve
   through the existing saved-record view within this snapshot only. */
import type { ResearchSnapshot } from './generated/contracts';
import { RecordGroup } from './InspectionDetails';

const proofLabel: Record<NonNullable<ResearchSnapshot['proofKind']>, string> = {
  synthetic_sample: 'Simulated sample',
  offline_test: 'Recorded offline test; times and actions are fixture evidence',
  live_model_on_supplied_evidence: 'Live model work on supplied evidence',
  live_mission_pending: 'Live mission accepted; no completed model or source result is implied',
  live_model_with_retrieval: 'Live model work with saved retrieval records; inspect source and call receipts for exact actions',
};

export function SavedAgentSteps({ snapshot }: { snapshot: ResearchSnapshot }) {
  const inspection = snapshot.inspection;
  const steps = [...(inspection?.steps ?? [])].sort((a, b) => a.sequence - b.sequence);
  const proof = snapshot.proofKind ?? 'synthetic_sample';

  return <details className="inline-detail saved-agent-steps">
    <summary>Saved agent steps{steps.length ? ' (' + steps.length + ')' : ''}</summary>
    <div className="inline-detail-body">
      <p>These are saved actions and outcomes, separate from the research findings. {proofLabel[proof]}</p>
      {!inspection ? <p>Step inspection was not included in this snapshot.</p>
        : steps.length === 0 ? <p>No saved agent steps were included in this snapshot.</p>
          : <ol className="saved-agent-step-list">{steps.map(step => <li key={step.id}>
            <details className="inline-detail saved-agent-step">
              <summary>Step {step.sequence} · {step.actorName?.trim() || 'Actor not recorded'} · {step.action} · {step.outcome}</summary>
              <div className="inline-detail-body">
                <p>{step.summary}</p>
                <dl className="detail-list">
                  <div><dt>Action</dt><dd>{step.action}</dd></div>
                  <div><dt>Outcome</dt><dd>{step.outcome}</dd></div>
                  <div><dt>Recorded at</dt><dd><time dateTime={step.recordedAt}>{step.recordedAt}</time>{inspection.timingBasis === 'controlled_clock' ? ' · controlled test time' : ''}</dd></div>
                  <div><dt>Saved explanation</dt><dd>{step.reasonSummary?.trim() || 'No saved explanation was recorded for this step.'}</dd></div>
                  <div><dt>Next action</dt><dd>{step.nextAction?.trim() || 'No next action was recorded.'}</dd></div>
                  <div><dt>Proof</dt><dd>{step.proof}</dd></div>
                </dl>
                <RecordGroup snapshot={snapshot} title="Exact saved inputs" references={step.inputReferences ?? []} />
                <RecordGroup snapshot={snapshot} title="Exact saved outputs" references={step.outputReferences ?? []} />
              </div>
            </details>
          </li>)}</ol>}
    </div>
  </details>;
}
