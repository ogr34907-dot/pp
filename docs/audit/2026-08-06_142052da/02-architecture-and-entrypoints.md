# 02. Architecture and Entrypoints

## Verified runtime chain

~~~
Uvicorn / interfaces.main:app
  -> FastAPI lifespan and BackendLifecycle
  -> AutopilotDaemonManager.start (unless DISABLE_AUTO_DAEMON=1)
  -> scripts.start_daemon.build_daemon
  -> EngineDaemon
  -> StoryPipelineRunner
  -> BaseStoryPipeline
  -> ChapterAftermathPipeline / canonical narrative sync
~~~

PLOTPILOT_USE_STORY_PIPELINE defaults to the StoryPipeline. The off and legacy
values are explicit emergency compatibility modes, not the normal path. The
deprecated AutopilotDaemon import path remains a compatibility surface; it is
not the production writing entrypoint.

## Responsibility map

| Layer | Responsibility retained in this audit |
| --- | --- |
| domain | entities, value objects, invariants and repository contracts |
| application | onboarding, blueprint, rewrite coordination, context, memory and orchestration services |
| engine | chapter runtime, context assembly, pipeline stages and canonical aftermath |
| infrastructure | SQLite repositories, Write Dispatch, prompt packages and model providers |
| interfaces | FastAPI routes, lifecycle and daemon process management |
| frontend | Vue/TypeScript user flows and API presentation |

## Runtime and dependency facts

- Python contract: >=3.14,<3.15; actual backend test interpreter: 3.14.6.
- Backend: FastAPI, Pydantic, SQLite and the existing Write Dispatch route.
- Frontend: Vue 3, TypeScript, Vite, Naive UI, Pinia, Vue Router and ECharts.
- Vector integration: existing ChromaDB/FAISS configuration; no new vector
  service was introduced.

## Stable boundaries deliberately retained

- CPMS/AI Invocation contracts remain the only prompt/render boundary.
- chapter-narrative-sync remains the canonical chapter-extraction contract.
- ChapterAftermathPipeline remains the gate before chapter advancement.
- Existing SQLite migration/Write Dispatch mechanisms remain authoritative.
- The repairs add bounded context and validation behavior rather than another
  persistence or memory stack.
