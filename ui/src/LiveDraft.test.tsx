/* Native draft persistence preserves unsent form state and frozen retry identity.
   Tauri IPC is mocked; Rust capability and origin checks have separate tests. */
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { invoke, isTauri } from '@tauri-apps/api/core';
import type { ResearchSnapshot } from './generated/contracts';
import type { LiveAgent, LiveFollowupRequest, LiveReceipt, LiveStartRequest } from './live';
import { LiveRequestForm } from './LiveRequestForm';
import sample from '../public/sample-run.json';

vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn(), isTauri: vi.fn(() => true) }));
afterEach(() => { cleanup(); vi.restoreAllMocks(); window.sessionStorage.removeItem('atlas-live-draft:synthetic-workspace'); vi.clearAllMocks(); vi.mocked(isTauri).mockReturnValue(true); });

const agents: LiveAgent[] = [
  { id: 'agent-synthesis', name: 'Sage', role: 'Evidence synthesis', skillVersion: 'synthesis-v1' },
  { id: 'agent-critique', name: 'Mira', role: 'Independent critique', skillVersion: 'critique-v1' },
];
const snapshot = sample as ResearchSnapshot;

function form(onSubmit: (request: LiveStartRequest | LiveFollowupRequest) => Promise<void>, workflowVersion?: string | null, receipt: LiveReceipt | null = null) {
  return <LiveRequestForm
    agents={agents} snapshot={snapshot} runId="parent-run" workspaceId="synthetic-workspace"
    parentAgentIds={['agent-synthesis', 'agent-critique']}
    parentRequiredAgentIds={['agent-critique']} retrievalRoutes={['publication-search']}
    workflowVersion={workflowVersion} receipt={receipt} receiptAgentIds={[]} submitting={false} error={null}
    onSubmit={onSubmit}
  />;
}

it('waits for native draft loading before exposing editable fields', async () => {
  let finishLoad: (value: string | null) => void = () => {};
  let saves = 0;
  vi.mocked(invoke).mockImplementation(async (command) => {
    if (command === 'load_live_draft') return new Promise<string | null>(resolve => { finishLoad = resolve; });
    if (command === 'save_live_draft') saves += 1;
    return undefined;
  });
  const onSubmit = vi.fn(async () => {});
  render(form(onSubmit));
  expect(screen.getByRole('status')).toHaveTextContent('Loading the saved local draft');
  expect(screen.queryByLabelText('Research question or follow-up')).not.toBeInTheDocument();
  expect(saves).toBe(0);
  finishLoad(null);
  expect(await screen.findByLabelText('Research question or follow-up')).toBeVisible();
  await waitFor(() => expect(saves).toBe(1));
  expect(onSubmit).not.toHaveBeenCalled();
});

it('restores and saves the unsent native question, evidence, and peer choices', async () => {
  const draft = {
    kind: 'start', question: 'Compare the supplied observations.', scope: 'Use only supplied evidence.',
    evidenceText: 'Synthetic source observation.', selectedAgents: ['agent-synthesis', 'agent-critique'],
    requiredAgents: ['agent-critique'], targetKey: '', followupRunId: null, pendingRequest: null,
  };
  const writes: string[] = [];
  vi.mocked(invoke).mockImplementation(async (command, args) => {
    if (command === 'load_live_draft') return JSON.stringify(draft);
    if (command === 'save_live_draft') {
      writes.push(String(args && !Array.isArray(args) && 'draft' in args ? args.draft : undefined));
      return undefined;
    }
    throw new Error('Unexpected native command');
  });
  const onSubmit = vi.fn(async () => {});
  render(form(onSubmit));
  expect(await screen.findByDisplayValue(draft.question)).toBeVisible();
  expect(screen.getByLabelText('Additional instructions')).toHaveValue(draft.scope);
  expect(screen.getByLabelText('Pasted source evidence')).toHaveValue(draft.evidenceText);
  expect(screen.getByRole('checkbox', { name: "Wait for Mira's contribution before completing this request" })).toBeChecked();
  await waitFor(() => expect(writes.length).toBeGreaterThan(0));
  expect(JSON.parse(writes.at(-1)!)).toEqual({ ...draft, runMode: 'review', credentialChoice: 'default' });
  expect(onSubmit).not.toHaveBeenCalled();
});

it('saves and submits an explicitly selected backup profile for a new mission', async () => {
  const writes: unknown[] = [];
  vi.mocked(invoke).mockImplementation(async (command, args) => {
    if (command === 'load_live_draft') return null;
    if (command === 'save_live_draft') {
      writes.push(JSON.parse(String(args && !Array.isArray(args) && 'draft' in args ? args.draft : undefined)));
      return undefined;
    }
    throw new Error('Unexpected native command');
  });
  const onSubmit = vi.fn(async (_request: LiveStartRequest | LiveFollowupRequest) => {});
  const view = render(form(onSubmit, 'stage4-mission-v1'));
  const profile = await screen.findByLabelText('Model account profile');
  expect(profile).toHaveValue('default');
  expect(screen.getByRole('option', { name: 'Primary (default)' })).toBeVisible();
  await userEvent.setup().type(screen.getByLabelText('Research question or follow-up'), 'Compare synthetic sources.');
  await userEvent.setup().selectOptions(profile, 'backup');
  await userEvent.setup().click(screen.getByRole('button', { name: 'Start research' }));
  await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
  const request = onSubmit.mock.calls[0][0];
  expect(request).toMatchObject({ credentialProfile: 'backup', question: 'Compare synthetic sources.' });
  expect(writes.some(value => {
    const draft = value as { credentialChoice?: string; pendingRequest?: LiveStartRequest };
    return draft.credentialChoice === 'backup' && draft.pendingRequest?.requestId === request.requestId && draft.pendingRequest.credentialProfile === 'backup';
  })).toBe(true);
  view.rerender(form(onSubmit, 'stage4-mission-v1', {
    requestId: request.requestId, runId: 'synthetic-run', taskId: 'synthetic-task', status: 'queued',
  }));
  expect(await screen.findByText('Requested model account profile: Backup.')).toBeVisible();
});

it('keeps account and agent choices visible while technical research settings are collapsed', async () => {
  vi.mocked(invoke).mockImplementation(async (command) => command === 'load_live_draft' ? null : undefined);
  const onSubmit = vi.fn(async () => {});
  render(form(onSubmit, 'stage4-mission-v1'));
  const profile = await screen.findByLabelText('Model account profile');
  expect(profile).toBeVisible();
  expect(profile.closest('details')).toBeNull();
  expect(screen.getByRole('group', { name: 'Starting agents' })).toBeVisible();
  const settings = screen.getByText('Research settings').closest('details');
  expect(settings).not.toHaveAttribute('open');
  expect(settings).toHaveTextContent('publication-search');
  expect(settings).toHaveTextContent('stage4-mission-v1');
  await userEvent.setup().click(screen.getByText('Research settings'));
  expect(settings).toHaveAttribute('open');
  expect(onSubmit).not.toHaveBeenCalled();
});

it('omits the default profile on a follow-up so the host inherits its parent', async () => {
  vi.mocked(invoke).mockImplementation(async (command) => command === 'load_live_draft' ? null : undefined);
  const onSubmit = vi.fn(async (_request: LiveStartRequest | LiveFollowupRequest) => {});
  render(form(onSubmit, 'stage4-mission-v1'));
  await screen.findByLabelText('Model account profile');
  await userEvent.setup().click(screen.getByRole('button', { name: 'Follow up on saved work' }));
  expect(screen.getByRole('option', { name: 'Same as earlier run (default)' })).toBeVisible();
  await userEvent.setup().type(screen.getByLabelText('Research question or follow-up'), 'Recheck the saved claim.');
  const target = snapshot.claims[0];
  await userEvent.setup().selectOptions(screen.getByLabelText('Work to continue'), `${target.id}:${target.version}`);
  await userEvent.setup().click(screen.getByRole('button', { name: 'Submit follow-up' }));
  await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
  expect(onSubmit.mock.calls[0][0]).toMatchObject({
    parentRunId: 'parent-run', parentSnapshotId: snapshot.snapshotId,
    targetId: target.id, targetVersion: target.version,
  });
  expect(onSubmit.mock.calls[0][0]).not.toHaveProperty('credentialProfile');
});

it('resets an explicit credential choice when a follow-up chooses a different parent', async () => {
  const draft = {
    kind: 'followup', question: 'Recheck a prior run.', scope: '', evidenceText: '',
    selectedAgents: ['agent-synthesis'], requiredAgents: [],
    targetKey: `${snapshot.claims[0].id}:${snapshot.claims[0].version}`,
    followupRunId: 'other-run', credentialChoice: 'backup', pendingRequest: null,
  };
  vi.mocked(invoke).mockImplementation(async (command) => command === 'load_live_draft' ? JSON.stringify(draft) : undefined);
  const onSubmit = vi.fn(async (_request: LiveStartRequest | LiveFollowupRequest) => {});
  render(form(onSubmit, 'stage4-mission-v1'));
  expect(await screen.findByLabelText('Model account profile')).toHaveValue('backup');
  await userEvent.setup().click(screen.getByRole('button', { name: 'Continue from selected run' }));
  expect(screen.getByLabelText('Model account profile')).toHaveValue('default');
  expect(screen.getByLabelText('Work to continue')).toHaveValue('');
  expect(onSubmit).not.toHaveBeenCalled();
});

it('keeps the profile choice in a browser session draft without invoking native commands', async () => {
  vi.mocked(isTauri).mockReturnValue(false);
  const onSubmit = vi.fn(async (_request: LiveStartRequest | LiveFollowupRequest) => {});
  const view = render(form(onSubmit, 'stage4-mission-v1'));
  await userEvent.setup().type(screen.getByLabelText('Research question or follow-up'), 'Preserve the backup choice.');
  await userEvent.setup().selectOptions(screen.getByLabelText('Model account profile'), 'backup');
  await waitFor(() => expect(JSON.parse(window.sessionStorage.getItem('atlas-live-draft:synthetic-workspace') || '{}').credentialChoice).toBe('backup'));
  view.unmount();
  render(form(onSubmit, 'stage4-mission-v1'));
  expect(screen.getByLabelText('Research question or follow-up')).toHaveValue('Preserve the backup choice.');
  expect(screen.getByLabelText('Model account profile')).toHaveValue('backup');
  expect(invoke).not.toHaveBeenCalled();
});

it('keeps legacy supplied-evidence requests free of credential-profile fields', async () => {
  vi.mocked(invoke).mockImplementation(async (command) => command === 'load_live_draft' ? null : undefined);
  const onSubmit = vi.fn(async (_request: LiveStartRequest | LiveFollowupRequest) => {});
  render(form(onSubmit, 'supplied-evidence-v1'));
  expect(await screen.findByLabelText('Research question or follow-up')).toBeVisible();
  expect(screen.queryByLabelText('Model account profile')).not.toBeInTheDocument();
  await userEvent.setup().type(screen.getByLabelText('Research question or follow-up'), 'Assess the supplied evidence.');
  await userEvent.setup().click(screen.getByRole('button', { name: 'Start research' }));
  await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
  expect(onSubmit.mock.calls[0][0]).not.toHaveProperty('credentialProfile');
});

it('keeps the exact pending follow-up identity until an explicit retry', async () => {
  const target = snapshot.claims[0];
  const pendingRequest = {
    requestId: 'request-synthetic-fixed', question: 'Check this saved claim.',
    scope: 'Compare the earlier evidence.', agentIds: ['agent-synthesis', 'agent-critique'],
    requiredAgentIds: ['agent-critique'], parentRunId: 'parent-run',
    parentSnapshotId: snapshot.snapshotId, targetId: target.id, targetVersion: target.version,
    credentialProfile: 'backup',
  };
  const draft = {
    kind: 'followup', question: pendingRequest.question, scope: pendingRequest.scope,
    evidenceText: '', selectedAgents: pendingRequest.agentIds,
    requiredAgents: pendingRequest.requiredAgentIds,
    targetKey: `${target.id}:${target.version}`, followupRunId: 'parent-run', pendingRequest,
  };
  vi.mocked(invoke).mockImplementation(async (command) =>
    command === 'load_live_draft' ? JSON.stringify(draft) : undefined
  );
  const onSubmit = vi.fn(async () => {});
  render(form(onSubmit, 'stage4-mission-v1'));
  await screen.findByRole('button', { name: 'Retry same request' });
  expect(screen.getByLabelText('Model account profile')).toHaveValue('backup');
  expect(screen.getByLabelText('Work to continue')).toHaveValue(draft.targetKey);
  expect(onSubmit).not.toHaveBeenCalled();
  await userEvent.setup().click(screen.getByRole('button', { name: 'Retry same request' }));
  expect(onSubmit).toHaveBeenCalledTimes(1);
  expect(onSubmit).toHaveBeenCalledWith(pendingRequest);
});

it.each([
  ['rejected native read', new Error('Draft access unavailable')],
  ['malformed saved JSON', '{not-json'],
  ['invalid saved draft shape', '{}'],
])('%s blocks autosave and submission until an explicit successful retry', async (_label, firstResult) => {
  let loads = 0;
  let saves = 0;
  let savedPayload: unknown = null;
  const recovered = {
    kind: 'start', question: 'Retained earlier draft.', scope: '', evidenceText: '',
    selectedAgents: ['agent-synthesis'], requiredAgents: [], targetKey: '',
    followupRunId: null, pendingRequest: null,
  };
  vi.mocked(invoke).mockImplementation(async (command, args) => {
    if (command === 'load_live_draft') {
      loads += 1;
      if (loads === 1) {
        if (firstResult instanceof Error) throw firstResult;
        return firstResult;
      }
      return firstResult instanceof Error ? JSON.stringify(recovered) : null;
    }
    if (command === 'save_live_draft') {
      saves += 1;
      savedPayload = JSON.parse(String(args && !Array.isArray(args) && 'draft' in args ? args.draft : undefined));
      return undefined;
    }
    throw new Error('Unexpected native command');
  });
  const onSubmit = vi.fn(async () => {});
  render(form(onSubmit));
  expect(await screen.findByText('The saved local draft could not be loaded. It has not been changed.')).toBeVisible();
  expect(loads).toBe(1);
  expect(saves).toBe(0);
  expect(screen.queryByLabelText('Research question or follow-up')).not.toBeInTheDocument();
  expect(onSubmit).not.toHaveBeenCalled();
  await userEvent.setup().click(screen.getByRole('button', { name: 'Retry loading draft' }));
  expect(await screen.findByLabelText('Research question or follow-up')).toBeVisible();
  await waitFor(() => expect(saves).toBe(1));
  if (firstResult instanceof Error) {
    expect(screen.getByLabelText('Research question or follow-up')).toHaveValue(recovered.question);
    expect(savedPayload).toEqual({ ...recovered, runMode: 'solo', credentialChoice: 'default' });
  }
  expect(loads).toBe(2);
  expect(onSubmit).not.toHaveBeenCalled();
});

it('saves the frozen request ID before sending and retries that ID after storage recovers', async () => {
  let storageUnavailable = true;
  const frozen: unknown[] = [];
  vi.mocked(invoke).mockImplementation(async (command, args) => {
    if (command === 'load_live_draft') return null;
    if (command === 'save_live_draft') {
      const raw = args && !Array.isArray(args) && 'draft' in args ? args.draft : undefined;
      const request = JSON.parse(String(raw)).pendingRequest;
      if (request) frozen.push(request);
      if (storageUnavailable) throw new Error('Native draft unavailable');
      return undefined;
    }
    throw new Error('Unexpected native command');
  });
  const onSubmit = vi.fn(async () => {});
  render(form(onSubmit));
  await screen.findByText('This draft could not be saved locally. Keep this window open while revising it.');
  await userEvent.setup().type(screen.getByLabelText('Research question or follow-up'), 'A new synthetic question.');
  await userEvent.setup().click(screen.getByRole('button', { name: 'Start research' }));
  await waitFor(() => expect(frozen.length).toBeGreaterThan(0));
  expect(onSubmit).not.toHaveBeenCalled();
  const first = frozen[0];
  storageUnavailable = false;
  await userEvent.setup().click(screen.getByRole('button', { name: 'Retry same request' }));
  await waitFor(() => expect(onSubmit).toHaveBeenCalledTimes(1));
  expect(onSubmit).toHaveBeenCalledWith(first);
});

it('saves the browser frozen request before the submission handler can dispatch it', async () => {
  vi.mocked(isTauri).mockReturnValue(false);
  const user = userEvent.setup();
  let savedAtDispatch: LiveStartRequest | null = null;
  const onSubmit = vi.fn(async (request: LiveStartRequest | LiveFollowupRequest) => {
    savedAtDispatch = JSON.parse(window.sessionStorage.getItem('atlas-live-draft:synthetic-workspace')!).pendingRequest;
    expect(savedAtDispatch).toEqual(request);
  });
  render(form(onSubmit));
  await user.type(screen.getByLabelText('Research question or follow-up'), 'Synthetic browser request.');
  await user.click(screen.getByRole('button', { name: 'Start research' }));
  expect(onSubmit).toHaveBeenCalledTimes(1);
  expect(savedAtDispatch).not.toBeNull();
});

it('does not dispatch a browser command if its frozen identity cannot be saved', async () => {
  vi.mocked(isTauri).mockReturnValue(false);
  const user = userEvent.setup();
  const onSubmit = vi.fn(async () => {});
  render(form(onSubmit));
  await user.type(screen.getByLabelText('Research question or follow-up'), 'Synthetic browser request.');
  vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new DOMException('Synthetic quota failure', 'QuotaExceededError'); });
  await user.click(screen.getByRole('button', { name: 'Start research' }));
  expect(onSubmit).not.toHaveBeenCalled();
  expect(screen.getByText(/This draft could not be saved/)).toBeVisible();
});
