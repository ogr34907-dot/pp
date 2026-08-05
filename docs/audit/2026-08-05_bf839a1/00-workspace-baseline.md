# Workspace Baseline

## Scope

- Audit executor: current Codex only. No subagents, background agents, or delegated audit work were used.
- Test workspace: `W:\novel\test`
- Formal workspace: `W:\novel\PlotPilot`
- Remote: `https://github.com/ogr34907-dot/pp.git`
- Audit baseline timestamp: 2026-08-05, Asia/Shanghai

## Git Baseline

| Location | Branch | HEAD | Status | Relationship to origin |
| --- | --- | --- | --- | --- |
| Remote default | `codex/plotpilot-memory-stability` | `bf839a1491aa15553309834faa760e967c3c8991` | N/A | Origin HEAD points here |
| Formal workspace | `codex/plotpilot-memory-stability` | `bf839a1491aa15553309834faa760e967c3c8991` | Clean | Tracks matching origin branch |
| Test workspace | `codex/plotpilot-memory-stability` | `bf839a1491aa15553309834faa760e967c3c8991` | Clean | Tracks matching origin branch |

Latest remote commit:

```text
bf839a1491aa15553309834faa760e967c3c8991
2026-08-05T18:52:07+08:00
fix(frontend): acknowledge embedding config save
```

`git fetch --prune origin` was run in both workspaces. Both Git status checks were clean and the three baseline revisions match. The audit is therefore based on the remote latest revision at the time of this record.

## Workspace Protection

- The formal workspace is treated as read-only until all audit fixes pass their required validation and exist on the remote target branch.
- All scanning, reports, tests, temporary data, source changes, commits, and pushes happen in the test workspace.
- No reset, clean, force push, rebase, cherry-pick, or direct file copy from test to formal workspace is permitted.
- `requirements-local.txt` and `.venv` remain local assets. The currently preserved `requirements-local.txt` SHA-256 is `95DA2F8BF1C38A80C54D1037E5EB5C462CEF8EABE39905CBD1C8D4D91A295824`.

## Environment

| Component | Observed version | Status |
| --- | --- | --- |
| Python test virtual environment | `3.14.6` | Meets `pyproject.toml` requirement `>=3.14,<3.15` |
| Node.js | `v24.18.0` | Available |
| npm | `11.16.0` | Available |
| Docker | `29.6.1` | Available |

## Initial Assessment

- `W:\novel\test` and `W:\novel\PlotPilot` are separate physical Git checkouts. The test checkout is an isolated normal repository, not a linked worktree and not a submodule.
- No baseline deviation, user working-tree modification, or remote divergence was observed.
- Formal workspace has not been used for source edits, dependency installation, database writes, temporary data, or tests during this audit.
