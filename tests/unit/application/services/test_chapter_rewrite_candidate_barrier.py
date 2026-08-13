"""A formal rewrite must retire downstream candidate work before rebuild."""

import pytest

from application.core.services.chapter_rewrite_coordinator import (
    ChapterRewriteConflictError,
    ChapterRewriteCoordinator,
)
from application.engine.services.worldline_rebuild_service import WorldlineRebuildService
from application.world.services.chapter_narrative_sync import (
    CHAPTER_NARRATIVE_PIPELINE_VERSION,
)
from domain.knowledge.chapter_summary import canonical_summary_payload_sha256
from domain.novel.candidate_chapter import CandidateStatus, GenerationRunState, RunMode
from domain.novel.value_objects.novel_id import NovelId
from infrastructure.persistence.database.chapter_candidate_repository import (
    CandidateGateError,
    ChapterCandidateRepository,
)
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_chapter_repository import (
    SqliteChapterRepository,
)
from infrastructure.persistence.database.sqlite_chapter_narrative_commit_repository import (
    SqliteChapterNarrativeCommitRepository,
)


class _SuccessfulAftermath:
    def __init__(self, db):
        self.db = db

    async def run_after_chapter_saved(self, *_args, **_kwargs):
        novel_id, chapter_number, content = _args[:3]
        content_sha256 = str(_kwargs["expected_content_sha256"])
        content_revision = int(_kwargs["expected_content_revision"])
        connection = self.db.get_connection()
        connection.execute(
            "INSERT OR IGNORE INTO knowledge (id, novel_id) VALUES ('knowledge-1', ?)",
            (novel_id,),
        )
        summary = {
            "summary": content,
            "key_events": content,
            "open_threads": "",
            "consistency_note": "",
            "beat_sections": [],
            "micro_beats": [],
        }
        payload_sha256 = canonical_summary_payload_sha256(**summary)
        connection.execute(
            "INSERT OR REPLACE INTO chapter_summaries "
            "(id, knowledge_id, chapter_number, summary, key_events, open_threads, "
            "consistency_note, beat_sections, micro_beats, source_content_sha256, "
            "source_content_revision, pipeline_version, sync_status, sync_attempts, "
            "canonical_payload_sha256) VALUES (?, 'knowledge-1', ?, ?, ?, ?, ?, '[]', '[]', ?, ?, ?, 'draft', 0, ?)",
            (
                f"summary-{chapter_number}",
                chapter_number,
                summary["summary"],
                summary["key_events"],
                summary["open_threads"],
                summary["consistency_note"],
                content_sha256,
                content_revision,
                CHAPTER_NARRATIVE_PIPELINE_VERSION,
                payload_sha256,
            ),
        )
        connection.commit()
        repository = SqliteChapterNarrativeCommitRepository(self.db)
        claim = repository.claim(
            novel_id=novel_id,
            chapter_number=chapter_number,
            content_sha256=content_sha256,
            pipeline_version=CHAPTER_NARRATIVE_PIPELINE_VERSION,
            expected_content_revision=content_revision,
        )
        repository.prepare_summary(
            novel_id=novel_id,
            chapter_number=chapter_number,
            content_sha256=content_sha256,
            pipeline_version=CHAPTER_NARRATIVE_PIPELINE_VERSION,
            attempt_count=claim.attempt_count,
            canonical_payload_sha256=payload_sha256,
        )
        repository.commit(
            novel_id=novel_id,
            chapter_number=chapter_number,
            content_sha256=content_sha256,
            pipeline_version=CHAPTER_NARRATIVE_PIPELINE_VERSION,
            attempt_count=claim.attempt_count,
            content_revision=content_revision,
            canonical_payload_sha256=payload_sha256,
        )
        return {
            "narrative_sync_ok": True,
            "commit_status": "committed",
            "pipeline_version": CHAPTER_NARRATIVE_PIPELINE_VERSION,
            "content_revision": content_revision,
        }


class _RecordingAftermath(_SuccessfulAftermath):
    def __init__(self, db):
        super().__init__(db)
        self.chapter_numbers = []

    async def run_after_chapter_saved(self, _novel_id, chapter_number, *_args, **_kwargs):
        self.chapter_numbers.append(chapter_number)
        return await super().run_after_chapter_saved(
            _novel_id, chapter_number, *_args, **_kwargs
        )


def _outline_chain() -> dict:
    return {"outline": {"contract_id": "root", "digest": "root-v1"}}


def _commit_first_chapter(repository: ChapterCandidateRepository) -> None:
    candidate = repository.create_streaming_candidate(
        novel_id="novel-1",
        chapter_number=1,
        title="第一章",
        outline_chain=_outline_chain(),
        llm_content="第一章原正文",
    )
    repository.mark_auditing(candidate.id)
    repository.finish_audit(candidate.id, audit={}, commit_plan={})
    repository.approve_for_commit(candidate.id, continue_after_commit=True)
    repository.commit_formal(candidate.id)
    repository.mark_sync_succeeded(candidate.id)


def _attach_rebuild_archive(
    db: DatabaseConnection,
    novel_id: str,
    *,
    start_chapter: int,
    end_chapter: int,
    retained_through: int,
    target_chapters: int,
) -> None:
    epoch = int(
        db.fetch_one(
            "SELECT generation_epoch FROM novel_generation_runs WHERE novel_id = ?",
            (novel_id,),
        )["generation_epoch"]
    )
    archive_id = f"test-archive-{epoch}"
    db.execute(
        "INSERT INTO worldline_archives "
        "(id, novel_id, old_generation_epoch, start_chapter, end_chapter, "
        "retained_through, target_chapters, status, prefix_digest, metadata_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, 'archived', '', '{}')",
        (
            archive_id,
            novel_id,
            max(0, epoch - 1),
            start_chapter,
            end_chapter,
            retained_through,
            target_chapters,
        ),
    )
    db.execute(
        "UPDATE worldline_rebuild_jobs SET archive_id = ? "
        "WHERE novel_id = ? AND generation_epoch = ?",
        (archive_id, novel_id, epoch),
    )
    db.commit()


@pytest.mark.asyncio
async def test_rewrite_retires_downstream_candidate_until_canonical_rebuild(tmp_path):
    db = DatabaseConnection(str(tmp_path / "rewrite-candidate-barrier.db"))
    db.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("novel-1", "Rewrite Candidate", "rewrite-candidate", 3),
    )
    db.get_connection().commit()
    candidates = ChapterCandidateRepository(db)
    candidates.start_run("novel-1", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=3)
    _commit_first_chapter(candidates)

    downstream = candidates.create_streaming_candidate(
        novel_id="novel-1",
        chapter_number=2,
        title="第二章",
        outline_chain=_outline_chain(),
        llm_content="基于旧第一章的候选正文",
    )
    trace = candidates.start_dag_run(downstream.id, content_revision=1)
    candidates.mark_auditing(downstream.id)
    candidates.finish_audit(downstream.id, audit={}, commit_plan={})

    chapters = SqliteChapterRepository(db)
    ChapterRewriteCoordinator(db=db, chapter_repository=chapters).rewrite(
        chapters.get_by_novel_and_number(NovelId("novel-1"), 1),
        "第一章重写后的正式正文",
    )

    assert candidates.get_candidate(downstream.id).status == CandidateStatus.STALE
    assert db.fetch_one(
        "SELECT status FROM candidate_dag_runs WHERE id = ?", (trace["id"],)
    )["status"] == "cancelled"
    run = candidates.get_run("novel-1")
    assert (run.state, run.generation_epoch, run.current_candidate_id) == (
        GenerationRunState.PAUSED,
        1,
        None,
    )
    assert (run.canonical_sync_status, run.next_action) == (
        "rebuilding",
        "rebuild_worldline",
    )
    assert db.fetch_one(
        "SELECT active_generation_epoch FROM worldline_generation_filters WHERE novel_id = ?",
        ("novel-1",),
    )["active_generation_epoch"] == 1
    assert db.fetch_one(
        "SELECT COUNT(*) AS total FROM worldline_rebuild_jobs "
        "WHERE novel_id = ? AND generation_epoch = ? AND status = 'pending'",
        ("novel-1", 1),
    )["total"] == 5
    with pytest.raises(CandidateGateError, match="retired generation epoch"):
        candidates.approve_for_commit(downstream.id, continue_after_commit=True)

    _attach_rebuild_archive(
        db,
        "novel-1",
        start_chapter=1,
        end_chapter=2,
        retained_through=0,
        target_chapters=3,
    )
    await WorldlineRebuildService(db, _SuccessfulAftermath(db)).rebuild("novel-1")

    resumed = candidates.start_run(
        "novel-1", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=3
    )
    assert resumed.current_formal_chapter == 1
    assert candidates.create_streaming_candidate(
        novel_id="novel-1",
        chapter_number=2,
        title="重写后的第二章",
        outline_chain=_outline_chain(),
    ).chapter_number == 2


def test_rewrite_refuses_to_retire_a_downstream_formal_sync(tmp_path):
    db = DatabaseConnection(str(tmp_path / "rewrite-pending-sync.db"))
    db.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("novel-1", "Rewrite Candidate", "rewrite-pending-sync", 3),
    )
    db.get_connection().commit()
    candidates = ChapterCandidateRepository(db)
    candidates.start_run("novel-1", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=3)
    _commit_first_chapter(candidates)

    downstream = candidates.create_streaming_candidate(
        novel_id="novel-1",
        chapter_number=2,
        title="第二章",
        outline_chain=_outline_chain(),
        llm_content="等待同步的第二章",
    )
    candidates.mark_auditing(downstream.id)
    candidates.finish_audit(downstream.id, audit={}, commit_plan={})
    candidates.approve_for_commit(downstream.id, continue_after_commit=True)
    candidates.commit_formal(downstream.id)

    chapters = SqliteChapterRepository(db)
    with pytest.raises(
        ChapterRewriteConflictError, match="pending canonical sync"
    ):
        ChapterRewriteCoordinator(db=db, chapter_repository=chapters).rewrite(
            chapters.get_by_novel_and_number(NovelId("novel-1"), 1),
            "不应写入的第一章正文",
        )

    assert chapters.get_by_novel_and_number(
        NovelId("novel-1"), 1
    ).content == "第一章原正文"
    assert candidates.get_candidate(downstream.id).status == CandidateStatus.SYNCING


@pytest.mark.asyncio
async def test_rewrite_rebuilds_a_ready_downstream_formal_tail_before_resuming(tmp_path):
    db = DatabaseConnection(str(tmp_path / "rewrite-ready-tail.db"))
    db.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("novel-1", "Rewrite Candidate", "rewrite-ready-tail", 3),
    )
    db.get_connection().commit()
    candidates = ChapterCandidateRepository(db)
    candidates.start_run("novel-1", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=3)
    _commit_first_chapter(candidates)
    downstream = candidates.create_streaming_candidate(
        novel_id="novel-1",
        chapter_number=2,
        title="第二章",
        outline_chain=_outline_chain(),
        llm_content="第二章正式正文",
    )
    candidates.mark_auditing(downstream.id)
    candidates.finish_audit(downstream.id, audit={}, commit_plan={})
    candidates.approve_for_commit(downstream.id, continue_after_commit=True)
    candidates.commit_formal(downstream.id)
    candidates.mark_sync_succeeded(downstream.id)

    chapters = SqliteChapterRepository(db)
    ChapterRewriteCoordinator(db=db, chapter_repository=chapters).rewrite(
        chapters.get_by_novel_and_number(NovelId("novel-1"), 1),
        "第一章重写后的正式正文",
    )

    assert candidates.get_candidate(downstream.id).status == CandidateStatus.COMMITTED
    assert candidates.get_run("novel-1").canonical_sync_status == "rebuilding"

    _attach_rebuild_archive(
        db,
        "novel-1",
        start_chapter=1,
        end_chapter=2,
        retained_through=0,
        target_chapters=3,
    )
    await WorldlineRebuildService(db, _SuccessfulAftermath(db)).rebuild("novel-1")

    resumed = candidates.start_run(
        "novel-1", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=3
    )
    assert resumed.current_formal_chapter == 2
    assert candidates.create_streaming_candidate(
        novel_id="novel-1",
        chapter_number=3,
        title="第三章",
        outline_chain=_outline_chain(),
    ).chapter_number == 3


@pytest.mark.asyncio
async def test_worldline_rebuild_replays_only_candidate_first_formal_chapters(tmp_path):
    db = DatabaseConnection(str(tmp_path / "rebuild-formal-only.db"))
    db.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("novel-1", "Rewrite Candidate", "rebuild-formal-only", 3),
    )
    db.get_connection().commit()
    candidates = ChapterCandidateRepository(db)
    candidates.start_run("novel-1", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=3)
    _commit_first_chapter(candidates)
    second = candidates.create_streaming_candidate(
        novel_id="novel-1",
        chapter_number=2,
        title="第二章",
        outline_chain=_outline_chain(),
        llm_content="第二章正式正文",
    )
    candidates.mark_auditing(second.id)
    candidates.finish_audit(second.id, audit={}, commit_plan={})
    candidates.approve_for_commit(second.id, continue_after_commit=True)
    candidates.commit_formal(second.id)
    candidates.mark_sync_succeeded(second.id)
    db.execute(
        "INSERT INTO chapters (id, novel_id, number, title, content, status) "
        "VALUES ('planned-chapter-3', 'novel-1', 3, '第三章计划', '', 'draft')"
    )
    db.get_connection().commit()

    ChapterRewriteCoordinator(
        db=db, chapter_repository=SqliteChapterRepository(db)
    ).rewrite(
        SqliteChapterRepository(db).get_by_novel_and_number(NovelId("novel-1"), 1),
        "第一章重写后的正式正文",
    )
    _attach_rebuild_archive(
        db,
        "novel-1",
        start_chapter=1,
        end_chapter=2,
        retained_through=0,
        target_chapters=3,
    )
    aftermath = _RecordingAftermath(db)

    await WorldlineRebuildService(db, aftermath).rebuild("novel-1")

    assert aftermath.chapter_numbers == [1, 2]
