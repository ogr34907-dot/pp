-- Keep the source-lineage digest beside the immutable Manifest Head evidence.
-- Existing 040-era archives receive an empty value and are intentionally
-- restore-incompatible rather than trusting mutable archive metadata.
ALTER TABLE worldline_manifest_head_snapshots
ADD COLUMN source_lineage_digest TEXT NOT NULL DEFAULT '';

CREATE TRIGGER IF NOT EXISTS trg_worldline_manifest_head_snapshot_lineage_required
BEFORE INSERT ON worldline_manifest_head_snapshots
WHEN NEW.source_lineage_digest IS NULL OR NEW.source_lineage_digest = ''
BEGIN
    SELECT RAISE(ABORT, 'worldline Manifest Head snapshot is immutable; source lineage evidence is required');
END;
