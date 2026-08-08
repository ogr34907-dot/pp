# Task 1 Report: Durable Canonical Status Reconciliation

## Implementation

- Added `canonical_aftermath_recovery.py`, a read-only durable resolver for the latest completed chapter.
- The resolver reuses `SqliteChapterNarrativeCommitRepository.is_current_version_ready(..., require_memory_sync=True)` for the ready decision.
- When SQLite is readable, it replaces shared canonical fields with the current terminal narrative or exhausted memory-sync failure, or clears stale canonical fields when the latest version is ready or has no terminal failure.
- When SQLite cannot be read, it leaves the shared-state fields unchanged.
- `QueryService.get_novel_status_dict()` now reconciles canonical fields before review-gate augmentation. This is required because the live `/autopilot/{novel_id}/status` route calls `QueryService`, not the route-local compatibility builders.

## Files Changed

- `application/engine/services/canonical_aftermath_recovery.py` (new)
- `application/engine/services/query_service.py`
- `tests/unit/interfaces/test_autopilot_canonical_status.py` (new)
- `tests/unit/application/engine/test_query_service_autopilot_invocation.py`

## RED

Command:

```powershell
pytest tests/unit/interfaces/test_autopilot_canonical_status.py -q
```

Output before implementation: `2 failed`. The terminal durable failure test received stale shared chapter `8` rather than durable chapter `63`; the ready test retained stale canonical fields. These failures were expected because `QueryService` only merged shared runtime fields.

Reviewer regression RED command:

```powershell
pytest tests/unit/interfaces/test_autopilot_canonical_status.py::test_status_surfaces_exhausted_durable_memory_sync_failure -q
```

Output before the memory-status extension: `1 failed`, with `KeyError: 'canonical_aftermath_chapter_number'`. This was expected because the initial resolver only recognized a terminal narrative commit failure, not a committed narrative row with an exhausted failed memory barrier.

## GREEN

Focused command:

```powershell
pytest tests/unit/interfaces/test_autopilot_canonical_status.py -q
```

Output: `3 passed in 0.77s`.

Broader relevant command:

```powershell
pytest tests/unit/interfaces/test_autopilot_canonical_status.py tests/unit/application/engine/test_query_service_autopilot_invocation.py tests/unit/application/engine/test_state_bootstrap_runtime_state.py tests/unit/interfaces/test_autopilot_resume_persist.py tests/unit/application/engine/test_chapter_aftermath_history_gate.py -q
```

Output: `27 passed in 3.12s`.

## Self-Review

- Verified that the live status path uses `QueryService` and kept route-local compatibility helpers untouched.
- Confirmed ready state uses the repository predicate with `require_memory_sync=True`; no duplicate readiness rule was added.
- Terminal detection is capped at three narrative or memory-sync attempts.
- The resolver is read-only and does not alter canonical commit, story-advance CAS, retry-cycle, or manuscript-prose behavior.
- Verified durable reads clear stale canonical fields, while durable read errors retain shared fields; the existing shared-state test now explicitly models that fallback.
- Independent review found the exhausted memory-sync case; it was added as a regression test and fixed before final verification.
- `git diff --check` completed without whitespace errors.

## Concerns

- Status now performs the required synchronous durable read on the live status path. If the SQLite store is unavailable, it deliberately preserves existing shared status rather than fabricating a replacement.
