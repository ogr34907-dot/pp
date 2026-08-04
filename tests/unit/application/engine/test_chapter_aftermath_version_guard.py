import hashlib
from types import SimpleNamespace

import pytest

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
