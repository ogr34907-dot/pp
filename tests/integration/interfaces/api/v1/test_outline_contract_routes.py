"""HTTP surface for the Outline Studio's draft/publish lifecycle."""


def test_outline_root_can_be_drafted_then_published_and_synced(client, test_novel_id):
    tree = client.get(f"/api/v1/outline/novels/{test_novel_id}/tree")
    assert tree.status_code == 200
    root = tree.json()["data"]
    assert root["node_type"] == "outline"
    assert root["outline_contract"]["status"] == "missing"

    draft = client.post(
        f"/api/v1/outline/contracts/{root['id']}/draft",
        json={
            "source": "author",
            "payload": {
                "title": "总纲：代价与归来",
                "creative_goal": "主角在牺牲中完成归来",
                "required_events": ["必须失去故乡"],
                "forbidden_events": ["不得无代价获胜"],
            },
        },
    )
    assert draft.status_code == 200
    revision = draft.json()["data"]["draft"]["revision"]

    publish = client.post(
        f"/api/v1/outline/contracts/{root['id']}/publish",
        json={"expected_revision": revision, "idempotency_key": "root-publish-v1"},
    )
    assert publish.status_code == 200
    assert publish.json()["data"]["active"]["status"] == "synced"

    refreshed = client.get(f"/api/v1/outline/novels/{test_novel_id}/tree")
    body = refreshed.json()["data"]
    assert body["title"] == "总纲：代价与归来"
    assert body["outline_contract"]["status"] == "synced"
