/* Confirm execution labels follow exact saved records and never expose reasoning text. */
import { cleanup, render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, describe, expect, it } from 'vitest';
import type { ResearchSnapshot, SavedRecordRef } from './generated/contracts';
import { ExecutionDiagnostics, getReasoningAvailability } from './ExecutionDiagnostics';
import sample from '../public/sample-run.json';
import stage2 from '../public/stage2-recorded/child.json';
import stage3 from '../public/stage3-recorded/snapshot.json';

const ref = (kind: string, id: string, version = 1): SavedRecordRef => ({ kind, id, version });

function snapshot(): ResearchSnapshot {
  const data = structuredClone(sample) as ResearchSnapshot;
  const attempt = data.attempts[0];
  attempt.selectedModel = null;
  attempt.returnedReasoning = { availability: 'restricted', text: 'PRIVATE REASONING MUST STAY HIDDEN' };
  const instance = ref('agent_instance', attempt.agentId);
  const configuration = ref('agent_configuration', 'config-exact', 2);
  const call = ref('agent_call', 'call-exact', 3);
  const input = ref('source_copy', 'copy-exact', 4);
  const output = ref('report', 'report-exact', 5);
  data.inspection = {
    snapshotReference: ref('research_snapshot', data.snapshotId),
    timingBasis: 'observed_local',
    initialSourceIds: [],
    sources: [],
    attempts: [{ attemptId: attempt.id, instanceReference: instance, configurationReference: configuration, callReferences: [call], inputReferences: [input], acceptedOutputReferences: [output], explanation: 'Synthetic execution inspection.' }],
    acceptedOutputReferences: [output],
    records: [
      { reference: instance, label: 'Saved agent instance', data: { display_name: 'Synthetic peer' } },
      { reference: configuration, label: 'Exact configuration version', data: { instance_id: attempt.agentId, provider: 'synthetic-provider', model_id: 'model-v2', reasoning_settings: { include_reasoning: false }, skills: { 'skill-one': { version: '2' } }, permitted_tools: { 'tool-a': { version: '2' }, 'tool-b': { version: '1' } } } },
      { reference: call, label: 'Exact call manifest', data: { call_id: 'model-call-one', attempt_id: attempt.id, task_id: attempt.taskId, instance_id: attempt.agentId, configuration_id: configuration.id, provider: 'synthetic-provider', model_id: 'model-v2', reasoning_settings: { include_reasoning: true }, loaded_skill_ids: ['skill-one'], exposed_skill_ids: ['skill-one'], exposed_tools: ['tool-a'] } },
      { reference: input, label: 'Exact input copy', data: { title: 'Synthetic input' } },
      { reference: output, label: 'Exact accepted output', data: { title: 'Synthetic output' } },
      { reference: ref('model_request_intent', 'intent-one', 1), label: 'Saved request intent', data: { attempt_id: attempt.id, call_ref: call, configuration_ref: configuration, request_sha256: 'request-hash' } },
      { reference: ref('model_response_receipt', 'response-one', 2), label: 'Saved model response receipt', data: { attempt_id: attempt.id, call_ref: call, configuration_ref: configuration, intent_ref: ref('model_request_intent', 'intent-one', 1), status: 'returned', returned_model: 'model-v2', usage: { prompt_tokens: 8, completion_tokens: 4, total_tokens: 12 }, timings: { elapsed_seconds: 1.2, provider_queue_seconds: 0.1 }, reasoning_status: 'partial' } },
      { reference: ref('capability_invocation', 'tool-one'), label: 'Saved tool invocation', data: { call_id: 'model-call-one', tool_id: 'tool-a', manifest_version: '2', status: 'completed' } },
      { reference: ref('skill_invocation', 'skill-one'), label: 'Saved skill invocation', data: { call_id: 'model-call-one', skill_id: 'skill-one', skill_version: '2', status: 'failed' } },
    ],
  };
  return data;
}

afterEach(cleanup);

describe('execution diagnostics', () => {
  it('separates configured, exposed, invoked and successful work with exact per-call receipts', async () => {
    const data = snapshot();
    const attemptId = data.attempts[0].id;
    const user = userEvent.setup();
    expect(getReasoningAvailability(data, attemptId)).toEqual({ request: true, requestBasis: 'saved_call', outcome: 'partial', outcomeBasis: 'response_receipt' });
    render(<ExecutionDiagnostics snapshot={data} attemptId={attemptId} />);
    const view = screen.getByRole('region', { name: 'Execution diagnostics' });
    expect(within(view).getByText('Configured tools').closest('div')).toHaveTextContent('tool-a@2, tool-b@1');
    expect(within(view).getByText('Exposed tools in saved calls').closest('div')).toHaveTextContent('tool-a');
    expect(within(view).getByText('Configured skills').closest('div')).toHaveTextContent('skill-one@2');
    expect(within(view).getByText(/Tool tool-a@2 · Saved success \(completed\)/)).toBeInTheDocument();
    expect(within(view).getByText(/Skill skill-one@2 · Saved outcome \(failed\)/)).toBeInTheDocument();
    expect(within(view).getByText('Usage').closest('div')).toHaveTextContent('Prompt 8 tokens · Completion 4 tokens · Total 12 tokens');
    expect(within(view).getByText('Timing').closest('div')).toHaveTextContent('Elapsed 1.2 s · Provider queue 0.1 s');
    expect(within(view).getByRole('region', { name: 'Exact saved inputs' })).toHaveTextContent('Exact input copy');
    expect(within(view).getByRole('region', { name: 'Exact accepted outputs' })).toHaveTextContent('Exact accepted output');
    expect(within(view).queryByText('PRIVATE REASONING MUST STAY HIDDEN')).not.toBeInTheDocument();
    await user.click(within(view).getByText('Saved model response receipt'));
    expect(within(view).getByText('12')).toBeInTheDocument();
    expect(within(view).getByText('1.2')).toBeInTheDocument();
  });

  it('does not substitute a nearby configuration version or attribute a different attempt call', () => {
    const data = snapshot();
    const attemptId = data.attempts[0].id;
    data.inspection!.attempts[0].configurationReference = ref('agent_configuration', 'config-exact', 99);
    expect(getReasoningAvailability(data, attemptId).request).toBeNull();
    render(<ExecutionDiagnostics snapshot={data} attemptId={attemptId} />);
    const view = screen.getByRole('region', { name: 'Execution diagnostics' });
    expect(within(view).getByText(/no nearby version was substituted/)).toBeInTheDocument();
    expect(within(view).queryByText('model-v2')).not.toBeInTheDocument();
    expect(within(view).queryByText('Saved success (returned)')).not.toBeInTheDocument();
    cleanup();
    data.inspection!.attempts[0].configurationReference = ref('agent_configuration', 'config-exact', 2);
    data.inspection!.records.find(item => item.reference.kind === 'agent_call')!.data.attempt_id = 'another-attempt';
    render(<ExecutionDiagnostics snapshot={data} attemptId={attemptId} />);
    expect(screen.getByText(/1 call reference\(s\) are unavailable or do not match/)).toBeInTheDocument();
    expect(screen.queryByText('Saved success (returned)')).not.toBeInTheDocument();
  });

  it('rejects response receipts whose saved configuration or request intent does not match', () => {
    const data = snapshot();
    const attemptId = data.attempts[0].id;
    const receipt = data.inspection!.records.find(item => item.reference.kind === 'model_response_receipt')!;
    receipt.data.configuration_ref = ref('agent_configuration', 'config-exact', 99);
    expect(getReasoningAvailability(data, attemptId).outcomeBasis).toBe('attempt_field');
    render(<ExecutionDiagnostics snapshot={data} attemptId={attemptId} />);
    expect(screen.getByText(/No response receipt was included for this call/)).toBeInTheDocument();
    cleanup();
    receipt.data.configuration_ref = ref('agent_configuration', 'config-exact', 2);
    receipt.data.intent_ref = ref('model_request_intent', 'intent-one', 99);
    expect(getReasoningAvailability(data, attemptId).outcomeBasis).toBe('attempt_field');
    render(<ExecutionDiagnostics snapshot={data} attemptId={attemptId} />);
    expect(screen.getByText(/No response receipt was included for this call/)).toBeInTheDocument();
  });

  it('labels prepared configuration and missing call outcomes without inventing an invocation', () => {
    const data = snapshot();
    const attemptId = data.attempts[0].id;
    const configuration = data.inspection!.records.find(item => item.reference.kind === 'agent_configuration')!;
    configuration.data.reasoning_settings = { include_reasoning: true };
    data.inspection!.attempts[0].callReferences = [];
    data.inspection!.records = data.inspection!.records.filter(item => !['agent_call', 'model_response_receipt', 'model_request_intent', 'capability_invocation', 'skill_invocation'].includes(item.reference.kind));
    expect(getReasoningAvailability(data, attemptId)).toEqual({ request: true, requestBasis: 'saved_configuration', outcome: 'restricted', outcomeBasis: 'attempt_field' });
    render(<ExecutionDiagnostics snapshot={data} attemptId={attemptId} />);
    const view = screen.getByRole('region', { name: 'Execution diagnostics' });
    expect(within(view).getByText(/Prepared configuration does not establish a model invocation/)).toBeInTheDocument();
    expect(within(view).getByText(/No linked skill or tool invocation receipt/)).toBeInTheDocument();
    expect(within(view).getByText('Configured reasoning flag').closest('div')).toHaveTextContent('True · saved configuration; request not verified');
    expect(within(view).queryByText('PRIVATE REASONING MUST STAY HIDDEN')).not.toBeInTheDocument();
    cleanup();
    data.inspection = null;
    expect(getReasoningAvailability(data, attemptId)).toEqual({ request: null, requestBasis: 'unavailable', outcome: 'restricted', outcomeBasis: 'attempt_field' });
    render(<ExecutionDiagnostics snapshot={data} attemptId={attemptId} />);
    expect(screen.getByText('Versioned execution inspection was not included for this attempt.')).toBeInTheDocument();
  });

  it('keeps recorded offline call manifests separate from response receipts and Stage 3 callbacks', () => {
    const recorded = stage2 as ResearchSnapshot;
    const attemptId = recorded.inspection!.attempts.find(item => item.callReferences?.length)!.attemptId;
    render(<ExecutionDiagnostics snapshot={recorded} attemptId={attemptId} />);
    const stage2View = screen.getByRole('region', { name: 'Execution diagnostics' });
    expect(within(stage2View).getByText('Configured provider and model').closest('div')).toHaveTextContent('groq · openai/gpt-oss-120b');
    expect(within(stage2View).getByText(/No response receipt was included for this call/)).toBeInTheDocument();
    expect(within(stage2View).queryByText(/Saved success \(returned\)/)).not.toBeInTheDocument();
    cleanup();
    const callbacks = stage3 as ResearchSnapshot;
    render(<ExecutionDiagnostics snapshot={callbacks} attemptId={callbacks.attempts[0].id} />);
    const stage3View = screen.getByRole('region', { name: 'Execution diagnostics' });
    expect(within(stage3View).getByText(/No call manifest was included for this attempt/)).toBeInTheDocument();
    expect(within(stage3View).queryByText(/Saved success \(returned\)/)).not.toBeInTheDocument();
  });
});
