import pytest

from application.engine.services.chapter_aftermath_pipeline import ChapterAftermathPipeline
from domain.novel.entities.chapter import Chapter, ChapterStatus
from domain.novel.value_objects.novel_id import NovelId
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_chapter_narrative_commit_repository import (
    SqliteChapterNarrativeCommitRepository,
)
from infrastructure.persistence.database.sqlite_chapter_repository import (
    SqliteChapterRepository,
)


async def _skip_bridge(self, novel_id, chapter_number, content):
    return None


async def _skip_auxiliary(self, novel_id, chapter_number, content, evidence):
    return None


@pytest.mark.asyncio
async def test_aftermath_updates_shared_memory_engine_after_canonical_sync(monkeypatch):
    sequence = []

    async def canonical_sync(*args, **kwargs):
        sequence.append("canonical")
        return {
            "narrative_sync_ok": True,
            "content_sha256": "current-hash",
            "content_revision": 1,
        }

    class SharedMemoryEngine:
        async def update_from_chapter(self, novel_id, chapter_number, content, outline):
            assert sequence == ["canonical"]
            assert (novel_id, chapter_number, content, outline) == (
                "novel-1",
                7,
                "final prose",
                "",
            )
            sequence.append("memory")
            return {"new_beats": 1, "new_clues": 1, "errors": []}

    monkeypatch.setattr(ChapterAftermathPipeline, "_extract_chapter_bridge", _skip_bridge)
    monkeypatch.setattr(ChapterAftermathPipeline, "_run_auxiliary_stages", _skip_auxiliary)
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.sync_chapter_narrative_after_save",
        canonical_sync,
    )
    pipeline = ChapterAftermathPipeline(
        knowledge_service=None,
        chapter_indexing_service=None,
        llm_service=object(),
        memory_engine=SharedMemoryEngine(),
    )

    result = await pipeline.run_after_chapter_saved("novel-1", 7, "final prose")

    assert sequence == ["canonical", "memory"]
    assert result["narrative_sync_ok"] is True
    assert result["memory_engine_ok"] is True
    assert result["memory_engine_new_beats"] == 1
    assert result["memory_engine_new_clues"] == 1
    await pipeline.drain_auxiliary_stages()


@pytest.mark.asyncio
async def test_aftermath_blocks_continuation_when_memory_engine_write_fails(monkeypatch):
    async def canonical_sync(*args, **kwargs):
        return {"narrative_sync_ok": True, "content_sha256": "current-hash", "content_revision": 1}

    class FailingMemoryEngine:
        async def update_from_chapter(self, novel_id, chapter_number, content, outline):
            return {"errors": ["memory state persistence failed"]}

    monkeypatch.setattr(ChapterAftermathPipeline, "_extract_chapter_bridge", _skip_bridge)
    monkeypatch.setattr(ChapterAftermathPipeline, "_run_auxiliary_stages", _skip_auxiliary)
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.sync_chapter_narrative_after_save",
        canonical_sync,
    )
    pipeline = ChapterAftermathPipeline(
        knowledge_service=None,
        chapter_indexing_service=None,
        llm_service=object(),
        memory_engine=FailingMemoryEngine(),
    )

    result = await pipeline.run_after_chapter_saved("novel-1", 7, "final prose")

    assert result["memory_engine_ok"] is False
    assert result["narrative_sync_ok"] is False
    assert result["failure_reason"] == "memory_engine_update_failed"
    await pipeline.drain_auxiliary_stages()


@pytest.mark.asyncio
async def test_aftermath_discards_memory_update_when_content_changes_after_sync(monkeypatch):
    async def canonical_sync(*args, **kwargs):
        return {"narrative_sync_ok": True, "content_sha256": "current-hash", "content_revision": 1}

    class MemoryEngine:
        def __init__(self):
            self.calls = 0

        async def update_from_chapter(self, novel_id, chapter_number, content, outline):
            self.calls += 1
            return {"errors": []}

    memory_engine = MemoryEngine()
    checks = iter((True, False, False))
    monkeypatch.setattr(ChapterAftermathPipeline, "_extract_chapter_bridge", _skip_bridge)
    monkeypatch.setattr(ChapterAftermathPipeline, "_run_auxiliary_stages", _skip_auxiliary)
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.sync_chapter_narrative_after_save",
        canonical_sync,
    )
    pipeline = ChapterAftermathPipeline(
        knowledge_service=None,
        chapter_indexing_service=None,
        llm_service=object(),
        memory_engine=memory_engine,
    )
    monkeypatch.setattr(
        pipeline,
        "_is_current_content_version",
        lambda *args, **kwargs: next(checks),
    )

    result = await pipeline.run_after_chapter_saved("novel-1", 7, "final prose")

    assert memory_engine.calls == 0
    assert result["narrative_sync_ok"] is False
    assert result["discarded_stale"] is True
    assert result["failure_reason"] == "source_version_mismatch"
    await pipeline.drain_auxiliary_stages()


@pytest.mark.asyncio
async def test_memory_engine_failure_stays_durable_until_the_same_version_retries(
    monkeypatch, tmp_path
):
    db = DatabaseConnection(str(tmp_path / "memory-retry-barrier.db"))
    db.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'Novel', 'novel-1')"
    )
    chapter_repository = SqliteChapterRepository(db)
    chapter = Chapter(
        id="chapter-1",
        novel_id=NovelId("novel-1"),
        number=1,
        title="Chapter",
        content="final prose",
        status=ChapterStatus.COMPLETED,
    )
    chapter_repository.save(chapter)
    chapter = chapter_repository.get_by_novel_and_number(NovelId("novel-1"), 1)
    db.execute("INSERT INTO knowledge (id, novel_id) VALUES ('knowledge-1', 'novel-1')")
    db.execute(
        "INSERT INTO chapter_summaries "
        "(id, knowledge_id, chapter_number, summary, source_content_sha256, "
        "pipeline_version, sync_status, sync_attempts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "summary-1",
            "knowledge-1",
            1,
            "已提交的规范摘要",
            chapter.content_sha256,
            "chapter-narrative-sync:v1",
            "committed",
            1,
        ),
    )
    db.execute(
        "INSERT INTO chapter_narrative_commits "
        "(novel_id, chapter_number, content_sha256, pipeline_version, content_revision, "
        "status, memory_status) VALUES (?, ?, ?, ?, ?, 'committed', 'pending')",
        (
            "novel-1",
            1,
            chapter.content_sha256,
            "chapter-narrative-sync:v1",
            chapter.content_revision,
        ),
    )

    async def canonical_sync(*_args, **_kwargs):
        return {
            "narrative_sync_ok": True,
            "content_sha256": chapter.content_sha256,
            "content_revision": chapter.content_revision,
        }

    class FailingMemoryEngine:
        async def update_from_chapter(self, *_args):
            return {"errors": ["injected memory failure"]}

    class WorkingMemoryEngine:
        async def update_from_chapter(self, *_args):
            return {"errors": [], "new_beats": 1, "new_clues": 1}

    monkeypatch.setattr(ChapterAftermathPipeline, "_extract_chapter_bridge", _skip_bridge)
    monkeypatch.setattr(ChapterAftermathPipeline, "_run_auxiliary_stages", _skip_auxiliary)
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.sync_chapter_narrative_after_save",
        canonical_sync,
    )
    failing = ChapterAftermathPipeline(
        knowledge_service=None,
        chapter_indexing_service=None,
        llm_service=object(),
        chapter_repository=chapter_repository,
        memory_engine=FailingMemoryEngine(),
    )

    failed = await failing.run_after_chapter_saved(
        "novel-1",
        1,
        chapter.content,
        expected_content_sha256=chapter.content_sha256,
        expected_content_revision=chapter.content_revision,
    )

    repository = SqliteChapterNarrativeCommitRepository(db)
    assert failed["narrative_sync_ok"] is False
    assert db.fetch_one(
        "SELECT memory_status FROM chapter_narrative_commits"
    )["memory_status"] == "failed"
    assert repository.is_current_version_ready(
        novel_id="novel-1",
        chapter_number=1,
        pipeline_version="chapter-narrative-sync:v1",
        require_memory_sync=True,
    ) is False

    recovered = ChapterAftermathPipeline(
        knowledge_service=None,
        chapter_indexing_service=None,
        llm_service=object(),
        chapter_repository=chapter_repository,
        memory_engine=WorkingMemoryEngine(),
    )
    retried = await recovered.run_after_chapter_saved(
        "novel-1",
        1,
        chapter.content,
        expected_content_sha256=chapter.content_sha256,
        expected_content_revision=chapter.content_revision,
    )

    assert retried["narrative_sync_ok"] is True
    assert dict(db.fetch_one(
        "SELECT memory_status, memory_failure_reason FROM chapter_narrative_commits"
    )) == {"memory_status": "committed", "memory_failure_reason": ""}
    assert repository.is_current_version_ready(
        novel_id="novel-1",
        chapter_number=1,
        pipeline_version="chapter-narrative-sync:v1",
        require_memory_sync=True,
    ) is True
    await failing.drain_auxiliary_stages()
    await recovered.drain_auxiliary_stages()
