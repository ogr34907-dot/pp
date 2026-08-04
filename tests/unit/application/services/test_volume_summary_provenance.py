import hashlib
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from application.blueprint.services.volume_summary_service import VolumeSummaryService
from domain.structure.story_node import NodeType, StoryNode


def _node(
    node_id: str,
    node_type: NodeType,
    number: int,
    *,
    parent_id: str | None = None,
    chapter_start: int | None = None,
    chapter_end: int | None = None,
) -> StoryNode:
    return StoryNode(
        id=node_id,
        novel_id="novel-1",
        node_type=node_type,
        number=number,
        title=node_id,
        order_index=number,
        parent_id=parent_id,
        chapter_start=chapter_start,
        chapter_end=chapter_end,
    )


def _source_version(*chapters: SimpleNamespace) -> str:
    payload = "|".join(
        f"{chapter.number}:{chapter.content_sha256}:{chapter.content_revision}"
        for chapter in sorted(chapters, key=lambda item: item.number)
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


class _NodeRepository:
    def __init__(self, nodes: list[StoryNode]):
        self.nodes = nodes
        self.updated = []

    async def get_by_id(self, node_id: str):
        return next((node for node in self.nodes if node.id == node_id), None)

    def get_children_sync(self, parent_id: str):
        return [node for node in self.nodes if node.parent_id == parent_id]

    async def get_by_novel(self, _novel_id: str):
        return list(self.nodes)

    def get_by_novel_sync(self, _novel_id: str):
        return list(self.nodes)

    async def update(self, node: StoryNode):
        self.updated.append(node)
        return node


class _ChapterRepository:
    def __init__(self, chapters: list[SimpleNamespace]):
        self.chapters = chapters

    def get_by_id(self, chapter_id):
        value = getattr(chapter_id, "value", chapter_id)
        return next((chapter for chapter in self.chapters if chapter.id == value), None)

    def list_by_novel(self, _novel_id):
        return list(self.chapters)


@pytest.mark.asyncio
async def test_act_summary_persists_committed_source_provenance():
    act = _node("act-1", NodeType.ACT, 1, chapter_start=1, chapter_end=1)
    chapter_node = _node("chapter-1", NodeType.CHAPTER, 1, parent_id="act-1")
    chapter = SimpleNamespace(
        id="chapter-1",
        number=1,
        title="第一章",
        content="林澈在钟楼发现暗门。",
        content_sha256="chapter-hash-1",
        content_revision=1,
    )
    nodes = _NodeRepository([act, chapter_node])
    service = VolumeSummaryService(
        llm_service=SimpleNamespace(
            generate=AsyncMock(return_value=SimpleNamespace(content="幕摘要正文"))
        ),
        story_node_repository=nodes,
        chapter_repository=_ChapterRepository([chapter]),
    )

    result = await service.generate_act_summary("novel-1", "act-1")

    assert result.success is True
    assert act.metadata["summary"] == "幕摘要正文"
    assert act.metadata["summary_state"] == {
        "status": "committed",
        "chapter_start": 1,
        "chapter_end": 1,
        "source_version": _source_version(chapter),
        "pipeline_version": "node-summary/v1",
    }
    assert nodes.updated == [act]


@pytest.mark.asyncio
async def test_volume_summary_rebuilds_stale_act_summary_before_reducing():
    volume = _node("volume-1", NodeType.VOLUME, 1, chapter_start=1, chapter_end=1)
    act = _node(
        "act-1",
        NodeType.ACT,
        1,
        parent_id="volume-1",
        chapter_start=1,
        chapter_end=1,
    )
    act.metadata = {
        "summary": "不应被卷摘要复用的旧幕摘要",
        "summary_state": {"status": "stale"},
    }
    chapter_node = _node("chapter-1", NodeType.CHAPTER, 1, parent_id="act-1")
    chapter = SimpleNamespace(
        id="chapter-1",
        number=1,
        title="第一章",
        content="林澈在钟楼发现暗门。",
        content_sha256="chapter-hash-1",
        content_revision=1,
    )
    nodes = _NodeRepository([volume, act, chapter_node])
    service = VolumeSummaryService(
        llm_service=SimpleNamespace(
            generate=AsyncMock(
                side_effect=[
                    SimpleNamespace(content="重建后的幕摘要"),
                    SimpleNamespace(content="基于当前正文的新卷摘要"),
                ]
            )
        ),
        story_node_repository=nodes,
        chapter_repository=_ChapterRepository([chapter]),
    )

    result = await service.generate_volume_summary("novel-1", 1)

    assert result.success is True
    assert act.metadata["summary"] == "重建后的幕摘要"
    assert volume.metadata["summary"] == "基于当前正文的新卷摘要"
    assert nodes.updated == [act, volume]


def test_summary_accessors_hide_stale_node_summaries():
    volume = _node("volume-1", NodeType.VOLUME, 1)
    act = _node("act-1", NodeType.ACT, 1)
    volume.metadata = {
        "summary": "过期卷摘要",
        "summary_state": {"status": "stale"},
    }
    act.metadata = {
        "summary": "过期幕摘要",
        "summary_state": {"status": "stale"},
    }
    service = VolumeSummaryService(
        llm_service=SimpleNamespace(),
        story_node_repository=_NodeRepository([volume, act]),
    )

    assert service.get_volume_summary("novel-1", 1) is None
    assert service.get_act_summary("novel-1", 1) is None


@pytest.mark.asyncio
async def test_checkpoint_summary_is_persisted_on_current_chapter_node():
    checkpoint_node = _node("chapter-20", NodeType.CHAPTER, 20, parent_id="act-4")
    chapter = SimpleNamespace(
        id="chapter-20",
        number=20,
        title="第二十章",
        content="钟楼暗门终于打开。",
        content_sha256="chapter-hash-20",
        content_revision=4,
    )
    nodes = _NodeRepository([checkpoint_node])
    service = VolumeSummaryService(
        llm_service=SimpleNamespace(
            generate=AsyncMock(return_value=SimpleNamespace(content="检查点摘要正文"))
        ),
        story_node_repository=nodes,
        chapter_repository=_ChapterRepository([chapter]),
    )

    result = await service.generate_checkpoint_summary("novel-1", 20)

    assert result.success is True
    assert checkpoint_node.metadata["checkpoint_summary"] == "检查点摘要正文"
    assert checkpoint_node.metadata["checkpoint_summary_state"] == {
        "status": "committed",
        "chapter_start": 20,
        "chapter_end": 20,
        "source_version": _source_version(chapter),
        "pipeline_version": "node-summary/v1",
    }
    assert nodes.updated == [checkpoint_node]
