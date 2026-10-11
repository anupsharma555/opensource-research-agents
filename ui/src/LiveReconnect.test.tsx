/* A remote live page cannot invoke native commands; the trusted File menu owns
   reconnection while the page keeps the last saved state visibly uncertain. */
import { cleanup, render, screen } from '@testing-library/react';
import { afterEach, expect, it, vi } from 'vitest';
import { invoke, isTauri } from '@tauri-apps/api/core';
import { ResearchApp } from './App';
import type { ResearchSnapshot } from './generated/contracts';
import sample from '../public/sample-run.json';

vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn(), isTauri: vi.fn(() => true) }));
afterEach(() => { cleanup(); vi.clearAllMocks(); vi.mocked(isTauri).mockReturnValue(true); });

function disconnectedView() {
  const snapshot = structuredClone(sample) as ResearchSnapshot;
  snapshot.proofKind = 'live_mission_pending';
  return <ResearchApp snapshot={snapshot} live={{ runId: 'saved-run-1', model: 'saved model', runs: [], error: null, connectionError: 'The local viewer could not read its host.', form: null, requestedAgentIds: [], onSelectRun: vi.fn() }} />;
}

it('points a disconnected Mac viewer to the trusted File menu without issuing a page command', () => {
  vi.mocked(invoke).mockResolvedValue(undefined);
  render(disconnectedView());
  expect(screen.getByText(/Showing the last saved progress; current host state is unknown/)).toBeVisible();
  expect(screen.getByText(/File → Reconnect saved workspace/)).toBeVisible();
  expect(screen.queryByRole('button', { name: 'Reconnect saved workspace' })).not.toBeInTheDocument();
  expect(invoke).not.toHaveBeenCalledWith('open_live_workspace', expect.anything());
});

it('keeps the stale-state warning without Mac menu guidance in a browser viewer', () => {
  vi.mocked(isTauri).mockReturnValue(false);
  render(disconnectedView());
  expect(screen.getByText(/Showing the last saved progress; current host state is unknown/)).toBeVisible();
  expect(screen.queryByText(/File → Reconnect saved workspace/)).not.toBeInTheDocument();
  expect(invoke).not.toHaveBeenCalled();
});
