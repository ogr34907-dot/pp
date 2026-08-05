# Memory, Aftermath, Continuity, and Vector Audit

## Confirmed existing mechanism

The audit did not find evidence that the long-form memory mechanism described
in the previous stability work is absent. The following components exist and
have targeted coverage:

| Capability | Primary implementation |
| --- | --- |
| Canonical narrative extraction | `application/world/services/chapter_narrative_sync.py` |
| Commit gate and retries | `application/engine/services/chapter_aftermath_pipeline.py` |
| Durable memory barrier | `application/engine/services/memory_engine.py` |
| Rewrite modes / invalidation | `application/core/services/chapter_rewrite_coordinator.py` |
| Checkpoint anchor persistence | `application/checkpoint/services/unified_checkpoint_service.py` |
| 30/100 chapter regression | `tests/unit/engine/test_memory_stability_observability.py` |

The schema includes source content hashes, revisions, pipeline versions,
status/error/attempt data on chapter summaries and a CAS-shaped
`chapter_narrative_commits` table. This matches the intended aftermath
contract rather than a best-effort, exception-only flow.

## Scope decision

No replacement, extraction, or broad refactor of MemoryEngine, CPMS, the
vector store, or the rewrite coordinator is justified by the current
evidence. The P0/P1 work must preserve their behavior. The full 30 and 100
chapter tests remain mandatory final regression gates because structural and
setting fixes can affect their input context.

## Residual audit limitation

The current audit's dynamic provider tests prove prompt marker absence and the
focused regression proves vector-failure recovery. The full from-new-book UI
through automatic prose flow remains an acceptance task after repair, not a
claim already proven in this report.
