import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

from application.engine.services.context_budget_allocator import ContextBudgetAllocator
from application.world.services.chapter_narrative_sync import (
    CHAPTER_NARRATIVE_PIPELINE_VERSION,
)
from domain.novel.entities.chapter import Chapter, ChapterStatus
from domain.novel.value_objects.novel_id import NovelId
from domain.structure.story_node import NodeType, StoryNode
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_chapter_repository import (
    SqliteChapterRepository,
)


def _node(
    node_id: str,
    node_type: NodeType,
    number: int,
    *,
    parent_id: str | None = None,
    chapter_start: int | None = None,
    chapter_end: int | None = None,
    description: str = "",
    metadata: dict | None = None,
) -> StoryNode:
    return StoryNode(
        id=node_id,
        novel_id="novel-1",
        node_type=node_type,
        number=number,
        title=node_id,
        order_index=number,
        parent_id=parent_id,
        chapter_start=chapter_start,
        chapter_end=chapter_end,
        description=description,
        metadata=metadata or {},
    )


def _source_version(*chapters: SimpleNamespace) -> str:
    payload = "|".join(
        f"{chapter.number}:{chapter.content_sha256}:{chapter.content_revision}"
        for chapter in sorted(chapters, key=lambda item: item.number)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class _StoryNodeRepository:
    def __init__(self, nodes):
        self.nodes = nodes

    def get_by_novel_sync(self, _novel_id):
        return list(self.nodes)


class _ChapterRepository:
    def __init__(self, chapters):
        self.chapters = chapters
        self.by_number = {chapter.number: chapter for chapter in chapters}

    def list_by_novel(self, _novel_id):
        return list(self.chapters)

    def get_by_novel_and_number(self, _novel_id, chapter_number):
        return self.by_number.get(int(chapter_number))


class _VectorFacade:
    def __init__(self, results):
        self.results = results
        self.calls = []
        self.vector_store = SimpleNamespace(
            list_collections=AsyncMock(return_value=["novel_novel-1_chunks"])
        )
        self.embedding_service = SimpleNamespace(get_dimension=lambda: 3)

    def sync_search(self, *, collection, query_text, limit):
        self.calls.append(
            {"collection": collection, "query_text": query_text, "limit": limit}
        )
        return list(self.results)


def test_recent_act_summaries_use_valid_metadata_and_fallback_for_stale_nodes():
    chapters = [
        SimpleNamespace(number=1, content_sha256="hash-1", content_revision=1),
        SimpleNamespace(number=2, content_sha256="hash-2", content_revision=1),
    ]
    volume = _node(
        "volume-committed",
        NodeType.VOLUME,
        1,
        chapter_start=1,
        chapter_end=2,
        metadata={
            "summary": "已提交卷摘要",
            "summary_state": {
                "status": "committed",
                "chapter_start": 1,
                "chapter_end": 2,
                "source_version": _source_version(*chapters),
            },
        },
    )
    committed = _node(
        "act-committed",
        NodeType.ACT,
        1,
        chapter_start=1,
        chapter_end=2,
        metadata={
            "summary": "已提交幕摘要",
            "summary_state": {
                "status": "committed",
                "chapter_start": 1,
                "chapter_end": 2,
                "source_version": _source_version(*chapters),
            },
        },
    )
    stale = _node(
        "act-stale",
        NodeType.ACT,
        2,
        chapter_start=3,
        chapter_end=4,
        description="失效幕描述回退",
        metadata={
            "summary": "不应进入正文的失效摘要",
            "summary_state": {"status": "stale"},
        },
    )
    checkpoint = _node("chapter-2", NodeType.CHAPTER, 2)
    checkpoint.metadata = {
        "checkpoint_summary": "最近有效检查点摘要",
        "checkpoint_summary_state": {
            "status": "committed",
            "chapter_start": 1,
            "chapter_end": 2,
            "source_version": _source_version(*chapters),
        },
    }
    allocator = ContextBudgetAllocator(
        story_node_repository=_StoryNodeRepository([volume, committed, stale, checkpoint]),
        chapter_repository=_ChapterRepository(chapters),
    )

    context = allocator._get_recent_act_summaries("novel-1", 5)

    assert "已提交幕摘要" in context
    assert "已提交卷摘要" in context
    assert "最近有效检查点摘要" in context
    assert "不应进入正文的失效摘要" not in context
    assert "失效幕描述回退" in context


def test_vector_recall_combines_narrative_query_and_filters_invalid_evidence():
    chapters = [
        SimpleNamespace(number=number, content_sha256=f"hash-{number}", content_revision=1)
        for number in range(7, 22)
    ]
    hits = [
        {"id": "current", "score": 0.99, "payload": {"chapter_number": 20, "text": "当前章", "sync_status": "committed"}},
        {"id": "future", "score": 0.98, "payload": {"chapter_number": 21, "text": "未来章", "sync_status": "committed"}},
        {"id": "recent", "score": 0.97, "payload": {"chapter_number": 19, "text": "T2 已含", "sync_status": "committed"}},
        {"id": "stale", "score": 0.96, "payload": {"chapter_number": 14, "text": "失效向量", "sync_status": "stale"}},
        {"id": "mismatch", "score": 0.95, "payload": {"chapter_number": 13, "text": "旧版本", "sync_status": "committed", "content_sha256": "wrong-hash", "content_revision": 1}},
        {"id": "missing-provenance", "score": 0.94, "payload": {"chapter_number": 12, "text": "无来源向量", "sync_status": "committed"}},
        {"id": "low", "score": 0.49, "payload": {"chapter_number": 12, "text": "低分向量", "sync_status": "committed"}},
        {"id": "valid-a", "score": 0.90, "payload": {"chapter_number": 11, "text": "有效证据 A", "sync_status": "committed", "content_sha256": "hash-11", "content_revision": 1, "pipeline_version": "chapter-narrative-sync:v1"}},
        {"id": "duplicate", "score": 0.89, "payload": {"chapter_number": 10, "text": "有效证据 A", "sync_status": "committed", "content_sha256": "hash-10", "content_revision": 1, "pipeline_version": "chapter-narrative-sync:v1"}},
        {"id": "valid-b", "score": 0.86, "payload": {"chapter_number": 9, "text": "有效证据 B", "sync_status": "committed", "content_sha256": "hash-9", "content_revision": 1, "pipeline_version": "chapter-narrative-sync:v1"}},
        {"id": "valid-c", "score": 0.85, "payload": {"chapter_number": 8, "text": "有效证据 C", "sync_status": "committed", "content_sha256": "hash-8", "content_revision": 1, "pipeline_version": "chapter-narrative-sync:v1"}},
        {"id": "valid-d", "score": 0.84, "payload": {"chapter_number": 7, "text": "不应超过三条", "sync_status": "committed", "content_sha256": "hash-7", "content_revision": 1, "pipeline_version": "chapter-narrative-sync:v1"}},
    ]
    vector_facade = _VectorFacade(hits)
    bible = SimpleNamespace(
        characters=[SimpleNamespace(name="林澈")],
        locations=[SimpleNamespace(name="钟楼")],
    )
    allocator = ContextBudgetAllocator(
        chapter_repository=_ChapterRepository(chapters),
        bible_repository=SimpleNamespace(get_by_novel_id=lambda _novel_id: bible),
    )
    allocator.vector_facade = vector_facade
    allocator._get_pending_foreshadowings = lambda *_args: "未解线索：钟楼暗门"

    context = allocator._get_vector_recall("novel-1", 20, "林澈前往钟楼")

    query = vector_facade.calls[0]
    assert query["limit"] == 8
    assert "林澈前往钟楼" in query["query_text"]
    assert "林澈" in query["query_text"]
    assert "钟楼" in query["query_text"]
    assert "暗门" in query["query_text"]
    assert "有效证据 A" in context
    assert "有效证据 B" in context
    assert "有效证据 C" in context
    assert context.count("有效证据 A") == 1
    for forbidden in ("当前章", "未来章", "T2 已含", "失效向量", "旧版本", "无来源向量", "低分向量", "不应超过三条"):
        assert forbidden not in context


def test_recent_chapters_use_only_current_committed_summaries_for_n3_to_n5(tmp_path):
    novel_id = "novel-1"
    db = DatabaseConnection(str(tmp_path / "recent-summaries.db"))
    db.execute(
        "INSERT INTO novels (id, title, slug) VALUES (?, ?, ?)",
        (novel_id, "Novel", novel_id),
    )
    chapter_repository = SqliteChapterRepository(db)
    for number in range(5, 10):
        chapter_repository.save(
            Chapter(
                id=f"chapter-{number}",
                novel_id=NovelId(novel_id),
                number=number,
                title=f"第{number}章",
                content=f"第{number}章正文，原始预览内容。",
                status=ChapterStatus.COMPLETED,
            )
        )
    db.execute("INSERT INTO knowledge (id, novel_id) VALUES ('knowledge-1', ?)", (novel_id,))
    summary_columns = {
        row["name"] for row in db.fetch_all("PRAGMA table_info(chapter_summaries)")
    }
    if "source_content_revision" not in summary_columns:
        db.execute(
            "ALTER TABLE chapter_summaries ADD COLUMN "
            "source_content_revision INTEGER NOT NULL DEFAULT 0"
        )

    def seed_summary(
        number,
        summary,
        open_threads,
        *,
        source_hash=None,
        source_revision=None,
        sync_status="committed",
    ):
        source = db.fetch_one(
            "SELECT content_sha256, content_revision FROM chapters WHERE novel_id = ? AND number = ?",
            (novel_id, number),
        )
        canonical_hash = source_hash or source["content_sha256"]
        db.execute(
            "INSERT INTO chapter_summaries "
            "(id, knowledge_id, chapter_number, summary, open_threads, source_content_sha256, "
            "source_content_revision, pipeline_version, sync_status, sync_attempts) "
            "VALUES (?, 'knowledge-1', ?, ?, ?, ?, ?, ?, ?, 1)",
            (
                f"summary-{number}",
                number,
                summary,
                open_threads,
                canonical_hash,
                source["content_revision"] if source_revision is None else source_revision,
                CHAPTER_NARRATIVE_PIPELINE_VERSION,
                sync_status,
            ),
        )
        db.execute(
            "INSERT INTO chapter_narrative_commits "
            "(novel_id, chapter_number, content_sha256, pipeline_version, content_revision, status) "
            "VALUES (?, ?, ?, ?, ?, 'committed')",
            (
                novel_id,
                number,
                source["content_sha256"],
                CHAPTER_NARRATIVE_PIPELINE_VERSION,
                source["content_revision"],
            ),
        )

    seed_summary(7, "第七章有效摘要", "第七章未解线程")
    seed_summary(6, "第六章旧修订摘要", "不应注入", source_revision=0)
    seed_summary(5, "第五章旧哈希摘要", "不应注入", source_hash="wrong-hash")
    allocator = ContextBudgetAllocator(chapter_repository=chapter_repository)

    context = allocator._get_recent_chapters(novel_id, 10)

    assert "第七章有效摘要" in context
    assert "第七章未解线程" in context
    assert "第7章正文" not in context
    assert "第六章旧修订摘要" not in context
    assert "第6章正文" in context
    assert "第五章旧哈希摘要" not in context
    assert "第5章正文" in context
    assert "第9章正文" in context
    assert "第8章正文" in context
