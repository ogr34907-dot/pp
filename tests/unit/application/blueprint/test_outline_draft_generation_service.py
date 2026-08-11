"""AI outline generation is constrained by the published parent contract."""

from types import SimpleNamespace

import pytest

from application.blueprint.services.outline_draft_generation_service import (
    OutlineDraftGenerationError,
    OutlineDraftGenerationService,
)
from domain.structure.outline_contract import OutlineLevel, OutlinePayload, OutlineSource
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.outline_contract_repository import OutlineContractRepository


class _LLM:
    def __init__(self, content: str):
        self.content = content
        self.prompts = []

    async def generate(self, prompt, _config):
        self.prompts.append(prompt)
        return SimpleNamespace(content=self.content)


@pytest.mark.asyncio
async def test_ai_draft_is_json_normalized_and_uses_only_synced_parent_context(tmp_path):
    db = DatabaseConnection(str(tmp_path / "outline-draft.db"))
    db.execute("INSERT INTO novels (id, title, slug) VALUES ('novel-1', '大纲小说', 'outline-draft')")
    db.get_connection().commit()
    repo = OutlineContractRepository(db)
    root = repo.ensure_root("novel-1")
    llm = _LLM(
        "```json\n{\"title\":\"AI 总纲\",\"creative_goal\":\"让主角承担代价\",\"required_events\":[\"失去故乡\"]}\n```"
    )
    service = OutlineDraftGenerationService(repo, llm, db)

    drafted = await service.generate_draft(root.id)

    assert drafted.draft.payload.title == "AI 总纲"
    assert drafted.draft.source == OutlineSource.AI
    assert "总纲" in llm.prompts[0].user

    repo.publish_and_sync(root.id, expected_revision=drafted.draft.revision)
    child = repo.create_contract(
        novel_id="novel-1", level=OutlineLevel.PART, parent_contract_id=root.id
    )
    llm.content = '{"title":"第一部","narrative_text":"承接总纲推进"}'
    child_draft = await service.generate_draft(child.id)
    assert child_draft.draft.payload.title == "第一部"
    assert "AI 总纲" in llm.prompts[-1].user


@pytest.mark.asyncio
async def test_outline_draft_requires_a_json_object(tmp_path):
    db = DatabaseConnection(str(tmp_path / "outline-draft-invalid.db"))
    db.execute("INSERT INTO novels (id, title, slug) VALUES ('novel-1', '大纲小说', 'outline-draft-invalid')")
    db.get_connection().commit()
    repo = OutlineContractRepository(db)
    root = repo.ensure_root("novel-1")
    service = OutlineDraftGenerationService(repo, _LLM("这不是 JSON"), db)

    with pytest.raises(OutlineDraftGenerationError, match="requires_json_object"):
        await service.generate_draft(root.id)
