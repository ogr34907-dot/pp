# 17. Fix Dependency Plan

## Scope and order

This plan covers only accepted current findings and the inherited follow-up
diff. It preserves the committed `142052da` architecture and does not expand
into a new persistence layer, queue, memory system, provider interface, or UI
redesign.

| Batch | Issues | Required action | Gate before next batch |
| --- | --- | --- | --- |
| 0 | BASELINE-001 | Preserve the inherited diff, document provenance, isolate all runtime data under the audit directory | Current issue register and source-level root-cause evidence exist |
| 1 | MEMORY-001 | Add a failing Mock Provider contract test, then add only a high-priority `memory-extraction` response that conforms to `MemoryDeltaPayload` | Completed: red then green evidence; complete Mock Provider plus MemoryEngine production-path selection passed |
| 2 | MEMORY-001 | Restart only isolated backend `127.0.0.1:8015`, inspect current test data, rerun explicit `retain_prose` replay | Completed: revision 5 proved the memory contract; replay-range boundary was isolated as MEMORY-002 |
| 3 | MEMORY-002 | Add a failing coordinator regression for an empty future planned node, then limit replay head to retained prose | Completed: red then green evidence and a successful real revision-6 `retain_prose` response |
| 4 | MANUSCRIPT-001/002, FRONTEND-001, MOCK-E2E-001..004 | Review the existing inherited diff and rerun their dedicated unit/integration/browser checks | No unrelated behavior or sensitivity regression is discovered |
| 5 | ONBOARD-001 | Make plot-outline preview state distinct from durable save state | UI no longer claims persistence until the explicit save returns; API/SQLite prove save after step-4 confirmation |
| 6 | BLUEPRINT-003 | Add red backend tests for short targets and out-of-bound manual ranges, then constrain range normalization and the UI editor/validation | All stage ranges are inside the true target, including a two-chapter real UI/API flow |
| 7 | SETTING-003 | Add a red macro-contract test for `worldbuilding.content` and `locations.list`, then register both as typed Variable Hub bindings and prove resolver/prompt propagation | The real, review-gated macro invocation resolves nonempty persisted settings; no call is accepted until this evidence exists |
| 8 | SETTING-004 | Add a red ContextBudgetAllocator regression for a persisted location absent from the outline, then add one bounded Bible location-catalog slot | The allocated writer context and a fresh review-gated prose request contain each saved test location without duplicating an outline-selected location |
| 8a | PROMPT-001 | Add a red review/resume test with a frozen explicit prose context and a stale Hub replacement, then preserve `lineage=explicit` raw values across session refreshes | Review and actual resumed Mock LLM Prompts retain the prepared context while an explicit variables-API edit can still replace its own alias |
| 9 | Current audit scope | Run full backend, migration/Write Dispatch, long-flow 30/100, frontend build/shared config, and UI/API Mock E2E | Completed: full suite, explicit slow tests, Mock API, frontend build and browser acceptance passed; gaps/warnings recorded |
| 10 | Submission | Review diff and sensitive-file exclusion, make logical commits, push, verify remote contains the acceptance commit | In progress after this evidence set is staged and reviewed |
| 11 | Formal synchronization | In `W:\novel\PlotPilot` only, run one `git pull --ff-only` and recheck SHA/status | Formal SHA equals remote target and checkout remains clean |

## Batch 1 exact test-first contract

1. Add a test in `tests/unit/infrastructure/ai/providers/test_mock_provider.py`
   that sends real `memory-extraction` response markers through `MockProvider`.
2. Validate its JSON using the production `MemoryDeltaPayload` model. The
   expected keys are literal: `completed_beats`, `revealed_clues`, and
   `fact_violations`; a top-level `characters` key must be absent.
3. Run the new selector before production code changes. It must fail because
   the present Mock detects generic character context and yields an extra
   top-level field.
4. Add the smallest dedicated intent in `_detect_intent()` before generic
   character detection, then add a deterministic `_memory_extraction()`
   factory method using only the existing memory schema.
5. Re-run the selector and the complete Mock Provider test module.

## Explicit non-changes

- Do not loosen `MemoryDeltaPayload.model_config = ConfigDict(extra="forbid")`.
- Do not suppress the three-attempt MemoryEngine failure or reclassify it as a
  successful replay.
- Do not change `chapter-narrative-sync`, chapter-rewrite invalidation, or
  real LLM provider behavior for a Mock-only contract bug.
- Do not reset, clean, or overwrite inherited test-workspace changes.
- Do not start, stop, migrate, or write to the formal workspace.
