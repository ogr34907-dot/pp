"""Application service for the logical five-level outline tree.

The physical ``story_nodes`` rows remain the source for Part → Volume → Act →
Chapter identity.  This service adds the total-outline root and makes the
published contract projection the only plan data a prose prompt can use.
"""

from __future__ import annotations

from typing import Any, Optional

from domain.structure.outline_contract import OutlineChain, OutlineLevel
from domain.structure.story_node import NodeType, StoryNode
from infrastructure.persistence.database.outline_contract_repository import (
    OutlineContractRepository,
    OutlineContractSlot,
)


_NODE_LEVELS = {
    NodeType.PART: OutlineLevel.PART,
    NodeType.VOLUME: OutlineLevel.VOLUME,
    NodeType.ACT: OutlineLevel.ACT,
    NodeType.CHAPTER: OutlineLevel.CHAPTER,
}


class OutlineContractService:
    """Bridge legacy structure nodes to versioned plan contracts."""

    def __init__(
        self,
        *,
        contract_repository: OutlineContractRepository,
        story_node_repository: Any,
    ) -> None:
        self.contract_repository = contract_repository
        self.story_node_repository = story_node_repository

    @staticmethod
    def _node_type(node: StoryNode) -> NodeType:
        value = node.node_type
        return value if isinstance(value, NodeType) else NodeType(str(value))

    @staticmethod
    def _node_dict(node: StoryNode, slot: Optional[OutlineContractSlot]) -> dict[str, Any]:
        payload = node.to_dict()
        active = slot.active if slot else None
        draft = slot.draft if slot else None
        payload["outline_contract"] = {
            "contract_id": slot.id if slot else None,
            "level": slot.level.value if slot else None,
            "status": active.status.value if active else ("draft" if draft else "missing"),
            "active_revision": active.revision if active else None,
            "draft_revision": draft.revision if draft else None,
            "author_locked": bool(slot.author_locked) if slot else False,
            "has_author_edits": bool(slot.has_author_edits) if slot else False,
        }
        return payload

    def logical_tree(self, novel_id: str) -> dict[str, Any]:
        """Return a root-first tree without rewriting legacy parent IDs."""

        root = self.contract_repository.ensure_root(novel_id)
        nodes = self.story_node_repository.get_by_novel_sync(novel_id)
        nodes_by_parent: dict[Optional[str], list[StoryNode]] = {}
        for node in nodes:
            if self._node_type(node) == NodeType.OUTLINE:
                continue
            nodes_by_parent.setdefault(node.parent_id, []).append(node)
        for child_nodes in nodes_by_parent.values():
            child_nodes.sort(key=lambda item: (item.order_index, item.number, item.id))

        def render(node: StoryNode) -> dict[str, Any]:
            slot = self.contract_repository.get_slot_by_story_node(novel_id, node.id)
            result = self._node_dict(node, slot)
            result["children"] = [render(child) for child in nodes_by_parent.get(node.id, [])]
            return result

        active = getattr(root, "active", None)
        draft = getattr(root, "draft", None)
        return {
            "id": root.id,
            "novel_id": novel_id,
            "node_type": OutlineLevel.OUTLINE.value,
            "number": 1,
            "order_index": 0,
            "title": active.payload.title if active else (draft.payload.title if draft else "总纲"),
            "description": active.payload.narrative_text if active else (draft.payload.narrative_text if draft else ""),
            "outline_contract": {
                "contract_id": root.id,
                "level": OutlineLevel.OUTLINE.value,
                "status": active.status.value if active else ("draft" if draft else "missing"),
                "active_revision": active.revision if active else None,
                "draft_revision": draft.revision if draft else None,
                "author_locked": bool(getattr(root, "author_locked", False)),
                "has_author_edits": bool(getattr(root, "has_author_edits", False)),
            },
            "children": [render(node) for node in nodes_by_parent.get(None, [])],
        }

    def ensure_contract_for_story_node(
        self, novel_id: str, story_node_id: str
    ) -> OutlineContractSlot:
        """Bind an existing physical node after its parent is published/synced."""

        existing = self.contract_repository.get_slot_by_story_node(novel_id, story_node_id)
        if existing is not None:
            return existing
        nodes = {node.id: node for node in self.story_node_repository.get_by_novel_sync(novel_id)}
        node = nodes.get(story_node_id)
        if node is None:
            raise KeyError(f"story node not found: {story_node_id}")
        level = _NODE_LEVELS.get(self._node_type(node))
        if level is None:
            raise ValueError(f"unsupported physical outline node: {self._node_type(node).value}")
        if level == OutlineLevel.PART:
            parent_contract_id = self.contract_repository.ensure_root(novel_id).id
        else:
            if not node.parent_id:
                raise ValueError(f"{level.value} node must have a physical parent")
            parent_contract_id = self.ensure_contract_for_story_node(novel_id, node.parent_id).id
        return self.contract_repository.create_contract(
            novel_id=novel_id,
            level=level,
            parent_contract_id=parent_contract_id,
            story_node_id=node.id,
        )

    def active_chain_for_chapter(self, novel_id: str, chapter_node_id: str) -> OutlineChain:
        """Assemble exactly the current root → part → volume → act → chapter chain."""

        nodes = {node.id: node for node in self.story_node_repository.get_by_novel_sync(novel_id)}
        chapter = nodes.get(chapter_node_id)
        if chapter is None:
            raise KeyError(f"story node not found: {chapter_node_id}")
        if self._node_type(chapter) != NodeType.CHAPTER:
            raise ValueError("prose context requires a chapter outline node")

        contracts = []
        root = self.contract_repository.ensure_root(novel_id)
        if root.active is not None:
            contracts.append(root.active)
        current: Optional[StoryNode] = chapter
        expected = (OutlineLevel.CHAPTER, OutlineLevel.ACT, OutlineLevel.VOLUME, OutlineLevel.PART)
        for level in expected:
            if current is None or _NODE_LEVELS.get(self._node_type(current)) != level:
                break
            slot = self.contract_repository.get_slot_by_story_node(novel_id, current.id)
            if slot is not None and slot.active is not None:
                contracts.append(slot.active)
            current = nodes.get(current.parent_id) if current.parent_id else None
        return OutlineChain(contracts)

    def published_context_for_chapter(self, novel_id: str, chapter_node_id: str) -> dict[str, Any]:
        chain = self.active_chain_for_chapter(novel_id, chapter_node_id)
        if not chain.ready_for_prose:
            raise ValueError(
                "outline chain is not ready for prose generation: " + ", ".join(chain.blockers)
            )
        return chain.to_prompt_context()

    def next_published_chapter_context(
        self, novel_id: str, *, after_chapter: int
    ) -> tuple[StoryNode, dict[str, Any]]:
        """Find the next physical chapter whose complete active chain is ready.

        This is deliberately stricter than looking at a chapter number alone:
        a draft, stale or conflicting parent blocks the LLM before any prose
        call can occur.
        """

        nodes = sorted(
            (
                node
                for node in self.story_node_repository.get_by_novel_sync(novel_id)
                if self._node_type(node) == NodeType.CHAPTER and node.number > after_chapter
            ),
            key=lambda node: (node.number, node.order_index, node.id),
        )
        blockers: list[str] = []
        for node in nodes:
            try:
                return node, self.published_context_for_chapter(novel_id, node.id)
            except (KeyError, ValueError) as exc:
                blockers.append(f"{node.id}:{exc}")
        if blockers:
            raise ValueError("no prose-ready chapter outline: " + "; ".join(blockers))
        raise ValueError("no planned chapter exists after the formal chapter cursor")
