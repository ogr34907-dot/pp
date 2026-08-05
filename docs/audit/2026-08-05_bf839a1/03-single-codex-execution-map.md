# Single-Codex Execution Map

This audit and repair follows the supplied single-agent work order. Read-only
shell commands may be grouped for latency, but all evidence review, edits,
tests, Git operations, and documentation are performed by the current Codex
in `W:\novel\test`. No subagent, background agent, delegated reviewer, or
parallel implementation process is used.

| Phase | Scope | Evidence artifact | Status |
| --- | --- | --- | --- |
| A | Git baseline, architecture, dependencies, and production entrypoints | `00`, `02` | Completed |
| B | Onboarding and setting lineage from UI to final model request | `04` to `06` | Completed; SETTING-001 and SETTING-002 fixed |
| C | Blueprint, volume, act, and chapter structure | `07` | Completed; DATA-001, BLUEPRINT-001, BLUEPRINT-002 fixed |
| D | EngineDaemon state machine, pause/resume/retry/recovery | `08` | Completed; AUTOPILOT-001 fixed |
| E | Prose context, prompt contract, final provider request, and budgets | `09` | Completed; provider prompt probe passed |
| F | Aftermath, MemoryEngine, continuity, and vector recall | `10` | Completed; 30/100 chapter regressions passed |
| G | SQLite, Write Dispatch, migration, and idempotency | `11` | Completed; DB-001 through DB-003 fixed |
| H/I | Frontend state, REST, and SSE contracts | `12`, `13` | Completed; SSE-001 fixed and frontend build passed |
| J/K | Tests, security, performance, observability, and issue de-duplication | `14`, `16`, `17`, `21` | Completed; OBS-001 and test isolation fixed |
| L | Minimal repairs, regression tests, and Mock-LLM acceptance evidence | `18` to `20` | Completed; final submission-time recheck remains |
| M | Commit, push, remote verification, and formal fast-forward | `22`, `23` | Pending commit gate |

The repair sequence followed the dependency order recorded in
`17-fix-dependency-plan.md`. No business-code change was made in
`W:\novel\PlotPilot`; that checkout remains reserved for the final fast-forward
operation after the remote branch contains the verified commit.
