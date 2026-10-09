/* Check that returning to the live workspace navigates only and explains a
   missing launch connection without silently starting a host or research task. */
import { cleanup, render, screen } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { afterEach, expect, it, vi } from 'vitest';
import { invoke, isTauri } from '@tauri-apps/api/core';
import { LiveWorkspaceAccess } from './LiveWorkspaceAccess';

vi.mock('@tauri-apps/api/core', () => ({ invoke: vi.fn(), isTauri: vi.fn(() => true) }));
afterEach(() => { cleanup(); vi.clearAllMocks(); });

it.each(['request', 'history'] as const)('opens only the selected %s view of the existing native connection', async view => {
  vi.mocked(invoke).mockResolvedValue(undefined);
  render(<LiveWorkspaceAccess view={view} />);
  await userEvent.click(screen.getByRole('button', { name: 'Open live workspace' }));
  expect(invoke).toHaveBeenCalledExactlyOnceWith('open_live_workspace', { view });
});

it('explains when a native window was launched without a live connection', async () => {
  vi.mocked(invoke).mockRejectedValue('This window was opened without a live connection.');
  render(<LiveWorkspaceAccess view="history" />);
  await userEvent.click(screen.getByRole('button', { name: 'Open live workspace' }));
  expect(await screen.findByRole('alert')).toHaveTextContent('This window was opened without a live connection.');
});

it('does not invoke native actions from a standalone browser example', () => {
  vi.mocked(isTauri).mockReturnValueOnce(false);
  render(<LiveWorkspaceAccess view="request" />);
  expect(screen.getByText('Open the local live app to access these tasks.')).toBeVisible();
  expect(invoke).not.toHaveBeenCalled();
});
