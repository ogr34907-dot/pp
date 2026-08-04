from types import SimpleNamespace

import pytest

from application.core.services.chapter_rewrite_coordinator import ChapterRewriteResult
from domain.novel.entities.chapter import Chapter, ChapterStatus
from domain.novel.value_objects.novel_id import NovelId
from engine.pipeline.base import BaseStoryPipeline
from engine.pipeline.context import PipelineContext


class _ChapterRepository:
    def __init__(self, chapter):
        self.chapter = chapter
        self.saved = []

    def get_by_novel_and_number(self, novel_id, chapter_number):
        return self.chapter

    def save(self, chapter):
        self.saved.append(chapter)


class _RewriteCoordinator:
    def __init__(self):
        self.calls = []

    def rewrite(self, chapter, content, *, rewrite_mode):
        self.calls.append((chapter, content, rewrite_mode))
        return ChapterRewriteResult(
            chapter=chapter,
            rewrite_mode=rewrite_mode,
            requires_rebuild=True,
            replay_completed=False,
            checkpoint_id="checkpoint-1",
        )


@pytest.mark.asyncio
async def test_story_pipeline_uses_shared_rewrite_coordinator_for_existing_prose():
    existing = Chapter(
        id="chapter-1",
        novel_id=NovelId("novel-1"),
        number=1,
        title="第一章",
        content="旧正文",
        status=ChapterStatus.COMPLETED,
    )
    repository = _ChapterRepository(existing)
    coordinator = _RewriteCoordinator()
    context = PipelineContext(
        novel_id="novel-1",
        chapter_number=1,
        chapter_content="新正文",
        chapter_node=SimpleNamespace(title="第一章"),
    )
    context.inject(
        chapter_repository=repository,
        chapter_rewrite_coordinator=coordinator,
    )

    await BaseStoryPipeline()._save_chapter_via_repository(context)

    assert coordinator.calls == [(existing, "新正文", "safe_snapshot")]
    assert context.metadata["rewrite_requires_rebuild"] is True
    assert repository.saved == []
