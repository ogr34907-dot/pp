"""Public dual-mode run state is server-authoritative, not inferred by the UI."""

from application.engine.dag.engine import DAGEngine
from application.engine.dag.models import get_default_dag
from application.blueprint.services.outline_contract_service import OutlineContractService
from domain.structure.outline_contract import OutlinePayload, OutlineSource
from domain.structure.story_node import NodeType, StoryNode
from infrastructure.persistence.database.outline_contract_repository import OutlineContractRepository
from infrastructure.persistence.database.story_node_repository import StoryNodeRepository
from interfaces.api.v1.engine import generation_routes


def _publish_next_chapter_chain(db, novel_id: str, *, chapter_number: int = 1) -> None:
    """Build the smallest published five-level chain accepted by a new run."""

    node_repo = StoryNodeRepository(db)
    parent_id = None
    nodes = []
    for index, node_type in enumerate(
        (NodeType.PART, NodeType.VOLUME, NodeType.ACT, NodeType.CHAPTER), start=1
    ):
        node = StoryNode(
            id=f"{novel_id}-{node_type.value}-{chapter_number}",
            novel_id=novel_id,
            node_type=node_type,
            number=chapter_number,
            title=f"{node_type.value}-{chapter_number}",
            order_index=index,
            parent_id=parent_id,
        )
        node_repo.save_sync(node)
        nodes.append(node)
        parent_id = node.id

    service = OutlineContractService(
        contract_repository=OutlineContractRepository(db), story_node_repository=node_repo
    )
    payload = OutlinePayload(
        title="已发布文学大纲",
        narrative_text="主角在代价中推进故事，并把当前结局交给下一阶段。",
        creative_goal="推进冲突和人物变化",
        entry_state="承接前一阶段结局",
        exit_state="留下下一阶段必须回应的变化",
        required_events=["发生不可逆选择"],
        state_changes={"characters": [{"name": "主角", "change": "承担代价"}]},
        handoff_conditions=["下一阶段承接本阶段结局"],
        chapter_start=chapter_number,
        chapter_end=chapter_number,
    )
    root = service.contract_repository.ensure_root(novel_id)
    root = service.contract_repository.save_draft(root.id, payload, source=OutlineSource.AUTHOR)
    service.contract_repository.publish_and_sync(root.id, expected_revision=root.draft.revision)
    for node in nodes:
        slot = service.ensure_contract_for_story_node(novel_id, node.id)
        slot = service.contract_repository.save_draft(slot.id, payload, source=OutlineSource.AUTHOR)
        service.contract_repository.publish_and_sync(slot.id, expected_revision=slot.draft.revision)


def test_production_candidate_workflow_uses_dag_v2_factory(monkeypatch, db):
    monkeypatch.setattr(generation_routes.api_dependencies, "get_database", lambda: db)

    service = generation_routes.get_candidate_workflow_service()

    assert isinstance(service.dag_engine, DAGEngine)
    assert service.dag_factory is get_default_dag


def test_start_review_mode_exposes_strict_backpressure_state(client, db, test_novel_id):
    _publish_next_chapter_chain(db, test_novel_id)
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


def test_start_rejects_a_new_novel_until_its_full_five_level_outline_is_synced(client, test_novel_id):
    response = client.post(
        f"/api/v1/generation/novels/{test_novel_id}/start",
        json={"run_mode": "chapter_review", "target_chapters": 1},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "generation_preflight:outline_chain_not_ready"


def test_start_accepts_a_complete_published_five_level_outline(client, db, test_novel_id):
    _publish_next_chapter_chain(db, test_novel_id)

    response = client.post(
        f"/api/v1/generation/novels/{test_novel_id}/start",
        json={"run_mode": "chapter_review", "target_chapters": 1},
    )

    assert response.status_code == 200
    assert response.json()["data"]["state"] == "running"


def test_start_rejects_an_active_full_canonical_resync(client, db, test_novel_id):
    _publish_next_chapter_chain(db, test_novel_id)
    db.execute(
        "UPDATE novels SET autopilot_recovery_reason = ? WHERE id = ?",
        ("canonical_aftermath_full_resync:run-1:9999999999", test_novel_id),
    )
    db.get_connection().commit()

    response = client.post(
        f"/api/v1/generation/novels/{test_novel_id}/start",
        json={"run_mode": "continuous", "target_chapters": 1},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "generation_preflight:canonical_resync_active"


def test_start_rejects_an_active_worldline_rebuild(client, db, test_novel_id):
    _publish_next_chapter_chain(db, test_novel_id)
    db.execute(
        """
        INSERT INTO novel_generation_runs
            (novel_id, run_mode, state, generation_epoch, target_chapters, canonical_sync_status, next_action)
        VALUES (?, 'chapter_review', 'paused', 3, 1, 'rebuilding', 'rebuild_worldline')
        """,
        (test_novel_id,),
    )
    db.execute(
        """
        INSERT INTO worldline_rebuild_jobs
            (id, novel_id, generation_epoch, job_type, status)
        VALUES ('rebuild-1', ?, 3, 'canonical_facts', 'running')
        """,
        (test_novel_id,),
    )
    db.get_connection().commit()

    response = client.post(
        f"/api/v1/generation/novels/{test_novel_id}/start",
        json={"run_mode": "continuous", "target_chapters": 1},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "generation_preflight:worldline_rebuild_active"


def test_start_rejects_a_conflicting_next_formal_chapter_number(client, db, test_novel_id):
    _publish_next_chapter_chain(db, test_novel_id)
    db.execute(
        """
        INSERT INTO chapters (id, novel_id, number, title, content, status)
        VALUES ('manual-chapter-1', ?, 1, '手工章节', '尚未经过候选提交', 'draft')
        """,
        (test_novel_id,),
    )
    db.get_connection().commit()

    response = client.post(
        f"/api/v1/generation/novels/{test_novel_id}/start",
        json={"run_mode": "chapter_review", "target_chapters": 1},
    )

    assert response.status_code == 409
    assert response.json()["detail"] == "generation_preflight:next_chapter_number_conflict"


def test_generation_state_returns_idle_before_any_run_starts(client, test_novel_id):
    status = client.get(f"/api/v1/generation/novels/{test_novel_id}/state")

    assert status.status_code == 200
    payload = status.json()["data"]
    assert payload["state"] == "idle"
    assert payload["candidate"] is None


def test_legacy_chapter_review_write_is_retired_for_new_candidate_runs(client, test_novel_id):
    response = client.put(
        f"/api/v1/novels/{test_novel_id}/chapters/1/review",
        json={"status": "approved", "memo": "旧审核不能提交正式章节"},
    )

    assert response.status_code == 410
    assert response.json()["detail"] == "candidate_first_required"
