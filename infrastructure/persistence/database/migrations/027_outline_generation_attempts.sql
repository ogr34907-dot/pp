-- A streamed outline is an attempt, not an outline revision.  Only a
-- validated completed attempt can create a normal draft revision.

ALTER TABLE outline_contract_versions
ADD COLUMN previous_sibling_digest TEXT NOT NULL DEFAULT '';

CREATE TABLE IF NOT EXISTS outline_generation_attempts (
    id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed', 'cancelled')),
    retry_of_attempt_id TEXT,
    context_digest TEXT NOT NULL DEFAULT '',
    prompt_snapshot_json TEXT NOT NULL DEFAULT '{}',
    accumulated_text TEXT NOT NULL DEFAULT '',
    draft_revision INTEGER,
    error TEXT NOT NULL DEFAULT '',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (contract_id) REFERENCES outline_contracts(id) ON DELETE CASCADE,
    FOREIGN KEY (retry_of_attempt_id) REFERENCES outline_generation_attempts(id) ON DELETE SET NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_outline_generation_attempts_one_running
ON outline_generation_attempts(contract_id)
WHERE status = 'running';

CREATE INDEX IF NOT EXISTS idx_outline_generation_attempts_contract
ON outline_generation_attempts(contract_id, created_at DESC);

CREATE TABLE IF NOT EXISTS outline_generation_attempt_events (
    id TEXT PRIMARY KEY,
    attempt_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    event_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (attempt_id) REFERENCES outline_generation_attempts(id) ON DELETE CASCADE,
    UNIQUE(attempt_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_outline_generation_attempt_events_resume
ON outline_generation_attempt_events(attempt_id, sequence);
