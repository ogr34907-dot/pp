"""Manifest cutover must close every legacy StoryNode/contract writer."""

import asyncio
import json
import sqlite3

import pytest

from domain.structure.outline_contract import OutlinePayload
from domain.structure.outline_plan import canonical_plan_digest
from domain.structure.story_node import NodeType, StoryNode
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.outline_contract_repository import (
    OutlineContractRepository,
)
from infrastructure.persistence.database.plan_projection_writer import PlanProjectionWriter
from infrastructure.persistence.database.planning_authority_guard import (
    PlanningAuthorityError,
    assert_story_node_write_allowed,
)
from infrastructure.persistence.database.story_node_repository import StoryNodeRepository
from infrastructure.persistence.database.sqlite_chapter_repository import SqliteChapterRepository
from application.blueprint.services.chapter_book_structure_sync import (
    purge_chapter_book_rows_not_matching_structure,
)
from application.core.services.chapter_rewrite_coordinator import ChapterRewriteCoordinator
from application.engine.services.autopilot_recovery_policy import AutopilotRecoveryPolicy


def _book_with_sealed_plan(tmp_path, *, manifest: bool):
    database = DatabaseConnection(str(tmp_path / "manifest-guard.db"))
    conn = database.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("novel-1", "Manifest", "manifest", 20),
    )
    conn.commit()
    contracts = OutlineContractRepository(database)
    root = contracts.ensure_root("novel-1")
    draft = contracts.save_draft(
        root.id,
        OutlinePayload(
            title="Root",
            narrative_text="A complete premise",
            creative_goal="Reach the irreversible ending",
            entry_state="start",
            exit_state="end",
        ),
    )
    published = contracts.publish_and_sync(
        root.id, expected_revision=draft.draft.revision, idempotency_key="root"
    )
    row = conn.execute(
        "SELECT active_version_id, digest FROM outline_contracts "
        "JOIN outline_contract_versions ON outline_contract_versions.id = active_version_id "
        "WHERE outline_contracts.id = ?",
        (published.id,),
    ).fetchone()
    from domain.structure.outline_plan import OutlinePlanItem

    item = OutlinePlanItem(
        logical_node_id=published.id,
        version_id=str(row[0]),
        version_digest=str(row[1]),
        level=published.level,
        sibling_index=0,
    )
    plan = contracts.create_plan_draft(
        novel_id="novel-1",
        items=(item,),
        canonical_prefix_digest="",
        canonical_boundary={"formal_head": 0},
    )
    plan = contracts.seal_plan_revision(plan.id)
    if manifest:
        conn.execute(
            "UPDATE outline_planning_heads SET authority_mode='manifest', "
            "authority_generation=1, projection_generation=1, "
            "active_plan_revision_id=?, active_plan_digest=? WHERE novel_id=?",
            (plan.id, plan.digest, "novel-1"),
        )
    conn.commit()
    return database, StoryNodeRepository(database), contracts, plan.id


@pytest.fixture
def manifest_book(tmp_path):
    return _book_with_sealed_plan(tmp_path, manifest=True)


@pytest.fixture
def legacy_plan_book(tmp_path):
    return _book_with_sealed_plan(tmp_path, manifest=False)


def _node(node_id: str = "part-1") -> StoryNode:
    return StoryNode(
        id=node_id,
        novel_id="novel-1",
        node_type=NodeType.PART,
        number=1,
        title="Part",
        order_index=0,
    )


def _seed_projection_node(database, node: StoryNode) -> None:
    """Test-only fixture data; production projection batches are disabled."""

    conn = database.get_connection()
    conn.execute(
        """
        INSERT INTO story_nodes (
            id, novel_id, parent_id, node_type, number, title, description, order_index,
            planning_status, planning_source,
            chapter_start, chapter_end, chapter_count, suggested_chapter_count,
            content, outline, word_count, status,
            themes, key_events, narrative_arc, conflicts,
            pov_character_id, timeline_start, timeline_end,
            metadata, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            node.id,
            node.novel_id,
            node.parent_id,
            node.node_type.value,
            node.number,
            node.title,
            node.description,
            node.order_index,
            node.planning_status.value,
            node.planning_source.value,
            node.chapter_start,
            node.chapter_end,
            node.chapter_count,
            node.suggested_chapter_count,
            node.content,
            node.outline,
            node.word_count,
            node.status,
            json.dumps(node.themes),
            json.dumps(node.key_events),
            node.narrative_arc,
            json.dumps(node.conflicts),
            node.pov_character_id,
            node.timeline_start,
            node.timeline_end,
            json.dumps(node.metadata),
            node.created_at.isoformat(),
            node.updated_at.isoformat(),
        ),
    )
    conn.commit()


def test_legacy_story_node_writers_fail_closed(manifest_book):
    database, repository, contracts, _ = manifest_book
    with pytest.raises(PlanningAuthorityError, match="legacy StoryNode"):
        repository.save_sync(_node())

    conn = database.get_connection()
    conn.execute(
        "INSERT INTO story_nodes (id, novel_id, node_type, number, title, order_index) "
        "VALUES ('part-1', 'novel-1', 'part', 1, 'old', 0)"
    )
    conn.commit()
    existing = asyncio.run(repository.get_by_id("part-1"))
    existing.title = "changed"
    with pytest.raises(PlanningAuthorityError, match="legacy StoryNode"):
        asyncio.run(repository.update(existing))
    with pytest.raises(PlanningAuthorityError, match="legacy StoryNode"):
        asyncio.run(repository.save_batch([existing]))
    with pytest.raises(PlanningAuthorityError, match="legacy StoryNode"):
        asyncio.run(repository.apply_merge_plan([], [{"id": "part-1", "title": "x", "order_index": 0}], []))
    with pytest.raises(PlanningAuthorityError, match="legacy StoryNode"):
        asyncio.run(repository.delete("part-1"))
    with pytest.raises(PlanningAuthorityError, match="legacy StoryNode"):
        asyncio.run(repository.update_chapter_ranges("novel-1"))
    with pytest.raises(PlanningAuthorityError, match="legacy StoryNode"):
        repository.bulk_replace_text_sync("novel-1", "old", "new")


def _head_snapshot(database):
    row = database.get_connection().execute(
        """
        SELECT authority_mode, active_plan_revision_id, active_plan_digest,
               authority_generation, projection_generation
        FROM outline_planning_heads
        WHERE novel_id = 'novel-1'
        """
    ).fetchone()
    return {
        "mode": str(row["authority_mode"]),
        "plan_id": row["active_plan_revision_id"],
        "digest": str(row["active_plan_digest"] or ""),
        "authority_generation": int(row["authority_generation"]),
        "projection_generation": int(row["projection_generation"]),
    }


def _apply_atomic(writer, database, plan_id, *, operation, creates=(), updates=(), deletes=(), **expected):
    head = _head_snapshot(database)
    return asyncio.run(
        writer.apply_atomic(
            novel_id="novel-1",
            plan_revision_id=plan_id,
            operation=operation,
            expected_active_plan_revision_id=expected.get("expected_active_plan_revision_id", head["plan_id"]),
            expected_active_plan_digest=expected.get("expected_active_plan_digest", head["digest"]),
            expected_authority_generation=expected.get(
                "expected_authority_generation", head["authority_generation"]
            ),
            expected_projection_generation=expected.get(
                "expected_projection_generation", head["projection_generation"]
            ),
            creates=creates,
            updates=updates,
            deletes=deletes,
        )
    )


def _insert_sealed_plan(
    database,
    plan_id: str,
    *,
    revision: int = 99,
    digest: str = "replacement-digest",
    status: str = "ready_for_review",
    reconciliation_status: str = "aligned",
):
    conn = database.get_connection()
    conn.execute(
        """
        INSERT INTO outline_plan_revisions
        (id, novel_id, revision, status, digest, canonical_prefix_digest,
         reconciliation_status, sealed_at)
        VALUES (?, 'novel-1', ?, ?, ?, '', ?, CURRENT_TIMESTAMP)
        """,
        (plan_id, revision, status, digest, reconciliation_status),
    )
    conn.commit()


def test_manifest_head_rejects_raw_switch_to_unbound_cloned_draft(manifest_book):
    database, _, contracts, _ = manifest_book
    draft = contracts.clone_active_plan_draft("novel-1")
    conn = database.get_connection()
    before = _head_snapshot(database)
    assert conn.execute(
        """
        SELECT COUNT(*)
        FROM outline_plan_projection_bindings AS binding
        JOIN outline_plan_revision_items AS item
          ON item.id = binding.plan_revision_item_id
        WHERE item.plan_revision_id = ?
        """,
        (draft.id,),
    ).fetchone()[0] == 0
    draft_digest = canonical_plan_digest(
        canonical_prefix_digest=draft.canonical_prefix_digest,
        items=draft.items,
    )
    assert draft_digest != before["digest"]

    conn.execute("BEGIN IMMEDIATE")
    try:
        conn.execute(
            """
            UPDATE outline_plan_revisions
            SET status = 'ready_for_review', digest = ?, sealed_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (draft_digest, draft.id),
        )
        with pytest.raises(sqlite3.IntegrityError, match="complete projection bindings"):
            conn.execute(
                """
                UPDATE outline_planning_heads
                SET active_plan_revision_id = ?, active_plan_digest = ?,
                    working_plan_revision_id = NULL,
                    authority_generation = ?, projection_generation = ?
                WHERE novel_id = 'novel-1'
                """,
                (
                    draft.id,
                    draft_digest,
                    before["authority_generation"] + 1,
                    before["projection_generation"] + 1,
                ),
            )
    finally:
        if conn.in_transaction:
            conn.rollback()

    assert _head_snapshot(database) == before


def test_manifest_head_allows_valid_binding_checked_generation_update(manifest_book):
    database, _, _, _ = manifest_book
    before = _head_snapshot(database)
    conn = database.get_connection()
    conn.execute(
        """
        UPDATE outline_planning_heads
        SET authority_generation = ?, projection_generation = ?
        WHERE novel_id = 'novel-1'
        """,
        (before["authority_generation"] + 1, before["projection_generation"] + 1),
    )
    conn.commit()

    assert _head_snapshot(database) == {
        **before,
        "authority_generation": before["authority_generation"] + 1,
        "projection_generation": before["projection_generation"] + 1,
    }


def test_atomic_writer_exposes_no_capability_or_transaction_callback(manifest_book):
    database, repository, _, plan_id = manifest_book
    writer = PlanProjectionWriter(repository)

    assert callable(writer.apply_atomic)
    assert not hasattr(writer, "projection_transaction")
    assert not hasattr(writer, "capability_for")
    assert not hasattr(writer, "activate_head")
    assert not hasattr(writer, "save_sync")
    assert not hasattr(writer, "repository")

    from infrastructure.persistence.database.planning_authority_guard import (
        ProjectionWriteCapability,
    )

    with pytest.raises(TypeError, match="private"):
        ProjectionWriteCapability(
            novel_id="novel-1",
            plan_revision_id=plan_id,
            authority_generation=1,
            connection_identity=id(database.get_connection()),
        )
    with pytest.raises(PlanningAuthorityError, match="legacy StoryNode"):
        repository.save_sync(_node())


def test_projection_capability_is_transaction_bound_and_private(manifest_book):
    database, repository, _, plan_id = manifest_book
    head = _head_snapshot(database)
    writer = PlanProjectionWriter(repository)
    with writer._projection_transaction(
        novel_id="novel-1",
        plan_revision_id=plan_id,
        operation="restore",
        expected_active_plan_revision_id=head["plan_id"],
        expected_active_plan_digest=head["digest"],
        expected_authority_generation=head["authority_generation"],
        expected_projection_generation=head["projection_generation"],
    ) as capability:
        repository.save_sync(_node("capability-node"), _capability=capability)
        other_connection = sqlite3.connect(database.db_path)
        other_connection.row_factory = sqlite3.Row
        try:
            with pytest.raises(PlanningAuthorityError, match="out of scope"):
                assert_story_node_write_allowed(
                    other_connection,
                    "novel-1",
                    operation="cross_connection",
                    capability=capability,
                )
        finally:
            other_connection.close()
        writer._activate_head(capability)

    with pytest.raises(PlanningAuthorityError, match="expired"):
        repository.save_sync(_node("post-commit-node"), _capability=capability)


def test_projection_capability_rejects_commit_rebegin_reuse(manifest_book):
    database, repository, _, plan_id = manifest_book
    head = _head_snapshot(database)
    writer = PlanProjectionWriter(repository)
    conn = database.get_connection()
    with pytest.raises(PlanningAuthorityError, match="transaction has ended"):
        with writer._projection_transaction(
            novel_id="novel-1",
            plan_revision_id=plan_id,
            operation="restore",
            expected_active_plan_revision_id=head["plan_id"],
            expected_active_plan_digest=head["digest"],
            expected_authority_generation=head["authority_generation"],
            expected_projection_generation=head["projection_generation"],
        ) as capability:
            conn.commit()
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("PRAGMA defer_foreign_keys = ON")
            repository.save_sync(
                _node("reused-after-commit"), _capability=capability
            )


def test_projection_transaction_rolls_back_without_designated_head_cas(manifest_book):
    database, repository, _, plan_id = manifest_book
    head = _head_snapshot(database)
    writer = PlanProjectionWriter(repository)

    with pytest.raises(PlanningAuthorityError, match="must activate"):
        with writer._projection_transaction(
            novel_id="novel-1",
            plan_revision_id=plan_id,
            operation="restore",
            expected_active_plan_revision_id=head["plan_id"],
            expected_active_plan_digest=head["digest"],
            expected_authority_generation=head["authority_generation"],
            expected_projection_generation=head["projection_generation"],
        ) as capability:
            repository.save_sync(_node("rollback-without-head"), _capability=capability)

    assert _head_snapshot(database) == head
    assert asyncio.run(repository.get_by_id("rollback-without-head")) is None


def test_atomic_projection_rejects_empty_caller_batch_before_head_cas(manifest_book):
    database, repository, _, plan_id = manifest_book
    writer = PlanProjectionWriter(repository)
    head = _head_snapshot(database)

    with pytest.raises(PlanningAuthorityError, match="caller-supplied"):
        _apply_atomic(
            writer,
            database,
            plan_id,
            operation="restore",
            creates=(),
            updates=(),
            deletes=(),
        )
    assert _head_snapshot(database) == head


def test_atomic_projection_rejects_a_sealed_non_active_plan_before_projection_dml(
    manifest_book,
):
    database, repository, _, _ = manifest_book
    replacement = "historical-plan"
    _insert_sealed_plan(database, replacement, digest="historical-digest")
    writer = PlanProjectionWriter(repository)

    with pytest.raises(PlanningAuthorityError, match="caller-supplied"):
        _apply_atomic(
            writer,
            database,
            replacement,
            operation="projection",
            creates=(_node("historical-projection"),),
        )

    assert asyncio.run(repository.get_by_id("historical-projection")) is None


def test_atomic_projection_rejects_every_caller_batch_before_node_writes(
    manifest_book,
):
    database, repository, _, plan_id = manifest_book
    writer = PlanProjectionWriter(repository)
    first = _node("first-part")
    second = _node("duplicate-part")
    second.number = first.number
    second.order_index = 1

    with pytest.raises(PlanningAuthorityError, match="caller-supplied"):
        _apply_atomic(
            writer,
            database,
            plan_id,
            operation="projection",
            creates=(first, second),
        )

    fresh = DatabaseConnection(database.db_path).get_connection()
    assert fresh.execute(
        "SELECT COUNT(*) FROM story_nodes WHERE id IN ('first-part', 'duplicate-part')"
    ).fetchone()[0] == 0


def test_atomic_publish_rejects_caller_batch_before_head_cas(manifest_book):
    database, repository, _, _ = manifest_book
    replacement = _head_snapshot(database)["plan_id"]
    before = _head_snapshot(database)
    writer = PlanProjectionWriter(repository)

    with pytest.raises(PlanningAuthorityError, match="caller-supplied"):
        _apply_atomic(
            writer,
            database,
            replacement,
            operation="publish",
            creates=(_node("publish-rollback"),),
        )

    fresh = DatabaseConnection(database.db_path).get_connection()
    assert fresh.execute(
        "SELECT COUNT(*) FROM story_nodes WHERE id = 'publish-rollback'"
    ).fetchone()[0] == 0
    assert _head_snapshot(database) == before


def test_atomic_publish_rejects_caller_batch_before_target_validation(manifest_book):
    database, repository, _, _ = manifest_book
    replacement = "publish-target"
    _insert_sealed_plan(database, replacement, digest="publish-target-digest")
    writer = PlanProjectionWriter(repository)

    before = _head_snapshot(database)
    with pytest.raises(PlanningAuthorityError, match="caller-supplied"):
        _apply_atomic(
            writer,
            database,
            replacement,
            operation="publish",
            creates=(_node("published-part"),),
        )

    fresh = DatabaseConnection(database.db_path).get_connection()
    head = fresh.execute(
        """
        SELECT authority_mode, active_plan_revision_id, active_plan_digest,
               authority_generation, projection_generation
        FROM outline_planning_heads WHERE novel_id = 'novel-1'
        """
    ).fetchone()
    assert tuple(head) == (
        before["mode"],
        before["plan_id"],
        before["digest"],
        before["authority_generation"],
        before["projection_generation"],
    )
    assert fresh.execute(
        "SELECT COUNT(*) FROM story_nodes WHERE id = 'published-part'"
    ).fetchone()[0] == 0


def test_atomic_writer_rejects_caller_batch_before_head_or_target_validation(
    manifest_book,
):
    database, repository, _, active_plan_id = manifest_book
    writer = PlanProjectionWriter(repository)

    with pytest.raises(PlanningAuthorityError, match="caller-supplied"):
        _apply_atomic(
            writer,
            database,
            active_plan_id,
            operation="publish",
            expected_authority_generation=99,
            creates=(_node("stale-head-node"),),
        )

    rejected_plan = "needs-author-decision"
    _insert_sealed_plan(
        database,
        rejected_plan,
        digest="needs-author-decision-digest",
        status="ready_for_review",
        reconciliation_status="author_decision_required",
    )
    with pytest.raises(PlanningAuthorityError, match="caller-supplied"):
        _apply_atomic(
            writer,
            database,
            rejected_plan,
            operation="publish",
            creates=(_node("non-publishable-node"),),
        )

    assert asyncio.run(repository.get_by_id("stale-head-node")) is None
    assert asyncio.run(repository.get_by_id("non-publishable-node")) is None


def test_atomic_cutover_rejects_caller_batch_without_head_switch(legacy_plan_book):
    database, repository, _, plan_id = legacy_plan_book
    writer = PlanProjectionWriter(repository)
    assert _head_snapshot(database) == {
        "mode": "legacy",
        "plan_id": None,
        "digest": "",
        "authority_generation": 0,
        "projection_generation": 0,
    }

    with pytest.raises(PlanningAuthorityError, match="caller-supplied"):
        _apply_atomic(
            writer,
            database,
            plan_id,
            operation="cutover",
            creates=(_node("cutover-part"),),
        )

    fresh = DatabaseConnection(database.db_path).get_connection()
    head = fresh.execute(
        """
        SELECT authority_mode, active_plan_revision_id, authority_generation,
               projection_generation
        FROM outline_planning_heads WHERE novel_id = 'novel-1'
        """
    ).fetchone()
    assert tuple(head) == ("legacy", None, 0, 0)
    assert fresh.execute(
        "SELECT COUNT(*) FROM story_nodes WHERE id = 'cutover-part'"
    ).fetchone()[0] == 0


def test_manifest_runtime_whitelist_does_not_allow_planning_changes(manifest_book):
    database, repository, _, _ = manifest_book
    _seed_projection_node(database, _node())
    node = asyncio.run(repository.get_by_id("part-1"))
    node.word_count = 42
    node.status = "completed"
    node.metadata = {"runtime.progress": 0.5}
    asyncio.run(repository.update(node))
    refreshed = asyncio.run(repository.get_by_id("part-1"))
    assert refreshed.title == "Part"
    assert refreshed.word_count == 42
    assert refreshed.status == "completed"
    assert refreshed.metadata["runtime.progress"] == 0.5

    node.title = "must fail"
    with pytest.raises(PlanningAuthorityError):
        asyncio.run(repository.update(node))


def test_direct_chapter_delete_and_tree_purge_are_blocked(manifest_book):
    database, repository, _, _ = manifest_book
    _seed_projection_node(database, _node())
    conn = database.get_connection()
    conn.execute(
        "INSERT INTO chapters (id, novel_id, number, title, content, status) "
        "VALUES ('chapter-1', 'novel-1', 1, 'Chapter', '', 'draft')"
    )
    conn.commit()
    chapter_repo = SqliteChapterRepository(database)
    with pytest.raises(PlanningAuthorityError, match="legacy StoryNode"):
        chapter_repo._delete_chapter_transaction_body(
            conn, "novel-1", 1, "chapter-1", "now"
        )
    with pytest.raises(PlanningAuthorityError, match="tree-driven"):
        purge_chapter_book_rows_not_matching_structure(
            repository, chapter_repo, "novel-1"
        )
    assert conn.execute(
        "SELECT COUNT(*) FROM chapters WHERE id = 'chapter-1'"
    ).fetchone()[0] == 1


def test_recovery_cleanup_skips_manifest_story_node_preplan(manifest_book):
    database, _, _, _ = manifest_book
    conn = database.get_connection()
    conn.execute(
        "INSERT INTO story_nodes (id, novel_id, node_type, number, title, order_index, "
        "outline, metadata) VALUES ('chapter-1', 'novel-1', 'chapter', 1, 'Chapter', 0, "
        "'old outline', '{\"chapter_preplan\": {\"detail_outline\": \"transient\"}}')"
    )
    conn.execute(
        "INSERT INTO chapters (id, novel_id, number, status, content) "
        "VALUES ('chapter-row-1', 'novel-1', 1, 'draft', 'draft')"
    )
    conn.commit()
    AutopilotRecoveryPolicy(database)._discard_transient_chapter_preplan("novel-1", 1)
    row = conn.execute(
        "SELECT outline, metadata FROM story_nodes WHERE id = 'chapter-1'"
    ).fetchone()
    chapter = conn.execute(
        "SELECT content, status FROM chapters WHERE id = 'chapter-row-1'"
    ).fetchone()
    assert row[0] == "old outline"
    assert "chapter_preplan" in row[1]
    assert chapter[0] == ""
    assert chapter[1] == "draft"


def test_rewrite_invalidation_does_not_mutate_manifest_projection(manifest_book):
    database, _, _, _ = manifest_book
    conn = database.get_connection()
    conn.execute(
        "INSERT INTO story_nodes (id, novel_id, node_type, number, title, order_index, "
        "metadata) VALUES ('act-1', 'novel-1', 'act', 1, 'Act', 0, "
        "'{\"summary_status\": \"ready\"}')"
    )
    conn.commit()
    coordinator = ChapterRewriteCoordinator.__new__(ChapterRewriteCoordinator)
    coordinator._db = database
    coordinator._invalidate_story_nodes(conn, "novel-1", 1)
    metadata = conn.execute(
        "SELECT metadata FROM story_nodes WHERE id = 'act-1'"
    ).fetchone()[0]
    assert metadata == '{"summary_status": "ready"}'


def test_legacy_outline_contract_mutators_fail_closed(manifest_book):
    _, _, contracts, _ = manifest_book
    with pytest.raises(Exception, match="manifest planning authority"):
        contracts.ensure_root("novel-1")
