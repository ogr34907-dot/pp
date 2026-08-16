-- Keep a Manifest Head from becoming authoritative without a complete,
-- immutable physical projection binding snapshot.  This mirrors the 037
-- migration-time validation for every Head identity or generation update.
CREATE TRIGGER IF NOT EXISTS trg_outline_planning_heads_manifest_projection_binding_guard
BEFORE UPDATE OF authority_mode, active_plan_revision_id, active_plan_digest,
                 authority_generation, projection_generation
ON outline_planning_heads
WHEN NEW.authority_mode = 'manifest'
 AND NEW.active_plan_revision_id IS NOT NULL
BEGIN
    SELECT CASE WHEN EXISTS (
        SELECT 1
        FROM outline_plan_revision_items AS item
        LEFT JOIN outline_plan_projection_bindings AS binding
          ON binding.plan_revision_item_id = item.id
        WHERE item.plan_revision_id = NEW.active_plan_revision_id
          AND binding.plan_revision_item_id IS NULL
    ) THEN RAISE(ABORT, 'active outline plan requires complete projection bindings') END;

    SELECT CASE WHEN EXISTS (
        SELECT 1
        FROM outline_plan_revision_items AS item
        JOIN outline_plan_projection_bindings AS binding
          ON binding.plan_revision_item_id = item.id
        WHERE item.plan_revision_id = NEW.active_plan_revision_id
          AND item.level = 'outline'
          AND (
              binding.story_node_id IS NOT NULL
              OR binding.parent_story_node_id IS NOT NULL
              OR binding.number IS NOT NULL
              OR binding.order_index IS NOT NULL
          )
    ) THEN RAISE(ABORT, 'outline root projection binding must be unbound') END;

    SELECT CASE WHEN EXISTS (
        SELECT 1
        FROM outline_plan_revision_items AS item
        JOIN outline_plan_projection_bindings AS binding
          ON binding.plan_revision_item_id = item.id
        LEFT JOIN story_nodes AS node ON node.id = binding.story_node_id
        WHERE item.plan_revision_id = NEW.active_plan_revision_id
          AND item.level <> 'outline'
          AND (
              (binding.story_node_id IS NULL AND item.expansion_state <> 'unexpanded')
              OR (binding.story_node_id IS NOT NULL AND (
                  binding.number IS NULL
                  OR binding.order_index IS NULL
                  OR node.id IS NULL
                  OR node.novel_id <> NEW.novel_id
                  OR node.node_type <> item.level
                  OR COALESCE(node.parent_id, '') <> COALESCE(binding.parent_story_node_id, '')
                  OR node.number <> binding.number
                  OR node.order_index <> binding.order_index
              ))
          )
    ) THEN RAISE(ABORT, 'active outline projection binding does not match StoryNode') END;

    SELECT CASE WHEN EXISTS (
        SELECT binding.story_node_id
        FROM outline_plan_projection_bindings AS binding
        JOIN outline_plan_revision_items AS item
          ON item.id = binding.plan_revision_item_id
        WHERE item.plan_revision_id = NEW.active_plan_revision_id
          AND binding.story_node_id IS NOT NULL
        GROUP BY binding.story_node_id
        HAVING COUNT(*) > 1
    ) THEN RAISE(ABORT, 'active outline plan has duplicate projection bindings') END;

    SELECT CASE WHEN EXISTS (
        SELECT 1
        FROM outline_plan_revision_items AS child_item
        JOIN outline_plan_projection_bindings AS child_binding
          ON child_binding.plan_revision_item_id = child_item.id
        LEFT JOIN outline_plan_revision_items AS parent_item
          ON parent_item.plan_revision_id = child_item.plan_revision_id
         AND parent_item.logical_node_id = child_item.parent_logical_node_id
        LEFT JOIN outline_plan_projection_bindings AS parent_binding
          ON parent_binding.plan_revision_item_id = parent_item.id
        WHERE child_item.plan_revision_id = NEW.active_plan_revision_id
          AND child_item.level <> 'outline'
          AND child_binding.story_node_id IS NOT NULL
          AND (
              parent_item.id IS NULL
              OR COALESCE(child_binding.parent_story_node_id, '')
                 <> COALESCE(parent_binding.story_node_id, '')
              OR (child_item.level <> 'part' AND parent_binding.story_node_id IS NULL)
          )
    ) THEN RAISE(ABORT, 'active outline projection binding parent is invalid') END;
END;

-- Fail closed during upgrade if a raw SQL caller changed a Manifest Head after
-- 037 took its one-time snapshot.
UPDATE outline_planning_heads
SET active_plan_revision_id = active_plan_revision_id,
    active_plan_digest = active_plan_digest
WHERE authority_mode = 'manifest'
  AND active_plan_revision_id IS NOT NULL;
