import json

import pytest

from domain.ai.services.llm_service import GenerationConfig
from domain.ai.value_objects.prompt import Prompt
from application.blueprint.services.chapter_planning_policy import has_rendered_chapter_execution_plan
from application.blueprint.services.chapter_plan_renderer import render_chapter_execution_plan
from application.engine.services.memory_engine import MemoryDeltaPayload
from infrastructure.ai.providers.mock_provider import MockProvider


async def _generate(user: str, system: str = "测试系统") -> str:
    provider = MockProvider()
    result = await provider.generate(Prompt(system=system, user=user), GenerationConfig())
    return result.content


def _loads(content: str) -> dict:
    return json.loads(content)


@pytest.mark.asyncio
async def test_mock_provider_macro_plan_returns_contract_shape():
    data = _loads(await _generate('请生成宏观结构，输出 "parts" 的部-卷-幕 JSON。'))

    parts = data["parts"]
    assert parts and parts[0]["volumes"]
    first_act = parts[0]["volumes"][0]["acts"][0]
    assert {"number", "title", "description", "key_events", "conflicts"} <= set(first_act)


@pytest.mark.asyncio
async def test_mock_provider_worldbuilding_returns_five_dimensions():
    data = _loads(await _generate("请生成世界观 worldbuilding 和核心法则。"))

    worldbuilding = data["worldbuilding"]
    assert {"core_rules", "geography", "society", "culture", "daily_life"} <= set(worldbuilding)
    assert data["style"]


@pytest.mark.asyncio
async def test_mock_provider_characters_and_locations_return_expected_arrays():
    characters = _loads(await _generate("请生成 characters 人物角色。"))["characters"]
    locations = _loads(await _generate("请生成 locations 地点地图。"))["locations"]

    assert len(characters) >= 3
    assert {"name", "role", "description", "voice_profile", "relationships"} <= set(characters[0])
    assert len(locations) >= 3
    assert {"id", "name", "type", "description", "connections"} <= set(locations[0])


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("stage_schema", "result_key"),
    [
        ('"characters": [{"name": "...", "role": "..."}]', "characters"),
        ('"locations": [{"id": "...", "name": "..."}]', "locations"),
    ],
)
async def test_mock_provider_stage_schema_wins_over_shared_worldbuilding_context(
    stage_schema: str,
    result_key: str,
):
    data = _loads(
        await _generate(
            "已有世界观 worldbuilding 与核心法则作为上下文。"
            f"请严格输出 {stage_schema}。"
        )
    )

    assert result_key in data
    assert len(data[result_key]) >= 3


@pytest.mark.asyncio
async def test_mock_provider_act_plan_schema_wins_over_character_context():
    data = _loads(
        await _generate(
            "可用人物：核心人物甲。"
            '请为这一幕规划 3 个章节，并严格输出 {"chapters": ['
            '{"number": 1, "title": "章节标题", "characters": ["人物ID"]}]}。'
        )
    )

    chapters = data["chapters"]
    assert len(chapters) == 3
    assert [chapter["number"] for chapter in chapters] == [1, 2, 3]
    for chapter in chapters:
        assert {"title", "main_event", "handoff_from_previous", "handoff_to_next"} <= set(chapter)


@pytest.mark.asyncio
async def test_mock_provider_chapter_preplan_returns_renderable_execution_script():
    data = _loads(
        await _generate(
            '请输出 JSON，包含 "detail_title"、"key_plot_points"、'
            '"chapter_characters" 与 "chapter_plan"。'
            "chapter_plan 必须覆盖七段执行剧本，并包含 characters 作为上下文。"
        )
    )

    plan = data["chapter_plan"]
    assert data["detail_title"]
    assert data["key_plot_points"]
    assert data["chapter_characters"]
    assert has_rendered_chapter_execution_plan(render_chapter_execution_plan(plan))


@pytest.mark.asyncio
async def test_mock_provider_chapter_prose_wins_over_character_context():
    content = await _generate(
        "文章字数：2000\n"
        "【连续性上下文】人物 characters：核心人物甲。\n"
        "【正文细纲】当前冲突地点发生必须回应的压力。\n"
        "请生成正文内容。"
    )

    assert not content.lstrip().startswith("{")
    assert "当前冲突地点" in content
    assert len(content) >= 2000


@pytest.mark.asyncio
async def test_mock_provider_narrative_sync_returns_non_empty_canonical_summary():
    data = _loads(
        await _generate(
            '你是叙事编辑，请输出 JSON，包含 "summary"、"key_events"、'
            '"open_threads"、"relation_triples" 与 "character_states"。'
            "第 1 章正文如下：核心人物甲做出选择，角色状态发生变化。"
        )
    )

    assert data["summary"]
    assert data["key_events"]
    assert data["open_threads"]
    assert isinstance(data["relation_triples"], list)
    assert isinstance(data["character_states"], list)


@pytest.mark.asyncio
async def test_mock_provider_memory_extraction_matches_canonical_memory_contract():
    data = _loads(
        await _generate(
            "你是叙事状态追踪引擎，只返回 JSON。"
            "字段必须为 completed_beats、revealed_clues、fact_violations。"
            'completed_beats 项必须含 beat_id、summary、chapter、characters_involved。'
            "请从第 1 章正文提取记忆增量。"
        )
    )

    payload = MemoryDeltaPayload.model_validate(data)

    assert payload.completed_beats
    assert payload.revealed_clues
    assert payload.fact_violations == []


@pytest.mark.asyncio
async def test_mock_provider_main_plot_options_return_three_options():
    data = _loads(await _generate("setup_main_plot_options_v1，请输出 plot_options。"))

    options = data["plot_options"]
    assert len(options) == 3
    assert {"id", "type", "title", "logline", "core_conflict", "starting_hook"} <= set(options[0])


@pytest.mark.asyncio
async def test_mock_provider_plot_outline_returns_contract_shape():
    data = _loads(await _generate('请输出 "plot_outline" 剧情总纲 JSON。'))

    outline = data["plot_outline"]
    assert {"main_story_overview", "stage_plan", "expected_ending", "core_conflict"} <= set(outline)
    assert len(outline["stage_plan"]) == 5
    assert {"phase", "label", "range_percent", "summary"} <= set(outline["stage_plan"][0])


@pytest.mark.asyncio
async def test_mock_provider_outline_cohort_returns_non_empty_payload_array():
    content = await _generate(
        'Return a JSON array of complete sibling plans. Each item must include '
        'title, narrative_text, creative_goal, entry_state, exit_state, conflicts, '
        'state_changes, handoff_conditions, chapter_start, and chapter_end.'
    )

    payloads = json.loads(content)
    assert isinstance(payloads, list)
    assert payloads
    assert {
        "title",
        "narrative_text",
        "creative_goal",
        "entry_state",
        "exit_state",
        "conflicts",
        "state_changes",
        "handoff_conditions",
        "chapter_start",
        "chapter_end",
    } <= set(payloads[0])
    for previous, current in zip(payloads, payloads[1:]):
        assert current["entry_state"] == previous["exit_state"]


@pytest.mark.asyncio
async def test_mock_provider_chapter_review_returns_review_contract():
    data = _loads(await _generate('章节 AI 审阅，请输出 "score" 和 "issues"。'))

    assert data["status"] in {"draft", "reviewed", "approved"}
    assert isinstance(data["score"], int)
    assert isinstance(data["suggestions"], list)


@pytest.mark.asyncio
async def test_mock_provider_macro_refactor_returns_proposal_contract_before_character_match():
    data = _loads(
        await _generate(
            """你是一个小说编辑，帮助修复角色的人设冲突。
请以 JSON 输出 natural_language_suggestion、suggested_mutations、suggested_tags 和 reasoning。"""
        )
    )

    assert data["natural_language_suggestion"]
    assert data["reasoning"]
    assert data["suggested_mutations"] == []
    assert data["suggested_tags"] == []


@pytest.mark.asyncio
async def test_mock_provider_does_not_emit_fixed_story_bias_terms():
    provider = MockProvider()
    prompts = [
        "请生成宏观结构，输出部-卷-幕 JSON。",
        "请生成世界观 worldbuilding。",
        "请生成 characters 人物角色。",
        "请生成 locations 地点地图。",
        "setup_main_plot_options_v1，请输出 plot_options。",
        '请输出 "plot_outline" 剧情总纲 JSON。',
    ]
    forbidden_terms = [
        "修仙",
        "灵气",
        "穿越",
        "宗门",
        "学院",
        "无法修炼",
        "科学修仙",
    ]

    outputs = []
    for user in prompts:
        result = await provider.generate(Prompt(system="测试系统", user=user), GenerationConfig())
        outputs.append(result.content)

    joined = "\n".join(outputs)
    assert "?" * 4 not in joined
    assert chr(0xFFFD) not in joined
    for term in forbidden_terms:
        assert term not in joined


@pytest.mark.asyncio
async def test_mock_provider_stream_matches_generate_content():
    provider = MockProvider()
    prompt = Prompt(system="测试系统", user="请生成世界观 worldbuilding。")

    direct = await provider.generate(prompt, GenerationConfig())
    streamed = "".join([chunk async for chunk in provider.stream_generate(prompt, GenerationConfig())])

    assert streamed == direct.content
