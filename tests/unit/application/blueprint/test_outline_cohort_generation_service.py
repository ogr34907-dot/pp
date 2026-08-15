"""Manifest-native sibling cohort generation keeps LLM output draft-only."""

from types import SimpleNamespace

import pytest

from application.blueprint.services.outline_cohort_generation_service import (
    OutlineCohortGenerationError,
    OutlineCohortGenerationService,
)
from application.blueprint.services.outline_contract_service import OutlineContractService
from domain.structure.outline_contract import OutlineLevel, OutlinePayload, OutlineSource
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.outline_contract_repository import (
    OutlineContractRepository,
)
from infrastructure.persistence.database.story_node_repository import StoryNodeRepository


class _LLM:
    def __init__(self, content: str):
        self.content = content
        self.prompts = []

    async def generate(self, prompt, _config):
        self.prompts.append(prompt)
        return SimpleNamespace(content=self.content)


def _root_item(database, repository):
    root = repository.ensure_root("novel-1")
    draft = repository.save_draft(
        root.id,
        OutlinePayload(
            title="总纲",
            narrative_text="主角在代价中重建秩序。",
            creative_goal="完成核心选择",
            entry_state="旧秩序仍然完整",
            exit_state="新秩序建立但付出代价",
            chapter_start=1,
            chapter_end=10,
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
    draft_plan = repository.clone_active_plan_draft(
        "novel-1", author_intent="让主角选择承担代价"
    )
    return active, draft_plan, draft_plan.items[0]


@pytest.mark.asyncio
async def test_generate_manifest_cohort_persists_attempt_then_replaces_draft(tmp_path):
    database = DatabaseConnection(str(tmp_path / "cohort-service.db"))
    database.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES "
        "('novel-1', '长篇小说', 'cohort-service', 10)"
    )
    database.execute("INSERT INTO bibles (id, novel_id) VALUES ('bible-1', 'novel-1')")
    database.execute(
        "INSERT INTO bible_world_settings (id, novel_id, name, description) VALUES "
        "('world-1', 'novel-1', '世界规则', '力量必须付出代价')"
    )
    database.get_connection().commit()
    repository = OutlineContractRepository(database)
    active, draft, root_item = _root_item(database, repository)
    contract_service = OutlineContractService(
        contract_repository=repository,
        story_node_repository=StoryNodeRepository(database),
    )
    llm = _LLM(
        '[{"title":"第一部","narrative_text":"主角离开故乡并失去依靠。",'
        '"creative_goal":"逼迫主角选择", "entry_state":"旧秩序仍然完整",'
        '"exit_state":"主角失去依靠", "conflicts":["仇家追击"],'
        '"state_changes":{"主角":[{"change":"失去依靠"}]},'
        '"handoff_conditions":["第二部承接追击"], "chapter_start":1,"chapter_end":5},'
        '{"title":"第二部","narrative_text":"主角反击并付出代价。",'
        '"creative_goal":"完成核心选择", "entry_state":"主角失去依靠",'
        '"exit_state":"新秩序建立但付出代价", "conflicts":["反击失败风险"],'
        '"state_changes":{"主角":[{"change":"承担代价"}]},'
        '"handoff_conditions":["后续承接新秩序"], "chapter_start":6,"chapter_end":10}]'
    )
    service = OutlineCohortGenerationService(repository, contract_service, llm, database)

    result = await service.generate_cohort(
        plan_revision_id=draft.id,
        parent_logical_node_id=root_item.logical_node_id,
        level=OutlineLevel.PART,
    )

    assert result["attempt"]["status"] == "completed"
    assert result["plan"].id == draft.id
    assert [item.level for item in result["plan"].items] == [
        OutlineLevel.OUTLINE,
        OutlineLevel.PART,
        OutlineLevel.PART,
    ]
    assert repository.get_planning_head("novel-1").active_plan_revision_id == active.id
    assert "世界规则" in llm.prompts[0].user
    assert "让主角选择承担代价" in llm.prompts[0].user
    assert "canonical_boundary" in llm.prompts[0].user


@pytest.mark.asyncio
async def test_invalid_manifest_cohort_response_marks_attempt_failed_without_plan_mutation(tmp_path):
    database = DatabaseConnection(str(tmp_path / "cohort-service-invalid.db"))
    database.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES "
        "('novel-1', '长篇小说', 'cohort-service-invalid', 10)"
    )
    database.get_connection().commit()
    repository = OutlineContractRepository(database)
    active, draft, root_item = _root_item(database, repository)
    service = OutlineCohortGenerationService(
        repository,
        OutlineContractService(
            contract_repository=repository,
            story_node_repository=StoryNodeRepository(database),
        ),
        _LLM('{"not":"a cohort"}'),
        database,
    )

    with pytest.raises(OutlineCohortGenerationError, match="JSON"):
        await service.generate_cohort(
            plan_revision_id=draft.id,
            parent_logical_node_id=root_item.logical_node_id,
            level=OutlineLevel.PART,
        )

    row = database.get_connection().execute(
        "SELECT id FROM outline_plan_cohort_attempts ORDER BY created_at DESC LIMIT 1"
    ).fetchone()
    attempt = repository.get_manifest_cohort_attempt(str(row["id"]))
    assert attempt["status"] == "failed"
    assert repository.get_plan_revision(draft.id).items == draft.items
    assert repository.get_planning_head("novel-1").active_plan_revision_id == active.id
