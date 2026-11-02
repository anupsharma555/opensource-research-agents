/* Measure the selected-run viewer and command receipts in Chromium against a
   synthetic local host. No provider or production workspace is used. Run with
   pinned Node PATH and PLAYWRIGHT_BROWSERS_PATH after building the frontend. */
import { spawn } from 'node:child_process';
import { readdir, readFile, mkdir, writeFile } from 'node:fs/promises';
import { resolve, join } from 'node:path';
import { performance } from 'node:perf_hooks';
import { createHash, randomUUID } from 'node:crypto';
import { gzipSync } from 'node:zlib';
import { chromium } from '@playwright/test';

const root = resolve(import.meta.dirname, '..');
const receiptDir = join(root, '.local/reviews/tech-stack-implementation/browser-performance');
const baselineRoot = join(root, '.local/reviews/tech-stack-implementation/baseline/ui/dist');
const currentRoot = join(root, 'ui/dist');
const commandsOnly = process.argv.includes('--commands-only');
const only = process.argv.includes('--baseline-only') ? 'baseline' : process.argv.includes('--current-only') ? 'current' : null;
if (process.argv.includes('--baseline-only') && process.argv.includes('--current-only')) throw new Error('Choose at most one version.');
if (commandsOnly && only) throw new Error('Choose commands or one frontend version.');
if (process.version !== 'v24.21.0') throw new Error(`Use the pinned Node 24.21.0; found ${process.version}.`);
if (!process.env.PLAYWRIGHT_BROWSERS_PATH?.startsWith(join(root, '.local/cache/playwright'))) throw new Error('Set PLAYWRIGHT_BROWSERS_PATH to the repository browser cache.');
await mkdir(receiptDir, { recursive: true });

const wait = ms => new Promise(resolveWait => setTimeout(resolveWait, ms));
async function assetSizes(directory) {
  const files = [join(directory, 'index.html'), ...(await readdir(join(directory, 'assets'))).map(name => join(directory, 'assets', name))];
  const entries = await Promise.all(files.map(async path => {
    const bytes = await readFile(path);
    return { name: path.slice(directory.length + 1), bytes: bytes.length, gzipBytes: gzipSync(bytes).length, sha256: createHash('sha256').update(bytes).digest('hex') };
  }));
  return { files: entries, manifestSha256: createHash('sha256').update(entries.map(file => `${file.name} ${file.sha256}`).join('\n')).digest('hex'), totalBytes: entries.reduce((sum, file) => sum + file.bytes, 0), totalGzipBytes: entries.reduce((sum, file) => sum + file.gzipBytes, 0) };
}
async function launchHost(label, webRoot) {
  const stateDir = join(receiptDir, 'runs', `${label}-${Date.now()}-${randomUUID().slice(0, 8)}`);
  await mkdir(stateDir, { recursive: true });
  const child = spawn(join(root, '.venv/bin/python'), [join(root, 'scripts/atlas_fixture.py'), '--state-dir', stateDir, '--web-root', webRoot, '--port', '0'], { cwd: root, stdio: ['ignore', 'pipe', 'pipe'] });
  let stderr = '';
  child.stderr.on('data', chunk => { stderr += String(chunk).slice(0, 2000); });
  const connectionFile = join(stateDir, 'connection.json');
  for (let attempt = 0; attempt < 100; attempt += 1) {
    if (child.exitCode !== null) throw new Error(`${label} fixture exited: ${stderr}`);
    try {
      const connection = JSON.parse(await readFile(connectionFile, 'utf8'));
      const health = await fetch(`${connection.origin}/api/live/health`);
      if (health.ok) return { child, connection, stateDir };
    } catch { /* The host has not finished writing the local connection yet. */ }
    await wait(100);
  }
  child.kill('SIGTERM');
  throw new Error(`${label} fixture did not become ready: ${stderr}`);
}

async function publishQuestion(stateDir, question) {
  const command = spawn(join(root, '.venv/bin/python'), [join(root, 'scripts/atlas_fixture.py'), '--state-dir', stateDir, '--publish-question', question], { cwd: root, stdio: ['ignore', 'ignore', 'pipe'] });
  let stderr = '';
  command.stderr.on('data', chunk => { stderr += String(chunk).slice(0, 2000); });
  const code = await new Promise(resolveExit => command.once('exit', resolveExit));
  if (code !== 0) throw new Error(`Synthetic state update failed: ${stderr}`);
}

async function measure(label, webRoot, browser) {
  const host = await launchHost(label, webRoot);
  const context = await browser.newContext();
  const page = await context.newPage();
  const stateReads = [];
  page.on('response', response => {
    if (new URL(response.url()).pathname !== '/api/live/state') return;
    stateReads.push({ at: performance.now(), status: response.status(), bytes: Number(response.headers()['content-length'] || 0) });
  });
  try {
    const url = new URL(host.connection.launchUrl);
    await page.goto(url.toString());
    await page.locator('.original-research-request').waitFor({ state: 'visible', timeout: 10000 });
    await wait(750); // Exclude launch/auth and initial stream invalidation from idle reads.
    const idleStart = performance.now();
    await wait(6000);
    const idleEnd = performance.now();
    const idleReads = stateReads.filter(read => read.at >= idleStart && read.at < idleEnd);
    const latenciesMs = [];
    for (let index = 0; index < 5; index += 1) {
      const question = `Synthetic saved change ${index + 1} ${randomUUID().slice(0, 8)}`;
      await publishQuestion(host.stateDir, question);
      const committedAt = performance.now();
      await page.locator('.original-research-request', { hasText: question }).waitFor({ state: 'visible', timeout: 12000 });
      latenciesMs.push(Math.round(performance.now() - committedAt));
      await wait(200);
    }
    return {
      label, fixture: 'LocalResearchServer + StateStore, external synthetic selected-run commits, no model calls',
      idleWindowMs: Math.round(idleEnd - idleStart), idleStateReads: idleReads.length,
      idleStateBytes: idleReads.reduce((sum, read) => sum + read.bytes, 0),
      idleReadStatuses: idleReads.map(read => read.status),
      commitToVisibleMs: latenciesMs, medianCommitToVisibleMs: [...latenciesMs].sort((a, b) => a - b)[2],
      assets: await assetSizes(webRoot),
    };
  } finally {
    await context.close();
    host.child.kill('SIGTERM');
  }
}

async function measureCommands(browser) {
  const host = await launchHost('commands', currentRoot);
  const context = await browser.newContext();
  const page = await context.newPage();
  try {
    await page.goto(host.connection.launchUrl);
    await page.locator('.original-research-request').waitFor({ state: 'visible', timeout: 10000 });
    const firstReceiptMs = [];
    const sameIdReplayMs = [];
    for (let index = 0; index < 5; index += 1) {
      const body = { requestId: `measure-${randomUUID()}`, question: `Synthetic command ${index + 1}`, scope: 'Synthetic acceptance measurement', agentIds: ['agent-synthesis'], evidenceText: 'Synthetic supplied evidence.' };
      const post = async () => page.evaluate(async body => {
        const started = performance.now();
        const response = await fetch('/api/live/start', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
        const receipt = await response.json();
        return { durationMs: performance.now() - started, status: response.status, receipt };
      }, body);
      const first = await post();
      const replay = await post();
      if (first.status !== 202 || replay.status !== 202 || first.receipt.requestId !== body.requestId || replay.receipt.requestId !== body.requestId || first.receipt.runId !== replay.receipt.runId) {
        throw new Error('Synthetic command acceptance or exact replay failed.');
      }
      const saved = await page.evaluate(async runId => {
        const response = await fetch(`/api/live/state?runId=${encodeURIComponent(runId)}`);
        const state = await response.json();
        return { status: response.status, runId: state.runId, matches: state.runs.filter(run => run.id === runId).length };
      }, first.receipt.runId);
      if (saved.status !== 200 || saved.runId !== first.receipt.runId || saved.matches !== 1) throw new Error('Synthetic command receipt did not identify one saved run.');
      firstReceiptMs.push(Math.round(first.durationMs * 10) / 10);
      sameIdReplayMs.push(Math.round(replay.durationMs * 10) / 10);
    }
    const median = values => [...values].sort((a, b) => a - b)[2];
    return { measuredAt: new Date().toISOString(), browser: `Playwright Chromium ${browser.version()}`, node: process.version,
      fixture: 'Current LocalResearchServer + StateStore; five synthetic starts and exact same-ID replays; no model calls',
      boundary: 'Browser fetch start to parsed HTTP 202 receipt; excludes model work, frontend render, and provider completion.',
      acceptedStarts: 5, acceptedSameIdReplays: 5, persistedRunCountPerRequest: 1,
      firstReceiptMs, medianFirstReceiptMs: median(firstReceiptMs), sameIdReplayMs, medianSameIdReplayMs: median(sameIdReplayMs),
      assets: await assetSizes(currentRoot) };
  } finally {
    await context.close();
    host.child.kill('SIGTERM');
  }
}

const browser = await chromium.launch({ headless: true });
try {
  if (commandsOnly) {
    const result = await measureCommands(browser);
    const path = join(receiptDir, 'commands.json');
    await writeFile(path, JSON.stringify(result, null, 2) + '\n');
    process.stdout.write(JSON.stringify({ receipt: path, acceptedStarts: result.acceptedStarts, acceptedSameIdReplays: result.acceptedSameIdReplays, firstReceiptMs: result.firstReceiptMs, medianFirstReceiptMs: result.medianFirstReceiptMs, sameIdReplayMs: result.sameIdReplayMs, medianSameIdReplayMs: result.medianSameIdReplayMs, manifestSha256: result.assets.manifestSha256 }, null, 2) + '\n');
  } else {
    const versions = only ? [[only, only === 'baseline' ? baselineRoot : currentRoot]] : [['baseline', baselineRoot], ['current', currentRoot]];
    const results = [];
    for (const [label, webRoot] of versions) results.push(await measure(label, webRoot, browser));
    const receipt = { measuredAt: new Date().toISOString(), baselineSource: 'git archive HEAD frontend assets', currentSource: 'working-tree frontend assets', browser: `Playwright Chromium ${browser.version()}`, node: process.version, workload: '6 seconds idle after launch, then five sequential synthetic StateStore commits to the selected run; timing begins when the commit command returns and ends when the changed question is visible in Research review.', results };
    const path = join(receiptDir, only ? `${only}.json` : 'comparison.json');
    await writeFile(path, JSON.stringify(receipt, null, 2) + '\n');
    process.stdout.write(JSON.stringify({ receipt: path, results: results.map(({ label, idleStateReads, idleStateBytes, commitToVisibleMs, medianCommitToVisibleMs, assets }) => ({ label, idleStateReads, idleStateBytes, commitToVisibleMs, medianCommitToVisibleMs, bundleBytes: assets.totalBytes, gzipBundleBytes: assets.totalGzipBytes, manifestSha256: assets.manifestSha256 })) }, null, 2) + '\n');
  }
} finally { await browser.close(); }
