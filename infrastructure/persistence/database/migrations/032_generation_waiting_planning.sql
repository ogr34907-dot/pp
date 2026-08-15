-- Rolling outline expansion is a normal persisted pause, not a failed run.
-- Rebuild the small run table because SQLite cannot alter a CHECK constraint.
CREATE TABLE novel_generation_runs_waiting_planning (
    novel_id TEXT PRIMARY KEY,
    run_mode TEXT NOT NULL DEFAULT 'continuous'
      CHECK(run_mode IN ('continuous', 'chapter_review')),
    state TEXT NOT NULL DEFAULT 'idle'
      CHECK(state IN (
        'idle', 'running', 'waiting_review', 'waiting_planning', 'paused',
        'stopped', 'completed', 'error'
      )),
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

INSERT INTO novel_generation_runs_waiting_planning
    (novel_id, run_mode, state, generation_epoch, target_chapters,
     current_formal_chapter, current_candidate_id, current_candidate_chapter,
     canonical_sync_status, next_action, last_error, max_pending_candidates,
     prefetch, updated_at, created_at)
SELECT novel_id, run_mode, state, generation_epoch, target_chapters,
       current_formal_chapter, current_candidate_id, current_candidate_chapter,
       canonical_sync_status, next_action, last_error, max_pending_candidates,
       prefetch, updated_at, created_at
FROM novel_generation_runs;

DROP TABLE novel_generation_runs;
ALTER TABLE novel_generation_runs_waiting_planning RENAME TO novel_generation_runs;
