"""Capability-bound compatibility projection writer.

This is the only runtime adapter allowed to mutate protected ``story_nodes``
fields for a manifest book.  It intentionally delegates to
``StoryNodeRepository`` so the repository and direct SQL paths share one guard.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Optional

from infrastructure.persistence.database.planning_authority_guard import (
    ProjectionWriteCapability,
    _issue_projection_capability,
)

if TYPE_CHECKING:
    from domain.structure.story_node import StoryNode
    from infrastructure.persistence.database.story_node_repository import StoryNodeRepository


class PlanProjectionWriter:
    """Write the current manifest projection using an explicit capability."""

    def __init__(self, repository: "StoryNodeRepository") -> None:
        self.repository = repository

    def capability_for(
        self,
        novel_id: str,
        plan_revision_id: str,
    ) -> ProjectionWriteCapability:
        return _issue_projection_capability(
            self.repository._get_connection(),
            novel_id=novel_id,
            plan_revision_id=plan_revision_id,
        )

    def save_sync(
        self,
        node: "StoryNode",
        capability: ProjectionWriteCapability,
    ) -> "StoryNode":
        return self.repository.save_sync(node, _capability=capability)

    def update(
        self,
        node: "StoryNode",
        capability: ProjectionWriteCapability,
    ) -> "StoryNode":
        return self.repository.update(node, _capability=capability)

    def save_batch(
        self,
        nodes: list["StoryNode"],
        capability: ProjectionWriteCapability,
    ) -> list["StoryNode"]:
        return self.repository.save_batch(nodes, _capability=capability)

    def apply_merge_plan(
        self,
        creates: list[dict],
        updates: list[dict],
        deletes: list[str],
        capability: ProjectionWriteCapability,
    ) -> None:
        self.repository.apply_merge_plan(
            creates,
            updates,
            deletes,
            _capability=capability,
        )
