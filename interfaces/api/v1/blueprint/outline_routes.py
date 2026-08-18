"""Outline Studio API: draft, publish/sync and five-level plan context."""

from __future__ import annotations

import json
from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from application.blueprint.services.outline_contract_service import OutlineContractService
from application.blueprint.services.outline_cohort_generation_service import (
    OutlineCohortGenerationError,
    OutlineCohortGenerationService,
)
from application.blueprint.services.outline_draft_generation_service import (
    OutlineDraftGenerationService,
)
from domain.structure.outline_contract import OutlineLevel, OutlinePayload, OutlineSource
from infrastructure.persistence.database.outline_contract_repository import (
    OutlineContractRepository,
    OutlineContractSlot,
    OutlineGateError,
)
from infrastructure.persistence.database.chapter_candidate_repository import (
    ChapterCandidateRepository,
)
from infrastructure.persistence.database.story_node_repository import StoryNodeRepository
from infrastructure.persistence.database.planning_authority_guard import (
    PlanningAuthorityError,
    assert_legacy_planning_mutation_allowed,
)
from interfaces.api import dependencies as api_dependencies


router = APIRouter(prefix="/outline", tags=["outline-studio"])
_MANIFEST_AUTHORITY_DETAIL = "manifest_planning_authority"


class OutlinePayloadDTO(BaseModel):
    model_config = ConfigDict(extra="allow")

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


class CohortExpandRequest(BaseModel):
    parent_logical_node_id: str = Field(..., min_length=1)
    level: OutlineLevel
    author_payloads: list[OutlinePayloadDTO] = Field(default_factory=list)
    retry_attempt_id: Optional[str] = None


class WorkingItemRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    payload: dict[str, Any] = Field(default_factory=dict)
    expected_plan_digest: str = Field(..., min_length=1)
    expected_version_digest: str = Field(..., min_length=1)
    source: str = "author"


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


def get_outline_cohort_generation_service() -> OutlineCohortGenerationService:
    db = api_dependencies.get_database()
    repository = OutlineContractRepository(db)
    return OutlineCohortGenerationService(
        repository,
        OutlineContractService(
            contract_repository=repository,
            story_node_repository=StoryNodeRepository(db),
        ),
        api_dependencies.get_llm_service(),
        db,
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
    if isinstance(exc, PlanningAuthorityError):
        raise HTTPException(status_code=410, detail=_MANIFEST_AUTHORITY_DETAIL) from exc
    if isinstance(exc, KeyError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, OutlineGateError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise exc


def _raise_manifest_cohort_error(exc: Exception) -> None:
    if isinstance(
        exc,
        (PlanningAuthorityError, OutlineGateError, OutlineCohortGenerationError),
    ):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, KeyError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise exc


def _assert_legacy_outline_mutation_allowed(
    service: OutlineContractService, novel_id: str, operation: str
) -> None:
    assert_legacy_planning_mutation_allowed(
        service.contract_repository._connection(), novel_id, operation=operation
    )


@router.get("/novels/{novel_id}/tree")
def get_outline_tree(
    novel_id: str,
    service: OutlineContractService = Depends(get_outline_service),
):
    """Logical total-outline root plus the existing physical structure tree."""

    try:
        return {"success": True, "data": service.logical_tree(novel_id)}
    except Exception as exc:
        _raise_contract_error(exc)


@router.get("/novels/{novel_id}/working-tree")
def get_working_outline_tree(
    novel_id: str,
    service: OutlineContractService = Depends(get_outline_service),
):
    """Read the open Manifest draft without changing the Active Tree contract."""

    try:
        return {"success": True, "data": service.working_tree(novel_id)}
    except Exception as exc:
        _raise_contract_error(exc)


@router.patch("/plan-revisions/{plan_revision_id}/items/{logical_node_id}")
def patch_working_outline_item(
    plan_revision_id: str,
    logical_node_id: str,
    body: WorkingItemRequest,
    service: OutlineContractService = Depends(get_outline_service),
):
    """Edit one leaf in the current Manifest Working Plan."""

    try:
        try:
            source = OutlineSource(body.source)
        except ValueError as exc:
            raise HTTPException(
                status_code=422, detail="source must be ai, author or imported"
            ) from exc
        repository = service.contract_repository
        current = next(
            (
                row
                for row in repository.working_plan_items_with_payload(
                    repository.get_plan_revision(plan_revision_id).novel_id
                )
                if row["logical_node_id"] == logical_node_id
            ),
            None,
        )
        if current is None or current["plan_revision_id"] != plan_revision_id:
            raise KeyError(f"working outline item not found: {logical_node_id}")
        incoming = dict(body.payload)
        merged = {**dict(current.get("payload") or {}), **incoming}
        if "extra" in incoming:
            merged["extra"] = {
                **dict((current.get("payload") or {}).get("extra") or {}),
                **dict(incoming.get("extra") or {}),
            }
        payload = OutlinePayload.from_dict(merged)
        service.contract_repository.update_working_plan_item(
            plan_revision_id=plan_revision_id,
            logical_node_id=logical_node_id,
            payload=payload,
            expected_plan_digest=body.expected_plan_digest,
            expected_version_digest=body.expected_version_digest,
            source=source,
        )
        refreshed = next(
            row
            for row in repository.working_plan_items_with_payload(
                current["novel_id"]
            )
            if row["logical_node_id"] == logical_node_id
        )
        return {"success": True, "data": refreshed}
    except HTTPException:
        raise
    except Exception as exc:
        _raise_contract_error(exc)


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


@router.post("/novels/{novel_id}/cohorts/expand")
async def expand_manifest_cohort(
    novel_id: str,
    body: CohortExpandRequest,
    service: OutlineCohortGenerationService = Depends(
        get_outline_cohort_generation_service
    ),
):
    """Open (or recover) the Manifest draft, then generate one sibling cohort."""

    try:
        draft = service.open_or_clone_cohort_draft(novel_id)
        generate_kwargs = {
            "plan_revision_id": draft.id,
            "parent_logical_node_id": body.parent_logical_node_id,
            "level": body.level,
            "author_payloads": tuple(
                OutlinePayload.from_dict(payload.model_dump())
                for payload in body.author_payloads
            ),
        }
        if body.retry_attempt_id:
            generate_kwargs["retry_of_attempt_id"] = body.retry_attempt_id
        result = await service.generate_cohort(
            **generate_kwargs,
        )
        return {"success": True, "data": result}
    except Exception as exc:
        _raise_manifest_cohort_error(exc)


@router.post("/cohort-attempts/{attempt_id}/publish")
async def publish_manifest_cohort(
    attempt_id: str,
    service: OutlineCohortGenerationService = Depends(
        get_outline_cohort_generation_service
    ),
):
    """Publish a completed Manifest cohort without legacy node publication."""

    try:
        result = await service.publish_completed_cohort(attempt_id=attempt_id)
        run = result.get("run") if isinstance(result, dict) else None
        if run is not None:
            novel_id = str(getattr(run, "novel_id", "") or "")
            generation_epoch = getattr(run, "generation_epoch", None)
            if not novel_id or generation_epoch is None:
                raise OutlineCohortGenerationError(
                    "runtime cohort publication returned an invalid generation run"
                )
            repository = ChapterCandidateRepository(api_dependencies.get_database())
            try:
                claimed = bool(
                    api_dependencies.get_generation_run_coordinator().claim(novel_id)
                )
            except Exception as exc:
                claimed = False
                reason = f"runtime_publish_runner_claim_failed:{exc}"
            else:
                reason = "runtime_publish_runner_claim_failed"
            continuation_error = None
            if not claimed:
                try:
                    current = repository.get_run(novel_id)
                    if (
                        current.state.value == "running"
                        and current.generation_epoch == int(generation_epoch)
                    ):
                        repository.record_runner_error(
                            novel_id,
                            expected_generation_epoch=int(generation_epoch),
                            reason=reason,
                        )
                    continuation_error = reason
                except Exception as persist_exc:
                    continuation_error = (
                        f"{reason}; error state persistence failed: {persist_exc}"
                    )
            result = {
                **result,
                "continuation_started": claimed,
                "continuation_error": continuation_error,
            }
        elif isinstance(result, dict):
            result = {
                **result,
                "continuation_started": False,
                "continuation_error": None,
            }
        return {
            "success": True,
            "data": result,
        }
    except Exception as exc:
        _raise_manifest_cohort_error(exc)


@router.post("/cohort-attempts/{attempt_id}/author-publish")
async def publish_manifest_cohort_for_author(
    attempt_id: str,
    service: OutlineCohortGenerationService = Depends(
        get_outline_cohort_generation_service
    ),
):
    """Publish a completed author planning cohort without resuming a run."""

    try:
        return {
            "success": True,
            "data": await service.publish_author_planning_cohort(attempt_id=attempt_id),
        }
    except Exception as exc:
        _raise_manifest_cohort_error(exc)


@router.post("/novels/{novel_id}/story-nodes/{story_node_id}/contract")
def bind_story_node_to_outline_contract(
    novel_id: str,
    story_node_id: str,
    service: OutlineContractService = Depends(get_outline_service),
):
    """Open the next logical level only after its parent plan is synced."""

    try:
        _assert_legacy_outline_mutation_allowed(service, novel_id, "bind_story_node")
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
    retry_attempt_id: Optional[str] = None,
    service: OutlineDraftGenerationService = Depends(get_outline_draft_generation_service),
):
    """Stream a single level's draft text, then persist its parsed draft revision."""

    async def events():
        try:
            async for event in service.stream_generate_draft(
                contract_id, retry_attempt_id=retry_attempt_id
            ):
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
        except Exception as exc:
            yield f"data: {json.dumps({'type': 'error', 'message': str(exc)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(
        events(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.get("/contracts/{contract_id}/generation-attempts/latest")
def get_latest_outline_generation_attempt(
    contract_id: str,
    after_sequence: int = Query(default=0, ge=0),
    service: OutlineContractService = Depends(get_outline_service),
):
    try:
        attempt = service.contract_repository.get_latest_generation_attempt(
            contract_id, after_sequence=after_sequence
        )
        return {"success": True, "data": attempt}
    except Exception as exc:
        _raise_contract_error(exc)


@router.post("/contracts/{contract_id}/generation-attempts/{attempt_id}/cancel")
def cancel_outline_generation_attempt(
    contract_id: str,
    attempt_id: str,
    service: OutlineContractService = Depends(get_outline_service),
):
    try:
        attempt = service.contract_repository.get_generation_attempt(attempt_id)
        if attempt["contract_id"] != contract_id:
            raise OutlineGateError("outline generation attempt belongs to another contract")
        return {"success": True, "data": service.contract_repository.cancel_generation_attempt(attempt_id)}
    except Exception as exc:
        _raise_contract_error(exc)


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
