"""删除章节后事务内重排 chapter_number 并级联相关表。"""

from pathlib import Path
import sqlite3

import pytest

from domain.novel.value_objects.chapter_id import ChapterId
from domain.novel.value_objects.novel_id import NovelId
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_chapter_repository import (
    SqliteChapterRepository,
)
from infrastructure.persistence.database.write_dispatch import sqlite_writes_bypass_queue

SCHEMA_PATH = (
    Path(__file__).resolve().parents[5]
    / "infrastructure"
    / "persistence"
    / "database"
    / "schema.sql"
)


@pytest.fixture
def db():
    database = DatabaseConnection(":memory:")
    database.get_connection().executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    database.get_connection().execute("PRAGMA foreign_keys = ON")
    database.get_connection().commit()
    yield database
    database.close()


@pytest.fixture
def repo(db):
    return SqliteChapterRepository(db)


def _seed_three_chapters(db, novel_id: str = "novel-del-1"):
    db.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        (novel_id, "N", "slug-del-1", 10),
    )
    db.execute(
        "INSERT INTO knowledge (id, novel_id, version) VALUES (?, ?, ?)",
        (f"k-{novel_id}", novel_id, 1),
    )
    db.execute(
        """
        INSERT INTO storylines (
            id, novel_id, storyline_type, status,
            estimated_chapter_start, estimated_chapter_end,
            current_milestone_index, extensions
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            f"sl-{novel_id}",
            novel_id,
            "main",
            "active",
            1,
            3,
            0,
            "{}",
        ),
    )
    for num, cid in [(1, "c1"), (2, "c2"), (3, "c3")]:
        db.execute(
            """
            INSERT INTO chapters (id, novel_id, number, title, content, outline, status)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (cid, novel_id, num, f"T{num}", f"body{num}", "", "draft"),
        )
    db.execute(
        """
        INSERT INTO chapter_reviews (novel_id, chapter_number, status, memo)
        VALUES (?, ?, ?, ?), (?, ?, ?, ?), (?, ?, ?, ?)
        """,
        (
            novel_id,
            1,
            "draft",
            "",
            novel_id,
            2,
            "draft",
            "",
            novel_id,
            3,
            "draft",
            "",
        ),
    )
    db.execute(
        """
        INSERT INTO triples (
            id, novel_id, subject, predicate, object, chapter_number, first_appearance
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        ("tr-1", novel_id, "a", "b", "c", 3, 3),
    )
    db.execute(
        """
        INSERT INTO plot_arcs (id, novel_id, slug, display_name, extensions)
        VALUES (?, ?, ?, ?, ?)
        """,
        (f"pa-{novel_id}", novel_id, "default", "A", "{}"),
    )
    db.execute(
        """
        INSERT INTO plot_points (
            id, plot_arc_id, sort_order, chapter_number, point_type, description, tension
        ) VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (f"pa-{novel_id}-p-3", f"pa-{novel_id}", 0, 3, "beat", "x", 50),
    )
    db.get_connection().commit()


class _BeforeFirstDeleteDmlConnection:
    """Try a competing Formal write after the final preflight and before DML."""

    def __init__(self, connection, before_first_dml):
        self._connection = connection
        self._before_first_dml = before_first_dml
        self._called = False

    def execute(self, sql, params=()):
        if not self._called and sql.lstrip().upper().startswith("UPDATE TRIPLES"):
            self._called = True
            self._before_first_dml()
        return self._connection.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._connection, name)


def test_delete_middle_chapter_renumbers_and_cascades(db, repo):
    novel_id = "novel-del-1"
    _seed_three_chapters(db, novel_id)

    repo.delete(ChapterId("c2"))

    rows = db.fetch_all(
        "SELECT id, number FROM chapters WHERE novel_id = ? ORDER BY number",
        (novel_id,),
    )
    assert [r["number"] for r in rows] == [1, 2]
    assert {r["id"] for r in rows} == {"c1", "c3"}

    tr = db.fetch_one(
        "SELECT chapter_number, first_appearance FROM triples WHERE id = ?",
        ("tr-1",),
    )
    assert tr["chapter_number"] == 2
    assert tr["first_appearance"] == 2

    pp = db.fetch_one(
        "SELECT id, chapter_number FROM plot_points WHERE plot_arc_id = ?",
        (f"pa-{novel_id}",),
    )
    assert pp["chapter_number"] == 2
    assert pp["id"] == f"pa-{novel_id}-p-2"

    rev = db.fetch_all(
        "SELECT chapter_number FROM chapter_reviews WHERE novel_id = ? ORDER BY chapter_number",
        (novel_id,),
    )
    assert [r["chapter_number"] for r in rev] == [1, 2]

    sl = db.fetch_one(
        "SELECT estimated_chapter_end FROM storylines WHERE novel_id = ?",
        (novel_id,),
    )
    assert sl["estimated_chapter_end"] == 2


def test_delete_last_chapter_no_shift_still_adjusts_bounds(db, repo):
    novel_id = "novel-del-2"
    _seed_three_chapters(db, novel_id)

    repo.delete(ChapterId("c3"))

    rows = db.fetch_all(
        "SELECT number FROM chapters WHERE novel_id = ? ORDER BY number",
        (novel_id,),
    )
    assert [r["number"] for r in rows] == [1, 2]
    sl = db.fetch_one(
        "SELECT estimated_chapter_end FROM storylines WHERE novel_id = ?",
        (novel_id,),
    )
    assert sl["estimated_chapter_end"] == 2


def test_delete_restores_caller_foreign_key_setting(db, repo):
    """A writer delete must not change the connection's FK policy."""

    _seed_three_chapters(db)
    connection = db.get_connection()
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.commit()

    repo.execute_delete_on_writer(ChapterId("c2"))

    assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 0


def test_delete_reloads_target_identity_after_lock_when_another_delete_renumbered_it(
    tmp_path, monkeypatch
):
    """A stale pre-lock chapter number must not leave a numbering gap."""

    db_path = tmp_path / "delete-stale-number.db"
    db = DatabaseConnection(str(db_path))
    repo = SqliteChapterRepository(db)
    novel_id = "delete-stale-number"
    _seed_three_chapters(db, novel_id)

    raced = False

    def renumber_competing_delete():
        nonlocal raced
        raced = True
        competing = sqlite3.connect(str(db_path))
        try:
            competing.execute("BEGIN IMMEDIATE")
            competing.execute(
                "UPDATE chapters SET number = number + 1000000 "
                "WHERE novel_id = ? AND number > 1",
                (novel_id,),
            )
            competing.execute("DELETE FROM chapters WHERE id = 'c1'")
            competing.execute(
                "UPDATE chapters SET number = number - 1000001 "
                "WHERE novel_id = ? AND number > 1",
                (novel_id,),
            )
            competing.commit()
        finally:
            competing.close()

    real_connection = db.get_connection()

    class _RaceBeforeDeleteLock:
        def execute(self, sql, params=()):
            if not raced and sql.strip().upper() == "BEGIN IMMEDIATE":
                renumber_competing_delete()
            return real_connection.execute(sql, params)

        def __getattr__(self, name):
            return getattr(real_connection, name)

    monkeypatch.setattr(db, "get_connection", lambda: _RaceBeforeDeleteLock())
    with sqlite_writes_bypass_queue():
        repo.execute_delete_on_writer(ChapterId("c2"))

    assert raced is True
    rows = db.fetch_all(
        "SELECT id, number FROM chapters WHERE novel_id = ? ORDER BY number",
        (novel_id,),
    )
    assert [(row["id"], row["number"]) for row in rows] == [("c3", 1)]
    db.close()


def test_delete_writer_locks_before_formal_preflight(tmp_path, monkeypatch):
    """A Formal identity cannot appear after preflight but before delete DML."""

    db_path = tmp_path / "delete-formal-write-lock.db"
    db = DatabaseConnection(str(db_path))
    repo = SqliteChapterRepository(db)
    novel_id = "delete-formal-write-lock"
    _seed_three_chapters(db, novel_id)
    competing = sqlite3.connect(str(db_path), timeout=0)
    raced = False
    blocked = False

    def insert_formal_identity() -> None:
        nonlocal raced, blocked
        raced = True
        try:
            competing.execute(
                """
                INSERT INTO pre_candidate_formal_history
                    (novel_id, chapter_number, chapter_id, content_sha256, content_revision)
                VALUES (?, 2, 'c2', 'raced-formal-hash', 1)
                """,
                (novel_id,),
            )
            competing.commit()
        except sqlite3.OperationalError as exc:
            assert "locked" in str(exc).lower()
            competing.rollback()
            blocked = True

    wrapped = _BeforeFirstDeleteDmlConnection(
        db.get_connection(), insert_formal_identity
    )
    monkeypatch.setattr(db, "get_connection", lambda: wrapped)
    try:
        with sqlite_writes_bypass_queue():
            repo.execute_delete_on_writer(ChapterId("c1"))
    finally:
        competing.close()

    assert raced is True
    assert blocked is True
    assert db.fetch_one(
        "SELECT COUNT(*) AS total FROM pre_candidate_formal_history "
        "WHERE novel_id = ?",
        (novel_id,),
    )["total"] == 0


def test_delete_rejects_renumbering_a_narrative_committed_tail(tmp_path):
    """Canonical narrative evidence is Formal identity even without candidate rows."""

    db = DatabaseConnection(str(tmp_path / "protected-tail-narrative.db"))
    repo = SqliteChapterRepository(db)
    novel_id = "protected-tail-narrative"
    db.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        (novel_id, "Protected narrative tail", novel_id, 3),
    )
    for number in (1, 2):
        db.execute(
            """
            INSERT INTO chapters (id, novel_id, number, title, content, status)
            VALUES (?, ?, ?, ?, '', 'draft')
            """,
            (f"protected-narrative-tail-{number}", novel_id, number, f"Chapter {number}"),
        )
    db.execute(
        """
        INSERT INTO chapter_narrative_commits
            (novel_id, chapter_number, content_sha256, pipeline_version,
             content_revision, status, memory_status)
        VALUES (?, 2, 'narrative-tail-hash', 'test-pipeline', 1, 'committed', 'committed')
        """,
        (novel_id,),
    )
    db.get_connection().commit()

    with sqlite_writes_bypass_queue():
        with pytest.raises(ValueError, match="Formal|正式|保护"):
            repo.execute_delete_on_writer(ChapterId("protected-narrative-tail-1"))

    rows = db.fetch_all(
        "SELECT id, number FROM chapters WHERE novel_id = ? ORDER BY number", (novel_id,)
    )
    assert [(row["id"], row["number"]) for row in rows] == [
        ("protected-narrative-tail-1", 1),
        ("protected-narrative-tail-2", 2),
    ]


@pytest.mark.parametrize("identity", ("legacy", "candidate"))
def test_delete_rejects_formal_identity_before_queue_or_fk_bypass(
    tmp_path, monkeypatch, identity
):
    """The writer must not make Formal provenance deletable by disabling FKs."""

    db = DatabaseConnection(str(tmp_path / f"protected-delete-{identity}.db"))
    repo = SqliteChapterRepository(db)
    novel_id = f"protected-delete-{identity}"
    db.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        (novel_id, "Protected", novel_id, 3),
    )
    db.execute(
        """
        INSERT INTO chapters (id, novel_id, number, title, content, status)
        VALUES ('protected-chapter', ?, 1, 'Protected', '', 'draft')
        """,
        (novel_id,),
    )
    db.execute(
        """
        INSERT INTO novel_generation_runs
            (novel_id, run_mode, state, generation_epoch, target_chapters,
             current_formal_chapter, canonical_sync_status)
        VALUES (?, 'chapter_review', 'paused', 0, 3, 0, 'ready')
        """,
        (novel_id,),
    )
    if identity == "legacy":
        db.execute(
            """
            INSERT INTO pre_candidate_formal_history
                (novel_id, chapter_number, chapter_id, content_sha256, content_revision)
            VALUES (?, 1, 'protected-chapter', 'legacy-hash', 1)
            """,
            (novel_id,),
        )
    else:
        db.execute(
            """
            INSERT INTO chapter_candidates
                (id, novel_id, chapter_number, generation_epoch, status, formal_chapter_id)
            VALUES ('protected-candidate', ?, 1, 0, 'syncing', 'protected-chapter')
            """,
            (novel_id,),
        )
        db.execute(
            """
            INSERT INTO chapter_candidate_formal_commits
                (candidate_id, novel_id, chapter_number, chapter_id,
                 content_sha256, content_revision, provenance, sync_status)
            VALUES ('protected-candidate', ?, 1, 'protected-chapter',
                    'candidate-hash', 1, 'candidate_commit', 'syncing')
            """,
            (novel_id,),
        )
    db.execute(
        """
        INSERT INTO chapter_narrative_commits
            (novel_id, chapter_number, content_sha256, pipeline_version,
             content_revision, status, memory_status)
        VALUES (?, 1, 'narrative-hash', 'test-pipeline', 1, 'committed', 'committed')
        """,
        (novel_id,),
    )
    db.execute(
        """
        INSERT INTO memory_atoms
            (id, novel_id, entity_id, chapter_number, status, payload_json)
        VALUES (?, ?, 'hero', 1, 'canonical', '{}')
        """,
        (f"memory-{identity}", novel_id),
    )
    db.get_connection().commit()

    def counts():
        return tuple(
            db.fetch_one(query, (novel_id,))["total"]
            for query in (
                "SELECT COUNT(*) AS total FROM chapters WHERE novel_id = ?",
                "SELECT COUNT(*) AS total FROM pre_candidate_formal_history WHERE novel_id = ?",
                "SELECT COUNT(*) AS total FROM chapter_candidate_formal_commits WHERE novel_id = ?",
                "SELECT COUNT(*) AS total FROM novel_generation_runs WHERE novel_id = ?",
                "SELECT COUNT(*) AS total FROM chapter_narrative_commits WHERE novel_id = ?",
                "SELECT COUNT(*) AS total FROM memory_atoms WHERE novel_id = ?",
            )
        )

    before = counts()
    with sqlite_writes_bypass_queue():
        with pytest.raises(ValueError, match="Formal|正式|保护"):
            repo.execute_delete_on_writer(ChapterId("protected-chapter"))
    assert counts() == before

    import infrastructure.persistence.database.write_dispatch as write_dispatch

    enqueued = []
    monkeypatch.setattr(write_dispatch, "allow_direct_sqlite_writes", lambda: False)
    monkeypatch.setattr(write_dispatch, "is_sqlite_writer_thread", lambda: False)
    monkeypatch.setattr(
        write_dispatch,
        "enqueue_delete_chapter",
        lambda chapter_id: enqueued.append(chapter_id) or True,
    )
    with pytest.raises(ValueError, match="Formal|正式|保护"):
        repo.delete(ChapterId("protected-chapter"))
    assert enqueued == []
    assert counts() == before


@pytest.mark.parametrize("identity", ("legacy", "candidate"))
def test_delete_rejects_renumbering_a_formal_tail_before_fk_bypass(tmp_path, identity):
    """Deleting an empty draft cannot renumber a later Formal chapter identity."""

    db = DatabaseConnection(str(tmp_path / f"protected-tail-{identity}.db"))
    repo = SqliteChapterRepository(db)
    novel_id = f"protected-tail-{identity}"
    db.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        (novel_id, "Protected tail", novel_id, 3),
    )
    for number in (1, 2):
        db.execute(
            """
            INSERT INTO chapters (id, novel_id, number, title, content, status)
            VALUES (?, ?, ?, ?, '', 'draft')
            """,
            (f"protected-tail-{number}", novel_id, number, f"Chapter {number}"),
        )
    if identity == "legacy":
        db.execute(
            """
            INSERT INTO pre_candidate_formal_history
                (novel_id, chapter_number, chapter_id, content_sha256, content_revision)
            VALUES (?, 2, 'protected-tail-2', 'legacy-hash', 1)
            """,
            (novel_id,),
        )
    else:
        db.execute(
            """
            INSERT INTO chapter_candidates
                (id, novel_id, chapter_number, generation_epoch, status, formal_chapter_id)
            VALUES ('protected-tail-candidate', ?, 2, 0, 'syncing', 'protected-tail-2')
            """,
            (novel_id,),
        )
        db.execute(
            """
            INSERT INTO chapter_candidate_formal_commits
                (candidate_id, novel_id, chapter_number, chapter_id,
                 content_sha256, content_revision, provenance, sync_status)
            VALUES ('protected-tail-candidate', ?, 2, 'protected-tail-2',
                    'candidate-hash', 1, 'candidate_commit', 'syncing')
            """,
            (novel_id,),
        )
    db.get_connection().commit()

    with sqlite_writes_bypass_queue():
        with pytest.raises(ValueError, match="Formal|正式|保护"):
            repo.execute_delete_on_writer(ChapterId("protected-tail-1"))

    rows = db.fetch_all(
        "SELECT id, number FROM chapters WHERE novel_id = ? ORDER BY number", (novel_id,)
    )
    assert [(row["id"], row["number"]) for row in rows] == [
        ("protected-tail-1", 1),
        ("protected-tail-2", 2),
    ]
