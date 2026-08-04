CREATE TABLE IF NOT EXISTS chapter_narrative_commits (
    novel_id TEXT NOT NULL,
    chapter_number INTEGER NOT NULL,
    content_sha256 TEXT NOT NULL,
    pipeline_version TEXT NOT NULL,
    content_revision INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'in_progress',
    failure_reason TEXT NOT NULL DEFAULT '',
    attempt_count INTEGER NOT NULL DEFAULT 1,
    vector_status TEXT NOT NULL DEFAULT 'not_started',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    committed_at TIMESTAMP,
    PRIMARY KEY (novel_id, chapter_number, content_sha256, pipeline_version),
    FOREIGN KEY (novel_id, chapter_number)
        REFERENCES chapters(novel_id, number) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_chapter_narrative_commits_chapter
ON chapter_narrative_commits(novel_id, chapter_number, content_revision);
