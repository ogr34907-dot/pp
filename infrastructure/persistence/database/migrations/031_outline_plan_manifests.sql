-- Immutable book-level manifests become the planning authority only after a
-- per-novel Head is explicitly switched from legacy to manifest mode.

ALTER TABLE outline_contract_versions
ADD COLUMN sealed_at TEXT;

CREATE TABLE IF NOT EXISTS outline_plan_revisions (
    id TEXT PRIMARY KEY,
    novel_id TEXT NOT NULL,
    revision INTEGER NOT NULL CHECK(revision > 0),
    parent_plan_revision_id TEXT,
    status TEXT NOT NULL DEFAULT 'draft'
      CHECK(status IN (
        'draft', 'generating', 'validating', 'ready_for_review',
        'published', 'archived', 'failed', 'stale'
      )),
    digest TEXT NOT NULL DEFAULT '',
    base_plan_digest TEXT NOT NULL DEFAULT '',
    replan_start_chapter INTEGER,
    canonical_prefix_digest TEXT NOT NULL DEFAULT '',
    canonical_boundary_json TEXT NOT NULL DEFAULT '{}',
    reconciliation_status TEXT NOT NULL DEFAULT 'aligned'
      CHECK(reconciliation_status IN (
        'aligned', 'repairable', 'author_decision_required'
      )),
    reconciliation_report_json TEXT NOT NULL DEFAULT '{}',
    author_intent TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT 'system',
    publish_idempotency_key TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    sealed_at TEXT,
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE,
    FOREIGN KEY (parent_plan_revision_id)
      REFERENCES outline_plan_revisions(id) ON DELETE RESTRICT,
    UNIQUE(novel_id, revision)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_outline_plan_revisions_sealed_digest
ON outline_plan_revisions(novel_id, digest)
WHERE sealed_at IS NOT NULL AND digest <> '';

CREATE INDEX IF NOT EXISTS idx_outline_plan_revisions_novel_status
ON outline_plan_revisions(novel_id, status, revision DESC);

CREATE TABLE IF NOT EXISTS outline_plan_revision_items (
    id TEXT PRIMARY KEY,
    plan_revision_id TEXT NOT NULL,
    logical_node_id TEXT NOT NULL,
    version_id TEXT NOT NULL,
    parent_logical_node_id TEXT,
    level TEXT NOT NULL
      CHECK(level IN ('outline', 'part', 'volume', 'act', 'chapter')),
    sibling_index INTEGER NOT NULL CHECK(sibling_index >= 0),
    expansion_state TEXT NOT NULL DEFAULT 'unexpanded'
      CHECK(expansion_state IN ('unexpanded', 'expanded')),
    validated_parent_digest TEXT NOT NULL DEFAULT '',
    validated_previous_sibling_digest TEXT NOT NULL DEFAULT '',
    is_reused INTEGER NOT NULL DEFAULT 0 CHECK(is_reused IN (0, 1)),
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (plan_revision_id)
      REFERENCES outline_plan_revisions(id) ON DELETE CASCADE,
    FOREIGN KEY (logical_node_id)
      REFERENCES outline_contracts(id) ON DELETE RESTRICT,
    FOREIGN KEY (version_id)
      REFERENCES outline_contract_versions(id) ON DELETE RESTRICT,
    FOREIGN KEY (parent_logical_node_id)
      REFERENCES outline_contracts(id) ON DELETE RESTRICT,
    UNIQUE(plan_revision_id, logical_node_id)
);

CREATE UNIQUE INDEX IF NOT EXISTS ux_outline_plan_items_sibling_position
ON outline_plan_revision_items(
    plan_revision_id,
    COALESCE(parent_logical_node_id, ''),
    level,
    sibling_index
);

CREATE INDEX IF NOT EXISTS idx_outline_plan_items_revision_level
ON outline_plan_revision_items(plan_revision_id, level, sibling_index);

CREATE TABLE IF NOT EXISTS outline_planning_heads (
    novel_id TEXT PRIMARY KEY,
    authority_mode TEXT NOT NULL DEFAULT 'legacy'
      CHECK(authority_mode IN ('legacy', 'manifest')),
    authority_generation INTEGER NOT NULL DEFAULT 0
      CHECK(authority_generation >= 0),
    active_plan_revision_id TEXT,
    active_plan_digest TEXT NOT NULL DEFAULT '',
    working_plan_revision_id TEXT,
    projection_generation INTEGER NOT NULL DEFAULT 0
      CHECK(projection_generation >= 0),
    auto_publish_repairable INTEGER NOT NULL DEFAULT 0
      CHECK(auto_publish_repairable IN (0, 1)),
    updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (novel_id) REFERENCES novels(id) ON DELETE CASCADE,
    FOREIGN KEY (active_plan_revision_id)
      REFERENCES outline_plan_revisions(id) ON DELETE RESTRICT,
    FOREIGN KEY (working_plan_revision_id)
      REFERENCES outline_plan_revisions(id) ON DELETE RESTRICT
);

ALTER TABLE chapter_candidates
ADD COLUMN planning_authority_generation INTEGER NOT NULL DEFAULT 0;

ALTER TABLE chapter_candidates
ADD COLUMN plan_revision_id TEXT
REFERENCES outline_plan_revisions(id) ON DELETE RESTRICT;

ALTER TABLE chapter_candidates
ADD COLUMN plan_digest TEXT NOT NULL DEFAULT '';

ALTER TABLE chapter_candidates
ADD COLUMN chapter_outline_digest TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_chapter_candidates_plan_revision
ON chapter_candidates(novel_id, plan_revision_id, status);

ALTER TABLE outline_generation_attempts
ADD COLUMN plan_revision_id TEXT
REFERENCES outline_plan_revisions(id) ON DELETE RESTRICT;

ALTER TABLE outline_generation_attempts
ADD COLUMN cohort_parent_logical_node_id TEXT;

ALTER TABLE outline_generation_attempts
ADD COLUMN cohort_level TEXT;

ALTER TABLE outline_generation_attempts
ADD COLUMN cohort_scope_json TEXT NOT NULL DEFAULT '{}';

CREATE INDEX IF NOT EXISTS idx_outline_generation_attempts_plan
ON outline_generation_attempts(plan_revision_id, cohort_parent_logical_node_id);

-- Sealing is the only allowed mutation of an unsealed revision. Once sealed,
-- publication and restore switch only the Head.
CREATE TRIGGER IF NOT EXISTS trg_outline_plan_revisions_sealed_update
BEFORE UPDATE ON outline_plan_revisions
WHEN OLD.sealed_at IS NOT NULL
BEGIN
    SELECT RAISE(ABORT, 'sealed outline plan revision is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_outline_plan_revisions_sealed_delete
BEFORE DELETE ON outline_plan_revisions
WHEN OLD.sealed_at IS NOT NULL
 AND EXISTS (SELECT 1 FROM novels WHERE id = OLD.novel_id)
BEGIN
    SELECT RAISE(ABORT, 'sealed outline plan revision is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_outline_plan_items_sealed_insert
BEFORE INSERT ON outline_plan_revision_items
WHEN EXISTS (
    SELECT 1 FROM outline_plan_revisions AS revision
    WHERE revision.id = NEW.plan_revision_id
      AND revision.sealed_at IS NOT NULL
)
BEGIN
    SELECT RAISE(ABORT, 'sealed outline plan items are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_outline_plan_items_sealed_update
BEFORE UPDATE ON outline_plan_revision_items
WHEN EXISTS (
    SELECT 1 FROM outline_plan_revisions AS revision
    WHERE revision.id = OLD.plan_revision_id
      AND revision.sealed_at IS NOT NULL
)
BEGIN
    SELECT RAISE(ABORT, 'sealed outline plan items are immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_outline_plan_items_sealed_delete
BEFORE DELETE ON outline_plan_revision_items
WHEN EXISTS (
    SELECT 1
    FROM outline_plan_revisions AS revision
    JOIN novels AS novel ON novel.id = revision.novel_id
    WHERE revision.id = OLD.plan_revision_id
      AND revision.sealed_at IS NOT NULL
)
BEGIN
    SELECT RAISE(ABORT, 'sealed outline plan items are immutable');
END;

-- Legacy books retain their current node-level behavior during shadow
-- migration. After cutover, a sealed content version cannot be repurposed by
-- old mutators or a direct SQL caller.
CREATE TRIGGER IF NOT EXISTS trg_outline_contract_versions_manifest_sealed_update
BEFORE UPDATE ON outline_contract_versions
WHEN OLD.sealed_at IS NOT NULL
 AND EXISTS (
    SELECT 1
    FROM outline_contracts AS contract
    JOIN outline_planning_heads AS head ON head.novel_id = contract.novel_id
    WHERE contract.id = OLD.contract_id
      AND head.authority_mode = 'manifest'
)
BEGIN
    SELECT RAISE(ABORT, 'sealed outline contract version is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_outline_contract_versions_manifest_sealed_delete
BEFORE DELETE ON outline_contract_versions
WHEN OLD.sealed_at IS NOT NULL
 AND EXISTS (
    SELECT 1
    FROM outline_contracts AS contract
    JOIN outline_planning_heads AS head ON head.novel_id = contract.novel_id
    JOIN novels AS novel ON novel.id = contract.novel_id
    WHERE contract.id = OLD.contract_id
      AND head.authority_mode = 'manifest'
)
BEGIN
    SELECT RAISE(ABORT, 'sealed outline contract version is immutable');
END;

CREATE TRIGGER IF NOT EXISTS trg_outline_planning_heads_active_insert
BEFORE INSERT ON outline_planning_heads
WHEN NEW.active_plan_revision_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM outline_plan_revisions AS revision
    WHERE revision.id = NEW.active_plan_revision_id
      AND revision.novel_id = NEW.novel_id
      AND revision.sealed_at IS NOT NULL
      AND revision.digest = NEW.active_plan_digest
)
BEGIN
    SELECT RAISE(ABORT, 'active outline plan must be a matching sealed revision');
END;

CREATE TRIGGER IF NOT EXISTS trg_outline_planning_heads_manifest_insert
BEFORE INSERT ON outline_planning_heads
WHEN NEW.authority_mode = 'manifest'
 AND (
    NEW.active_plan_revision_id IS NULL
    OR NEW.active_plan_digest = ''
    OR NEW.projection_generation <> NEW.authority_generation
 )
BEGIN
    SELECT RAISE(ABORT, 'manifest authority requires an active sealed plan');
END;

CREATE TRIGGER IF NOT EXISTS trg_outline_planning_heads_active_update
BEFORE UPDATE OF active_plan_revision_id, active_plan_digest ON outline_planning_heads
WHEN NEW.active_plan_revision_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM outline_plan_revisions AS revision
    WHERE revision.id = NEW.active_plan_revision_id
      AND revision.novel_id = NEW.novel_id
      AND revision.sealed_at IS NOT NULL
      AND revision.digest = NEW.active_plan_digest
)
BEGIN
    SELECT RAISE(ABORT, 'active outline plan must be a matching sealed revision');
END;

CREATE TRIGGER IF NOT EXISTS trg_outline_planning_heads_manifest_update
BEFORE UPDATE OF authority_mode, active_plan_revision_id, active_plan_digest,
                 authority_generation, projection_generation
ON outline_planning_heads
WHEN NEW.authority_mode = 'manifest'
 AND (
    NEW.active_plan_revision_id IS NULL
    OR NEW.active_plan_digest = ''
    OR NEW.projection_generation <> NEW.authority_generation
 )
BEGIN
    SELECT RAISE(ABORT, 'manifest authority requires an active sealed plan');
END;

CREATE TRIGGER IF NOT EXISTS trg_outline_planning_heads_working_insert
BEFORE INSERT ON outline_planning_heads
WHEN NEW.working_plan_revision_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM outline_plan_revisions AS revision
    WHERE revision.id = NEW.working_plan_revision_id
      AND revision.novel_id = NEW.novel_id
      AND revision.sealed_at IS NULL
      AND revision.status IN ('draft', 'generating', 'validating')
 )
BEGIN
    SELECT RAISE(ABORT, 'working plan must be a same-novel editable draft');
END;

CREATE TRIGGER IF NOT EXISTS trg_outline_planning_heads_working_update
BEFORE UPDATE OF working_plan_revision_id ON outline_planning_heads
WHEN NEW.working_plan_revision_id IS NOT NULL
 AND NOT EXISTS (
    SELECT 1 FROM outline_plan_revisions AS revision
    WHERE revision.id = NEW.working_plan_revision_id
      AND revision.novel_id = NEW.novel_id
      AND revision.sealed_at IS NULL
      AND revision.status IN ('draft', 'generating', 'validating')
 )
BEGIN
    SELECT RAISE(ABORT, 'working plan must be a same-novel editable draft');
END;

CREATE TRIGGER IF NOT EXISTS trg_outline_planning_heads_generation_monotonic
BEFORE UPDATE OF authority_generation ON outline_planning_heads
WHEN NEW.authority_generation < OLD.authority_generation
BEGIN
    SELECT RAISE(ABORT, 'planning authority generation cannot decrease');
END;

CREATE TRIGGER IF NOT EXISTS trg_outline_planning_heads_manifest_delete
BEFORE DELETE ON outline_planning_heads
WHEN OLD.authority_mode = 'manifest'
 AND EXISTS (SELECT 1 FROM novels WHERE id = OLD.novel_id)
BEGIN
    SELECT RAISE(ABORT, 'manifest planning Head is immutable');
END;
