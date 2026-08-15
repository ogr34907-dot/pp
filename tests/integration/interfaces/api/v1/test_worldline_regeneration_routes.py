"""Public preview/execute/restore surface for generic worldline regeneration."""

import hashlib

from infrastructure.persistence.database.chapter_candidate_repository import (
    ChapterCandidateRepository,
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
