# Task 3A Implementation Report

## Outcome

Implemented canonical chapter narrative extraction and its SQLite durability foundation in commit
`c005d5d7` (`feat: make chapter narrative sync canonical`). The implementation keeps
`chapter-narrative-sync` as the canonical extraction contract, adds content versioning and atomic
CAS claims, returns a typed mapping-compatible `AftermathCommitResult`, commits only validated and
confirmed canonical writes, and indexes vectors after commit with source provenance.

No Task 3B retry, pause/gating, stale-claim recovery, or chapter-advance behavior was added.

## Canonical Contract and Compatibility

- Fixed the tracked CPMS package source to
  `infrastructure/ai/prompt_packages/nodes/chapter-narrative-sync/`.
- Added required integer `chapter_number` to the manifest and runtime binding.
- Fixed `user.md` so it renders `第 {chapter_number} 章` and inserts `{content}` once.
- Added `AftermathCommitResult`, a `Mapping[str, Any]` with `.get()`, item lookup, iteration, and
  legacy flags (`vector_stored`, `narrative_sync_ok`, etc.). Existing dictionary consumers remain
  compatible while new consumers receive hash/version/status/error/attempt/vector provenance.
- `ChapterAftermathPipeline` now preserves the canonical success/failure result instead of marking
  every non-throwing return successful.
- Audit review-only aftermath payload remains namespaced under `chapter_aftermath_review` and can no
  longer replace the canonical `chapter_summary` field.

## SQLite Migration and Legacy Backfill

Clean-install `schema.sql` and runtime migration support now include:

- `chapters.content_sha256`, `chapters.content_revision`;
- `chapter_summaries.source_content_sha256`, `pipeline_version`, `sync_status`, `sync_error`,
  `sync_attempts`;
- `chapter_narrative_commits`, keyed by
  `(novel_id, chapter_number, content_sha256, pipeline_version)` with revision, status, failure,
  attempts, vector status, and timestamps;
- migration file `014_chapter_narrative_commits.sql` for the existing migration runner.

On database initialization, stored chapter text is hashed mechanically with SHA-256. Existing rows
receive revision 1 without any LLM call. Existing summaries lacking provenance receive the matching
chapter hash where available, `pipeline_version='legacy'`, `sync_status='legacy'`, zero attempts, and
no canonical claim rows. This deliberately avoids a whole-book recomputation.

`SqliteChapterRepository.save()` computes the hash on every normal save, preserves the revision for
identical content, and atomically increments it when content changes. Direct legacy SQL writes with
missing/stale provenance are normalized mechanically when the canonical claim reads the source row.
Summary provenance now round-trips through the existing whole-knowledge save/load path, preventing a
later chapter update from erasing an earlier committed summary's source metadata.

## CAS and Commit Semantics

- Claim acquisition and final transitions use the existing SQLite connection and writer bypass for
  short, synchronous transactions; no transaction spans an LLM or vector await.
- Only the successful `INSERT OR IGNORE` claimant invokes the LLM.
- Existing `committed` versions return `reused`; existing `in_progress` versions return immediately;
  failed versions remain failed until Task 3B defines retry policy.
- Missing chapters and source-hash mismatches fail before LLM invocation.
- Provider errors, empty summaries, malformed structured list fields, missing summary writes, and
  critical structured repository failures mark both claim and prepared summary failed.
- The claim becomes `committed` only after a non-empty validated bundle, visible summary/provenance
  row, unchanged source hash, and critical canonical persistence.
- Vector indexing starts after that commit and carries content SHA-256, content revision, and pipeline
  version. Vector failure records `vector_status='failed'` but does not downgrade canonical commit.

## Red to Green Evidence

Each behavior was first observed failing before its production change:

- Prompt package test failed on stale `prompts_defaults.json` source; then passed after manifest and
  template correction.
- Runtime prompt-binding test failed because `chapter_number` was absent; then passed after binding.
- Clean/legacy migration tests failed on missing content/provenance fields; then passed after schema,
  runtime migration, and mechanical backfill.
- Chapter repository test observed empty hash/revision 0; then passed with stable/incrementing versions.
- Canonical reuse/change test observed no `commit_status` and unconditional extraction; then passed
  with one claim per version and two extractions for two hashes.
- Provider/empty/malformed extraction tests observed `committed`; then passed with durable failures.
- Vector provenance test observed an empty provenance payload; then passed with hash/revision/version.
- Summary writer exception escaped; then passed as a persisted failed result.
- Structured persistence exception was logged and committed; then passed as a failed claim/summary.
- Concrete triple writer failure was swallowed internally; then passed using canonical strict mode.
- Aftermath consumer test observed failed canonical sync overwritten to success; then passed after
  mapping propagation.
- Two-chapter test observed earlier provenance reset; then passed after repository/entity round-trip.
- Audit review isolation test observed the merge contract missing; then passed with canonical fields
  protected.

## Verification

Python runner: `W:\novel\PlotPilot\.venv\Scripts\python.exe` (plain `pytest` was not on PATH).

Final targeted and adjacent suite:

```text
python -m pytest -q \
  tests/unit/application/world/test_chapter_narrative_sync_idempotency.py \
  tests/unit/application/engine/test_chapter_continuity.py \
  tests/unit/application/services/test_chapter_indexing_service.py \
  tests/unit/infrastructure/ai/test_chapter_narrative_sync_prompt.py \
  tests/unit/infrastructure/ai/test_prompt_package_sync.py \
  tests/unit/infrastructure/ai/test_prompt_seed_loader.py \
  tests/unit/infrastructure/ai/test_required_cpms_prompts.py \
  tests/unit/infrastructure/persistence/database/test_chapter_narrative_commit_migration.py \
  tests/unit/infrastructure/persistence/database/test_migration_runner.py \
  tests/unit/infrastructure/persistence/database/test_sqlite_knowledge_orphan_triples.py

53 passed, 32 warnings in 5.50s
```

Warnings are existing `datetime.utcnow()` deprecations. Changed Python modules also passed
`python -m compileall -q`. `git diff --check` exited 0; Git emitted only CRLF conversion notices.

## Commits

- `c005d5d7` - `feat: make chapter narrative sync canonical`
- Report finalization is committed separately after this file records the implementation hash.

## Deferred to Task 3B / Concerns

- No automatic retry, backoff, pause, gate, stale `in_progress` reclamation, or failed-claim reopening.
- No next-chapter advancement changes and no autopilot stop/pause behavior changes.
- Vector retry policy remains undefined; only provenance and independent status are persisted here.
- No old-chapter replay or summary/vector recall strategy was introduced.
- Verification was the requested targeted plus adjacent suite, not the entire repository test suite.

## Review Fix Round 1

Independent review found five Important issues in the original implementation: stale source content
could commit, a replaced summary could commit, critical writes treated enqueue/`False` as success,
the standalone migration wrapper skipped the provenance upgrade, and vector-status persistence errors
escaped after canonical commit. Commit `e8879eff` addresses those findings.

New tests were observed red before each implementation change:

- an out-of-band `chapters.content` update during the LLM await was incorrectly committed;
- a prepared summary replaced with an empty/different-provenance row was incorrectly committed;
- vector-status persistence failure escaped after an otherwise committed canonical result;
- a failed tension write still committed;
- a queue that merely accepted a summary/event batch was treated as enough even when no row was visible;
- the actual `_apply_migration_files()` wrapper left a legacy database without provenance columns;
- the AI prose projection changed content but left its source hash/revision untouched.

The fix recomputes and validates source content hash plus revision in the final SQLite transaction,
requires the exact non-empty prepared summary row to remain in-progress, performs canonical summary and
dialogue writes through the existing direct SQLite bypass before committing, propagates critical extras
failure, applies canonical provenance backfill from the standalone migration wrapper, isolates vector
status write failure, and versions the AI prose projection update.

Verification after the fix:

```text
python -m pytest -q \
  tests/unit/application/world/test_chapter_narrative_sync_idempotency.py \
  tests/unit/application/engine/test_chapter_continuity.py \
  tests/unit/application/services/test_chapter_indexing_service.py \
  tests/unit/application/ai_invocation/test_chapter_prose_generation_contract.py \
  tests/unit/infrastructure/ai/test_chapter_narrative_sync_prompt.py \
  tests/unit/infrastructure/ai/test_prompt_package_sync.py \
  tests/unit/infrastructure/ai/test_prompt_seed_loader.py \
  tests/unit/infrastructure/ai/test_required_cpms_prompts.py \
  tests/unit/infrastructure/persistence/database/test_chapter_narrative_commit_migration.py \
  tests/unit/infrastructure/persistence/database/test_migration_runner.py \
  tests/unit/infrastructure/persistence/database/test_sqlite_knowledge_orphan_triples.py

77 passed, 48 existing datetime.utcnow() deprecation warnings
```

`python -m compileall -q` passed for the changed Python modules and `git diff --check` passed.
