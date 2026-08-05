# Blueprint, Volume, Act, and Chapter Structure Audit

## `BLUEPRINT-001`: safe merge fallback is unsafe

The dynamic blueprint probe forced `confirm_macro_plan_safe()` to reject a
merge. The service made one safe-merge call and then invoked the legacy unsafe
writer once. That writer reported success and created three nodes.

```json
{
  "safe_merge_calls": 1,
  "unsafe_fallback_calls": 1,
  "fallback_result": {"success": true, "created_nodes": 3,
                      "message": "unsafe write used"}
}
```

The fallback sits in
`application/blueprint/services/continuous_planning_service.py`. A safe merge
rejection must stop and preserve the structure, not invoke a separate writer
with weaker guarantees.

## `DATA-001`: act confirmation can delete authored prose

`confirm_act_planning()` removes existing chapter children and calls
`purge_chapter_book_rows_not_matching_structure()`. The dynamic probe created
a chapter with non-empty body text and an act plan which no longer included
it. The result removed that chapter without requesting a snapshot,
confirmation, or rewrite mode.

```json
{
  "authored_prose_present": true,
  "confirmation_or_snapshot_required": false,
  "deleted_chapter_ids": ["chapter-audit-1"],
  "removed_count": 1
}
```

This is P0. The narrow repair is a pre-delete fail-closed scan for authored
content in both the act confirmation path and the structure sync helper. It
does not claim to provide nonexistent prose branching.

## `BLUEPRINT-002`: direct API act creation bypasses daemon guards

The daemon delegate already applies capacity and parent/child constraints.
The service behind `POST /acts/{act_id}/create-next` does not. The probe
created act number 4 directly under an already constrained volume without
consulting capacity data:

```json
{
  "capacity_checked_by_method": false,
  "created_act_number": 4,
  "created_act_parent": "volume-1",
  "direct_create_result": {"success": true}
}
```

The service must share the daemon's preflight rules: target chapters, volume
capacity, valid parent, and duplicate numbering. This is P1 because it causes
invalid future structure rather than immediately deleting existing prose.
