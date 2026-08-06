from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from domain.novel.entities.novel import AutopilotStatus, NovelStage
from domain.novel.value_objects.novel_id import NovelId
from domain.structure.story_node import NodeType, StoryNode
from engine.runtime.act_planning_delegate import run_act_planning
from engine.runtime.daemon_host import DaemonHostMixin


class _StoryNodeRepo:
    def __init__(self, nodes, children):
        self.nodes = list(nodes)
        self.children = children
        self.saved = []

    async def get_by_novel(self, _novel_id):
        return list(self.nodes)

    async def save(self, node):
        self.nodes.append(node)
        self.saved.append(node)

    def get_children_sync(self, node_id):
        return list(self.children.get(node_id, []))


def _node(node_id, node_type, number, *, parent_id=None):
    return StoryNode(
        id=node_id,
        novel_id="novel-1",
        node_type=node_type,
        number=number,
        title=node_id,
        parent_id=parent_id,
        order_index=number,
    )


@pytest.mark.asyncio
async def test_act_planning_creates_next_volume_when_every_existing_volume_is_full():
    part = _node("part-1", NodeType.PART, 1)
    part.suggested_chapter_count = 20
    volume_1 = _node("volume-1", NodeType.VOLUME, 1, parent_id="part-1")
    volume_2 = _node("volume-2", NodeType.VOLUME, 2, parent_id="part-1")
    acts = [
        _node(f"act-{number}", NodeType.ACT, number, parent_id="volume-1")
        for number in range(1, 4)
    ] + [
        _node(f"act-{number}", NodeType.ACT, number, parent_id="volume-2")
        for number in range(4, 7)
    ]
    for act in acts:
        act.chapter_count = 2
        act.suggested_chapter_count = 9
    repo = _StoryNodeRepo([part, volume_1, volume_2, *acts], {})

    async def create_next_act_auto(*, novel_id, current_act_id, parent_volume_id=None):
        assert novel_id == "novel-1"
        assert current_act_id == "act-6"
        assert parent_volume_id == "volume-novel-1-3"
        next_act = _node("act-7", NodeType.ACT, 7, parent_id=parent_volume_id)
        repo.nodes.append(next_act)
        repo.children[next_act.id] = [
            _node("chapter-7", NodeType.CHAPTER, 7, parent_id=next_act.id)
        ]
        return {"success": True, "next_act": next_act.to_dict()}

    planning_service = SimpleNamespace(create_next_act_auto=create_next_act_auto)
    novel = SimpleNamespace(
        novel_id=NovelId("novel-1"),
        current_act=6,
        current_auto_chapters=6,
        target_chapters=20,
        current_stage=NovelStage.ACT_PLANNING,
        autopilot_status=AutopilotStatus.RUNNING,
        auto_approve_mode=True,
    )
    host = SimpleNamespace(
        story_node_repo=repo,
        planning_service=planning_service,
        _is_still_running=lambda _novel: True,
        _update_shared_state=MagicMock(),
        _flush_novel=MagicMock(),
    )
    host._find_parent_volume_for_new_act = lambda **kwargs: (
        DaemonHostMixin._find_parent_volume_for_new_act(host, **kwargs)
    )

    await run_act_planning(host, novel)

    created_volume = next(node for node in repo.saved if node.node_type == NodeType.VOLUME)
    assert created_volume.number == 3
    assert created_volume.parent_id == "part-1"
    assert created_volume.suggested_chapter_count == 8
    assert part.suggested_chapter_count == 20
    assert part not in repo.saved
    assert next(node for node in repo.nodes if node.id == "act-7").parent_id == created_volume.id


@pytest.mark.asyncio
async def test_act_planning_stops_without_creating_volume_when_act_reservations_exhaust_target(monkeypatch):
    volume_1 = _node("volume-1", NodeType.VOLUME, 1, parent_id="part-1")
    volume_2 = _node("volume-2", NodeType.VOLUME, 2, parent_id="part-1")
    acts = [
        _node(f"act-{number}", NodeType.ACT, number, parent_id="volume-1")
        for number in range(1, 4)
    ] + [
        _node(f"act-{number}", NodeType.ACT, number, parent_id="volume-2")
        for number in range(4, 7)
    ]
    for act in acts:
        act.suggested_chapter_count = 1
    repo = _StoryNodeRepo([volume_1, volume_2, *acts], {})
    request = AsyncMock()
    monkeypatch.setattr("engine.runtime.act_planning_delegate._request_act_invocation", request)
    novel = SimpleNamespace(
        novel_id=NovelId("novel-1"),
        current_act=6,
        current_auto_chapters=0,
        target_chapters=6,
        current_stage=NovelStage.ACT_PLANNING,
        autopilot_status=AutopilotStatus.RUNNING,
        auto_approve_mode=True,
        consecutive_error_count=0,
    )
    host = SimpleNamespace(
        story_node_repo=repo,
        planning_service=SimpleNamespace(create_next_act_auto=AsyncMock()),
        _is_still_running=lambda _novel: True,
        _update_shared_state=MagicMock(),
        _flush_novel=MagicMock(),
    )
    host._find_parent_volume_for_new_act = lambda **kwargs: (
        DaemonHostMixin._find_parent_volume_for_new_act(host, **kwargs)
    )

    await run_act_planning(host, novel)

    assert not repo.saved
    assert novel.current_stage == NovelStage.PAUSED_FOR_REVIEW
    assert novel.autopilot_status == AutopilotStatus.STOPPED
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_continuation_volume_uses_the_smaller_of_planned_and_reserved_capacity():
    part = _node("part-1", NodeType.PART, 1)
    volume_1 = _node("volume-1", NodeType.VOLUME, 1, parent_id=part.id)
    volume_2 = _node("volume-2", NodeType.VOLUME, 2, parent_id=part.id)
    acts = [
        _node(f"act-{number}", NodeType.ACT, number, parent_id=volume_1.id)
        for number in range(1, 4)
    ] + [
        _node(f"act-{number}", NodeType.ACT, number, parent_id=volume_2.id)
        for number in range(4, 7)
    ]
    for act in acts:
        act.suggested_chapter_count = 1
    chapters = [
        _node(f"chapter-{number}", NodeType.CHAPTER, number, parent_id=acts[0].id)
        for number in range(1, 9)
    ]
    repo = _StoryNodeRepo([part, volume_1, volume_2, *acts, *chapters], {})

    async def create_next_act_auto(*, parent_volume_id, **_kwargs):
        next_act = _node("act-7", NodeType.ACT, 7, parent_id=parent_volume_id)
        repo.nodes.append(next_act)
        repo.children[next_act.id] = [
            _node("chapter-9", NodeType.CHAPTER, 9, parent_id=next_act.id)
        ]
        return {"success": True}

    novel = SimpleNamespace(
        novel_id=NovelId("novel-1"),
        current_act=6,
        current_auto_chapters=8,
        target_chapters=10,
        current_stage=NovelStage.ACT_PLANNING,
        autopilot_status=AutopilotStatus.RUNNING,
        auto_approve_mode=True,
    )
    host = SimpleNamespace(
        story_node_repo=repo,
        planning_service=SimpleNamespace(create_next_act_auto=create_next_act_auto),
        _is_still_running=lambda _novel: True,
        _update_shared_state=MagicMock(),
        _flush_novel=MagicMock(),
    )
    host._find_parent_volume_for_new_act = lambda **kwargs: (
        DaemonHostMixin._find_parent_volume_for_new_act(host, **kwargs)
    )

    await run_act_planning(host, novel)

    continuation_volume = next(
        node for node in repo.saved if node.node_type == NodeType.VOLUME
    )
    assert continuation_volume.suggested_chapter_count == 2


@pytest.mark.asyncio
async def test_continuation_volume_is_limited_by_parent_part_remaining_capacity():
    part = _node("part-1", NodeType.PART, 1)
    part.suggested_chapter_count = 7
    volume = _node("volume-1", NodeType.VOLUME, 1, parent_id=part.id)
    volume.suggested_chapter_count = 6
    acts = [
        _node(f"act-{number}", NodeType.ACT, number, parent_id=volume.id)
        for number in range(1, 21)
    ]
    repo = _StoryNodeRepo([part, volume, *acts], {})

    async def create_next_act_auto(*, parent_volume_id, **_kwargs):
        next_act = _node("act-21", NodeType.ACT, 21, parent_id=parent_volume_id)
        repo.nodes.append(next_act)
        repo.children[next_act.id] = [
            _node("chapter-1", NodeType.CHAPTER, 1, parent_id=next_act.id)
        ]
        return {"success": True}

    novel = SimpleNamespace(
        novel_id=NovelId("novel-1"),
        current_act=20,
        current_auto_chapters=0,
        target_chapters=10,
        current_stage=NovelStage.ACT_PLANNING,
        autopilot_status=AutopilotStatus.RUNNING,
        auto_approve_mode=True,
    )
    host = SimpleNamespace(
        story_node_repo=repo,
        planning_service=SimpleNamespace(create_next_act_auto=create_next_act_auto),
        _is_still_running=lambda _novel: True,
        _update_shared_state=MagicMock(),
        _flush_novel=MagicMock(),
    )
    host._find_parent_volume_for_new_act = lambda **_kwargs: None

    await run_act_planning(host, novel)

    continuation_volume = next(node for node in repo.saved if node.node_type == NodeType.VOLUME)
    assert continuation_volume.suggested_chapter_count == 1
    assert part.suggested_chapter_count == 7


@pytest.mark.asyncio
async def test_continuation_volume_counts_persisted_volume_capacity_against_novel_target():
    part = _node("part-1", NodeType.PART, 1)
    part.suggested_chapter_count = 10
    volume = _node("volume-1", NodeType.VOLUME, 1, parent_id=part.id)
    volume.suggested_chapter_count = 6
    acts = [
        _node(f"act-{number}", NodeType.ACT, number, parent_id=volume.id)
        for number in range(1, 21)
    ]
    repo = _StoryNodeRepo([part, volume, *acts], {})

    async def create_next_act_auto(*, parent_volume_id, **_kwargs):
        next_act = _node("act-21", NodeType.ACT, 21, parent_id=parent_volume_id)
        repo.nodes.append(next_act)
        repo.children[next_act.id] = [
            _node("chapter-1", NodeType.CHAPTER, 1, parent_id=next_act.id)
        ]
        return {"success": True}

    novel = SimpleNamespace(
        novel_id=NovelId("novel-1"),
        current_act=20,
        current_auto_chapters=0,
        target_chapters=10,
        current_stage=NovelStage.ACT_PLANNING,
        autopilot_status=AutopilotStatus.RUNNING,
        auto_approve_mode=True,
    )
    host = SimpleNamespace(
        story_node_repo=repo,
        planning_service=SimpleNamespace(create_next_act_auto=create_next_act_auto),
        _is_still_running=lambda _novel: True,
        _update_shared_state=MagicMock(),
        _flush_novel=MagicMock(),
    )
    host._find_parent_volume_for_new_act = lambda **_kwargs: None

    await run_act_planning(host, novel)

    continuation_volume = next(node for node in repo.saved if node.node_type == NodeType.VOLUME)
    assert continuation_volume.suggested_chapter_count == 4
    assert part.suggested_chapter_count == 10


@pytest.mark.asyncio
async def test_act_planning_pauses_before_llm_when_parent_volume_capacity_is_fully_reserved(monkeypatch):
    volume = _node("volume-1", NodeType.VOLUME, 1, parent_id="part-1")
    volume.suggested_chapter_count = 3
    completed_act = _node("act-1", NodeType.ACT, 1, parent_id=volume.id)
    completed_act.chapter_count = 3
    target_act = _node("act-2", NodeType.ACT, 2, parent_id=volume.id)
    target_act.suggested_chapter_count = 3
    repo = _StoryNodeRepo([volume, completed_act, target_act], {target_act.id: []})
    request = AsyncMock()
    monkeypatch.setattr("engine.runtime.act_planning_delegate._request_act_invocation", request)
    novel = SimpleNamespace(
        novel_id=NovelId("novel-1"),
        current_act=1,
        current_auto_chapters=3,
        target_chapters=20,
        current_stage=NovelStage.ACT_PLANNING,
        autopilot_status=AutopilotStatus.RUNNING,
        auto_approve_mode=True,
    )
    host = SimpleNamespace(
        story_node_repo=repo,
        planning_service=SimpleNamespace(confirm_act_planning=AsyncMock()),
        _is_still_running=lambda _novel: True,
        _update_shared_state=MagicMock(),
        _flush_novel=MagicMock(),
    )

    await run_act_planning(host, novel)

    assert novel.current_stage == NovelStage.PAUSED_FOR_REVIEW
    assert novel.autopilot_status == AutopilotStatus.STOPPED
    request.assert_not_awaited()


@pytest.mark.asyncio
async def test_act_planning_limits_llm_budget_to_remaining_target_capacity(monkeypatch):
    volume = _node("volume-1", NodeType.VOLUME, 1, parent_id="part-1")
    act = _node("act-1", NodeType.ACT, 1, parent_id=volume.id)
    act.suggested_chapter_count = 3
    existing = _node("chapter-2", NodeType.CHAPTER, 2, parent_id="act-previous")
    planned = _node("chapter-3", NodeType.CHAPTER, 3, parent_id=act.id)
    repo = _StoryNodeRepo([volume, act, existing], {act.id: []})
    calls = {"children": 0}

    def get_children_sync(node_id):
        calls["children"] += 1
        return [] if calls["children"] == 1 else [planned]

    repo.get_children_sync = get_children_sync
    request = AsyncMock(
        return_value=SimpleNamespace(
            status="completed",
            payload={
                "commit": SimpleNamespace(
                    result={
                        "continuation": {
                            "act_plan": {
                                "chapters": [
                                    {
                                        "number": 1,
                                        "title": "最后一章",
                                        "main_event": "收束冲突",
                                        "handoff_from_previous": "承接前章",
                                        "handoff_to_next": "完成目标",
                                    }
                                ]
                            }
                        }
                    }
                )
            },
        )
    )
    monkeypatch.setattr(
        "engine.runtime.act_planning_delegate._consume_pending_act_plan",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "engine.runtime.act_planning_delegate._read_shared_state",
        lambda _novel_id: {},
    )
    monkeypatch.setattr(
        "engine.runtime.act_planning_delegate._request_act_invocation",
        request,
    )
    novel = SimpleNamespace(
        novel_id=NovelId("novel-1"),
        current_act=0,
        current_auto_chapters=2,
        target_chapters=3,
        current_stage=NovelStage.ACT_PLANNING,
        autopilot_status=AutopilotStatus.RUNNING,
        auto_approve_mode=True,
    )
    host = SimpleNamespace(
        story_node_repo=repo,
        planning_service=SimpleNamespace(confirm_act_planning=AsyncMock()),
        _is_still_running=lambda _novel: True,
        _update_shared_state=MagicMock(),
        _flush_novel=MagicMock(),
    )

    await run_act_planning(host, novel)

    assert request.await_args.kwargs["chapter_budget"] == 1


@pytest.mark.asyncio
async def test_act_planning_limits_llm_budget_to_remaining_parent_volume_capacity(monkeypatch):
    """PLANNING-CAPACITY-006: daemon planning must not overbook a parent volume."""
    volume = _node("volume-1", NodeType.VOLUME, 1, parent_id="part-1")
    volume.suggested_chapter_count = 8
    completed_act = _node("act-1", NodeType.ACT, 1, parent_id=volume.id)
    completed_act.chapter_count = 6
    current_act = _node("act-2", NodeType.ACT, 2, parent_id=volume.id)
    current_act.suggested_chapter_count = 6
    repo = _StoryNodeRepo([volume, completed_act, current_act], {current_act.id: []})
    request = AsyncMock(
        return_value=SimpleNamespace(
            status="completed",
            payload={
                "commit": SimpleNamespace(
                    result={
                        "continuation": {
                            "act_plan": {
                                "chapters": [
                                    {
                                        "number": 1,
                                        "title": "卷末收束",
                                        "main_event": "完成当前卷约定",
                                        "handoff_from_previous": "承接前幕",
                                        "handoff_to_next": "转入下一卷",
                                    }
                                ]
                            }
                        }
                    }
                )
            },
        )
    )
    monkeypatch.setattr(
        "engine.runtime.act_planning_delegate._consume_pending_act_plan",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "engine.runtime.act_planning_delegate._read_shared_state",
        lambda _novel_id: {},
    )
    monkeypatch.setattr(
        "engine.runtime.act_planning_delegate._request_act_invocation",
        request,
    )
    novel = SimpleNamespace(
        novel_id=NovelId("novel-1"),
        current_act=1,
        current_auto_chapters=6,
        target_chapters=100,
        current_stage=NovelStage.ACT_PLANNING,
        autopilot_status=AutopilotStatus.RUNNING,
        auto_approve_mode=True,
    )
    host = SimpleNamespace(
        story_node_repo=repo,
        planning_service=SimpleNamespace(confirm_act_planning=AsyncMock()),
        _is_still_running=lambda _novel: True,
        _update_shared_state=MagicMock(),
        _flush_novel=MagicMock(),
    )

    await run_act_planning(host, novel)

    assert request.await_args.kwargs["chapter_budget"] == 2


@pytest.mark.asyncio
async def test_act_planning_pauses_after_third_structured_persistence_failure(monkeypatch):
    volume = _node("volume-1", NodeType.VOLUME, 1, parent_id="part-1")
    act = _node("act-1", NodeType.ACT, 1, parent_id=volume.id)
    act.suggested_chapter_count = 1
    repo = _StoryNodeRepo([volume, act], {act.id: []})
    monkeypatch.setattr(
        "engine.runtime.act_planning_delegate._consume_pending_act_plan",
        lambda *_args, **_kwargs: {
            "chapters": [
                {
                    "number": 1,
                    "title": "合法计划",
                    "main_event": "推进冲突",
                    "handoff_from_previous": "承接前章",
                    "handoff_to_next": "留下悬念",
                }
            ]
        },
    )
    novel = SimpleNamespace(
        novel_id=NovelId("novel-1"),
        current_act=0,
        current_auto_chapters=0,
        target_chapters=3,
        current_stage=NovelStage.ACT_PLANNING,
        autopilot_status=AutopilotStatus.RUNNING,
        auto_approve_mode=True,
        consecutive_error_count=2,
    )
    host = SimpleNamespace(
        story_node_repo=repo,
        planning_service=SimpleNamespace(
            confirm_act_planning=AsyncMock(side_effect=ValueError("invalid structure"))
        ),
        _is_still_running=lambda _novel: True,
        _update_shared_state=MagicMock(),
        _flush_novel=MagicMock(),
    )

    await run_act_planning(host, novel)

    assert novel.autopilot_status == AutopilotStatus.ERROR
    assert novel.current_stage == NovelStage.PAUSED_FOR_REVIEW
    host._flush_novel.assert_called_once_with(novel)


def test_parent_volume_selection_respects_persisted_volume_capacity():
    """PLANNING-CAPACITY-004: a full volume cannot receive another act."""
    volume_1 = _node("volume-1", NodeType.VOLUME, 1, parent_id="part-1")
    volume_1.suggested_chapter_count = 3
    volume_2 = _node("volume-2", NodeType.VOLUME, 2, parent_id="part-1")
    volume_2.suggested_chapter_count = 5
    first_act = _node("act-1", NodeType.ACT, 1, parent_id=volume_1.id)
    first_act.suggested_chapter_count = 3

    parent = DaemonHostMixin._find_parent_volume_for_new_act(
        SimpleNamespace(),
        volume_nodes=[volume_1, volume_2],
        act_nodes=[first_act],
        current_auto_chapters=3,
        target_chapters=8,
        rec_acts_per_volume=3,
        novel_id="novel-1",
    )

    assert parent is volume_2


def test_parent_volume_selection_uses_remaining_capacity_after_recommended_act_count():
    """PLANNING-CAPACITY-008: recommendation count cannot strand usable volume capacity."""
    volume = _node("volume-1", NodeType.VOLUME, 1, parent_id="part-1")
    volume.suggested_chapter_count = 8
    acts = [
        _node(f"act-{number}", NodeType.ACT, number, parent_id=volume.id)
        for number in (1, 2)
    ]
    for act in acts:
        act.suggested_chapter_count = 3

    parent = DaemonHostMixin._find_parent_volume_for_new_act(
        SimpleNamespace(),
        volume_nodes=[volume],
        act_nodes=acts,
        current_auto_chapters=0,
        target_chapters=20,
        rec_acts_per_volume=2,
        novel_id="novel-1",
    )

    assert parent is volume


@pytest.mark.asyncio
async def test_first_act_inherits_parent_contract_and_volume_capacity(monkeypatch):
    """PLANNING-STRUCTURE-002: the generated first act must carry its parent promise."""
    part = _node("part-1", NodeType.PART, 1)
    part.title = "安丰塘前"
    part.description = "本部先解决安丰塘的春汛与修堤危机。"
    volume = _node("volume-1", NodeType.VOLUME, 1, parent_id=part.id)
    volume.title = "春汛决堤"
    volume.description = "公开记账、按工发粮，并在汛期前堵住管涌。"
    volume.suggested_chapter_count = 3
    repo = _StoryNodeRepo([part, volume], {})

    monkeypatch.setattr(
        "engine.runtime.act_planning_delegate._consume_pending_act_plan",
        lambda *_args, **_kwargs: None,
    )
    monkeypatch.setattr(
        "engine.runtime.act_planning_delegate._read_shared_state",
        lambda _novel_id: {},
    )
    monkeypatch.setattr(
        "engine.runtime.act_planning_delegate._request_act_invocation",
        AsyncMock(return_value=SimpleNamespace(status="completed", payload={})),
    )
    running = iter([True, False])
    novel = SimpleNamespace(
        novel_id=NovelId("novel-1"),
        current_act=0,
        current_auto_chapters=0,
        target_chapters=20,
        current_stage=NovelStage.ACT_PLANNING,
        autopilot_status=AutopilotStatus.RUNNING,
        auto_approve_mode=True,
    )
    host = SimpleNamespace(
        story_node_repo=repo,
        planning_service=SimpleNamespace(confirm_act_planning=AsyncMock()),
        _is_still_running=lambda _novel: next(running),
        _update_shared_state=MagicMock(),
        _flush_novel=MagicMock(),
    )
    host._find_parent_volume_for_new_act = lambda **kwargs: (
        DaemonHostMixin._find_parent_volume_for_new_act(host, **kwargs)
    )

    await run_act_planning(host, novel)

    first_act = next(node for node in repo.saved if node.node_type == NodeType.ACT)
    assert first_act.suggested_chapter_count == 3
    assert first_act.order_index > volume.order_index
    for marker in (
        "安丰塘前",
        "本部先解决安丰塘的春汛与修堤危机。",
        "春汛决堤",
        "公开记账、按工发粮，并在汛期前堵住管涌。",
    ):
        assert marker in first_act.description
