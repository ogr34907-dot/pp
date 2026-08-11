-- Published five-level plan contracts are intentionally separate from the
-- legacy physical story_nodes hierarchy.  This adds a logical "outline" root
-- without rebuilding a production SQLite table that has many foreign keys.

CREATE TABLE IF NOT EXISTS outline_contracts (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL,
    level TEXT NOT NULL CHECK(level IN ('outline', 'part', 'volume', 'act', 'chapter')),
    story_node_id TEXT,
    parent_contract_id TEXT,
    active_version_id TEXT,
    draft_version_id TEXT,
    status TEXT NOT NULL DEFAULT 'draft'
      CHECK(status IN ('draft', 'published', 'syncing', 'synced', 'stale', 'conflict')),
    author_locked INTEGER NOT NULL DEFAULT 0,
    has_author_edits INTEGER NOT NULL DEFAULT 0,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE,
    FOREIGN KEY (parent_contract_id) REFERENCES outline_contracts(id) ON DELETE CASCADE
);

-- One logical total outline per novel.  Parts/volumes/acts/chapters can have
-- multiple siblings, so only the root gets a uniqueness rule.
CREATE UNIQUE INDEX IF NOT EXISTS ux_outline_contracts_single_root
ON outline_contracts(novel_id)
WHERE level = 'outline' AND parent_contract_id IS NULL;

-- A physical node may have one logical contract; roots deliberately have no
-- physical node because legacy part rows remain parentless in story_nodes.
CREATE UNIQUE INDEX IF NOT EXISTS ux_outline_contracts_story_node
ON outline_contracts(novel_id, story_node_id)
WHERE story_node_id IS NOT NULL;

CREATE INDEX IF NOT EXISTS idx_outline_contracts_parent
ON outline_contracts(parent_contract_id);
CREATE INDEX IF NOT EXISTS idx_outline_contracts_novel_level
ON outline_contracts(novel_id, level);

CREATE TABLE IF NOT EXISTS outline_contract_versions (
    id TEXT PRIMARY KEY,
    contract_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    payload_json TEXT NOT NULL,
    digest TEXT NOT NULL,
    parent_revision_digest TEXT DEFAULT '',
    source TEXT NOT NULL DEFAULT 'ai' CHECK(source IN ('ai', 'author', 'imported')),
    status TEXT NOT NULL DEFAULT 'draft',
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (contract_id) REFERENCES outline_contracts(id) ON DELETE CASCADE,
    UNIQUE(contract_id, revision)
);

CREATE INDEX IF NOT EXISTS idx_outline_contract_versions_contract_revision
ON outline_contract_versions(contract_id, revision DESC);

-- The active projection is the only outline data that generation/context
-- assembly may consume.  Superseded, stale and draft revisions are excluded.
CREATE TABLE IF NOT EXISTS outline_plan_projections (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL,
    contract_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    digest TEXT NOT NULL,
    payload_json TEXT NOT NULL,
    is_active INTEGER NOT NULL DEFAULT 1,
    synced_at TIMESTAMP,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE,
    FOREIGN KEY (contract_id) REFERENCES outline_contracts(id) ON DELETE CASCADE,
    FOREIGN KEY (version_id) REFERENCES outline_contract_versions(id) ON DELETE CASCADE,
    UNIQUE(contract_id, version_id)
);

CREATE INDEX IF NOT EXISTS idx_outline_plan_projections_active
ON outline_plan_projections(novel_id, is_active, contract_id);

-- Idempotency records protect publish/sync retries from switching revisions
-- twice after a transport timeout.
CREATE TABLE IF NOT EXISTS outline_operation_keys (
    novel_id TEXT NOT NULL,
    idempotency_key TEXT NOT NULL,
    operation TEXT NOT NULL,
    contract_id TEXT NOT NULL,
    revision INTEGER NOT NULL,
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (novel_id, idempotency_key, operation),
    FOREIGN KEY (contract_id) REFERENCES outline_contracts(id) ON DELETE CASCADE
);
