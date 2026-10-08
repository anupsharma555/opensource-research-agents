# Contributing

Keep a change small enough to explain and broad enough to prove its behavior.
Read [AGENTS.md](AGENTS.md) and [setup](docs/development/setup.md) before editing.
Use one focused branch or isolated checkout per concurrent assignment. State owned
paths and integration inputs so workers can coordinate without a repository-wide lock.

Explain the user-visible problem, selected approach, alternatives when material,
and evidence that the result works. Use plain language and precise examples.
Define technical terms when they are first needed. Do not use fluent prose,
schema validity or a successful tool call as substitutes for scientific evidence.

For interface changes, edit the canonical Python source, run `scripts/dev generate`,
and update the [compatibility record](docs/interfaces/compatibility.md). Test the
changed behavior, an affected failure path and an unaffected sibling where relevant.
Then run `scripts/dev check`. Native app changes additionally require a Mac build
and a visible review of the affected journey.

Never commit credentials, personal identifiers, private tracker links, source
payloads without permission, or local runtime artifacts. Default checks stay
offline. Live experiments must name their model, input/version, resource allowance
and proof limits, preserve actual results, and avoid silent provider fallbacks.

Review the exact staged paths and diff before pushing. Use a pull request for
subsequent shared changes, retain both sides of a conflict until deliberately
reconciled, and regenerate derived files from the chosen source rather than
merging generated output by hand. A good completion note names what was verified,
what was not rerun, and any remaining boundary.
