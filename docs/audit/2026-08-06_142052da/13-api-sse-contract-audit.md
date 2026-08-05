# 13. API and SSE Contract Audit

## API evidence

- Novel creation/reload, macro confirmation, act chapter generation/confirm
  and Beat Sheet persistence ran through real FastAPI routers and SQLite with
  only the external LLM transport replaced.
- Manuscript entity compatibility routes return unified prop fields and make
  create, update and delete visible before the HTTP result is returned.
- AI Invocation review/preview/resume paths now share the explicit-variable
  preservation policy.
- Existing API, planning, embedding settings, daemon, runtime settings and
  autopilot route suites passed in the full backend run.

## SSE and state observations

The full suite covers the existing autopilot SSE protocol and DAG route/SSE
contracts. The browser acceptance did not open a long-lived live SSE stream;
therefore it does not claim a new browser-network endurance test. No SSE
contract was changed in this batch.

## Error behavior

Required memory/extraction failure remains propagated and pauses the relevant
flow. Optional vector indexing reports failure/retry state without corrupting
the canonical commit. The tests explicitly distinguish these outcomes from a
successful no-op extraction.
