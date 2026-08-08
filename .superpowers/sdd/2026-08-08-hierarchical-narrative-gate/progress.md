# SDD ledger — plan: user-provided 通用层级叙事契约与防跑偏门禁

Setup: user-designated isolated checkout `W:\novel\test`, branch `codex/hierarchical-narrative-gate`, base `a9440709`.

Task 1: completed — hierarchy snapshot, deterministic alignment gate, and TDD tests (`111275b1` plus `95f86fae`).
Task 2: completed — safe macro merge field preservation and macro/act planning gates (`26e6615f`, `57fc82e2`, `7d407734`).
Task 3: completed — chapter preplan and all prose-entry gates with MemoryEngine/context/vector evidence (`8bef0a26`).
Task 4: completed — governance audit, override/replan APIs, and UI status/preview (`7a026794`).
Task 5: completed — API planning-factory bypass closure, installer module compile repair, full verification, and runtime smoke checks (final SHA recorded in task-5-report.md).

Verification ledger (2026-08-08):

- Focused hierarchy/governance suites: 60 passed.
- Complete Python suite: 2174 passed, 12 skipped, 3 deselected, 1 warning.
- Frontend unit suite: 12 passed across 7 files.
- Frontend typecheck and production build: passed (`npm run build`).
- Full `compileall` over application/engine/infrastructure/interfaces/domain/shared/scripts: passed.
- `git diff --check`: passed.
- Deterministic smoke paths: valid pass, volume contradiction block, vector-degraded review, exact one-shot override, zero-write replan preview, and frontend status payload contract build verification all passed.

Residual risks and deployment notes: direct legacy service callers that omit a gate retain compatibility behavior; production API factories now inject the deterministic gate. Governance report cache is process-local, with SQLite governance events as the route reconstruction fallback; multi-worker deployments must share the same governance database. Replan previews emit audit events but never mutate story nodes, chapter content, or narrative contracts. No external narrative contract is marked `COMMITTED` by this code task.
