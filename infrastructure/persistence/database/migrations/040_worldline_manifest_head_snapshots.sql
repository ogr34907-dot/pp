-- Immutable Manifest Head evidence for Worldline archive/rebase/restore.
-- Legacy archives intentionally have no row here and remain incompatible with
-- Manifest restore; a physical tail must never be restored through old SQL.

CREATE TABLE IF NOT EXISTS worldline_manifest_head_snapshots (
    archive_id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL,
    active_plan_revision_id TEXT NOT NULL,
    active_plan_digest TEXT NOT NULL,
    authority_generation INTEGER NOT NULL CHECK(authority_generation >= 0),
    projection_generation INTEGER NOT NULL CHECK(projection_generation >= 0),
    binding_digest TEXT NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (archive_id) REFERENCES worldline_archives(id) ON DELETE CASCADE,
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE,
    FOREIGN KEY (active_plan_revision_id)
      REFERENCES outline_plan_revisions(id) ON DELETE RESTRICT,
    CHECK(active_plan_digest <> ''),
    CHECK(binding_digest <> '')
);

CREATE INDEX IF NOT EXISTS idx_worldline_manifest_head_snapshots_novel
ON worldline_manifest_head_snapshots(novel_id, created_at DESC);

CREATE TRIGGER IF NOT EXISTS trg_worldline_manifest_head_snapshot_immutable_update
BEFORE UPDATE ON worldline_manifest_head_snapshots
BEGIN
    SELECT RAISE(ABORT, 'worldline Manifest Head snapshot is immutable');
END;

-- Direct deletion would let a caller replace a valid snapshot under the
-- archive key. A foreign-key cascade runs after its parent archive is gone,
-- preserving the archive lifecycle without granting direct write access.
CREATE TRIGGER IF NOT EXISTS trg_worldline_manifest_head_snapshot_immutable_delete
BEFORE DELETE ON worldline_manifest_head_snapshots
WHEN EXISTS (
    SELECT 1 FROM worldline_archives WHERE id = OLD.archive_id
)
BEGIN
    SELECT RAISE(ABORT, 'worldline Manifest Head snapshot is immutable');
END;

-- SQLite's REPLACE path can remove a row without using the UPDATE trigger.
CREATE TRIGGER IF NOT EXISTS trg_worldline_manifest_head_snapshot_immutable_insert
BEFORE INSERT ON worldline_manifest_head_snapshots
WHEN EXISTS (
    SELECT 1 FROM worldline_manifest_head_snapshots
    WHERE archive_id = NEW.archive_id
)
BEGIN
    SELECT RAISE(ABORT, 'worldline Manifest Head snapshot is immutable');
END;
