"""Application-facing logical tree and prose-context gate."""

from dataclasses import dataclass

import pytest

from application.blueprint.services.outline_contract_service import OutlineContractService
from domain.structure.outline_contract import OutlineLevel, OutlinePayload, OutlineSource
from domain.structure.story_node import NodeType, StoryNode
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.outline_contract_repository import OutlineContractRepository
from infrastructure.persistence.database.story_node_repository import StoryNodeRepository


@dataclass
class _NodeRepo:
    nodes: list[StoryNode]

    def get_by_novel_sync(self, novel_id: str):
        return [node for node in self.nodes if node.novel_id == novel_id]


class _Contracts:
    """Small fake that exposes the repository boundary used by the service."""

    def __init__(self):
        self.root = type("Root", (), {"id": "root", "level": OutlineLevel.OUTLINE, "active": None})()

    def ensure_root(self, novel_id):
        return self.root

    def get_slot_by_story_node(self, novel_id, story_node_id):
        return None


class _ManifestContracts:
    def __init__(self, rows):
        self.rows = rows

    def get_planning_head(self, novel_id):
        return type(
            "Head",
            (),
            {
                "authority_mode": "manifest",
                "active_plan_revision_id": "plan-1",
                "active_plan_digest": "plan-digest",
            },
        )()

    def active_plan_items_with_payload(self, novel_id):
        return list(self.rows)


def _node(id: str, node_type: NodeType, parent_id: str | None = None) -> StoryNode:
    return StoryNode(
        id=id,
        novel_id="novel-1",
        node_type=node_type,
        number=1,
        title=id,
        order_index=1,
        parent_id=parent_id,
    )


def _payload(title: str) -> OutlinePayload:
    return OutlinePayload(
        title=title,
        creative_goal="保持计划与事实一致",
        required_events=["人物选择带来代价"],
    )


def test_logical_tree_wraps_legacy_part_roots_under_one_outline_root():
    service = OutlineContractService(
        contract_repository=_Contracts(),
        story_node_repository=_NodeRepo(
            [
                _node("part-1", NodeType.PART),
                _node("volume-1", NodeType.VOLUME, "part-1"),
                _node("act-1", NodeType.ACT, "volume-1"),
                _node("chapter-1", NodeType.CHAPTER, "act-1"),
            ]
        ),
    )

    tree = service.logical_tree("novel-1")
    assert tree["node_type"] == "outline"
    assert tree["children"][0]["id"] == "part-1"
    assert tree["children"][0]["children"][0]["children"][0]["children"][0]["id"] == "chapter-1"


def test_prose_context_refuses_missing_or_unsynced_contract_chain():
    service = OutlineContractService(
        contract_repository=_Contracts(),
        story_node_repository=_NodeRepo([_node("chapter-1", NodeType.CHAPTER)]),
    )

    with pytest.raises(ValueError, match="outline chain"):
        service.published_context_for_chapter("novel-1", "chapter-1")


def test_published_context_uses_only_active_synced_revisions(tmp_path):
    db = DatabaseConnection(str(tmp_path / "outline-context.db"))
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("novel-1", "Outline Novel", "outline-novel", 20),
    )
    conn.commit()
    node_repo = StoryNodeRepository(db)
    parent_id = None
    nodes = []
    for level, node_type in (
        (OutlineLevel.PART, NodeType.PART),
        (OutlineLevel.VOLUME, NodeType.VOLUME),
        (OutlineLevel.ACT, NodeType.ACT),
        (OutlineLevel.CHAPTER, NodeType.CHAPTER),
    ):
        node = _node(f"{level.value}-1", node_type, parent_id)
        nodes.append(node)
        node_repo.save_sync(node)
        parent_id = node.id
    service = OutlineContractService(
        contract_repository=OutlineContractRepository(db), story_node_repository=node_repo
    )

    root = service.contract_repository.ensure_root("novel-1")
    root_draft = service.contract_repository.save_draft(root.id, _payload("总纲 v1"), source=OutlineSource.AUTHOR)
    service.contract_repository.publish_and_sync(root.id, expected_revision=root_draft.draft.revision)
    for node in nodes:
        slot = service.ensure_contract_for_story_node("novel-1", node.id)
        draft = service.contract_repository.save_draft(slot.id, _payload(f"{node.id} v1"))
        service.contract_repository.publish_and_sync(slot.id, expected_revision=draft.draft.revision)

    context = service.published_context_for_chapter("novel-1", "chapter-1")
    assert tuple(context) == ("outline", "part", "volume", "act", "chapter")
    assert context["outline"]["payload"]["title"] == "总纲 v1"

    # An unpublished author edit never leaks into the next chapter prompt.
    service.contract_repository.save_draft(root.id, _payload("总纲 v2 草稿"), source=OutlineSource.AUTHOR)
    context_after_draft = service.published_context_for_chapter("novel-1", "chapter-1")
    assert context_after_draft["outline"]["payload"]["title"] == "总纲 v1"

    next_node, next_context = service.next_published_chapter_context("novel-1", after_chapter=0)
    assert next_node.id == "chapter-1"
    assert next_context["chapter"]["payload"]["title"] == "chapter-1 v1"


def test_manifest_chain_uses_logical_plan_parent_not_physical_story_parent():
    nodes = [
        _node("part-1", NodeType.PART),
        _node("volume-1", NodeType.VOLUME, None),
        _node("act-1", NodeType.ACT, None),
        _node("chapter-1", NodeType.CHAPTER, None),
    ]
    rows = []
    payloads = {
        "root": ("outline", None, "总纲"),
        "part-1": ("part", "root", "部纲"),
        "volume-1": ("volume", "part-1", "卷纲"),
        "act-1": ("act", "volume-1", "幕纲"),
        "chapter-1": ("chapter", "act-1", "章纲"),
    }
    for logical_id, (level, parent_id, title) in payloads.items():
        rows.append(
            {
                "item_id": f"item-{logical_id}",
                "logical_node_id": logical_id,
                "parent_logical_node_id": parent_id,
                "level": level,
                "sibling_index": 0,
                "expansion_state": "expanded",
                "validated_parent_digest": "",
                "validated_previous_sibling_digest": "",
                "is_reused": 0,
                "novel_id": "novel-1",
                "story_node_id": None if logical_id == "root" else logical_id,
                "parent_contract_id": parent_id,
                "author_locked": 0,
                "version_id": f"version-{logical_id}",
                "version_revision": 1,
                "version_digest": f"digest-{logical_id}",
                "payload_json": __import__("json").dumps(
                    {
                        "title": title,
                        "narrative_text": title,
                        "creative_goal": "推进目标",
                        "entry_state": "前态",
                        "exit_state": "后态",
                        "state_changes": {"characters": []},
                        "handoff_conditions": ["继续"],
                    }
                ),
                "version_status": "synced",
            }
        )
    service = OutlineContractService(
        contract_repository=_ManifestContracts(rows),
        story_node_repository=_NodeRepo(nodes),
    )

    chain = service.active_chain_for_chapter("novel-1", "chapter-1")

    assert tuple(chain._contracts) == (
        OutlineLevel.OUTLINE,
        OutlineLevel.PART,
        OutlineLevel.VOLUME,
        OutlineLevel.ACT,
        OutlineLevel.CHAPTER,
    )
    assert chain.contract_for(OutlineLevel.VOLUME).payload.title == "卷纲"
