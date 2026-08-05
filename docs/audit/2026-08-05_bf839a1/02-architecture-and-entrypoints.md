# Architecture and Production Entrypoints

## Runtime path confirmed from code

```text
uvicorn interfaces.main:app
  -> FastAPI lifespan
    -> BackendLifecycle.startup()
      -> Write Dispatch consumer startup
      -> AutopilotDaemonManager.start()
        -> run_autopilot_daemon_process()
          -> scripts.start_daemon.build_daemon()
            -> EngineDaemon
              -> StoryPipelineRunner / BaseStoryPipeline
```

Relevant implementation files are `interfaces/main.py`, `interfaces/runtime.py`,
`interfaces/runtime_state.py`, `interfaces/daemon_manager.py`,
`scripts/start_daemon.py`, `engine/runtime/engine_daemon.py`,
`engine/runtime/runner.py`, `engine/runtime/writing_delegate.py`, and
`infrastructure/engine/story_pipeline_environment.py`.

`PLOTPILOT_USE_STORY_PIPELINE` defaults to the StoryPipeline writing route.
Only explicit values such as `off`, `legacy`, `false`, or `0` use the legacy
writer. `DISABLE_AUTO_DAEMON=1` suppresses lifecycle daemon startup and is
useful only for controlled test/API runs.

## Major ownership boundaries

| Boundary | Primary responsibility | Audit conclusion |
| --- | --- | --- |
| `domain` | aggregates, identifiers, value objects, invariants | retained; no architecture replacement proposed |
| `application/onboarding`, `core` | novel setup and generation preferences | correct persistence model exists; several readers use the wrong aggregate fields |
| `application/blueprint` | volume/act/chapter planning and structural confirmation | contains P0 destructive confirmation and unsafe fallback paths |
| `application/engine`, `engine/pipeline` | context, writing orchestration, aftermath | long-form memory path is substantially implemented; setup injection is incomplete |
| `application/world` | Bible, narrative promise, canonical narrative extraction | canonical sync exists; several setup readers are stale |
| `infrastructure/persistence` | SQLite schema, migrations, repositories, Write Dispatch | targeted SQL, migration order, and run-state persistence repairs are needed |
| `interfaces/api` | HTTP/SSE API and frontend-facing state projection | normal routes work; non-log SSE lacks replay-contract semantics |
| `frontend` | Vue 3 interface, API clients, stores, cockpit | embedding-save feedback already exists; no broad visual redesign is justified before functional fixes |

## Technology and execution environment

| Component | Observed baseline |
| --- | --- |
| Backend | FastAPI, Python 3.14.6 test virtual environment |
| Frontend | Vue 3, TypeScript, Vite, Naive UI, Pinia |
| Persistence | SQLite with explicit Write Dispatch model |
| Vector support | ChromaDB / FAISS adapters |
| CI configuration | backend workflow selects Python 3.14 |
| Python contract | `pyproject.toml` requires `>=3.14,<3.15` |

The repository no longer uses `datetime.utcnow()` in production source, and
the existing `tests/unit/test_time_api_contract.py` guards that contract.
