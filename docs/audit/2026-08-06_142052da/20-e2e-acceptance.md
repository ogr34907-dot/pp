# 20. E2E Acceptance

| Acceptance condition | Result | Evidence class |
| --- | --- | --- |
| New test novel persists and reloads core settings | pass | real FastAPI + SQLite + Mock LLM |
| Macro plan, act chapters and Beat Sheet persist | pass | real FastAPI + SQLite + Mock LLM |
| Worldbuilding and locations reach macro request | pass | isolated invocation/Prompt evidence |
| Unmentioned saved location reaches writer context once | pass | context allocation + isolated API evidence |
| Review/resume preserves frozen full prose context | pass | route test captures actual Mock LLM prompt |
| Canonical extraction/memory blocks false advancement | pass | aftermath, MemoryEngine and long-flow tests |
| Vector failure can recover after canonical commit | pass | chapter-12 long-flow injection |
| Retained-prose rewrite ignores empty planning tail | pass | coordinator and long-flow tests |
| 100 chapter loop is genuine rather than truncated | pass | explicit slow test with chapter_count=100 |
| Embedding configuration save has visible feedback | pass | local browser DOM success message |
| Frontend production build | pass | Vite + vue-tsc |

Excluded from the E2E scope: real paid/provider LLM calls, real user data,
formal-workspace runtime activity, and protected pre-existing prose review
sessions. Those exclusions are intentional safety controls, not failed tests.
