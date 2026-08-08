# Canonical Aftermath Full Resync Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add an explicit, resumable, ordered full-book canonical aftermath resync that repairs all completed chapters without changing prose or allowing autopilot to cross an unfinished MemoryEngine gate.

**Architecture:** Reuse `ChapterAftermathPipeline`, `SqliteChapterNarrativeCommitRepository`, the existing shared autopilot state, and the existing SSE conventions. A new application orchestrator scans completed chapters in order, skips chapters whose canonical summary, required MemoryEngine state, and vector state are already usable, and invokes the existing chapter-aftermath pipeline only for missing or stale work. The API streams progress and keeps the novel paused; a rerun resumes from the first chapter that is not fully synchronized.

**Tech Stack:** Python 3.14, FastAPI, SQLite, existing StoryPipeline/ChapterAftermathPipeline, Vue 3, TypeScript, Vitest, existing SSE and local embedding service.

## Global Constraints

- Work and tests run only in `W:\novel\test` until the branch is pushed.
- Never modify `chapters.content`, drafts, or prose branches.
- Process only `chapters.status = 'completed'` rows with non-empty content, ordered by `number ASC`.
- Stop at the first canonical or MemoryEngine failure; do not process later chapters.
- Require exact source `content_sha256`, `content_revision`, and `chapter-narrative-sync:v1` CAS before every derived write.
- A fully ready canonical chapter is not sent through the LLM; vector-only retry remains non-blocking for canonical readiness.
- Start the operation paused, keep it paused after success or failure, and never resume prose generation from the resync endpoint.
- Do not introduce a new database, queue, vector store, or memory system; use existing SQLite and SSE infrastructure.
- Python commands must run through `W:\novel\test\.venv\Scripts\python.exe` (Python 3.14.6).

---

### Task 1: Ordered Full-Resync Application Service

**Files:**
- Create: `application/engine/services/canonical_aftermath_full_resync.py`
- Modify: `infrastructure/persistence/database/sqlite_chapter_narrative_commit_repository.py` only for a narrowly scoped full-resync claim/lease helper
- Test: `tests/unit/engine/test_canonical_aftermath_full_resync.py`

**Interfaces:**
- Consumes: `SqliteChapterNarrativeCommitRepository`, `ChapterAftermathPipeline.run_after_chapter_saved()`, `is_current_version_ready(..., require_memory_sync=True)`, `reclaim_terminal_failure()`, `reclaim_terminal_memory_failure()`, and the existing novel runtime state fields.
- Produces: `FullResyncResult` and `async def resync_all_completed_chapters(*, novel_id: str, database: Any, aftermath_pipeline: Any, emit: Callable[[dict[str, Any]], Awaitable[None]] | None = None) -> FullResyncResult`.
- Event fields: `started`, `chapter`, `vector`, `failed`, `completed`; chapter events carry `chapter_number`, `action`, `processed`, `synced`, `skipped`, and `total`.

- [ ] **Step 1: Add failing service tests.** Cover three completed chapters where chapter 1 is fully ready and is skipped, chapter 2 has no commit and is synchronized with exact hash/revision, and chapter 3 fails. Assert chapter order, one LLM pipeline call for chapter 2, no chapter 3 call after its failure, failure chapter/reason, and `remains_paused=True`.
- [ ] **Step 2: Add failing tests for resume and CAS.** Run the resync twice and assert the second run skips chapter 1 and the already committed chapter 2. Mutate chapter 2 content/revision during the fake pipeline and assert `source_version_mismatch`, no stale write, and no later chapter processing. Add a concurrent same-novel claim test asserting the second run returns `conflict`.
- [ ] **Step 3: Run the focused tests and verify the expected RED failures.**

Run:

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\unit\engine\test_canonical_aftermath_full_resync.py
```

Expected: collection or assertion failures because the new orchestrator and claim contract do not yet exist.

- [ ] **Step 4: Implement the minimal ordered orchestrator.** Read all eligible completed chapters once, calculate/verify each source hash, claim a per-novel full-resync marker with a CAS/lease, and emit `started`. For each chapter, first check canonical plus MemoryEngine readiness and vector status; skip only when all required states are usable. For an unready chapter, reclaim its exact terminal canonical or MemoryEngine state when applicable, call `run_after_chapter_saved()` with expected hash/revision, verify readiness again, emit the chapter result, and stop on the first hard failure. Update the existing shared paused state after every chapter and preserve the marker on failure/cancellation.
- [ ] **Step 5: Run the focused tests and verify GREEN.**

Run the same command and expect all service tests to pass.

- [ ] **Step 6: Run existing canonical history/idempotency tests.**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\unit\application\engine\test_chapter_aftermath_history_gate.py tests\unit\application\world\test_chapter_narrative_sync_idempotency.py tests\unit\engine\test_canonical_aftermath_auto_recovery.py
```

### Task 2: FastAPI SSE Endpoint And Durable Pause State

**Files:**
- Modify: `interfaces/api/v1/engine/autopilot_routes.py`
- Test: `tests/unit/interfaces/test_autopilot_full_resync.py`

**Interfaces:**
- Consumes: Task 1 `resync_all_completed_chapters()` and existing `get_database()`, `get_chapter_aftermath_pipeline()`, `_request_manual_stop()`/shared state helpers, and `_SSE_THREAD_POOL` conventions.
- Produces: `POST /api/v1/autopilot/{novel_id}/canonical-aftermath/resync-all` as `text/event-stream`.
- HTTP contract: `404` for missing novel, `409` for an active same-novel resync, `503` for unavailable database/pipeline, and SSE terminal `failed`/`completed` events for work outcomes.

- [ ] **Step 1: Add failing route tests.** Assert the endpoint returns `text/event-stream`, emits `started` then ordered `chapter` and terminal events, pauses before work, never calls resume, and maps an active marker to HTTP 409.
- [ ] **Step 2: Run route tests and verify RED.**
- [ ] **Step 3: Implement the streaming generator.** Pause the novel before creating the task, pass an async emitter into Task 1, serialize each event as `data: <json>\n\n`, handle `asyncio.CancelledError` by preserving the paused marker, and close the marker only after a completed scan. Do not block the event loop with synchronous SQLite calls; use the existing thread pool only for short state operations.
- [ ] **Step 4: Run route tests and the existing resume/status tests.**

```powershell
.\.venv\Scripts\python.exe -m pytest -q tests\unit\interfaces\test_autopilot_full_resync.py tests\unit\interfaces\test_autopilot_canonical_status.py tests\unit\interfaces\test_autopilot_resume_persist.py
```

### Task 3: Frontend Full-Resync Control And Progress

**Files:**
- Modify: `frontend/src/api/endpoints.ts`
- Modify: `frontend/src/api/autopilot.ts`
- Modify: `frontend/src/components/autopilot/AutopilotPanel.vue`
- Modify: `frontend/src/components/autopilot/canonicalAftermathGate.ts` only for the new action label/state mapping
- Test: `frontend/src/api/autopilotFullResync.spec.ts`
- Test: `frontend/src/components/autopilot/canonicalAftermathFullResync.spec.ts`

**Interfaces:**
- Consumes: Task 2 SSE endpoint and existing `fetchUrl`, `HttpError`, `getCanonicalAftermathPresentation()`, and panel status refresh.
- Produces: `autopilotApi.consumeCanonicalAftermathFullResync(novelId, handlers)` with typed `started/chapter/vector/failed/completed` events.

- [ ] **Step 1: Add failing Vitest tests.** Assert the endpoint path, POST method, SSE event parsing, progress counters, failure chapter display, and that a completed event triggers status refresh without auto-resume.
- [ ] **Step 2: Run the focused Vitest tests and verify RED.**

```powershell
Set-Location frontend
npm run test:unit -- src/api/autopilotFullResync.spec.ts src/components/autopilot/canonicalAftermathFullResync.spec.ts
```

- [ ] **Step 3: Add the API route and stream parser.** Keep the parser compatible with the existing SSE framing and AbortController conventions; surface HTTP errors through the existing `HttpError` helpers.
- [ ] **Step 4: Add the panel control.** Show an icon-plus-text “全章重同步” action only for the canonical gate while stopped/paused, render `processed / total` and current chapter, disable competing resume/start actions while active, and show the first failure reason. Use existing Naive UI components and no new visual style system.
- [ ] **Step 5: Run focused frontend tests and the existing canonical aftermath Vitest suite.**

```powershell
npm run test:unit -- src/api/autopilotFullResync.spec.ts src/components/autopilot/canonicalAftermathFullResync.spec.ts src/api/autopilotCanonicalAftermath.spec.ts src/components/autopilot/canonicalAftermathGate.spec.ts
```

### Task 4: Full Verification, Commit, Push, And Deployment

**Files:**
- No new production files in this task; update the SDD progress ledger at `.superpowers/sdd/2026-08-08-canonical-aftermath-auto-recovery/progress.md` with final evidence.

- [ ] **Step 1: Run the complete backend suite and required regressions.**

```powershell
.\.venv\Scripts\python.exe -m pytest tests -q
.\.venv\Scripts\python.exe -m pytest tests\unit\engine\test_memory_stability_observability.py::test_thirty_chapter_regression_recovers_injected_vector_failure -q
.\.venv\Scripts\python.exe -m pytest -m slow tests\unit\engine\test_memory_stability_observability.py::test_hundred_chapter_memory_stability_regression -q
```

- [ ] **Step 2: Run frontend checks.**

```powershell
Set-Location frontend
npm run check:shared-config
npm run test:unit
npm run build
```

- [ ] **Step 3: Run `git diff --check`, inspect the complete diff/stat, and verify no runtime data, `.models`, `.venv`, `node_modules`, or `requirements-local.txt` changes are staged.
- [ ] **Step 4: Commit the implementation on `codex/canonical-aftermath-auto-recovery` and push it to `origin` without force-pushing.
- [ ] **Step 5: Stop formal services, fetch and fast-forward `W:\novel\PlotPilot` to the pushed branch while preserving `data\plotpilot.db`, WAL/SHM, `data\chromadb`, `.models\bge-small-zh-v1.5`, `.venv`, `frontend\node_modules`, and `requirements-local.txt`.
- [ ] **Step 6: Restart backend on `127.0.0.1:8005` and frontend on `127.0.0.1:3019`; verify health, OpenAPI, frontend HTTP 200, the full-resync route, and the affected novel’s paused canonical status.
