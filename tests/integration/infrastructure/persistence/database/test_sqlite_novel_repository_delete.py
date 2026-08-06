"""Deletion contracts for SqliteNovelRepository."""

from domain.novel.entities.novel import Novel
from domain.novel.value_objects.novel_id import NovelId
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_novel_repository import (
    SqliteNovelRepository,
)
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
