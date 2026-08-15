-- Reassert the cutover invariants with new trigger names so databases that
-- already recorded 031/032/033 receive the same policy on upgrade. The
-- triggers are deliberately additive and do not introduce mutable bypass
-- flags or deletion-scope tables.
CREATE TRIGGER IF NOT EXISTS trg_outline_planning_heads_manifest_to_legacy_v2
BEFORE UPDATE OF authority_mode ON outline_planning_heads
WHEN OLD.authority_mode = 'manifest'
 AND NEW.authority_mode = 'legacy'
 AND EXISTS (SELECT 1 FROM novels WHERE id = OLD.novel_id)
BEGIN
    SELECT RAISE(ABORT, 'manifest planning Head cannot return to legacy');
END;

CREATE TRIGGER IF NOT EXISTS trg_outline_planning_heads_publishable_insert_v2
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

CREATE TRIGGER IF NOT EXISTS trg_outline_planning_heads_publishable_update_v2
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

-- A sealed revision is not a usable Head unless its immutable item set is
-- complete.  This is the SQL backstop for direct callers; the repository
-- performs the same checks with richer payload validation before sealing.
CREATE TRIGGER IF NOT EXISTS trg_outline_planning_heads_topology_insert_v2
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
      AND EXISTS (
          SELECT 1
          FROM outline_plan_revision_items AS item
          WHERE item.plan_revision_id = revision.id
      )
      AND (
          SELECT COUNT(*)
          FROM outline_plan_revision_items AS root_item
          WHERE root_item.plan_revision_id = revision.id
            AND root_item.level = 'outline'
            AND root_item.parent_logical_node_id IS NULL
      ) = 1
      AND NOT EXISTS (
          SELECT 1
          FROM outline_plan_revision_items AS bad_root
          WHERE bad_root.plan_revision_id = revision.id
            AND bad_root.level = 'outline'
            AND bad_root.parent_logical_node_id IS NOT NULL
      )
      AND NOT EXISTS (
          SELECT 1
          FROM outline_plan_revision_items AS item
          LEFT JOIN outline_contracts AS contract
            ON contract.id = item.logical_node_id
           AND contract.novel_id = revision.novel_id
          LEFT JOIN outline_contract_versions AS version
            ON version.id = item.version_id
           AND version.contract_id = item.logical_node_id
          WHERE item.plan_revision_id = revision.id
            AND (
                contract.id IS NULL
                OR version.id IS NULL
                OR version.sealed_at IS NULL
            )
      )
      AND NOT EXISTS (
          SELECT 1
          FROM outline_plan_revision_items AS child
          LEFT JOIN outline_plan_revision_items AS parent
            ON parent.plan_revision_id = child.plan_revision_id
           AND parent.logical_node_id = child.parent_logical_node_id
          LEFT JOIN outline_contract_versions AS parent_version
            ON parent_version.id = parent.version_id
          WHERE child.plan_revision_id = revision.id
            AND (
                (child.level <> 'outline' AND parent.id IS NULL)
                OR (
                    child.level <> 'outline'
                    AND child.validated_parent_digest <> COALESCE(parent_version.digest, '')
                )
            )
      )
      AND NOT EXISTS (
          SELECT 1
          FROM outline_plan_revision_items AS child
          LEFT JOIN outline_plan_revision_items AS previous
            ON previous.plan_revision_id = child.plan_revision_id
           AND previous.parent_logical_node_id IS child.parent_logical_node_id
           AND previous.level = child.level
           AND previous.sibling_index = child.sibling_index - 1
          LEFT JOIN outline_contract_versions AS previous_version
            ON previous_version.id = previous.version_id
          WHERE child.plan_revision_id = revision.id
            AND child.validated_previous_sibling_digest <> COALESCE(previous_version.digest, '')
      )
 )
BEGIN
    SELECT RAISE(ABORT, 'active outline plan topology is incomplete');
END;

CREATE TRIGGER IF NOT EXISTS trg_outline_planning_heads_topology_update_v2
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
      AND EXISTS (
          SELECT 1
          FROM outline_plan_revision_items AS item
          WHERE item.plan_revision_id = revision.id
      )
      AND (
          SELECT COUNT(*)
          FROM outline_plan_revision_items AS root_item
          WHERE root_item.plan_revision_id = revision.id
            AND root_item.level = 'outline'
            AND root_item.parent_logical_node_id IS NULL
      ) = 1
      AND NOT EXISTS (
          SELECT 1
          FROM outline_plan_revision_items AS bad_root
          WHERE bad_root.plan_revision_id = revision.id
            AND bad_root.level = 'outline'
            AND bad_root.parent_logical_node_id IS NOT NULL
      )
      AND NOT EXISTS (
          SELECT 1
          FROM outline_plan_revision_items AS item
          LEFT JOIN outline_contracts AS contract
            ON contract.id = item.logical_node_id
           AND contract.novel_id = revision.novel_id
          LEFT JOIN outline_contract_versions AS version
            ON version.id = item.version_id
           AND version.contract_id = item.logical_node_id
          WHERE item.plan_revision_id = revision.id
            AND (
                contract.id IS NULL
                OR version.id IS NULL
                OR version.sealed_at IS NULL
            )
      )
      AND NOT EXISTS (
          SELECT 1
          FROM outline_plan_revision_items AS child
          LEFT JOIN outline_plan_revision_items AS parent
            ON parent.plan_revision_id = child.plan_revision_id
           AND parent.logical_node_id = child.parent_logical_node_id
          LEFT JOIN outline_contract_versions AS parent_version
            ON parent_version.id = parent.version_id
          WHERE child.plan_revision_id = revision.id
            AND (
                (child.level <> 'outline' AND parent.id IS NULL)
                OR (
                    child.level <> 'outline'
                    AND child.validated_parent_digest <> COALESCE(parent_version.digest, '')
                )
            )
      )
      AND NOT EXISTS (
          SELECT 1
          FROM outline_plan_revision_items AS child
          LEFT JOIN outline_plan_revision_items AS previous
            ON previous.plan_revision_id = child.plan_revision_id
           AND previous.parent_logical_node_id IS child.parent_logical_node_id
           AND previous.level = child.level
           AND previous.sibling_index = child.sibling_index - 1
          LEFT JOIN outline_contract_versions AS previous_version
            ON previous_version.id = previous.version_id
          WHERE child.plan_revision_id = revision.id
            AND child.validated_previous_sibling_digest <> COALESCE(previous_version.digest, '')
      )
 )
BEGIN
    SELECT RAISE(ABORT, 'active outline plan topology is incomplete');
END;

CREATE TRIGGER IF NOT EXISTS trg_outline_contract_versions_sealed_global_update_v2
BEFORE UPDATE ON outline_contract_versions
WHEN OLD.sealed_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'sealed outline contract version is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_outline_contract_versions_sealed_global_delete_v2
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

-- Revalidate every existing manifest Head during upgrade.  The no-op update
-- deliberately exercises the same publishability and topology triggers, so
-- a malformed pre-035 Head aborts the migration instead of remaining trusted.
UPDATE outline_planning_heads
SET active_plan_digest = active_plan_digest
WHERE authority_mode = 'manifest';
