"""Bible setup invocation keeps its authored variable snapshot current."""

from fastapi import FastAPI
from fastapi.testclient import TestClient

from application.ai_invocation.variable_hub import VariableWrite
from application.core.v1_length_tiers import build_v1_structure_black_box_hint
from domain.ai.services.llm_service import GenerationConfig, GenerationResult
from domain.ai.value_objects.prompt import Prompt
from domain.ai.value_objects.token_usage import TokenUsage
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_ai_invocation_repository import (
    SqliteVariableHubRepository,
)
from interfaces.api.v1.engine import ai_invocation_routes


class _StreamingLLM:
    async def generate(self, prompt: Prompt, config: GenerationConfig) -> GenerationResult:
        return GenerationResult(
            content="HTTP正文",
            token_usage=TokenUsage(input_tokens=1, output_tokens=1),
        )

    async def stream_generate(self, prompt: Prompt, config: GenerationConfig):
        yield "HTTP"
        yield "正文"


def test_bible_setup_invocation_materializes_inputs_and_refreshes_the_snapshot(
    tmp_path,
    monkeypatch,
):
    db = DatabaseConnection(str(tmp_path / "plotpilot-test-bible-vars.db"))
    monkeypatch.setattr(ai_invocation_routes, "get_database", lambda db_path=None: db)
    monkeypatch.setattr(
        "infrastructure.persistence.database.connection.get_database",
        lambda db_path=None: db,
    )
    monkeypatch.setattr(ai_invocation_routes, "get_llm_service", lambda: _StreamingLLM())

    import infrastructure.ai.prompt_manager as prompt_manager_module
    import infrastructure.ai.prompt_registry as prompt_registry_module

    prompt_manager_module._manager_instance = prompt_manager_module.PromptManager(db)
    prompt_registry_module._registry_instance = prompt_registry_module.PromptRegistry(
        prompt_manager=prompt_manager_module._manager_instance
    )

    app = FastAPI()
    app.include_router(ai_invocation_routes.router)
    client = TestClient(app)
    internal_hint = build_v1_structure_black_box_hint("standard", 500, 2000)
    author_premise = "旧设定：少年在海底城觉醒"

    created = client.post(
        "/ai-invocations",
        json={
            "operation": "bible.setup.worldbuilding",
            "node_key": "bible-worldbuilding",
            "policy": "FULL_INTERACTIVE",
            "context": {"novel_id": "novel-bible-vars"},
            "variables": {
                "novel_title": "变量小说",
                "premise": f"{internal_hint}\n\n{author_premise}",
                "target_chapters": 120,
                "target_words_per_chapter": 2500,
                "genre_opening_profile": {"source_level": "test", "opening_mechanism": "压迫开局"},
                "genre_reader_contract": {"reader_promise": "升级破局"},
                "genre_rhythm_constraints": {"payoff_interval": "三章一回收"},
            },
        },
    )
    assert created.status_code == 200, created.text
    session_id = created.json()["session"]["id"]

    repo = SqliteVariableHubRepository(db)
    assert repo.get_value("novel.setup.premise", "novel_id:novel-bible-vars").value == author_premise
    assert repo.get_value("novel.setup.summary", "novel_id:novel-bible-vars") is None
    assert repo.get_value("system.worldbuilding.fields_desc", "novel_id:novel-bible-vars") is None
    assert repo.get_value("novel.worldbuilding.full", "novel_id:novel-bible-vars") is None
    assert repo.get_value("novel.genre.opening_profile", "novel_id:novel-bible-vars") is None
    assert repo.get_value("novel.genre.reader_contract", "novel_id:novel-bible-vars") is None

    repo.set_value(
        VariableWrite(
            key="novel.setup.premise",
            value="新设定：变量中心改为天空城债务法则",
            context_key="novel_id:novel-bible-vars",
            source_node_key="test",
        )
    )
    refreshed = client.get(f"/ai-invocations/{session_id}")
    assert refreshed.status_code == 200, refreshed.text
    plan = refreshed.json()["session"]["variable_plan"]
    assert plan["aliases"]["premise"] == "新设定：变量中心改为天空城债务法则"
    assert plan["aliases"]["genre_reader_contract"]["reader_promise"] == "升级破局"
    assert any(
        item["key"] == "premise" and item["value"] == "新设定：变量中心改为天空城债务法则"
        for item in plan["snapshot_items"]
    )
    assert all(item["key"] != "fields_desc" for item in plan["snapshot_items"])
    assert all(item["key"] != "genre_reader_contract" for item in plan["snapshot_items"])
    assert "新设定：变量中心改为天空城债务法则" in refreshed.json()["session"]["prompt_snapshot"]["prompt"]["user"]

    db.close_all(skip_checkpoint=True)
