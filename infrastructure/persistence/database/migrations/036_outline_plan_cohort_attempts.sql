-- Manifest-native generation attempts. These rows are logs for an editable
-- PlanRevision, never an alternate planning authority or publish mechanism.

CREATE TABLE IF NOT EXISTS outline_plan_cohort_attempts (
    id TEXT PRIMARY KEY,
    plan_revision_id TEXT NOT NULL,
    parent_logical_node_id TEXT NOT NULL,
    level TEXT NOT NULL CHECK(level IN ('part', 'volume', 'act', 'chapter')),
    status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed', 'cancelled')),
    retry_of_attempt_id TEXT,
    scope_json TEXT NOT NULL DEFAULT '{}',
    context_digest TEXT NOT NULL DEFAULT '',
    prompt_snapshot_json TEXT NOT NULL DEFAULT '{}',
    accumulated_text TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (plan_revision_id) REFERENCES outline_plan_revisions(id) ON DELETE CASCADE,
    FOREIGN KEY (retry_of_attempt_id) REFERENCES outline_plan_cohort_attempts(id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_outline_plan_cohort_attempt_one_running
ON outline_plan_cohort_attempts(plan_revision_id, parent_logical_node_id, level)
WHERE status = 'running';

CREATE INDEX IF NOT EXISTS idx_outline_plan_cohort_attempts_lookup
ON outline_plan_cohort_attempts(plan_revision_id, parent_logical_node_id, level, created_at DESC);

CREATE TABLE IF NOT EXISTS outline_plan_cohort_attempt_events (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    event_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (attempt_id) REFERENCES outline_plan_cohort_attempts(id) ON DELETE CASCADE,
    UNIQUE(attempt_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_outline_plan_cohort_attempt_events_resume
ON outline_plan_cohort_attempt_events(attempt_id, sequence);
