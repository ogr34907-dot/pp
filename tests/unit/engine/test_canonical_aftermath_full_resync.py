import asyncio
import hashlib

import pytest

from infrastructure.persistence.database.connection import DatabaseConnection
from application.world.services.chapter_narrative_sync import CHAPTER_NARRATIVE_PIPELINE_VERSION


def _seed(db: DatabaseConnection) -> dict[int, tuple[str, int, str]]:
    db.execute(
        "INSERT INTO novels (id, title, slug, autopilot_status, current_stage) "
        "VALUES ('novel-1', 'Demo', 'demo', 'running', 'writing')"
    )
    values = {}
    for number, content, revision in (
        (1, "chapter one", 1),
        (2, "chapter two", 4),
        (3, "chapter three", 2),
    ):
        digest = hashlib.sha256(content.encode()).hexdigest()
        values[number] = (content, revision, digest)
        db.execute(
            "INSERT INTO chapters (id, novel_id, number, content, content_sha256, "
            "content_revision, status) VALUES (?, 'novel-1', ?, ?, ?, ?, 'completed')",
            (f"chapter-{number}", number, content, digest, revision),
        )
    db.execute("INSERT INTO knowledge (id, novel_id) VALUES ('knowledge-1', 'novel-1')")
    db.execute(
        "INSERT INTO chapter_narrative_commits "
        "(novel_id, chapter_number, content_sha256, pipeline_version, content_revision, "
        "status, attempt_count, vector_status, memory_status) "
        "VALUES ('novel-1', 1, ?, ?, 1, 'committed', 1, 'stored', 'committed')",
        (values[1][2], CHAPTER_NARRATIVE_PIPELINE_VERSION),
    )
    db.execute(
        "INSERT INTO chapter_summaries "
        "(id, knowledge_id, chapter_number, summary, source_content_sha256, "
        "source_content_revision, pipeline_version, sync_status, sync_attempts) "
        "VALUES ('summary-1', 'knowledge-1', 1, 'ready', ?, 1, ?, 'committed', 1)",
        (values[1][2], CHAPTER_NARRATIVE_PIPELINE_VERSION),
    )
    db.commit()
    return values


class FakePipeline:
    def __init__(self, db: DatabaseConnection, values: dict[int, tuple[str, int, str]]):
        self.db = db
        self.values = values
        self.calls: list[tuple[int, str, int]] = []
        self.fail_chapter_three = True

    async def run_after_chapter_saved(self, novel_id, chapter_number, content, **kwargs):
        self.calls.append((chapter_number, kwargs["expected_content_sha256"], kwargs["expected_content_revision"]))
        if chapter_number == 3 and self.fail_chapter_three:
            self.fail_chapter_three = False
            return {"narrative_sync_ok": False, "failure_reason": "API returned empty content"}
        digest = kwargs["expected_content_sha256"]
        revision = kwargs["expected_content_revision"]
        self.db.execute(
            "INSERT OR REPLACE INTO chapter_narrative_commits "
            "(novel_id, chapter_number, content_sha256, pipeline_version, content_revision, "
            "status, attempt_count, vector_status, memory_status) VALUES (?, ?, ?, ?, ?, 'committed', 1, 'stored', 'committed')",
            (novel_id, chapter_number, digest, CHAPTER_NARRATIVE_PIPELINE_VERSION, revision),
        )
        self.db.execute(
            "INSERT OR REPLACE INTO chapter_summaries "
            "(id, knowledge_id, chapter_number, summary, source_content_sha256, source_content_revision, pipeline_version, sync_status, sync_attempts) "
            "VALUES (?, 'knowledge-1', ?, 'ready', ?, ?, ?, 'committed', 1)",
            (f"summary-{chapter_number}", chapter_number, digest, revision, CHAPTER_NARRATIVE_PIPELINE_VERSION),
        )
        self.db.commit()
        return {"narrative_sync_ok": True, "vector_stored": True}


@pytest.mark.asyncio
async def test_order_skip_sync_stop_and_pause(tmp_path):
    from application.engine.services.canonical_aftermath_full_resync import resync_all_completed_chapters

    db = DatabaseConnection(str(tmp_path / "resync.db"))
    values = _seed(db)
    pipeline = FakePipeline(db, values)
    result = await resync_all_completed_chapters(novel_id="novel-1", database=db, aftermath_pipeline=pipeline)
    assert result.status == "failed"
    assert result.failed_chapter == 3
    assert result.failure_reason == "API returned empty content"
    assert result.remains_paused is True
    novel_state = db.fetch_one("SELECT autopilot_status, current_stage FROM novels WHERE id='novel-1'")
    assert novel_state == {"autopilot_status": "stopped", "current_stage": "paused_for_review"}
    assert pipeline.calls == [(2, values[2][2], values[2][1]), (3, values[3][2], values[3][1])]
    assert result.skipped_count == 1
    assert result.synced_count == 1


@pytest.mark.asyncio
async def test_resume_is_idempotent_and_rejects_concurrent_claim(tmp_path):
    from application.engine.services.canonical_aftermath_full_resync import resync_all_completed_chapters

    db = DatabaseConnection(str(tmp_path / "resync.db"))
    values = _seed(db)
    pipeline = FakePipeline(db, values)
    first = await resync_all_completed_chapters(novel_id="novel-1", database=db, aftermath_pipeline=pipeline)
    assert first.failed_chapter == 3
    fixed_hash = hashlib.sha256(b"chapter three fixed").hexdigest()
    db.execute("UPDATE chapters SET status='completed', content='chapter three fixed', content_sha256=?, content_revision=3 WHERE novel_id='novel-1' AND number=3", (fixed_hash,))
    db.commit()
    second = await resync_all_completed_chapters(novel_id="novel-1", database=db, aftermath_pipeline=pipeline)
    assert second.status == "completed", second
    assert [call[0] for call in pipeline.calls] == [2, 3, 3]

    gate = asyncio.Event()
    db.execute("DELETE FROM chapter_narrative_commits WHERE chapter_number=2")
    db.execute("DELETE FROM chapter_summaries WHERE chapter_number=2")
    db.commit()
    class BlockingPipeline(FakePipeline):
        async def run_after_chapter_saved(self, *args, **kwargs):
            gate.set()
            await asyncio.sleep(0.05)
            return await super().run_after_chapter_saved(*args, **kwargs)

    db.execute("UPDATE novels SET autopilot_recovery_reason='' WHERE id='novel-1'")
    db.commit()
    blocking = BlockingPipeline(db, values)
    task = asyncio.create_task(resync_all_completed_chapters(novel_id="novel-1", database=db, aftermath_pipeline=blocking))
    await gate.wait()
    conflict = await resync_all_completed_chapters(novel_id="novel-1", database=db, aftermath_pipeline=blocking)
    assert conflict.status == "conflict"
    await task


@pytest.mark.asyncio
async def test_source_version_mutation_stops_without_stale_write(tmp_path):
    from application.engine.services.canonical_aftermath_full_resync import resync_all_completed_chapters

    db = DatabaseConnection(str(tmp_path / "resync.db"))
    values = _seed(db)
    class MutatingPipeline(FakePipeline):
        async def run_after_chapter_saved(self, novel_id, chapter_number, content, **kwargs):
            if chapter_number == 2:
                self.db.execute("UPDATE chapters SET content='rewritten', content_revision=5 WHERE novel_id='novel-1' AND number=2")
                self.db.commit()
                self.calls.append((chapter_number, kwargs["expected_content_sha256"], kwargs["expected_content_revision"]))
                return {"narrative_sync_ok": True}
            return await super().run_after_chapter_saved(novel_id, chapter_number, content, **kwargs)

    pipeline = MutatingPipeline(db, values)
    result = await resync_all_completed_chapters(novel_id="novel-1", database=db, aftermath_pipeline=pipeline)
    assert result.status == "failed"
    assert result.failed_chapter == 2
    assert result.failure_reason == "source_version_mismatch"
    assert db.fetch_one("SELECT 1 FROM chapter_narrative_commits WHERE chapter_number=2") is None


@pytest.mark.asyncio
async def test_cancellation_marks_marker_and_can_resume_immediately(tmp_path):
    from application.engine.services.canonical_aftermath_full_resync import resync_all_completed_chapters

    db = DatabaseConnection(str(tmp_path / "cancel.db"))
    values = _seed(db)
    entered = asyncio.Event()

    class Cancellable(FakePipeline):
        async def run_after_chapter_saved(self, *args, **kwargs):
            entered.set()
            await asyncio.sleep(10)

    task = asyncio.create_task(resync_all_completed_chapters(novel_id="novel-1", database=db, aftermath_pipeline=Cancellable(db, values)))
    await entered.wait()
    task.cancel()
    cancelled = await task
    assert cancelled.status == "cancelled"
    marker = db.fetch_one("SELECT autopilot_recovery_reason FROM novels WHERE id='novel-1'")["autopilot_recovery_reason"]
    assert "|cancelled" in marker

    resumed = await resync_all_completed_chapters(novel_id="novel-1", database=db, aftermath_pipeline=FakePipeline(db, values))
    assert resumed.status == "failed"  # chapter 3 remains the first hard failure


@pytest.mark.asyncio
async def test_unavailable_dependency_and_pipeline_shape_are_reported(tmp_path):
    from application.engine.services.canonical_aftermath_full_resync import resync_all_completed_chapters

    unavailable = await resync_all_completed_chapters(novel_id="novel-1", database=None, aftermath_pipeline=None)
    assert unavailable.status == "unavailable"

    class MissingMemory:
        _memory_engine = None
    db_missing = DatabaseConnection(str(tmp_path / "missing-memory.db"))
    _seed(db_missing)
    missing_memory = await resync_all_completed_chapters(novel_id="novel-1", database=db_missing, aftermath_pipeline=MissingMemory())
    assert missing_memory.status == "unavailable"

    db = DatabaseConnection(str(tmp_path / "unavailable.db"))
    _seed(db)
    class BrokenPipeline(FakePipeline):
        async def run_after_chapter_saved(self, *args, **kwargs):
            return None
    events = []
    result = await resync_all_completed_chapters(novel_id="novel-1", database=db, aftermath_pipeline=BrokenPipeline(db, {}), emit=events.append)
    assert result.status == "unavailable"
    assert result.failure_reason == "pipeline_invalid_result"
    assert result.failed_chapter == 2
    assert events[-1]["type"] == "failed"
    assert events[-1]["chapter_number"] == 2


@pytest.mark.asyncio
async def test_progress_and_event_fields_are_durable(tmp_path):
    from application.engine.services.canonical_aftermath_full_resync import resync_all_completed_chapters

    db = DatabaseConnection(str(tmp_path / "events.db"))
    values = _seed(db)
    events = []
    class VectorFailure(FakePipeline):
        async def run_after_chapter_saved(self, novel_id, chapter_number, content, **kwargs):
            if chapter_number == 2:
                self.db.execute("INSERT OR REPLACE INTO chapter_narrative_commits (novel_id, chapter_number, content_sha256, pipeline_version, content_revision, status, attempt_count, vector_status, memory_status) VALUES (?, ?, ?, ?, ?, 'committed', 1, 'failed', 'committed')", (novel_id, chapter_number, kwargs['expected_content_sha256'], CHAPTER_NARRATIVE_PIPELINE_VERSION, kwargs['expected_content_revision']))
                self.db.commit()
                return {"narrative_sync_ok": True, "vector_stored": False}
            return await super().run_after_chapter_saved(novel_id, chapter_number, content, **kwargs)
    db.execute("INSERT INTO chapter_narrative_commits (novel_id, chapter_number, content_sha256, pipeline_version, content_revision, status, attempt_count, vector_status, memory_status) VALUES ('novel-1', 2, ?, ?, ?, 'committed', 1, 'failed', 'committed')", (values[2][2], CHAPTER_NARRATIVE_PIPELINE_VERSION, values[2][1]))
    db.execute("INSERT INTO chapter_summaries (id, knowledge_id, chapter_number, summary, source_content_sha256, source_content_revision, pipeline_version, sync_status, sync_attempts) VALUES ('summary-2', 'knowledge-1', 2, 'ready', ?, ?, ?, 'committed', 1)", (values[2][2], values[2][1], CHAPTER_NARRATIVE_PIPELINE_VERSION))
    db.commit()
    result = await resync_all_completed_chapters(novel_id="novel-1", database=db, aftermath_pipeline=VectorFailure(db, values), emit=events.append)
    assert result.vector_failed_chapters == [2]
    assert events[0]["type"] == "started"
    assert events[0]["pending_chapters"] == 2
    assert events[-1]["type"] == "failed"
    assert {"processed", "synced", "skipped", "total"} <= events[1].keys()
    marker = db.fetch_one("SELECT autopilot_recovery_reason FROM novels")["autopilot_recovery_reason"]
    assert "chapter=3" in marker and "processed=2" in marker and "total=3" in marker


def test_full_resync_marker_cas_across_database_connections(tmp_path):
    from infrastructure.persistence.database.sqlite_chapter_narrative_commit_repository import SqliteChapterNarrativeCommitRepository

    path = str(tmp_path / "lease.db")
    first = DatabaseConnection(path)
    _seed(first)
    second = DatabaseConnection(path)
    repo_one = SqliteChapterNarrativeCommitRepository(first)
    repo_two = SqliteChapterNarrativeCommitRepository(second)
    assert repo_one.claim_full_resync(novel_id="novel-1", run_id="run-one")
    assert not repo_two.claim_full_resync(novel_id="novel-1", run_id="run-two")
    assert repo_two.renew_full_resync(novel_id="novel-1", run_id="run-one", chapter_number=2, processed_count=1, total_chapters=3)
    marker = second.fetch_one("SELECT autopilot_recovery_reason FROM novels WHERE id='novel-1'")["autopilot_recovery_reason"]
    assert "chapter=2" in marker and "processed=1" in marker and "total=3" in marker
    assert repo_one.mark_full_resync_failure(novel_id="novel-1", run_id="run-one", reason="db down", status="unavailable")
    assert repo_two.claim_full_resync(novel_id="novel-1", run_id="run-two")
