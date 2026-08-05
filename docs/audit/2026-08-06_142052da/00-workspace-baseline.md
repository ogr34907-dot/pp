# 00. Workspace Baseline

## Scope and execution boundary

- Audit date: 2026-08-06 (Asia/Shanghai)
- Executor: current Codex only; no subagent, parallel agent, background agent, or delegated implementation was used.
- Formal workspace (read-only during this audit): `W:\novel\PlotPilot`
- Test workspace (the only writable workspace): `W:\novel\test`
- Remote: `https://github.com/ogr34907-dot/pp.git`
- Remote default branch, confirmed with `git ls-remote --symref origin HEAD`: `codex/plotpilot-memory-stability`

## Git baseline

| Source | Branch | HEAD | State at baseline |
| --- | --- | --- | --- |
| Remote default branch | `codex/plotpilot-memory-stability` | `142052da869cd9c2e6dbca194ff1f22d143b540d` | Remote HEAD; fetched without pull |
| Formal workspace | `codex/plotpilot-memory-stability` | `142052da869cd9c2e6dbca194ff1f22d143b540d` | Clean; tracks `origin/codex/plotpilot-memory-stability` |
| Test workspace base | `codex/plotpilot-memory-stability` | `142052da869cd9c2e6dbca194ff1f22d143b540d` | Contains inherited uncommitted audit repairs |

Baseline commit metadata:

```text
2026-08-06T00:26:32+08:00
fix: complete PlotPilot audit stability repairs
```

The remote, formal checkout, and test checkout share the same committed base. This audit is not a clean-tree audit because the test workspace already contains inherited, uncommitted repair work. Those changes are preserved and are listed below; they must be reviewed, validated, and either included deliberately or left uncommitted. No `reset`, `clean`, `restore`, or overwrite was run.

## Inherited test-workspace changes

```text
M  frontend/src/components/StoryStructureTree.vue
M  infrastructure/ai/providers/mock_provider.py
M  infrastructure/persistence/database/manuscript_entity_repository.py
M  interfaces/api/v1/core/manuscript_entity_routes.py
M  interfaces/daemon_manager.py
M  tests/unit/infrastructure/ai/providers/test_mock_provider.py
M  tests/unit/interfaces/test_daemon_manager.py
?? tests/integration/interfaces/api/v1/test_manuscript_entity_routes.py
```

The initial diff summary is 495 insertions and 49 deletions across the seven tracked files. The untracked integration test is not represented by that summary. The work is traceable to manuscript compatibility/Write Dispatch acknowledgement, a structure-tree empty state, daemon orphan cleanup, and deterministic Mock Provider regressions. It remains subject to the issue-register and test gates in this audit.

## Environment baseline

| Item | Observed value |
| --- | --- |
| Python executable for all backend work | `W:\novel\test\.venv\Scripts\python.exe` |
| Python version | `3.14.6` |
| Project constraint | `requires-python = ">=3.14,<3.15"` in `pyproject.toml` |
| Backend | FastAPI + Uvicorn + Pydantic 2 + SQLite |
| Frontend | Vue 3 + TypeScript + Vite + Naive UI + Pinia + Vue Router + ECharts |
| Persistence | SQLite with the existing Write Dispatch single-writer path |
| Vector support | ChromaDB / FAISS according to installed project configuration |
| Local embedding dependencies | `requirements-local.txt` exists and is explicitly preserved; it will not be deleted, overwritten, or staged |

The installation helper still names the pinned embedded distribution `Python 3.14.5`; it validates the Python 3.14 minor series. The active test environment is the compatible newer patch release 3.14.6.

## Verified production entry and compatibility paths

The code evidence establishes the following default chain:

```text
Uvicorn (interfaces.main:app)
  -> FastAPI lifespan / BackendLifecycle.startup
  -> AutopilotDaemonManager.start (unless DISABLE_AUTO_DAEMON=1)
  -> child process uses scripts.start_daemon.build_daemon
  -> EngineDaemon
  -> StoryPipelineRunner + DaemonHostMixin
  -> BaseStoryPipeline
```

- `scripts/start_daemon.py` creates `EngineDaemon` at line 238.
- `EngineDaemon` instantiates `StoryPipelineRunner`; the runner inherits `BaseStoryPipeline`.
- With `PLOTPILOT_USE_STORY_PIPELINE` unset, the default is StoryPipeline writing. `off`/`legacy` is an explicit emergency fallback, not the default.
- `DISABLE_AUTO_DAEMON=1` prevents the FastAPI-managed daemon child from starting.
- `application.engine.services.autopilot_daemon.AutopilotDaemon` remains a deprecated import-compatibility path and is not the default production entry.

## Available commands at baseline

```powershell
# Backend, isolated test workspace only
& 'W:\novel\test\.venv\Scripts\python.exe' -m uvicorn interfaces.main:app --host 127.0.0.1 --port 8015

# Backend tests, isolated test workspace only
& 'W:\novel\test\.venv\Scripts\python.exe' -m pytest tests -v

# Frontend
Set-Location W:\novel\test\frontend
npm run build
npm run dev -- --port 3015
```

`README.md` documents the Uvicorn development path on port 8005 and Python 3.14.x. `README_LOGGING.md` separately documents that directly running `interfaces/main.py` uses port 8000; this is an explicit alternate path rather than evidence that the Uvicorn entry is stale.

## Formal-workspace protection record

The formal workspace received only permitted read-only Git operations during baseline verification, including `git fetch --prune origin`. It was not pulled, edited, formatted, started, migrated, or used for test data. It remains eligible for exactly one future `git pull --ff-only` only after all test gates pass, the accepted commit is pushed, and the remote default branch is confirmed to contain that commit.

## Baseline assessment

- Baseline adequacy: **conditional**. The committed base is current and consistent across remote/formal/test, but the test workspace has inherited uncommitted changes.
- Audit provenance: code analysis begins from `142052da869cd9c2e6dbca194ff1f22d143b540d` plus the explicit inherited test-workspace diff above.
- Formal workspace protection: **verified at baseline**.
- Safe to continue in test workspace: **yes**, provided all changes remain scoped, are attributed in the issue register, and are not overwritten.
