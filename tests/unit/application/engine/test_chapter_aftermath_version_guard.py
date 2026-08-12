import hashlib
from types import SimpleNamespace

import pytest

from domain.novel.entities.chapter import ChapterStatus
from application.engine.services.chapter_aftermath_pipeline import (
    ChapterAftermathPipeline,
)


class _ChapterRepository:
    def get_by_novel_and_number(self, novel_id, chapter_number):
        return SimpleNamespace(
            content="新版本正文",
            content_sha256=hashlib.sha256("新版本正文".encode("utf-8")).hexdigest(),
            content_revision=2,
        )


class _DraftChapterRepository:
    def get_by_novel_and_number(self, novel_id, chapter_number):
        return SimpleNamespace(
            content="旧直写草稿",
            content_sha256=hashlib.sha256("旧直写草稿".encode("utf-8")).hexdigest(),
            content_revision=1,
            status=ChapterStatus.DRAFT,
        )


class _CandidateAuthorityDb:
    def fetch_one(self, sql, _params=()):
        if "novel_generation_runs" in sql:
            return {"novel_id": "novel-1"}
        if "chapter_candidate_formal_commits" in sql:
            return None
        raise AssertionError(f"unexpected authority query: {sql}")


class _DirectCompletedChapterRepository:
    db = _CandidateAuthorityDb()

    def get_by_novel_and_number(self, novel_id, chapter_number):
        return SimpleNamespace(
            id="legacy-direct-chapter",
            content="旧路径写入的完成正文",
            content_sha256=hashlib.sha256("旧路径写入的完成正文".encode("utf-8")).hexdigest(),
            content_revision=1,
            status=ChapterStatus.COMPLETED,
        )


@pytest.mark.asyncio
async def test_aftermath_discards_outdated_content_version_before_side_effects(monkeypatch):
    called = False

    async def should_not_run(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("stale job must not enter narrative sync")

    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.sync_chapter_narrative_after_save",
        should_not_run,
    )
    pipeline = ChapterAftermathPipeline(
        knowledge_service=object(),
        chapter_indexing_service=None,
        llm_service=object(),
        chapter_repository=_ChapterRepository(),
    )
    old_content = "旧版本正文"

    result = await pipeline.run_after_chapter_saved(
        "novel-1",
        2,
        old_content,
        expected_content_sha256=hashlib.sha256(old_content.encode("utf-8")).hexdigest(),
        expected_content_revision=1,
    )

    assert result["discarded_stale"] is True
    assert called is False


@pytest.mark.asyncio
async def test_aftermath_discards_non_candidate_draft_before_side_effects(monkeypatch):
    called = False

    async def should_not_run(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("draft must not enter canonical sync")

    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.sync_chapter_narrative_after_save",
        should_not_run,
    )
    pipeline = ChapterAftermathPipeline(
        knowledge_service=object(),
        chapter_indexing_service=None,
        llm_service=object(),
        chapter_repository=_DraftChapterRepository(),
    )

    result = await pipeline.run_after_chapter_saved("novel-1", 2, "旧直写草稿")

    assert result["discarded_uncommitted"] is True
    assert result["failure_reason"] == "candidate_first_required"
    assert called is False


@pytest.mark.asyncio
async def test_aftermath_discards_direct_completed_chapter_when_candidate_run_exists(monkeypatch):
    called = False

    async def should_not_run(*args, **kwargs):
        nonlocal called
        called = True

    pipeline = ChapterAftermathPipeline(
        knowledge_service=object(),
        chapter_indexing_service=None,
        llm_service=object(),
        chapter_repository=_DirectCompletedChapterRepository(),
    )
    monkeypatch.setattr(pipeline, "_extract_chapter_bridge", should_not_run)
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.sync_chapter_narrative_after_save",
        should_not_run,
    )

    result = await pipeline.run_after_chapter_saved("novel-1", 2, "旧路径写入的完成正文")

    assert result["discarded_uncommitted"] is True
    assert result["failure_reason"] == "candidate_first_required"
    assert called is False
