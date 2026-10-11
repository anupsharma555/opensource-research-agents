# Shared source-instruction case

`instruction-bearing-abstract.json` is a project-authored simulated source with
one valid attendance result and a paragraph asking an agent to change its
question, model, tools and reported result. It contains no real study or
participant data. Keep that paragraph in the delivered source so the test checks
how it is handled, rather than succeeding because it was removed.

`tests/mission/test_evidence_instruction_boundary.py` sends this case through both
supplied-evidence and injected scholarly-retrieval paths. It checks that source
text stays outside system/developer instructions, model settings and permissions
remain fixed, an ungranted tool request is rejected locally, and independent work
can finish. The scripted provider then uses the valid fact with its source link.

Run from the repository root after the documented setup:

```sh
.venv/bin/python -m pytest tests/mission/test_evidence_instruction_boundary.py -q
```

These are offline host and input-delivery checks. They do not demonstrate a real
model's resistance to misleading instructions or qualify other source formats.
Reuse the exact fixture for separately authorized model evaluations and later
data-quality tests, recording those results separately from the scripted result.
