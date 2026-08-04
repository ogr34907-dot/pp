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
    volume_1 = _node("volume-1", NodeType.VOLUME, 1, parent_id="part-1")
    volume_2 = _node("volume-2", NodeType.VOLUME, 2, parent_id="part-1")
    acts = [
        _node(f"act-{number}", NodeType.ACT, number, parent_id="volume-1")
        for number in range(1, 4)
    ] + [
        _node(f"act-{number}", NodeType.ACT, number, parent_id="volume-2")
        for number in range(4, 7)
    ]
    repo = _StoryNodeRepo([volume_1, volume_2, *acts], {})

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
    assert next(node for node in repo.nodes if node.id == "act-7").parent_id == created_volume.id


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
