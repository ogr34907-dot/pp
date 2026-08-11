-- Read-only archive for a retired chapter tail.  Archive entries contain the
-- exact pre-reset rows so a branch can be restored without polluting active
-- facts, projections or retrieval.

CREATE TABLE IF NOT EXISTS worldline_archives (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL,
    old_generation_epoch INTEGER NOT NULL,
    start_chapter INTEGER NOT NULL,
    end_chapter INTEGER NOT NULL,
    retained_through INTEGER NOT NULL,
    target_chapters INTEGER NOT NULL,
    status TEXT NOT NULL DEFAULT 'archived'
      CHECK(status IN ('archiving', 'archived', 'restoring', 'restored', 'failed')),
    prefix_digest TEXT NOT NULL DEFAULT '',
    metadata_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    restored_at TIMESTAMP,
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE
);
CREATE INDEX IF NOT EXISTS idx_worldline_archives_novel ON worldline_archives(novel_id, created_at DESC);

CREATE TABLE IF NOT EXISTS worldline_archive_entries (
    id TEXT PRIMARY KEY,
    archive_id TEXT NOT NULL,
    source_table TEXT NOT NULL,
    source_key TEXT NOT NULL,
    chapter_number INTEGER,
    payload_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (archive_id) REFERENCES worldline_archives(id) ON DELETE CASCADE,
    UNIQUE(archive_id, source_table, source_key)
);
CREATE INDEX IF NOT EXISTS idx_worldline_archive_entries_lookup
ON worldline_archive_entries(archive_id, source_table, chapter_number);

CREATE TABLE IF NOT EXISTS worldline_regeneration_previews (
    token TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL,
    start_chapter INTEGER NOT NULL,
    target_chapters INTEGER NOT NULL,
    current_generated_chapters INTEGER NOT NULL,
    retained_through INTEGER NOT NULL,
    operation TEXT NOT NULL CHECK(operation IN ('regenerate', 'continue')),
    generation_epoch INTEGER NOT NULL,
    prefix_digest TEXT NOT NULL DEFAULT '',
    payload_json TEXT NOT NULL DEFAULT '{}',
    consumed_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE
);

CREATE TABLE IF NOT EXISTS worldline_regeneration_operations (
    novel_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    operation TEXT NOT NULL,
    archive_id TEXT,
    result_json TEXT NOT NULL DEFAULT '{}',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY(novel_id, idempotency_key, operation),
    FOREIGN KEY (archive_id) REFERENCES worldline_archives(id) ON DELETE SET NULL
);

CREATE TABLE IF NOT EXISTS worldline_rebuild_jobs (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL,
    generation_epoch INTEGER NOT NULL,
    archive_id TEXT,
    job_type TEXT NOT NULL CHECK(job_type IN ('canonical_facts', 'memory', 'vectors', 'foreshadowing', 'macro_summaries')),
    status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending', 'running', 'completed', 'failed')),
    failure_reason TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE,
    FOREIGN KEY (archive_id) REFERENCES worldline_archives(id) ON DELETE SET NULL,
    UNIQUE(novel_id, generation_epoch, job_type)
);

-- Retrieval layers can read this barrier before physical vector cleanup has
-- completed.  New writes/rebuilds tag their epoch; retired epochs are blocked.
CREATE TABLE IF NOT EXISTS worldline_generation_filters (
    novel_id TEXT PRIMARY KEY,
    active_generation_epoch INTEGER NOT NULL DEFAULT 0,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE
);
