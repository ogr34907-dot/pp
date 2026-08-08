# Task 2 Report: FastAPI Full Resync SSE Endpoint

## Changed Files

- `interfaces/api/v1/engine/autopilot_routes.py`
  - Added `POST /api/v1/autopilot/{novel_id}/canonical-aftermath/resync-all`.
  - Performs novel, durable-lease, and dependency preflight in `_SSE_THREAD_POOL`.
  - Persists the legal `autopilot_status='stopped'` + `current_stage='paused_for_review'` state before starting work.
  - Runs Task 1's async service in a thread-pool worker and bridges ordered events through a thread-safe queue as JSON SSE frames.
  - Preserves the pause/marker on client cancellation and never resumes prose automatically.
  - Maps missing novels to 404, active leases to 409, and unavailable dependencies/database to 503.
- `tests/unit/interfaces/test_autopilot_full_resync.py`
  - Covers ordered SSE events and pause/no-resume behavior, plus 404/409/503 HTTP mappings.

## TDD Evidence

RED command:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\unit\interfaces\test_autopilot_full_resync.py
```

RED output: 2 tests failed as expected with `AttributeError` because the new route was not yet defined.

GREEN command:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\unit\interfaces\test_autopilot_full_resync.py
```

GREEN output: `4 passed in 1.34s`.

Required regression command:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\unit\interfaces\test_autopilot_full_resync.py tests\unit\interfaces\test_autopilot_canonical_status.py tests\unit\interfaces\test_autopilot_resume_persist.py
```

Regression output: `14 passed in 2.39s`.

Additional checks: `py_compile` succeeded and `git diff --check` reported no whitespace errors.

## Commit

Implementation commit SHA: `082f9a05`.

## Concerns

- The worker thread remains responsible for finishing a started service run after an SSE client disconnects; the endpoint does not clear the durable marker or resume the novel on cancellation.
- A race after preflight can still produce a service-level conflict; that outcome is represented as a terminal SSE failure event because HTTP headers have already been sent.

## Fix Round 1

RED command:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\unit\interfaces\test_autopilot_full_resync.py
```

RED output: 2 new tests failed as expected. The route started the worker without a persisted `paused_for_review` stage, and `resume_from_review`/`start_autopilot` did not reject an active full-resync marker.

GREEN command:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\unit\interfaces\test_autopilot_full_resync.py
```

GREEN output: `8 passed in 1.64s` (one existing Starlette/httpx deprecation warning from the TestClient dependency).

Regression output:

```text
20 passed in 2.67s
```

The fix adds atomic pause persistence before the service worker, bounded preflight/pause executor timeouts using runtime DB settings, active-lease guards for start and resume, an ASGI route/content-type test, and a real `DatabaseConnection` persisted-state assertion.

## Fix Round 2

RED command:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\unit\interfaces\test_autopilot_full_resync.py
```

RED output: the direct-write regression raised `LookupError('novel_not_found')` because a collecting transaction was used for the read, and the route race test showed no HTTP 409/no worker guard when the marker changed after preflight.

GREEN command:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\unit\interfaces\test_autopilot_full_resync.py
```

GREEN output: `11 passed in 2.30s`.

Regression output: `23 passed in 3.54s` across full-resync, canonical-status, and resume-persistence tests. `py_compile` and `git diff --check` also pass.

The pause write now reads through `database.fetch_one`, performs a short CAS update under `sqlite_writes_bypass_queue()`, preserves a marker written by another `DatabaseConnection`, and returns HTTP 409 before creating the worker. The route no longer invokes the non-CAS manual-stop DB update for this entrypoint; shared pause state and the stop signal are still published.
