# Task 4 Report: Governance Audit, Override, Replan Preview, and UI Status

## Implementation

- Extended `NarrativeGovernanceService` with hierarchy alignment status/preview,
  exact-digest one-shot override, and no-write replan preview methods. Reports
  are normalized with decision, violations, repair plan, evidence refs, and
  snapshot/candidate digests.
- Persisted alignment evaluations, override decisions, and replan previews as
  `governance_events` through `SqliteGovernanceRepository`; added event listing
  so a route-created service can validate a persisted digest and reject a
  previously consumed override.
- Added governance API routes (including compatibility aliases) for hierarchy
  status/preview, override, and safe replan preview. Invalid or stale digests
  and empty reasons return HTTP 400 before any StoryNode/contract write.
- Added frontend governance DTOs/API helpers and a compact hierarchy status and
  repair preview in `NarrativeGovernanceCockpit`, with explicit loading, error,
  empty, block/review/degraded, and overridden states.

## TDD Evidence

RED:

`pytest -q tests/unit/application/governance/test_hierarchy_governance_audit.py`

Result: 2 failed at the expected missing `NarrativeGovernanceService` seam.

GREEN:

`pytest -q tests/unit/application/governance`

Result: `6 passed`.

`pytest -q tests/unit/application/governance tests/unit/application/engine/services/test_hierarchical_narrative_alignment_gate.py tests/unit/application/blueprint/services/test_chapter_preplanning_hierarchy_gate.py tests/unit/application/workflows/test_hierarchy_gate_prose_entry.py`

Result: `25 passed`.

`npm run build` (from `frontend`)

Result: Vue typecheck and Vite production build passed.

`python -m compileall -q application/governance/service.py infrastructure/persistence/database/sqlite_governance_repository.py interfaces/api/v1/engine/governance_routes.py` and `git diff --check` passed.

## Concerns

- The alignment report cache is process-local for immediate override UX, while
  persisted evaluation events provide the route-reconstruction fallback and
  single-use audit check. A multi-process deployment should keep all API
  workers on the same governance database (the existing repository contract).
- Replan preview intentionally emits a governance event but has
  `would_write: false` and an empty mutation list; it never updates StoryNodes,
  chapter content, or the narrative contract.
