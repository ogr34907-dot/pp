import asyncio
import sqlite3
from types import SimpleNamespace
from typing import Optional

import pytest

import application.blueprint.services.story_structure_service as story_structure_service_module
from application.blueprint.services.chapter_book_structure_sync import (
    purge_chapter_book_rows_not_matching_structure,
)
from application.blueprint.services.story_structure_service import StoryStructureService
from domain.novel.value_objects.chapter_id import ChapterId
from domain.structure.story_node import NodeType
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.planning_authority_guard import PlanningAuthorityError
from infrastructure.persistence.database.sqlite_chapter_repository import (
    SqliteChapterRepository,
)
from infrastructure.persistence.database.story_node_repository import StoryNodeRepository
from infrastructure.persistence.database.write_dispatch import sqlite_writes_bypass_queue


class _FakeStoryRepo:
    def __init__(self, nodes):
        self._nodes = {node.id: node for node in nodes}
        self.deleted_ids = []

    async def get_by_id(self, node_id):
        return self._nodes.get(node_id)

    def get_by_novel_sync(self, novel_id):
        return [node for node in self._nodes.values() if node.novel_id == novel_id]

    async def get_tree(self, novel_id):
        nodes = [node for node in self._nodes.values() if node.novel_id == novel_id]
        return SimpleNamespace(
            to_tree_dict=lambda: {
                "novel_id": novel_id,
                "nodes": [
                    {
                        "id": node.id,
                        "novel_id": node.novel_id,
                        "node_type": node.node_type.value,
                        "number": node.number,
                        "children": [],
                    }
                    for node in nodes
                ],
            }
        )

    async def delete(self, node_id):
        existed = node_id in self._nodes
        if existed:
            self.deleted_ids.append(node_id)
            del self._nodes[node_id]
        return existed


class _FakeChapterRepo:
    def __init__(self, chapters, on_delete=None):
        self._chapters = {number: chapter for number, chapter in chapters.items()}
        self.deleted_numbers = []
        self._on_delete = on_delete

    def get_by_novel_and_number(self, novel_id, chapter_number):
        return self._chapters.get(chapter_number)

    def list_by_novel(self, novel_id):
        return list(self._chapters.values())

    def delete(self, chapter_id: ChapterId):
        for number, chapter in list(self._chapters.items()):
            current_id = chapter.id.value if hasattr(chapter.id, "value") else chapter.id
            if current_id == chapter_id.value:
                self.deleted_numbers.append(number)
                del self._chapters[number]
                if self._on_delete is not None:
                    self._on_delete(chapter_id.value)
                return


class _FakeCoordinator:
    def __init__(self):
        self.calls = []

    def on_chapter_deleted(self, novel_id: str, deleted_chapter_number: int) -> None:
        self.calls.append((novel_id, deleted_chapter_number))


def _node(node_id: str, node_type: NodeType, number: int, parent_id: Optional[str] = None):
    return SimpleNamespace(
        id=node_id,
        novel_id="novel-1",
        parent_id=parent_id,
        node_type=node_type,
        number=number,
        is_chapter=lambda: node_type == NodeType.CHAPTER,
    )


def _chapter(number: int):
    return SimpleNamespace(id=f"chapter-{number}", number=number)


def _sqlite_delete_service(tmp_path, *, fail_root=False, orphan=False, foreign_keys=True):
    database = DatabaseConnection(str(tmp_path / "atomic-structure-delete.db"))
    connection = database.get_connection()
    connection.execute(f"PRAGMA foreign_keys = {'ON' if foreign_keys else 'OFF'}")
    connection.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'Novel', 'novel-1')"
    )
    connection.execute(
        """
        INSERT INTO story_nodes
            (id, novel_id, parent_id, node_type, number, title, order_index)
        VALUES
            ('act-1', 'novel-1', NULL, 'act', 1, 'Act', 0),
            ('chapter-1', 'novel-1', 'act-1', 'chapter', 1, 'Chapter', 1)
        """
    )
    connection.execute(
        """
        INSERT INTO chapters (id, novel_id, number, title, content, status)
        VALUES ('chapter-row-1', 'novel-1', 1, 'Chapter', '', 'draft')
        """
    )
    if orphan:
        connection.execute(
            """
            INSERT INTO chapters (id, novel_id, number, title, content, status)
            VALUES ('orphan-row-2', 'novel-1', 2, 'Orphan', '', 'draft')
            """
        )
    if fail_root:
        connection.execute(
            """
            CREATE TRIGGER fail_act_delete
            BEFORE DELETE ON story_nodes
            WHEN OLD.id = 'act-1'
            BEGIN
                SELECT RAISE(IGNORE);
            END
            """
        )
    connection.commit()
    coordinator = _FakeCoordinator()
    service = StoryStructureService(
        StoryNodeRepository(database),
        chapter_repository=SqliteChapterRepository(database),
        chapter_renumber_coordinator=coordinator,
    )
    return database, connection, service, coordinator


def test_delete_node_removes_descendant_chapters_before_deleting_structure_node():
    repo = _FakeStoryRepo(
        [
            _node("act-1", NodeType.ACT, 1),
            _node("chapter-1", NodeType.CHAPTER, 1, parent_id="act-1"),
            _node("chapter-2", NodeType.CHAPTER, 2, parent_id="act-1"),
        ]
    )
    chapter_repo = _FakeChapterRepo({1: _chapter(1), 2: _chapter(2)})
    coordinator = _FakeCoordinator()
    service = StoryStructureService(
        repo,
        chapter_repository=chapter_repo,
        chapter_renumber_coordinator=coordinator,
    )

    result = asyncio.run(service.delete_node("act-1"))

    assert result is True
    assert chapter_repo.deleted_numbers == [2, 1]
    assert coordinator.calls == [
        ("novel-1", 2),
        ("novel-1", 1),
    ]
    assert repo.deleted_ids == ["act-1"]


def test_delete_node_returns_true_when_direct_chapter_delete_removes_story_node():
    repo = _FakeStoryRepo([_node("chapter-1", NodeType.CHAPTER, 1)])
    chapter_repo = _FakeChapterRepo(
        {1: _chapter(1)},
        on_delete=lambda chapter_id: repo._nodes.pop(chapter_id, None),
    )
    coordinator = _FakeCoordinator()
    service = StoryStructureService(
        repo,
        chapter_repository=chapter_repo,
        chapter_renumber_coordinator=coordinator,
    )

    result = asyncio.run(service.delete_node("chapter-1"))

    assert result is True
    assert chapter_repo.deleted_numbers == [1]
    assert coordinator.calls == [("novel-1", 1)]
    assert asyncio.run(repo.get_by_id("chapter-1")) is None
    assert repo.deleted_ids == []


def test_delete_node_succeeds_when_repo_delete_returns_false_but_node_is_gone():
    """模拟持久化队列先级联删掉 story_nodes，单行 DELETE 影响 0 行但仍应视为成功。"""

    class _RaceStoryRepo(_FakeStoryRepo):
        async def delete(self, node_id):
            if node_id in self._nodes:
                del self._nodes[node_id]
            return False

    repo = _RaceStoryRepo([_node("chapter-1", NodeType.CHAPTER, 1)])
    chapter_repo = _FakeChapterRepo({1: _chapter(1)})
    coordinator = _FakeCoordinator()
    service = StoryStructureService(
        repo,
        chapter_repository=chapter_repo,
        chapter_renumber_coordinator=coordinator,
    )

    assert asyncio.run(service.delete_node("chapter-1")) is True


def test_delete_node_rolls_back_chapters_when_structure_delete_fails(tmp_path):
    database, connection, service, coordinator = _sqlite_delete_service(
        tmp_path, fail_root=True
    )

    with sqlite_writes_bypass_queue():
        result = asyncio.run(service.delete_node("act-1"))

    assert result is False
    assert [row[0] for row in connection.execute(
        "SELECT id FROM chapters WHERE novel_id = 'novel-1'"
    )] == ["chapter-row-1"]
    assert [row[0] for row in connection.execute(
        "SELECT id FROM story_nodes WHERE novel_id = 'novel-1' ORDER BY order_index"
    )] == ["act-1", "chapter-1"]
    assert coordinator.calls == []
    database.close()


def test_delete_node_purges_orphan_chapters_after_atomic_commit(tmp_path):
    database, connection, service, coordinator = _sqlite_delete_service(tmp_path, orphan=True)

    with sqlite_writes_bypass_queue():
        assert asyncio.run(service.delete_node("act-1")) is True

    assert connection.execute(
        "SELECT id FROM chapters WHERE novel_id = 'novel-1'"
    ).fetchall() == []
    assert coordinator.calls == [("novel-1", 1), ("novel-1", 1)]
    database.close()


def test_delete_node_rechecks_authority_after_begin_immediate(monkeypatch):
    class _BeginAwareConnection:
        def __init__(self):
            self._connection = sqlite3.connect(":memory:")
            self._connection.execute("PRAGMA foreign_keys = OFF")
            self.authority_changed = False
            self.begin_seen = False

        @property
        def in_transaction(self):
            return self._connection.in_transaction

        def execute(self, sql, params=()):
            if sql.strip().upper() == "BEGIN IMMEDIATE":
                self.authority_changed = True
                self.begin_seen = True
            return self._connection.execute(sql, params)

        def commit(self):
            return self._connection.commit()

        def rollback(self):
            return self._connection.rollback()

    class _TransactionalStoryRepo(_FakeStoryRepo):
        def __init__(self, nodes, connection):
            super().__init__(nodes)
            self._connection = connection

        def _get_connection(self):
            return self._connection

    class _TransactionalChapterRepo(_FakeChapterRepo):
        def __init__(self, chapters, connection):
            super().__init__(chapters)
            self.db = SimpleNamespace(get_connection=lambda: connection)
            self.transaction_body_calls = []

        def _delete_chapter_transaction_body(self, *args):
            self.transaction_body_calls.append(args)

    connection = _BeginAwareConnection()
    repo = _TransactionalStoryRepo(
        [
            _node("act-1", NodeType.ACT, 1),
            _node("chapter-1", NodeType.CHAPTER, 1, parent_id="act-1"),
        ],
        connection,
    )
    chapter_repo = _TransactionalChapterRepo({1: _chapter(1)}, connection)
    coordinator = _FakeCoordinator()

    def assert_current_authority(conn, novel_id, *, operation):
        if conn.in_transaction and conn.authority_changed:
            raise PlanningAuthorityError("authority changed")

    monkeypatch.setattr(
        story_structure_service_module,
        "assert_story_node_write_allowed",
        assert_current_authority,
    )
    service = StoryStructureService(
        repo,
        chapter_repository=chapter_repo,
        chapter_renumber_coordinator=coordinator,
    )

    with pytest.raises(PlanningAuthorityError, match="authority changed"):
        asyncio.run(service.delete_node("act-1"))

    assert connection.begin_seen is True
    assert connection._connection.execute("PRAGMA foreign_keys").fetchone()[0] == 0
    assert chapter_repo.transaction_body_calls == []
    assert chapter_repo.deleted_numbers == []
    assert repo.deleted_ids == []
    assert coordinator.calls == []


def test_get_tree_does_not_delete_orphan_chapter_rows():
    repo = _FakeStoryRepo([_node("act-1", NodeType.ACT, 1)])
    chapter_repo = _FakeChapterRepo({1: _chapter(1)})
    service = StoryStructureService(repo, chapter_repository=chapter_repo)

    result = asyncio.run(service.get_tree("novel-1"))

    assert result["novel_id"] == "novel-1"
    assert chapter_repo.deleted_numbers == []


def test_structure_sync_rejects_an_orphan_row_with_authored_prose():
    """DATA-001: an out-of-tree chapter body must block destructive sync."""
    repo = _FakeStoryRepo([])
    chapter_repo = _FakeChapterRepo(
        {
            1: SimpleNamespace(
                id=SimpleNamespace(value="chapter-1"),
                number=1,
                content="This chapter has authored prose and must survive.",
            )
        }
    )

    with pytest.raises(ValueError, match="正文"):
        purge_chapter_book_rows_not_matching_structure(repo, chapter_repo, "novel-1")

    assert chapter_repo.deleted_numbers == []


def test_delete_node_preflights_all_descendants_before_queueing_a_formal_baseline():
    """A later protected descendant must stop the whole structural delete up front."""

    class _IdentityAwareStoryRepo(_FakeStoryRepo):
        def __init__(self, nodes, connection):
            super().__init__(nodes)
            self._connection = connection

        def _get_connection(self):
            return self._connection

    connection = sqlite3.connect(":memory:")
    connection.execute(
        """
        CREATE TABLE pre_candidate_formal_history (
            novel_id TEXT NOT NULL,
            chapter_number INTEGER NOT NULL,
            chapter_id TEXT NOT NULL
        )
        """
    )
    connection.execute(
        """
        INSERT INTO pre_candidate_formal_history (novel_id, chapter_number, chapter_id)
        VALUES ('novel-1', 2, 'chapter-2')
        """
    )
    connection.commit()
    repo = _IdentityAwareStoryRepo(
        [
            _node("act-1", NodeType.ACT, 1),
            _node("chapter-1", NodeType.CHAPTER, 1, parent_id="act-1"),
            _node("chapter-2", NodeType.CHAPTER, 2, parent_id="act-1"),
        ],
        connection,
    )
    chapter_repo = _FakeChapterRepo(
        {
            1: SimpleNamespace(id="chapter-1", number=1, content=""),
            2: SimpleNamespace(id="chapter-2", number=2, content=""),
        }
    )
    service = StoryStructureService(repo, chapter_repository=chapter_repo)

    with pytest.raises(ValueError, match="Formal|正式|保护"):
        asyncio.run(service.delete_node("act-1"))

    assert chapter_repo.deleted_numbers == []
    assert repo.deleted_ids == []
