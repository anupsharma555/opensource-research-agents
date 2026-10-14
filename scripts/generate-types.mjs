/* Generate desktop request and snapshot types from canonical Python-derived schemas.
   Each output is produced from its matching JSON Schema, so UI commands and saved
   views share the backend's field definitions. --check detects drift without edits. */
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { compile } from 'json-schema-to-typescript';

const repositoryRoot = resolve(fileURLToPath(new URL('..', import.meta.url)));
const checkOnly = process.argv.includes('--check');
const contracts = [
  ['runtime_plan', 'runtime-plan', 'RuntimePlan'],
  ['research', 'contracts', 'ResearchSnapshot'],
  ['live_start', 'live-start', 'LiveStartRequest'],
  ['live_followup', 'live-followup', 'LiveFollowupRequest'],
  ['live_receipt', 'live-receipt', 'LiveReceipt'],
  ['live_state', 'live-state', 'LiveState'],
  ['retrieval', 'retrieval', 'RetrievalEnvelope'],
];

for (const [schemaName, fileName, typeName] of contracts) {
  const relativeSchema = `contracts/generated/${schemaName}.schema.json`;
  const schema = JSON.parse(await readFile(resolve(repositoryRoot, relativeSchema), 'utf8'));
  const compiled = await compile(schema, typeName, { bannerComment: '', additionalProperties: false });
  const output = `/* Generated from ${relativeSchema}. Do not edit by hand. */\n${compiled}`;
  const outputPath = resolve(repositoryRoot, `ui/src/generated/${fileName}.ts`);
  if (checkOnly) {
    let existing;
    try { existing = await readFile(outputPath, 'utf8'); }
    catch { throw new Error(`Missing ${fileName}.ts; run scripts/dev generate.`); }
    if (existing !== output) throw new Error(`Generated ${fileName}.ts differs; run scripts/dev generate.`);
  } else {
    await mkdir(resolve(repositoryRoot, 'ui/src/generated'), { recursive: true });
    await writeFile(outputPath, output);
  }
}
process.stdout.write(checkOnly ? 'Generated UI types match the canonical schemas.\n' : 'Generated snapshot and live command types.\n');
