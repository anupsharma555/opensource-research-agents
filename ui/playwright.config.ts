/* Run Atlas in a real browser against a disposable, provider-free local host.
   Reports, traces and connection files stay below ignored .local/. */
import { defineConfig, devices } from '@playwright/test';

export default defineConfig({
  testDir: './e2e',
  fullyParallel: false,
  workers: 1,
  retries: 0,
  timeout: 30000,
  outputDir: '../.local/atlas-browser/results',
  reporter: [['list'], ['json', { outputFile: '.local/atlas-browser/report.json' }]],
  use: { baseURL: 'http://127.0.0.1:4177', trace: 'retain-on-failure', screenshot: 'only-on-failure' },
  projects: [{ name: 'chromium', use: { ...devices['Desktop Chrome'] } }],
  webServer: {
    command: '.venv/bin/python scripts/atlas_fixture.py --state-dir .local/atlas-browser/workspace',
    cwd: '..',
    url: 'http://127.0.0.1:4177/api/live/health',
    reuseExistingServer: false,
    timeout: 15000,
  },
});
