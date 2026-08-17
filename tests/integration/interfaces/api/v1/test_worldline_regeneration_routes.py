"""Public preview/execute/restore surface for generic worldline regeneration."""

import hashlib

from infrastructure.persistence.database.chapter_candidate_repository import (
    ChapterCandidateRepository,
)
from tests.integration.application.engine.test_worldline_regeneration_service import (
    _materialize_manifest_worldline,
    _seed,
)


def _seed_chapters(db, novel_id: str) -> None:
    for number in (1, 2):
        db.execute(
            """
            INSERT INTO chapters
                (id, novel_id, number, title, content, content_sha256, content_revision, status)
            VALUES (?, ?, ?, ?, ?, ?, 1, 'completed')
            """,
            (
                f"chapter-{number}",
                novel_id,
                number,
                f"第{number}章",
                f"正文{number}",
                hashlib.sha256(f"正文{number}".encode("utf-8")).hexdigest(),
            ),
        )
    db.get_connection().commit()
    ChapterCandidateRepository(db).import_legacy_formal_history(novel_id)


def test_worldline_preview_execute_and_restore_are_explicit_and_idempotent(client, db, test_novel_id):
    _seed_chapters(db, test_novel_id)
    preview = client.post(
        f"/api/v1/worldline-regeneration/novels/{test_novel_id}/preview",
        json={"start_chapter": 2, "target_chapters": 6},
    )
    assert preview.status_code == 200
    data = preview.json()["data"]
    assert data["operation"] == "regenerate"
    assert data["retained_through"] == 1

    executed = client.post(
        f"/api/v1/worldline-regeneration/novels/{test_novel_id}/execute",
        json={
            "preview_token": data["token"],
            "run_mode": "chapter_review",
            "idempotency_key": "regenerate-v1",
        },
    )
    assert executed.status_code == 200
    archive_id = executed.json()["data"]["archive_id"]
    assert client.get(f"/api/v1/worldline-regeneration/novels/{test_novel_id}/archives").json()["data"][0]["id"] == archive_id
    rebuild_status = client.get(
        f"/api/v1/worldline-regeneration/novels/{test_novel_id}/rebuild-status"
    )
    assert rebuild_status.status_code == 200
    assert rebuild_status.json()["data"]["run"]["next_action"] == "rebuild_worldline"
    assert len(rebuild_status.json()["data"]["jobs"]) == 5

    restored = client.post(
        f"/api/v1/worldline-regeneration/novels/{test_novel_id}/archives/{archive_id}/restore",
        json={"run_mode": "continuous", "idempotency_key": "restore-v1"},
    )
    assert restored.status_code == 200
    assert restored.json()["data"]["operation"] == "restore"
    assert db.get_connection().execute(
        "SELECT COUNT(*) FROM chapters WHERE novel_id = ?", (test_novel_id,)
    ).fetchone()[0] == 2


def test_manifest_worldline_routes_rebase_and_restore_the_bound_head(client, db):
    _seed(db)
    original = _materialize_manifest_worldline(db)

    preview = client.post(
        "/api/v1/worldline-regeneration/novels/novel-1/preview",
        json={"start_chapter": 2, "target_chapters": 6},
    )
    assert preview.status_code == 200
    assert preview.json()["data"]["operation"] == "regenerate"

    executed = client.post(
        "/api/v1/worldline-regeneration/novels/novel-1/execute",
        json={
            "preview_token": preview.json()["data"]["token"],
            "run_mode": "chapter_review",
            "idempotency_key": "manifest-regenerate-v1",
        },
    )
    assert executed.status_code == 200
    archive_id = executed.json()["data"]["archive_id"]
    assert db.get_connection().execute(
        "SELECT active_plan_revision_id FROM outline_planning_heads "
        "WHERE novel_id = 'novel-1'"
    ).fetchone()[0] != original.id

    restored = client.post(
        f"/api/v1/worldline-regeneration/novels/novel-1/archives/{archive_id}/restore",
        json={"run_mode": "chapter_review", "idempotency_key": "manifest-restore-v1"},
    )
    assert restored.status_code == 200
    assert restored.json()["data"]["operation"] == "restore"
    assert db.get_connection().execute(
        "SELECT active_plan_revision_id FROM outline_planning_heads "
        "WHERE novel_id = 'novel-1'"
    ).fetchone()[0] == original.id


def test_manifest_worldline_continue_route_replays_the_same_key(client, db):
    _seed(db)
    _materialize_manifest_worldline(db)

    preview = client.post(
        "/api/v1/worldline-regeneration/novels/novel-1/preview",
        json={"start_chapter": 4, "target_chapters": 8},
    )
    assert preview.status_code == 200
    assert preview.json()["data"]["operation"] == "continue"
    request = {
        "preview_token": preview.json()["data"]["token"],
        "run_mode": "continuous",
        "idempotency_key": "manifest-continue-v1",
    }

    first = client.post(
        "/api/v1/worldline-regeneration/novels/novel-1/execute", json=request
    )
    repeated = client.post(
        "/api/v1/worldline-regeneration/novels/novel-1/execute", json=request
    )

    assert first.status_code == 200
    assert repeated.status_code == 200
    assert repeated.json()["data"] == first.json()["data"]
