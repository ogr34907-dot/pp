# 03. Single-Codex Execution Map

## Execution discipline

All audit, test, repair, Git, and documentation actions are performed by the
current Codex in `W:\novel\test`. No subagent, parallel agent, background
agent, delegated reviewer, or delegated implementation process is used. Read-
only shell checks may be grouped, but decisions and file edits remain serial.

## Differential audit sequence

```text
Committed full-audit baseline (142052da)
  -> inherited follow-up diff inventory
  -> current E2E reproduction and evidence
  -> issue register + dependency plan
  -> test-first Mock Provider repair
  -> replay / continuation verification
  -> full backend, frontend, long-flow, and UI/API acceptance
  -> review, commit, push, remote verification
  -> one formal fast-forward pull
```

## Workspace access map

| Workspace | Permitted work in this audit | Prohibited work |
| --- | --- | --- |
| `W:\novel\test` | All source/test/doc edits, isolated services, test data, databases, logs, Git commit and push | Overwriting inherited changes; staging local runtime/sensitive artifacts |
| `W:\novel\PlotPilot` | Allowed read-only Git baseline verification; one future `git pull --ff-only` after the remote gate | Business edits, test execution, runtime data, migrations, commits, resets, cleanup, and pre-acceptance pull |

## Verification gates

1. Every new behavior begins with a focused failing regression test.
2. A narrow test must prove the fix before the isolated backend is restarted.
3. Dynamic replay must prove the exact original failure path is unblocked
   without treating a failed memory stage as success.
4. Full test/build/E2E evidence must precede staging, commit, push, and
   formal synchronization.
