"""Explicit preview, archive and restore API for tail worldline regeneration."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from application.engine.services.worldline_regeneration_service import (
    WorldlineRegenerationError,
    WorldlineRegenerationService,
)
from application.engine.services.worldline_rebuild_service import (
    WorldlineRebuildError,
    WorldlineRebuildService,
)
from interfaces.api import dependencies as api_dependencies


router = APIRouter(prefix="/worldline-regeneration", tags=["worldline-regeneration"])


class WorldlinePreviewRequest(BaseModel):
    start_chapter: int = Field(..., ge=1)
    target_chapters: int = Field(..., ge=1, le=100000)


class WorldlineExecuteRequest(BaseModel):
    preview_token: str = Field(..., min_length=1)
    run_mode: str = "continuous"
    idempotency_key: str = Field(default="", max_length=256)


class WorldlineRestoreRequest(BaseModel):
    run_mode: str = "continuous"
    idempotency_key: str = Field(default="", max_length=256)


def get_worldline_regeneration_service() -> WorldlineRegenerationService:
    return WorldlineRegenerationService(api_dependencies.get_database())


def get_worldline_rebuild_service() -> WorldlineRebuildService:
    return WorldlineRebuildService(
        api_dependencies.get_database(), api_dependencies.get_chapter_aftermath_pipeline()
    )


def _result_dict(result) -> dict[str, Any]:
    return {
        "operation": result.operation,
        "novel_id": result.novel_id,
        "archive_id": result.archive_id,
        "generation_epoch": result.generation_epoch,
        "retained_through": result.retained_through,
        "next_action": result.next_action,
    }


def _preview_dict(preview) -> dict[str, Any]:
    return {
        "token": preview.token,
        "novel_id": preview.novel_id,
        "operation": preview.operation,
        "start_chapter": preview.start_chapter,
        "target_chapters": preview.target_chapters,
        "current_generated_chapters": preview.current_generated_chapters,
        "retained_through": preview.retained_through,
        "archive_from": preview.archive_from,
        "archive_to": preview.archive_to,
        "generation_epoch": preview.generation_epoch,
        "prefix_digest": preview.prefix_digest,
        "counts": preview.counts,
    }


def _raise_worldline_error(exc: Exception) -> None:
    if isinstance(exc, KeyError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, WorldlineRegenerationError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, WorldlineRebuildError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise exc


@router.post("/novels/{novel_id}/preview")
def preview_worldline_regeneration(
    novel_id: str,
    body: WorldlinePreviewRequest,
    service: WorldlineRegenerationService = Depends(get_worldline_regeneration_service),
):
    try:
        return {
            "success": True,
            "data": _preview_dict(
                service.preview(
                    novel_id,
                    start_chapter=body.start_chapter,
                    target_chapters=body.target_chapters,
                )
            ),
        }
    except Exception as exc:
        _raise_worldline_error(exc)


@router.post("/novels/{novel_id}/execute")
def execute_worldline_regeneration(
    novel_id: str,
    body: WorldlineExecuteRequest,
    service: WorldlineRegenerationService = Depends(get_worldline_regeneration_service),
):
    try:
        return {
            "success": True,
            "data": _result_dict(
                service.execute(
                    novel_id,
                    preview_token=body.preview_token,
                    run_mode=body.run_mode,
                    idempotency_key=body.idempotency_key,
                )
            ),
        }
    except Exception as exc:
        _raise_worldline_error(exc)


@router.get("/novels/{novel_id}/archives")
def list_worldline_archives(
    novel_id: str,
    service: WorldlineRegenerationService = Depends(get_worldline_regeneration_service),
):
    try:
        return {"success": True, "data": service.list_archives(novel_id)}
    except Exception as exc:
        _raise_worldline_error(exc)


@router.post("/novels/{novel_id}/archives/{archive_id}/restore")
def restore_worldline_archive(
    novel_id: str,
    archive_id: str,
    body: WorldlineRestoreRequest,
    service: WorldlineRegenerationService = Depends(get_worldline_regeneration_service),
):
    try:
        return {
            "success": True,
            "data": _result_dict(
                service.restore(
                    novel_id,
                    archive_id=archive_id,
                    run_mode=body.run_mode,
                    idempotency_key=body.idempotency_key,
                )
            ),
        }
    except Exception as exc:
        _raise_worldline_error(exc)


@router.get("/novels/{novel_id}/rebuild-status")
def get_worldline_rebuild_status(
    novel_id: str,
    service: WorldlineRebuildService = Depends(get_worldline_rebuild_service),
):
    try:
        return {"success": True, "data": service.status(novel_id)}
    except Exception as exc:
        _raise_worldline_error(exc)


@router.post("/novels/{novel_id}/rebuild")
async def rebuild_worldline_prefix(
    novel_id: str,
    service: WorldlineRebuildService = Depends(get_worldline_rebuild_service),
):
    """Replay the retained formal prefix before author can select a run mode."""

    try:
        return {"success": True, "data": await service.rebuild(novel_id)}
    except Exception as exc:
        _raise_worldline_error(exc)


@router.post("/novels/{novel_id}/rebuild/cancel")
def cancel_worldline_rebuild(
    novel_id: str,
    service: WorldlineRebuildService = Depends(get_worldline_rebuild_service),
):
    try:
        return {"success": True, "data": service.cancel(novel_id)}
    except Exception as exc:
        _raise_worldline_error(exc)
