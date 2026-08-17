"""Caller-supplied StoryNode projection batches are unsafe until declared."""

from __future__ import annotations

import asyncio

import pytest

from domain.structure.outline_contract import OutlinePayload
from domain.structure.outline_plan import OutlinePlanItem
from domain.structure.story_node import NodeType, StoryNode
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.outline_contract_repository import (
    OutlineContractRepository,
)
from infrastructure.persistence.database.plan_projection_writer import PlanProjectionWriter
from infrastructure.persistence.database.planning_authority_guard import (
    PlanningAuthorityError,
)
from infrastructure.persistence.database.story_node_repository import StoryNodeRepository


def _sealed_plan(tmp_path, *, manifest: bool) -> tuple[
    DatabaseConnection, StoryNodeRepository, str
]:
    database = DatabaseConnection(str(tmp_path / "projection-disabled.db"))
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
    plan = contracts.create_plan_draft(
        novel_id="novel-1",
        items=(
            OutlinePlanItem(
                logical_node_id=published.id,
                version_id=str(row[0]),
                version_digest=str(row[1]),
                level=published.level,
                sibling_index=0,
            ),
        ),
        canonical_prefix_digest="",
        canonical_boundary={"formal_head": 0},
    )
    plan = contracts.seal_plan_revision(plan.id)
    if manifest:
        conn.execute(
            "UPDATE outline_planning_heads SET authority_mode = 'manifest', "
            "authority_generation = 1, projection_generation = 1, "
            "active_plan_revision_id = ?, active_plan_digest = ? "
            "WHERE novel_id = ?",
            (plan.id, plan.digest, "novel-1"),
        )
        conn.commit()
    return database, StoryNodeRepository(database), plan.id


def _part() -> StoryNode:
    return StoryNode(
        id="part-1",
        novel_id="novel-1",
        node_type=NodeType.PART,
        number=1,
        title="Part",
        order_index=0,
    )


def _head(database: DatabaseConnection) -> tuple[object, ...]:
    row = database.get_connection().execute(
        """
        SELECT authority_mode, active_plan_revision_id, active_plan_digest,
               authority_generation, projection_generation
        FROM outline_planning_heads WHERE novel_id = 'novel-1'
        """
    ).fetchone()
    return tuple(row)


def _seal_working_revision(
    contracts: OutlineContractRepository,
    conn,
    plan_revision_id: str,
):  # type: ignore[no-untyped-def]
    conn.execute("BEGIN IMMEDIATE")
    conn.execute(
        """
        UPDATE outline_contract_versions
        SET sealed_at = CURRENT_TIMESTAMP
        WHERE sealed_at IS NULL
          AND id IN (
            SELECT version_id FROM outline_plan_revision_items
            WHERE plan_revision_id = ?
        )
        """,
        (plan_revision_id,),
    )
    conn.execute(
        """
        UPDATE outline_plan_revisions
        SET status = 'ready_for_review', sealed_at = CURRENT_TIMESTAMP,
            updated_at = CURRENT_TIMESTAMP
        WHERE id = ? AND sealed_at IS NULL
        """,
        (plan_revision_id,),
    )
    return contracts.get_plan_revision(plan_revision_id, _connection=conn)


def _working_target(
    tmp_path,
    *,
    physical_number: int = 1,
    physical_order_index: int = 0,
):  # type: ignore[no-untyped-def]
    database, nodes, active_plan_id = _sealed_plan(tmp_path, manifest=True)
    contracts = OutlineContractRepository(database)
    draft = contracts.clone_active_plan_draft("novel-1")
    root = next(item for item in draft.items if item.level.value == "outline")
    expanded = contracts.replace_draft_cohort_payloads(
        plan_revision_id=draft.id,
        parent_logical_node_id=root.logical_node_id,
        payloads=(
            OutlinePayload(
                title="Part one",
                narrative_text="The protagonist leaves home and loses support.",
                creative_goal="Force an irreversible choice",
                entry_state="The old order still holds",
                exit_state="The protagonist loses support",
                chapter_start=1,
                chapter_end=10,
            ),
        ),
    )
    conn = database.get_connection()
    conn.execute(
        """
        UPDATE outline_plan_projection_bindings
        SET number = ?, order_index = ?
        WHERE plan_revision_item_id IN (
            SELECT id FROM outline_plan_revision_items
            WHERE plan_revision_id = ? AND level = 'part'
        )
        """,
        (physical_number, physical_order_index, expanded.id),
    )
    conn.commit()
    target = _seal_working_revision(contracts, conn, expanded.id)
    part = next(item for item in target.items if item.level.value == "part")
    binding = conn.execute(
        """
        SELECT binding.story_node_id
        FROM outline_plan_projection_bindings AS binding
        JOIN outline_plan_revision_items AS item
          ON item.id = binding.plan_revision_item_id
        WHERE item.plan_revision_id = ? AND item.logical_node_id = ?
        """,
        (target.id, part.logical_node_id),
    ).fetchone()
    assert binding is not None and binding[0]
    return database, nodes, active_plan_id, target, str(binding[0]), conn


def _working_full_chain(tmp_path):  # type: ignore[no-untyped-def]
    database, nodes, active_plan_id = _sealed_plan(tmp_path, manifest=True)
    contracts = OutlineContractRepository(database)
    draft = contracts.clone_active_plan_draft("novel-1")
    parent = next(item for item in draft.items if item.level.value == "outline")
    expanded = draft
    for title in ("Part", "Volume", "Act", "Chapter"):
        expanded = contracts.replace_draft_cohort_payloads(
            plan_revision_id=expanded.id,
            parent_logical_node_id=parent.logical_node_id,
            payloads=(
                OutlinePayload(
                    title=title,
                    narrative_text=f"{title} narrative",
                    creative_goal=f"{title} goal",
                    entry_state=f"{title} entry",
                    exit_state=f"{title} exit",
                    chapter_start=1,
                    chapter_end=1,
                ),
            ),
        )
        parent = next(
            item
            for item in expanded.items
            if item.parent_logical_node_id == parent.logical_node_id
        )
    conn = database.get_connection()
    target = _seal_working_revision(contracts, conn, expanded.id)
    chapter = next(item for item in target.items if item.level.value == "chapter")
    binding = conn.execute(
        """
        SELECT binding.story_node_id
        FROM outline_plan_projection_bindings AS binding
        JOIN outline_plan_revision_items AS item
          ON item.id = binding.plan_revision_item_id
        WHERE item.plan_revision_id = ? AND item.logical_node_id = ?
        """,
        (target.id, chapter.logical_node_id),
    ).fetchone()
    assert binding is not None and binding[0]
    return database, nodes, active_plan_id, target, chapter, str(binding[0]), conn


class _HostileRepository(StoryNodeRepository):
    def __init__(self, database: DatabaseConnection) -> None:
        super().__init__(database)
        self.save_batch_calls = 0

    async def save_batch(self, nodes, *, _capability=None):  # type: ignore[no-untyped-def]
        self.save_batch_calls += 1
        return await super().save_batch(nodes, _capability=_capability)


def test_projection_writer_rejects_publish_batch_before_repository_dml(tmp_path):
    database, _, plan_id = _sealed_plan(tmp_path, manifest=True)
    repository = _HostileRepository(database)
    writer = PlanProjectionWriter(repository)
    before = _head(database)

    with pytest.raises(PlanningAuthorityError, match="caller-supplied"):
        asyncio.run(
            writer.apply_atomic(
                novel_id="novel-1",
                plan_revision_id=plan_id,
                operation="publish",
                expected_active_plan_revision_id=plan_id,
                expected_active_plan_digest=str(before[2]),
                expected_authority_generation=int(before[3]),
                expected_projection_generation=int(before[4]),
                creates=(_part(),),
            )
        )

    assert repository.save_batch_calls == 0
    assert database.get_connection().execute(
        "SELECT COUNT(*) FROM story_nodes WHERE id = 'part-1'"
    ).fetchone()[0] == 0
    assert _head(database) == before


def test_projection_writer_rejects_empty_cutover_without_switching_head(tmp_path):
    database, repository, plan_id = _sealed_plan(tmp_path, manifest=False)
    writer = PlanProjectionWriter(repository)
    before = _head(database)

    with pytest.raises(PlanningAuthorityError, match="caller-supplied"):
        asyncio.run(
            writer.apply_atomic(
                novel_id="novel-1",
                plan_revision_id=plan_id,
                operation="cutover",
                expected_active_plan_revision_id=None,
                expected_active_plan_digest="",
                expected_authority_generation=0,
                expected_projection_generation=0,
            )
        )

    assert _head(database) == before


def test_projection_permit_helpers_require_a_clean_connection(
    tmp_path,
):
    database, _, _ = _sealed_plan(tmp_path, manifest=True)
    conn = database.get_connection()
    from infrastructure.persistence.database.planning_authority_guard import (
        _begin_projection_writer_session,
    )

    conn.execute("BEGIN IMMEDIATE")
    try:
        with pytest.raises(PlanningAuthorityError, match="clean connection"):
            _begin_projection_writer_session(conn)
    finally:
        if conn.in_transaction:
            conn.rollback()


@pytest.mark.asyncio
async def test_bound_projection_materializes_declared_nodes_then_cas_activates_head(tmp_path):
    """The writer derives projection DML only from the frozen target bindings."""

    database, nodes, active_plan_id, target, binding_id, conn = _working_target(tmp_path)
    before = _head(database)
    statements: list[str] = []
    conn.set_trace_callback(statements.append)

    try:
        await PlanProjectionWriter(nodes).apply_bound_projection(
            conn,
            novel_id="novel-1",
            plan_revision_id=target.id,
            expected_active_plan_revision_id=active_plan_id,
            expected_active_plan_digest=str(before[2]),
            expected_authority_generation=int(before[3]),
            expected_projection_generation=int(before[4]),
            expected_working_plan_revision_id=target.id,
        )
    finally:
        conn.set_trace_callback(None)
    assert conn.in_transaction
    assert not any(
        statement.lstrip().upper().startswith(
            ("BEGIN", "COMMIT", "SAVEPOINT", "RELEASE")
        )
        for statement in statements
    )
    conn.commit()

    node = await nodes.get_by_id(binding_id)
    assert node is not None
    assert node.title == "Part one"
    assert node.parent_id is None
    assert node.number == 1
    assert node.order_index == 0
    assert node.metadata["manifest.version_digest"]
    assert _head(database) == (
        "manifest",
        target.id,
        target.digest,
        int(before[3]) + 1,
        int(before[4]) + 1,
    )


@pytest.mark.asyncio
async def test_bound_projection_rejects_an_incomplete_sealed_binding_set(tmp_path):
    database, nodes, active_plan_id, target, binding_id, conn = _working_target(tmp_path)
    before = _head(database)
    conn.execute("DROP TRIGGER trg_outline_plan_projection_bindings_sealed_delete")
    conn.execute(
        "DELETE FROM outline_plan_projection_bindings WHERE story_node_id = ?",
        (binding_id,),
    )

    with pytest.raises(PlanningAuthorityError, match="binding"):
        await PlanProjectionWriter(nodes).apply_bound_projection(
            conn,
            novel_id="novel-1",
            plan_revision_id=target.id,
            expected_active_plan_revision_id=active_plan_id,
            expected_active_plan_digest=str(before[2]),
            expected_authority_generation=int(before[3]),
            expected_projection_generation=int(before[4]),
            expected_working_plan_revision_id=target.id,
        )

    assert not conn.in_transaction
    assert _head(database) == before
    assert await nodes.get_by_id(binding_id) is None


@pytest.mark.asyncio
async def test_bound_projection_rejects_a_cross_novel_node_using_a_declared_id(tmp_path):
    database, nodes, active_plan_id, target, binding_id, conn = _working_target(tmp_path)
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("novel-2", "Other", "other", 1),
    )
    conn.execute(
        """
        INSERT INTO story_nodes
            (id, novel_id, parent_id, node_type, number, title, order_index)
        VALUES (?, 'novel-2', NULL, 'part', 1, 'Forged', 0)
        """,
        (binding_id,),
    )
    conn.commit()
    before = _head(database)
    conn.execute("BEGIN IMMEDIATE")

    with pytest.raises(PlanningAuthorityError, match="cross-novel|forged"):
        await PlanProjectionWriter(nodes).apply_bound_projection(
            conn,
            novel_id="novel-1",
            plan_revision_id=target.id,
            expected_active_plan_revision_id=active_plan_id,
            expected_active_plan_digest=str(before[2]),
            expected_authority_generation=int(before[3]),
            expected_projection_generation=int(before[4]),
            expected_working_plan_revision_id=target.id,
        )

    forged = conn.execute(
        "SELECT novel_id, title FROM story_nodes WHERE id = ?", (binding_id,)
    ).fetchone()
    assert tuple(forged) == ("novel-2", "Forged")
    assert _head(database) == before


class _TransactionReplacingRepository(StoryNodeRepository):
    replaced_transaction = False

    async def save_batch(self, nodes, *, _capability=None):  # type: ignore[no-untyped-def]
        result = await super().save_batch(nodes, _capability=_capability)
        conn = self._get_connection()
        conn.commit()
        conn.execute("BEGIN IMMEDIATE")
        self.replaced_transaction = True
        return result


@pytest.mark.asyncio
async def test_bound_projection_rolls_back_node_dml_when_head_cas_loses(tmp_path):
    database, _, active_plan_id, target, binding_id, conn = _working_target(tmp_path)
    conn.commit()
    before = tuple(
        conn.execute(
            "SELECT active_plan_revision_id, active_plan_digest, authority_generation, "
            "projection_generation, working_plan_revision_id "
            "FROM outline_planning_heads WHERE novel_id = 'novel-1'"
        ).fetchone()
    )
    conn.execute(
        """
        CREATE TRIGGER inject_projection_head_race
        AFTER INSERT ON story_nodes
        WHEN NEW.novel_id = 'novel-1'
        BEGIN
            UPDATE outline_planning_heads
            SET working_plan_revision_id = NULL
            WHERE novel_id = NEW.novel_id;
        END
        """
    )
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")

    with pytest.raises(PlanningAuthorityError, match="Head changed"):
        await PlanProjectionWriter(StoryNodeRepository(database)).apply_bound_projection(
            conn,
            novel_id="novel-1",
            plan_revision_id=target.id,
            expected_active_plan_revision_id=active_plan_id,
            expected_active_plan_digest=str(before[1]),
            expected_authority_generation=int(before[2]),
            expected_projection_generation=int(before[3]),
            expected_working_plan_revision_id=target.id,
        )

    assert not conn.in_transaction
    assert await StoryNodeRepository(database).get_by_id(binding_id) is None
    after = tuple(
        conn.execute(
            "SELECT active_plan_revision_id, active_plan_digest, authority_generation, "
            "projection_generation, working_plan_revision_id "
            "FROM outline_planning_heads WHERE novel_id = 'novel-1'"
        ).fetchone()
    )
    assert after == before


@pytest.mark.asyncio
async def test_bound_projection_does_not_delegate_dml_to_a_transaction_replacing_repository(
    tmp_path,
):
    database, _, active_plan_id, target, binding_id, conn = _working_target(tmp_path)
    before = _head(database)
    repository = _TransactionReplacingRepository(database)

    await PlanProjectionWriter(repository).apply_bound_projection(
        conn,
        novel_id="novel-1",
        plan_revision_id=target.id,
        expected_active_plan_revision_id=active_plan_id,
        expected_active_plan_digest=str(before[2]),
        expected_authority_generation=int(before[3]),
        expected_projection_generation=int(before[4]),
        expected_working_plan_revision_id=target.id,
    )
    conn.rollback()

    assert repository.replaced_transaction is False
    assert await StoryNodeRepository(database).get_by_id(binding_id) is None
    assert _head(database) == before


@pytest.mark.asyncio
async def test_bound_projection_recomputes_the_sealed_plan_digest(tmp_path):
    database, nodes, active_plan_id, target, _, conn = _working_target(tmp_path)
    conn.execute("DROP TRIGGER trg_outline_plan_revisions_sealed_update")
    conn.execute(
        "UPDATE outline_plan_revisions SET digest = ? WHERE id = ?",
        ("f" * 64, target.id),
    )
    conn.commit()
    before = _head(database)
    conn.execute("BEGIN IMMEDIATE")

    try:
        with pytest.raises(PlanningAuthorityError, match="digest"):
            await PlanProjectionWriter(nodes).apply_bound_projection(
                conn,
                novel_id="novel-1",
                plan_revision_id=target.id,
                expected_active_plan_revision_id=active_plan_id,
                expected_active_plan_digest=str(before[2]),
                expected_authority_generation=int(before[3]),
                expected_projection_generation=int(before[4]),
                expected_working_plan_revision_id=target.id,
            )
    finally:
        if conn.in_transaction:
            conn.rollback()

    assert _head(database) == before


@pytest.mark.asyncio
async def test_bound_projection_preserves_frozen_nonlocal_physical_coordinates(tmp_path):
    database, nodes, active_plan_id, target, binding_id, conn = _working_target(
        tmp_path,
        physical_number=7,
        physical_order_index=4,
    )
    before = _head(database)

    await PlanProjectionWriter(nodes).apply_bound_projection(
        conn,
        novel_id="novel-1",
        plan_revision_id=target.id,
        expected_active_plan_revision_id=active_plan_id,
        expected_active_plan_digest=str(before[2]),
        expected_authority_generation=int(before[3]),
        expected_projection_generation=int(before[4]),
        expected_working_plan_revision_id=target.id,
    )
    conn.commit()

    node = await nodes.get_by_id(binding_id)
    assert node is not None
    assert (node.number, node.order_index) == (7, 4)


@pytest.mark.asyncio
async def test_bound_projection_rejects_an_extra_node_created_before_head_cas(tmp_path):
    database, _, active_plan_id, target, binding_id, conn = _working_target(tmp_path)
    conn.commit()
    before = _head(database)
    conn.execute(
        """
        CREATE TRIGGER inject_extra_projection_node
        AFTER INSERT ON story_nodes
        WHEN NEW.novel_id = 'novel-1' AND NEW.id <> 'extra-node'
        BEGIN
            INSERT INTO story_nodes
                (id, novel_id, parent_id, node_type, number, title, order_index)
            VALUES ('extra-node', NEW.novel_id, NULL, 'chapter', 99, 'Extra', 98);
        END
        """
    )
    conn.commit()
    conn.execute("BEGIN IMMEDIATE")

    try:
        with pytest.raises(PlanningAuthorityError, match="extra"):
            await PlanProjectionWriter(StoryNodeRepository(database)).apply_bound_projection(
                conn,
                novel_id="novel-1",
                plan_revision_id=target.id,
                expected_active_plan_revision_id=active_plan_id,
                expected_active_plan_digest=str(before[2]),
                expected_authority_generation=int(before[3]),
                expected_projection_generation=int(before[4]),
                expected_working_plan_revision_id=target.id,
            )
    finally:
        if conn.in_transaction:
            conn.rollback()

    assert await StoryNodeRepository(database).get_by_id(binding_id) is None
    assert await StoryNodeRepository(database).get_by_id("extra-node") is None
    assert _head(database) == before


@pytest.mark.asyncio
async def test_bound_projection_rejects_deleting_a_referenced_story_node(tmp_path):
    database, nodes, active_plan_id, target, binding_id, conn = _working_target(tmp_path)
    before = _head(database)
    await PlanProjectionWriter(nodes).apply_bound_projection(
        conn,
        novel_id="novel-1",
        plan_revision_id=target.id,
        expected_active_plan_revision_id=active_plan_id,
        expected_active_plan_digest=str(before[2]),
        expected_authority_generation=int(before[3]),
        expected_projection_generation=int(before[4]),
        expected_working_plan_revision_id=target.id,
    )
    conn.commit()
    conn.execute(
        """
        INSERT INTO chapter_elements
            (id, chapter_id, element_type, element_id, relation_type)
        VALUES ('element-1', ?, 'event', 'event-1', 'appears')
        """,
        (binding_id,),
    )
    conn.commit()

    contracts = OutlineContractRepository(database)
    draft = contracts.clone_active_plan_draft("novel-1")
    part = next(item for item in draft.items if item.level.value == "part")
    replanned, _ = contracts.prepare_future_replan_draft(
        plan_revision_id=draft.id,
        changed_logical_node_id=part.logical_node_id,
    )
    replacement = _seal_working_revision(contracts, conn, replanned.id)
    before_replacement = _head(database)

    with pytest.raises(PlanningAuthorityError, match="referenced"):
        await PlanProjectionWriter(nodes).apply_bound_projection(
            conn,
            novel_id="novel-1",
            plan_revision_id=replacement.id,
            expected_active_plan_revision_id=target.id,
            expected_active_plan_digest=str(before_replacement[2]),
            expected_authority_generation=int(before_replacement[3]),
            expected_projection_generation=int(before_replacement[4]),
            expected_working_plan_revision_id=replacement.id,
        )

    assert not conn.in_transaction
    assert await nodes.get_by_id(binding_id) is not None
    assert _head(database) == before_replacement


@pytest.mark.asyncio
async def test_bound_projection_rejects_deleting_a_formal_story_node(tmp_path):
    (
        database,
        nodes,
        active_plan_id,
        target,
        chapter,
        chapter_node_id,
        conn,
    ) = _working_full_chain(tmp_path)
    before = _head(database)
    await PlanProjectionWriter(nodes).apply_bound_projection(
        conn,
        novel_id="novel-1",
        plan_revision_id=target.id,
        expected_active_plan_revision_id=active_plan_id,
        expected_active_plan_digest=str(before[2]),
        expected_authority_generation=int(before[3]),
        expected_projection_generation=int(before[4]),
        expected_working_plan_revision_id=target.id,
    )
    conn.commit()
    conn.execute(
        """
        INSERT INTO chapters
            (id, novel_id, number, title, content, content_sha256, content_revision)
        VALUES ('formal-chapter-1', 'novel-1', 1, 'Chapter', 'Approved prose',
                'formal-digest', 1)
        """
    )
    conn.execute(
        """
        INSERT INTO pre_candidate_formal_history
            (novel_id, chapter_number, chapter_id, content_sha256, content_revision)
        VALUES ('novel-1', 1, 'formal-chapter-1', 'formal-digest', 1)
        """
    )
    conn.commit()

    contracts = OutlineContractRepository(database)
    draft = contracts.clone_active_plan_draft("novel-1")
    replanned, _ = contracts.prepare_future_replan_draft(
        plan_revision_id=draft.id,
        changed_logical_node_id=chapter.logical_node_id,
    )
    replacement = _seal_working_revision(contracts, conn, replanned.id)
    before_replacement = _head(database)

    with pytest.raises(PlanningAuthorityError, match="Formal"):
        await PlanProjectionWriter(nodes).apply_bound_projection(
            conn,
            novel_id="novel-1",
            plan_revision_id=replacement.id,
            expected_active_plan_revision_id=target.id,
            expected_active_plan_digest=str(before_replacement[2]),
            expected_authority_generation=int(before_replacement[3]),
            expected_projection_generation=int(before_replacement[4]),
            expected_working_plan_revision_id=replacement.id,
        )

    assert not conn.in_transaction
    assert await nodes.get_by_id(chapter_node_id) is not None
    assert _head(database) == before_replacement


@pytest.mark.asyncio
async def test_bound_projection_restores_a_sealed_revision_without_a_working_draft(
    tmp_path,
):
    database, nodes, archived_plan_id, current, binding_id, conn = _working_target(
        tmp_path
    )
    before_publish = _head(database)
    await PlanProjectionWriter(nodes).apply_bound_projection(
        conn,
        novel_id="novel-1",
        plan_revision_id=current.id,
        expected_active_plan_revision_id=archived_plan_id,
        expected_active_plan_digest=str(before_publish[2]),
        expected_authority_generation=int(before_publish[3]),
        expected_projection_generation=int(before_publish[4]),
        expected_working_plan_revision_id=current.id,
    )
    conn.commit()
    before_restore = _head(database)
    archived_digest = str(
        conn.execute(
            "SELECT digest FROM outline_plan_revisions WHERE id = ?",
            (archived_plan_id,),
        ).fetchone()[0]
    )
    conn.execute("BEGIN IMMEDIATE")

    await PlanProjectionWriter(nodes).apply_bound_projection(
        conn,
        novel_id="novel-1",
        plan_revision_id=archived_plan_id,
        operation="restore",
        expected_active_plan_revision_id=current.id,
        expected_active_plan_digest=str(before_restore[2]),
        expected_authority_generation=int(before_restore[3]),
        expected_projection_generation=int(before_restore[4]),
        expected_working_plan_revision_id=None,
    )
    assert conn.in_transaction
    conn.commit()

    assert await nodes.get_by_id(binding_id) is None
    assert _head(database) == (
        "manifest",
        archived_plan_id,
        archived_digest,
        int(before_restore[3]) + 1,
        int(before_restore[4]) + 1,
    )
