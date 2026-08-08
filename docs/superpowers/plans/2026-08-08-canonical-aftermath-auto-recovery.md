# Canonical Aftermath Auto-Recovery Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Correct stale canonical failure status and add one bounded automatic recovery cycle for transient terminal LLM failures.

**Architecture:** Add a small application service that reads the latest completed prose version, reconciles its durable canonical state, and executes the existing CAS recovery path. API status, manual retry, and daemon recovery consume that service instead of independently interpreting shared state.

**Tech Stack:** Python 3.14, FastAPI, SQLite, pytest, existing StoryPipeline and ChapterAftermathPipeline.

## Global Constraints

- Work and tests run only in `W:\novel\test` until the branch is pushed.
- Do not modify prose during canonical recovery.
- Keep each canonical extraction cycle capped at three attempts.
- Permit at most one automatic extra cycle per chapter content hash and revision.
- Do not bypass canonical commit or story-advance CAS checks.

---

### Task 1: Durable Canonical Status Reconciliation

**Files:**
- Create: `application/engine/services/canonical_aftermath_recovery.py`
- Modify: `interfaces/api/v1/engine/autopilot_routes.py`
- Test: `tests/unit/interfaces/test_autopilot_canonical_status.py`

- [ ] Write a failing test proving durable chapter 63 failure overrides stale shared chapter 8 fields.
- [ ] Write a failing test proving a ready latest commit clears stale canonical fields.
- [ ] Run both tests and confirm the expected failures.
- [ ] Implement the durable state resolver and final status reconciliation.
- [ ] Run both tests and confirm they pass.

### Task 2: Correct Runtime Failure Publication

**Files:**
- Modify: `engine/runtime/writing_delegate.py`
- Test: `tests/unit/engine/test_story_pipeline_canonical_state.py`

- [ ] Write a failing test proving a canonical pause publishes chapter number and persisted failure reason.
- [ ] Write a failing test proving successful recovery clears stale canonical fields.
- [ ] Run both tests and confirm the expected failures.
- [ ] Implement the minimal shared-state publication changes.
- [ ] Run both tests and confirm they pass.

### Task 3: Bounded Automatic Recovery

**Files:**
- Modify: `application/engine/services/canonical_aftermath_recovery.py`
- Modify: `engine/runtime/novel_lifecycle.py`
- Test: `tests/unit/engine/test_canonical_aftermath_auto_recovery.py`

- [ ] Write failing tests for one transient recovery, hard-failure blocking, and exhausted-marker blocking.
- [ ] Run the tests and confirm the expected failures.
- [ ] Implement the exact-version recovery marker and one-cycle recovery path.
- [ ] Run the tests and confirm they pass.

### Task 4: Manual Route Reuse And Regression Verification

**Files:**
- Modify: `interfaces/api/v1/engine/autopilot_routes.py`
- Modify: `tests/unit/interfaces/test_autopilot_resume_persist.py`

- [ ] Update route tests to exercise the shared service and preserve pause behavior.
- [ ] Run all canonical aftermath, lifecycle, status, and resume tests.
- [ ] Run `git diff --check` and review the complete diff.
- [ ] Commit and push the new branch.
- [ ] Fast-forward the formal workspace to the pushed branch, restart services, and verify health plus durable chapter status.
