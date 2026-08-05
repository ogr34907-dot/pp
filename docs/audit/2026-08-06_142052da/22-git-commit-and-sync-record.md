# 22. Git Commit and Sync Record

## Baseline and workspace controls

| Item | Value |
| --- | --- |
| Remote | https://github.com/ogr34907-dot/pp.git |
| Target branch | codex/plotpilot-memory-stability |
| Audit baseline | 142052da869cd9c2e6dbca194ff1f22d143b540d |
| Formal workspace at start | same baseline, clean and read-only |
| Test workspace | W:\novel\test, normal independent Git checkout |
| Python for all backend checks | W:\novel\test\.venv\Scripts\python.exe (3.14.6) |

## Accepted code commit

| Item | Value |
| --- | --- |
| Commit | 078234d2 |
| Subject | fix: complete audit follow-up stability repairs |
| Scope | 21 audited source/test files, 1202 insertions and 89 deletions |
| Test evidence | fresh full backend suite: 2056 passed, 12 skipped, 3 deselected, 1 third-party warning |
| Push status | pending audit-evidence commit and normal push |
| Formal synchronization | prohibited until push and remote SHA verification complete |

## Sensitive-file control

The code commit excludes requirements-local.txt, local models, .venv,
frontend build output, SQLite/WAL/SHM files, logs, screenshots, raw prompt
captures and audit runtime directories. Audit Markdown is force-added
individually from the report root; ignored runtime descendants are not staged.

## Next controlled actions

1. Commit this audit evidence set.
2. Push the current branch normally, without force.
3. Fetch/verify that origin contains the pushed head.
4. Create the final report and sync record update.
5. Perform exactly one formal git pull --ff-only only after its clean branch
   state and remote target are confirmed.
