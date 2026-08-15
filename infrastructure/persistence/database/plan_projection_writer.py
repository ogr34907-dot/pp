"""Reserved manifest-to-StoryNode projection boundary.

The first Manifest migration records logical topology and sealed outline
payloads. It deliberately does not record an immutable physical StoryNode
projection batch. Accepting caller-provided create/update/delete operations
would therefore let an arbitrary physical tree masquerade as an approved
Manifest projection.

This boundary stays fail-closed until a later migration introduces a sealed
physical projection declaration. Keeping the constructor and coroutine name
avoids an accidental compatibility break while ensuring no caller can use the
old generic batch API to change a Head or StoryNode tree.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import TYPE_CHECKING, Optional

from infrastructure.persistence.database.planning_authority_guard import (
    PlanningAuthorityError,
)

if TYPE_CHECKING:
    from domain.structure.story_node import StoryNode
    from infrastructure.persistence.database.story_node_repository import StoryNodeRepository


class PlanProjectionWriter:
    """Reject undeclared physical projections until their snapshot exists."""

    def __init__(self, repository: "StoryNodeRepository") -> None:
        # Preserve dependency-injection compatibility without retaining a
        # callable repository path for projection DML.
        del repository

    async def apply_atomic(
        self,
        *,
        novel_id: str,
        plan_revision_id: str,
        operation: str,
        expected_active_plan_revision_id: Optional[str],
        expected_active_plan_digest: str,
        expected_authority_generation: int,
        expected_projection_generation: int,
        creates: Sequence["StoryNode"] = (),
        updates: Sequence["StoryNode"] = (),
        deletes: Sequence[str] = (),
    ) -> None:
        """Fail closed instead of accepting an unverifiable projection batch.

        Parameters remain intentionally visible because a future sealed
        projection declaration must bind the exact Head CAS and physical
        batch. They are not consumed today: doing so would imply that
        caller-supplied StoryNode values are Manifest facts.
        """

        del (
            novel_id,
            plan_revision_id,
            operation,
            expected_active_plan_revision_id,
            expected_active_plan_digest,
            expected_authority_generation,
            expected_projection_generation,
            creates,
            updates,
            deletes,
        )
        raise PlanningAuthorityError(
            "caller-supplied StoryNode projection batches are disabled until "
            "the Manifest stores an immutable physical projection declaration"
        )
