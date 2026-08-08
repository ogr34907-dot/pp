import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from application.engine.services import canonical_aftermath_recovery
from application.world.services.chapter_narrative_sync import (
    CHAPTER_NARRATIVE_PIPELINE_VERSION,
)
from domain.novel.entities.novel import AutopilotStatus, NovelStage
from engine.runtime.novel_lifecycle import process_novel
from infrastructure.persistence.database.connection import DatabaseConnection


def _seed_terminal_failure(
    database: DatabaseConnection,
    *,
    failure_reason: str,
) -> tuple[str, int]:
    content = "latest completed prose"
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    content_revision = 2
    database.execute(
        "INSERT INTO novels "
        "(id, title, slug, autopilot_status, current_stage, autopilot_recovery_reason) "
        "VALUES ('novel-1', 'Demo', 'demo', 'running', 'paused_for_review', '')"
    )
    database.execute(
        "INSERT INTO chapters "
        "(id, novel_id, number, content, content_sha256, content_revision, status) "
        "VALUES ('chapter-63', 'novel-1', 63, ?, ?, ?, 'completed')",
        (content, content_sha256, content_revision),
    )
    database.execute(
        "INSERT INTO chapter_narrative_commits "
        "(novel_id, chapter_number, content_sha256, pipeline_version, content_revision, "
        "status, failure_reason, attempt_count) "
        "VALUES ('novel-1', 63, ?, ?, ?, 'failed', ?, 3)",
        (
            content_sha256,
            CHAPTER_NARRATIVE_PIPELINE_VERSION,
            content_revision,
            failure_reason,
        ),
    )
    database.commit()
    return content_sha256, content_revision


class _SuccessfulPipeline:
    def __init__(
        self,
        database: DatabaseConnection,
        content_sha256: str,
        content_revision: int,
    ) -> None:
        self.database = database
        self.content_sha256 = content_sha256
        self.content_revision = content_revision
        self.calls = 0

    async def run_after_chapter_saved(self, *args, **kwargs):
        self.calls += 1
        claim = self.database.fetch_one(
            "SELECT status FROM chapter_narrative_commits "
            "WHERE novel_id = 'novel-1' AND chapter_number = 63"
        )
        assert claim["status"] == "stale"
        self.database.execute(
            "INSERT INTO knowledge (id, novel_id) VALUES ('knowledge-1', 'novel-1')"
        )
        self.database.execute(
            "INSERT INTO chapter_summaries "
            "(id, knowledge_id, chapter_number, summary, source_content_sha256, "
            "source_content_revision, pipeline_version, sync_status, sync_attempts) "
            "VALUES ('summary-63', 'knowledge-1', 63, 'summary', ?, ?, ?, "
            "'committed', 1)",
            (
                self.content_sha256,
                self.content_revision,
                CHAPTER_NARRATIVE_PIPELINE_VERSION,
            ),
        )
        self.database.execute(
            "UPDATE chapter_narrative_commits "
            "SET status = 'committed', failure_reason = '', memory_status = 'committed' "
            "WHERE novel_id = 'novel-1' AND chapter_number = 63"
        )
        self.database.commit()
        return {"narrative_sync_ok": True, "commit_status": "committed"}


class _NoopPipeline:
    def __init__(self) -> None:
        self.calls = 0

    async def run_after_chapter_saved(self, *args, **kwargs):
        self.calls += 1
        return {
            "narrative_sync_ok": False,
            "failure_reason": "API returned empty content",
        }


@pytest.mark.asyncio
async def test_transient_terminal_failure_runs_one_recovery_cycle(tmp_path):
    database = DatabaseConnection(str(tmp_path / "transient.db"))
    content_sha256, content_revision = _seed_terminal_failure(
        database,
        failure_reason="API returned empty content",
    )
    pipeline = _SuccessfulPipeline(database, content_sha256, content_revision)

    result = await canonical_aftermath_recovery.attempt_automatic_canonical_aftermath_recovery(
        novel_id="novel-1",
        database=database,
        aftermath_pipeline=pipeline,
    )

    assert result.disposition == "recovered"
    assert result.chapter_number == 63
    assert result.content_revision == 2
    assert pipeline.calls == 1
    marker = database.fetch_one(
        "SELECT autopilot_recovery_reason FROM novels WHERE id = 'novel-1'"
    )["autopilot_recovery_reason"]
    assert marker == result.recovery_marker
    assert content_sha256 in marker


@pytest.mark.asyncio
async def test_non_retryable_terminal_failure_does_not_run_recovery(tmp_path):
    database = DatabaseConnection(str(tmp_path / "hard.db"))
    _seed_terminal_failure(
        database,
        failure_reason="canonical_summary_write_missing",
    )
    pipeline = _NoopPipeline()

    result = await canonical_aftermath_recovery.attempt_automatic_canonical_aftermath_recovery(
        novel_id="novel-1",
        database=database,
        aftermath_pipeline=pipeline,
    )

    assert result.disposition == "not_retryable"
    assert pipeline.calls == 0
    marker = database.fetch_one(
        "SELECT autopilot_recovery_reason FROM novels WHERE id = 'novel-1'"
    )["autopilot_recovery_reason"]
    assert marker == ""


@pytest.mark.asyncio
async def test_failed_recovery_marker_blocks_same_version_on_later_tick(tmp_path):
    database = DatabaseConnection(str(tmp_path / "exhausted.db"))
    _seed_terminal_failure(
        database,
        failure_reason="API returned empty content",
    )
    pipeline = _NoopPipeline()

    first = await canonical_aftermath_recovery.attempt_automatic_canonical_aftermath_recovery(
        novel_id="novel-1",
        database=database,
        aftermath_pipeline=pipeline,
    )
    second = await canonical_aftermath_recovery.attempt_automatic_canonical_aftermath_recovery(
        novel_id="novel-1",
        database=database,
        aftermath_pipeline=pipeline,
    )

    assert first.disposition == "failed"
    assert second.disposition == "exhausted"
    assert second.recovery_marker == first.recovery_marker
    assert pipeline.calls == 1


@pytest.mark.asyncio
async def test_post_recovery_read_error_returns_failed_instead_of_escaping(
    tmp_path,
    monkeypatch,
):
    database = DatabaseConnection(str(tmp_path / "post-read-error.db"))
    _seed_terminal_failure(
        database,
        failure_reason="API returned empty content",
    )
    pipeline = _NoopPipeline()
    real_claims = canonical_aftermath_recovery.SqliteChapterNarrativeCommitRepository(
        database
    )

    class _Claims:
        def __init__(self, _database):
            self.ready_calls = 0

        def is_current_version_ready(self, **kwargs):
            self.ready_calls += 1
            if self.ready_calls == 1:
                return False
            raise RuntimeError("database temporarily unavailable")

        def claim_bounded_automatic_recovery(self, **kwargs):
            return real_claims.claim_bounded_automatic_recovery(**kwargs)

        def restore_terminal_failure(self, **kwargs):
            return real_claims.restore_terminal_failure(**kwargs)

    monkeypatch.setattr(
        canonical_aftermath_recovery,
        "SqliteChapterNarrativeCommitRepository",
        _Claims,
    )

    result = await canonical_aftermath_recovery.attempt_automatic_canonical_aftermath_recovery(
        novel_id="novel-1",
        database=database,
        aftermath_pipeline=pipeline,
    )

    assert result.disposition == "failed"
    assert "database temporarily unavailable" in result.failure_reason
    assert pipeline.calls == 1


@pytest.mark.asyncio
async def test_lifecycle_routes_recovered_canonical_version_through_writing_advance():
    host = MagicMock()
    host._is_still_running.return_value = True
    host._latest_completed_chapter_number.return_value = 63
    host._is_chapter_narrative_ready.return_value = False
    host.circuit_breaker = None
    novel = MagicMock()
    novel.novel_id.value = "novel-1"
    novel.current_stage = NovelStage.PAUSED_FOR_REVIEW
    novel.autopilot_status = AutopilotStatus.RUNNING
    novel.auto_approve_mode = True
    recovered = SimpleNamespace(
        disposition="recovered",
        chapter_number=63,
        content_revision=2,
        recovery_marker="marker",
        failure_reason="",
    )

    with patch(
        "engine.runtime.novel_lifecycle.attempt_automatic_canonical_aftermath_recovery",
        new=AsyncMock(return_value=recovered),
        create=True,
    ) as attempt:
        await process_novel(host, novel)

    attempt.assert_awaited_once()
    assert novel.current_stage == NovelStage.WRITING
    host._update_shared_state.assert_any_call(
        "novel-1",
        current_stage="writing",
        autopilot_pause_reason="",
        autopilot_recovery_reason="",
        canonical_aftermath_chapter_number=None,
        canonical_aftermath_failure_reason="",
    )
