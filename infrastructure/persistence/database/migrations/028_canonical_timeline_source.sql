-- Keep chapter-aftermath timeline rows distinguishable from authored Bible notes.
-- Existing rows default to the neutral authored value; no historical facts are
-- inferred or backfilled for existing novels.
ALTER TABLE bible_timeline_notes ADD COLUMN source_type TEXT NOT NULL DEFAULT 'bible';
ALTER TABLE bible_timeline_notes ADD COLUMN chapter_number INTEGER;
