"""Runtime-state persistence contracts for SqliteNovelRepository."""

from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_novel_repository import (
    SqliteNovelRepository,
)
from infrastructure.persistence.database.write_dispatch import (
    startup_sqlite_writes_bypass_queue,
)
from domain.novel.entities.novel import Novel
from domain.novel.value_objects.novel_id import NovelId


def test_save_round_trips_runtime_recovery_fields(tmp_path):
    """DB-002/AUTOPILOT-001: save must not erase durable recovery state."""
    database = DatabaseConnection(str(tmp_path / "runtime-state.db"))
    repository = SqliteNovelRepository(database)
    novel = Novel(
        id=NovelId("novel-1"),
        title="Runtime novel",
        author="Author",
        target_chapters=30,
        autopilot_run_epoch=41,
        active_pipeline_step="compose",
        active_pipeline_run_id="run-41",
        last_stable_stage="writing",
    )
    novel.autopilot_recovery_reason = "service_restart_interrupted"

    with startup_sqlite_writes_bypass_queue():
        repository.save(novel)

    reloaded = repository.get_by_id(NovelId("novel-1"))

    assert reloaded.autopilot_run_epoch == 41
    assert reloaded.active_pipeline_step == "compose"
    assert reloaded.active_pipeline_run_id == "run-41"
    assert reloaded.last_stable_stage == "writing"
    assert getattr(reloaded, "autopilot_recovery_reason", "") == "service_restart_interrupted"
