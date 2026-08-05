# Autopilot State-Machine and Restart Audit

## Observed lifecycle behavior

`BackendLifecycle.startup()` invokes a reset path for novels left with
`autopilot_status = 'running'`. The dynamic probe created a running record and
then exercised startup. The persisted row became `stopped`, while its stage
and transient run metadata remained visible:

```json
{
  "autopilot_status": "stopped",
  "current_stage": "writing",
  "autopilot_run_epoch": 41,
  "active_pipeline_step": "compose",
  "active_pipeline_run_id": "run-41",
  "last_stable_stage": "writing"
}
```

The state is therefore not silently left as running after a process restart.
However, the `novels` table has no persistent recovery-reason field. A user
cannot distinguish a manual stop from a server-interruption stop after reload.
This is `AUTOPILOT-001`.

## Aggregate/repository persistence defect

`Novel` owns `autopilot_run_epoch`, `active_pipeline_step`,
`active_pipeline_run_id`, and `last_stable_stage`. A save/read dynamic probe
showed all four becoming their defaults after `SqliteNovelRepository.save()`:

```json
{
  "active_pipeline_run_id": "",
  "active_pipeline_step": "",
  "autopilot_run_epoch": 0,
  "last_stable_stage": ""
}
```

This is `DB-002`, a P1 persistence defect. The repair must add these columns
to the repository's INSERT, UPSERT, parameter tuple, and read projection. It
must not change canonical aftermath gating or invent a second recovery state
machine.

## Target contract

On service restart, a running novel becomes stopped with the durable reason
`service_restart_interrupted`. A deliberate user start clears that reason.
The API's existing status projection should expose it. A manual pause remains
distinct from recovery interruption.
