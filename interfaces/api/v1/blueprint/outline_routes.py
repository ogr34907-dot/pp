"""Outline Studio API: draft, publish/sync and five-level plan context."""

from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from application.blueprint.services.outline_contract_service import OutlineContractService
from application.blueprint.services.outline_draft_generation_service import (
    OutlineDraftGenerationService,
)
from domain.structure.outline_contract import OutlinePayload, OutlineSource
from infrastructure.persistence.database.outline_contract_repository import (
    OutlineContractRepository,
    OutlineContractSlot,
    OutlineGateError,
)
from infrastructure.persistence.database.chapter_candidate_repository import (
    ChapterCandidateRepository,
)
from infrastructure.persistence.database.story_node_repository import StoryNodeRepository
from interfaces.api import dependencies as api_dependencies


router = APIRouter(prefix="/outline", tags=["outline-studio"])


class OutlinePayloadDTO(BaseModel):
    title: str = ""
    narrative_text: str = ""
    creative_goal: str = ""
    entry_state: str = ""
    exit_state: str = ""
    required_events: list[str] = Field(default_factory=list)
    forbidden_events: list[str] = Field(default_factory=list)
    state_changes: dict[str, list[dict[str, Any]]] = Field(default_factory=dict)
    foreshadowing: dict[str, list[str]] = Field(default_factory=dict)
    chapter_start: Optional[int] = None
    chapter_end: Optional[int] = None
    word_budget: Optional[int] = None
    handoff_conditions: list[str] = Field(default_factory=list)
    pov: str = ""
    scenes: list[str] = Field(default_factory=list)
    beats: list[str] = Field(default_factory=list)
    conflicts: list[str] = Field(default_factory=list)
    ending_hook: str = ""
    extra: dict[str, Any] = Field(default_factory=dict)


class SaveDraftRequest(BaseModel):
    payload: OutlinePayloadDTO
    source: str = "author"


class PublishRequest(BaseModel):
    expected_revision: int = Field(..., ge=1)
    idempotency_key: str = Field(default="", max_length=200)
    author_locked: Optional[bool] = None


def get_outline_service() -> OutlineContractService:
    db = api_dependencies.get_database()
    return OutlineContractService(
        contract_repository=OutlineContractRepository(db),
        story_node_repository=StoryNodeRepository(db),
    )


def get_outline_draft_generation_service() -> OutlineDraftGenerationService:
    db = api_dependencies.get_database()
    return OutlineDraftGenerationService(
        OutlineContractRepository(db), api_dependencies.get_llm_service(), db
    )


def _revision_to_dict(revision) -> Optional[dict[str, Any]]:
    if revision is None:
        return None
    return {
        "revision": revision.revision,
        "status": revision.status.value,
        "source": revision.source.value,
        "digest": revision.published_digest or revision.digest,
        "parent_revision_digest": revision.parent_revision_digest,
        "payload": revision.payload.canonical_dict(),
    }


def _slot_to_dict(slot: OutlineContractSlot) -> dict[str, Any]:
    return {
        "id": slot.id,
        "novel_id": slot.novel_id,
        "level": slot.level.value,
        "parent_contract_id": slot.parent_contract_id,
        "story_node_id": slot.story_node_id,
        "author_locked": slot.author_locked,
        "has_author_edits": slot.has_author_edits,
        "active": _revision_to_dict(slot.active),
        "draft": _revision_to_dict(slot.draft),
    }


def _raise_contract_error(exc: Exception) -> None:
    if isinstance(exc, KeyError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, OutlineGateError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise exc


@router.get("/novels/{novel_id}/tree")
def get_outline_tree(
    novel_id: str,
    service: OutlineContractService = Depends(get_outline_service),
):
    """Logical total-outline root plus the existing physical structure tree."""

    return {"success": True, "data": service.logical_tree(novel_id)}


@router.get("/contracts/{contract_id}")
def get_outline_contract(
    contract_id: str,
    service: OutlineContractService = Depends(get_outline_service),
):
    try:
        return {"success": True, "data": _slot_to_dict(service.contract_repository.get_slot(contract_id))}
    except Exception as exc:
        _raise_contract_error(exc)


@router.get("/contracts/{contract_id}/versions")
def get_outline_versions(
    contract_id: str,
    service: OutlineContractService = Depends(get_outline_service),
):
    try:
        return {
            "success": True,
            "data": [_revision_to_dict(version) for version in service.contract_repository.list_versions(contract_id)],
        }
    except Exception as exc:
        _raise_contract_error(exc)


@router.post("/contracts/{contract_id}/draft")
def save_outline_draft(
    contract_id: str,
    body: SaveDraftRequest,
    service: OutlineContractService = Depends(get_outline_service),
):
    """Save an author/AI draft without changing the active plan projection."""

    try:
        try:
            source = OutlineSource(body.source)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="source must be ai, author or imported") from exc
        payload = OutlinePayload.from_dict(body.payload.model_dump())
        slot = service.contract_repository.save_draft(contract_id, payload, source=source)
        return {"success": True, "data": _slot_to_dict(slot)}
    except HTTPException:
        raise
    except Exception as exc:
        _raise_contract_error(exc)


@router.post("/contracts/{contract_id}/publish")
def publish_outline_contract(
    contract_id: str,
    body: PublishRequest,
    service: OutlineContractService = Depends(get_outline_service),
):
    """Switch to a specific draft revision and synchronously refresh its projection."""

    try:
        slot = service.contract_repository.publish_and_sync(
            contract_id,
            expected_revision=body.expected_revision,
            idempotency_key=body.idempotency_key,
            author_locked=body.author_locked,
        )
        active = slot.active
        stale_candidates = 0
        if active is not None:
            stale_candidates = ChapterCandidateRepository(api_dependencies.get_database()).stale_candidates_for_outline_contract(
                slot.novel_id,
                slot.id,
                active.published_digest or active.digest,
            )
        return {
            "success": True,
            "data": {**_slot_to_dict(slot), "stale_candidates": stale_candidates},
        }
    except Exception as exc:
        _raise_contract_error(exc)


@router.post("/novels/{novel_id}/story-nodes/{story_node_id}/contract")
def bind_story_node_to_outline_contract(
    novel_id: str,
    story_node_id: str,
    service: OutlineContractService = Depends(get_outline_service),
):
    """Open the next logical level only after its parent plan is synced."""

    try:
        slot = service.ensure_contract_for_story_node(novel_id, story_node_id)
        return {"success": True, "data": _slot_to_dict(slot)}
    except Exception as exc:
        _raise_contract_error(exc)


@router.post("/contracts/{contract_id}/generate-draft")
async def generate_outline_draft(
    contract_id: str,
    service: OutlineDraftGenerationService = Depends(get_outline_draft_generation_service),
):
    """Generate exactly one AI draft after checking the published parent gate."""

    try:
        slot = await service.generate_draft(contract_id)
        return {"success": True, "data": _slot_to_dict(slot)}
    except Exception as exc:
        _raise_contract_error(exc)


@router.get("/contracts/{contract_id}/generate-draft-stream")
async def stream_outline_draft_generation(
    contract_id: str,
    service: OutlineDraftGenerationService = Depends(get_outline_draft_generation_service),
):
    """Stream a single level's draft text, then persist its parsed draft revision."""

    async def events():
        try:
            async for event in service.stream_generate_draft(contract_id):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/novels/{novel_id}/chapters/{chapter_node_id}/published-context")
def get_published_chapter_outline_context(
    novel_id: str,
    chapter_node_id: str,
    service: OutlineContractService = Depends(get_outline_service),
):
    """Diagnostic endpoint used by generation wiring and the review desk."""

    try:
        return {
            "success": True,
            "data": service.published_context_for_chapter(novel_id, chapter_node_id),
        }
    except Exception as exc:
        _raise_contract_error(exc)
