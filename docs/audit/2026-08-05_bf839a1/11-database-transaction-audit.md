# Database, Migration, Transaction, and Write Dispatch Audit

## `DB-001`: destructive SQLite REPLACE

`StoryNodeRepository.save_batch()` issues `INSERT OR REPLACE INTO story_nodes`.
SQLite implements REPLACE as deletion followed by insertion. Because
`story_nodes.parent_id` uses `ON DELETE CASCADE`, saving an existing parent
can delete child nodes. The dynamic probe observed one child before save and
zero after save.

The repair is an `INSERT ... ON CONFLICT(id) DO UPDATE` upsert with an
explicit non-key update list. It preserves the parent row identity and avoids
the cascade. This is a narrow repository fix, not a SQLite-layer rewrite.

## `DB-001b`: structure natural keys are unprotected

The probe inserted two acts with the same novel, parent, node type, and
number. The duplicate count was two. Existing user databases might already
contain such rows, so a new global unique index could fail migration.

The compatible option is an insert/update trigger that rejects *new* natural
key collisions while allowing legacy duplicates to be edited when the natural
key is unchanged. Service errors must explain the collision.

## `DB-001c`: Write Dispatch boundary

The normal `DatabaseConnection.execute()` rejects a write when no consumer is
ready, but StoryNodeRepository methods use raw cursor execution and commits.
The probe confirmed the normal connection rejects un-routed SQL. This is a
real architectural inconsistency, but broad repository conversion would be a
high-blast-radius rewrite without a demonstrated loss on every method.

This audit schedules the P0 `save_batch` SQL correction and targeted
confirmation tests. The wider raw-cursor inventory is recorded as a residual
risk, not silently folded into the P0 patch.

## `DB-003`: migration ordering is not dependency aware

The migration runner sorts filenames lexically. On a new database,
`add_macro_diagnosis_context_patch.sql` executes before the migration which
creates `macro_diagnosis_results`, logging `no such table`. The first open
lacks `context_patch` and `total_words_at_run`; the second open supplies them.

The repair must retain published filenames and introduce a narrow, stable
dependency order for that pair. New and upgraded databases must both reach
the expected schema after one initialization.

## Schema compatibility

The baseline schema already contains chapter body hash/revision fields and
canonical summary/commit metadata. Database work in this batch must preserve
those memory-stability migrations and use temporary test databases only.
