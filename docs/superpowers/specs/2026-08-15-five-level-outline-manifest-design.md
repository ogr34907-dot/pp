# PlotPilot Five-Level Outline Manifest Design

## Baseline And Goal

- Baseline branch: `codex/plotpilot-memory-stability`
- Starting HEAD: `149e3207d83bb4abff20ec3b0a5886dd3ddd81bd`
- Worktree at design approval: clean
- Implementation workspace: `W:\novel\test`

Build one durable five-level planning authority for long-form novels:

```text
outline -> part -> volume -> act -> chapter -> Candidate prose
```

The system must plan complete sibling cohorts, preserve author text, support
future-only replanning, reconcile plans with formal history, and continue to
use the existing Candidate-first, Canonical Aftermath, Memory Barrier,
Advance Exactly Once, and Worldline workflows.

## Scope

This change replaces planning authority and its compatibility boundaries. It
does not replace Candidate, Formal, Canonical, Memory, Vector, or Worldline
authorities.

In scope:

- book-level immutable plan manifests and a single active head;
- complete sibling cohort generation, validation, review, and publication;
- field-level author provenance and locks;
- future-only replanning with deterministic impact closure;
- exact reconciliation against current Formal/Canonical history;
- Candidate plan pins and publish/approve/Formal concurrency guards;
- StoryNode as a read-only compatibility projection;
- rolling outline expansion and the Outline Studio workflow.

Out of scope:

- changing prose-generation, review, Canonical, or Memory algorithms;
- changing prose prompts, model selection, temperature, or token budgets;
- generating all chapter outlines for a large novel at book creation;
- removing StoryNode or legacy read compatibility;
- creating parallel Canonical, Memory, Candidate, or Worldline systems;
- unrelated module or file-size refactors.

The outline-specific LLM request/response contract must change from one-node
generation to cohort planning. That is the only prompt-contract change in
scope.

## Authorities

### Planning Authority

The only planning truth is the plan revision referenced by the novel's
`outline_planning_heads` row. It must be sealed, reconciled, and have a
projection generation matching the head before prose generation can consume
it.

### Historical Truth

Formal chapter content, exact content hash/revision, Canonical narrative
commits, and current-version Memory remain historical truth. Planning data is
never written into long-term memory as an occurred fact.

### Writing Authority

A Candidate is the only pre-Formal prose authority. It pins the active plan
head and exact five-level content versions used by its LLM call. Manual and
continuous modes both remain Candidate-first.

## Data Model

### `outline_planning_heads`

One row per novel and the sole active pointer:

```text
novel_id                    PRIMARY KEY
authority_mode              legacy | manifest
authority_generation        monotonic CAS integer
active_plan_revision_id     nullable before cutover
active_plan_digest          exact digest of active manifest
working_plan_revision_id    nullable editor draft pointer only
projection_generation       generation currently projected to StoryNode
auto_publish_repairable     per-book opt-in, default false
updated_at
```

No `PlanRevision.is_active` flag exists. Head identity and plan status cannot
become two competing active-plan decisions. `working_plan_revision_id` lets
the editor resume one draft but never supplies prose Context and never implies
publication; only `active_plan_revision_id` is Planning Authority.

### `outline_plan_revisions`

Book-level logical snapshots:

```text
id
novel_id
revision
parent_plan_revision_id
status
digest
base_plan_digest
replan_start_chapter
canonical_prefix_digest
canonical_boundary_json
reconciliation_status
reconciliation_report_json
author_intent
created_by
publish_idempotency_key
created_at / sealed_at
```

Lifecycle:

```text
draft -> generating -> validating -> ready_for_review
```

`failed` and `stale` are non-publishable outcomes. Publication state is
derived from the Head and projection generation. Once a revision reaches
`ready_for_review`, its identity, items, digest, Canonical prefix, and author
intent are immutable. Retry creates a new draft or reuses an identical sealed
digest. Restore changes only the Head.

### `outline_plan_revision_items`

Historical topology and content membership:

```text
id
plan_revision_id
logical_node_id             existing outline contract identity
version_id                  immutable OutlineContractVersion
parent_logical_node_id
level                       outline | part | volume | act | chapter
sibling_index
expansion_state             unexpanded | expanded
validated_parent_digest
validated_previous_sibling_digest
is_reused
```

Required constraints:

- unique `(plan_revision_id, logical_node_id)`;
- unique sibling position within `(plan_revision_id, parent, level)`;
- `ON DELETE RESTRICT` from items to contracts and versions;
- sealed plan items cannot be updated or deleted.

Historical views, Diff, restore, and active-chain assembly use these stored
topology fields. They never infer history from a StoryNode's current parent,
number, title, or outline.

### Existing Outline Contracts

`outline_contracts` remains the stable logical node/slot registry.
`outline_contract_versions` remains immutable content storage. A sealed
version can be reused by several manifests and therefore cannot be marked
stale or superseded when one manifest is replaced.

After cutover, contract active/draft pointers, contract status, version
status, and `outline_plan_projections` are compatibility caches only. Plan
lifecycle belongs to PlanRevision/Items.

### Candidate Pins

Candidate persistence adds:

```text
planning_authority_generation
plan_revision_id
plan_digest
chapter_outline_digest
```

The existing `outline_chain_json` stores each of the five logical node IDs,
version IDs, revisions, and digests. `outline_chain_digest` covers that exact
canonical JSON.

### Field Provenance

Outline payloads add a top-level field map:

```json
{
  "field_provenance": {
    "narrative_text": {"source": "author", "author_locked": true},
    "entry_state": {"source": "ai", "author_locked": false}
  }
}
```

An AI merge may fill empty or unlocked fields. It cannot normalize, replace,
or trim locked author text. UI DTO round trips must preserve the complete
payload, including structured state and unknown compatible extension fields.

## Canonical Prefix And Reconciliation

`novel_generation_runs.generation_epoch` is a runtime cancellation token and
must not enter historical identity. There is no
`chapter_narrative_commits.narrative_commit_digest` column and none is added.

The prefix is a deterministic digest of a continuous sequence ordered by
chapter number:

- legacy Formal identity comes from `pre_candidate_formal_history`;
- Candidate-first Formal identity requires exact agreement among `chapters`
  and `chapter_candidate_formal_commits` for chapter ID, hash, and revision;
- Canonical identity uses the current matching `chapter_narrative_commits`
  stable primary-key fields plus `content_revision`;
- every chapter from 1 through the Formal head must have one proven identity.

Canonical readiness and Memory readiness are separate live gates and are not
mixed into the prefix digest. A Worldline identity uses the durable successful
lineage/archive identity, never the runtime generation epoch.

Reconciliation states:

- `aligned`: planned boundary matches actual Formal/Canonical state;
- `repairable`: only uncommitted future dependencies need replacement;
- `author_decision_required`: a hard conflict touches occurred history or a
  locked author decision.

Reconciliation is re-run before cohort creation, before the planning LLM,
before publication, before the Candidate LLM, before approval, inside Formal
commit, and at actual part/volume/act boundaries.

In manual mode, `repairable` always stops at review. Continuous mode may
auto-publish only with explicit per-book opt-in, no hard errors, no affected
author lock, no affected committing/syncing Candidate, and unchanged Head and
Canonical prefix under the publish transaction.

## Cohort Generation

One author operation generates all direct children of one parent:

1. Clone the active manifest into a draft revision.
2. Pin author intent, target chapters, target ending, and Canonical boundary.
3. Generate one sibling range/goal/handoff matrix.
4. Expand each literary synopsis in order, carrying the previous exit.
5. Review the complete cohort as a whole.
6. Run deterministic validation.
7. Present the complete cohort for author edit/review.
8. Publish the entire manifest atomically.

Generation attempts remain durable and gain `plan_revision_id` and cohort
scope. Cancellation, transport failure, or invalid output never modifies the
active plan.

Hard validation covers:

- exact, gap-free, non-overlapping parent chapter-range coverage;
- ordered sibling entry/exit compatibility and monotonic time;
- child coverage of stable parent goal IDs;
- final child convergence on the parent exit/ending direction;
- stable-ID character, relationship, task, and resource continuity;
- open thread and foreshadow carry, payoff, or explicit deferral;
- immutable Canonical prefix and author-locked fields;
- complete chapter rhythm contract for chapter outlines.

LLM literary review can add findings but cannot waive these checks.

## Impact Closure And Version Reuse

A changed node affects:

1. itself and all descendants;
2. all later siblings and their descendants;
3. all ancestor summaries up to the total outline.

Completed Canonical ranges are locked. A later item is reusable only when its
validated parent and previous-sibling dependencies, ranges, stable state IDs,
tasks, and foreshadows remain compatible. The default is stale/revalidate,
not optimistic reuse.

`ABCDEF -> ABCHJK` therefore reuses proven A/B/C versions, removes D/E/F from
the new manifest without deleting them, adds H/J/K, and creates a new total
outline version. Unexpanded lower levels are explicit state, not failure.

## Atomic Publication And Candidate Concurrency

Publication uses one `BEGIN IMMEDIATE` transaction:

1. CAS the current Head ID, digest, generation, and Canonical prefix.
2. Re-read the sealed target manifest and every expected version.
3. Re-run all hard validation and reconciliation.
4. Reject an affected Candidate already in `committing` or `syncing`.
5. Mark only affected pre-Formal Candidate/Audit rows stale.
6. Apply the compatibility StoryNode projection.
7. Update projection generation and switch the Head.
8. Record persistent idempotency and commit.

Any failure rolls back the plan, projection, Candidate states, and Head.

Candidate CAS occurs at three gates:

- LLM gate: pin before generation and revalidate when storing the result;
- approval gate: revalidate content/audit revisions and exact chain membership;
- Formal gate: revalidate inside the existing `BEGIN IMMEDIATE` commit.

A far-future plan may replace the Head without invalidating an open Candidate
when the new manifest still contains its exact pinned chain. Affected open
Candidates become stale. A Formal Candidate is never revoked and retains its
plan provenance permanently.

## Future Replan And Worldline Rewrite

Future replan requires `replan_start_chapter > Formal head`. It creates a new
manifest and may stale future Candidate/Audit rows, but cannot delete or edit
chapters, Canonical state, Memory, Vector, or Narrative Commit rows.

If the boundary overlaps Formal history, the request becomes a Worldline
rewrite. The existing workflow previews impact, requires explicit author
confirmation, archives the old tail, invalidates all old-version derived
state, and rebuilds from the retained prefix. Archives record the old Head,
plan digest, projection mapping, and durable lineage identity. A rebase plan
cannot become active and N+1 cannot run before Canonical and Memory rebuilds
are ready. Restore performs a Head CAS and never `INSERT OR REPLACE`s immutable
manifest rows.

## StoryNode Projection Cutover

StoryNode remains a compatibility/navigation projection for current modules.
The active projection preferentially reuses stable physical IDs and natural
key positions. A topology replacement may retire/rebind a slot, but historical
manifests remain independent of that binding.

`StoryNodeRepository` reads `outline_planning_heads`. In manifest mode, normal
writes fail closed for:

```text
parent_id, node_type, number, order_index, title, description, outline,
planning_status, planning_source, chapter_start/end/count,
suggested_chapter_count, themes, key_events, narrative_arc, conflicts,
pov_character_id, timeline_start/end, planning.* metadata
```

The normal runtime whitelist is limited to `word_count`, display `status`,
`runtime.*` metadata, and timestamps. `content` is not writable through
StoryNode.

Only a private `PlanProjectionWriter` can write protected fields. Its
capability is bound to one SQLite connection, novel, expected Head, plan, and
authority generation. It is valid only inside the publish or confirmed
Worldline transaction. No boolean force/skip parameter exists.

Repository guards do not cover direct SQL. Before cutover, every application
runtime DML statement touching protected StoryNode fields must be converted to
the runtime patch API, projection writer, or manifest-mode fail-closed path.
Offline migration/backup/clone bootstrap is isolated and cannot bypass a live
book's authority.

The cutover inventory includes all repository mutators plus the known callers
and direct writers in `continuous_planning_service.py`,
`story_structure_service.py`, `engine/runtime/act_planning_delegate.py`,
`application/core/services/novel_service.py`, `sqlite_chapter_repository.py`,
`autopilot_recovery_policy.py`, `chapter_rewrite_coordinator.py`,
`volume_summary_service.py`, `chapter_book_structure_sync.py`, and
`bible_service.py`. Whole-metadata replacement is prohibited because it can
overwrite `planning.*`; runtime updates patch only their namespace. Each path
has a manifest-mode fail/no-op or capability-path test before cutover.

`OutlineContractRepository` uses the same Head policy. In manifest mode its
legacy `save_draft`, `publish_and_sync`, `bind`, `invalidate`, and single-node
projection mutators fail closed or delegate to the manifest transaction.

Legacy structure queries remain. Mutations return `410` or adapt to the same
manifest/cohort use case. The old chapter/structure purge is not reachable
from normal future planning. Only a confirmed Worldline transaction can
delete a Formal tail.

## Rolling Expansion And Runtime

No next chapter outline is a normal persisted state:

```text
state = waiting_planning
next_action = expand_outline_cohort
```

The runtime does not call `fail_run`. Planning expansion can start only after
the preceding Formal, Canonical, and Memory barriers are ready. No N+1 prose
LLM call occurs until a complete five-level chain is active and synced.

## Migration And Rollback

Published migration files are never edited. A new migration adds Head,
Manifest, Item, attempt scope, Candidate pins, indexes, and immutability
constraints.

Backfill classes:

- a complete synced OutlineContract chain becomes the initial sealed manifest;
- legacy StoryNode planning becomes a draft and must reconcile with Formal;
- mixed/inconsistent books become `planning_migration_required` and cannot
  activate manifest mode automatically;
- existing chapter hashes, revisions, Candidate, Canonical, and Memory rows are
  unchanged.

Shadow verification performs no LLM calls and does not affect Context. It
compares manifest chains, ranges, digests, and StoryNode projections against
the current legacy view. Cutover is per book and atomic. One book never uses
both authorities.

Before cutover, rollback leaves the inactive new tables in place. After
cutover and before any new Candidate/Formal work, Head CAS can restore the
recorded legacy snapshot. Once new Formal history exists, rollback requires a
manifest restore or Worldline; raw table rollback is forbidden.

Database backup/export copies the new tables normally. The standalone novel
clone in `scripts/backup_novel.py` performs a logical planning copy with new
IDs and remapped foreign keys, reseals the cloned manifest against an empty
Canonical prefix, and creates a fresh Head. It does not copy attempts, open
Candidates/Audits, publish idempotency keys, or Worldline lineage. Clone and
offline migration never receive live projection capability.

Worldline archive/restore preserves immutable plan identity and uses foreign-
key order: contracts/versions, revisions, items, Candidate/attempt plan
references, projection mapping, then Head CAS last. Restore never depends on
mutable StoryNode text and cannot expose a half-restored plan.

Direct deletion of a sealed revision or item remains forbidden. Deleting the
owning novel through the existing novel-deletion transaction is the sole
exception and must cascade Head, revisions, items, attempts, and plan pins
without orphans; integration tests cover both the immutability rejection and
whole-novel cleanup.

## UI

Outline Studio provides:

- a complete sibling cohort overview;
- literary synopsis as the primary editor;
- secondary continuity, state, task, foreshadow, and constraint tabs;
- whole-cohort generation, validation, review, and publication;
- field source/lock indicators with author-text preservation;
- old/new version Diff and deterministic impact preview;
- future replan and explicit Worldline conflict choices;
- durable attempt recovery, cancellation, and reconnect.

The writing structure tree is read-only navigation showing active plan
revision, publication/sync state, stale state, parent lock, and expansion
state. SSE and shared UI state observe progress and never decide business
completion.

## Completion Gates

No cutover or delivery is allowed unless tests prove:

- exactly one planning authority per book;
- atomic cohort publication and persistent idempotency;
- immutable sealed versions and manifests;
- no direct application DML can mutate protected StoryNode fields;
- author locks are byte-preserved;
- precise Candidate invalidation and three CAS gates;
- Canonical prefix and reconciliation fail closed;
- future replan never touches historical truth;
- Worldline archive/rebase/restore isolates the old worldline;
- manual and continuous modes remain Candidate-first;
- Memory failure prevents N+1;
- Context contains Bible, active outline, Canonical state, recent chapters,
  long-term Memory, and Vector recall, while excluding draft/superseded plans,
  Candidate text, and retired worldlines;
- migration, backup, clone, crash recovery, API, frontend, and concurrency
  suites all pass.
