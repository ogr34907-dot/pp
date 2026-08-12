"""Chapter update API error contracts."""

from fastapi.testclient import TestClient

from application.core.services.chapter_rewrite_coordinator import (
    ChapterRewriteConflictError,
)
from interfaces.api.v1.core import chapters
from interfaces.main import app


class _ConflictingChapterService:
    def update_chapter_by_novel_and_number(self, *_args, **_kwargs):
        raise ChapterRewriteConflictError("pending canonical sync")


def test_chapter_update_returns_conflict_for_a_pending_canonical_sync():
    app.dependency_overrides[chapters.get_chapter_service] = (
        lambda: _ConflictingChapterService()
    )
    app.dependency_overrides[chapters.get_chapter_aftermath_pipeline] = lambda: object()
    app.dependency_overrides[chapters.get_knowledge_service] = lambda: object()
    try:
        response = TestClient(app, raise_server_exceptions=False).put(
            "/api/v1/novels/novel-1/chapters/1",
            json={"content": "rewritten prose"},
        )
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 409
    assert response.json()["detail"] == "pending canonical sync"
