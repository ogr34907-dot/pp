import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

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


class _ReadyCanonicalDb:
    def fetch_one(self, sql, _params=()):
        if "novel_generation_runs" in sql:
            return {"novel_id": "novel-1"}
        if "chapter_candidate_formal_commits" in sql:
            return {
                "chapter_id": "formal-chapter-2",
                "content_sha256": hashlib.sha256("正式正文".encode("utf-8")).hexdigest(),
                "content_revision": 2,
            }
        raise AssertionError(f"unexpected authority query: {sql}")


class _ReadyCanonicalChapterRepository:
    def __init__(self):
        self.db = _ReadyCanonicalDb()
        self.chapter = SimpleNamespace(
            id="formal-chapter-2",
            content="正式正文",
            content_sha256=hashlib.sha256("正式正文".encode("utf-8")).hexdigest(),
            content_revision=2,
            status=ChapterStatus.COMPLETED,
        )

    def get_by_novel_and_number(self, novel_id, chapter_number):
        return self.chapter


class _StaleFormalAuthorityDb:
    def fetch_one(self, sql, _params=()):
        if "novel_generation_runs" in sql:
            return {"novel_id": "novel-1"}
        if "chapter_candidate_formal_commits" in sql:
            return {
                "chapter_id": "formal-chapter-2",
                "content_sha256": hashlib.sha256("候选正式正文".encode("utf-8")).hexdigest(),
                "content_revision": 1,
            }
        raise AssertionError(f"unexpected authority query: {sql}")


class _RewrittenCanonicalChapterRepository:
    db = _StaleFormalAuthorityDb()

    def get_by_novel_and_number(self, novel_id, chapter_number):
        return SimpleNamespace(
            id="formal-chapter-2",
            content="人工改写正文",
            content_sha256=hashlib.sha256("人工改写正文".encode("utf-8")).hexdigest(),
            content_revision=2,
            status=ChapterStatus.COMPLETED,
        )


class _MemoryCommitRepository:
    def __init__(self, _db):
        pass

    def claim_memory_sync(self, **_kwargs):
        return "claimed"

    def fail_memory_sync(self, **_kwargs):
        return False


class _FailingMemoryEngine:
    llm_service = None

    async def update_canonical_version_from_chapter(self, *args, **kwargs):
        raise RuntimeError("memory extraction failed")


def _side_effect_probes():
    return {
        "bridge": AsyncMock(),
        "reconcile": MagicMock(return_value={"checked": True}),
        "auxiliary": AsyncMock(),
    }


def _canonical_pipeline(probes, *, memory_engine=None):
    repository = _ReadyCanonicalChapterRepository()
    pipeline = ChapterAftermathPipeline(
        knowledge_service=object(),
        chapter_indexing_service=None,
        llm_service=object(),
        chapter_repository=repository,
        character_narrative_kernel=SimpleNamespace(
            reconcile_after_chapter=probes["reconcile"]
        ),
        evolution_snapshot_service=MagicMock(),
        unified_checkpoint_service=MagicMock(),
        prop_lifecycle_syncer=probes["auxiliary"],
        memory_engine=memory_engine,
    )
    pipeline._extract_chapter_bridge = probes["bridge"]
    pipeline._run_auxiliary_stages = probes["auxiliary"]
    return pipeline


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


@pytest.mark.asyncio
async def test_aftermath_discards_chapter_that_no_longer_matches_formal_authority(monkeypatch):
    called = False

    async def should_not_run(*args, **kwargs):
        nonlocal called
        called = True

    pipeline = ChapterAftermathPipeline(
        knowledge_service=object(),
        chapter_indexing_service=None,
        llm_service=object(),
        chapter_repository=_RewrittenCanonicalChapterRepository(),
    )
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.sync_chapter_narrative_after_save",
        should_not_run,
    )

    result = await pipeline.run_after_chapter_saved("novel-1", 2, "人工改写正文")

    assert result["discarded_uncommitted"] is True
    assert result["failure_reason"] == "candidate_first_required"
    assert called is False


@pytest.mark.asyncio
async def test_aftermath_primary_sync_failure_does_not_publish_downstream_derivations(monkeypatch):
    probes = _side_effect_probes()
    pipeline = _canonical_pipeline(probes)

    async def failed_sync(*args, **kwargs):
        return {"narrative_sync_ok": False, "failure_reason": "primary_sync_failed"}

    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.sync_chapter_narrative_after_save",
        failed_sync,
    )

    result = await pipeline.run_after_chapter_saved("novel-1", 2, "正式正文")
    await pipeline.drain_auxiliary_stages()

    assert result["narrative_sync_ok"] is False
    assert result["failure_reason"] == "primary_sync_failed"
    probes["bridge"].assert_not_awaited()
    probes["reconcile"].assert_not_called()
    probes["auxiliary"].assert_not_awaited()


@pytest.mark.asyncio
async def test_aftermath_durable_memory_failure_does_not_publish_downstream_derivations(
    monkeypatch,
):
    probes = _side_effect_probes()
    pipeline = _canonical_pipeline(probes, memory_engine=_FailingMemoryEngine())
    monkeypatch.setattr(
        "infrastructure.persistence.database.sqlite_chapter_narrative_commit_repository.SqliteChapterNarrativeCommitRepository",
        _MemoryCommitRepository,
    )
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.sync_chapter_narrative_after_save",
        AsyncMock(
            return_value={
                "narrative_sync_ok": True,
                "content_sha256": hashlib.sha256("正式正文".encode("utf-8")).hexdigest(),
                "content_revision": 2,
            }
        ),
    )

    result = await pipeline.run_after_chapter_saved("novel-1", 2, "正式正文")
    await pipeline.drain_auxiliary_stages()

    assert result["narrative_sync_ok"] is False
    assert result["failure_reason"] == "memory_engine_update_failed"
    probes["bridge"].assert_not_awaited()
    probes["reconcile"].assert_not_called()
    probes["auxiliary"].assert_not_awaited()
