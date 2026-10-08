/* Check that JavaScript accepts and rejects the same sample records as Python.
   The script reads shared examples and generated JSON rules describing allowed
   fields and values, then reports any mismatch. It checks record shape offline,
   not whether research claims are true. */
import assert from 'node:assert/strict';
import { readFile } from 'node:fs/promises';
import Ajv2020 from 'ajv/dist/2020.js';

const ajv = new Ajv2020({ strict: false, allErrors: true });
// Pydantic emits discriminator metadata; JSON Schema's oneOf performs validation.
// No coercion, default filling, or removal of unknown fields is enabled here.
const fixtures = JSON.parse(await readFile('contracts/examples.json', 'utf8'));
const validators = new Map();
for (const fixture of fixtures) {
  if (!validators.has(fixture.schema)) {
    const schema = JSON.parse(await readFile(`contracts/generated/${fixture.schema}.schema.json`, 'utf8'));
    validators.set(fixture.schema, ajv.compile(schema));
  }
  const validate = validators.get(fixture.schema);
  const accepted = validate(fixture.value);
  assert.equal(accepted, fixture.accepted,
    `${fixture.name}: expected ${fixture.accepted ? 'accepted' : 'rejected'}; ${ajv.errorsText(validate.errors)}`);
}
console.log(`JavaScript boundary: ${fixtures.length} shared accepted/rejected contract cases passed.`);
