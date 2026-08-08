from __future__ import annotations

from typing import Any

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

from application.governance.service import NarrativeGovernanceService
from infrastructure.persistence.database.connection import get_database
from infrastructure.persistence.database.sqlite_governance_repository import (
    SqliteGovernanceRepository,
)

router = APIRouter(prefix="/novels/{novel_id}/governance", tags=["narrative-governance"])


class ContractPayload(BaseModel):
    title_promise: str | None = None
    core_question: str | None = None
    theme_anchors: list[str] | None = None
    forbidden_early_payoffs: list[str] | None = None
    reveal_budget: dict[str, Any] | None = None


class MergeStorylinesPayload(BaseModel):
    source_ids: list[str] = Field(default_factory=list)
    target_id: str | None = None
    title: str | None = None
    aliases: list[str] = Field(default_factory=list)
    promise_tags: list[str] = Field(default_factory=list)


class BudgetPreviewPayload(BaseModel):
    chapter_number: int | None = None


class ReviewActionPayload(BaseModel):
    report_id: str
    action: str = "accepted"
    patch: dict[str, Any] | None = None


class HierarchyAlignmentPayload(BaseModel):
    chapter_id: str
    candidate: dict[str, Any] = Field(default_factory=dict)


class HierarchyOverridePayload(BaseModel):
    candidate_digest: str
    reason: str


class HierarchyReplanPayload(HierarchyAlignmentPayload):
    pass


def _service() -> NarrativeGovernanceService:
    db = get_database()
    repo = SqliteGovernanceRepository(db)
    novel_repo = None
    storyline_repo = None
    try:
        from interfaces.api.dependencies import get_novel_repository

        novel_repo = get_novel_repository()
    except Exception:
        novel_repo = None
    try:
        from infrastructure.persistence.database.sqlite_storyline_repository import SqliteStorylineRepository

        storyline_repo = SqliteStorylineRepository(db)
    except Exception:
        storyline_repo = None
    story_node_repo = None
    try:
        from infrastructure.persistence.database.story_node_repository import StoryNodeRepository

        story_node_repo = StoryNodeRepository(db)
    except Exception:
        story_node_repo = None
    from application.engine.services.hierarchical_narrative_alignment_gate import (
        HierarchicalNarrativeAlignmentGate,
    )

    return NarrativeGovernanceService(
        repo,
        novel_repo,
        storyline_repo,
        db,
        hierarchy_gate=HierarchicalNarrativeAlignmentGate(),
        story_node_repository=story_node_repo,
    )


def _payload_dict(model: BaseModel, *, exclude_none: bool = False) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_none=exclude_none)  # type: ignore[attr-defined]
    return model.dict(exclude_none=exclude_none)


@router.get("/state")
async def get_governance_state(novel_id: str) -> dict[str, Any]:
    try:
        return _service().get_state(novel_id)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"governance state failed: {exc}") from exc


@router.post("/contract")
async def update_governance_contract(novel_id: str, payload: ContractPayload) -> dict[str, Any]:
    try:
        return _service().update_contract(novel_id, _payload_dict(payload, exclude_none=True)).to_dict()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"governance contract update failed: {exc}") from exc


@router.post("/storylines/merge")
async def merge_governance_storylines(novel_id: str, payload: MergeStorylinesPayload) -> dict[str, Any]:
    try:
        return _service().merge_storylines(novel_id, _payload_dict(payload)).to_dict()
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"governance storyline merge failed: {exc}") from exc


@router.post("/chapter-budget/preview")
async def preview_governance_budget(novel_id: str, payload: BudgetPreviewPayload) -> dict[str, Any]:
    try:
        return _service().prepare_chapter(novel_id, payload.chapter_number)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"governance budget preview failed: {exc}") from exc


@router.post("/review-action")
async def apply_governance_review_action(novel_id: str, payload: ReviewActionPayload) -> dict[str, Any]:
    try:
        return _service().review_action(novel_id, _payload_dict(payload))
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"governance review action failed: {exc}") from exc


@router.post("/hierarchy/status")
@router.post("/hierarchy/preview")
@router.post("/hierarchy-alignment/status")
@router.post("/hierarchy-alignment/preview")
async def preview_hierarchy_alignment(novel_id: str, payload: HierarchyAlignmentPayload) -> dict[str, Any]:
    try:
        return _service().preview_hierarchy_alignment(novel_id, payload.chapter_id, payload.candidate)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"hierarchy alignment preview failed: {exc}") from exc


@router.post("/hierarchy/override")
@router.post("/hierarchy-alignment/override")
async def override_hierarchy_alignment(novel_id: str, payload: HierarchyOverridePayload) -> dict[str, Any]:
    try:
        return _service().override_hierarchy_alignment(novel_id, payload.candidate_digest, payload.reason)
    except ValueError as exc:
        # Exact/stale digest and malformed reasons are client errors and never mutate data.
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"hierarchy override failed: {exc}") from exc


@router.post("/replan/preview")
@router.post("/hierarchy/replan/preview")
@router.post("/safe-replan/preview")
async def preview_hierarchy_replan(novel_id: str, payload: HierarchyReplanPayload) -> dict[str, Any]:
    try:
        return _service().preview_hierarchy_replan(novel_id, payload.chapter_id, payload.candidate)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"hierarchy replan preview failed: {exc}") from exc
