/* Exercise real browser, cookie, routing and durable command boundaries.
   Only the disposable offline host is contacted; traces remain local. */
import { test, expect } from '@playwright/test';
import AxeBuilder from '@axe-core/playwright';
import { readFile } from 'node:fs/promises';
import { execFileSync } from 'node:child_process';

test.beforeEach(async ({ page }) => {
  const connection = JSON.parse(await readFile('.local/atlas-browser/workspace/connection.json', 'utf8'));
  await page.goto(`${connection.launchUrl}&test=offline`);
  await expect(page.getByRole('button', { name: 'Research review', exact: true })).toBeVisible();
  await expect(page).not.toHaveURL(/token=/);
});

test('navigates evidence, returns through history, preserves a draft and accepts one exact follow-up', async ({ page }) => {
  await page.getByRole('button', { name: 'Evidence', exact: true }).click();
  await expect(page).toHaveURL(/view=evidence/);
  await expect(page.getByRole('button', { name: 'Evidence', exact: true })).toHaveAttribute('aria-current', 'page');
  await page.getByRole('button', { name: /Adult study B · independent randomized comparison/ }).click();
  await expect(page.getByRole('heading', { name: 'Adult study B · independent randomized comparison' })).toBeVisible();
  await page.getByText('Source origin, copy and discovery records').click();
  await expect(page.getByText(/Source ID: A2 · copy ID:/)).toBeVisible();
  await page.getByRole('button', { name: 'Communication', exact: true }).click();
  await page.goBack();
  await expect(page.getByRole('button', { name: 'Evidence', exact: true })).toHaveAttribute('aria-current', 'page');
  await page.reload();
  await expect(page.getByRole('button', { name: 'Evidence', exact: true })).toHaveAttribute('aria-current', 'page');
  await page.getByRole('button', { name: 'Start or follow up', exact: true }).click();
  await page.getByRole('button', { name: 'Follow up on saved work' }).click();
  const question = `Synthetic follow-up ${Date.now()}`;
  await page.getByLabel('Research question or follow-up').fill(question);
  await page.getByLabel('Work to continue').selectOption({ index: 1 });
  await page.reload();
  await expect(page.getByLabel('Research question or follow-up')).toHaveValue(question);
  const post = page.waitForResponse(response => response.url().endsWith('/api/live/followups'));
  await page.getByRole('button', { name: 'Submit follow-up' }).click();
  const response = await post;
  expect(response.status()).toBe(202);
  const body = response.request().postDataJSON();
  const receipt = await response.json();
  await expect(page.getByText(`Request accepted for run ${receipt.runId}`)).toBeVisible();
  // Real server + StateStore replay: same operation, one persisted run.
  const replay = await page.evaluate(async body => {
    const response = await fetch('/api/live/followups', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    return { status: response.status, receipt: await response.json() };
  }, body);
  expect(replay.status).toBe(202);
  expect(replay.receipt.runId).toBe(receipt.runId);
  expect(body.targetVersion).toBeGreaterThan(0);
  const state = await page.evaluate(async () => (await fetch('/api/live/state')).json());
  expect(state.runs.filter((run: { id: string }) => run.id === receipt.runId)).toHaveLength(1);
});

test('recovers a committed follow-up after its response is lost without creating another run', async ({ page }) => {
  await page.getByRole('button', { name: 'Start or follow up', exact: true }).click();
  await page.getByRole('button', { name: 'Follow up on saved work' }).click();
  await page.getByLabel('Research question or follow-up').fill(`Lost response ${Date.now()}`);
  await page.getByLabel('Work to continue').selectOption({ index: 1 });
  let committed: { requestId: string; runId: string } | null = null;
  let originalBody: Record<string, unknown> | null = null;
  await page.route('**/api/live/followups', async route => {
    if (committed) return route.continue();
    originalBody = route.request().postDataJSON();
    const response = await route.fetch();
    expect(response.status()).toBe(202);
    committed = await response.json();
    await route.abort('failed');
  });
  await page.getByRole('button', { name: 'Submit follow-up' }).click();
  await expect(page.getByText('Request response unconfirmed')).toBeVisible();
  expect(committed).not.toBeNull();
  const savedBeforeRetry = await page.evaluate(async () => (await fetch('/api/live/state')).json());
  expect(savedBeforeRetry.runs.filter((run: { id: string }) => run.id === committed!.runId)).toHaveLength(1);
  await page.reload();
  await expect(page.getByRole('button', { name: 'Retry same request' })).toBeVisible();
  await page.getByRole('button', { name: 'Retry same request' }).click();
  await expect(page.getByText(`Request accepted for run ${committed!.runId}`)).toBeVisible();
  const savedAfterRetry = await page.evaluate(async () => (await fetch('/api/live/state')).json());
  expect(savedAfterRetry.runs.filter((run: { id: string }) => run.id === committed!.runId)).toHaveLength(1);
  expect(originalBody!.requestId).toBe(committed!.requestId);
});

test('shows push refresh diagnostics and keeps failure distinct from saved state', async ({ page }) => {
  await page.getByText('Technical run details', { exact: true }).click();
  await expect(page.getByText('Host notifications with periodic verification')).toBeVisible();
  await page.route('**/api/live/state*', route => route.fulfill({ status: 503, contentType: 'application/json', body: JSON.stringify({ error: 'Synthetic viewer interruption' }) }));
  await expect(page.getByText(/Synthetic viewer interruption/).first()).toBeVisible({ timeout: 10000 });
  await expect(page.getByText(/current host state (is )?unknown/).first()).toBeVisible();
  await page.unroute('**/api/live/state*');
  await expect(page.getByText(/Synthetic viewer interruption/)).toHaveCount(0, { timeout: 10000 });
});

test('a transient first update-stream failure reconnects to host notifications', async ({ page }) => {
  let attempts = 0;
  await page.route('**/api/live/updates', route => {
    attempts += 1;
    if (attempts === 1) return route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":"Synthetic stream interruption"}' });
    return route.continue();
  });
  await page.reload();
  await page.getByText('Technical run details', { exact: true }).click();
  await expect.poll(() => attempts).toBeGreaterThan(0);
  await expect(page.getByText('Host notifications with periodic verification')).toBeVisible({ timeout: 15000 });
  expect(attempts).toBeGreaterThan(1);
});

test('reconnect reads saved host changes without issuing a command', async ({ page }) => {
  let commands = 0;
  page.on('request', request => { if (request.method() === 'POST' && /\/api\/live\/(start|followups)$/.test(request.url())) commands += 1; });
  await page.route('**/api/live/state*', route => route.fulfill({ status: 503, contentType: 'application/json', body: '{"error":"Synthetic disconnection"}' }));
  await expect(page.getByText(/current host state (is )?unknown/).first()).toBeVisible({ timeout: 10000 });
  const question = `Changed while disconnected ${Date.now()}`;
  execFileSync('.venv/bin/python', ['scripts/atlas_fixture.py', '--state-dir', '.local/atlas-browser/workspace', '--publish-question', question]);
  expect(commands).toBe(0);
  await page.unroute('**/api/live/state*');
  await expect(page.getByText(/Synthetic disconnection/)).toHaveCount(0, { timeout: 10000 });
  await expect(page.locator('.original-research-request')).toHaveText(question);
  expect(commands).toBe(0);
});

test('connected offline History lists and opens a newly accepted saved run', async ({ page }) => {
  await expect(page.getByText('RECORDED OFFLINE TEST').first()).toBeVisible();
  await page.getByRole('button', { name: 'Saved runs', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Find an earlier run' })).toBeVisible();
  const question = `History acceptance ${Date.now()}`;
  const accepted = await page.evaluate(async question => {
    const response = await fetch('/api/live/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ requestId: `request-${crypto.randomUUID()}`, question, scope: 'Synthetic history check', agentIds: ['agent-synthesis'], evidenceText: 'Synthetic supplied evidence.' }) });
    return { status: response.status, receipt: await response.json() };
  }, question);
  expect(accepted.status).toBe(202);
  const savedRun = page.getByRole('article', { name: `Saved run ${question}` });
  await expect(savedRun).toBeVisible({ timeout: 10000 });
  await savedRun.getByRole('button', { name: 'Open saved run' }).click();
  await expect(page).toHaveURL(new RegExp(`runId=${accepted.receipt.runId}`));
  await expect(page.locator('.original-research-request')).toHaveText(question);
  await expect(page.getByText('RECORDED OFFLINE TEST').first()).toBeVisible();
});

test('launch session is private and unauthenticated clients cannot read or write', async ({ page, browser }) => {
  await expect(page).not.toHaveURL(/token=/);
  const cookies = await page.context().cookies();
  const session = cookies.find(cookie => cookie.name === 'ora_session');
  expect(session?.httpOnly).toBe(true);
  expect(session?.sameSite).toBe('Strict');
  const stranger = await browser.newContext();
  try {
    const state = await stranger.request.get('http://127.0.0.1:4177/api/live/state');
    const stream = await stranger.request.get('http://127.0.0.1:4177/api/live/updates');
    const command = await stranger.request.post('http://127.0.0.1:4177/api/live/start', { data: {} });
    expect([state.status(), stream.status(), command.status()]).toEqual([401, 401, 401]);
  } finally { await stranger.close(); }
  const crossOrigin = await page.request.post('http://127.0.0.1:4177/api/live/start', { headers: { Origin: 'http://example.invalid' }, data: {} });
  expect(crossOrigin.status()).toBe(403);
  const persisted = await page.evaluate(() => JSON.stringify({ local: { ...window.localStorage }, session: { ...window.sessionStorage } }));
  expect(persisted).not.toContain('Adult study B · independent randomized comparison');
  expect(persisted).not.toContain('syn-abstract-disagreement-snapshot-v2');
});

test('independent review sends two configured peers and their required contributions', async ({ page }) => {
  await page.getByRole('button', { name: 'Start or follow up', exact: true }).click();
  await page.getByRole('button', { name: 'Independent review (2+)' }).click();
  await expect(page.getByRole('checkbox', { name: /^Synthesis peer ·/ })).toBeChecked();
  await expect(page.getByRole('checkbox', { name: /^Critique peer ·/ })).toBeChecked();
  await page.getByLabel('Research question or follow-up').fill('Compare two synthetic interpretations.');
  await page.getByLabel('Pasted source evidence').fill('Synthetic evidence supplied for the offline fixture.');
  const post = page.waitForResponse(response => response.url().endsWith('/api/live/start'));
  await page.getByRole('button', { name: 'Start research' }).click();
  const response = await post;
  expect(response.status()).toBe(202);
  const body = response.request().postDataJSON();
  expect(body.agentIds).toEqual(['agent-synthesis', 'agent-critique']);
  expect(body.requiredAgentIds).toEqual(body.agentIds);
  const receipt = await response.json();
  const state = await page.evaluate(async () => (await fetch('/api/live/state')).json());
  expect(state.runs.filter((run: { id: string }) => run.id === receipt.runId)).toHaveLength(1);
});

test('request form has no serious or critical automated accessibility violations', async ({ page }) => {
  await page.getByRole('button', { name: 'Start or follow up', exact: true }).click();
  await expect(page.getByLabel('Research question or follow-up')).toBeVisible();
  const result = await new AxeBuilder({ page }).include('.live-request').analyze();
  expect(result.violations.filter(item => ['serious', 'critical'].includes(item.impact ?? ''))).toEqual([]);
});
