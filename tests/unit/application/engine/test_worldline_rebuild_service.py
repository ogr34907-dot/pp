"""A regenerated worldline cannot resume until its retained prefix is canonical again."""

import pytest

from application.engine.services.worldline_rebuild_service import WorldlineRebuildService
from application.engine.services.worldline_regeneration_service import WorldlineRegenerationService
from infrastructure.persistence.database.connection import DatabaseConnection


class _Aftermath:
    def __init__(self, ok: bool = True):
        self.ok = ok
        self.chapters: list[int] = []

    async def run_after_chapter_saved(self, _novel_id, chapter_number, _content, **_kwargs):
        self.chapters.append(chapter_number)
        return {"narrative_sync_ok": self.ok, "failure_reason": "fake-sync-failed" if not self.ok else ""}


def _seed(db):
    conn = db.get_connection()
    conn.execute("INSERT INTO novels (id, title, slug, target_chapters) VALUES ('novel-1', '重建小说', 'rebuild', 5)")
    for number in (1, 2):
        conn.execute(
            """
            INSERT INTO chapters (id, novel_id, number, title, content, content_sha256, content_revision, status)
            VALUES (?, 'novel-1', ?, ?, ?, ?, 1, 'completed')
            """,
            (f"chapter-{number}", number, f"第{number}章", f"正文{number}", f"hash-{number}"),
        )
    conn.commit()


@pytest.mark.asyncio
async def test_rebuild_replays_retained_prefix_before_mode_can_resume(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline-rebuild.db"))
    _seed(db)
    reset = WorldlineRegenerationService(db)
    preview = reset.preview("novel-1", start_chapter=2, target_chapters=5)
    reset.execute("novel-1", preview_token=preview.token, run_mode="continuous")
    aftermath = _Aftermath()

    result = await WorldlineRebuildService(db, aftermath).rebuild("novel-1")

    assert result["status"] == "completed"
    assert aftermath.chapters == [1]
    run = db.fetch_one(
        "SELECT state, canonical_sync_status, next_action FROM novel_generation_runs WHERE novel_id = 'novel-1'"
    )
    assert (run["state"], run["canonical_sync_status"], run["next_action"]) == (
        "paused",
        "ready",
        "select_run_mode",
    )
    assert db.fetch_one(
        "SELECT COUNT(*) AS total FROM worldline_rebuild_jobs WHERE novel_id = 'novel-1' AND status = 'completed'"
    )["total"] == 5
