# Saved contribution review

`snapshot.json` is generated from a fresh disposable database by
`ora.coordination.contribution_demo`. It contains actual local callback, peer
message, critique, optional uptake and summary-coverage records. It contains no
model call, retrieved publication or scientific report.

Open **Previous samples → Open shared contribution review**, then
**Communication → How peers used shared work** and **What the saved explanations
cover**. The fixed route is `?recorded=stage3&review=contributions`; arbitrary file
or URL parameters are not accepted. The original Stage 3 sample stays available.

Regenerate with the pinned Python environment:

```sh
.venv/bin/python -m ora.coordination.contribution_demo
```

The command prints its fresh state path, snapshot path, proposal query and exact
summary references. After checking CLI equality, privacy markers, no-report state
and the UI tests, copy that generated snapshot to this directory. Do not edit its
records by hand. This saved view is separate from Atlas's older live execution
path; the real connected loop/UI acceptance belongs to Stage 4.
