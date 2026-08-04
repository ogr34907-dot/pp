-- Persist replay anchors for existing unified checkpoint tables.
ALTER TABLE novel_checkpoints ADD COLUMN anchor_chapter INTEGER;
