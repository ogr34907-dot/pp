import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from domain.novel.entities.novel import AutopilotStatus, NovelStage
from interfaces.api.v1.engine import autopilot_routes
from infrastructure.persistence.database.connection import DatabaseConnection


class _Repo:
    def __init__(self):
        self.patches = []

    def patch(self, novel_id, **fields):
        self.patches.append((novel_id.value, fields))


def test_resume_persist_keeps_explicit_next_stage(monkeypatch):
    repo = _Repo()

    monkeypatch.setattr(
        autopilot_routes,
        "_persist_autopilot_running_sync",
        lambda *args, **kwargs: {"decision": SimpleNamespace(next_stage="paused_for_review"), "run_epoch": 7},
    )
    monkeypatch.setattr(autopilot_routes, "get_novel_repository", lambda: repo)

    result = autopilot_routes._persist_autopilot_resume_sync(
        "novel-1",
        next_stage=NovelStage.ACT_PLANNING.value,
        current_act=0,
        max_auto_chapters=9999,
        target_chapters=150,
        target_words_per_chapter=2000,
    )

    assert result["run_epoch"] == 7
    assert len(repo.patches) == 1
    novel_id, fields = repo.patches[0]
    assert novel_id == "novel-1"
    assert fields["autopilot_status"] == AutopilotStatus.RUNNING
    assert fields["current_stage"] == NovelStage.ACT_PLANNING
    assert fields["current_act"] == 0
    assert fields["last_stable_stage"] == "act_planning"


def test_manual_resume_clears_a_persisted_restart_interruption_reason(monkeypatch):
    """AUTOPILOT-001: an explicit user resume clears stale restart status."""
    repo = _Repo()

    monkeypatch.setattr(
        autopilot_routes,
        "_persist_autopilot_running_sync",
        lambda *args, **kwargs: {"decision": SimpleNamespace(next_stage="act_planning"), "run_epoch": 8},
    )
    monkeypatch.setattr(autopilot_routes, "get_novel_repository", lambda: repo)

    autopilot_routes._persist_autopilot_resume_sync(
        "novel-1",
        next_stage=NovelStage.ACT_PLANNING.value,
        current_act=1,
        max_auto_chapters=9999,
        target_chapters=150,
        target_words_per_chapter=2000,
    )

    _novel_id, fields = repo.patches[0]
    assert fields["autopilot_recovery_reason"] == ""


def test_manual_resume_blocks_terminal_canonical_failure(tmp_path, monkeypatch):
    db = DatabaseConnection(str(tmp_path / "resume.db"))
    content = "已完成正文"
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    db.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'Novel', 'novel-1')"
    )
    db.execute(
        "INSERT INTO chapters "
        "(id, novel_id, number, content, content_sha256, content_revision, status) "
        "VALUES ('chapter-1', 'novel-1', 1, ?, ?, 1, 'completed')",
        (content, content_sha256),
    )
    db.execute(
        "INSERT INTO chapter_narrative_commits "
        "(novel_id, chapter_number, content_sha256, pipeline_version, "
        "content_revision, status, failure_reason, attempt_count) "
        "VALUES ('novel-1', 1, ?, 'chapter-narrative-sync:v1', "
        "1, 'failed', 'provider unavailable', 3)",
        (content_sha256,),
    )
    db.commit()
    assert (
        autopilot_routes._canonical_resume_block_reason("novel-1", db=db)
        == "canonical_aftermath_not_ready"
    )


@pytest.mark.asyncio
async def test_canonical_retry_route_preserves_resume_guard(monkeypatch):
    """Retry completion does not auto-resume; normal resume remains explicit."""
    resume_calls = []
    database = object()
    pipeline = object()
    recovery = SimpleNamespace(
        disposition="recovered",
        chapter_number=8,
        content_revision=2,
        failure_reason="",
    )
    attempt = AsyncMock(return_value=recovery)
    monkeypatch.setattr(autopilot_routes, "get_database", lambda *_args, **_kwargs: database)
    monkeypatch.setattr(autopilot_routes, "get_chapter_aftermath_pipeline", lambda: pipeline)
    monkeypatch.setattr(
        autopilot_routes,
        "attempt_manual_canonical_aftermath_recovery",
        attempt,
        raising=False,
    )
    monkeypatch.setattr(
        autopilot_routes,
        "resume_from_review",
        lambda *_args, **_kwargs: resume_calls.append(True),
    )

    result = await autopilot_routes.retry_canonical_aftermath("novel-1")

    assert result["success"] is True
    assert result["remains_paused"] is True
    attempt.assert_awaited_once_with(
        novel_id="novel-1",
        database=database,
        aftermath_pipeline=pipeline,
    )
    assert resume_calls == []


@pytest.mark.asyncio
async def test_canonical_retry_route_surfaces_shared_service_failure(monkeypatch):
    recovery = SimpleNamespace(
        disposition="failed",
        chapter_number=8,
        content_revision=2,
        failure_reason="canonical_aftermath_manual_recovery_failed:provider disconnected",
    )
    attempt = AsyncMock(return_value=recovery)
    monkeypatch.setattr(autopilot_routes, "get_database", lambda *_args, **_kwargs: object())
    monkeypatch.setattr(autopilot_routes, "get_chapter_aftermath_pipeline", lambda: object())
    monkeypatch.setattr(
        autopilot_routes,
        "attempt_manual_canonical_aftermath_recovery",
        attempt,
        raising=False,
    )

    with pytest.raises(Exception) as error:
        await autopilot_routes.retry_canonical_aftermath("novel-1")

    assert getattr(error.value, "status_code", None) == 502
    assert "provider disconnected" in str(getattr(error.value, "detail", ""))
