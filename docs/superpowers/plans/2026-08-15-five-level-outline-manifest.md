# Five-Level Outline Manifest Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: use
> `superpowers:test-driven-development` for every behavior change. Execute one
> task at a time, review the diff, and commit only after the listed gates pass.

**Goal:** Replace node-level planning authority with one immutable book-level
five-level plan manifest while preserving Candidate-first, Canonical, Memory,
Advance, and Worldline behavior.

**Architecture:** Extend the existing OutlineContract content/version model
with a single persisted planning Head and immutable PlanRevision membership.
StoryNode becomes a guarded compatibility projection. All plan switches,
projection writes, and affected Candidate invalidation share one SQLite writer
transaction.

**Tech Stack:** Python 3.14, FastAPI, SQLite, pytest, Vue 3, TypeScript, Naive
UI, Vitest, Vite, Playwright.

## Global Constraints

- Work only in `W:\novel\test` until all tests, commits, and push succeed.
- Starting HEAD is `149e3207d83bb4abff20ec3b0a5886dd3ddd81bd`.
- Five levels are exactly `outline -> part -> volume -> act -> chapter`.
- One novel has exactly one planning authority at a time: `legacy` or
  `manifest`.
- Existing Candidate-first, Formal hash/revision, Canonical Aftermath, Memory
  Barrier, Advance Exactly Once, and Worldline guards must remain intact.
- Do not change prose/review prompts, model parameters, temperatures, token
  budgets, Canonical algorithms, or Memory algorithms.
- Planning drafts, stale/superseded manifests, Candidate prose, and retired
  worldlines must never enter prose Context.
- Do not edit existing published migration files or delete legacy read paths.
- Ordinary future replanning may never delete or mutate Formal history.
- Sealed ContractVersion, PlanRevision, and PlanRevisionItem rows are immutable.
- Every production change starts with a failing behavior test and every task
  ends with `git diff --check` plus an independent commit.

## File Map

### New files

- `infrastructure/persistence/database/migrations/031_outline_plan_manifests.sql`
  - Head, PlanRevision, PlanRevisionItem, Candidate pins, attempt scope,
    indexes, and immutable-row triggers.
- `domain/structure/outline_plan.py`
  - Pure plan/head/item/reconciliation value types and manifest digest.
- `application/blueprint/services/outline_plan_validation.py`
  - Pure deterministic cohort and impact-closure functions.
- `infrastructure/persistence/database/outline_plan_projection_writer.py`
  - Capability-bound compatibility projection writes.
- `tests/unit/domain/structure/test_outline_plan.py`
- `tests/unit/application/blueprint/test_outline_plan_validation.py`
- `tests/integration/infrastructure/persistence/database/test_outline_plan_repository.py`
- `tests/integration/application/blueprint/test_outline_manifest_lifecycle.py`

### Existing backend files to modify

- `domain/structure/outline_contract.py`
- `infrastructure/persistence/database/outline_contract_repository.py`
- `infrastructure/persistence/database/story_node_repository.py`
- `infrastructure/persistence/database/chapter_candidate_repository.py`
- `infrastructure/persistence/database/sqlite_chapter_repository.py`
- `application/blueprint/services/outline_contract_service.py`
- `application/blueprint/services/outline_draft_generation_service.py`
- `application/blueprint/services/chapter_book_structure_sync.py`
- `application/blueprint/services/continuous_planning_service.py`
- `application/blueprint/services/story_structure_service.py`
- `application/blueprint/services/volume_summary_service.py`
- `application/core/services/novel_service.py`
- `application/engine/services/candidate_chapter_workflow.py`
- `application/engine/services/autopilot_recovery_policy.py`
- `application/core/services/chapter_rewrite_coordinator.py`
- `application/engine/services/worldline_regeneration_service.py`
- `application/engine/services/worldline_rebuild_service.py`
- `application/world/services/bible_service.py`
- `engine/runtime/act_planning_delegate.py`
- `interfaces/api/v1/blueprint/outline_routes.py`
- `interfaces/api/v1/blueprint/story_structure.py`
- `interfaces/api/v1/blueprint/continuous_planning_routes.py`
- `interfaces/api/v1/engine/generation.py`
- `scripts/backup_novel.py`

### Existing frontend files to modify

- `frontend/src/api/structure.ts`
- `frontend/src/domain/outlinePresentation.ts`
- `frontend/src/domain/outlinePresentation.spec.ts`
- `frontend/src/api/structure.spec.ts`
- `frontend/src/views/OutlineStudio.spec.ts`
- `frontend/src/views/OutlineStudio.vue`
- `frontend/src/components/StoryStructureTree.vue`

### Additional integration tests

- `tests/integration/scripts/test_backup_novel.py`
- `tests/integration/infrastructure/persistence/database/test_sqlite_novel_repository_delete.py`

---

## Task 0: Freeze The Current Behavioral Baseline

**Files:**

- Create: none.
- Modify: design and implementation-plan documents only.

**Produces:** A reviewed current-HEAD specification plus recorded green safety
baseline. Missing manifest behavior is introduced red-first inside Tasks 1-9;
the branch never commits intentionally failing tests.

- [x] Run the existing safety baseline and record the result:

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/domain/structure/test_outline_contract.py `
  tests/integration/infrastructure/persistence/database/test_outline_contract_repository.py `
  tests/unit/application/engine/test_candidate_chapter_workflow.py `
  tests/integration/infrastructure/persistence/database/test_chapter_candidate_repository.py `
  tests/integration/application/engine/test_worldline_regeneration_service.py -v
```

Recorded at starting HEAD `149e3207d83bb4abff20ec3b0a5886dd3ddd81bd`:
`96 passed in 28.64s` on 2026-08-15.

- [ ] Run `git diff --check` and commit only tests/docs:

```powershell
git add docs/superpowers/specs/2026-08-15-five-level-outline-manifest-design.md `
  docs/superpowers/plans/2026-08-15-five-level-outline-manifest.md
git commit -m "docs: define outline manifest refactor"
```

## Task 1: Add Immutable Plan Storage And Backfill

**Files:**

- Create: `infrastructure/persistence/database/migrations/031_outline_plan_manifests.sql`
- Create: `domain/structure/outline_plan.py`
- Create: `tests/unit/domain/structure/test_outline_plan.py`
- Create: `tests/integration/infrastructure/persistence/database/test_outline_plan_repository.py`
- Create: `tests/integration/application/blueprint/test_outline_manifest_lifecycle.py`
- Modify: `infrastructure/persistence/database/outline_contract_repository.py`
- Modify: `tests/unit/infrastructure/persistence/database/test_migration_runner.py`

**Interfaces:**

```python
class PlanningAuthorityMode(str, Enum):
    LEGACY = "legacy"
    MANIFEST = "manifest"

class PlanReconciliationStatus(str, Enum):
    ALIGNED = "aligned"
    REPAIRABLE = "repairable"
    AUTHOR_DECISION_REQUIRED = "author_decision_required"

def canonical_plan_digest(
    *, canonical_prefix_digest: str, items: Sequence[OutlinePlanItem]
) -> str: ...

OutlineContractRepository.ensure_planning_head(novel_id: str) -> PlanningHead
OutlineContractRepository.create_plan_draft(...) -> OutlinePlanRevision
OutlineContractRepository.get_plan_revision(plan_revision_id: str) -> OutlinePlanRevision
OutlineContractRepository.get_active_plan(novel_id: str) -> OutlinePlanRevision | None
OutlineContractRepository.backfill_initial_plan(novel_id: str) -> BackfillResult
```

- [ ] Write migration tests for fresh DB, upgrade from 030, rerun
  idempotency, foreign-key enforcement, one Head per novel, item uniqueness,
  non-authoritative working draft pointer, Candidate columns, attempt plan
  scope, and sealed-row UPDATE/DELETE rejection.
- [ ] Verify the migration tests fail before adding migration 031.
- [ ] Add SQL tables and columns exactly as defined in the design. Use
  triggers for sealed PlanRevision/Item immutability and `RESTRICT` foreign
  keys for historical versions.
- [ ] Add pure domain parsing and canonical digest tests. Digest input order
  must not change output; topology or version digest changes must.
- [ ] Add repository tests for initial legacy Head, draft creation, identical
  version reuse, valid backfill, and `planning_migration_required` on mixed
  invalid data.
- [ ] Add deletion tests proving direct sealed-row deletion fails while an
  owning-novel delete leaves no Head, revision, item, attempt, or plan-pin row.
- [ ] Implement the minimum repository methods. Do not switch any book to
  manifest mode in this task.
- [ ] Run:

```powershell
.\.venv\Scripts\python.exe -m pytest `
  tests/unit/domain/structure/test_outline_plan.py `
  tests/unit/infrastructure/persistence/database/test_migration_runner.py `
  tests/integration/infrastructure/persistence/database/test_outline_plan_repository.py -v
git diff --check
```

- [ ] Commit:

```powershell
git add domain/structure/outline_plan.py `
  infrastructure/persistence/database/migrations/031_outline_plan_manifests.sql `
  infrastructure/persistence/database/outline_contract_repository.py `
  tests/unit/domain/structure/test_outline_plan.py `
  tests/unit/infrastructure/persistence/database/test_migration_runner.py `
  tests/integration/infrastructure/persistence/database/test_outline_plan_repository.py
git commit -m "feat: add immutable outline plan manifests"
```

## Task 2: Enforce Persistence Guards And Projection Ownership

**Files:**

- Create: `infrastructure/persistence/database/outline_plan_projection_writer.py`
- Modify: `infrastructure/persistence/database/story_node_repository.py`
- Modify: `infrastructure/persistence/database/outline_contract_repository.py`
- Modify direct StoryNode DML callers listed in the design and Phase 0 audit:
  `continuous_planning_service.py`, `story_structure_service.py`,
  `engine/runtime/act_planning_delegate.py`,
  `application/core/services/novel_service.py`,
  `sqlite_chapter_repository.py`, `autopilot_recovery_policy.py`,
  `chapter_rewrite_coordinator.py`, `volume_summary_service.py`,
  `chapter_book_structure_sync.py`, and `bible_service.py`.
- Modify: `tests/integration/infrastructure/persistence/database/test_story_node_repository.py`
- Modify: `tests/integration/infrastructure/persistence/database/test_outline_contract_repository.py`
- Modify the focused unit tests for each converted direct writer.

**Interfaces:**

```python
@dataclass(frozen=True)
class ProjectionWriteCapability:
    novel_id: str
    plan_revision_id: str
    authority_generation: int
    connection_identity: int

PlanProjectionWriter.apply(
    conn: sqlite3.Connection,
    capability: ProjectionWriteCapability,
    items: Sequence[ProjectionItem],
) -> None

StoryNodeRepository.patch_runtime_fields(
    novel_id: str,
    node_id: str,
    *,
    word_count: int | None = None,
    status: str | None = None,
    runtime_metadata: Mapping[str, Any] | None = None,
) -> StoryNode
```

- [ ] Write failing tests for protected StoryNode create/update/delete/reorder,
  runtime whitelist success, metadata namespace isolation, wrong/expired/
  cross-connection capability rejection, and no generic force bypass.
- [ ] Write failing tests that legacy OutlineContract mutators reject or
  delegate when the Head is manifest mode.
- [ ] Add a direct-DML test for every runtime caller found in Phase 0. Each
  manifest-mode path must fail/no-op safely or use projection capability.
- [ ] Implement the shared Head policy check at both repositories.
- [ ] Implement projection capability as an internal object created only from
  a live connection after Head CAS. Never serialize or expose it through API.
- [ ] Convert direct runtime StoryNode planning DML. Do not grant backup,
  clone, or CLI code a live-book bypass.
- [ ] Replace whole-metadata updates with namespaced runtime patches so no
  caller can overwrite `planning.*` indirectly.
- [ ] Narrow `chapter_book_structure_sync` to empty draft-placeholder repair;
  it must reject Formal, formal-commit, and nonempty chapter deletion.
- [ ] Run focused repository/caller tests plus:

```powershell
git grep -n -E "(INSERT INTO|UPDATE|DELETE FROM) story_nodes" -- "*.py"
.\.venv\Scripts\python.exe -m pytest `
  tests/integration/infrastructure/persistence/database/test_story_node_repository.py `
  tests/integration/infrastructure/persistence/database/test_outline_contract_repository.py `
  tests/unit/application/engine/test_autopilot_recovery_policy.py `
  tests/unit/application/services/test_chapter_rewrite_candidate_barrier.py -v
git diff --check
```

- [ ] Commit `feat: guard outline projection writes`.

## Task 3: Make Manifest The Read Authority And Reconcile Formal History

**Files:**

- Modify: `application/blueprint/services/outline_contract_service.py`
- Modify: `infrastructure/persistence/database/outline_contract_repository.py`
- Modify: `application/engine/services/candidate_chapter_workflow.py`
- Modify: `tests/unit/application/blueprint/test_outline_contract_service.py`
- Modify: `tests/unit/application/engine/test_candidate_chapter_workflow.py`
- Modify: `tests/integration/test_context_builder_integration.py`

**Interfaces:**

```python
OutlineContractService.active_manifest_chain_for_chapter(
    novel_id: str, chapter_number: int
) -> dict[str, Any]

OutlineContractService.compute_canonical_prefix(
    novel_id: str, through_chapter: int
) -> CanonicalPrefix

OutlineContractService.reconcile_plan_boundary(
    *, novel_id: str, plan_revision_id: str
) -> ReconciliationReport
```

- [ ] Write failing tests that active chain reads PlanItems rather than
  StoryNode parent traversal and excludes draft/stale/history.
- [ ] Write exact prefix tests for legacy Formal, Candidate-first Formal,
  missing chapter, mismatched hash/revision, wrong narrative-commit version,
  and stable ordering. Assert runtime `generation_epoch` changes do not change
  the digest.
- [ ] Write reconciliation tests for aligned, repairable, and author-decision
  outcomes. Canonical and Memory readiness must be separate gates.
- [ ] Implement manifest chain assembly and prefix calculation using existing
  tables; add no Canonical digest column and change no Canonical/Memory code.
- [ ] Change no-plan-next-chapter behavior to persisted
  `waiting_planning/expand_outline_cohort`, only after current Canonical and
  Memory are ready.
- [ ] Add Context sentinels proving Bible, active five-level outline,
  Canonical, Recent, Memory, and Vector are present while Candidate and retired
  plan sentinels are absent.
- [ ] Run focused service, workflow, and Context tests; commit
  `feat: read prose plans from active manifests`.

## Task 4: Generate And Validate Complete Sibling Cohorts

**Files:**

- Create: `application/blueprint/services/outline_plan_validation.py`
- Create: `tests/unit/application/blueprint/test_outline_plan_validation.py`
- Modify: `application/blueprint/services/outline_draft_generation_service.py`
- Modify: `infrastructure/persistence/database/outline_contract_repository.py`
- Modify: `interfaces/api/v1/blueprint/outline_routes.py`
- Modify existing outline generation/API tests.

**Interfaces:**

```python
OutlineDraftGenerationService.generate_cohort(
    *, plan_revision_id: str, parent_logical_node_id: str,
    author_intent: str, retry_attempt_id: str | None = None
) -> AsyncIterator[dict[str, Any]]

validate_outline_cohort(
    *, parent: OutlinePlanItem, siblings: Sequence[OutlinePlanItem],
    canonical_boundary: Mapping[str, Any]
) -> CohortValidationReport
```

- [ ] Write red tests for matrix-first generation, sequential synopsis
  expansion, whole-cohort review, persisted attempt recovery/cancel/retry, and
  no active-plan change on failure.
- [ ] Write pure validator tests for range gaps/overlap, entry/exit mismatch,
  backward time, missing parent-goal coverage, wrong final exit, state/task/
  foreshadow discontinuity, author-lock overwrite, Canonical prefix mutation,
  and chapter rhythm violations.
- [ ] Extend attempts with plan/cohort scope and reuse existing durable events.
  Do not add a second attempt/event system.
- [ ] Replace one-node planning calls with the cohort contract only in the
  five-level planner. Do not alter prose prompts or provider settings.
- [ ] Expose create/resume/cancel/read cohort endpoints; no endpoint publishes
  one sibling.
- [ ] Run pure, service, repository, and API tests; commit
  `feat: generate complete outline cohorts`.

## Task 5: Add Author Locks, Diff, Impact Closure, And Future Replan

**Files:**

- Modify: `domain/structure/outline_contract.py`
- Modify: `domain/structure/outline_plan.py`
- Modify: `application/blueprint/services/outline_plan_validation.py`
- Modify: `application/blueprint/services/outline_contract_service.py`
- Modify: `infrastructure/persistence/database/outline_contract_repository.py`
- Modify: `interfaces/api/v1/blueprint/outline_routes.py`
- Modify relevant domain/service/API tests.

**Interfaces:**

```python
merge_outline_payload(
    *, base: OutlinePayload, author: OutlinePayload | None,
    generated: OutlinePayload
) -> OutlinePayload

compute_plan_impact(
    *, base_plan: OutlinePlanRevision, changed_logical_node_ids: set[str]
) -> PlanImpact

OutlineContractService.replan_future(
    *, novel_id: str, start_chapter: int, author_intent: str,
    expected_head_generation: int
) -> OutlinePlanRevision
```

- [ ] Write red tests that locked text is byte-identical after AI merge and
  unlocked empty structure fields are filled with AI provenance.
- [ ] Write red impact tests for downward descendants, later siblings and
  descendants, and ancestor summaries. Test deterministic reuse and default
  fail-closed behavior.
- [ ] Write the `ABCDEF -> ABCHJK` integration test: A/B/C exact versions
  reused, D/E/F absent only from the new manifest, H/J/K continuous, old
  manifest still renders D/E/F.
- [ ] Reject future replan at or before Formal head with an
  `author_decision_required/worldline_required` result.
- [ ] Implement plan Diff and impact API output without deleting old versions.
- [ ] Run domain, service, repository, and API tests; commit
  `feat: support versioned future outline replanning`.

## Task 6: Publish Atomically And Pin Candidate Authority

**Files:**

- Modify: `infrastructure/persistence/database/outline_contract_repository.py`
- Modify: `infrastructure/persistence/database/chapter_candidate_repository.py`
- Modify: `application/engine/services/candidate_chapter_workflow.py`
- Modify: `interfaces/api/v1/blueprint/outline_routes.py`
- Modify candidate/outline repository, workflow, and route tests.

**Interfaces:**

```python
OutlineContractRepository.publish_plan_revision(
    *, plan_revision_id: str, expected_head_generation: int,
    expected_head_digest: str, expected_canonical_prefix_digest: str,
    idempotency_key: str
) -> PublishPlanResult

ChapterCandidateRepository.assert_plan_pin_current(
    conn: sqlite3.Connection, candidate_id: str
) -> None
```

- [ ] Write fault-injection tests after every publish step. Any exception must
  leave Head, projection, Candidate/Audit, and manifest visibility unchanged.
- [ ] Write idempotent retry tests and competing publisher CAS tests.
- [ ] Write Candidate tests for generation-result save, approve, and Formal
  CAS. A changed far future plan retaining the exact pinned chain remains
  valid; an affected chain becomes stale.
- [ ] Assert affected `committing/syncing` blocks plan publication. Assert
  already Formal Candidate provenance remains readable and is never revoked.
- [ ] Move affected Candidate/Audit invalidation into the same writer
  transaction as projection and Head switch.
- [ ] Implement per-book `repairable` auto-publish conditions; manual mode and
  unsafe continuous mode stop at `ready_for_review`.
- [ ] Run all outline/candidate repository, workflow, route, and concurrency
  tests; commit `feat: publish outline plans atomically`.

## Task 7: Integrate Rolling Expansion And Worldline Rebase

**Files:**

- Modify: `application/engine/services/candidate_chapter_workflow.py`
- Modify: `application/engine/services/autopilot_recovery_policy.py`
- Modify: `application/engine/services/worldline_regeneration_service.py`
- Modify: `application/engine/services/worldline_rebuild_service.py`
- Modify: `application/core/services/chapter_rewrite_coordinator.py`
- Modify Worldline, recovery, workflow, and Memory-barrier tests.

- [ ] Write red tests that no outline after a ready chapter produces a normal
  `expand_outline_cohort` wait state and zero N+1 prose LLM calls.
- [ ] Write crash tests for planning wait, completed cohort before publish,
  published plan before Candidate start, and Memory committed before Advance.
- [ ] Extend Worldline archive entries to include Head, PlanRevision IDs,
  projection mapping, and durable lineage identity without deleting immutable
  manifests.
- [ ] Restore in foreign-key order: contracts/versions, revisions, items,
  Candidate/attempt plan references, projection mapping, then Head CAS last.
  Fault injection at each boundary must leave the old Head fully visible.
- [ ] Write rebase/restore tests proving old tail plans and Memory/Vector facts
  are invisible and restore changes Head atomically without mixed projection.
- [ ] Preserve current Canonical/Memory/Advance implementation and call order.
- [ ] Run workflow, recovery, Worldline, Context, and Memory tests; commit
  `feat: align outline plans with worldline recovery`.

## Task 8: Retire Legacy Planning Writes At Cutover

**Files:**

- Modify: `interfaces/api/v1/blueprint/story_structure.py`
- Modify: `interfaces/api/v1/blueprint/continuous_planning_routes.py`
- Modify: `interfaces/api/v1/engine/generation.py`
- Modify: `application/blueprint/services/continuous_planning_service.py`
- Modify route/service tests.

- [ ] Write route tests for read-only structure queries and stable `410` on
  incompatible mutation endpoints after manifest cutover.
- [ ] For an old endpoint with equivalent whole-cohort semantics, write an
  adapter test proving it delegates to the same manifest use case and cannot
  publish partially.
- [ ] Remove normal-workflow reachability to chapter/structure purge. Retain
  only isolated legacy repair and confirmed Worldline paths.
- [ ] Add static tests/audit output proving no legacy endpoint or background
  path is a second Planning Authority.
- [ ] Run all blueprint/generation route tests and direct-write scans; commit
  `refactor: retire legacy outline write authorities`.

## Task 9: Build The Outline Studio Workflow

**Files:**

- Modify frontend files listed in File Map.
- Add focused domain/component specs next to the changed frontend modules.

- [ ] Write Vitest red tests for complete cohort rendering, payload round
  trip, field provenance/locks, version Diff, impact preview, conflict choice,
  attempt reconnect/cancel, and no partial publish action.
- [ ] Update `structure.ts` DTOs and APIs without dropping `state_changes`,
  `extra`, structured continuity, or unknown compatible payload fields.
- [ ] Make literary synopsis the primary editor. Put continuity, task,
  foreshadow, and constraints in secondary tabs.
- [ ] Add whole-cohort generate/validate/publish and future-replan workflows.
- [ ] Make `StoryStructureTree` read-only navigation with active revision,
  sync, stale, parent-lock, and expansion state; edit opens Outline Studio.
- [ ] Verify desktop and mobile with Playwright screenshots. Ensure controls do
  not overlap, text fits, and progress state cannot resize the layout.
- [ ] Run:

```powershell
Set-Location frontend
npm run lint
npm run test:unit
npm run build
Set-Location ..
git diff --check
```

- [ ] Commit `feat: add cohort outline studio workflow`.

## Task 10: Full Verification, Review, Push, And Formal Sync

- [ ] Run the complete backend suite:

```powershell
.\.venv\Scripts\python.exe -m pytest tests/ -v
```

- [ ] Run frontend lint, unit, build, and the focused Playwright E2E.
- [ ] Run migration tests against fresh, legacy-only, mixed, existing Formal,
  and Worldline archive fixtures.
- [ ] Run clone tests for planning ID/FK remapping, empty Canonical boundary,
  fresh Head, and exclusion of attempts, open Candidate/Audit, idempotency, and
  lineage rows. Run whole-novel deletion cleanup tests for every new table.
- [ ] Run final direct-write audits for StoryNode, OutlineContract, chapters,
  Candidate Formal, Narrative Commit, Memory update, and Advance entry points.
- [ ] Re-run the full manual, continuous, crash, replan, Worldline,
  concurrency, and Context-sentinel matrix from the design.
- [ ] Run:

```powershell
git diff --check
git status
git log --oneline -30
```

- [ ] Perform a whole-branch code review. Fix all load-bearing findings and
  re-run affected tests.
- [ ] Push only after all gates pass. Verify the remote branch resolves to the
  final local HEAD.
- [ ] In `W:\novel\PlotPilot`, require a clean worktree, pull the pushed branch,
  and verify local HEAD equals remote. Never copy uncommitted files directly.

## Final No-Go Conditions

Stop the current task immediately if any test or review proves:

- two planning authorities can affect one manifest-mode book;
- a sealed version or manifest can be mutated or deleted;
- a plan publish can become partially visible;
- a legacy or direct SQL writer can change protected StoryNode planning data;
- a Candidate can cross an affected plan change at LLM, approval, or Formal;
- future replan changes Formal/Canonical/Memory/Vector state;
- Worldline restore exposes old-tail facts or mixed plan projections;
- Memory failure permits N+1 prose generation;
- author-locked text changes;
- Context loses required Bible/history/memory data or includes draft/retired
  planning data.
