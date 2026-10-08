// Generate the React snapshot type from the canonical research schema, or check for drift.
import { readFile, writeFile, mkdir } from 'node:fs/promises';
import { resolve } from 'node:path';
import { fileURLToPath } from 'node:url';
import { compile } from 'json-schema-to-typescript';

const repositoryRoot = resolve(fileURLToPath(new URL('..', import.meta.url)));
const schemaPath = resolve(repositoryRoot, 'contracts/generated/research.schema.json');
const outputPath = resolve(repositoryRoot, 'ui/src/generated/contracts.ts');
const checkOnly = process.argv.includes('--check');

const schema = JSON.parse(await readFile(schemaPath, 'utf8'));
const compiled = await compile(schema, 'ResearchSnapshot', {
  bannerComment: '',
  additionalProperties: false,
});
const output = `/* Generated from contracts/generated/research.schema.json. Do not edit by hand. */\n${compiled}`;

if (checkOnly) {
  let existing;
  try {
    existing = await readFile(outputPath, 'utf8');
  } catch {
    throw new Error('Generated UI types are missing; run npm run generate:types.');
  }
  if (existing !== output) {
    throw new Error('Generated UI types differ from the canonical schema; run npm run generate:types.');
  }
  process.stdout.write('Generated UI types match the canonical schema.\n');
} else {
  await mkdir(resolve(repositoryRoot, 'ui/src/generated'), { recursive: true });
  await writeFile(outputPath, output);
  process.stdout.write(`Generated ${outputPath}\n`);
}
