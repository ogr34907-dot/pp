from dataclasses import dataclass

import pytest

from application.blueprint.services.future_replan_service import (
    FutureReplanDecision,
    FutureReplanService,
)
from domain.structure.outline_contract import OutlineLevel, OutlinePayload, OutlineSource
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.outline_contract_repository import (
    OutlineContractRepository,
)


@dataclass
class _FormalHistory:
    head: int
    blockers: tuple[str, ...] = ()

    def formal_history_snapshot(self, _novel_id: str):
        return self.head, self.blockers


def _active_manifest(tmp_path):
    database = DatabaseConnection(str(tmp_path / "future-replan.db"))
    database.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES "
        "('novel-1', '重规划', 'future-replan', 10)"
    )
    database.get_connection().commit()
    repository = OutlineContractRepository(database)
    root = repository.ensure_root("novel-1")
    draft = repository.save_draft(
        root.id,
        OutlinePayload(
            title="总纲", narrative_text="完整总纲", creative_goal="目标",
            entry_state="开始", exit_state="结束", chapter_start=1, chapter_end=10,
        ),
        source=OutlineSource.AUTHOR,
    )
    repository.publish_and_sync(root.id, expected_revision=draft.draft.revision)
    active = repository.backfill_initial_plan("novel-1").plan
    conn = database.get_connection()
    conn.execute(
        "UPDATE outline_planning_heads SET authority_mode='manifest', "
        "authority_generation=1, projection_generation=1 WHERE novel_id='novel-1'"
    )
    conn.commit()
    return repository, active


def test_future_replan_creates_draft_only_after_the_formal_head(tmp_path):
    repository, active = _active_manifest(tmp_path)
    service = FutureReplanService(repository, _FormalHistory(head=0))

    result = service.preview_and_open_draft(
        novel_id="novel-1",
        changed_logical_node_id=active.items[0].logical_node_id,
        replan_start_chapter=1,
        author_intent="改写未来方向",
    )

    assert result.decision == FutureReplanDecision.FUTURE_REPLAN
    assert result.draft_plan is not None
    assert result.draft_plan.parent_plan_revision_id == active.id
    assert result.invalidated_logical_node_ids == (active.items[0].logical_node_id,)


def test_future_replan_crossing_formal_history_requires_worldline(tmp_path):
    repository, active = _active_manifest(tmp_path)
    service = FutureReplanService(repository, _FormalHistory(head=1))

    result = service.preview_and_open_draft(
        novel_id="novel-1",
        changed_logical_node_id=active.items[0].logical_node_id,
        replan_start_chapter=1,
        author_intent="改写已完成剧情",
    )

    assert result.decision == FutureReplanDecision.WORLDLINE_REQUIRED
    assert result.draft_plan is None
    assert repository.get_planning_head("novel-1").working_plan_revision_id is None


def test_future_replan_rejects_unproven_formal_history(tmp_path):
    repository, active = _active_manifest(tmp_path)
    service = FutureReplanService(
        repository, _FormalHistory(head=0, blockers=("formal:chapter:1",))
    )

    result = service.preview_and_open_draft(
        novel_id="novel-1",
        changed_logical_node_id=active.items[0].logical_node_id,
        replan_start_chapter=1,
        author_intent="改写未来方向",
    )

    assert result.decision == FutureReplanDecision.HISTORY_UNPROVEN
    assert result.draft_plan is None
    assert result.blockers == ("formal:chapter:1",)
