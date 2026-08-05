# Testing, Security, Performance, and Observability Audit

## Test Results

All Python commands use only `W:\novel\test\.venv\Scripts\python.exe`,
which reports Python `3.14.6`. The repository default backend suite completed
with `2036 passed, 12 skipped, 3 deselected, 1 warning in 145.75s`. The one
warning is the existing Starlette `TestClient` warning about an `httpx`
deprecation; it is not a test failure.

Focused acceptance and regression evidence includes:

| Area | Evidence | Result |
| --- | --- | --- |
| FastAPI, SQLite, real services, deterministic external LLM | `tests/integration/test_mock_llm_api_acceptance.py` | Passed |
| StoryPipeline default memory route | memory observability default-pipeline selector | Passed |
| Thirty chapter vector-failure recovery | `test_thirty_chapter_regression_recovers_injected_vector_failure` | Passed |
| True hundred chapter slow flows | memory regression plus workflow performance selector | Both passed |
| Restart interruption and autopilot resume | runtime and resume test selectors | Passed |
| Final provider settings contract | safe trace-marker probe with initial/re-dispatched calls | Passed |

`npm --prefix frontend run check:shared-config` and
`npm --prefix frontend run build` both passed. The frontend package has no
independent `lint` or `test` script. This is recorded as an environment/tooling
gap; no lint or frontend-test success is claimed.

## Test Isolation Repair

`TEST-002` was found while adding the FastAPI acceptance test. CPMS singletons
could retain a `PromptManager`, `PromptRegistry`, and `PromptGateway` bound to
a temporary SQLite database after a test ended. The test fixture now clears
only those test-bound singletons in setup/teardown. Production fail-closed
behavior for missing CPMS nodes remains intact. This avoids order-dependent
false failures without broadening production state management.

## Security and Logging

`OBS-001` is fixed: malformed planning JSON diagnostics no longer log raw
model output. The diagnostic retains bounded non-sensitive facts such as
length, digest, parser position, and error class. The audit reports and
provider probe record only boolean trace-marker presence, never raw prompts,
API keys, private gateway URLs, local embedding-model paths, or user prose.

The local `requirements-local.txt` remains excluded from Git and its required
SHA-256 is `95DA2F8BF1C38A80C54D1037E5EB5C462CEF8EABE39905CBD1C8D4D91A295824`.
No local embedding model, `.venv`, database, runtime log, or build artifact is
included in the intended commit.

## Performance and Operational Scope

No broad performance claim is made. The bounded vector/T3 retrieval and
chapter-memory regressions exercise the relevant limits and failure recovery.
The 30- and 100-chapter tests are real iteration loops, not short-loop
substitutes. Browser-level visual performance and live-provider cost testing
remain outside this offline deterministic acceptance run.
