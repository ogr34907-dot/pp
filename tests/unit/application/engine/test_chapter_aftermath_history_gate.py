import hashlib
from unittest.mock import AsyncMock

import pytest

from application.engine.services.chapter_aftermath_pipeline import ChapterAftermathPipeline
from domain.novel.entities.chapter import Chapter, ChapterStatus
from domain.novel.value_objects.novel_id import NovelId
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_chapter_narrative_commit_repository import (
    SqliteChapterNarrativeCommitRepository,
)
from infrastructure.persistence.database.sqlite_chapter_repository import SqliteChapterRepository


def _seed_completed_chapter(repository, novel_id, number, content):
    chapter = Chapter(
        id=f"chapter-{number}",
        novel_id=NovelId(novel_id),
        number=number,
        title=f"第{number}章",
        content=content,
        status=ChapterStatus.COMPLETED,
    )
    repository.save(chapter)
    return repository.get_by_novel_and_number(NovelId(novel_id), number)


@pytest.mark.asyncio
async def test_history_gate_requires_checkpoint_before_replaying_uncommitted_legacy_head(tmp_path):
    database = DatabaseConnection(str(tmp_path / "history-gate.sqlite"))
    database.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-history', 'History', 'history')"
    )
    repository = SqliteChapterRepository(database)
    _seed_completed_chapter(repository, "novel-history", 1, "第一章正文")
    _seed_completed_chapter(repository, "novel-history", 2, "第二章正文")
    pipeline = ChapterAftermathPipeline(
        knowledge_service=None,
        chapter_indexing_service=None,
        llm_service=object(),
        chapter_repository=repository,
    )
    pipeline.run_after_chapter_saved = AsyncMock()

    result = await pipeline.ensure_prior_chapters_committed("novel-history", 3)

    assert result["ready"] is False
    assert result["failure_reason"] == "canonical_history_checkpoint_required"
    pipeline.run_after_chapter_saved.assert_not_awaited()


@pytest.mark.asyncio
async def test_history_gate_replays_only_chapters_after_active_checkpoint(tmp_path, monkeypatch):
    database = DatabaseConnection(str(tmp_path / "history-anchor.sqlite"))
    database.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-history', 'History', 'history')"
    )
    database.execute(
        "INSERT INTO novel_checkpoints (id, novel_id, trigger_type, name, anchor_chapter) "
        "VALUES ('checkpoint-1', 'novel-history', 'manual', 'checkpoint', 1)"
    )
    repository = SqliteChapterRepository(database)
    _seed_completed_chapter(repository, "novel-history", 1, "第一章正文")
    second = _seed_completed_chapter(repository, "novel-history", 2, "第二章正文")
    ready_chapters = set()

    def is_current_version_ready(self, *, novel_id, chapter_number, pipeline_version):
        return chapter_number in ready_chapters

    monkeypatch.setattr(
        SqliteChapterNarrativeCommitRepository,
        "is_current_version_ready",
        is_current_version_ready,
    )
    pipeline = ChapterAftermathPipeline(
        knowledge_service=None,
        chapter_indexing_service=None,
        llm_service=object(),
        chapter_repository=repository,
    )

    async def replay(novel_id, chapter_number, content, **kwargs):
        ready_chapters.add(chapter_number)
        return {"narrative_sync_ok": True}

    pipeline.run_after_chapter_saved = AsyncMock(side_effect=replay)

    result = await pipeline.ensure_prior_chapters_committed("novel-history", 3)

    assert result["ready"] is True
    assert result["replayed_chapters"] == [2]
    assert pipeline.run_after_chapter_saved.await_args.args == (
        "novel-history",
        2,
        "第二章正文",
    )
    assert pipeline.run_after_chapter_saved.await_args.kwargs == {
        "expected_content_sha256": hashlib.sha256("第二章正文".encode("utf-8")).hexdigest(),
        "expected_content_revision": second.content_revision,
    }


@pytest.mark.asyncio
async def test_history_gate_wraps_replay_failure_in_canonical_history_reason(tmp_path, monkeypatch):
    database = DatabaseConnection(str(tmp_path / "history-failure.sqlite"))
    database.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-history', 'History', 'history')"
    )
    database.execute(
        "INSERT INTO novel_checkpoints (id, novel_id, trigger_type, name, anchor_chapter) "
        "VALUES ('checkpoint-1', 'novel-history', 'manual', 'checkpoint', 1)"
    )
    repository = SqliteChapterRepository(database)
    _seed_completed_chapter(repository, "novel-history", 1, "第一章正文")
    _seed_completed_chapter(repository, "novel-history", 2, "第二章正文")
    monkeypatch.setattr(
        SqliteChapterNarrativeCommitRepository,
        "is_current_version_ready",
        lambda self, **_kwargs: False,
    )
    pipeline = ChapterAftermathPipeline(
        knowledge_service=None,
        chapter_indexing_service=None,
        llm_service=object(),
        chapter_repository=repository,
    )
    pipeline.run_after_chapter_saved = AsyncMock(
        return_value={"narrative_sync_ok": False, "failure_reason": "source_hash_mismatch"}
    )

    result = await pipeline.ensure_prior_chapters_committed("novel-history", 3)

    assert result["ready"] is False
    assert result["failure_reason"] == "canonical_history_replay_failed"
    assert result["failure_cause"] == "source_hash_mismatch"


@pytest.mark.asyncio
async def test_history_gate_falls_back_to_sqlite_when_repository_has_no_list_method(
    tmp_path,
    monkeypatch,
):
    database = DatabaseConnection(str(tmp_path / "history-lookup.sqlite"))
    database.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-history', 'History', 'history')"
    )
    database.execute(
        "INSERT INTO novel_checkpoints (id, novel_id, trigger_type, name, anchor_chapter) "
        "VALUES ('checkpoint-1', 'novel-history', 'manual', 'checkpoint', 1)"
    )
    backing_repository = SqliteChapterRepository(database)
    _seed_completed_chapter(backing_repository, "novel-history", 1, "第一章正文")
    _seed_completed_chapter(backing_repository, "novel-history", 2, "第二章正文")

    class LookupOnlyRepository:
        db = database

        def get_by_novel_and_number(self, novel_id, chapter_number):
            return backing_repository.get_by_novel_and_number(novel_id, chapter_number)

    ready_chapters = set()
    monkeypatch.setattr(
        SqliteChapterNarrativeCommitRepository,
        "is_current_version_ready",
        lambda self, *, chapter_number, **_kwargs: chapter_number in ready_chapters,
    )
    pipeline = ChapterAftermathPipeline(
        knowledge_service=None,
        chapter_indexing_service=None,
        llm_service=object(),
        chapter_repository=LookupOnlyRepository(),
    )

    async def replay(novel_id, chapter_number, content, **kwargs):
        ready_chapters.add(chapter_number)
        return {"narrative_sync_ok": True}

    pipeline.run_after_chapter_saved = AsyncMock(side_effect=replay)

    result = await pipeline.ensure_prior_chapters_committed("novel-history", 3)

    assert result["ready"] is True
    assert result["replayed_chapters"] == [2]
