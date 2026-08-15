# Architecture Convergence Execution Plan - Current HEAD

## Baseline

- Base HEAD: `36fd3c65fbaa5a1916d62c6cd089c0125064aee6`
- Branch: `codex/plotpilot-memory-stability`
- Starting worktree: clean
- Scope: minimum evidence-backed convergence only

## Dependency Graph

```text
Phase 0 characterization/docs
  -> Phase 1 Planning Context authority
  -> Phase 2 Candidate post-formal recovery/CAS
  -> Phase 3 Worldline formal-head authority
  -> Phase 4 Candidate DAG redundant planning call
  -> Phase 5 lifecycle/docs/direct-write audit
  -> Phase 6 full verification and delivery
```

Core Candidate/Canonical phases are serial. No two writers modify the same
business authority at once.

## Global Guardrails

- Work, tests, and commits only in `W:\novel\test`.
- Keep both `chapter_review` and `continuous` modes Candidate-first.
- Keep Prompt text, provider/model/temperature/token settings, outline schema,
  database schema, and completed-beats window unchanged.
- Reuse OutlineContract, Candidate repository, narrative commit repository,
  and worldline services; do not introduce parallel authorities.
- Every production fix follows red -> green -> focused regression ->
  `git diff --check` -> independent commit.

## Phase 0 - Current-HEAD Characterization and Rebaseline

**Goal:** Freeze current behavior and record which old-ticket items still
exist.

**Current problem:** Old local architecture documents describe `c60237b` plus
a dirty worktree, not clean HEAD `36fd3c65`.

**Evidence:** `git log c60237b..HEAD` contains one 114-file commit; task-start
status is clean; full baseline is green.

**Files:**

- `docs/architecture_convergence_rebaseline.md`
- `docs/architecture_convergence_execution_plan_current_head.md`

**Allowed:** Documentation only.

**Forbidden:** Production edits.

**Tests:** `git diff --check`; verify status contains only the two documents.

**Completion:** Both documents describe Starting HEAD, clean worktree,
authorities, old-ticket decisions, new findings, and phase gates.

**Rollback:** Remove only these new document changes; never reset the branch.

## Phase 1 - Planning Context Authority

**Goal:** Ensure Candidate prompts only see the published five-level planning
authority plus non-plan Bible/premise data.

**Current problem:** `ContextAssembler.build_story_anchor()` reads raw root and
act `story_nodes.description/narrative_arc`; the allocator injects that text at
T0 even when it is unpublished or stale.

**Code evidence:**

- `application/engine/services/context_assembler.py::build_story_anchor`
- `application/engine/services/context_budget_allocator.py::_collect_all_slots`
- `application/blueprint/services/outline_contract_service.py::published_context_for_chapter`

**Files:**

- Test: existing ContextBuilder/Assembler integration test file
- Production: `application/engine/services/context_assembler.py`

**Allowed:** Replace raw StoryNode planning text with existing non-plan novel
premise/storyline inputs or omit it when no authoritative non-plan anchor is
available.

**Forbidden:** Outline schema/publish rules, Prompt templates, rhythm fields,
token tiers/quotas, Candidate repository, Canonical/Memory code.

**Tests:** Production-shaped sentinel: Bible + published outline present;
unpublished/raw StoryNode marker absent.

**Completion:** Red test proves the leak; green test proves only authoritative
planning reaches the Candidate context.

**Rollback:** Revert this phase if Bible or published Outline sentinels disappear
or Context fingerprints change beyond the removed raw marker.

## Phase 2 - Candidate Post-Formal Recovery and Publication CAS

**Goal:** Once Formal exists, allow only exact-version Canonical retry and never
publish an old sync as ready.

**Current problems:**

1. `edit_content()` and `request_regeneration()` accept `FAILED` even when
   `formal_chapter_id` exists.
2. Restart turns Candidate `SYNCING` into retryable `FAILED` but leaves the
   exact narrative claim `in_progress`.
3. `mark_sync_succeeded()` publishes ready without a final in-transaction
   Candidate/formal/chapter version comparison.

**Code evidence:**

- `ChapterCandidateRepository.edit_content/request_regeneration`
- `ChapterCandidateRepository.recover_after_service_restart`
- `ChapterCandidateRepository.mark_sync_succeeded`
- `SqliteChapterNarrativeCommitRepository.claim/reclaim_terminal_failure`

**Files:**

- Test: `tests/integration/infrastructure/persistence/database/test_chapter_candidate_repository.py`
- Test: `tests/unit/application/engine/test_candidate_chapter_workflow.py`
- Production: `infrastructure/persistence/database/chapter_candidate_repository.py`
- Production only if the failing recovery test requires it:
  `application/engine/services/candidate_chapter_workflow.py`

**Allowed:** Reuse `_formal_version_matches`; add minimal exact-version/status
guards; retire only the abandoned exact narrative claim on known service
restart.

**Forbidden:** New state machine, new recovery service, schema change, LLM
retry changes, Narrative Sync algorithm changes.

**Tests:**

- Formal-sync-failed Candidate rejects edit and regenerate but accepts retry.
- Restarted syncing Candidate can retry the same formal content without a new
  prose-generation call.
- Rewrite between aftermath and Candidate publication fails closed and does not
  advance `current_formal_chapter`.

**Completion:** All three tests fail for the expected pre-fix reason, then pass;
existing dual-mode/retry/rewrite tests remain green.

**Rollback:** Revert if legal pre-formal author edits, re-audit, Candidate sync
retry, or rewrite/worldline rebuild is blocked.

## Phase 3 - Worldline Formal-Head Authority

**Goal:** Make worldline preview and execution use the same continuous formal
head as Candidate generation.

**Current problem:** Two raw `MAX(chapters.number)` queries count empty drafts
and placeholders as generated Formal chapters.

**Code evidence:**

- `WorldlineRegenerationService.preview`
- `WorldlineRegenerationService.execute`
- `ChapterCandidateRepository.formal_chapter_head`
- `ChapterCandidateRepository.assert_formal_history_is_proven`

**Files:**

- Test: `tests/integration/application/engine/test_worldline_regeneration_service.py`
- Production: `application/engine/services/worldline_regeneration_service.py`

**Allowed:** Reuse the Candidate repository authority for head/provenance.

**Forbidden:** Archive schema, rebuild algorithm, epoch semantics, destructive
tail table list, run-mode semantics.

**Tests:** Formal head 10 plus empty draft 11: starting at 11 is `continue`, the
draft does not enlarge the archive range, and preview/execute use the same head.

**Completion:** Red test reproduces wrong regenerate classification; green test
uses Formal head and all existing worldline tests pass.

**Rollback:** Revert if proven legacy formal-history imports or Candidate formal
prefixes can no longer preview/execute.

## Phase 4 - Remove Redundant Candidate DAG Planning Call

> **Superseded execution order:** Do not start this phase until Phase 3A below
> is complete. The five-level Manifest publish transaction is a Planning
> Authority P0; DAG call reduction is not.

## Phase 3A - Atomic Manifest Publish and Candidate Pin Barrier

**Goal:** Publish a sealed, aligned Manifest revision, update its physical
StoryNode projection, stale only affected non-Formal Candidates, and switch
the active Head in one SQLite transaction.

**Current problem:** `PlanProjectionWriter.apply_atomic()` owns projection and
Head CAS, while `ChapterCandidateRepository.stale_candidates_for_outline_contract()`
commits separately. The legacy route therefore has a publish-to-stale window
in which an old Candidate can still be approved or enter Formal.

**Code evidence:**

- `infrastructure/persistence/database/plan_projection_writer.py::apply_atomic`
- `infrastructure/persistence/database/chapter_candidate_repository.py::stale_candidates_for_outline_contract`
- `interfaces/api/v1/blueprint/outline_routes.py`

**Files:**

- Test: existing Manifest projection/write-guard and Candidate repository test
  modules, plus one focused Manifest publish integration test when necessary.
- Production: `plan_projection_writer.py`, the smallest Candidate repository
  helper needed for transaction-owned scoped stale, and a Manifest publish
  application service or adapter. The legacy outline route may only delegate
  or reject; it must not retain a second publishing transaction.

**Allowed:** Extend the existing projection writer's private transaction so it
can invoke a narrowly typed, same-connection stale operation before Head
activation; preserve Formal Candidate provenance; reject incomplete, stale,
unsealed, unaligned, or non-current-CAS publish attempts.

**Forbidden:** Expose `ProjectionWriteCapability`; restore node-level planning
authority; change Candidate-first, Formal/Canonical/Memory algorithms, Prompt
contracts, Worldline semantics, or stale a Formal Candidate solely because a
future plan changed.

**Tests:**

- Publish failure leaves old Head, projection, and Candidates unchanged.
- A Candidate whose exact chapter chain is unchanged remains usable after a
  future-only replan.
- A pending Candidate whose pinned chain is affected becomes stale in the same
  transaction as the Head switch; an injected concurrent approve/Formal
  attempt is rejected.
- A Formal Candidate remains provenance-only and is never retracted by plan
  publication.
- Head digest/generation/CAS mismatch rolls back projection and candidate
  changes together.

**Completion:** A single begin/commit boundary owns projection DML, scoped
Candidate stale, and Head activation. No API route can publish a Manifest then
call stale in a second transaction.

**Rollback:** Revert the phase if a future-only plan change needlessly stales
an unchanged current Candidate, or if a projection/Head failure can leave a
partially switched plan.

**Goal:** Candidate DAG executes published required events/rhythm once without
an unused second outline-decomposition LLM call.

**Current problem:** Default DAG injects `plan_outline`; Candidate `BeatNode`
rebuilds its own plan from the published chapter payload and does not consume
`chapter_plan_json`.

**Code evidence:**

- `application/engine/dag/models.py::get_default_dag`
- `application/engine/dag/nodes/planning_chapter_outline_node.py`
- `application/engine/dag/nodes/execution_nodes.py::BeatNode.execute`
- `CandidateChapterWorkflowService._generate_candidate_with_authority`

**Files:** Existing DAG/candidate tests and the smallest existing DAG model or
candidate factory file needed to disable the node in Candidate mode.

**Allowed:** Candidate-mode skip/disable using existing node conditions or DAG
factory inputs.

**Forbidden:** Prompt text, node prompt, published outline/rhythm schema,
non-Candidate custom DAG semantics, new DAG definition.

**Tests:** Candidate default DAG makes zero outline-decomposition LLM calls and
still delivers published beats/rhythm to writer/audit nodes.

**Completion:** Red/green call-count test and existing DAG engine/registry/
Candidate tests pass.

**Rollback:** Revert if Candidate loses required events, rhythm, DAG trace, or
bounded revision behavior.

## Phase 5 - Authority Documentation and Final Direct-Write Audit

**Goal:** Make the active lifecycle discoverable and classify every remaining
writer/advance path.

**Current problem:** `docs/ARCHITECTURE.md` describes legacy daemon flow as
primary and `docs/CHAPTER_LIFECYCLE.md` does not exist.

**Files:**

- `docs/ARCHITECTURE.md`
- `docs/CHAPTER_LIFECYCLE.md`
- final report/documentation only

**Allowed:** Documentation and deletion of code only if `git grep` proves zero
runtime/public/dynamic use and replacement tests already exist.

**Forbidden:** Cosmetic large-file splitting; removal of legacy fallback,
queue, checkpoint, V1/V2, alias, or transaction code without proof.

**Tests:** Re-run all required `git grep` writer/Memory/advance scans and link
each production hit to its authority and guard.

**Completion:** A developer can trace Manual, Continuous, Rewrite, Worldline,
Recovery, and Context paths from 8-10 named authority entry points.

**Rollback:** Revert if docs claim an authority not supported by code.

## Phase 6 - Full Verification and Delivery

**Goal:** Prove P0 contracts on the final tree, create phase commits, push, and
fast-forward the formal workspace only after remote confirmation.

**Tests:**

```powershell
Set-Location W:\novel\test
.\.venv\Scripts\python.exe -m pytest tests/ -v
Set-Location frontend
npm run lint
npm run test:unit
npm run build
Set-Location ..
git diff --check
git status
git log --oneline -20
```

Then repeat direct-write scans for chapters, narrative commits, Memory updates,
and both advance entry points.

**Allowed:** Phase-specific commits, push named branch, verify remote SHA, then
fast-forward `W:\novel\PlotPilot` from remote.

**Forbidden:** Push with a failed P0 or verification gate; hand-copy files into
the formal workspace; force push/reset/clean.

**Completion:** All P0 rows pass, all commands exit zero, remote matches final
test HEAD, and formal workspace matches the remote with a clean status.

**Rollback:** Stop before push/sync on any failed gate; fix in the owning phase
without destructive history operations.

## Explicitly Deferred

- Candidate prompt duplication and post-allocation plan token accounting: real
  but prohibited by the first-pass Prompt/context-budget freeze.
- Generic shared-state DAG observability convergence: Candidate UI already uses
  the durable authority; legacy monitor mismatch does not control business.
- Legacy daemon recovery refactor and emergency writer reintegration: public
  start/resume is retired and internal Formal is blocked, so it is not an
  active new-book P0 path.
- Full atomic publication of every auxiliary narrative asset: generation is
  already blocked until commit/memory readiness; changing all repositories is
  high risk and requires a separate fault-injection design.
- Physical deletion of V1/V2/queue/checkpoint/legacy aliases: runtime call proof
  is incomplete.
