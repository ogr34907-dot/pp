"""HTTP surface for the Outline Studio's draft/publish lifecycle."""

from infrastructure.persistence.database.outline_contract_repository import OutlineContractRepository


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


def test_outline_generation_attempt_can_be_recovered_and_cancelled(client, db, test_novel_id):
    repository = OutlineContractRepository(db)
    root = repository.ensure_root(test_novel_id)
    attempt = repository.start_generation_attempt(
        root.id,
        prompt_snapshot={"system": "outline", "user": "draft"},
        context_digest="outline-context-v1",
    )

    recovered = client.get(
        f"/api/v1/outline/contracts/{root.id}/generation-attempts/latest",
        params={"after_sequence": 0},
    )
    assert recovered.status_code == 200
    assert recovered.json()["data"]["id"] == attempt["id"]
    assert recovered.json()["data"]["events"][0]["type"] == "started"

    cancelled = client.post(
        f"/api/v1/outline/contracts/{root.id}/generation-attempts/{attempt['id']}/cancel"
    )
    assert cancelled.status_code == 200
    assert cancelled.json()["data"]["status"] == "cancelled"


def test_outline_draft_preserves_unknown_payload_fields_in_extra(client, test_novel_id):
    root = client.get(f"/api/v1/outline/novels/{test_novel_id}/tree").json()["data"]
    payload = {
        "title": "Forward-compatible outline",
        "state_changes": {"characters": [{"id": "hero", "to": "resolved"}]},
        "foreshadowing": {"setup": ["a sealed letter"], "payoff": ["the seal breaks"]},
        "extra": {"schema_version": 3},
        "field_provenance": {"title": "manifest-import"},
        "field_locks": {"creative_goal": True},
        "future_schema_field": {"retained": True},
    }

    response = client.post(
        f"/api/v1/outline/contracts/{root['id']}/draft",
        json={"source": "author", "payload": payload},
    )

    assert response.status_code == 200
    saved = response.json()["data"]["draft"]["payload"]
    assert saved["state_changes"] == payload["state_changes"]
    assert saved["foreshadowing"] == payload["foreshadowing"]
    assert saved["extra"] == {
        "schema_version": 3,
        "field_provenance": payload["field_provenance"],
        "field_locks": payload["field_locks"],
        "future_schema_field": payload["future_schema_field"],
    }

    round_trip = client.post(
        f"/api/v1/outline/contracts/{root['id']}/draft",
        json={"source": "author", "payload": saved},
    )
    assert round_trip.status_code == 200
    assert round_trip.json()["data"]["draft"]["payload"] == saved


def test_legacy_outline_publish_without_a_draft_remains_conflict(client, db, test_novel_id):
    root = OutlineContractRepository(db).ensure_root(test_novel_id)

    response = client.post(
        f"/api/v1/outline/contracts/{root.id}/publish",
        json={"expected_revision": 1, "idempotency_key": "missing-draft"},
    )

    assert response.status_code == 409
