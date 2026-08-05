ALTER TABLE chapter_narrative_commits
ADD COLUMN memory_status TEXT NOT NULL DEFAULT 'not_required';

ALTER TABLE chapter_narrative_commits
ADD COLUMN memory_failure_reason TEXT NOT NULL DEFAULT '';
