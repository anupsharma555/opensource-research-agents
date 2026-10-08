import { readFileSync } from 'node:fs';
import { parseEnv } from 'node:util';

const model = 'jevk5-0.2';
const endpoint = 'https://api.codiv.ai/v1/systemone';
const args = process.argv.slice(2);
if (args.some((arg) => arg !== '--live' && !/^--test=[123]$/.test(arg))) {
  console.error('Usage: node examples/codiv-classification.mjs [--test=1|2|3] [--live]');
  process.exit(1);
}

const testNumber = args.find((arg) => arg.startsWith('--test='))?.slice(7) ?? '1';
const report = JSON.parse(readFileSync(new URL(
  '../experiments/codiv-classification-results-2026-10-03.json', import.meta.url,
), 'utf8'));
const test = report.tests.find((item) => item.id === `test_${testNumber}`);
const payload = { model, ...test.input };
const live = args.includes('--live');

console.log(JSON.stringify({
  mode: live ? 'live: one API request' : 'preview: no API request',
  test: test.id,
  expected: test.expected,
  request: payload,
}, null, 2));

if (live) {
  let key = '';
  try {
    const config = parseEnv(readFileSync(new URL('../.env', import.meta.url), 'utf8'));
    key = config.TYPESAFE_API_KEY ?? '';
    if (!key || /\s/.test(key)) throw new Error('Set a valid TYPESAFE_API_KEY in the project .env.');
    if (config.TYPESAFE_BASE_URL !== 'https://api.codiv.ai') {
      throw new Error('TYPESAFE_BASE_URL must be https://api.codiv.ai.');
    }
    const response = await fetch(endpoint, {
      method: 'POST',
      redirect: 'error',
      signal: AbortSignal.timeout(40_000),
      headers: {
        Authorization: `Bearer ${key}`,
        'Content-Type': 'application/json',
        'User-Agent': 'ORA-Codiv-Test/0.1',
      },
      body: JSON.stringify(payload),
    });
    const body = (await response.text()).replaceAll(key, '[REDACTED]');
    if (!response.ok) throw new Error(`Codiv HTTP ${response.status}: ${body}`);
    const result = JSON.parse(body);
    if (result.model !== model || !result.answers?.review_route || !result.answers?.reports_direct_effect) {
      throw new Error('Codiv returned an unexpected model or missing answers.');
    }
    console.log(JSON.stringify({ http_status: response.status, response: result }, null, 2));
  } catch (error) {
    const message = error instanceof Error ? error.message : String(error);
    console.error(key ? message.replaceAll(key, '[REDACTED]') : message);
    process.exitCode = 1;
  }
}
