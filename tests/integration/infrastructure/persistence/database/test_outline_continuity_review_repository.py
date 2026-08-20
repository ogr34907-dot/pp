"""SQLite persistence for immutable outline continuity review history."""

import json

import pytest

from domain.structure.outline_continuity import (
    ContinuityDecision,
    ContinuityReviewReport,
    ContinuityReviewScope,
)
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.outline_continuity_review_repository import (
    OutlineContinuityReviewRepository,
)


@pytest.fixture
def continuity_repo(tmp_path):
    database = DatabaseConnection(str(tmp_path / "continuity-reviews.db"))
    conn = database.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'Novel', 'novel-1')"
    )
    conn.execute(
        """
        INSERT INTO outline_plan_revisions
            (id, novel_id, revision, status, digest, canonical_prefix_digest,
             reconciliation_status)
        VALUES ('plan-1', 'novel-1', 1, 'draft', 'plan-digest', 'canonical', 'aligned')
        """
    )
    conn.execute(
        """
        INSERT INTO outline_plan_revisions
            (id, novel_id, revision, status, digest, canonical_prefix_digest,
             reconciliation_status)
        VALUES ('plan-2', 'novel-1', 2, 'draft', 'plan-digest-2', 'canonical', 'aligned')
        """
    )
    conn.commit()
    return database, OutlineContinuityReviewRepository(database)


def _scope() -> ContinuityReviewScope:
    return ContinuityReviewScope(
        parent_logical_node_id="part-1",
        level="volume",
        parent_version_id="part-version-1",
        parent_version_digest="part-digest-1",
        children=(
            {
                "logical_node_id": "volume-1",
                "version_id": "volume-version-1",
                "version_digest": "volume-digest-1",
            },
        ),
    )


def _begin(
    repository: OutlineContinuityReviewRepository,
    *,
    fingerprint: str,
    plan_digest: str = "plan-digest",
    plan_revision_id: str = "plan-1",
    force: bool = False,
):
    return repository.begin(
        novel_id="novel-1",
        plan_revision_id=plan_revision_id,
        scope=_scope(),
        plan_digest=plan_digest,
        scope_fingerprint=fingerprint,
        context_digest="context-digest",
        prompt_node_version_id="prompt-v1",
        prompt_hash="prompt-hash",
        schema_version="schema-v1",
        ruleset_version="rules-v1",
        model="model-a",
        force=force,
    )


def _passing_report(fingerprint: str) -> ContinuityReviewReport:
    return ContinuityReviewReport(
        decision=ContinuityDecision.PASS,
        confidence=0.95,
        scope_fingerprint=fingerprint,
        model="model-a",
        schema_version="schema-v1",
        ruleset_version="rules-v1",
    )


def test_migration_creates_review_metadata_and_plan_defaults(continuity_repo):
    database, _ = continuity_repo
    conn = database.get_connection()
    revision_columns = {
        row["name"] for row in conn.execute("PRAGMA table_info(outline_plan_revisions)")
    }
    run_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(outline_continuity_review_runs)")
    }
    override_columns = {
        row["name"]
        for row in conn.execute("PRAGMA table_info(outline_continuity_review_overrides)")
    }

    assert {"narrative_review_state", "narrative_review_receipt_json"} <= revision_columns
    assert {
        "id",
        "novel_id",
        "plan_revision_id",
        "scope_parent_logical_node_id",
        "level",
        "plan_digest",
        "scope_fingerprint",
        "context_digest",
        "prompt_node_version_id",
        "prompt_hash",
        "schema_version",
        "ruleset_version",
        "model",
        "state",
        "decision",
        "confidence",
        "report_json",
        "raw_response",
        "error",
        "created_at",
        "completed_at",
    } <= run_columns
    assert {
        "id",
        "novel_id",
        "plan_revision_id",
        "plan_digest",
        "action",
        "idempotency_key",
        "actor",
        "reason",
        "scope_fingerprints_json",
        "review_ids_json",
        "created_at",
    } <= override_columns
    assert tuple(
        conn.execute(
            "SELECT narrative_review_state, narrative_review_receipt_json "
            "FROM outline_plan_revisions WHERE id = 'plan-1'"
        ).fetchone()
    ) == ("not_required", "{}")


def test_begin_is_idempotent_and_force_waits_for_the_running_run(continuity_repo):
    _, repository = continuity_repo

    first = _begin(repository, fingerprint="scope-v1")
    assert _begin(repository, fingerprint="scope-v1")["id"] == first["id"]
    with pytest.raises(ValueError, match="running"):
        _begin(repository, fingerprint="scope-v1", force=True)

    completed = repository.complete(first["id"], report=_passing_report("scope-v1"))
    assert completed["state"] == "succeeded"
    assert _begin(repository, fingerprint="scope-v1")["id"] == first["id"]

    retry = _begin(repository, fingerprint="scope-v1", force=True)
    assert retry["id"] != first["id"]
    assert retry["state"] == "running"


def test_begin_reuses_completed_runs_when_scope_fingerprint_is_unchanged(continuity_repo):
    _, repository = continuity_repo

    original = _begin(repository, fingerprint="scope-shared")
    repository.complete(original["id"], report=_passing_report("scope-shared"))

    changed_digest = _begin(
        repository,
        fingerprint="scope-shared",
        plan_digest="plan-digest-2",
    )
    changed_revision = _begin(
        repository,
        fingerprint="scope-shared",
        plan_digest="plan-digest-2",
        plan_revision_id="plan-2",
    )

    assert changed_digest["id"] == original["id"]
    assert changed_digest["plan_digest"] == "plan-digest"
    assert changed_revision["id"] != original["id"]
    assert changed_revision["plan_revision_id"] == "plan-2"


def test_current_requires_the_exact_scope_fingerprint_but_not_plan_digest(continuity_repo):
    _, repository = continuity_repo

    old_run = _begin(repository, fingerprint="scope-old")
    new_run = _begin(repository, fingerprint="scope-new")
    repository.complete(old_run["id"], report=_passing_report("scope-old"))

    assert repository.current(
        plan_revision_id="plan-1",
        plan_digest="plan-digest",
        scope_fingerprint="scope-new",
    ) is None
    assert repository.current(
        plan_revision_id="plan-1",
        plan_digest="new-plan-digest",
        scope_fingerprint="scope-old",
    )["id"] == old_run["id"]

    repository.complete(new_run["id"], report=_passing_report("scope-new"))
    current = repository.current(
        plan_revision_id="plan-1",
        plan_digest="plan-digest",
        scope_fingerprint="scope-new",
    )

    assert current is not None
    assert current["id"] == new_run["id"]
    assert current["report"]["decision"] == "pass"
    assert repository.latest(
        plan_revision_id="plan-1", scope_parent_logical_node_id="part-1"
    )["id"] == new_run["id"]


def test_unavailable_failure_persists_a_structured_current_report(continuity_repo):
    _, repository = continuity_repo

    started = _begin(repository, fingerprint="scope-unavailable")
    failed = repository.fail(
        started["id"],
        error="provider timed out",
        raw_response="gateway timeout",
    )
    current = repository.current(
        plan_revision_id="plan-1",
        plan_digest="plan-digest",
        scope_fingerprint="scope-unavailable",
    )

    assert failed["state"] == "succeeded"
    assert failed["decision"] == "unavailable"
    assert failed["report"] == {
        "decision": "unavailable",
        "confidence": None,
        "scope_fingerprint": "scope-unavailable",
        "issues": [],
        "suggestions": [],
        "model": "model-a",
        "schema_version": "schema-v1",
        "ruleset_version": "rules-v1",
        "error": "provider timed out",
    }
    assert current is not None
    assert current["id"] == started["id"]
    assert current["report"] == failed["report"]


def test_acknowledgement_requires_a_reason_and_preserves_ordered_audit_inputs(
    continuity_repo,
):
    _, repository = continuity_repo

    with pytest.raises(ValueError, match="actor"):
        repository.acknowledge(
            novel_id="novel-1",
            plan_revision_id="plan-1",
            plan_digest="plan-digest",
            action="author_publish",
            idempotency_key="ack-1",
            actor=" ",
            reason="Reviewed",
            scope_fingerprints=("scope-b", "scope-a"),
            review_ids=("review-2", "review-1"),
        )
    with pytest.raises(ValueError, match="reason"):
        repository.acknowledge(
            novel_id="novel-1",
            plan_revision_id="plan-1",
            plan_digest="plan-digest",
            action="author_publish",
            idempotency_key="ack-1",
            actor="author-1",
            reason=" ",
            scope_fingerprints=("scope-b", "scope-a"),
            review_ids=("review-2", "review-1"),
        )

    receipt = repository.acknowledge(
        novel_id="novel-1",
        plan_revision_id="plan-1",
        plan_digest="plan-digest",
        action="author_publish",
        idempotency_key="ack-1",
        actor="author-1",
        reason="The known risks are accepted.",
        scope_fingerprints=("scope-b", "scope-a"),
        review_ids=("review-2", "review-1"),
    )
    repeated = repository.acknowledge(
        novel_id="novel-1",
        plan_revision_id="plan-1",
        plan_digest="plan-digest",
        action="author_publish",
        idempotency_key="ack-1",
        actor="another-author",
        reason="This cannot replace the original receipt.",
        scope_fingerprints=("scope-a",),
        review_ids=("review-1",),
    )

    assert repeated["id"] == receipt["id"]
    assert receipt["plan_digest"] == "plan-digest"
    assert receipt["scope_fingerprints"] == ["scope-b", "scope-a"]
    assert receipt["review_ids"] == ["review-2", "review-1"]
    assert receipt["actor"] == "author-1"
    assert json.loads(json.dumps(receipt)) == receipt


def test_caller_owned_transaction_can_roll_back_a_started_run(continuity_repo):
    database, repository = continuity_repo
    conn = database.get_connection()

    conn.execute("BEGIN")
    started = repository.begin_in_transaction(
        conn,
        novel_id="novel-1",
        plan_revision_id="plan-1",
        scope=_scope(),
        plan_digest="plan-digest",
        scope_fingerprint="rollback-scope",
    )
    assert repository.get(started["id"], _connection=conn)["state"] == "running"
    conn.rollback()

    with pytest.raises(KeyError, match="continuity review run not found"):
        repository.get(started["id"])
