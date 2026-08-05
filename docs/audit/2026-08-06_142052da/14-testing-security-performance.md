# 14. Testing, Security and Performance

## Test inventory executed

| Class | Command / scope | Result |
| --- | --- | --- |
| Focused memory/context | cache, context brief and memory stability modules | passed |
| Long-flow | explicit 30 chapter and 100 chapter StoryPipeline regressions | passed |
| Workflow load | explicit 100 chapter generation simulation | passed |
| API integration | Mock LLM planning chain and manuscript compatibility routes | passed |
| Full backend | python -m pytest tests -q | 2056 passed, 12 skipped, 3 deselected |
| Frontend config | npm run check:shared-config | passed |
| Frontend production type/build | npm run build | passed |
| Browser UI | isolated local Vite/API flow | passed with one expected Tauri probe warning |

The full suite produced one third-party FastAPI/TestClient deprecation warning.
It is not a project test failure. Twelve skips and three default deselections
are reported rather than hidden; the slow coverage required by this work order
was run explicitly.

## Security and data handling

- No real LLM request, API key, local embedding model, user novel, prompt
  capture, SQLite database, runtime log or screenshot is staged for commit.
- requirements-local.txt and local embedding assets remain untouched and
  excluded from the source commit.
- Dynamic test data lived only in ignored audit/runtime paths or pytest
  temporary directories.

## Performance evidence

The explicit hundred-iteration memory regression and the hundred-iteration
workflow simulation complete deterministically within this environment. They
are correctness regressions, not a production throughput benchmark; no
unsubstantiated latency/SLA claim is made.
