import hashlib
from types import SimpleNamespace

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
