"""Persistence behavior for immutable book-level outline manifests."""

import sqlite3

import pytest

from domain.structure.outline_contract import OutlinePayload, OutlineSource
from domain.structure.outline_plan import (
    BackfillStatus,
    OutlinePlanItem,
    PlanRevisionStatus,
    PlanningAuthorityMode,
)
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.outline_contract_repository import (
    OutlineContractRepository,
)


@pytest.fixture
def plan_repo(tmp_path):
    database = DatabaseConnection(str(tmp_path / "outline-plan.db"))
    conn = database.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("novel-1", "Manifest Novel", "manifest-novel", 80),
    )
    conn.commit()
    return database, OutlineContractRepository(database)


def _published_root(
    database: DatabaseConnection,
    repository: OutlineContractRepository,
    *,
    title: str = "总纲",
) -> OutlinePlanItem:
    root = repository.ensure_root("novel-1")
    draft = repository.save_draft(
        root.id,
        OutlinePayload(
            title=title,
            narrative_text="主角在六个阶段中以持续代价追寻目标。",
            creative_goal="完成全书不可逆的核心选择",
            entry_state="旧秩序仍然完整",
            exit_state="新秩序建立但付出代价",
        ),
        source=OutlineSource.AUTHOR,
    )
    published = repository.publish_and_sync(
        root.id,
        expected_revision=draft.draft.revision,
        idempotency_key=f"publish-{title}",
    )
    conn = database.get_connection()
    row = conn.execute(
        """
        SELECT contract.active_version_id AS version_id, version.digest
        FROM outline_contracts AS contract
        JOIN outline_contract_versions AS version
          ON version.id = contract.active_version_id
        WHERE contract.id = ?
        """,
        (published.id,),
    ).fetchone()
    return OutlinePlanItem(
        logical_node_id=published.id,
        version_id=str(row["version_id"]),
        version_digest=str(row["digest"]),
        level=published.level,
        sibling_index=0,
        expansion_state="unexpanded",
    )


def test_head_starts_legacy_and_working_draft_is_not_active(plan_repo):
    database, repository = plan_repo
    item = _published_root(database, repository)

    first_head = repository.ensure_planning_head("novel-1")
    second_head = repository.ensure_planning_head("novel-1")
    draft = repository.create_plan_draft(
        novel_id="novel-1",
        items=(item,),
        canonical_prefix_digest="",
        canonical_boundary={"formal_head": 0},
        author_intent="保留核心结局",
        created_by="author",
    )
    head_with_draft = repository.get_planning_head("novel-1")

    assert first_head == second_head
    assert first_head.authority_mode == PlanningAuthorityMode.LEGACY
    assert first_head.authority_generation == 0
    assert first_head.active_plan_revision_id is None
    assert draft.status == PlanRevisionStatus.DRAFT
    assert head_with_draft.working_plan_revision_id == draft.id
    assert head_with_draft.active_plan_revision_id is None

    sealed = repository.seal_plan_revision(draft.id)
    sealed_head = repository.get_planning_head("novel-1")

    assert sealed.status == PlanRevisionStatus.READY_FOR_REVIEW
    assert sealed.sealed_at
    assert sealed_head.working_plan_revision_id is None
    assert sealed_head.active_plan_revision_id is None


def test_identical_sealed_digest_reuses_existing_revision_and_content_version(plan_repo):
    database, repository = plan_repo
    item = _published_root(database, repository)
    first = repository.create_plan_draft(
        novel_id="novel-1",
        items=(item,),
        canonical_prefix_digest="",
        canonical_boundary={"formal_head": 0},
    )
    sealed = repository.seal_plan_revision(first.id)
    retry = repository.create_plan_draft(
        novel_id="novel-1",
        items=(item,),
        canonical_prefix_digest="",
        canonical_boundary={"formal_head": 0},
    )

    reused = repository.seal_plan_revision(retry.id)
    conn = database.get_connection()

    assert reused.id == sealed.id
    assert conn.execute(
        "SELECT COUNT(*) FROM outline_plan_revisions WHERE novel_id = 'novel-1'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM outline_contract_versions WHERE contract_id = ?",
        (item.logical_node_id,),
    ).fetchone()[0] == 1


def test_valid_synced_legacy_projection_backfills_once_without_cutover(plan_repo):
    database, repository = plan_repo
    item = _published_root(database, repository)

    result = repository.backfill_initial_plan("novel-1")
    repeated = repository.backfill_initial_plan("novel-1")
    head = repository.get_planning_head("novel-1")
    active = repository.get_active_plan("novel-1")

    assert result.status == BackfillStatus.MIGRATED
    assert repeated.status == BackfillStatus.ALREADY_BACKFILLED
    assert active is not None
    assert active.id == result.plan.id == repeated.plan.id
    assert active.status == PlanRevisionStatus.READY_FOR_REVIEW
    assert active.items == (item,)
    assert head.active_plan_revision_id == active.id
    assert head.active_plan_digest == active.digest
    assert head.authority_mode == PlanningAuthorityMode.LEGACY


def test_mixed_or_tampered_legacy_projection_stays_fail_closed(plan_repo):
    database, repository = plan_repo
    _published_root(database, repository)
    conn = database.get_connection()
    conn.execute("UPDATE outline_plan_projections SET digest = 'tampered'")
    conn.commit()

    result = repository.backfill_initial_plan("novel-1")

    assert result.status == BackfillStatus.PLANNING_MIGRATION_REQUIRED
    assert "digest" in result.reason
    assert repository.get_active_plan("novel-1") is None
    assert conn.execute(
        "SELECT COUNT(*) FROM outline_plan_revisions WHERE novel_id = 'novel-1'"
    ).fetchone()[0] == 0


def test_sealed_manifest_rows_and_versions_reject_direct_mutation(plan_repo):
    database, repository = plan_repo
    item = _published_root(database, repository)
    result = repository.backfill_initial_plan("novel-1")
    conn = database.get_connection()
    conn.execute(
        """
        UPDATE outline_planning_heads
        SET authority_mode = 'manifest', authority_generation = 1,
            projection_generation = 1
        WHERE novel_id = 'novel-1'
        """
    )
    conn.commit()

    statements = (
        (
            "DELETE FROM outline_planning_heads WHERE novel_id = 'novel-1'",
            (),
        ),
        (
            "UPDATE outline_plan_revisions SET author_intent = 'changed' WHERE id = ?",
            (result.plan.id,),
        ),
        (
            "DELETE FROM outline_plan_revisions WHERE id = ?",
            (result.plan.id,),
        ),
        (
            "UPDATE outline_plan_revision_items SET sibling_index = 9 "
            "WHERE plan_revision_id = ?",
            (result.plan.id,),
        ),
        (
            "DELETE FROM outline_plan_revision_items WHERE plan_revision_id = ?",
            (result.plan.id,),
        ),
        (
            "UPDATE outline_contract_versions SET payload_json = '{}' WHERE id = ?",
            (item.version_id,),
        ),
        (
            "DELETE FROM outline_contract_versions WHERE id = ?",
            (item.version_id,),
        ),
    )

    for sql, parameters in statements:
        with pytest.raises(sqlite3.IntegrityError, match="immutable"):
            conn.execute(sql, parameters)
        conn.rollback()


def test_legacy_publish_invalidates_only_the_shadow_pointer_then_rebackfills(plan_repo):
    database, repository = plan_repo
    _published_root(database, repository, title="总纲 v1")
    first = repository.backfill_initial_plan("novel-1").plan
    root = repository.ensure_root("novel-1")

    draft = repository.save_draft(
        root.id,
        OutlinePayload(
            title="总纲 v2",
            narrative_text="未来规划已经改变。",
            creative_goal="新的全书目标",
            entry_state="开始",
            exit_state="新的结束",
        ),
        source=OutlineSource.AUTHOR,
    )
    repository.publish_and_sync(root.id, expected_revision=draft.draft.revision)

    invalidated_head = repository.get_planning_head("novel-1")
    preserved = repository.get_plan_revision(first.id)
    assert invalidated_head.authority_mode == PlanningAuthorityMode.LEGACY
    assert invalidated_head.active_plan_revision_id is None
    assert preserved.digest == first.digest
    assert preserved.items == first.items

    second = repository.backfill_initial_plan("novel-1").plan
    assert second.id != first.id
    assert second.digest != first.digest


def test_head_rejects_manifest_without_active_plan_and_cross_novel_working_draft(
    plan_repo,
):
    database, repository = plan_repo
    item = _published_root(database, repository)
    draft = repository.create_plan_draft(
        novel_id="novel-1",
        items=(item,),
        canonical_prefix_digest="",
        canonical_boundary={"formal_head": 0},
    )
    conn = database.get_connection()

    with pytest.raises(sqlite3.IntegrityError, match="active sealed plan"):
        conn.execute(
            "UPDATE outline_planning_heads SET authority_mode = 'manifest' "
            "WHERE novel_id = 'novel-1'"
        )
    conn.rollback()

    conn.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-2', 'Other', 'other')"
    )
    conn.execute(
        "INSERT INTO outline_planning_heads (novel_id) VALUES ('novel-2')"
    )
    conn.commit()
    with pytest.raises(sqlite3.IntegrityError, match="same-novel editable draft"):
        conn.execute(
            "UPDATE outline_planning_heads SET working_plan_revision_id = ? "
            "WHERE novel_id = 'novel-2'",
            (draft.id,),
        )
    conn.rollback()
