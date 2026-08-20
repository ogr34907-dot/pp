ALTER TABLE outline_plan_revisions
ADD COLUMN narrative_review_state TEXT NOT NULL DEFAULT 'not_required'
  CHECK(narrative_review_state IN ('not_required', 'pending', 'pass', 'acknowledged'));

ALTER TABLE outline_plan_revisions
ADD COLUMN narrative_review_receipt_json TEXT NOT NULL DEFAULT '{}';

CREATE TABLE IF NOT EXISTS outline_continuity_review_runs (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL,
    plan_revision_id TEXT NOT NULL,
    scope_parent_logical_node_id TEXT NOT NULL,
    level TEXT NOT NULL,
    plan_digest TEXT NOT NULL,
    scope_fingerprint TEXT NOT NULL,
    context_digest TEXT NOT NULL DEFAULT '',
    prompt_node_version_id TEXT NOT NULL DEFAULT '',
    prompt_hash TEXT NOT NULL DEFAULT '',
    schema_version TEXT NOT NULL DEFAULT '',
    ruleset_version TEXT NOT NULL DEFAULT '',
    model TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT 'running'
      CHECK(state IN ('running', 'succeeded', 'failed', 'cancelled')),
    decision TEXT NOT NULL DEFAULT 'unavailable'
      CHECK(decision IN ('pass', 'review', 'conflict', 'unavailable')),
    confidence REAL,
    report_json TEXT NOT NULL DEFAULT '{}',
    raw_response TEXT NOT NULL DEFAULT '',
    error TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    completed_at TEXT,
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE,
    FOREIGN KEY (plan_revision_id) REFERENCES outline_plan_revisions(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_outline_continuity_review_runs_running_scope
ON outline_continuity_review_runs(scope_fingerprint)
WHERE state = 'running';

CREATE INDEX IF NOT EXISTS idx_outline_continuity_review_runs_current
ON outline_continuity_review_runs(plan_revision_id, scope_fingerprint, completed_at DESC);

CREATE TABLE IF NOT EXISTS outline_continuity_review_overrides (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL,
    plan_revision_id TEXT NOT NULL,
    plan_digest TEXT NOT NULL,
    action TEXT NOT NULL,
    idempotency_key TEXT NOT NULL DEFAULT '',
    actor TEXT NOT NULL,
    reason TEXT NOT NULL,
    scope_fingerprints_json TEXT NOT NULL DEFAULT '[]',
    review_ids_json TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE,
    FOREIGN KEY (plan_revision_id) REFERENCES outline_plan_revisions(id) ON DELETE CASCADE
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_outline_continuity_review_overrides_idempotency
ON outline_continuity_review_overrides(novel_id, action, idempotency_key)
WHERE idempotency_key <> '';
