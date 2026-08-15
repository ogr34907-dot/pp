-- Explicit author-approved baseline for pre-Candidate completed chapters.
-- These rows preserve provenance without fabricating candidate/audit/commit records.
CREATE TABLE IF NOT EXISTS pre_candidate_formal_history (
    novel_id TEXT NOT NULL,
    chapter_number INTEGER NOT NULL,
    chapter_id TEXT NOT NULL,
    content_sha256 TEXT NOT NULL,
    content_revision INTEGER NOT NULL,
    imported_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (novel_id, chapter_number),
    UNIQUE (chapter_id),
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE,
    FOREIGN KEY (chapter_id) REFERENCES chapters(id) ON DELETE RESTRICT,
    FOREIGN KEY (novel_id, chapter_number) REFERENCES chapters(novel_id, number) ON DELETE RESTRICT
);
