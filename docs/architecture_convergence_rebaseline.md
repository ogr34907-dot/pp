# Architecture Convergence Rebaseline

## Old Baseline

- Old baseline: `c60237bd73eb63805272367e793146a9a3ba2be9`
- Branch: `codex/plotpilot-memory-stability`
- Assessment date: 2026-08-15

## Starting HEAD

- Starting HEAD: `36fd3c65fbaa5a1916d62c6cd089c0125064aee6`
- Commit: `36fd3c65 fix: complete candidate-first writing stability flow`
- `c60237b` is an ancestor of Starting HEAD.
- This is the only commit in `c60237b..36fd3c65`.

## Dirty Worktree Reality

The test workspace was clean at task start. `git status --short` and the
HEAD-relative diff both had no output. The previous 114-file repair set is
already contained in `36fd3c65`; it is not an uncommitted patch and must not be
recreated.

Fresh behavior baseline at this exact HEAD:

```text
pytest: 2597 passed, 12 skipped, 6 deselected
frontend lint: passed
frontend unit: 25 files / 67 tests passed
frontend build: passed
git diff --check: passed
```

No real provider was called. No Prompt, model, temperature, token budget,
outline template, or database schema is changed by this convergence pass.

## Changes Since Old Baseline

### `36fd3c65 fix: complete candidate-first writing stability flow`

Purpose: finish the Candidate-first writing flow and close the concrete
memory/version/DAG/planning failures found in the previous audit.

Affected architecture:

- Candidate formal history now records `content_sha256`, `content_revision`,
  provenance, and Canonical sync state.
- Generation start now checks persisted target chapters, published outline,
  formal-history provenance, worldline/rebuild barriers, and chapter-slot
  conflicts before creating a run.
- Root outline generation receives the existing Bible projection.
- Five-level outline drafts have published-parent and sibling handoff gates;
  chapter rhythm is validated before save.
- Candidate generation runs the protected default DAG V2 and persists DAG
  attempts/events for reconnectable progress.
- Mutable DAG run/toggle/edit controls are retired with `410`; the candidate
  console is read-only.
- Public legacy prose/autopilot entry points are retired with `410`; internal
  legacy completed writes are blocked.
- Rewrite/worldline invalidation, current-version memory visibility, bounded
  completed beats, and target-chapter authority were strengthened.

The commit changes 114 files (`+6581/-1664`). The present task keeps these
working improvements and only addresses independently proven gaps.

## Current Creative Lifecycle

```text
Novel + Bible
  -> synced outline -> part -> volume -> act -> chapter contract
  -> GenerationStartPreflight
  -> CandidateChapterWorkflowService.generate_next
  -> protected DAG V2 + ContextBuilder
  -> Candidate audit
  -> author review OR continuous safety decision
  -> ChapterCandidateRepository.commit_formal
  -> ChapterAftermathPipeline
  -> ChapterNarrativeSync + current-version MemoryEngine
  -> ChapterCandidateRepository.mark_sync_succeeded
  -> next Candidate
```

Manual mode stops at `awaiting_review`. Editing invalidates the old audit and
commit plan, re-audit generates no prose, and approval is the only public path
to formal commit. Continuous mode still creates a Candidate and stops unless
that Candidate reaches `committed`. A failed Canonical/Memory stage therefore
prevents the N+1 LLM call.

## Current Architecture Authorities

| Business fact | Current authority | Decision |
|---|---|---|
| Planning | `OutlineContractRepository` + `OutlineContractService` active synced five-level chain | Keep. Remove raw `story_nodes` planning text from prompt authority. |
| Candidate | `CandidateChapterWorkflowService` + `ChapterCandidateRepository` | Keep and harden post-formal state guards. |
| Formal write | `ChapterCandidateRepository.commit_formal()` | Keep as the only new-book generated-prose writer. |
| Canonical commit | `ChapterNarrativeSync` + `SqliteChapterNarrativeCommitRepository` | Keep; do not redesign the transaction kernel. |
| Canonical visibility | exact current chapter hash/revision joins in Memory/Context/Knowledge readers | Keep strict generation readers; record non-generation legacy view debt. |
| Memory | `MemoryEngine.update_canonical_version_from_chapter()` behind narrative `memory_status` | Keep. |
| Recovery | Candidate run/restart state plus exact-version Canonical claim repository | Close the abandoned in-progress claim handoff; do not create another recovery service. |
| Runtime state | `novel_generation_runs` + Candidate repository for the new workflow | Legacy daemon state is compatibility-only and cannot write formal prose. |
| Observability | durable Candidate DAG run/attempt/event tables | Shared-state DAG projection is legacy display fallback only. |
| Worldline | `WorldlineRegenerationService` + `WorldlineRebuildService` + generation epoch guard | Reuse Candidate formal head instead of raw chapter `MAX(number)`. |

## P0 Reality Check

| Contract | Current result | Evidence / remaining action |
|---|---|---|
| P0-1 one five-level planning authority | PARTIAL | Published chain is authoritative, but `ContextAssembler.build_story_anchor()` still injects raw root/act `story_nodes` text at T0. Remove that prompt-visible bypass. |
| P0-2 Bible reaches LLM | PASS | API composition root injects Bible/Context services; root outline has `_root_bible_context`; Candidate generation uses `build_structured_context()`. |
| P0-3 both modes are Candidate-first | PASS | Public direct prose and legacy autopilot starts are `410`; both modes use `CandidateChapterWorkflowService`. |
| P0-4 Candidate isolation | PASS | Candidate rows and version history are separate until `commit_formal()`. |
| P0-5 exact formal identity | PASS | Formal authority carries novel/chapter/hash/revision/candidate provenance. |
| P0-6 old revision invisible | PASS for generation path | Rewrite invalidates downstream assets and Context/Memory readers require current hash/revision. |
| P0-7 Aftermath barrier | PARTIAL | Barrier exists, but final Candidate ready publication lacks an in-transaction exact-version CAS. |
| P0-8 Memory barrier | PASS | `narrative_sync_ok` remains false until required Memory status is committed. |
| P0-9 advance exactly once | PASS with recovery gap | Candidate cursor advances after sync; restart can strand an `in_progress` narrative claim. Close that handoff. |
| P0-10 Worldline isolation | PARTIAL | Rebuild is version guarded, but preview/execute tail classification uses raw `MAX(chapters.number)`. |
| P0-11 Context integrity | PARTIAL | Required slots are present; raw StoryNode plan text is an unpublished-plan leak and must be removed. |

## Old Ticket Reassessment

| Old item | Status | Current evidence | Decision / action |
|---|---|---|---|
| Phase 0 behavior characterization | ALREADY_DONE | Full baseline is green; Candidate/manual/continuous/rewrite/worldline/current-version tests exist. | Add only tests that reproduce newly proven gaps. |
| Phase 1 shared content guard | NEW_IMPLEMENTATION_BETTER | `domain/shared/final_text_evidence.py`, repository CAS checks, Candidate formal provenance, and rewrite coordinator cover the actual writers. | Preserve; no new generic guard module. |
| Phase 2 Canonical visibility | PARTIALLY_DONE | Generation Context and triples use current committed hash/revision; some general Knowledge summaries remain a legacy/raw view. | Do not broaden this pass unless a generation-path leakage test fails. Document remaining view debt. |
| Phase 3 split writing progress | OBSOLETE | New-book generation does not use legacy WritingDelegate; public legacy start is `410` and internal completed write is blocked. | Do not refactor dead compatibility code for file shape. |
| Phase 4 thin `novel_lifecycle` router | REVISE | Recovery decisions remain duplicated/fail-open in legacy daemon code, but that write path is currently inaccessible before LLM/formal write. | Keep as explicit legacy debt; do not risk the Candidate path in this pass. |
| Phase 5 Context DI | NEW_IMPLEMENTATION_BETTER | API composition root supplies ContextBuilder/Assembler/Allocator/Memory; internal constructors are compatibility fallbacks. | Preserve construction; fix only raw StoryNode planning input. |
| Phase 6 Candidate workflow cleanup | PARTIALLY_DONE | Shared commit/sync path exists; Formal-sync failure can still be edited/regenerated and final ready lacks exact-version CAS. | Add behavior tests and minimal repository guards. |
| Phase 7 aftermath helper extraction | OBSOLETE | Current order and barrier tests are stronger than a cosmetic split. | No refactor without a behavioral defect. |
| Phase 8 split narrative sync | OBSOLETE | File length alone is not a defect; moving 3000 lines would raise transaction risk. | Keep facade intact. |
| Phase 9 transaction hardening | PARTIALLY_DONE | Claim/memory/advance idempotency and stale-version tests exist; final Candidate publication CAS is missing. | Add the missing CAS test/fix only. |
| Phase 10 recovery/worldline convergence | PARTIALLY_DONE | Rebuild reuses exact-version aftermath; Candidate restart does not retire an abandoned narrative claim and worldline tail uses raw MAX. | Fix these two concrete gaps. |
| Phase 11 dead code/docs | KEEP | `docs/ARCHITECTURE.md` still presents legacy daemon flow as primary; `docs/CHAPTER_LIFECYCLE.md` is absent. | Update docs after code gates are green; no speculative deletion. |
| Five-level literary outline | ALREADY_DONE | `outline -> part -> volume -> act -> chapter`, parent digest/sync gates, sibling handoff, Bible root context, and rhythm validation are present. | Preserve. |
| Independent narrative skeleton removal | PARTIALLY_DONE | Candidate prose uses only OutlineContract, but legacy planning still writes physical `story_nodes` fields that ContextAssembler reads. | Remove prompt visibility now; retain compatibility storage until call-proof supports deletion/migration. |
| Candidate DAG V2 execution | PARTIALLY_DONE | Candidate uses durable DAG V2; one inserted `plan_outline` node makes a redundant planning call whose output BeatNode ignores. | Disable that node in Candidate mode with a behavior test, without changing prompts. |
| Mutable DAG controls | ALREADY_DONE | API mutations return `410`; frontend editor/control stores were removed. | Preserve. |
| LLM provider/config work | NEW_IMPLEMENTATION_BETTER | Already delivered and validated in `36fd3c65`. | Out of scope; no real LLM call. |
| Queue/checkpoint/V1/V2 deletion | BLOCKED | Production and compatibility references remain; no full runtime deletion proof exists. | Keep; do not silently substitute semantics. |

## Direct-Write Classification

| Writer | Classification and guard |
|---|---|
| `ChapterCandidateRepository.commit_formal` | New-book formal authority. Atomic Candidate, formal hash/revision, sync barrier. |
| `ChapterRewriteCoordinator` | Legal existing-formal rewrite. CAS, downstream stale, epoch/rebuild barrier. |
| `autopilot.continuations._write_chapter_draft` | Legacy compatibility writer. Completed transition is unconditionally Candidate-first blocked. |
| `PersistenceQueue.handle_upsert_chapter` | Draft/import compatibility. Canonical/completed overwrite guard rejects Formal changes. |
| `daemon_host._save_*` / `WritingDelegate` / StoryPipeline | Legacy daemon compatibility. Entry pauses on Candidate-first before generation/formal write. |
| `SqliteChapterRepository.save/update` | CRUD/draft/import repository. Existing prose/canonical changes route to rewrite coordinator or are rejected. |
| `ChapterNarrativeSync` chapter updates | Metadata/tension/summary state after formal prose exists; it does not replace prose. |
| `StateSnapshotManager` | Snapshot restore compatibility, not generated-prose authority; remains outside active Candidate run. |
| `SqliteChapterNarrativeCommitRepository` | Sole narrative claim, memory-status, and old StoryPipeline advance transaction authority. Candidate cursor remains owned by Candidate repository. |

## New Architecture Findings

1. Raw root/act StoryNode descriptions are a second prompt-visible planning
   authority and can leak unpublished planning into T0 context.
2. A failed Candidate with `formal_chapter_id` can be edited or regenerated,
   corrupting the retry-sync recovery state.
3. Service restart marks a syncing Candidate retryable but leaves the exact
   narrative claim `in_progress`, so retry can remain stuck.
4. Candidate ready publication lacks a final exact-version CAS against the
   Candidate, formal authority row, and current chapter.
5. Worldline preview/execute classify the tail with raw `MAX(number)`, so an
   empty draft placeholder can turn a safe continue into destructive regenerate.
6. Candidate DAG includes a second planning-decomposition node whose output is
   not consumed by Candidate BeatNode.
7. Candidate plan/rhythm duplication and unbudgeted final prompt material are
   real efficiency concerns, but changing Prompt/context budget contracts is
   explicitly deferred from this convergence pass.
8. Generic DAG shared-state observability and durable Candidate DAG trace can
   disagree after process restart; Candidate UI already uses the durable source.

## Product Constraints Preserved

- `novels.target_chapters` remains the sole book-length authority.
- `completed_beats` remains capped at the most recent 500 details.
- The two author modes remain unchanged.
- No candidate-only content enters Formal, Canonical, Memory, Vector, or N+1.
- Commercial-fiction rhythm fields remain active: `chapter_function`,
  `intensity_curve`, `chapter_goal`, `chapter_delta`, `ending_hook`, and the
  conditional `decisive_choice`, `cost_or_risk`, `turn_or_payoff` fields.
- No real LLM call is required for this task.
