/* Bundled native pages use the selected directory; connected loopback pages navigate in place. */
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { invoke, isTauri } from '@tauri-apps/api/core';
import { LiveWorkspaceAccess } from './LiveWorkspaceAccess';

vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn(), isTauri: vi.fn(() => true) }));
afterEach(() => { cleanup(); vi.unstubAllGlobals(); vi.clearAllMocks(); vi.mocked(isTauri).mockReturnValue(true); });

function at(url: string) {
  const location = new URL(url);
  vi.stubGlobal('location', location);
}

it.each(['request', 'history'] as const)('connects the selected directory to the %s view', async view => {
  at('tauri://localhost/');
  vi.mocked(invoke).mockResolvedValueOnce(null).mockResolvedValueOnce(undefined);
  render(<LiveWorkspaceAccess view={view} />);
  await userEvent.type(screen.getByRole('textbox', { name: 'Saved local workspace directory' }), '/tmp/saved-research');
  await userEvent.click(screen.getByRole('button', { name: 'Connect selected workspace' }));
  expect(invoke).toHaveBeenCalledWith('connect_live_workspace', { stateDir: '/tmp/saved-research', view, runId: null });
});

it('explains when a saved selection cannot be reopened', async () => {
  at('tauri://localhost/');
  vi.mocked(invoke).mockResolvedValueOnce(null).mockRejectedValueOnce('The selected workspace is unavailable.');
  render(<LiveWorkspaceAccess view="history" />);
  await userEvent.click(screen.getByRole('button', { name: 'Retry saved selection' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('The selected workspace is unavailable.');
});

it('does not invoke native actions from a standalone browser example', () => {
  at('https://example.test/?recorded=stage3');
  vi.mocked(isTauri).mockReturnValue(false);
  render(<LiveWorkspaceAccess view="request" />);
  expect(screen.getByText('Open the local live app to access these tasks.')).toBeVisible();
  expect(invoke).not.toHaveBeenCalled();
});

it.each(['request', 'history'] as const)('opens the connected %s view from a native loopback recorded page without IPC', view => {
  at('http://127.0.0.1:51986/?recorded=stage3&returnRunId=run-parent_1');
  render(<LiveWorkspaceAccess view={view} />);
  expect(screen.getByRole('link', { name: 'Open connected workspace' })).toHaveAttribute('href', `/live?view=${view}&runId=run-parent_1`);
  expect(screen.queryByRole('textbox', { name: 'Saved local workspace directory' })).not.toBeInTheDocument();
  expect(screen.queryByRole('button', { name: 'Retry saved selection' })).not.toBeInTheDocument();
  expect(invoke).not.toHaveBeenCalled();
});

it('opens the same loopback route in a browser and never invokes native commands', () => {
  at('http://127.0.0.1:51986/?recorded=stage2&returnRunId=run-browser');
  vi.mocked(isTauri).mockReturnValue(false);
  render(<LiveWorkspaceAccess view="request" />);
  expect(screen.getByRole('link', { name: 'Open connected workspace' })).toHaveAttribute('href', '/live?view=request&runId=run-browser');
  expect(invoke).not.toHaveBeenCalled();
});

it('does not expose native selection commands to a different remote Tauri page', () => {
  at('http://localhost:3020/?recorded=stage3');
  render(<LiveWorkspaceAccess view="request" />);
  expect(screen.getByText('Open the local live app to access these tasks.')).toBeVisible();
  expect(screen.queryByRole('textbox', { name: 'Saved local workspace directory' })).not.toBeInTheDocument();
  expect(invoke).not.toHaveBeenCalled();
});
