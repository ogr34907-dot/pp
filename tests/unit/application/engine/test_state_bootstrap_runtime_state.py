import hashlib

from application.engine.services.shared_state_repository import SharedStateRepository
from application.engine.services.state_bootstrap import StateBootstrap
from infrastructure.persistence.database.connection import DatabaseConnection


def test_state_bootstrap_preserves_autopilot_recovery_reason(monkeypatch):
    """AUTOPILOT-002: a restart keeps a manual pause visible and resumable."""
    shared = SharedStateRepository(shared_dict={})
    bootstrap = StateBootstrap(shared_state=shared)
    monkeypatch.setattr(bootstrap, "_macro_structure_ready", lambda _novel_id: False)

    bootstrap._load_novel_state(
        {
            "id": "novel-1",
            "title": "Demo",
            "autopilot_status": "stopped",
            "autopilot_recovery_reason": "manual_pause",
            "current_stage": "writing",
            "current_act": 1,
            "current_chapter_in_act": 2,
            "current_beat_index": 0,
            "current_auto_chapters": 0,
            "target_chapters": 20,
            "target_words_per_chapter": 2500,
            "consecutive_error_count": 0,
            "last_chapter_tension": 0,
            "auto_approve_mode": False,
            "needs_review": False,
        }
    )

    raw = shared.get_raw_state("novel-1")

    assert raw["autopilot_recovery_reason"] == "manual_pause"


def test_state_bootstrap_preserves_configured_protection_limit(monkeypatch):
    """STATUS-003: restart must not replace a configured safety cap with 9999."""
    shared = SharedStateRepository(shared_dict={})
    bootstrap = StateBootstrap(shared_state=shared)
    monkeypatch.setattr(bootstrap, "_macro_structure_ready", lambda _novel_id: False)

    bootstrap._load_novel_state(
        {
            "id": "novel-1",
            "title": "Demo",
            "autopilot_status": "stopped",
            "autopilot_recovery_reason": "manual_pause",
            "current_stage": "writing",
            "current_act": 1,
            "current_chapter_in_act": 2,
            "current_beat_index": 0,
            "current_auto_chapters": 0,
            "max_auto_chapters": 1,
            "target_chapters": 500,
            "target_words_per_chapter": 2000,
            "consecutive_error_count": 0,
            "last_chapter_tension": 0,
            "auto_approve_mode": False,
            "needs_review": False,
        }
    )

    raw = shared.get_raw_state("novel-1")

    assert raw["max_auto_chapters"] == 1


def test_state_bootstrap_does_not_relabel_canonical_pause_as_macro_review(monkeypatch):
    shared = SharedStateRepository(shared_dict={})
    bootstrap = StateBootstrap(shared_state=shared)
    monkeypatch.setattr(bootstrap, "_macro_structure_ready", lambda _novel_id: True)

    bootstrap._load_novel_state(
        {
            "id": "novel-1",
            "title": "Demo",
            "autopilot_status": "running",
            "current_stage": "paused_for_review",
            "current_act": 1,
            "current_chapter_in_act": 7,
            "current_beat_index": 0,
            "current_auto_chapters": 7,
            "max_auto_chapters": 20,
            "target_chapters": 20,
            "target_words_per_chapter": 2500,
            "consecutive_error_count": 0,
            "last_chapter_tension": 0,
            "auto_approve_mode": False,
            "needs_review": True,
        }
    )

    raw = shared.get_raw_state("novel-1")

    assert raw.get("writing_substep", "") != "macro_planning"


def test_state_bootstrap_ignores_terminal_failure_before_latest_completed_chapter(
    tmp_path, monkeypatch
):
    db = DatabaseConnection(str(tmp_path / "latest-chapter.db"))
    old_content = "旧章"
    latest_content = "最新章"
    old_sha = hashlib.sha256(old_content.encode()).hexdigest()
    latest_sha = hashlib.sha256(latest_content.encode()).hexdigest()
    db.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) "
        "VALUES ('novel-1', 'Demo', 'demo', 20)"
    )
    db.execute(
        "INSERT INTO chapters (id, novel_id, number, content, content_sha256, content_revision, status) "
        "VALUES ('chapter-7', 'novel-1', 7, ?, ?, 1, 'completed')",
        (old_content, old_sha),
    )
    db.execute(
        "INSERT INTO chapters (id, novel_id, number, content, content_sha256, content_revision, status) "
        "VALUES ('chapter-8', 'novel-1', 8, ?, ?, 1, 'completed')",
        (latest_content, latest_sha),
    )
    db.execute(
        "INSERT INTO chapter_narrative_commits "
        "(novel_id, chapter_number, content_sha256, pipeline_version, content_revision, status, attempt_count, failure_reason) "
        "VALUES ('novel-1', 7, ?, 'chapter-narrative-sync:v1', 1, 'failed', 3, 'old failure')",
        (old_sha,),
    )
    db.commit()
    monkeypatch.setattr(
        "infrastructure.persistence.database.connection.get_database", lambda *_args, **_kwargs: db
    )

    failure = StateBootstrap(shared_state=SharedStateRepository(shared_dict={}))._canonical_aftermath_failure(
        "novel-1"
    )

    assert failure is None
