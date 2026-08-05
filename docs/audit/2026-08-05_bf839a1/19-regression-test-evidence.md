# Regression Test Evidence

## Runtime and Selection

All backend commands use `W:\novel\test\.venv\Scripts\python.exe` under
Python 3.14.6. No test command writes to the formal workspace or its database.
The listed long-running selectors are project tests, not ad hoc substitutes.

## Completed Commands and Results

| Command or selector | Result | What it proves |
| --- | --- | --- |
| `tests/integration/test_mock_llm_api_acceptance.py` plus workflow, memory, runtime, and resume selectors | `15 passed, 1 deselected, 1 warning in 9.07s` | Real FastAPI/SQLite/services with deterministic LLM boundary, default memory pipeline, restart, and resume flows |
| `tests/unit/engine/test_memory_stability_observability.py::test_thirty_chapter_regression_recovers_injected_vector_failure` | `1 passed in 7.66s` | Thirty chapters retain continuity across an injected vector failure and recovery |
| `tests/unit/engine/test_memory_stability_observability.py::test_hundred_chapter_memory_stability_regression` and `tests/integration/test_workflow_e2e.py::TestPerformance::test_100_chapter_generation_simulation` | `2 passed in 11.86s` | Both slow selectors execute genuine 100-chapter simulations |
| Default backend pytest suite | `2036 passed, 12 skipped, 3 deselected, 1 warning in 145.75s` | Repository-wide default backend regression evidence |
| `npm --prefix frontend run check:shared-config` | Passed | Shared frontend taxonomy/config check |
| `npm --prefix frontend run build` | Passed | TypeScript production build plus Vite production build |
| `PYTHONPATH='.'` setting prompt probe | 2 provider calls; six markers present in both | Initial and re-dispatched final provider payloads retain title, genre, theme, world, style, and taboo settings |

## Notes and Limits

The correct 30-chapter selector is
`test_thirty_chapter_regression_recovers_injected_vector_failure`; an older
name cited in earlier notes does not exist. The Starlette warning is about the
current `TestClient`/`httpx` deprecation path. It is recorded, not hidden.

The frontend package does not define standalone lint or test scripts. Build
and shared-config evidence is therefore reported, while lint/test success is
not claimed. Full raw pytest logs are retained only as ignored local evidence
and are not added to Git.
