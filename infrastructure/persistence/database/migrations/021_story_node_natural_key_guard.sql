-- Preserve existing duplicate rows for compatibility, but forbid new collisions.
CREATE TRIGGER IF NOT EXISTS trg_story_nodes_natural_key_insert
BEFORE INSERT ON story_nodes
WHEN EXISTS (
    SELECT 1
    FROM story_nodes AS existing
    WHERE existing.id <> NEW.id
      AND existing.novel_id = NEW.novel_id
      AND COALESCE(existing.parent_id, '') = COALESCE(NEW.parent_id, '')
      AND existing.node_type = NEW.node_type
      AND existing.number = NEW.number
)
BEGIN
    SELECT RAISE(ABORT, 'story_nodes natural key conflict');
END;

CREATE TRIGGER IF NOT EXISTS trg_story_nodes_natural_key_update
BEFORE UPDATE OF novel_id, parent_id, node_type, number ON story_nodes
WHEN (
    NEW.novel_id IS NOT OLD.novel_id
    OR COALESCE(NEW.parent_id, '') IS NOT COALESCE(OLD.parent_id, '')
    OR NEW.node_type IS NOT OLD.node_type
    OR NEW.number IS NOT OLD.number
)
AND EXISTS (
    SELECT 1
    FROM story_nodes AS existing
    WHERE existing.id <> NEW.id
      AND existing.novel_id = NEW.novel_id
      AND COALESCE(existing.parent_id, '') = COALESCE(NEW.parent_id, '')
      AND existing.node_type = NEW.node_type
      AND existing.number = NEW.number
)
BEGIN
    SELECT RAISE(ABORT, 'story_nodes natural key conflict');
END;
