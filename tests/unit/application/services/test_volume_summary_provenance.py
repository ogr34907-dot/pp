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
        self.runtime_updates = []

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

    def update_runtime_fields(self, node_id: str, *, runtime_metadata=None, **_kwargs):
        node = next(node for node in self.nodes if node.id == node_id)
        node.metadata = node.metadata or {}
        for key, value in (runtime_metadata or {}).items():
            if value is None:
                node.metadata.pop(key, None)
            else:
                node.metadata[key] = value
        self.runtime_updates.append((node_id, dict(runtime_metadata or {})))
        return True


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
    assert act.metadata["runtime.summary"] == "幕摘要正文"
    assert act.metadata["runtime.summary_state"] == {
        "status": "committed",
        "chapter_start": 1,
        "chapter_end": 1,
        "source_chapter_numbers": [1],
        "source_version": _source_version(chapter),
        "pipeline_version": "node-summary/v1",
    }
    assert "summary" not in act.metadata
    assert nodes.updated == []
    assert nodes.runtime_updates[0][0] == act.id


@pytest.mark.asyncio
async def test_fresh_runtime_summary_clears_rewrite_invalidation_marker():
    act = _node("act-1", NodeType.ACT, 1, chapter_start=1, chapter_end=1)
    act.metadata = {"runtime.summary_invalidated_from_chapter": 1}
    chapter_node = _node("chapter-1", NodeType.CHAPTER, 1, parent_id="act-1")
    chapter = SimpleNamespace(
        id="chapter-1",
        number=1,
        title="第一章",
        content="重写后的正文。",
        content_sha256="rewritten-hash-1",
        content_revision=2,
    )
    nodes = _NodeRepository([act, chapter_node])
    service = VolumeSummaryService(
        llm_service=SimpleNamespace(
            generate=AsyncMock(return_value=SimpleNamespace(content="重建后的幕摘要"))
        ),
        story_node_repository=nodes,
        chapter_repository=_ChapterRepository([chapter]),
    )

    result = await service.generate_act_summary("novel-1", "act-1")

    assert result.success is True
    assert act.metadata["runtime.summary"] == "重建后的幕摘要"
    assert "runtime.summary_invalidated_from_chapter" not in act.metadata


@pytest.mark.asyncio
async def test_part_summary_persists_current_source_provenance_from_child_volumes():
    part = _node("part-1", NodeType.PART, 1)
    volume = _node(
        "volume-1",
        NodeType.VOLUME,
        1,
        parent_id="part-1",
        chapter_start=1,
        chapter_end=2,
    )
    first_chapter = SimpleNamespace(
        id="chapter-1",
        number=1,
        title="第一章",
        content="林澈进入钟楼。",
        content_sha256="chapter-hash-1",
        content_revision=1,
    )
    second_chapter = SimpleNamespace(
        id="chapter-2",
        number=2,
        title="第二章",
        content="林澈发现暗门。",
        content_sha256="chapter-hash-2",
        content_revision=2,
    )
    nodes = _NodeRepository([part, volume])
    service = VolumeSummaryService(
        llm_service=SimpleNamespace(
            generate=AsyncMock(return_value=SimpleNamespace(content="部摘要正文"))
        ),
        story_node_repository=nodes,
        chapter_repository=_ChapterRepository([first_chapter, second_chapter]),
    )

    result = await service.generate_part_summary("novel-1", 1)

    assert result.success is True
    assert part.metadata["runtime.summary"] == "部摘要正文"
    assert part.metadata["runtime.summary_state"] == {
        "status": "committed",
        "chapter_start": 1,
        "chapter_end": 2,
        "source_chapter_numbers": [1, 2],
        "source_version": _source_version(first_chapter, second_chapter),
        "pipeline_version": "node-summary/v1",
    }
    assert service.is_node_summary_current(part) is True


@pytest.mark.asyncio
async def test_part_summary_validates_the_exact_sparse_child_volume_sources():
    part = _node("part-1", NodeType.PART, 1)
    first_volume = _node(
        "volume-1",
        NodeType.VOLUME,
        1,
        parent_id="part-1",
        chapter_start=1,
        chapter_end=2,
    )
    later_volume = _node(
        "volume-2",
        NodeType.VOLUME,
        2,
        parent_id="part-1",
        chapter_start=4,
        chapter_end=5,
    )
    chapters = [
        SimpleNamespace(
            id=f"chapter-{number}",
            number=number,
            title=f"第{number}章",
            content=f"正文-{number}",
            content_sha256=f"chapter-hash-{number}",
            content_revision=number,
        )
        for number in range(1, 6)
    ]
    nodes = _NodeRepository([part, first_volume, later_volume])
    service = VolumeSummaryService(
        llm_service=SimpleNamespace(
            generate=AsyncMock(return_value=SimpleNamespace(content="非连续部摘要"))
        ),
        story_node_repository=nodes,
        chapter_repository=_ChapterRepository(chapters),
    )

    result = await service.generate_part_summary("novel-1", 1)

    assert result.success is True
    assert part.metadata["runtime.summary_state"]["source_chapter_numbers"] == [1, 2, 4, 5]
    assert service.is_node_summary_current(part) is True


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
    assert act.metadata["runtime.summary"] == "重建后的幕摘要"
    assert volume.metadata["runtime.summary"] == "基于当前正文的新卷摘要"
    assert nodes.updated == []
    assert [node_id for node_id, _ in nodes.runtime_updates] == [act.id, volume.id]


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


def test_summary_accessor_falls_back_to_valid_legacy_summary_when_runtime_is_stale():
    volume = _node("volume-1", NodeType.VOLUME, 1, chapter_start=1, chapter_end=1)
    chapter = SimpleNamespace(
        id="chapter-1",
        number=1,
        content="林澈在钟楼发现暗门。",
        content_sha256="chapter-hash-1",
        content_revision=1,
    )
    volume.metadata = {
        "runtime.summary": "来源不匹配的运行时卷摘要",
        "runtime.summary_state": {
            "status": "committed",
            "chapter_start": 1,
            "chapter_end": 1,
            "source_version": "stale-runtime-source",
            "pipeline_version": "node-summary/v1",
        },
        "summary": "仍然有效的旧卷摘要",
        "summary_state": {
            "status": "committed",
            "chapter_start": 1,
            "chapter_end": 1,
            "source_version": _source_version(chapter),
            "pipeline_version": "node-summary/v1",
        },
    }
    service = VolumeSummaryService(
        llm_service=SimpleNamespace(),
        story_node_repository=_NodeRepository([volume]),
        chapter_repository=_ChapterRepository([chapter]),
    )

    assert service.get_volume_summary("novel-1", 1) == "仍然有效的旧卷摘要"


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
    assert checkpoint_node.metadata["runtime.checkpoint_summary"] == "检查点摘要正文"
    assert checkpoint_node.metadata["runtime.checkpoint_summary_state"] == {
        "status": "committed",
        "chapter_start": 20,
        "chapter_end": 20,
        "source_chapter_numbers": [20],
        "source_version": _source_version(chapter),
        "pipeline_version": "node-summary/v1",
    }
    assert "checkpoint_summary" not in checkpoint_node.metadata
    assert nodes.updated == []
    assert nodes.runtime_updates[0][0] == checkpoint_node.id
