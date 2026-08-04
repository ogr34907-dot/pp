import hashlib

import pytest

from application.checkpoint.services.unified_checkpoint_service import (
    UnifiedCheckpointService,
)
from application.core.services.chapter_rewrite_coordinator import (
    ChapterReplayError,
    ChapterRewriteCoordinator,
)
from application.core.services.chapter_service import ChapterService
from domain.novel.entities.chapter import Chapter, ChapterStatus
from domain.novel.value_objects.novel_id import NovelId
from infrastructure.persistence.database.chapter_draft_repository import (
    ChapterDraftRepository,
)
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_chapter_repository import (
    SqliteChapterRepository,
)


class _UnusedNovelRepository:
    pass


class _RecordingVectorStore:
    def __init__(self):
        self.calls = []

    def invalidate_from_chapter(self, novel_id, chapter_number):
        self.calls.append((novel_id, chapter_number))


class _ReplayAftermath:
    def __init__(self, fail_chapter=None):
        self.calls = []
        self.fail_chapter = fail_chapter

    async def run_after_chapter_saved(
        self,
        novel_id,
        chapter_number,
        content,
        *,
        expected_content_sha256,
        expected_content_revision,
    ):
        self.calls.append(
            (
                novel_id,
                chapter_number,
                content,
                expected_content_sha256,
                expected_content_revision,
            )
        )
        if chapter_number == self.fail_chapter:
            return {
                "narrative_sync_ok": False,
                "failure_reason": "canonical_commit_failed",
            }
        return {"narrative_sync_ok": True}


def _seed_completed_chapter(repo, novel_id, number, content):
    chapter = Chapter(
        id=f"chapter-{number}",
        novel_id=NovelId(novel_id),
        number=number,
        title=f"第{number}章",
        content=content,
        status=ChapterStatus.COMPLETED,
    )
    repo.save(chapter)
    return chapter


def test_safe_snapshot_rewrite_invalidates_downstream_state_and_pauses_mainline(
    tmp_path,
):
    db = DatabaseConnection(str(tmp_path / "rewrite.db"))
    novel_id = "novel-rewrite"
    db.execute(
        "INSERT INTO novels (id, title, slug, autopilot_status, current_stage) "
        "VALUES (?, ?, ?, 'running', 'writing')",
        (novel_id, "Rewrite", novel_id),
    )
    repo = SqliteChapterRepository(db)
    _seed_completed_chapter(repo, novel_id, 1, "第一章旧正文")
    _seed_completed_chapter(repo, novel_id, 2, "第二章旧正文")
    _seed_completed_chapter(repo, novel_id, 3, "第三章旧正文")
    db.execute("INSERT INTO knowledge (id, novel_id) VALUES ('knowledge-1', ?)", (novel_id,))

    for number in (2, 3):
        content = f"第{number}章旧正文"
        content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        db.execute(
            "INSERT INTO chapter_summaries "
            "(id, knowledge_id, chapter_number, summary, source_content_sha256, "
            "pipeline_version, sync_status, sync_attempts) "
            "VALUES (?, 'knowledge-1', ?, '摘要', ?, 'chapter-narrative-sync:v1', 'committed', 1)",
            (f"summary-{number}", number, content_sha256),
        )
        db.execute(
            "INSERT INTO chapter_narrative_commits "
            "(novel_id, chapter_number, content_sha256, pipeline_version, content_revision, status) "
            "VALUES (?, ?, ?, 'chapter-narrative-sync:v1', 1, 'committed')",
            (novel_id, number, content_sha256),
        )
        db.execute(
            "INSERT INTO chapter_evolution_snapshots "
            "(snapshot_id, novel_id, branch_id, chapter_number, status) "
            "VALUES (?, ?, 'main', ?, 'active')",
            (f"evolution-{number}", novel_id, number),
        )

    vector_store = _RecordingVectorStore()
    checkpoints = UnifiedCheckpointService(db, repo)
    service = ChapterService(
        repo,
        _UnusedNovelRepository(),
        chapter_rewrite_coordinator=ChapterRewriteCoordinator(
            db=db,
            chapter_repository=repo,
            chapter_draft_repository=ChapterDraftRepository(db),
            checkpoint_service=checkpoints,
            vector_store=vector_store,
        ),
    )

    updated = service.update_chapter_by_novel_and_number(
        novel_id,
        2,
        "第二章重写正文",
    )

    assert updated.content == "第二章重写正文"
    rewritten = db.fetch_one(
        "SELECT content_sha256, content_revision FROM chapters "
        "WHERE novel_id = ? AND number = 2",
        (novel_id,),
    )
    assert rewritten["content_sha256"] == hashlib.sha256(
        "第二章重写正文".encode("utf-8")
    ).hexdigest()
    assert rewritten["content_revision"] == 2
    assert db.fetch_one(
        "SELECT content FROM chapter_drafts WHERE novel_id = ? AND chapter_number = 2",
        (novel_id,),
    )["content"] == "第二章旧正文"
    assert [row["sync_status"] for row in db.fetch_all(
        "SELECT sync_status FROM chapter_summaries WHERE chapter_number >= 2 ORDER BY chapter_number"
    )] == ["stale", "stale"]
    assert [row["status"] for row in db.fetch_all(
        "SELECT status FROM chapter_narrative_commits WHERE novel_id = ? ORDER BY chapter_number",
        (novel_id,),
    )] == ["stale", "stale"]
    assert [row["status"] for row in db.fetch_all(
        "SELECT status FROM chapter_evolution_snapshots WHERE novel_id = ? ORDER BY chapter_number",
        (novel_id,),
    )] == ["stale", "stale"]
    checkpoint = db.fetch_one(
        "SELECT anchor_chapter, is_active FROM novel_checkpoints "
        "WHERE novel_id = ? ORDER BY created_at DESC LIMIT 1",
        (novel_id,),
    )
    assert dict(checkpoint) == {"anchor_chapter": 1, "is_active": 1}
    novel = db.fetch_one(
        "SELECT autopilot_status, current_stage FROM novels WHERE id = ?",
        (novel_id,),
    )
    assert dict(novel) == {
        "autopilot_status": "stopped",
        "current_stage": "paused_for_review",
    }
    assert vector_store.calls == [(novel_id, 2)]


def test_retain_prose_replays_in_order_and_reindexes_current_versions(
    monkeypatch, tmp_path
):
    db = DatabaseConnection(str(tmp_path / "retain-prose.db"))
    novel_id = "novel-replay"
    db.execute(
        "INSERT INTO novels (id, title, slug, autopilot_status, current_stage) "
        "VALUES (?, ?, ?, 'running', 'writing')",
        (novel_id, "Replay", novel_id),
    )
    repo = SqliteChapterRepository(db)
    _seed_completed_chapter(repo, novel_id, 1, "第一章正文")
    _seed_completed_chapter(repo, novel_id, 2, "第二章旧正文")
    _seed_completed_chapter(repo, novel_id, 3, "第三章正文")
    aftermath = _ReplayAftermath()
    reindex_calls = []

    monkeypatch.setattr(
        "application.core.services.chapter_rewrite_coordinator.reindex_chapter_entity_mentions",
        lambda *args, **kwargs: reindex_calls.append((args, kwargs)),
        raising=False,
    )
    coordinator = ChapterRewriteCoordinator(
        db=db,
        chapter_repository=repo,
        aftermath_pipeline=aftermath,
    )

    result = coordinator.rewrite(
        repo.get_by_novel_and_number(NovelId(novel_id), 2),
        "第二章重写正文",
        rewrite_mode="retain_prose",
    )

    assert result.requires_rebuild is False
    assert result.replay_completed is True
    assert [(call[1], call[2], call[4]) for call in aftermath.calls] == [
        (2, "第二章重写正文", 2),
        (3, "第三章正文", 1),
    ]
    assert [args[:3] for args, _kwargs in reindex_calls] == [
        (novel_id, 2, "第二章重写正文"),
        (novel_id, 3, "第三章正文"),
    ]
    assert [kwargs["expected_content_revision"] for _args, kwargs in reindex_calls] == [
        2,
        1,
    ]
    novel = db.fetch_one(
        "SELECT autopilot_status, current_stage FROM novels WHERE id = ?", (novel_id,)
    )
    assert dict(novel) == {
        "autopilot_status": "stopped",
        "current_stage": "paused_for_review",
    }


def test_retain_prose_keeps_mainline_paused_when_a_replay_commit_fails(tmp_path):
    db = DatabaseConnection(str(tmp_path / "retain-failure.db"))
    novel_id = "novel-replay-failure"
    db.execute(
        "INSERT INTO novels (id, title, slug, autopilot_status, current_stage) "
        "VALUES (?, ?, ?, 'running', 'writing')",
        (novel_id, "Replay", novel_id),
    )
    repo = SqliteChapterRepository(db)
    _seed_completed_chapter(repo, novel_id, 1, "第一章正文")
    _seed_completed_chapter(repo, novel_id, 2, "第二章旧正文")
    _seed_completed_chapter(repo, novel_id, 3, "第三章正文")
    coordinator = ChapterRewriteCoordinator(
        db=db,
        chapter_repository=repo,
        aftermath_pipeline=_ReplayAftermath(fail_chapter=3),
    )

    with pytest.raises(ChapterReplayError, match="chapter_3_canonical_replay_failed"):
        coordinator.rewrite(
            repo.get_by_novel_and_number(NovelId(novel_id), 2),
            "第二章重写正文",
            rewrite_mode="retain_prose",
        )

    novel = db.fetch_one(
        "SELECT autopilot_status, current_stage FROM novels WHERE id = ?", (novel_id,)
    )
    assert dict(novel) == {
        "autopilot_status": "stopped",
        "current_stage": "paused_for_review",
    }


def test_safe_snapshot_rewrite_invalidates_derived_memory_state(tmp_path):
    db = DatabaseConnection(str(tmp_path / "rewrite-memory.db"))
    novel_id = "novel-memory"
    db.execute(
        "INSERT INTO novels (id, title, slug) VALUES (?, ?, ?)",
        (novel_id, "Memory", novel_id),
    )
    repo = SqliteChapterRepository(db)
    _seed_completed_chapter(repo, novel_id, 1, "第一章正文")
    _seed_completed_chapter(repo, novel_id, 2, "第二章旧正文")
    db.execute(
        "INSERT INTO memory_atoms (id, novel_id, entity_id, chapter_number) "
        "VALUES ('atom-early', ?, 'entity-stable', 1)",
        (novel_id,),
    )
    db.execute(
        "INSERT INTO memory_atoms (id, novel_id, entity_id, chapter_number) "
        "VALUES ('atom-stale', ?, 'entity-stale', 2)",
        (novel_id,),
    )
    db.execute(
        "INSERT INTO memory_atom_links (id, novel_id, source_atom_id, target_atom_id) "
        "VALUES ('link-stale', ?, 'atom-stale', 'atom-early')",
        (novel_id,),
    )
    db.execute(
        "INSERT INTO memory_projections (novel_id, entity_id) VALUES (?, 'entity-stable')",
        (novel_id,),
    )
    db.execute(
        "INSERT INTO memory_projections (novel_id, entity_id) VALUES (?, 'entity-stale')",
        (novel_id,),
    )
    db.execute(
        "INSERT INTO memory_engine_state (novel_id, state_json, last_updated_chapter) "
        "VALUES (?, '{}', 2)",
        (novel_id,),
    )

    ChapterRewriteCoordinator(db=db, chapter_repository=repo).rewrite(
        repo.get_by_novel_and_number(NovelId(novel_id), 2),
        "第二章重写正文",
    )

    assert [row["id"] for row in db.fetch_all(
        "SELECT id FROM memory_atoms ORDER BY id"
    )] == ["atom-early"]
    assert db.fetch_all("SELECT * FROM memory_atom_links") == []
    assert [row["entity_id"] for row in db.fetch_all(
        "SELECT entity_id FROM memory_projections ORDER BY entity_id"
    )] == ["entity-stable"]
    assert db.fetch_all("SELECT * FROM memory_engine_state") == []
