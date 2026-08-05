# Unified Issue Register

## Remediation Status at Submission Gate

The detailed entries below are the baseline findings and reproduction evidence.
They are retained to preserve the audit trail. The table records their state
after the minimal repair batches and regression suite; it is the current
status, not a claim that the baseline behavior still exists.

| ID | Priority | Final status | Evidence |
| --- | --- | --- | --- |
| DATA-001 | P0 | Fixed | Destructive authored-prose paths reject before any partial deletion; blueprint/structure tests pass |
| BLUEPRINT-001 | P0 | Fixed | Safe macro failure is propagated and makes no legacy unsafe writer call |
| DB-001 | P0 | Fixed | SQLite UPSERT preserves parent identity and child rows |
| DB-001b | P1 | Fixed | Compatible natural-key guard blocks new collisions without breaking legacy-row metadata edits |
| SETTING-001 | P1 | Fixed | Six trace settings reach both final-provider attempts and remain within budget |
| SETTING-002 | P1 | Fixed | Empty values overwrite canonical and alias Variable Hub entries |
| AUTOPILOT-001 | P1 | Fixed | Restart reason is persisted, projected, and cleared on explicit start |
| DB-002 | P1 | Fixed | Full Novel save/hydration round-trips recovery fields |
| DB-003 | P1 | Fixed | Fresh and upgrade migration tests prove declared dependency ordering |
| BLUEPRINT-002 | P1 | Fixed | Direct next-act path uses shared capacity, parent, target, and numbering preflight |
| SSE-001 | P2 | Fixed | Stable event IDs, replay cursor behavior, client dedupe, and state calibration are covered by protocol tests |
| OBS-001 | P2 | Fixed | Parse diagnostics no longer emit raw model content; tests retain safe diagnostic facts |
| TEST-001 | P2 | Fixed within project scope | Required cross-boundary tests were added; missing frontend lint/test scripts remain explicitly documented |

## Additional Finding During Acceptance

## TEST-002: CPMS singleton state can contaminate temporary-database tests

- Severity: **P2**
- Verification: **dynamic confirmation**
- Status: **fixed**
- Module: CPMS test setup and teardown
- Trigger: a FastAPI acceptance test initializes CPMS against a temporary
  SQLite database, closes it, then a later test observes the singleton registry
  as already seeded while its database is gone.
- Root cause: test-global `PromptManager`, `PromptRegistry`, and
  `PromptGateway` retain references to the finished temporary database.
- Minimal repair: clear only these test-bound singleton references before and
  after the acceptance test. No production singleton behavior or CPMS
  fail-closed semantics were relaxed.
- Evidence: the complete backend suite and the combined acceptance suite pass
  without test-order dependency.

## Classification convention

- **Dynamic confirmation** means an isolated test-database or final-provider
  probe reproduced the behavior on the audit baseline.
- **Code-level confirmation** means the production path was traced but the
  full external failure was not intentionally induced.
- All implementation, test, and rollback work is limited to `W:\novel\test`.

## DATA-001: Act replanning can delete authored prose

- Severity: **P0**
- Verification: **dynamic confirmation**
- Module: blueprint structure confirmation and chapter-book synchronization
- Audit commit: `bf839a1491aa15553309834faa760e967c3c8991`
- Code locations: `continuous_planning_service.py` act confirmation/removal
  path; `chapter_book_structure_sync.py:37` purge helper.
- Production entry: EngineDaemon planning transition and direct blueprint
  confirmation API.
- Trigger / reproduction: create a chapter with non-empty `content`, confirm
  an act plan which omits it, then allow the normal synchronization path.
- Expected: a destructive change is rejected and the mainline is paused until
  an explicit recoverable rewrite workflow is selected.
- Actual: the chapter is deleted; the probe reported `removed_count: 1` and
  no snapshot or confirmation requirement.
- Impact: existing books, manual writing, autopilot planning, and long-term
  memory; this is possible irreversible user-data loss.
- Root cause: both delete paths treat tree mismatch as sufficient authority to
  remove a chapter and do not distinguish empty planned chapters from authored
  prose.
- Minimal repair: preflight every deletion candidate; if any has non-empty
  content, abort before deleting anything with a typed blocking error. Apply
  the same defense in the global purge helper.
- Files expected: `continuous_planning_service.py`,
  `chapter_book_structure_sync.py`, focused blueprint/structure tests.
- Explicit non-changes: no invented prose branches; no automatic deletion
  snapshots; no broad planning rewrite.
- Required tests: failure test with authored body, empty-draft deletion still
  allowed, no partial deletion when one candidate is authored.
- Dependencies: none. Must precede all other structural work.
- Rollback risk: low; behavior becomes fail-closed and preserves rows.

## BLUEPRINT-001: Safe macro merge failure falls back to unsafe writer

- Severity: **P0**
- Verification: **dynamic confirmation**
- Module: continuous macro planning
- Code location: `application/blueprint/services/continuous_planning_service.py`
  safe persistence fallback near `confirm_macro_plan_safe()`.
- Production entry: macro-plan confirmation API and daemon planning flow.
- Trigger / reproduction: force `confirm_macro_plan_safe()` to reject a
  structure conflict.
- Expected: return a blocking error / pause and leave persisted structure
  unchanged.
- Actual: `confirm_macro_plan()` executes as a fallback and reports success;
  probe observed `unsafe_fallback_calls: 1`.
- Impact: existing and new books, manual confirmation and autopilot; it can
  bypass structure safety rules and lead to data loss.
- Root cause: exception handling equates safe writer failure with permission
  to use a legacy writer whose safeguards are weaker.
- Minimal repair: remove the unsafe fallback. Preserve and surface the safe
  failure without any secondary write.
- Files expected: `continuous_planning_service.py`, macro confirmation tests.
- Explicit non-changes: do not delete the legacy method if other known-safe
  callers still use it; do not change macro API schema.
- Required tests: safe failure makes zero legacy calls and persists zero new
  nodes; normal safe confirmation still succeeds.
- Dependencies: none. Can be fixed in the first P0 batch with DATA-001.
- Rollback risk: low; an unsafe automatic write becomes an explicit failure.

## DB-001: Story-node batch save deletes children through SQLite REPLACE

- Severity: **P0**
- Verification: **dynamic confirmation**
- Module: SQLite story node repository
- Code location: `infrastructure/persistence/database/story_node_repository.py`
  `save_batch()` near the `INSERT OR REPLACE` statement.
- Production entry: macro/volume/act/chapter persistence from planning service
  and runtime delegates.
- Trigger / reproduction: persist parent plus child, then save the same parent
  through `save_batch()`.
- Expected: update parent values while preserving child rows.
- Actual: child count changes from one to zero because REPLACE performs a
  delete and `story_nodes.parent_id` cascades on delete.
- Impact: existing and new books, manual/planned structure, automatic writing;
  potential permanent structural and chapter loss.
- Root cause: SQLite REPLACE semantic was used as an upsert.
- Minimal repair: use `INSERT ... ON CONFLICT(id) DO UPDATE` and update only
  non-primary-key columns.
- Files expected: story node repository and focused SQLite integration tests.
- Explicit non-changes: no repository-wide Write Dispatch rewrite in this
  batch.
- Required tests: update parent retains children; new insert works; multi-node
  batch maintains parent relationships.
- Dependencies: none; migration guard DB-001b may follow but is independent.
- Rollback risk: low; preserves row identity and foreign-key relationships.

## DB-001b: New duplicate structural natural keys are allowed

- Severity: **P1**
- Verification: **dynamic confirmation**
- Module: story-node schema and repository/service validation
- Code location: `schema.sql` story_nodes definition and migrations.
- Production entry: direct planning API, macro plan persistence, daemon act
  planning.
- Trigger / reproduction: insert two rows with same novel, parent, node type,
  and number. Probe count was two.
- Expected: reject a new duplicate natural key while retaining compatibility
  for legacy databases that already contain duplicates.
- Actual: duplicate rows are accepted.
- Impact: ambiguous parent/child planning, duplicate acts/chapters, unstable
  autopilot selection.
- Root cause: only `id` is unique; natural planning identity has no guard.
- Minimal repair: compatible insert/update trigger or equivalent guard that
  blocks a *new* collision but permits edits that do not change an existing
  legacy duplicate's natural key.
- Files expected: migration, schema bootstrap if needed, repository error
  translation, tests for new DB and legacy duplicate DB.
- Explicit non-changes: no simple unique index that can make upgrade fail.
- Required tests: duplicate insert fails, natural-key-changing update fails,
  metadata-only edit of legacy duplicate succeeds.
- Dependencies: DB-001 tests establish safe parent persistence first.
- Rollback risk: medium only for callers relying on invalid duplicate writes;
  they must receive clear validation errors.

## SETTING-001: Locked onboarding settings do not reach final prose prompts

- Severity: **P1**
- Verification: **dynamic confirmation**
- Module: onboarding, Variable Hub, setup context, context allocator, prose
  invocation, writing delegate.
- Code locations: `variable_backfill.py`, `world/bible.py`,
  `setup_context_builder.py`, `context_builder.py`, `narrative_promise.py`,
  `writing_delegate.py`, `prose_composer.py`, chapter prose contract/package.
- Production entry: Home.vue creation -> novel API -> StoryPipeline default
  writer -> CPMS -> final LLM provider.
- Trigger / reproduction: save unique title/genre/theme/world/style/taboo,
  create normal composition and redispatch it to a recording provider.
- Expected: all locked values appear in the final initial and retry provider
  requests as a budgeted settings block.
- Actual: both prompts contain only the theme marker; title, genre, world,
  style, and taboo are absent.
- Impact: new and existing books, automatic writing, manual pipeline use, and
  long-form consistency.
- Root cause: readers use nonexistent top-level aggregate fields instead of
  `Novel.generation_prefs`; allocator lacks novel repository injection;
  narrative promise and chapter contract cannot carry the missing data.
- Minimal repair: centralize the authoritative preference projection, update
  all named readers, inject the repository into the allocator, and bind a
  budgeted settings block to prose composition without bypassing CPMS.
- Files expected: only the named readers/contracts/composer plus tests.
- Explicit non-changes: no new prompt system or unbounded direct prompt
  concatenation.
- Required tests: final-provider spy checks six markers on first and retry
  requests; token total stays within budget; saved/reloaded novel works.
- Dependencies: can begin after P0 because it is otherwise independent.
- Rollback risk: medium prompt behavior changes, mitigated by strict spy tests.

## SETTING-002: Clearing settings leaves stale Variable Hub values

- Severity: **P1**
- Verification: **dynamic confirmation**
- Module: NovelService Variable Hub projection
- Code location: `application/core/services/novel_service.py`
  `_sync_variable_hub_from_novel()` skips `""` and `None`.
- Production entry: settings update API then later planning/writing invocation.
- Trigger / reproduction: write a genre marker, clear it via normal update,
  then read canonical and `novel.setup.*` alias entries.
- Expected: both values are blank/current and no old setting enters later
  requests.
- Actual: both still contain `TRACE_OLD_GENRE_SHOULD_BE_CLEARED`.
- Impact: every book type; stale private/user-authored setting can affect
  subsequent planning and prose.
- Root cause: blank entries are skipped instead of overwriting persistent
  compatibility/canonical records.
- Minimal repair: always write all authoritative keys, including empty string;
  synchronise canonical and aliases with the current value.
- Files expected: NovelService/Variable Hub helper and service tests.
- Explicit non-changes: no deletion of arbitrary user Variables outside the
  known onboarding projection.
- Required tests: set then clear each locked setting, assert aliases and
  canonical keys empty, final provider prompt lacks the former marker.
- Dependencies: SETTING-001; implement in same settings batch.
- Rollback risk: low; removes incorrect stale state.

## AUTOPILOT-001: Restart interruption is not durably explainable

- Severity: **P1**
- Verification: **dynamic confirmation**
- Module: backend lifecycle, novel aggregate, status projection
- Code locations: `interfaces/runtime.py`, `domain/novel/entities/novel.py`,
  `sqlite_novel_repository.py`, schema/migration, autopilot API routes.
- Production entry: FastAPI lifecycle startup after a daemon/process restart.
- Trigger / reproduction: start with persisted `running` novel, run startup,
  reload status.
- Expected: status is stopped and a durable reason identifies service restart;
  explicit user start clears it.
- Actual: status becomes stopped but table has no recovery-reason column.
- Impact: users cannot distinguish a safe manual stop from interrupted work;
  recovery UX and diagnostics are ambiguous.
- Root cause: lifecycle behavior has no domain/persistence/API projection for
  its stop reason.
- Minimal repair: add `autopilot_recovery_reason` to aggregate, schema,
  compatible migration, repository read/write, lifecycle reset, start clear,
  and status DTO/API projection.
- Files expected: the named lifecycle/domain/repository/schema/API tests.
- Explicit non-changes: do not alter canonical aftermath gate semantics.
- Required tests: restart reason persists across reload; user start clears it;
  status endpoint returns it.
- Dependencies: DB-002 persistence work shares a repository test fixture.
- Rollback risk: low, nullable/default-empty compatible column.

## DB-002: Novel full save drops run-recovery fields

- Severity: **P1**
- Verification: **dynamic confirmation**
- Module: SQLite Novel repository
- Code location: `infrastructure/persistence/database/sqlite_novel_repository.py`
  save SQL and parameter projection.
- Production entry: aggregate persistence from lifecycle/pipeline transitions.
- Trigger / reproduction: save a Novel populated with epoch, active step, run
  id, and stable stage, then reload.
- Expected: fields round-trip unchanged.
- Actual: they read as empty string or zero.
- Impact: restart recovery, observability, and any workflow that performs full
  aggregate save.
- Root cause: repository INSERT/UPSERT and argument tuple omit existing
  schema/domain fields.
- Minimal repair: include all four columns in INSERT, conflict update,
  parameters, and hydration; test round trip.
- Files expected: SQLite Novel repository and test fixture.
- Explicit non-changes: no status-machine redesign.
- Required tests: exact round trip, existing partial patch behavior unchanged.
- Dependencies: none; coordinate with AUTOPILOT-001.
- Rollback risk: low.

## DB-003: Fresh database migration order is incomplete on first open

- Severity: **P1**
- Verification: **dynamic confirmation**
- Module: migration runner
- Code locations: `migration_runner.py`,
  `add_macro_diagnosis_context_patch.sql`,
  `add_macro_diagnosis_results.sql`.
- Production entry: SQLite database initialization / upgrade.
- Trigger / reproduction: open a new database once.
- Expected: all macro diagnosis columns exist after one initialization.
- Actual: lexical ordering attempts patch-before-create, logs `no such table`,
  and only a second open adds `context_patch` and `total_words_at_run`.
- Impact: fresh installations have inconsistent schema and later behavior.
- Root cause: filename sort has no declared dependency handling.
- Minimal repair: preserve published filenames and add a narrow stable ordering
  rule for this dependency; do not suppress failed migration as success.
- Files expected: migration runner and migration integration tests.
- Explicit non-changes: no mass migration rename/rewrite.
- Required tests: first-open fresh schema complete; old partial DB upgrades;
  intentional SQL error remains visible/failing.
- Dependencies: none.
- Rollback risk: low.

## BLUEPRINT-002: Direct next-act API bypasses capacity limits

- Severity: **P1**
- Verification: **dynamic confirmation**
- Module: continuous planning service and direct API
- Code locations: `continuous_planning_service.py:create_next_act_auto`,
  `continuous_planning_routes.py`, `act_planning_delegate.py`.
- Production entry: `POST /acts/{act_id}/create-next`.
- Trigger / reproduction: invoke service under a constrained volume; it creates
  the next act without capacity metadata check.
- Expected: same volume, target-chapter, parent, and numbering checks as daemon
  act planning.
- Actual: act 4 is created under `volume-1` and probe records no capacity
  check.
- Impact: invalid volume overflow, duplicate/empty structure, autopilot drift.
- Root cause: service path and daemon delegate implement divergent guards.
- Minimal repair: extract/reuse one preflight validation path in the service
  so the API cannot bypass it.
- Files expected: planning service/delegate tests and route-level test.
- Explicit non-changes: no change to endpoint URL or public DTO.
- Required tests: volume full rejection, target exhausted rejection, invalid
  parent / duplicate number rejection, allowed next act success.
- Dependencies: DATA-001 and DB-001b should land first to avoid structural
  deletion/duplicate side effects while tests exercise planning.
- Rollback risk: medium for callers that depended on invalid overflow.

## SSE-001: General SSE events are not replayable or deduplicated

- Severity: **P2**
- Verification: **code-level confirmation**
- Module: autopilot SSE route and DAG frontend store
- Code locations: `interfaces/api/v1/engine/autopilot_routes.py`,
  `AutopilotTerminalLog.vue`, `frontend/src/stores/dagRunStore.ts`.
- Production entry: browser EventSource reconnect after disconnection.
- Actual: logs have `after_seq`, but general events have no standard event id,
  no `Last-Event-ID` resume and no client deduplication.
- Impact: stale or duplicate cockpit state after reconnect; no current data-loss
  reproduction was induced.
- Minimal repair: add stable event IDs/cursor and client dedupe only after P0/P1
  state contract is fixed.
- Tests: reconnect and replay ordering, duplicate suppression, restart state.
- Dependency / risk: after AUTOPILOT-001; medium protocol compatibility risk.

## OBS-001: Planning parse errors log raw private content

- Severity: **P2**
- Verification: **code-level confirmation**
- Module: continuous planning diagnostics
- Code location: `continuous_planning_service.py:2199-2200`.
- Production entry: malformed LLM planning JSON.
- Actual: first 1,000 and final 500 raw characters are logged.
- Impact: private setting, planning, or model output disclosure in logs.
- Minimal repair: replace raw values with length, digest, parser error class and
  safe position metadata.
- Tests: logger capture proves raw marker absent and diagnostics retained.
- Dependency / risk: independent; low behavior risk.

## TEST-001: Required cross-boundary regression coverage is incomplete

- Severity: **P2**
- Verification: **code-level confirmation**
- Scope: test suite and frontend scripts.
- Actual: no dedicated frontend test/lint scripts; no focused standard SSE
  replay test; no single browser/API test asserting all six locked settings in
  both provider attempts.
- Minimal repair: add only tests needed by the listed fixes and record the
  absent tooling truthfully. Do not fabricate a lint success command.
- Dependency / risk: follows implementation; low.

## Residual Non-blocking Coverage Limits

The frontend package still does not define a standalone lint or test command,
and a real-browser UI plus Mock-LLM full-autopilot journey was not executed.
These are recorded as explicit non-blocking acceptance limits. They do not
invalidate the passing backend, API, SSE, StoryPipeline, and production-build
evidence, but they prevent a claim that browser-level UI behavior has been
fully exercised.
