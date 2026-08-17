-- Immutable digest of every source archive entry used by Manifest restore.
-- Archives created before this evidence exists remain restore-incompatible.
ALTER TABLE worldline_manifest_head_snapshots
ADD COLUMN source_archive_digest TEXT NOT NULL DEFAULT '';

CREATE TRIGGER IF NOT EXISTS trg_worldline_manifest_head_snapshot_archive_digest_required
BEFORE INSERT ON worldline_manifest_head_snapshots
WHEN NEW.source_archive_digest IS NULL OR NEW.source_archive_digest = ''
BEGIN
    SELECT RAISE(ABORT, 'worldline Manifest Head snapshot is immutable; archive evidence is required');
END;
