"""Real API and SQLite acceptance coverage with the LLM boundary replaced."""

from __future__ import annotations

import asyncio
import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from application.blueprint.services.beat_sheet_service import BeatSheetService
from application.blueprint.services.continuous_planning_service import (
    ContinuousPlanningService,
)
from domain.ai.services.llm_service import GenerationResult
from domain.ai.value_objects.token_usage import TokenUsage
from domain.novel.value_objects.novel_id import NovelId
from infrastructure.persistence.database.chapter_element_repository import (
    ChapterElementRepository,
)
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_beat_sheet_repository import (
    SqliteBeatSheetRepository,
)
from infrastructure.persistence.database.sqlite_chapter_repository import (
    SqliteChapterRepository,
)
from infrastructure.persistence.database.sqlite_novel_repository import (
    SqliteNovelRepository,
)
from infrastructure.persistence.database.story_node_repository import StoryNodeRepository
from interfaces.api.dependencies import get_beat_sheet_service
from interfaces.api.v1.blueprint.beat_sheet_routes import router as beat_sheet_router
from interfaces.api.v1.blueprint.continuous_planning_routes import (
    get_service as get_planning_service,
)
from interfaces.api.v1.blueprint.continuous_planning_routes import (
    router as planning_router,
)
from interfaces.api.v1.core.novels import router as novels_router


class _DeterministicLLM:
    """Replace only the external model transport; services and stores stay real."""

    def __init__(self) -> None:
        self._stream_payloads = [
            json.dumps(
                {
                    "node_updates": [
                        {
                            "node_id": "P1",
                            "title": "审计第一部",
                            "description": "主线建立",
                        },
                        {
                            "node_id": "V1_1",
                            "title": "审计第一卷",
                            "description": "开局卷",
                        },
                        {
                            "node_id": "A1_1_1",
                            "title": "审计第一幕",
                            "description": "开局冲突",
                            "estimated_chapters": 10,
                            "narrative_goal": "建立核心冲突",
                            "plot_points": ["发现线索"],
                            "key_characters": ["审计主角"],
                            "key_locations": ["审计地点"],
                            "emotional_arc": "平静到警觉",
                            "setup_for": [],
                            "payoff_from": [],
                        },
                    ]
                },
                ensure_ascii=False,
            ),
            json.dumps(
                {
                    "chapters": [
                        {
                            "number": index,
                            "title": f"审计第{index}章",
                            "main_event": f"推进事件{index}",
                            "handoff_from_previous": (
                                "开篇进入冲突" if index == 1 else "承接前章"
                            ),
                            "handoff_to_next": "留下下一章问题",
                            "outline": f"审计章节{index}的执行纲要",
                        }
                        for index in range(1, 4)
                    ]
                },
                ensure_ascii=False,
            ),
        ]
        self._beat_sheet_payload = json.dumps(
            {
                "scenes": [
                    {
                        "title": "场景一",
                        "goal": "建立问题",
                        "pov_character": "审计主角",
                        "location": "审计地点",
                        "tone": "紧张",
                        "estimated_words": 400,
                    },
                    {
                        "title": "场景二",
                        "goal": "推进冲突",
                        "pov_character": "审计主角",
                        "location": "审计地点",
                        "tone": "压迫",
                        "estimated_words": 400,
                    },
                    {
                        "title": "场景三",
                        "goal": "留下钩子",
                        "pov_character": "审计主角",
                        "location": "审计地点",
                        "tone": "悬疑",
                        "estimated_words": 400,
                    },
                ]
            },
            ensure_ascii=False,
        )

    async def stream_generate(self, _prompt, _config):
        yield self._stream_payloads.pop(0)

    async def generate(self, _prompt, _config):
        return GenerationResult(
            content=self._beat_sheet_payload,
            token_usage=TokenUsage(input_tokens=1, output_tokens=1),
        )


class _EmptyStorylineRepository:
    def get_by_novel_id(self, _novel_id):
        return []


def _clear_cpms_singletons() -> None:
    """Keep this temporary database from becoming the next test's CPMS source."""
    import infrastructure.ai.prompt_gateway as prompt_gateway_module
    import infrastructure.ai.prompt_manager as prompt_manager_module
    import infrastructure.ai.prompt_registry as prompt_registry_module

    prompt_gateway_module._prompt_gateway = None
    prompt_manager_module._manager_instance = None
    prompt_registry_module._registry_instance = None


def test_mock_llm_api_chain_persists_structure_chapters_and_beat_sheet(
    tmp_path,
    monkeypatch,
):
    """A broken API/service/store handoff must not look like a completed plan."""
    _clear_cpms_singletons()
    database = DatabaseConnection(str(tmp_path / "mock-llm-e2e.db"))

    def get_test_database(*_args, **_kwargs):
        return database

    monkeypatch.setattr(
        "infrastructure.persistence.database.connection.get_database",
        get_test_database,
    )
    monkeypatch.setattr("interfaces.api.dependencies.get_database", get_test_database)

    try:
        novel_repo = SqliteNovelRepository(database)
        chapter_repo = SqliteChapterRepository(database)
        story_repo = StoryNodeRepository(database)
        llm = _DeterministicLLM()
        planning_service = ContinuousPlanningService(
            story_node_repo=story_repo,
            chapter_element_repo=ChapterElementRepository(database.db_path),
            llm_service=llm,
            chapter_repository=chapter_repo,
            novel_repository=novel_repo,
        )
        beat_sheet_service = BeatSheetService(
            beat_sheet_repo=SqliteBeatSheetRepository(database),
            chapter_repo=chapter_repo,
            storyline_repo=_EmptyStorylineRepository(),
            llm_service=llm,
            vector_store=None,
        )

        app = FastAPI()
        app.include_router(novels_router, prefix="/api/v1")
        app.include_router(planning_router, prefix="/api/v1")
        app.include_router(beat_sheet_router, prefix="/api/v1")
        app.dependency_overrides[get_planning_service] = lambda: planning_service
        app.dependency_overrides[get_beat_sheet_service] = lambda: beat_sheet_service
        client = TestClient(app)

        created = client.post(
            "/api/v1/novels/",
            json={
                "novel_id": "mock-llm-e2e",
                "title": "TRACE_TITLE_A81F",
                "author": "audit",
                "target_chapters": 12,
                "premise": "TRACE_THEME_C63D",
                "genre": "TRACE_GENRE_B72C",
                "world_preset": "TRACE_WORLD_RULE_D54E",
                "story_structure": "TRACE_STRUCTURE_P72K",
                "pacing_control": "TRACE_PACING_Q83L",
                "writing_style": "TRACE_STYLE_E45F",
                "special_requirements": "TRACE_TABOO_G27B",
                "target_words_per_chapter": 1200,
            },
        )
        assert created.status_code == 201, created.text

        reloaded = client.get("/api/v1/novels/mock-llm-e2e")
        assert reloaded.status_code == 200, reloaded.text
        assert reloaded.json()["title"] == "TRACE_TITLE_A81F"
        assert reloaded.json()["locked_genre"] == "TRACE_GENRE_B72C"
        assert reloaded.json()["locked_world_preset"] == "TRACE_WORLD_RULE_D54E"
        assert reloaded.json()["locked_writing_style"] == "TRACE_STYLE_E45F"
        assert reloaded.json()["locked_special_requirements"] == "TRACE_TABOO_G27B"

        macro = asyncio.run(
            planning_service.generate_macro_plan(
                novel_id="mock-llm-e2e",
                target_chapters=10,
                structure_preference={
                    "parts": 1,
                    "volumes_per_part": 1,
                    "acts_per_volume": 1,
                },
            )
        )
        assert macro["success"] is True
        assert len(macro["structure"]) == 1

        macro_confirm = client.post(
            "/api/v1/planning/novels/mock-llm-e2e/macro/confirm",
            json={"structure": macro["structure"]},
        )
        assert macro_confirm.status_code == 200, macro_confirm.text
        acts = [
            node
            for node in story_repo.get_by_novel_sync("mock-llm-e2e")
            if node.node_type.value == "act"
        ]
        assert len(acts) == 1

        act_plan = client.post(
            f"/api/v1/planning/acts/{acts[0].id}/chapters/generate",
            json={"chapter_count": 3},
        )
        assert act_plan.status_code == 200, act_plan.text
        assert len(act_plan.json()["chapters"]) == 3

        act_confirm = client.post(
            f"/api/v1/planning/acts/{acts[0].id}/chapters/confirm",
            json={"chapters": act_plan.json()["chapters"]},
        )
        assert act_confirm.status_code == 200, act_confirm.text
        chapter = chapter_repo.get_by_novel_and_number(NovelId("mock-llm-e2e"), 1)
        assert chapter is not None
        chapter_id = getattr(chapter.id, "value", chapter.id)

        beat_generated = client.post(
            "/api/v1/beat-sheets/generate",
            json={"chapter_id": chapter_id, "outline": chapter.outline},
        )
        assert beat_generated.status_code == 200, beat_generated.text
        assert beat_generated.json()["total_scenes"] == 3

        beat_reloaded = client.get(f"/api/v1/beat-sheets/{chapter_id}")
        assert beat_reloaded.status_code == 200, beat_reloaded.text
        assert beat_reloaded.json()["total_scenes"] == 3
        assert len(chapter_repo.list_by_novel(NovelId("mock-llm-e2e"))) == 3
    finally:
        _clear_cpms_singletons()
        database.close_all(skip_checkpoint=True)
