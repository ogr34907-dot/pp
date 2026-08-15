-- The initial manifest migration intentionally left legacy books in shadow
-- mode.  Once a content version is reused by any sealed manifest, it is an
-- immutable historical payload regardless of the Head's current mode.
-- Parent-novel deletion remains allowed by the disappearing owner graph of
-- the real foreign-key cascade.  No mutable permission row or force flag can
-- authorize deletion of a live sealed payload.
CREATE TRIGGER IF NOT EXISTS trg_outline_contract_versions_sealed_global_update
BEFORE UPDATE ON outline_contract_versions
WHEN OLD.sealed_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'sealed outline contract version is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_outline_contract_versions_sealed_global_delete
BEFORE DELETE ON outline_contract_versions
WHEN OLD.sealed_at IS NOT NULL
 AND EXISTS (
    SELECT 1
    FROM outline_contracts AS contract
    JOIN novels AS novel ON novel.id = contract.novel_id
    WHERE contract.id = OLD.contract_id
 )
BEGIN
    SELECT RAISE(ABORT, 'sealed outline contract version is immutable');
END;

-- Cutover is deliberately one way.  Restoring an old plan is a Head switch
-- to another sealed manifest revision, never a return to mutable node-level
-- planning.
CREATE TRIGGER IF NOT EXISTS trg_outline_planning_heads_manifest_to_legacy
BEFORE UPDATE OF authority_mode ON outline_planning_heads
WHEN OLD.authority_mode = 'manifest'
 AND NEW.authority_mode = 'legacy'
 AND EXISTS (SELECT 1 FROM novels WHERE id = OLD.novel_id)
BEGIN
    SELECT RAISE(ABORT, 'manifest planning Head cannot return to legacy');
END;

-- A sealed row is historical content, not proof that it is publishable.
-- Until an explicit author-confirmed repairable state exists, only an aligned
-- ready-for-review revision may become the manifest Head.
CREATE TRIGGER IF NOT EXISTS trg_outline_planning_heads_publishable_insert
BEFORE INSERT ON outline_planning_heads
WHEN NEW.authority_mode = 'manifest'
 AND NEW.active_plan_revision_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1
    FROM outline_plan_revisions AS revision
    WHERE revision.id = NEW.active_plan_revision_id
      AND revision.novel_id = NEW.novel_id
      AND revision.sealed_at IS NOT NULL
      AND revision.status = 'ready_for_review'
      AND revision.reconciliation_status = 'aligned'
      AND revision.digest = NEW.active_plan_digest
 )
BEGIN
    SELECT RAISE(ABORT, 'active outline plan must be ready_for_review and aligned');
END;

CREATE TRIGGER IF NOT EXISTS trg_outline_planning_heads_publishable_update
BEFORE UPDATE OF authority_mode, active_plan_revision_id, active_plan_digest
ON outline_planning_heads
WHEN NEW.authority_mode = 'manifest'
 AND NEW.active_plan_revision_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1
    FROM outline_plan_revisions AS revision
    WHERE revision.id = NEW.active_plan_revision_id
      AND revision.novel_id = NEW.novel_id
      AND revision.sealed_at IS NOT NULL
      AND revision.status = 'ready_for_review'
      AND revision.reconciliation_status = 'aligned'
      AND revision.digest = NEW.active_plan_digest
 )
BEGIN
    SELECT RAISE(ABORT, 'active outline plan must be ready_for_review and aligned');
END;

-- Heads created by 031/032 predate the stronger publishability constraint.
-- Re-run every manifest Head through the new trigger inside this migration's
-- savepoint. A malformed legacy Head therefore blocks the upgrade and stays
-- visibly unresolved instead of becoming a silently trusted authority.
UPDATE outline_planning_heads
SET active_plan_revision_id = active_plan_revision_id,
    active_plan_digest = active_plan_digest
WHERE authority_mode = 'manifest';
