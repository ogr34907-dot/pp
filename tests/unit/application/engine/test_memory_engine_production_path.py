import asyncio
import json
from types import SimpleNamespace

import pytest

import application.engine.services.chapter_aftermath_pipeline as aftermath_pipeline_module
from application.engine.services.chapter_aftermath_pipeline import ChapterAftermathPipeline
from application.engine.services.memory_engine import MemoryEngine
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
async def test_memory_engine_recovers_transient_failure_within_three_attempts(
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
        "source_content_revision, pipeline_version, sync_status, sync_attempts) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            "summary-1",
            "knowledge-1",
            1,
            "已提交的规范摘要",
            chapter.content_sha256,
            chapter.content_revision,
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

    class TransientMemoryEngine:
        def __init__(self):
            self.calls = 0

        async def update_from_chapter(self, *_args):
            self.calls += 1
            if self.calls < 3:
                return {"errors": ["injected transient memory failure"]}
            return {"errors": [], "new_beats": 1, "new_clues": 1}

    memory_engine = TransientMemoryEngine()
    monkeypatch.setattr(ChapterAftermathPipeline, "_extract_chapter_bridge", _skip_bridge)
    monkeypatch.setattr(ChapterAftermathPipeline, "_run_auxiliary_stages", _skip_auxiliary)
    monkeypatch.setattr(aftermath_pipeline_module, "_retry_delay_seconds", lambda _attempt: 0)
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.sync_chapter_narrative_after_save",
        canonical_sync,
    )
    pipeline = ChapterAftermathPipeline(
        knowledge_service=None,
        chapter_indexing_service=None,
        llm_service=object(),
        chapter_repository=chapter_repository,
        memory_engine=memory_engine,
    )

    result = await pipeline.run_after_chapter_saved(
        "novel-1",
        1,
        chapter.content,
        expected_content_sha256=chapter.content_sha256,
        expected_content_revision=chapter.content_revision,
    )

    repository = SqliteChapterNarrativeCommitRepository(db)
    assert result["narrative_sync_ok"] is True
    assert memory_engine.calls == 3
    assert dict(db.fetch_one(
        "SELECT memory_status, memory_failure_reason, memory_attempt_count "
        "FROM chapter_narrative_commits"
    )) == {
        "memory_status": "committed",
        "memory_failure_reason": "",
        "memory_attempt_count": 3,
    }
    assert repository.is_current_version_ready(
        novel_id="novel-1",
        chapter_number=1,
        pipeline_version="chapter-narrative-sync:v1",
        require_memory_sync=True,
    ) is True
    await pipeline.drain_auxiliary_stages()


def _canonical_memory_fixture(tmp_path, filename):
    db = DatabaseConnection(str(tmp_path / filename))
    db.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'Novel', 'novel-1')"
    )
    chapter_repository = SqliteChapterRepository(db)
    chapter_repository.save(
        Chapter(
            id="chapter-1",
            novel_id=NovelId("novel-1"),
            number=1,
            title="Chapter",
            content="final prose",
            status=ChapterStatus.COMPLETED,
        )
    )
    chapter = chapter_repository.get_by_novel_and_number(NovelId("novel-1"), 1)
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
    db.get_connection().commit()
    return db, chapter_repository, chapter


class _NoBible:
    def get_by_novel_id(self, _novel_id):
        return None


class _DivergentMemoryExtraction:
    def __init__(self):
        self.calls = 0

    async def generate(self, _prompt, _config):
        self.calls += 1
        suffix = "first" if self.calls == 1 else "divergent-retry"
        return SimpleNamespace(
            content=json.dumps(
                {
                    "completed_beats": [
                        {
                            "beat_id": f"beat-{suffix}",
                            "summary": f"beat {suffix}",
                            "chapter": 1,
                        }
                    ],
                    "revealed_clues": [
                        {
                            "clue_id": f"clue-{suffix}",
                            "content": f"clue {suffix}",
                            "revealed_at_chapter": 1,
                        }
                    ],
                    "fact_violations": [],
                }
            )
        )


def _install_memory_aftermath_stubs(monkeypatch, chapter):
    async def canonical_sync(*_args, **_kwargs):
        return {
            "narrative_sync_ok": True,
            "content_sha256": chapter.content_sha256,
            "content_revision": chapter.content_revision,
        }

    monkeypatch.setattr(ChapterAftermathPipeline, "_extract_chapter_bridge", _skip_bridge)
    monkeypatch.setattr(ChapterAftermathPipeline, "_run_auxiliary_stages", _skip_auxiliary)
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.sync_chapter_narrative_after_save",
        canonical_sync,
    )
    monkeypatch.setattr(
        "application.engine.services.memory_engine.get_prompt_gateway",
        lambda: SimpleNamespace(
            render=lambda *_args, **_kwargs: SimpleNamespace(prompt="memory prompt")
        ),
    )


def _durable_memory_payload(db):
    row = db.fetch_one(
        "SELECT state_json FROM memory_engine_state WHERE novel_id = 'novel-1'"
    )
    return json.loads(row["state_json"])


def test_stale_memory_claim_is_recovered_after_lease_expiry(tmp_path):
    db, _chapter_repository, chapter = _canonical_memory_fixture(
        tmp_path, "stale-memory-claim.db"
    )
    repository = SqliteChapterNarrativeCommitRepository(db)
    claim_kwargs = {
        "novel_id": "novel-1",
        "chapter_number": 1,
        "content_sha256": chapter.content_sha256,
        "pipeline_version": "chapter-narrative-sync:v1",
        "content_revision": chapter.content_revision,
    }

    assert repository.claim_memory_sync(**claim_kwargs) == "claimed"
    db.execute(
        "UPDATE chapter_narrative_commits SET updated_at = ?",
        ("2000-01-01T00:00:00+00:00",),
    )
    db.commit()

    assert repository.claim_memory_sync(**claim_kwargs) == "claimed"
    recovered = db.fetch_one(
        "SELECT memory_status, memory_attempt_count FROM chapter_narrative_commits"
    )
    assert dict(recovered) == {
        "memory_status": "in_progress",
        "memory_attempt_count": 2,
    }


@pytest.mark.asyncio
async def test_memory_engine_retries_three_times_then_closes_the_gate(
    monkeypatch, tmp_path
):
    db, chapter_repository, chapter = _canonical_memory_fixture(
        tmp_path, "memory-retry-limit.db"
    )
    _install_memory_aftermath_stubs(monkeypatch, chapter)
    monkeypatch.setattr(
        aftermath_pipeline_module,
        "_retry_delay_seconds",
        lambda _attempt: 0,
        raising=False,
    )

    class AlwaysFailingMemoryEngine:
        def __init__(self):
            self.calls = 0

        async def update_from_chapter(self, *_args):
            self.calls += 1
            return {"errors": ["injected memory failure"]}

    memory_engine = AlwaysFailingMemoryEngine()
    pipeline = ChapterAftermathPipeline(
        knowledge_service=None,
        chapter_indexing_service=None,
        llm_service=object(),
        chapter_repository=chapter_repository,
        memory_engine=memory_engine,
    )

    result = await pipeline.run_after_chapter_saved(
        "novel-1",
        1,
        chapter.content,
        expected_content_sha256=chapter.content_sha256,
        expected_content_revision=chapter.content_revision,
    )

    row = db.fetch_one(
        "SELECT memory_status, memory_attempt_count "
        "FROM chapter_narrative_commits WHERE novel_id = 'novel-1'"
    )
    assert memory_engine.calls == 3
    assert result["narrative_sync_ok"] is False
    assert result["failure_reason"] == "memory_engine_update_failed"
    assert dict(row) == {"memory_status": "failed", "memory_attempt_count": 3}

    exhausted = await pipeline.run_after_chapter_saved(
        "novel-1",
        1,
        chapter.content,
        expected_content_sha256=chapter.content_sha256,
        expected_content_revision=chapter.content_revision,
    )
    assert exhausted["failure_reason"] == "memory_engine_sync_exhausted"
    assert memory_engine.calls == 3
    await pipeline.drain_auxiliary_stages()


@pytest.mark.asyncio
async def test_same_canonical_version_does_not_reextract_divergent_memory_ids(
    monkeypatch, tmp_path
):
    db, chapter_repository, chapter = _canonical_memory_fixture(
        tmp_path, "same-version-memory.db"
    )
    _install_memory_aftermath_stubs(monkeypatch, chapter)
    extraction = _DivergentMemoryExtraction()
    pipeline = ChapterAftermathPipeline(
        knowledge_service=None,
        chapter_indexing_service=None,
        llm_service=extraction,
        chapter_repository=chapter_repository,
        memory_engine=MemoryEngine(extraction, _NoBible(), db),
    )

    first = await pipeline.run_after_chapter_saved(
        "novel-1",
        1,
        chapter.content,
        expected_content_sha256=chapter.content_sha256,
        expected_content_revision=chapter.content_revision,
    )
    repeated = await pipeline.run_after_chapter_saved(
        "novel-1",
        1,
        chapter.content,
        expected_content_sha256=chapter.content_sha256,
        expected_content_revision=chapter.content_revision,
    )

    payload = _durable_memory_payload(db)
    assert first["memory_engine_new_beats"] == 1
    assert repeated["memory_engine_new_beats"] == 0
    assert extraction.calls == 1
    assert [beat["beat_id"] for beat in payload["completed_beats"]] == ["beat-first"]
    assert [clue["clue_id"] for clue in payload["revealed_clues"]] == ["clue-first"]
    await pipeline.drain_auxiliary_stages()


@pytest.mark.asyncio
async def test_persisted_memory_repairs_failed_commit_barrier_without_duplicate_state(
    monkeypatch, tmp_path
):
    db, chapter_repository, chapter = _canonical_memory_fixture(
        tmp_path, "memory-barrier-repair.db"
    )
    _install_memory_aftermath_stubs(monkeypatch, chapter)
    monkeypatch.setattr(aftermath_pipeline_module, "_retry_delay_seconds", lambda _attempt: 0)
    extraction = _DivergentMemoryExtraction()
    pipeline = ChapterAftermathPipeline(
        knowledge_service=None,
        chapter_indexing_service=None,
        llm_service=extraction,
        chapter_repository=chapter_repository,
        memory_engine=MemoryEngine(extraction, _NoBible(), db),
    )
    original_set_status = SqliteChapterNarrativeCommitRepository.set_memory_sync_status
    failed_commit = False

    def fail_first_committed_status(self, **kwargs):
        nonlocal failed_commit
        if kwargs["memory_status"] == "committed" and not failed_commit:
            failed_commit = True
            return False
        return original_set_status(self, **kwargs)

    monkeypatch.setattr(
        SqliteChapterNarrativeCommitRepository,
        "set_memory_sync_status",
        fail_first_committed_status,
    )

    interrupted = await pipeline.run_after_chapter_saved(
        "novel-1",
        1,
        chapter.content,
        expected_content_sha256=chapter.content_sha256,
        expected_content_revision=chapter.content_revision,
    )
    retry_pipeline = ChapterAftermathPipeline(
        knowledge_service=None,
        chapter_indexing_service=None,
        llm_service=extraction,
        chapter_repository=chapter_repository,
        memory_engine=MemoryEngine(extraction, _NoBible(), db),
    )
    retried = await retry_pipeline.run_after_chapter_saved(
        "novel-1",
        1,
        chapter.content,
        expected_content_sha256=chapter.content_sha256,
        expected_content_revision=chapter.content_revision,
    )

    payload = _durable_memory_payload(db)
    barrier = db.fetch_one(
        "SELECT memory_status FROM chapter_narrative_commits WHERE novel_id = 'novel-1'"
    )
    assert interrupted["narrative_sync_ok"] is True
    assert retried["narrative_sync_ok"] is True
    assert extraction.calls == 1
    assert [beat["beat_id"] for beat in payload["completed_beats"]] == ["beat-first"]
    assert [clue["clue_id"] for clue in payload["revealed_clues"]] == ["clue-first"]
    assert barrier["memory_status"] == "committed"
    await pipeline.drain_auxiliary_stages()
    await retry_pipeline.drain_auxiliary_stages()


@pytest.mark.asyncio
async def test_canonical_memory_is_durable_before_barrier_under_write_dispatch(
    monkeypatch, tmp_path
):
    from application.engine.services import persistence_queue as persistence_queue_module
    from application.engine.services.persistence_queue import PersistenceQueue

    db, chapter_repository, chapter = _canonical_memory_fixture(
        tmp_path, "memory-write-dispatch.db"
    )
    _install_memory_aftermath_stubs(monkeypatch, chapter)
    extraction = _DivergentMemoryExtraction()
    queue = PersistenceQueue()
    queue.initialize()
    monkeypatch.setattr(persistence_queue_module, "_persistence_queue", queue)
    monkeypatch.setenv("PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES", "0")
    pipeline = ChapterAftermathPipeline(
        knowledge_service=None,
        chapter_indexing_service=None,
        llm_service=extraction,
        chapter_repository=chapter_repository,
        memory_engine=MemoryEngine(extraction, _NoBible(), db),
    )

    result = await pipeline.run_after_chapter_saved(
        "novel-1",
        1,
        chapter.content,
        expected_content_sha256=chapter.content_sha256,
        expected_content_revision=chapter.content_revision,
    )

    state_row = db.fetch_one(
        "SELECT state_json FROM memory_engine_state WHERE novel_id = 'novel-1'"
    )
    barrier = db.fetch_one(
        "SELECT memory_status FROM chapter_narrative_commits WHERE novel_id = 'novel-1'"
    )
    assert result["narrative_sync_ok"] is True
    assert state_row is not None
    assert json.loads(state_row["state_json"])["applied_aftermath_versions"] == [
        {
            "novel_id": "novel-1",
            "chapter_number": 1,
            "content_sha256": chapter.content_sha256,
            "content_revision": chapter.content_revision,
        }
    ]
    assert barrier["memory_status"] == "committed"
    assert queue.get_stats()["queued"] == 0
    await pipeline.drain_auxiliary_stages()


@pytest.mark.asyncio
async def test_midflight_rewrite_cannot_merge_stale_memory(monkeypatch, tmp_path):
    db, chapter_repository, chapter = _canonical_memory_fixture(
        tmp_path, "midflight-memory-rewrite.db"
    )
    _install_memory_aftermath_stubs(monkeypatch, chapter)
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingExtraction(_DivergentMemoryExtraction):
        async def generate(self, prompt, config):
            started.set()
            await release.wait()
            return await super().generate(prompt, config)

    extraction = BlockingExtraction()
    pipeline = ChapterAftermathPipeline(
        knowledge_service=None,
        chapter_indexing_service=None,
        llm_service=extraction,
        chapter_repository=chapter_repository,
        memory_engine=MemoryEngine(extraction, _NoBible(), db),
    )
    task = asyncio.create_task(
        pipeline.run_after_chapter_saved(
            "novel-1",
            1,
            chapter.content,
            expected_content_sha256=chapter.content_sha256,
            expected_content_revision=chapter.content_revision,
        )
    )
    await started.wait()
    chapter_repository.save(
        Chapter(
            id="chapter-1",
            novel_id=NovelId("novel-1"),
            number=1,
            title="Chapter",
            content="rewritten prose",
            status=ChapterStatus.COMPLETED,
        )
    )
    release.set()

    result = await task
    state_row = db.fetch_one(
        "SELECT state_json FROM memory_engine_state WHERE novel_id = 'novel-1'"
    )
    assert result["discarded_stale"] is True
    assert result["failure_reason"] == "source_version_mismatch"
    assert state_row is None
    await pipeline.drain_auxiliary_stages()


@pytest.mark.asyncio
async def test_concurrent_same_version_pauses_follower_without_reextracting_memory(
    monkeypatch, tmp_path
):
    db, chapter_repository, chapter = _canonical_memory_fixture(
        tmp_path, "concurrent-canonical-memory.db"
    )
    _install_memory_aftermath_stubs(monkeypatch, chapter)
    started = asyncio.Event()
    release = asyncio.Event()

    class BlockingExtraction(_DivergentMemoryExtraction):
        async def generate(self, prompt, config):
            started.set()
            await release.wait()
            return await super().generate(prompt, config)

    extraction = BlockingExtraction()
    memory_engine = MemoryEngine(extraction, _NoBible(), db)
    pipeline = ChapterAftermathPipeline(
        knowledge_service=None,
        chapter_indexing_service=None,
        llm_service=extraction,
        chapter_repository=chapter_repository,
        memory_engine=memory_engine,
    )
    first = asyncio.create_task(
        pipeline.run_after_chapter_saved(
            "novel-1",
            1,
            chapter.content,
            expected_content_sha256=chapter.content_sha256,
            expected_content_revision=chapter.content_revision,
        )
    )
    await started.wait()
    second = asyncio.create_task(
        pipeline.run_after_chapter_saved(
            "novel-1",
            1,
            chapter.content,
            expected_content_sha256=chapter.content_sha256,
            expected_content_revision=chapter.content_revision,
        )
    )
    await asyncio.sleep(0)
    release.set()
    results = await asyncio.gather(first, second)

    payload = _durable_memory_payload(db)
    assert results[0]["narrative_sync_ok"] is True
    assert results[1]["narrative_sync_ok"] is False
    assert results[1]["failure_reason"] == "memory_engine_sync_in_progress"
    assert results[1]["memory_status"] == "in_progress"
    assert extraction.calls == 1
    assert [beat["beat_id"] for beat in payload["completed_beats"]] == ["beat-first"]
    assert [clue["clue_id"] for clue in payload["revealed_clues"]] == ["clue-first"]
    await pipeline.drain_auxiliary_stages()


@pytest.mark.asyncio
async def test_committed_memory_barrier_reuses_without_reextracting(monkeypatch, tmp_path):
    db, chapter_repository, chapter = _canonical_memory_fixture(
        tmp_path, "pending-memory-race.db"
    )
    _install_memory_aftermath_stubs(monkeypatch, chapter)
    marker = {
        "novel_id": "novel-1",
        "chapter_number": 1,
        "content_sha256": chapter.content_sha256,
        "content_revision": chapter.content_revision,
    }
    db.execute(
        "INSERT INTO memory_engine_state (novel_id, state_json, last_updated_chapter) "
        "VALUES (?, ?, ?)",
        ("novel-1", json.dumps({"applied_aftermath_versions": [marker]}), 1),
    )
    db.commit()
    assert SqliteChapterNarrativeCommitRepository(db).set_memory_sync_status(
        novel_id="novel-1",
        chapter_number=1,
        content_sha256=chapter.content_sha256,
        pipeline_version="chapter-narrative-sync:v1",
        content_revision=chapter.content_revision,
        memory_status="committed",
    ) is True

    class MustNotExtract:
        async def update_canonical_version_from_chapter(self, *_args, **_kwargs):
            raise AssertionError("committed memory barrier must be reused")

    pipeline = ChapterAftermathPipeline(
        knowledge_service=None,
        chapter_indexing_service=None,
        llm_service=object(),
        chapter_repository=chapter_repository,
        memory_engine=MustNotExtract(),
    )
    result = await pipeline.run_after_chapter_saved(
        "novel-1",
        1,
        chapter.content,
        expected_content_sha256=chapter.content_sha256,
        expected_content_revision=chapter.content_revision,
    )

    assert result["narrative_sync_ok"] is True
    assert result["memory_engine_ok"] is True
    assert result["memory_status"] == "committed"
    await pipeline.drain_auxiliary_stages()
