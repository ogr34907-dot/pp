-- Candidate generation owns the authoritative DAG execution trace.  The
-- trace is deliberately separate from formal chapter state so unapproved
-- prose and its analysis cannot become canonical facts.

CREATE TABLE IF NOT EXISTS candidate_dag_runs (
    id TEXT PRIMARY KEY,
    candidate_id TEXT NOT NULL,
    content_revision INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed', 'cancelled')),
    current_node_id TEXT NOT NULL DEFAULT '',
    final_state_json TEXT NOT NULL DEFAULT '{}',
    failure_reason TEXT NOT NULL DEFAULT '',
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (candidate_id) REFERENCES chapter_candidates(id) ON DELETE CASCADE
);

CREATE INDEX IF NOT EXISTS idx_candidate_dag_runs_candidate_revision
ON candidate_dag_runs(candidate_id, content_revision, started_at DESC);

CREATE TABLE IF NOT EXISTS candidate_dag_node_attempts (
    id TEXT PRIMARY KEY,
    dag_run_id TEXT NOT NULL,
    node_id TEXT NOT NULL,
    node_type TEXT NOT NULL DEFAULT '',
    attempt INTEGER NOT NULL DEFAULT 1,
    status TEXT NOT NULL CHECK(status IN ('running', 'completed', 'failed', 'skipped')),
    duration_ms INTEGER NOT NULL DEFAULT 0,
    outputs_json TEXT NOT NULL DEFAULT '{}',
    error TEXT NOT NULL DEFAULT '',
    started_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    completed_at TIMESTAMP,
    FOREIGN KEY (dag_run_id) REFERENCES candidate_dag_runs(id) ON DELETE CASCADE,
    UNIQUE(dag_run_id, node_id, attempt)
);

CREATE TABLE IF NOT EXISTS candidate_dag_events (
    id TEXT PRIMARY KEY,
    dag_run_id TEXT NOT NULL,
    sequence INTEGER NOT NULL,
    event_json TEXT NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (dag_run_id) REFERENCES candidate_dag_runs(id) ON DELETE CASCADE,
    UNIQUE(dag_run_id, sequence)
);

CREATE INDEX IF NOT EXISTS idx_candidate_dag_events_resume
ON candidate_dag_events(dag_run_id, sequence);
