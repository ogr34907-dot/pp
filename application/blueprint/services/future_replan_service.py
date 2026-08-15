"""Fail-closed routing for future planning changes versus Worldline rewrite."""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Any, Optional, Protocol

from domain.structure.outline_plan import OutlinePlanRevision
from domain.structure.outline_plan_validation import compute_replan_impact_closure
from infrastructure.persistence.database.outline_contract_repository import (
    OutlineContractRepository,
)


class FormalHistoryReader(Protocol):
    def formal_history_snapshot(self, novel_id: str) -> tuple[int, tuple[str, ...]]: ...


class FutureReplanDecision(str, Enum):
    FUTURE_REPLAN = "future_replan"
    WORLDLINE_REQUIRED = "worldline_required"
    HISTORY_UNPROVEN = "history_unproven"


@dataclass(frozen=True)
class FutureReplanPreview:
    decision: FutureReplanDecision
    invalidated_logical_node_ids: tuple[str, ...]
    ancestor_logical_node_ids: tuple[str, ...]
    formal_head: int
    blockers: tuple[str, ...] = ()
    draft_plan: Optional[OutlinePlanRevision] = None


class FutureReplanService:
    """Open a new plan draft only when the affected range is future-only."""

    def __init__(
        self,
        repository: OutlineContractRepository,
        formal_history: FormalHistoryReader,
    ) -> None:
        self.repository = repository
        self.formal_history = formal_history

    def preview_and_open_draft(
        self,
        *,
        novel_id: str,
        changed_logical_node_id: str,
        replan_start_chapter: int,
        author_intent: str,
    ) -> FutureReplanPreview:
        active = self.repository.get_active_plan(novel_id)
        if active is None:
            raise ValueError("future replan requires an active plan")
        closure = compute_replan_impact_closure(
            active.items, changed_logical_node_id
        )
        formal_head, blockers = self.formal_history.formal_history_snapshot(novel_id)
        boundary = int(replan_start_chapter)
        if boundary < 1:
            raise ValueError("replan_start_chapter must be positive")
        if blockers:
            return FutureReplanPreview(
                decision=FutureReplanDecision.HISTORY_UNPROVEN,
                invalidated_logical_node_ids=closure.invalidated_logical_node_ids,
                ancestor_logical_node_ids=closure.ancestor_logical_node_ids,
                formal_head=formal_head,
                blockers=tuple(blockers),
            )
        if boundary <= formal_head:
            return FutureReplanPreview(
                decision=FutureReplanDecision.WORLDLINE_REQUIRED,
                invalidated_logical_node_ids=closure.invalidated_logical_node_ids,
                ancestor_logical_node_ids=closure.ancestor_logical_node_ids,
                formal_head=formal_head,
            )
        draft = self.repository.clone_active_plan_draft(
            novel_id,
            replan_start_chapter=boundary,
            author_intent=author_intent,
            created_by="author",
        )
        return FutureReplanPreview(
            decision=FutureReplanDecision.FUTURE_REPLAN,
            invalidated_logical_node_ids=closure.invalidated_logical_node_ids,
            ancestor_logical_node_ids=closure.ancestor_logical_node_ids,
            formal_head=formal_head,
            draft_plan=draft,
        )
