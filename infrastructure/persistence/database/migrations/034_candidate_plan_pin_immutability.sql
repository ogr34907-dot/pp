-- A Candidate is authored against one immutable planning provenance.  The
-- active Head may later move for a compatible future-only replan, but this
-- original pin must never be repointed in place.
ALTER TABLE chapter_candidates
ADD COLUMN plan_pin_fingerprint TEXT NOT NULL DEFAULT '';

-- SQLite has no portable SHA function in migrations.  A length-delimited
-- fingerprint is sufficient here because the trigger below protects both the
-- provenance fields and their checksum after this migration is applied.
UPDATE chapter_candidates
SET plan_pin_fingerprint =
    CAST(length(CAST(planning_authority_generation AS TEXT)) AS TEXT)
    || ':' || CAST(planning_authority_generation AS TEXT)
    || '|' || CAST(length(COALESCE(plan_revision_id, '<null>')) AS TEXT)
    || ':' || COALESCE(plan_revision_id, '<null>')
    || '|' || CAST(length(plan_digest) AS TEXT)
    || ':' || plan_digest
WHERE plan_pin_fingerprint = '';

CREATE TRIGGER IF NOT EXISTS trg_chapter_candidates_plan_pin_immutable
BEFORE UPDATE OF planning_authority_generation, plan_revision_id, plan_digest,
                 outline_chain_json, outline_chain_digest, chapter_outline_digest,
                 plan_pin_fingerprint
ON chapter_candidates
BEGIN
    SELECT RAISE(ABORT, 'candidate plan pin is immutable');
END;
