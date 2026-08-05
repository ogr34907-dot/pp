# 19. Regression Test Evidence

## Fresh commands and results

~~~
Python: W:\novel\test\.venv\Scripts\python.exe (3.14.6)

tests/unit/application/engine/test_memory_engine_cache.py                 8 passed
tests/unit/engine/test_context_brief.py                                   6 passed
test_memory_stability_observability.py::test_thirty...                    1 passed
test_memory_stability_observability.py::test_hundred...                   1 passed (slow)
test_workflow_e2e.py::TestPerformance::test_100...                        1 passed (slow)
tests/integration/interfaces/api/v1/test_manuscript_entity_routes.py      6 passed
tests/integration/test_mock_llm_api_acceptance.py                          1 passed
python -m pytest tests -q                                                  2056 passed, 12 skipped, 3 deselected
npm run check:shared-config                                                passed
npm run build                                                              passed
~~~

Earlier focused acceptance evidence retained from this same differential run:

~~~
autopilot contract/policy + setup outline continuation                     18 passed
rewrite coordinator + context budget allocation                            15 passed
Mock Provider                                                              16 passed
manuscript API + daemon manager                                            11 passed, 1 third-party warning
memory production/observability                                            15 passed, 1 slow deselected
StoryPipeline gate/prose/rewrite/retry                                     40 passed
fact-lock/context recall/aftermath guard                                   10 passed
delegate/evolution/recent-context                                          22 passed
chapter prose invocation routes                                             8 passed
~~~

The only recurring warning is FastAPI/TestClient's third-party httpx
deprecation notice. It is not a failing assertion and does not suppress any
project warning/error.
