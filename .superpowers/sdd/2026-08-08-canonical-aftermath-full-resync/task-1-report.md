# Task 1 Report: Ordered Full Resync Service

## Changed Files

- `application/engine/services/canonical_aftermath_full_resync.py`
  - Added `FullResyncResult` and the ordered `resync_all_completed_chapters()` service.
  - Reads completed non-empty chapters in number order, performs exact hash/revision checks, skips fully ready canonical + MemoryEngine + vector versions, reclaims terminal canonical/MemoryEngine failures, invokes the existing aftermath pipeline, verifies readiness after each invocation, emits lifecycle/chapter/vector/failure/completion events, stops on the first hard failure, and keeps the novel paused.
  - Added in-process per-novel exclusion plus a durable SQLite lease marker.
- `infrastructure/persistence/database/sqlite_chapter_narrative_commit_repository.py`
  - Added narrowly scoped `claim_full_resync()`, `renew_full_resync()`, `mark_full_resync_failure()`, and `finish_full_resync()` CAS helpers using the existing `novels.autopilot_recovery_reason` field.
- `tests/unit/engine/test_canonical_aftermath_full_resync.py`
  - Added tests for ordered skip/sync/stop behavior, resume idempotency, concurrent conflict, and source-version mutation protection.

## TDD Evidence

RED command:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\unit\engine\test_canonical_aftermath_full_resync.py
```

RED output (before implementation): 3 collected tests, all failed during import with `ModuleNotFoundError: No module named 'application.engine.services.canonical_aftermath_full_resync'`.

GREEN command:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\unit\engine\test_canonical_aftermath_full_resync.py
```

GREEN output: `3 passed in 0.86s`.

Canonical history/idempotency regression command:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\unit\application\engine\test_chapter_aftermath_history_gate.py tests\unit\application\world\test_chapter_narrative_sync_idempotency.py tests\unit\engine\test_canonical_aftermath_auto_recovery.py
```

Regression output: `48 passed in 49.64s`.

Additional checks: `py_compile` succeeded for the two Python implementation files and `git diff --check` reported no whitespace errors.

## Commit

Implementation commit SHA: `3a0bb5b5666c8533287942a728013a6ebcae8630`.

## Concerns

- The durable marker uses the existing `novels.autopilot_recovery_reason` column, as required; failure markers remain diagnosable and are explicitly reclaimable by a later user-triggered run.
- Cancellation records a `|cancelled` marker and emits a cancellation event; the marker is immediately reclaimable by a later explicit run while preserving pause state. An API/SSE adapter is outside Task 1.

## Fix Round 1

Review-driven regressions were added for cancellation/restart, unavailable dependencies and invalid pipeline results, durable chapter/processed/total marker progress, event ordering and fields, vector-only retry, and cross-`DatabaseConnection` lease CAS.

RED command:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\unit\engine\test_canonical_aftermath_full_resync.py
```

RED output: 3 new tests failed as expected: cancellation propagated `CancelledError`, invalid pipeline output raised `AttributeError`, and synchronous event collection raised `TypeError`.

GREEN command:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\unit\engine\test_canonical_aftermath_full_resync.py
```

GREEN output: `7 passed in 1.47s`.

Regression command and output: the canonical history/idempotency/auto-recovery command above completed with `48 passed in 48.32s`.

Fix commit: `d169a7e92d061a696fa505ca2e10d67f72326388`.

Post-review hardening additionally treats an explicitly missing `_memory_engine` dependency as `unavailable`, marks the result paused immediately after a successful durable claim, and emits a diagnostic failure event for unexpected infrastructure exceptions. Focused verification remains `7 passed in 1.62s`; latest fix commit follows in Git history.
