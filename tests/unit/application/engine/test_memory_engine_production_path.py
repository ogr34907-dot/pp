import pytest

from application.engine.services.chapter_aftermath_pipeline import ChapterAftermathPipeline


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
    checks = iter((True, False))
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
