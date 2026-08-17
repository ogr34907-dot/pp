-- Distinguish canonical-sync retries that otherwise return to the same
-- Candidate/Formal/Run state tuple after a prior failure.
ALTER TABLE chapter_candidate_formal_commits
ADD COLUMN sync_attempt INTEGER NOT NULL DEFAULT 0;
