# Change Log

## Scope

This is a focused repair set against audit baseline
`bf839a1491aa15553309834faa760e967c3c8991`. All code and test changes are in
the test workspace. The formal workspace is not edited.

## Functional Changes

| Area | Main files | Change |
| --- | --- | --- |
| Safe structure persistence | `application/blueprint/services/continuous_planning_service.py`, `chapter_book_structure_sync.py` | Reject destructive plans that would delete authored prose; do not fall back from safe macro confirmation to an unsafe writer; apply shared next-act guards |
| Settings lineage and prompt contract | `application/ai_invocation/variable_backfill.py`, `application/blueprint/services/setup_context_builder.py`, `application/engine/services/context_builder.py`, `application/core/services/novel_service.py`, `engine/runtime/writing_delegate.py` | Read authoritative generation preferences, preserve them through context assembly and final invocation, and clear stale values deterministically |
| SQLite integrity and migrations | `story_node_repository.py`, `sqlite_novel_repository.py`, `migration_runner.py`, `schema.sql`, migrations `021` and `022` | Replace unsafe REPLACE behavior with UPSERT, enforce compatible structure keys, order dependent migrations, and persist autopilot recovery fields |
| Autopilot and SSE | `interfaces/runtime.py`, `autopilot_routes.py`, DAG route/client/store files | Persist restart reason, expose state truthfully, and provide event IDs, replay, deduplication, and calibrated client state |
| Privacy and config correctness | `continuous_planning_service.py`, `llm_control_service.py`, AI settings/provider files | Avoid raw planning-output logging and preserve explicit token configuration semantics |
| Test isolation | `tests/conftest.py`, Mock-LLM acceptance test | Isolate temporary CPMS singleton state and avoid connecting default pytest runs to repository runtime data |

## Test Additions and Extensions

The repair set adds or extends focused tests for safe macro confirmation,
authored-prose preservation, structure natural keys, migration behavior,
recovery-field round trips, restart state, provider prompt settings,
capacity preflight, SSE protocol replay/deduplication, and FastAPI Mock-LLM
acceptance. The added test `tests/integration/test_mock_llm_api_acceptance.py`
uses real project routes/services/repositories/SQLite and mocks only the
external LLM boundary.

## Deliberate Exclusions

No `.venv`, `requirements-local.txt`, local embedding model, `node_modules`,
`frontend/dist`, runtime database, log, secret, user prose, or formal-workspace
file is part of this change set. The preserved local requirements SHA-256 is
recorded in `00-workspace-baseline.md` and `14-testing-security-performance.md`.
