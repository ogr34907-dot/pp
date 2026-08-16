-- A sealed manifest needs its own immutable physical projection provenance.
-- StoryNode references deliberately have no FK because archived Worldlines may
-- remove live nodes while their historical bindings remain inspectable.

CREATE TABLE IF NOT EXISTS outline_plan_projection_bindings (
    plan_revision_item_id TEXT PRIMARY KEY
      REFERENCES outline_plan_revision_items(id) ON DELETE CASCADE,
    story_node_id TEXT,
    parent_story_node_id TEXT,
    number INTEGER,
    order_index INTEGER,
    CHECK (
        (story_node_id IS NULL
         AND parent_story_node_id IS NULL
         AND number IS NULL
         AND order_index IS NULL)
        OR
        (story_node_id IS NOT NULL
         AND number IS NOT NULL
         AND order_index IS NOT NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_outline_plan_projection_bindings_story_node
ON outline_plan_projection_bindings(story_node_id)
WHERE story_node_id IS NOT NULL;

-- Freeze only the plan currently referenced by a Head. Historical plans which
-- were never authoritative remain unbound and therefore fail closed.
INSERT INTO outline_plan_projection_bindings
    (plan_revision_item_id, story_node_id, parent_story_node_id, number, order_index)
SELECT item.id,
       CASE WHEN item.level = 'outline' THEN NULL ELSE node.id END,
       CASE WHEN item.level = 'outline' THEN NULL ELSE node.parent_id END,
       CASE WHEN item.level = 'outline' THEN NULL ELSE node.number END,
       CASE WHEN item.level = 'outline' THEN NULL ELSE node.order_index END
FROM outline_planning_heads AS head
JOIN outline_plan_revision_items AS item
  ON item.plan_revision_id = head.active_plan_revision_id
JOIN outline_contracts AS contract
  ON contract.id = item.logical_node_id
 AND contract.novel_id = head.novel_id
LEFT JOIN story_nodes AS node ON node.id = contract.story_node_id
WHERE head.active_plan_revision_id IS NOT NULL
  AND (
      item.level = 'outline'
      OR node.id IS NOT NULL
      OR item.expansion_state = 'unexpanded'
  );

CREATE TRIGGER IF NOT EXISTS trg_outline_plan_projection_bindings_duplicate_insert
BEFORE INSERT ON outline_plan_projection_bindings
WHEN NEW.story_node_id IS NOT NULL
 AND EXISTS (
    SELECT 1
    FROM outline_plan_projection_bindings AS existing_binding
    JOIN outline_plan_revision_items AS existing_item
      ON existing_item.id = existing_binding.plan_revision_item_id
    JOIN outline_plan_revision_items AS new_item
      ON new_item.id = NEW.plan_revision_item_id
    WHERE existing_item.plan_revision_id = new_item.plan_revision_id
      AND existing_binding.story_node_id = NEW.story_node_id
 )
BEGIN
    SELECT RAISE(ABORT, 'outline plan projection binding duplicates a physical StoryNode');
END;

CREATE TRIGGER IF NOT EXISTS trg_outline_plan_projection_bindings_duplicate_update
BEFORE UPDATE OF plan_revision_item_id, story_node_id ON outline_plan_projection_bindings
WHEN NEW.story_node_id IS NOT NULL
 AND EXISTS (
    SELECT 1
    FROM outline_plan_projection_bindings AS existing_binding
    JOIN outline_plan_revision_items AS existing_item
      ON existing_item.id = existing_binding.plan_revision_item_id
    JOIN outline_plan_revision_items AS new_item
      ON new_item.id = NEW.plan_revision_item_id
    WHERE existing_binding.plan_revision_item_id <> OLD.plan_revision_item_id
      AND existing_item.plan_revision_id = new_item.plan_revision_id
      AND existing_binding.story_node_id = NEW.story_node_id
 )
BEGIN
    SELECT RAISE(ABORT, 'outline plan projection binding duplicates a physical StoryNode');
END;

CREATE TRIGGER IF NOT EXISTS trg_outline_plan_projection_bindings_sealed_insert
BEFORE INSERT ON outline_plan_projection_bindings
WHEN EXISTS (
    SELECT 1
    FROM outline_plan_revision_items AS item
    JOIN outline_plan_revisions AS revision ON revision.id = item.plan_revision_id
    WHERE item.id = NEW.plan_revision_item_id
      AND revision.sealed_at IS NOT NULL
)
BEGIN
    SELECT RAISE(ABORT, 'sealed outline plan projection bindings are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_outline_plan_projection_bindings_sealed_update
BEFORE UPDATE ON outline_plan_projection_bindings
WHEN EXISTS (
    SELECT 1
    FROM outline_plan_revision_items AS item
    JOIN outline_plan_revisions AS revision ON revision.id = item.plan_revision_id
    WHERE item.id = OLD.plan_revision_item_id
      AND revision.sealed_at IS NOT NULL
)
 OR EXISTS (
    SELECT 1
    FROM outline_plan_revision_items AS item
    JOIN outline_plan_revisions AS revision ON revision.id = item.plan_revision_id
    WHERE item.id = NEW.plan_revision_item_id
      AND revision.sealed_at IS NOT NULL
)
BEGIN
    SELECT RAISE(ABORT, 'sealed outline plan projection bindings are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_outline_plan_projection_bindings_sealed_delete
BEFORE DELETE ON outline_plan_projection_bindings
WHEN EXISTS (
    SELECT 1
    FROM outline_plan_revision_items AS item
    JOIN outline_plan_revisions AS revision ON revision.id = item.plan_revision_id
    JOIN novels AS novel ON novel.id = revision.novel_id
    WHERE item.id = OLD.plan_revision_item_id
      AND revision.sealed_at IS NOT NULL
)
BEGIN
    SELECT RAISE(ABORT, 'sealed outline plan projection bindings are immutable');
END;

-- Migration-time validation fails closed rather than blessing a Head whose
-- current mutable cache cannot be frozen into a complete physical snapshot.
CREATE TRIGGER trg_outline_plan_projection_bindings_migration_validate_head
BEFORE UPDATE OF active_plan_revision_id, active_plan_digest ON outline_planning_heads
WHEN NEW.active_plan_revision_id IS NOT NULL
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

UPDATE outline_planning_heads
SET active_plan_revision_id = active_plan_revision_id,
    active_plan_digest = active_plan_digest
WHERE active_plan_revision_id IS NOT NULL;

DROP TRIGGER trg_outline_plan_projection_bindings_migration_validate_head;
