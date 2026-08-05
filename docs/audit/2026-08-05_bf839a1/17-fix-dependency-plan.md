# Repair Dependency Plan and Completion Record

## Boundaries Preserved

All work remains within the existing DDD, CPMS, StoryPipeline, EngineDaemon,
SQLite, Write Dispatch, vector, MemoryEngine, Evolution, and UnifiedCheckpoint
architecture. No alternate database, queue, memory system, frontend state
system, global repository migration, or broad API redesign was introduced.

## Completed Ordered Batches

| Batch | Issues | Minimal repair outcome | Verification state |
| --- | --- | --- | --- |
| 1 | DATA-001, BLUEPRINT-001 | Block authored-prose deletion before mutation; remove unsafe safe-merge fallback | Completed and covered by focused planning tests |
| 2 | DB-001, DB-001b, DB-003 | Safe SQLite UPSERT, compatibility-safe natural-key guard, declared migration dependency order | Completed and covered by repository/migration tests |
| 3 | SETTING-001, SETTING-002 | Authoritative `generation_prefs` projection, budgeted provider propagation, and blank-value replacement | Completed and verified with final provider trace probe |
| 4 | DB-002, AUTOPILOT-001 | Full recovery-field persistence, restart-reason schema/domain/API projection, explicit-start reset | Completed and covered by runtime/repository/API tests |
| 5 | BLUEPRINT-002 | Reuse shared direct next-act preflight for capacity, hierarchy, target, and numbering | Completed and covered by service/route tests |
| 6 | OBS-001, SSE-001, TEST-001, TEST-002 | Privacy-safe parser diagnostics, event ID/replay/deduplication, cross-boundary acceptance test, CPMS test isolation | Completed and covered by protocol, API, and full-suite tests |

## Final Verification Gate

The following evidence has been completed in the test workspace and is
rechecked immediately before commit:

1. Focused unit and integration tests for each batch.
2. Fresh and existing-database migration tests.
3. Final-provider spy covering first and re-dispatched prompt calls.
4. Autopilot restart and status-projection tests.
5. Frontend shared-config type check and production build.
6. Real 30-chapter regression and both true 100-chapter slow selectors.
7. Real FastAPI plus Mock-LLM acceptance from novel creation through macro
   plan, safe structure confirmation, and Beat Sheet persistence.
8. Full default backend pytest suite.
9. Submission checks for whitespace, full diff, sensitive/runtime-file
   exclusion, branch, remote, and local embedding dependency preservation.

## Deliberate Non-Changes

The work did not expand into browser-driven visual redesign, an external LLM
run, full prose branch isolation, a new queue/database/memory system, a broad
SSE replacement, global repository conversion, or frontend lint/test tooling
creation. These are explicitly out of scope unless later evidence identifies a
blocking defect.
