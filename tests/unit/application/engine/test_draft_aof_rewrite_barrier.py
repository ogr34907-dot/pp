from application.core.services.chapter_rewrite_coordinator import ChapterRewriteResult
from application.engine.services import draft_aof
from domain.novel.entities.chapter import Chapter, ChapterStatus
from domain.novel.value_objects.novel_id import NovelId
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_chapter_repository import (
    SqliteChapterRepository,
)


class _RewriteCoordinator:
    def __init__(self):
        self.calls = []

    def rewrite(self, chapter, content, *, rewrite_mode):
        self.calls.append((chapter, content, rewrite_mode))
        return ChapterRewriteResult(
            chapter=chapter,
            rewrite_mode=rewrite_mode,
            requires_rebuild=True,
            replay_completed=False,
        )


def test_aof_recovery_routes_existing_prose_through_rewrite_coordinator(
    monkeypatch, tmp_path
):
    db = DatabaseConnection(str(tmp_path / "aof-rewrite.db"))
    db.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'Novel', 'novel-1')"
    )
    repository = SqliteChapterRepository(db)
    existing = Chapter(
        id="chapter-1",
        novel_id=NovelId("novel-1"),
        number=1,
        title="第1章",
        content="旧正文",
        status=ChapterStatus.COMPLETED,
    )
    repository.save(existing)
    coordinator = _RewriteCoordinator()

    monkeypatch.setattr(
        "infrastructure.persistence.database.connection.get_database",
        lambda *_args, **_kwargs: db,
    )
    monkeypatch.setattr(
        "application.core.services.chapter_rewrite_coordinator.ChapterRewriteCoordinator.for_chapter_repository",
        lambda *_args, **_kwargs: coordinator,
    )

    draft_aof._recover_draft_to_db("novel-1", 1, "更长的新正文")

    assert len(coordinator.calls) == 1
    rewritten, content, rewrite_mode = coordinator.calls[0]
    assert rewritten.id == existing.id
    assert content == "更长的新正文"
    assert rewrite_mode == "safe_snapshot"
