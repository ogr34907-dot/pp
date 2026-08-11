"""Public dual-mode run state is server-authoritative, not inferred by the UI."""


def test_start_review_mode_exposes_strict_backpressure_state(client, test_novel_id):
    started = client.post(
        f"/api/v1/generation/novels/{test_novel_id}/start",
        json={"run_mode": "chapter_review", "target_chapters": 10},
    )
    assert started.status_code == 200
    data = started.json()["data"]
    assert data["run_mode"] == "chapter_review"
    assert data["state"] == "running"
    assert data["max_pending_candidates"] == 1
    assert data["prefetch"] == 0

    status = client.get(f"/api/v1/generation/novels/{test_novel_id}/state")
    assert status.status_code == 200
    assert status.json()["data"]["state"] == "running"

    stopped = client.post(f"/api/v1/generation/novels/{test_novel_id}/stop")
    assert stopped.status_code == 200
    # The legacy status route is what Home and Workbench already poll.  It
    # must consume the durable candidate run rather than show stale writing.
    legacy_status = client.get(f"/api/v1/autopilot/{test_novel_id}/status")
    assert legacy_status.status_code == 200
    assert legacy_status.json()["autopilot_status"] == "stopped"
    assert legacy_status.json()["generation"]["state"] == "stopped"
