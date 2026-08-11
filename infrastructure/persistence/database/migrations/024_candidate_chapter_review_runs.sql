-- Candidate chapters are deliberately separate from chapters: unreviewed prose
-- must not affect canonical facts, memory, vectors or the next prompt.

CREATE TABLE IF NOT EXISTS novel_generation_runs (
    novel_id TEXT PRIMARY KEY,
    run_mode TEXT NOT NULL DEFAULT 'continuous'
      CHECK(run_mode IN ('continuous', 'chapter_review')),
    state TEXT NOT NULL DEFAULT 'idle'
      CHECK(state IN ('idle', 'running', 'waiting_review', 'paused', 'stopped', 'completed', 'error')),
    generation_epoch INTEGER NOT NULL DEFAULT 0,
    target_chapters INTEGER NOT NULL DEFAULT 0,
    current_formal_chapter INTEGER NOT NULL DEFAULT 0,
    current_candidate_id TEXT,
    current_candidate_chapter INTEGER,
    canonical_sync_status TEXT NOT NULL DEFAULT 'ready',
    next_action TEXT NOT NULL DEFAULT '',
    last_error TEXT NOT NULL DEFAULT '',
    max_pending_candidates INTEGER NOT NULL DEFAULT 1,
    prefetch INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS chapter_candidates (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL,
    chapter_number INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    generation_epoch INTEGER NOT NULL,
    status TEXT NOT NULL
      CHECK(status IN (
        'streaming', 'auditing', 'awaiting_review', 'committing', 'syncing', 'committed',
        'stale', 'regenerating', 'rejected', 'failed', 'cancelled'
      )),
    outline_chain_json TEXT NOT NULL DEFAULT '{}',
    outline_chain_digest TEXT NOT NULL DEFAULT '',
    llm_content TEXT NOT NULL DEFAULT '',
    author_content TEXT,
    content_revision INTEGER NOT NULL DEFAULT 0,
    audit_revision INTEGER NOT NULL DEFAULT 0,
    commit_plan_revision INTEGER NOT NULL DEFAULT 0,
    commit_plan_content_revision INTEGER NOT NULL DEFAULT 0,
    audit_json TEXT NOT NULL DEFAULT '{}',
    commit_plan_json TEXT NOT NULL DEFAULT '{}',
    feedback TEXT NOT NULL DEFAULT '',
    failure_reason TEXT NOT NULL DEFAULT '',
    continue_after_commit INTEGER NOT NULL DEFAULT 0,
    formal_chapter_id TEXT,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE
);

-- A novel may never have two token-consuming/open candidate workflows.
CREATE UNIQUE INDEX IF NOT EXISTS ux_chapter_candidates_one_open_per_novel
ON chapter_candidates(novel_id)
WHERE status IN ('streaming', 'auditing', 'awaiting_review', 'committing', 'syncing', 'regenerating');

CREATE INDEX IF NOT EXISTS idx_chapter_candidates_novel_epoch
ON chapter_candidates(novel_id, generation_epoch, chapter_number);

CREATE TABLE IF NOT EXISTS chapter_candidate_versions (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    content_revision INTEGER NOT NULL,
    content TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL CHECK(source IN ('llm', 'author', 'regenerated')),
    feedback TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (candidate_id) REFERENCES chapter_candidates(id) ON DELETE CASCADE,
    UNIQUE(candidate_id, content_revision)
);

CREATE TABLE IF NOT EXISTS chapter_candidate_formal_commits (
    candidate_id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL,
    chapter_number INTEGER NOT NULL,
    chapter_id TEXT NOT NULL,
    sync_status TEXT NOT NULL DEFAULT 'syncing'
      CHECK(sync_status IN ('syncing', 'ready', 'failed')),
    failure_reason TEXT NOT NULL DEFAULT '',
    committed_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    synced_at TIMESTAMP,
    FOREIGN KEY (candidate_id) REFERENCES chapter_candidates(id) ON DELETE CASCADE,
    FOREIGN KEY (novel_id, chapter_number) REFERENCES chapters(novel_id, number) ON DELETE RESTRICT,
    UNIQUE(novel_id, chapter_number)
);

CREATE INDEX IF NOT EXISTS idx_candidate_formal_commits_sync
ON chapter_candidate_formal_commits(novel_id, sync_status);
