ALTER TABLE chapter_narrative_commits
ADD COLUMN advance_status TEXT NOT NULL DEFAULT 'pending';

ALTER TABLE chapter_narrative_commits
ADD COLUMN advance_applied_at TIMESTAMP;

UPDATE chapter_narrative_commits
SET advance_status = 'applied',
    advance_applied_at = COALESCE(advance_applied_at, committed_at, updated_at, CURRENT_TIMESTAMP)
WHERE status = 'committed';
