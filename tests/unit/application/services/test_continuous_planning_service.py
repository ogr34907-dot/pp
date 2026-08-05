import hashlib
import json
import logging
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

import application.blueprint.services.continuous_planning_service as continuous_planning_module
from application.blueprint.services.continuous_planning_service import (
    ContinuousPlanningService,
    _extract_outer_json_value,
    _incremental_macro_parts_trustworthy,
    _try_parse_parts_from_llm_buffer,
    get_macro_plan_progress,
)
from application.blueprint.services.chapter_planning_policy import (
    validate_lightweight_act_plan,
)
from domain.ai.value_objects.prompt import Prompt
from domain.novel.value_objects.generation_preferences import GenerationPreferences
from domain.structure.story_node import NodeType, StoryNode


def _make_service() -> ContinuousPlanningService:
    return ContinuousPlanningService(
        story_node_repo=Mock(),
        chapter_element_repo=Mock(),
        llm_service=Mock(),
    )


def _story_node(
    node_id: str,
    node_type: NodeType,
    number: int,
    *,
    parent_id: str | None = None,
    order_index: int | None = None,
    chapter_start: int | None = None,
    chapter_end: int | None = None,
    description: str = "",
    metadata: dict | None = None,
) -> StoryNode:
    return StoryNode(
        id=node_id,
        novel_id="novel-1",
        node_type=node_type,
        number=number,
        title=node_id,
        order_index=order_index if order_index is not None else number,
        parent_id=parent_id,
        chapter_start=chapter_start,
        chapter_end=chapter_end,
        description=description,
        metadata=metadata or {},
    )


def _summary_source_version(*chapters: SimpleNamespace) -> str:
    payload = "|".join(
        f"{chapter.number}:{chapter.content_sha256}:{chapter.content_revision}"
        for chapter in sorted(chapters, key=lambda item: item.number)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class _AsyncStoryNodeRepo:
    def __init__(self, nodes: list[StoryNode]):
        self.nodes = nodes

    async def get_by_novel(self, _novel_id: str):
        return list(self.nodes)

    def get_tree(self, _novel_id: str):
        return SimpleNamespace(nodes=list(self.nodes))


class _VersionedChapterRepo:
    def __init__(self, chapters: list[SimpleNamespace]):
        self.chapters = chapters

    def list_by_novel(self, _novel_id):
        return list(self.chapters)


class _NextActCreationRepo:
    """In-memory structure boundary for next-act preflight tests."""

    def __init__(self, nodes: list[StoryNode]):
        self._nodes = {node.id: node for node in nodes}
        self.saved: list[StoryNode] = []

    async def get_by_id(self, node_id: str):
        return self._nodes.get(node_id)

    async def get_by_novel(self, novel_id: str):
        return [node for node in self._nodes.values() if node.novel_id == novel_id]

    async def save(self, node: StoryNode):
        self.saved.append(node)
        self._nodes[node.id] = node
        return node


def _next_act_service(
    nodes: list[StoryNode],
    *,
    target_chapters: int,
) -> tuple[ContinuousPlanningService, _NextActCreationRepo]:
    story_repo = _NextActCreationRepo(nodes)
    service = ContinuousPlanningService(
        story_node_repo=story_repo,
        chapter_element_repo=Mock(),
        llm_service=Mock(),
        novel_repository=SimpleNamespace(
            get_by_id=Mock(
                return_value=SimpleNamespace(target_chapters=target_chapters)
            )
        ),
    )
    service._get_bible_context = Mock(return_value={})
    service._generate_next_act_info = AsyncMock(
        return_value={
            "title": "下一幕",
            "description": "继续推进主线",
            "suggested_chapter_count": 5,
        }
    )
    return service, story_repo


def test_quick_macro_prompt_for_long_book_uses_leading_volume_detail():
    svc = _make_service()

    prompt = svc._build_quick_macro_prompt({"premise": "主角在高武世界成长。"}, 500)

    assert "只为开篇前导卷规划幕节点" in prompt.system
    assert "后续卷的幕节点留给写作过程中动态生成" in prompt.system
    assert "开篇前导卷标题" in prompt.user
    assert "前 1-2 部" not in prompt.system


@pytest.mark.asyncio
async def test_previous_act_summaries_use_committed_metadata_and_stale_fallback():
    chapters = [
        SimpleNamespace(number=1, content_sha256="hash-1", content_revision=1),
        SimpleNamespace(number=2, content_sha256="hash-2", content_revision=1),
    ]
    committed = _story_node(
        "act-committed",
        NodeType.ACT,
        1,
        parent_id="volume-1",
        chapter_start=1,
        chapter_end=2,
        metadata={
            "summary": "已提交的前幕摘要",
            "summary_state": {
                "status": "committed",
                "chapter_start": 1,
                "chapter_end": 2,
                "source_version": _summary_source_version(*chapters),
            },
        },
    )
    previous_volume = _story_node(
        "volume-1",
        NodeType.VOLUME,
        1,
        chapter_start=1,
        chapter_end=2,
        metadata={
            "summary": "已提交的前卷摘要",
            "summary_state": {
                "status": "committed",
                "chapter_start": 1,
                "chapter_end": 2,
                "source_version": _summary_source_version(*chapters),
            },
        },
    )
    checkpoint = _story_node("chapter-2", NodeType.CHAPTER, 2, parent_id="act-committed")
    checkpoint.metadata = {
        "checkpoint_summary": "最近有效检查点摘要",
        "checkpoint_summary_state": {
            "status": "committed",
            "chapter_start": 1,
            "chapter_end": 2,
            "source_version": _summary_source_version(*chapters),
        },
    }
    stale = _story_node(
        "act-stale",
        NodeType.ACT,
        2,
        parent_id="volume-1",
        chapter_start=3,
        chapter_end=4,
        description="失效幕描述",
        metadata={
            "summary": "失效幕摘要",
            "summary_state": {"status": "stale"},
        },
    )
    current = _story_node(
        "act-current",
        NodeType.ACT,
        3,
        parent_id="volume-1",
        chapter_start=5,
        chapter_end=6,
    )
    service = ContinuousPlanningService(
        story_node_repo=_AsyncStoryNodeRepo([previous_volume, committed, checkpoint, stale, current]),
        chapter_element_repo=Mock(),
        chapter_repository=_VersionedChapterRepo(chapters),
        llm_service=Mock(),
    )

    previous = await service._get_previous_acts_summary(current)

    assert "已提交的前幕摘要" in previous
    assert "已提交的前卷摘要" in previous
    assert "最近有效检查点摘要" in previous
    assert "失效幕摘要" not in previous
    assert "失效幕描述" in previous


@pytest.mark.asyncio
async def test_find_act_for_chapter_uses_chapter_parent_before_range_fallback():
    parent_act = _story_node(
        "act-parent",
        NodeType.ACT,
        1,
        chapter_start=1,
        chapter_end=20,
    )
    misleading_range = _story_node(
        "act-range",
        NodeType.ACT,
        99,
        chapter_start=9,
        chapter_end=9,
    )
    chapter = _story_node(
        "chapter-9",
        NodeType.CHAPTER,
        9,
        parent_id="act-parent",
    )
    service = ContinuousPlanningService(
        story_node_repo=_AsyncStoryNodeRepo([parent_act, misleading_range, chapter]),
        chapter_element_repo=Mock(),
        llm_service=Mock(),
    )

    resolved = await service._find_act_for_chapter("novel-1", 9)

    assert resolved is parent_act


@pytest.mark.asyncio
async def test_dual_track_context_uses_only_current_source_matched_volume_summaries():
    chapters = [
        SimpleNamespace(number=number, content_sha256=f"hash-{number}", content_revision=1)
        for number in range(1, 7)
    ]
    older_volume = _story_node(
        "volume-older",
        NodeType.VOLUME,
        1,
        chapter_start=1,
        chapter_end=2,
        metadata={
            "summary": "最近有效前卷摘要",
            "summary_state": {
                "status": "committed",
                "chapter_start": 1,
                "chapter_end": 2,
                "source_version": _summary_source_version(*chapters[:2]),
            },
        },
    )
    stale_volume = _story_node(
        "volume-stale",
        NodeType.VOLUME,
        2,
        chapter_start=3,
        chapter_end=4,
        metadata={
            "summary": "不应进入规划的失效卷摘要",
            "summary_state": {"status": "stale"},
        },
    )
    current_volume = _story_node(
        "volume-current",
        NodeType.VOLUME,
        3,
        chapter_start=5,
        chapter_end=6,
        metadata={
            "summary": "当前有效卷摘要",
            "summary_state": {
                "status": "committed",
                "chapter_start": 5,
                "chapter_end": 6,
                "source_version": _summary_source_version(*chapters[4:]),
            },
        },
    )
    current_act = _story_node(
        "act-current",
        NodeType.ACT,
        3,
        parent_id="volume-current",
        chapter_start=5,
        chapter_end=6,
    )
    service = ContinuousPlanningService(
        story_node_repo=_AsyncStoryNodeRepo(
            [older_volume, stale_volume, current_volume, current_act]
        ),
        chapter_element_repo=Mock(),
        chapter_repository=_VersionedChapterRepo(chapters),
        llm_service=Mock(),
    )

    context = await service._collect_dual_track_context("novel-1", current_act, {})

    assert "当前有效卷摘要" in context["current_volume_summary"]
    assert "最近有效前卷摘要" in context["volume_summary"]
    assert "不应进入规划的失效卷摘要" not in context["volume_summary"]


def test_parse_llm_response_repairs_truncated_macro_plan_json():
    svc = _make_service()

    response = """```json
{
  "parts": [
    {
      "title": "第一部",
      "volumes": [
        {
          "title": "卷一",
          "acts": [
            {
              "title": "初入京城",
              "description": "主角进入京城，卷入风暴",
              "core_conflict": "必须在权斗中站稳脚跟"
            }
          ]
        }
      ]
    }
  ],
  "theme": "权谋成长"
"""

    result = svc._parse_llm_response(response)

    assert result["theme"] == "权谋成长"
    assert result["parts"][0]["volumes"][0]["acts"][0]["title"] == "初入京城"


def test_parse_llm_response_repairs_unterminated_string():
    svc = _make_service()

    response = """{
  "parts": [
    {
      "title": "第一部",
      "volumes": [
        {
          "title": "卷一",
          "acts": [
            {
              "title": "初入京城",
              "description": "主角进入京城",
              "core_conflict": "站稳脚跟"
            },
            {
              "title": "风暴将至",
              "description": "主角发现
"""

    result = svc._parse_llm_response(response)

    acts = result["parts"][0]["volumes"][0]["acts"]
    assert len(acts) == 2
    assert acts[0]["title"] == "初入京城"
    assert acts[1]["title"] == "风暴将至"


def test_parse_llm_response_repairs_missing_comma_between_fields():
    svc = _make_service()

    response = """{
  "parts": [
    {
      "title": "第一部"
      "volumes": [
        {
          "title": "卷一",
          "acts": []
        }
      ]
    }
  ],
  "theme": "权谋成长"
}"""

    result = svc._parse_llm_response(response)

    assert result["theme"] == "权谋成长"
    assert result["parts"][0]["title"] == "第一部"
    assert result["parts"][0]["volumes"][0]["title"] == "卷一"


def test_extract_outer_json_value_prefers_object_root_over_leading_array():
    text = '["noise"] {"parts": [], "theme": "x"}'

    result = _extract_outer_json_value(text)

    assert result == '{"parts": [], "theme": "x"}'


def test_incremental_macro_parts_trustworthy_rejects_single_char_titles():
    bad = [
        {
            "title": "第一部",
            "volumes": [
                {"title": "卷一", "acts": [{"title": "X", "description": ""}]},
            ],
        }
    ]
    assert _incremental_macro_parts_trustworthy(bad) is False

    ok = [
        {
            "title": "第一部",
            "volumes": [
                {"title": "卷一", "acts": [{"title": "初入山门", "description": ""}]},
            ],
        }
    ]
    assert _incremental_macro_parts_trustworthy(ok) is True


def test_try_parse_parts_from_llm_buffer_accepts_complete_valid_minimal_json():
    raw = '{"parts": [{"title": "第一部", "volumes": [{"title": "卷一", "acts": [{"title": "序幕", "description": ""}]}]}], "theme": "x"}'
    parts = _try_parse_parts_from_llm_buffer(raw)
    assert parts is not None
    assert parts[0]["title"] == "第一部"
    assert parts[0]["volumes"][0]["acts"][0]["title"] == "序幕"


def test_act_planning_prompt_passes_locked_contract_variables_to_cpms(monkeypatch):
    prefs = GenerationPreferences(
        locked_genre="综合言情 / 都市言情·甜宠",
        locked_world_preset="现代都市关系场域",
        locked_special_requirements="核心回报是亲密关系推进。",
    )
    novel_repo = Mock()
    novel_repo.get_by_id.return_value = Mock(generation_prefs=prefs)
    svc = ContinuousPlanningService(
        story_node_repo=Mock(),
        chapter_element_repo=Mock(),
        llm_service=Mock(),
        novel_repository=novel_repo,
    )
    act_node = Mock(
        id="act-1",
        novel_id="novel-1",
        title="第一幕：纸钱与樱树",
        description="苏念把樱花看成纸钱，摩挲母亲留下的旧红绳。",
    )
    captured = {}

    def fake_render(_contract, variables):
        captured.update(variables)
        return Prompt(system="system", user=str(variables))

    monkeypatch.setattr(
        ContinuousPlanningService,
        "_render_contract_prompt",
        staticmethod(fake_render),
    )

    prompt = svc._build_act_planning_prompt(
        act_node=act_node,
        bible_context={},
        previous_summary=None,
        chapter_count=6,
    )

    assert captured["genre_label"] == "综合言情 / 都市言情·甜宠"
    assert captured["world_preset"] == "现代都市关系场域"
    assert captured["special_requirements"] == "核心回报是亲密关系推进。"
    assert "漂移禁令" not in captured["context"]
    assert "禁止把" not in captured["context"]
    assert prompt.user == str(captured)


@pytest.mark.asyncio
async def test_stream_macro_llm_text_repairs_partial_text_when_stream_breaks():
    async def broken_stream(_prompt, _config):
        yield """{
          "parts": [
            {
              "title": "第一部",
              "volumes": [
                {
                  "title": "第一卷",
                  "acts": [
                    {
                      "title": "第一幕：灰鸦街醒来",
                      "description": "主角重生后先在灰鸦街求生",
                      "core_conflict": "隐藏异常并取得训练资格"
                    },
                    {
                      "title": "第二幕：廉价训练舱",
                      "description": "主角争夺第一批训练资源"
        """
        raise RuntimeError("peer closed connection")

    svc = _make_service()
    svc.llm_service.stream_generate = broken_stream

    raw = await svc._stream_macro_llm_text("novel-1", Mock(), Mock())
    parsed = svc._parse_llm_response(raw)

    acts = parsed["parts"][0]["volumes"][0]["acts"]
    assert acts[0]["title"] == "第一幕：灰鸦街醒来"
    assert acts[1]["title"] == "第二幕：廉价训练舱"


@pytest.mark.asyncio
async def test_generate_macro_plan_precise_mode_repairs_missing_act_fields_and_rebalances_chapters():
    llm_service = AsyncMock()
    llm_service.generate = AsyncMock(side_effect=[
        """{
          "node_updates": [
            {"node_id": "P1", "title": "寒门燃灯", "description": "寒门少年被卷入京师风暴"},
            {"node_id": "V1_1", "title": "初入京城", "description": "立足与试探"},
            {
              "node_id": "A1_1_1",
              "title": "雪夜叩门",
              "description": "主角深夜入京，撞见命案",
              "estimated_chapters": 7,
              "plot_points": ["进京", "撞见命案"],
              "setup_for": ["A1_1_2"],
              "payoff_from": []
            },
            {
              "node_id": "A1_1_2",
              "title": "朝堂余烬",
              "description": "主角被迫接触权贵",
              "estimated_chapters": 9,
              "narrative_goal": "建立主角与朝堂的冲突面",
              "plot_points": ["见权贵", "受逼迫"],
              "key_characters": ["主角-棋子"],
              "key_locations": ["都察院-压力源"],
              "emotional_arc": "紧张→压抑",
              "setup_for": ["A1_1_3"],
              "payoff_from": ["A1_1_1"]
            }
          ]
        }""",
        """{
          "node_updates": [
            {
              "node_id": "A1_1_1",
              "narrative_goal": "把主角拖入主线阴谋",
              "key_characters": ["主角-闯入者"],
              "key_locations": ["京城-漩涡入口"],
              "emotional_arc": "戒备→惊惧"
            }
          ]
        }""",
    ])
    svc = ContinuousPlanningService(
        story_node_repo=Mock(),
        chapter_element_repo=Mock(),
        llm_service=llm_service,
    )
    svc._get_bible_context = Mock(return_value={})

    result = await svc.generate_macro_plan(
        novel_id="novel-1",
        target_chapters=100,
        structure_preference={"parts": 1, "volumes_per_part": 5, "acts_per_volume": 5},
    )

    parts = result["structure"]
    assert len(parts) == 1
    assert len(parts[0]["volumes"]) == 5
    assert all(len(volume["acts"]) == 5 for volume in parts[0]["volumes"])

    first_volume = parts[0]["volumes"][0]
    assert parts[0]["title"] == "寒门燃灯"
    assert first_volume["title"] == "初入京城"
    assert first_volume["acts"][0]["title"] == "雪夜叩门"
    assert first_volume["acts"][1]["title"] == "朝堂余烬"
    assert first_volume["acts"][2]["title"] == "第3幕"
    assert first_volume["acts"][0]["narrative_goal"] == "把主角拖入主线阴谋"
    assert first_volume["acts"][0]["key_characters"] == ["主角-闯入者"]
    assert first_volume["acts"][0]["key_locations"] == ["京城-漩涡入口"]
    assert first_volume["acts"][0]["emotional_arc"] == "戒备→惊惧"

    all_acts = [act for volume in parts[0]["volumes"] for act in volume["acts"]]
    assert sum(act["estimated_chapters"] for act in all_acts) == 100
    assert llm_service.generate.await_count == 2

    progress = get_macro_plan_progress("novel-1")
    assert progress["status"] == "completed"
    assert progress["current"] == 5
    assert progress["total"] == 5


@pytest.mark.asyncio
async def test_confirm_act_planning_rejects_chapters_beyond_novel_target_before_mutation():
    act = _story_node("act-1", NodeType.ACT, 1, parent_id="volume-1")
    existing = _story_node("chapter-3", NodeType.CHAPTER, 3, parent_id="act-other")
    story_repo = SimpleNamespace(
        get_by_id=AsyncMock(return_value=act),
        get_children_sync=Mock(return_value=[]),
        get_by_novel_sync=Mock(return_value=[act, existing]),
        delete=AsyncMock(),
        save_batch=AsyncMock(),
        update=AsyncMock(),
    )
    service = ContinuousPlanningService(
        story_node_repo=story_repo,
        chapter_element_repo=SimpleNamespace(
            delete_by_chapter=AsyncMock(),
            save_batch=AsyncMock(),
        ),
        chapter_repository=None,
        novel_repository=SimpleNamespace(
            get_by_id=Mock(return_value=SimpleNamespace(target_chapters=3))
        ),
        llm_service=Mock(),
    )
    service._write_chapter_plan_variables = Mock()
    chapters = [
        {
            "number": 1,
            "title": "越界一",
            "main_event": "推进冲突",
            "handoff_from_previous": "承接前章",
            "handoff_to_next": "留下悬念",
        },
        {
            "number": 2,
            "title": "越界二",
            "main_event": "推进冲突",
            "handoff_from_previous": "承接前章",
            "handoff_to_next": "留下悬念",
        },
    ]

    with pytest.raises(ValueError, match="目标章节数"):
        await service.confirm_act_planning("act-1", chapters)

    story_repo.delete.assert_not_awaited()
    story_repo.save_batch.assert_not_awaited()


@pytest.mark.asyncio
async def test_replanning_act_rejects_existing_authored_chapter_before_delete():
    """DATA-001: no act-replan path may delete a chapter that has prose."""
    act = _story_node("act-1", NodeType.ACT, 1, parent_id="volume-1")
    chapter = _story_node("chapter-1", NodeType.CHAPTER, 1, parent_id="act-1")
    chapter_repo = SimpleNamespace(
        get_by_novel_and_number=Mock(return_value=None),
        list_by_novel=Mock(
            return_value=[
                SimpleNamespace(
                    id=SimpleNamespace(value="chapter-1"),
                    number=1,
                    content="Authored body must not be removed by replanning.",
                )
            ]
        ),
        delete=Mock(),
    )
    story_repo = SimpleNamespace(
        get_children_sync=Mock(return_value=[chapter]),
        delete=AsyncMock(),
    )
    service = ContinuousPlanningService(
        story_node_repo=story_repo,
        chapter_element_repo=SimpleNamespace(delete_by_chapter=AsyncMock()),
        chapter_repository=chapter_repo,
        llm_service=Mock(),
    )

    with pytest.raises(ValueError, match="正文"):
        await service._remove_chapter_children_of_act(act.id)

    chapter_repo.delete.assert_not_called()
    story_repo.delete.assert_not_awaited()


@pytest.mark.asyncio
async def test_safe_macro_failure_does_not_call_unsafe_fallback():
    """BLUEPRINT-001: a rejected safe merge must not be written unsafely."""

    class _FailingSafeMergeHost:
        def __init__(self):
            self.unsafe_calls = 0

        async def confirm_macro_plan_safe(self, **_kwargs):
            raise RuntimeError("safe merge conflict")

        async def confirm_macro_plan(self, **_kwargs):
            self.unsafe_calls += 1
            return {"success": True}

    host = _FailingSafeMergeHost()

    with pytest.raises(RuntimeError, match="safe merge conflict"):
        await ContinuousPlanningService.persist_macro_structure_with_fallback(
            host,
            "novel-1",
            [],
        )

    assert host.unsafe_calls == 0


@pytest.mark.parametrize("title", ["description", "描述"])
def test_act_plan_validation_rejects_description_placeholder_titles(title):
    errors = validate_lightweight_act_plan(
        [
            {
                "number": 1,
                "title": title,
                "main_event": "推进冲突",
                "handoff_from_previous": "承接前章",
                "handoff_to_next": "留下悬念",
            }
        ],
        expected_count=1,
    )

    assert "placeholder" in " ".join(errors)


@pytest.mark.asyncio
async def test_create_next_act_rejects_a_full_parent_volume_before_invoking_llm():
    """BLUEPRINT-002: direct creation must not overflow a volume."""
    volume = _story_node("volume-1", NodeType.VOLUME, 1)
    current = _story_node("act-3", NodeType.ACT, 3, parent_id=volume.id)
    service, story_repo = _next_act_service(
        [
            volume,
            _story_node("act-1", NodeType.ACT, 1, parent_id=volume.id),
            _story_node("act-2", NodeType.ACT, 2, parent_id=volume.id),
            current,
        ],
        target_chapters=30,
    )

    with pytest.raises(ValueError, match="幕容量"):
        await service.create_next_act_auto("novel-1", current.id)

    assert story_repo.saved == []
    service._generate_next_act_info.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_next_act_rejects_when_planned_chapters_reach_target_before_invoking_llm():
    """BLUEPRINT-002: a new act cannot be created after target capacity is planned."""
    volume = _story_node("volume-1", NodeType.VOLUME, 1)
    current = _story_node("act-1", NodeType.ACT, 1, parent_id=volume.id)
    chapters = [
        _story_node(
            f"chapter-{number}",
            NodeType.CHAPTER,
            number,
            parent_id=current.id,
        )
        for number in range(1, 31)
    ]
    service, story_repo = _next_act_service(
        [volume, current, *chapters],
        target_chapters=30,
    )

    with pytest.raises(ValueError, match="目标章节数"):
        await service.create_next_act_auto("novel-1", current.id)

    assert story_repo.saved == []
    service._generate_next_act_info.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_next_act_rejects_current_act_without_a_volume_parent():
    """BLUEPRINT-002: a malformed parent link must not produce an orphan act."""
    part = _story_node("part-1", NodeType.PART, 1)
    current = _story_node("act-1", NodeType.ACT, 1, parent_id=part.id)
    service, story_repo = _next_act_service(
        [part, current],
        target_chapters=30,
    )

    with pytest.raises(ValueError, match="父卷"):
        await service.create_next_act_auto("novel-1", current.id)

    assert story_repo.saved == []
    service._generate_next_act_info.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_next_act_rejects_duplicate_global_act_number_before_invoking_llm():
    """BLUEPRINT-002: global act numbering drives daemon selection and stays unique."""
    volume_one = _story_node("volume-1", NodeType.VOLUME, 1)
    volume_two = _story_node("volume-2", NodeType.VOLUME, 2)
    current = _story_node("act-3", NodeType.ACT, 3, parent_id=volume_one.id)
    duplicate_next = _story_node("act-4-existing", NodeType.ACT, 4, parent_id=volume_two.id)
    service, story_repo = _next_act_service(
        [volume_one, volume_two, current, duplicate_next],
        target_chapters=80,
    )

    with pytest.raises(ValueError, match="第 4 幕"):
        await service.create_next_act_auto("novel-1", current.id)

    assert story_repo.saved == []
    service._generate_next_act_info.assert_not_awaited()


@pytest.mark.asyncio
async def test_create_next_act_creates_the_next_act_after_passing_preflight():
    """BLUEPRINT-002: valid direct creation keeps the established generation path."""
    volume = _story_node("volume-1", NodeType.VOLUME, 1)
    current = _story_node("act-1", NodeType.ACT, 1, parent_id=volume.id)
    service, story_repo = _next_act_service(
        [volume, current],
        target_chapters=30,
    )

    result = await service.create_next_act_auto("novel-1", current.id)

    assert result["success"] is True
    assert len(story_repo.saved) == 1
    assert story_repo.saved[0].parent_id == volume.id
    assert story_repo.saved[0].number == 2
    service._generate_next_act_info.assert_awaited_once()


def test_parse_llm_response_logs_safe_metadata_without_raw_planning_content(
    caplog,
    monkeypatch,
):
    """OBS-001: terminal planning parse errors must not disclose model output."""
    service = _make_service()
    marker = "TRACE_PRIVATE_PLANNING_MARKER"
    malformed = f'{{"secret":"{marker}", "parts": [}}'
    monkeypatch.setattr(continuous_planning_module, "repair_json", lambda value: value)
    monkeypatch.setattr(
        continuous_planning_module,
        "_repair_json_string",
        lambda value: value,
    )

    with caplog.at_level(logging.ERROR, logger=continuous_planning_module.__name__):
        with pytest.raises(json.JSONDecodeError):
            service._parse_llm_response(malformed)

    assert marker not in caplog.text
    assert "content_length=" in caplog.text
    assert "content_sha256=" in caplog.text
    assert "line=1" in caplog.text
