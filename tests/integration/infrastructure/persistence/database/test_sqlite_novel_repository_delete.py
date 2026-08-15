"""Deletion contracts for SqliteNovelRepository."""

import sqlite3

import pytest

from domain.novel.entities.novel import Novel
from domain.novel.value_objects.novel_id import NovelId
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_novel_repository import (
    SqliteNovelRepository,
)
from infrastructure.persistence.database.outline_contract_repository import (
    OutlineContractRepository,
)
from domain.structure.outline_contract import OutlinePayload, OutlineSource
from domain.structure.outline_plan import OutlinePlanItem
from infrastructure.persistence.database.write_dispatch import (
    startup_sqlite_writes_bypass_queue,
)


def _save_novel(repository: SqliteNovelRepository, novel_id: str) -> None:
    repository.save(
        Novel(
            id=NovelId(novel_id),
            title=novel_id,
            author="Author",
            target_chapters=10,
        )
    )


def _seed_novel_scoped_rows(database: DatabaseConnection, novel_id: str) -> None:
    connection = database.get_connection()
    connection.execute(
        """
        INSERT INTO bible_style_notes (id, novel_id, category, content)
        VALUES (?, ?, 'general', 'style note')
        """,
        (f"{novel_id}-style", novel_id),
    )
    connection.execute(
        """
        INSERT INTO memory_atoms (id, novel_id, entity_id)
        VALUES (?, ?, 'character-1')
        """,
        (f"{novel_id}-atom", novel_id),
    )
    connection.execute(
        """
        INSERT INTO memory_atom_links (id, novel_id, source_atom_id, target_atom_id)
        VALUES (?, ?, ?, ?)
        """,
        (
            f"{novel_id}-atom-link",
            novel_id,
            f"{novel_id}-atom",
            f"{novel_id}-atom",
        ),
    )
    connection.execute(
        """
        INSERT INTO memory_projections (novel_id, entity_id, projection_type)
        VALUES (?, 'character-1', 'character')
        """,
        (novel_id,),
    )
    connection.execute(
        """
        INSERT INTO anti_ai_audits (audit_id, novel_id, chapter_number)
        VALUES (?, ?, 1)
        """,
        (f"{novel_id}-audit", novel_id),
    )
    connection.commit()


def test_delete_removes_all_novel_scoped_rows_even_without_legacy_fk_enforcement(
    tmp_path,
):
    """A deleted novel must not leave globally keyed Bible or memory rows behind."""
    database = DatabaseConnection(str(tmp_path / "delete-novel.db"))
    repository = SqliteNovelRepository(database)
    deleted_novel_id = "novel-delete"
    retained_novel_id = "novel-retain"

    with startup_sqlite_writes_bypass_queue():
        _save_novel(repository, deleted_novel_id)
        _save_novel(repository, retained_novel_id)
        _seed_novel_scoped_rows(database, deleted_novel_id)
        _seed_novel_scoped_rows(database, retained_novel_id)

        # Existing databases can have been written by a legacy connection
        # without SQLite FK enforcement. Deletion must still be complete.
        connection = database.get_connection()
        connection.execute("PRAGMA foreign_keys = OFF")
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 0

        repository.delete(NovelId(deleted_novel_id))

    for table, novel_id_column in (
        ("novels", "id"),
        ("bible_style_notes", "novel_id"),
        ("memory_atoms", "novel_id"),
        ("memory_atom_links", "novel_id"),
        ("memory_projections", "novel_id"),
        ("anti_ai_audits", "novel_id"),
    ):
        deleted_count = database.fetch_one(
            f'SELECT COUNT(*) AS count FROM "{table}" WHERE "{novel_id_column}" = ?',
            (deleted_novel_id,),
        )["count"]
        retained_count = database.fetch_one(
            f'SELECT COUNT(*) AS count FROM "{table}" WHERE "{novel_id_column}" = ?',
            (retained_novel_id,),
        )["count"]
        assert deleted_count == 0, table
        assert retained_count == 1, table


def test_delete_manifest_book_removes_sealed_plan_history_without_orphans(tmp_path):
    database = DatabaseConnection(str(tmp_path / "delete-manifest-novel.db"))
    novels = SqliteNovelRepository(database)
    outlines = OutlineContractRepository(database)
    novel_id = "novel-manifest-delete"

    with startup_sqlite_writes_bypass_queue():
        _save_novel(novels, novel_id)
        root = outlines.ensure_root(novel_id)
        draft = outlines.save_draft(
            root.id,
            OutlinePayload(
                title="总纲",
                narrative_text="完整规划",
                creative_goal="完成全书目标",
                entry_state="开始",
                exit_state="结束",
            ),
            source=OutlineSource.AUTHOR,
        )
        outlines.publish_and_sync(root.id, expected_revision=draft.draft.revision)
        backfill = outlines.backfill_initial_plan(novel_id)
        connection = database.get_connection()
        connection.execute(
            """
            UPDATE outline_planning_heads
            SET authority_mode = 'manifest', authority_generation = 1,
                projection_generation = 1
            WHERE novel_id = ?
            """,
            (novel_id,),
        )
        connection.commit()

        novels.delete(NovelId(novel_id))

    assert backfill.plan is not None
    assert database.fetch_one(
        "SELECT COUNT(*) AS count FROM outline_planning_heads WHERE novel_id = ?",
        (novel_id,),
    )["count"] == 0
    assert database.fetch_one(
        "SELECT COUNT(*) AS count FROM outline_plan_revisions WHERE novel_id = ?",
        (novel_id,),
    )["count"] == 0
    assert database.fetch_one(
        "SELECT COUNT(*) AS count FROM outline_plan_revision_items "
        "WHERE plan_revision_id = ?",
        (backfill.plan.id,),
    )["count"] == 0
    assert database.fetch_one(
        "SELECT COUNT(*) AS count FROM outline_contract_versions WHERE contract_id = ?",
        (root.id,),
    )["count"] == 0


def test_controlled_delete_handles_manifest_replan_parent_chain(tmp_path):
    """A public full-book delete may clear r2 -> r1 only inside its own TXN."""

    database = DatabaseConnection(str(tmp_path / "delete-manifest-chain.db"))
    novels = SqliteNovelRepository(database)
    outlines = OutlineContractRepository(database)
    novel_id = "novel-manifest-chain"

    with startup_sqlite_writes_bypass_queue():
        _save_novel(novels, novel_id)
        root = outlines.ensure_root(novel_id)
        first_draft = outlines.save_draft(
            root.id,
            OutlinePayload(
                title="总纲 r1",
                narrative_text="第一版未来规划。",
                creative_goal="完成第一版目标",
                entry_state="开始",
                exit_state="结束",
            ),
            source=OutlineSource.AUTHOR,
        )
        outlines.publish_and_sync(root.id, expected_revision=first_draft.draft.revision)
        first_plan = outlines.backfill_initial_plan(novel_id).plan
        assert first_plan is not None

        second_draft = outlines.save_draft(
            root.id,
            OutlinePayload(
                title="总纲 r2",
                narrative_text="替换后的未来规划。",
                creative_goal="完成第二版目标",
                entry_state="开始",
                exit_state="新的结束",
            ),
            source=OutlineSource.AUTHOR,
        )
        conn = database.get_connection()
        version = conn.execute(
            """
            SELECT version.id, version.digest
            FROM outline_contracts AS contract
            JOIN outline_contract_versions AS version
              ON version.id = contract.draft_version_id
            WHERE contract.id = ?
            """,
            (root.id,),
        ).fetchone()
        second_plan = outlines.create_plan_draft(
            novel_id=novel_id,
            items=(
                OutlinePlanItem(
                    logical_node_id=root.id,
                    version_id=str(version["id"]),
                    version_digest=str(version["digest"]),
                    level=root.level,
                    sibling_index=0,
                ),
            ),
            canonical_prefix_digest="",
            canonical_boundary={"formal_head": 0},
            parent_plan_revision_id=first_plan.id,
        )
        second_plan = outlines.seal_plan_revision(second_plan.id)
        conn.execute(
            """
            UPDATE outline_planning_heads
            SET authority_mode = 'manifest', authority_generation = 1,
                projection_generation = 1
            WHERE novel_id = ?
            """,
            (novel_id,),
        )
        conn.execute(
            """
            UPDATE outline_planning_heads
            SET active_plan_revision_id = ?, active_plan_digest = ?,
                authority_generation = 2, projection_generation = 2
            WHERE novel_id = ?
            """,
            (second_plan.id, second_plan.digest, novel_id),
        )
        conn.commit()

        with pytest.raises(sqlite3.IntegrityError):
            conn.execute("DELETE FROM novels WHERE id = ?", (novel_id,))
        conn.rollback()

        novels.delete(NovelId(novel_id))

    checks = {
        "novels": "SELECT COUNT(*) AS count FROM novels WHERE id = ?",
        "outline_planning_heads": (
            "SELECT COUNT(*) AS count FROM outline_planning_heads WHERE novel_id = ?"
        ),
        "outline_plan_revisions": (
            "SELECT COUNT(*) AS count FROM outline_plan_revisions WHERE novel_id = ?"
        ),
        "outline_plan_revision_items": (
            "SELECT COUNT(*) AS count FROM outline_plan_revision_items AS item "
            "JOIN outline_plan_revisions AS revision "
            "ON revision.id = item.plan_revision_id WHERE revision.novel_id = ?"
        ),
        "outline_contract_versions": (
            "SELECT COUNT(*) AS count FROM outline_contract_versions AS version "
            "JOIN outline_contracts AS contract ON contract.id = version.contract_id "
            "WHERE contract.novel_id = ?"
        ),
        "outline_contracts": (
            "SELECT COUNT(*) AS count FROM outline_contracts WHERE novel_id = ?"
        ),
    }
    for table, sql in checks.items():
        assert database.fetch_one(sql, (novel_id,))["count"] == 0, table
