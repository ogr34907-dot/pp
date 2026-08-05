# 23. Final Report (Pre-Synchronization)

## Decision

**Conditional pass: the planned core repairs are accepted, the acceptance
evidence is committed and pushed, and two non-blocking limitations remain.**

This is intentionally a pre-synchronization report.  At the time this file
was created, the formal checkout has not been edited or pulled.  A separate
controlled `git pull --ff-only` is permitted only after this report itself is
committed, pushed, and its remote SHA is verified.

## Repository Provenance

| Item | Value |
| --- | --- |
| Remote | `https://github.com/ogr34907-dot/pp.git` |
| Target and remote default branch | `codex/plotpilot-memory-stability` |
| Audit baseline | `142052da869cd9c2e6dbca194ff1f22d143b540d` |
| Baseline subject | `fix: complete PlotPilot audit stability repairs` |
| Accepted follow-up code commit | `078234d2797c5ed17441b0440eda83cb5ccd41b8` |
| Code subject | `fix: complete audit follow-up stability repairs` |
| Acceptance-evidence commit before this report | `ffdb7d9c17c9bb871d23805154c504e82285e020` |
| Evidence subject | `docs(audit): record acceptance evidence` |
| Test checkout before this report | `ffdb7d9c17c9bb871d23805154c504e82285e020`, clean |
| Remote tracking branch before this report | `ffdb7d9c17c9bb871d23805154c504e82285e020` |
| Formal checkout before synchronization | `142052da869cd9c2e6dbca194ff1f22d143b540d`, clean, behind two commits |

All implementation, test execution, Git commits, and pushes were performed
from `W:\\novel\\test`.  `W:\\novel\\PlotPilot` remained a read-only formal
checkout during the repair and acceptance work.  No business file, database,
configuration, dependency, or service was written there.

## Accepted Scope

The work remains within the existing DDD, StoryPipeline, EngineDaemon, CPMS,
SQLite/Write Dispatch, MemoryEngine, canonical narrative synchronization, and
Vue application boundaries.  It does not introduce a database, queue, memory
system, vector service, provider interface, or alternate execution path.

The accepted `078234d2` follow-up repair contains 21 source/test files with
1,202 insertions and 89 deletions.  It closes the audited follow-up items:

- `MEMORY-001`: deterministic Mock memory extraction conforms to the existing
  strict `MemoryDeltaPayload` contract, preserving the existing retry and
  fail-closed behavior.
- `MEMORY-002`: retained-prose replay stops at the last nonempty prose chapter
  and does not treat future planned rows as invalid retained content.
- `SETTING-003`, `SETTING-004`, and `PROMPT-001`: persisted worldbuilding and
  locations reach the appropriate macro/prose request paths; a bounded T1
  location catalog is accounted for; review/resume preserves frozen explicit
  prose context rather than replacing it with stale Variable Hub data.
- `ONBOARD-001` and `BLUEPRINT-003`: an AI plot preview is not labeled as
  persisted until saved, and phase chapter ranges remain within a short
  novel's actual target.
- `MANUSCRIPT-001`, `MANUSCRIPT-002`, and `API-001`: compatibility access uses
  unified entity sources, response-bearing writes wait for visible persistence,
  and whitespace-only holder IDs retain the documented HTTP 422 boundary.
- `FRONTEND-001`: a non-autopilot empty structure tree exposes the existing
  narrative-planning action.
- `MOCK-E2E-001` through `MOCK-E2E-004`: deterministic provider responses now
  match the real macro, preplanning, prose, and canonical-sync contracts.
- Daemon test hygiene limits orphan cleanup to the current interpreter and
  supports an explicit isolated-test opt-out.

The prior memory-stability repairs already present in baseline `142052da` were
kept in place and exercised by the full suite and the long-flow regressions;
they were not reimplemented or broadened.

## Acceptance Evidence

Backend commands used the project-selected interpreter only:
`W:\\novel\\test\\.venv\\Scripts\\python.exe` (Python 3.14.6).

| Check | Result |
| --- | --- |
| `python -m pytest tests -q` | `2056 passed, 12 skipped, 3 deselected, 1 warning` |
| MemoryEngine cache unit module | `8 passed` |
| Context brief unit module | `6 passed` |
| 30-chapter recovery regression | `1 passed` |
| 100-chapter memory-stability regression (`slow`, explicit) | `1 passed` |
| Separate 100-chapter workflow simulation (`slow`, explicit) | `1 passed` |
| Mock LLM API acceptance | `1 passed` |
| Manuscript FastAPI/SQLite route acceptance | `6 passed` |
| `npm --prefix W:\\novel\\test\\frontend run check:shared-config` | passed |
| `npm --prefix W:\\novel\\test\\frontend run build` | passed |
| Browser acceptance for embedding-config save feedback | passed; DOM contained `嵌入配置已保存` |

The 30-chapter regression includes a chapter-9 restart, chapter-12 vector
failure/recovery, chapter-17 extraction failure with three attempts and
manual recovery, and a chapter-10 rewrite after chapter 20.  It validates
ordered replay, canonical guards, derived memory, foreshadowing, triples, and
auxiliary-stage draining.  The slow 100-chapter check uses a genuine
`chapter_count=100` loop.

The sole backend warning is a third-party FastAPI/TestClient dependency-chain
deprecation from `httpx`; it is not a test failure or a project warning
suppression.  Dynamic acceptance used real FastAPI services and SQLite with a
deterministic Mock LLM transport.  It did not use a paid/real provider, real
user data, the formal checkout runtime, or the protected prose-review
sessions.

## Current Issue Status

For this differential closure, 13 P1 items are accepted and committed; two P2
items (`API-001` and `FRONTEND-001`) are accepted and committed.  The
provenance-only `BASELINE-001` is documented, and the following items remain
non-blocking:

- `TEST-002` (P2): `frontend/package.json` does not define a standalone lint
  or frontend unit-test command.  The shared-config check, TypeScript check
  embedded in the production build, Vite build, and browser acceptance passed.
  This is a coverage gap, not a claim that lint or frontend unit tests passed.
- Browser-preview Tauri IPC probe (P3): a recoverable desktop-shell detection
  warning appears before the configured HTTP fallback.  The UI and API flow
  continue normally.  No source change is justified without a desktop-shell
  reproduction.

The detailed root cause, call path, test-first evidence, and explicit
non-changes are preserved in `16-issue-register.md`, `17-fix-dependency-plan.md`,
`18-change-log.md`, `19-regression-test-evidence.md`, and
`20-e2e-acceptance.md` in this directory.

## Data and Sensitive-File Controls

No force push, reset, clean, restore, rebase, or cherry-pick was used.
`requirements-local.txt`, local embedding models, `.venv`, SQLite databases,
WAL/SHM files, logs, screenshots, raw prompt captures, caches, and build
outputs remain local and are excluded from Git.  Audit runtime descendants
under `ui-e2e-*` also remain ignored and must not be staged.

## Formal Synchronization Gate

Before this report is committed, the test checkout and its remote tracking
branch are both `ffdb7d9c17c9bb871d23805154c504e82285e020`; the formal checkout
is clean at `142052da869cd9c2e6dbca194ff1f22d143b540d` on the same target branch.
After this report is committed and pushed, the only permitted formal-workspace
write is one `git pull --ff-only`.  The final user handoff must record the
actual post-pull formal SHA and clean status rather than predicting it here.

## Final Pre-Sync Conclusion

**Conditional pass: core planned repairs are accepted and the only remaining
findings are the explicit non-blocking P2/P3 limitations above.  Formal
synchronization is still pending the final report commit, normal push, remote
SHA verification, and one clean fast-forward operation.**
