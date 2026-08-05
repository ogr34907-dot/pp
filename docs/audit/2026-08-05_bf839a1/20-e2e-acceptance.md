# E2E Acceptance Record

## Executed Acceptance Path

The strongest dynamically executed acceptance path is **real API + real
application services + real SQLite + deterministic Mock LLM**. It creates a
test novel, saves six unique setting markers, refreshes those settings,
generates a macro plan, safely confirms macro and chapter structure, persists
Beat Sheets, reloads them, and verifies real chapter rows in temporary SQLite.

The default StoryPipeline path is separately exercised with its real memory
and aftermath fixtures: it propagates persisted memory to later context,
recovers from a vector failure, supports restart/recovery state, and passes
the long 30/100 chapter continuity regressions. Final-provider trace evidence
checks that settings reach both initial and retry dispatches.

## Requirement Matrix

| Requirement | Classification | Status | Evidence |
| --- | --- | --- | --- |
| New novel creation and setting save | Real API + Mock LLM | Passed | `test_mock_llm_api_acceptance.py` |
| Refresh retains settings | Real API + Mock LLM | Passed | Same acceptance test |
| Macro plan and safe persistence | Real API + Mock LLM | Passed | Same acceptance test plus planning safety tests |
| Act/chapter and Beat Sheet persistence | Real API + Mock LLM | Passed | Same acceptance test |
| Final provider sees T0 locked settings on first/retry | Final provider spy | Passed | Safe trace-marker probe |
| Default writing memory/aftermath connection | Real StoryPipeline fixture | Passed | Memory observability regression |
| Vector failure recovery | Real StoryPipeline fixture | Passed | Thirty-chapter regression |
| Pause/restart/resume status | Real runtime/API fixture | Passed | Runtime and resume tests |
| Cross-act/volume long-run behavior | Deterministic long-flow fixture | Passed | Both true 100-chapter selectors |
| SSE ID/replay/dedupe | Backend/store protocol tests | Passed | Autopilot and DAG SSE tests |
| Browser UI journey | Real UI + API + Mock LLM | Not executed | Explicitly not claimed |
| Real external provider | Live provider | Not executed | Credentials/cost/user-data protected |

## Acceptance Interpretation

The executed backend, API, persistence, prompt, and deterministic pipeline
evidence is sufficient for the scoped repair release. The lack of a
browser-controlled UI journey and standalone frontend lint/test scripts are
non-blocking residual acceptance limits. They are not rewritten as green
results, and they remain appropriate candidates for a later UI-focused test
work item.
