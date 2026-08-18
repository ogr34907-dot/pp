"""Regression coverage for frontend-facing error responses."""

from uuid import uuid4


def test_invalid_chapter_element_type_is_a_client_error(client, test_novel_id):
    response = client.get(
        "/api/v1/chapters/elements/not-a-real-element/element-1/chapters"
    )

    assert response.status_code == 400


def test_missing_planning_act_is_not_reported_as_server_error(client, test_novel_id):
    response = client.get("/api/v1/planning/acts/act-does-not-exist")

    assert response.status_code == 404


def test_missing_outline_novel_is_not_reported_as_foreign_key_server_error(client):
    response = client.get(f"/api/v1/outline/novels/{uuid4()}/tree")

    assert response.status_code == 404


def test_delete_novel_is_visible_to_the_following_read_when_queue_mode_is_enabled(
    client, db, monkeypatch
):
    novel_id = "delete-read-after-write"
    db.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        (novel_id, "Delete read test", novel_id, 5),
    )
    db.get_connection().commit()
    monkeypatch.setenv("PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES", "0")

    deleted = client.delete(f"/api/v1/novels/{novel_id}")
    assert deleted.status_code == 204

    refreshed = client.get(f"/api/v1/novels/{novel_id}")
    assert refreshed.status_code == 404
