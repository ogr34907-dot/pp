"""HTTP contract for evidence-based outline continuity review."""

from domain.structure.outline_continuity import (
    ContinuityDecision,
    ContinuityIssue,
    ContinuityIssueSeverity,
    ContinuityReviewReport,
    ContinuityReviewScope,
)
from domain.structure.outline_contract import OutlineLevel, OutlinePayload, OutlineSource
from infrastructure.persistence.database.outline_continuity_review_repository import (
    OutlineContinuityReviewRepository,
)
from application.blueprint.services.outline_continuity_review_service import (
    OutlineContinuityReviewService,
)
from infrastructure.persistence.database.outline_contract_repository import (
    OutlineContractRepository,
)


def _review_run(db, *, decision: ContinuityDecision = ContinuityDecision.REVIEW):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('review-novel', 'Review Novel', 'review-novel')"
    )
    conn.execute(
        """
        INSERT INTO outline_plan_revisions
            (id, novel_id, revision, status, digest, canonical_prefix_digest,
             reconciliation_status)
        VALUES ('review-plan', 'review-novel', 1, 'draft', 'review-digest',
                'canonical', 'aligned')
        """
    )
    conn.commit()
    reviews = OutlineContinuityReviewRepository(db)
    scope = ContinuityReviewScope(
        parent_logical_node_id="outline-root",
        level="part",
        parent_version_id="root-version",
        parent_version_digest="root-digest",
    )
    run = reviews.begin(
        novel_id="review-novel",
        plan_revision_id="review-plan",
        scope=scope,
        plan_digest="review-digest",
        scope_fingerprint="review-scope",
    )
    return reviews.complete(
        run["id"],
        report=ContinuityReviewReport(
            decision=decision,
            confidence=0.9,
            scope_fingerprint="review-scope",
        ),
    )


def test_continuity_status_returns_current_report(client, db):
    run = _review_run(db)

    response = client.get(
        "/api/v1/outline/plan-revisions/review-plan/continuity-review",
        params={"parent_logical_node_id": "outline-root"},
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["plan_digest"] == "review-digest"
    assert data["latest"]["id"] == run["id"]
    assert data["current"]["id"] == run["id"]
    assert data["technical_blockers"] == [
        "outline plan requires at least one item"
    ]


def test_review_decision_can_be_acknowledged_without_a_reason(client, db):
    run = _review_run(db)

    response = client.post(
        "/api/v1/outline/plan-revisions/review-plan/continuity-review/acknowledge",
        json={
            "expected_plan_digest": "review-digest",
            "state": "acknowledged",
            "review_ids": [run["id"]],
            "scope_fingerprints": [run["scope_fingerprint"]],
            "actor": "author",
            "idempotency_key": "review-no-reason",
        },
    )

    assert response.status_code == 200
    data = response.json()["data"]
    assert data["state"] == "acknowledged"
    assert data["receipt"]["override_id"]


def test_conflict_acknowledgement_requires_a_reason(client, db):
    run = _review_run(db, decision=ContinuityDecision.CONFLICT)
    path = "/api/v1/outline/plan-revisions/review-plan/continuity-review/acknowledge"
    payload = {
        "expected_plan_digest": "review-digest",
        "state": "acknowledged",
        "review_ids": [run["id"]],
        "scope_fingerprints": [run["scope_fingerprint"]],
        "actor": "author",
        "idempotency_key": "conflict-reason",
    }

    missing_reason = client.post(path, json=payload)
    confirmed = client.post(path, json={**payload, "reason": "The conflict is intentional."})

    assert missing_reason.status_code == 422
    assert confirmed.status_code == 200
    assert confirmed.json()["data"]["state"] == "acknowledged"


def test_plural_review_routes_match_the_documented_contract(client, db):
    _review_run(db)

    response = client.get(
        "/api/v1/outline/plan-revisions/review-plan/continuity-reviews/latest",
        params={"parent_logical_node_id": "outline-root"},
    )

    assert response.status_code == 200
    assert response.json()["data"]["current"]["decision"] == "review"


def test_suggestion_apply_requires_the_exact_scope_fingerprint(client, db):
    run = _review_run(db)
    path = f"/api/v1/outline/continuity-reviews/{run['id']}/suggestions/issue-1/apply"
    response = client.post(
        path,
        json={
            "logical_node_id": "outline-root",
            "expected_plan_digest": "review-digest",
            "expected_version_digest": "root-digest",
            "scope_fingerprint": "wrong-scope",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "continuity review is stale"


def test_receipt_rejects_a_report_when_its_scope_fingerprint_is_stale(client, db):
    _, working, _, run = _working_review_with_suggestion(db)

    response = client.post(
        f"/api/v1/outline/plan-revisions/{working.id}/continuity-review/acknowledge",
        json={
            "expected_plan_digest": working.digest,
            "state": "acknowledged",
            "review_ids": [run["id"]],
            "scope_fingerprints": ["stale-scope"],
            "actor": "author",
            "idempotency_key": "stale-review-receipt",
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "outline_narrative_confirmation_required"


def _working_review_with_suggestion(db, *, locked: bool = False):
    novel_id = "suggestion-novel"
    db.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        (novel_id, "Suggestion Novel", "suggestion-novel", 10),
    )
    db.get_connection().commit()
    repository = OutlineContractRepository(db)
    root = repository.ensure_root(novel_id)
    root_draft = repository.save_draft(
        root.id,
        OutlinePayload(
            title="总纲",
            narrative_text="总纲说明",
            creative_goal="完成总目标",
            entry_state="开始",
            exit_state="结束",
            chapter_start=1,
            chapter_end=10,
        ),
        source=OutlineSource.AUTHOR,
    )
    repository.publish_and_sync(root.id, expected_revision=root_draft.draft.revision)
    active = repository.backfill_initial_plan(novel_id).plan
    assert active is not None
    conn = db.get_connection()
    conn.execute(
        "UPDATE outline_planning_heads SET authority_mode='manifest', "
        "authority_generation=1, projection_generation=1 WHERE novel_id=?",
        (novel_id,),
    )
    conn.commit()
    working = repository.clone_active_plan_draft(novel_id)
    child_payload = OutlinePayload(
        title="第一部",
        narrative_text="第一部说明",
        creative_goal="旧目标",
        entry_state="开始",
        exit_state="结束",
        chapter_start=1,
        chapter_end=10,
        extra=(
            {"_field_provenance": {"creative_goal": {"locked": True}}}
            if locked
            else {}
        ),
    )
    working = repository.replace_draft_cohort_payloads(
        plan_revision_id=working.id,
        parent_logical_node_id=root.id,
        payloads=(child_payload,),
    )
    part = next(item for item in working.items if item.level is OutlineLevel.PART)
    reviews = OutlineContinuityReviewRepository(db)
    scope = ContinuityReviewScope(
        parent_logical_node_id=root.id,
        level="part",
        parent_version_id=next(
            item for item in working.items if item.logical_node_id == root.id
        ).version_id,
        parent_version_digest=next(
            item for item in working.items if item.logical_node_id == root.id
        ).version_digest,
        children=(
            {
                "logical_node_id": part.logical_node_id,
                "version_id": part.version_id,
                "version_digest": part.version_digest,
                "sibling_index": part.sibling_index,
            },
        ),
    )
    scope_fingerprint = OutlineContinuityReviewService(
        repository, reviews, object(), db
    ).current_scope_fingerprint(working.id, root.id, working.digest)
    run = reviews.begin(
        novel_id=novel_id,
        plan_revision_id=working.id,
        scope=scope,
        plan_digest=working.digest,
        scope_fingerprint=scope_fingerprint,
    )
    run = reviews.complete(
        run["id"],
        report=ContinuityReviewReport(
            decision=ContinuityDecision.REVIEW,
            confidence=0.9,
            scope_fingerprint=scope_fingerprint,
            issues=(
                ContinuityIssue(
                    id="improve-goal",
                    code="goal_bridge",
                    severity=ContinuityIssueSeverity.WARNING,
                    scope={"logical_node_id": part.logical_node_id},
                    message="The child needs a more explicit goal.",
                    suggested_patch={"creative_goal": "建立明确的主线冲突"},
                ),
            ),
        ),
    )
    return repository, working, part, run


def test_suggestion_apply_updates_current_working_item(client, db):
    repository, working, part, run = _working_review_with_suggestion(db)

    response = client.post(
        f"/api/v1/outline/continuity-reviews/{run['id']}/suggestions/improve-goal/apply",
        json={
            "logical_node_id": part.logical_node_id,
            "expected_plan_digest": working.digest,
            "expected_version_digest": part.version_digest,
            "scope_fingerprint": run["scope_fingerprint"],
        },
    )

    assert response.status_code == 200
    updated = repository.get_plan_revision(working.id)
    updated_part = next(item for item in updated.items if item.logical_node_id == part.logical_node_id)
    assert updated_part.version_digest != part.version_digest
    assert updated.narrative_review_state.value == "pending"


def test_suggestion_apply_rejects_an_author_locked_field(client, db):
    _, working, part, run = _working_review_with_suggestion(db, locked=True)

    response = client.post(
        f"/api/v1/outline/continuity-reviews/{run['id']}/suggestions/improve-goal/apply",
        json={
            "logical_node_id": part.logical_node_id,
            "expected_plan_digest": working.digest,
            "expected_version_digest": part.version_digest,
            "scope_fingerprint": run["scope_fingerprint"],
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "continuity suggestion changes an author-locked field"
