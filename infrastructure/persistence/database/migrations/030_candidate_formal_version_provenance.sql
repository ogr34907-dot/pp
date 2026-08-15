-- Bind Candidate-first formal authority to the exact accepted chapter version.
ALTER TABLE chapter_candidate_formal_commits
ADD COLUMN content_sha256 TEXT NOT NULL DEFAULT '';

ALTER TABLE chapter_candidate_formal_commits
ADD COLUMN content_revision INTEGER NOT NULL DEFAULT 0;

ALTER TABLE chapter_candidate_formal_commits
ADD COLUMN provenance TEXT NOT NULL DEFAULT 'candidate_commit';

-- Existing Candidate-first rows inherit the chapter version present at upgrade.
-- Runtime admission recomputes the body hash and fails closed if these fields disagree.
UPDATE chapter_candidate_formal_commits
SET content_sha256 = COALESCE(
        (SELECT chapter.content_sha256
         FROM chapters AS chapter
         WHERE chapter.id = chapter_candidate_formal_commits.chapter_id),
        ''
    ),
    content_revision = COALESCE(
        (SELECT chapter.content_revision
         FROM chapters AS chapter
         WHERE chapter.id = chapter_candidate_formal_commits.chapter_id),
        0
    );
