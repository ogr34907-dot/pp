# Architecture Cleanup Call Audit (2026-08-12)

## Scope and Method

This is a static call audit only. No production code, configuration, migration, or test was changed.

Commands used:

```powershell
rg -n -S 'SnapshotService|UnifiedCheckpointService|LLMClient|...'
rg -n -S 'get_persistence_queue\(|PersistentQueueV2\(|...'
rg -n -S '^from engine\.|^import engine\.'
```

Static absence means only that no textual Python call was found under the searched paths. It is not runtime proof: dynamic imports, route discovery, user scripts, and external integrations remain possible.

## Production Entry Points

| Entry point | Current reachable path | Status |
| --- | --- | --- |
| FastAPI / daily local service | `interfaces/main.py` -> `interfaces/daemon_manager.py` -> `scripts/start_daemon.build_daemon()` | Live production path. |
| Background writing daemon | `run_autopilot_daemon_process()` -> `build_daemon()` -> `AutopilotDaemon._process_novel()` -> `engine.runtime.novel_lifecycle.process_novel()` | Live production path. |
| Legacy/new writing branch | `engine/runtime/writing_delegate.py:run_writing()` selects `run_legacy_writing()` unless `use_story_pipeline_for_writing` is true | Both branches remain potentially reachable. |
| HTTP routes | `interfaces/api/routes.py` registers snapshot, checkpoint, evolution, and other engine routers | Public API roots are live. |
| Evaluation/developer scripts | `scripts/evaluation/**`, `scripts/setup/start_daemon.py`, `scripts/test_engine_pipeline.py` | Not daily API startup, but checked-in runnable paths. |

## `engine/`, `application/engine/`, and `engine/runtime/`

### `engine/runtime` is live

`engine/runtime` is not removable. The daemon and runner use:

- `engine/runtime/daemon_loop.py`, `daemon_host.py`, `novel_lifecycle.py`
- `engine/runtime/writing_delegate.py` and `legacy_writing_delegate.py`
- `engine/runtime/runner.py:StoryPipelineRunner`
- `engine/runtime/checkpoint_manager/manager.py`

`application/engine/services/autopilot_daemon.py` is marked deprecated but is still instantiated through the normal daemon process (`interfaces/daemon_manager.py` -> `scripts/start_daemon.py`). It delegates lifecycle operations into `engine.runtime`; it cannot be deleted until the daemon factory creates `EngineDaemon` or `StoryPipelineRunner` directly and that path is integration-tested.

`engine/runtime/writing_delegate.py` keeps two write paths. The default branch still imports and runs `engine/runtime/legacy_writing_delegate.py`; therefore neither the legacy writer nor its pipeline dependencies are statically deletable.

### `application/engine` is also live

It supplies the API/daemon boundary: persistence queue V1, shared state, streaming bus, stop signals, query/state bootstrap, aftermath, candidate workflow, and DAG services. `interfaces/runtime.py`, `interfaces/daemon_manager.py`, `interfaces/main.py`, `engine/pipeline/base.py`, and many API dependencies import it directly.

### Root `engine/` is live, but contains migration residue

`engine/pipeline/**`, `engine/pipelines/**`, and `engine/runtime/**` are referenced by the daemon, StoryPipeline, and theme registry. Do not remove the root package.

There is duplicated code under `engine/application/**`:

- `engine/application/checkpoint_manager/manager.py` and `engine/runtime/checkpoint_manager/manager.py` have identical SHA-256 in this checkout.
- Nevertheless `interfaces/api/v1/engine/checkpoint_routes.py` imports the `engine.application` path for create/rollback/list, and several `scripts/evaluation/**` files import `engine.application.*` quality/checkpoint/orchestrator paths.
- `engine/application/writing_orchestrator.py` is not byte-identical to `engine/runtime/writing_orchestrator.py`; treat it as an independent compatibility risk until behavioral comparison and caller migration are complete.

**Deletion gate:** migrate `checkpoint_routes.py` and all checked-in script callers to `engine.runtime`, add import-smoke coverage for their replacements, then use a clean-tree `rg` proving no `engine.application` imports before deleting the alias package.

## Persistence Queues V1/V2

| Component | Static call evidence | Test evidence | Conclusion |
| --- | --- | --- | --- |
| V1 `application/engine/services/persistence_queue.py` | Startup initializes/injects/consumes it in `interfaces/runtime.py`, `interfaces/daemon_manager.py`; write dispatch and pipeline code call `get_persistence_queue()` | `tests/unit/application/engine/test_persistence_queue.py`; daemon-manager coverage | Live authoritative write queue. Not removable. |
| V2 `persistence_queue_v2.py` | No production call to `get_persistent_queue_v2()` or `initialize_persistent_queue_v2()` found. `StatePublisher` indirectly creates `PersistenceQueueAdapter`. | `tests/unit/application/engine/test_persistence_queue_v2.py` | V2 is reachable only through the adapter's lazy constructor. Not proven unused. |
| Adapter `persistence_queue_adapter.py` | Only `application/engine/services/state_publisher.py` directly calls `get_persistence_queue_adapter()` | No adapter-specific test found | It attempts V2 then silently falls back to V1. This is a semantic fallback, not a completed migration. |

`PersistenceQueueAdapter` currently catches any V2 initialization exception and falls back to V1. This conflicts with the desired explicit queue migration rule: V1 and V2 have different durability/consumer semantics. Do not delete either implementation first. First select one production queue in startup composition, remove silent fallback, then add restart/recovery and enqueue-consumption integration tests.

## Snapshots and Checkpoints

These are distinct live concepts and cannot be treated as duplicate names:

| Service | Role and reachable callers | Test evidence | Conclusion |
| --- | --- | --- | --- |
| `application/snapshot/services/snapshot_service.py:SnapshotService` | Semantic novel snapshots. Injected by `interfaces/api/dependencies.py:get_snapshot_service`; used by public `snapshot_routes.py`; also used by `application/engine/services/state_bootstrap.py`. | `tests/unit/application/snapshot/services/test_snapshot_service_engine_state.py`, database integration coverage | Live. |
| `application/evolution/services/snapshot_service.py:EvolutionSnapshotService` | Per-chapter evolution state. Injected into `evolution_routes.py`. | `tests/unit/application/evolution/test_snapshot_service.py` | Live, separate domain model. |
| `application/checkpoint/services/unified_checkpoint_service.py:UnifiedCheckpointService` | Worldline/rewrite checkpoint authority. Injected into `worldline_routes.py`, `ChapterRewriteCoordinator`, and daemon startup helper. | `tests/unit/application/checkpoint/test_unified_checkpoint_*.py`, rewrite coordinator tests | Live. |
| `engine/runtime/checkpoint_manager/manager.py:CheckpointManager` | Engine checkpoint graph. API dependency plus checkpoint routes. | No dedicated runtime manager unit test found in the searched suite; route and evaluation callers exist. | Live but needs focused API/service tests before migration. |

The old `engine.application.checkpoint_manager` is a duplicate import location, not evidence that `CheckpointManager` itself is obsolete.

## Runtime Import Aliases

| Alias mechanism | Actual callers | Status |
| --- | --- | --- |
| `engine/application/**` -> `engine/runtime/**` compatibility modules | Public checkpoint routes and evaluation scripts directly import `engine.application.*` | Live compatibility surface. |
| `application/dtos/__init__.py` `sys.modules` aliases | No non-test textual `application.dtos.*` caller found | Candidate for removal only after import-time smoke test and clean-tree check; dynamic/external consumers remain unknown. |
| `application/services/__init__.py` `sys.modules` aliases | No non-test textual `application.services.*` caller found | Same candidate status; verify package import side effects before removal. |
| `interfaces/daemon_manager.py` `sys.modules["__shared_state"]` | Read via `sys.modules.get("__shared_state")` in runtime/blueprint paths | Live inter-process shared-state bridge. Do not delete without replacing the injection contract. |

## Legacy `LLMClient`

`infrastructure/ai/llm_client.py:LLMClient` has live non-test consumers:

- `interfaces/api/dependencies.py:get_tension_analyzer()`
- `interfaces/api/v1/reader/__init__.py:_get_service()`
- `scripts/setup/start_daemon.py`
- `scripts/evaluation/novel_writing_runner.py`

`LLMClient` wraps `DynamicLLMService` and exposes string-prompt `generate` / `stream_generate` compatibility. It is not safe to delete while the reader and tension API services instantiate it. This audit did not inspect or modify LLM implementation files.

**Deletion/migration gate:** change each caller to the typed LLM service contract, add tests for reader simulation and tension analysis using the replacement, then verify `rg` has no non-test `LLMClient` imports. Keep evaluation script migration separate from API production migration.

## Recommended Order (No Code Change in This Audit)

1. Replace the production `AutopilotDaemon` factory with the selected candidate-first runtime and prove both writing modes with integration tests.
2. Make queue selection explicit in startup; add V2 recovery tests; only then remove V1/adapter or V2 according to the selected target.
3. Migrate `checkpoint_routes.py` and scripts from `engine.application` to `engine.runtime`; delete duplicate aliases only after a clean static scan.
4. Keep the three snapshot/checkpoint domains separate until a data-model migration explicitly merges them.
5. Migrate LLMClient consumers under the LLM workstream, then remove the wrapper after a clean call graph and API tests.

## Audit Limitation / Required Runtime Proof

Before deletion, run the normal `8005` startup, exercise candidate generation in `continuous` and `chapter_review`, invoke checkpoint/snapshot/worldline endpoints, and collect import/runtime telemetry. Static `rg` evidence alone does not prove a desktop-local feature or an external script is unused.
