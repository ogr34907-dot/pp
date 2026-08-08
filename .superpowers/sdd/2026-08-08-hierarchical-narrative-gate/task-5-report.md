# Task 5 Report: Final Governance Audit and Verification

## Load-bearing findings resolved

- Three production planning dependency factories (`generation.get_continuous_planning_service`, `blueprint.continuous_planning_routes.get_service`, and `blueprint.story_structure.get_planning_service`) constructed `ContinuousPlanningService` without a hierarchy gate. Each now injects `HierarchicalNarrativeAlignmentGate`, closing the API planning bypass while preserving direct legacy callers.
- `scripts/install/__init__.py` contained a malformed nested docstring and invalid bare text. It was reduced to a valid documentation-only module so the repository-wide compileall check can execute.

## Verification evidence

Commands and results:

```text
python -m pytest -q tests/unit/application/engine/services/test_hierarchical_narrative_alignment_gate.py tests/unit/application/services/test_continuous_planning_service.py tests/unit/application/blueprint/services/test_chapter_preplanning_hierarchy_gate.py tests/unit/application/workflows/test_hierarchy_gate_prose_entry.py tests/unit/application/governance/test_hierarchy_governance_audit.py
60 passed in 2.12s

python -m pytest -q tests/unit
1862 passed, 7 skipped, 1 deselected, 1 warning in 96.81s

python -m pytest -q tests/integration
205 passed, 4 skipped, 1 deselected, 1 warning in 53.21s

python -m pytest -q
2174 passed, 12 skipped, 3 deselected, 1 warning in 159.30s

python -m pytest -q tests/integration/interfaces/api/v1/test_generation_api.py tests/unit/interfaces/api/test_architecture_boundaries.py tests/unit/application/services/test_continuous_planning_service.py
76 passed, 1 warning in 4.73s

npm run test:unit -- --run (frontend)
7 files passed, 12 tests passed

npm run build (frontend)
vue-tsc and Vite production build passed

python -m compileall -q application engine infrastructure interfaces domain shared scripts
EXIT:0

git diff --check
EXIT:0
```

Deterministic smoke script assertions passed:

- valid hierarchy alignment -> `pass`
- volume contradiction -> `block`
- degraded vector evidence -> `review`
- exact digest + non-empty one-shot override -> `pass` and consumed once
- replan preview -> `would_write == false`, `mutations == []`
- frontend status payload contract -> verified by TypeScript/build

The initial unbounded full-suite attempt was terminated by the parent after a Windows runner resource hang; the bounded rerun above completed the entire 2,183-test selection successfully. ChromaDB integration tests were skipped by their existing environment markers.

## Residual risks

- Direct legacy callers that do not inject a gate intentionally retain old behavior; all production planning/prose API factories now inject the gate.
- Alignment report cache is process-local; shared SQLite governance events are required for multi-worker route reconstruction and single-use override auditing.
- Replan preview emits a governance event but has no StoryNode/chapter/contract mutations. This task does not mark any external narrative contract `COMMITTED`.

## Commits

- `4c91c9f6` — `chore: complete hierarchy gate verification` (API factory gate wiring and installer compile repair).
- `55a32c48` — `docs: record final hierarchy gate verification` (this report and the SDD ledger).
