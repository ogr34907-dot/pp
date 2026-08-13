"""Candidate-first dual-mode generation control plane.

This router owns the public authority state consumed by Home, Workbench and
the future review desk.  It never guesses a writing status from the browser.
"""

from __future__ import annotations

from typing import Any, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from domain.novel.candidate_chapter import RunMode
from application.blueprint.services.outline_contract_service import OutlineContractService
from application.engine.services.candidate_chapter_workflow import (
    CandidateChapterWorkflowService,
    CandidateWorkflowError,
)
from application.engine.services.generation_start_preflight import (
    GenerationStartPreflight,
    GenerationStartPreflightError,
)
from application.engine.dag.engine import DAGEngine
from application.engine.dag.models import get_default_dag
from infrastructure.persistence.database.chapter_candidate_repository import (
    CandidateGateError,
    ChapterCandidateRepository,
)
from infrastructure.persistence.database.outline_contract_repository import OutlineContractRepository
from interfaces.api import dependencies as api_dependencies


router = APIRouter(prefix="/generation", tags=["candidate-generation"])


class StartGenerationRequest(BaseModel):
    run_mode: str = "continuous"
    target_chapters: int = Field(..., ge=1, le=100000)


class ContentRequest(BaseModel):
    content: str = Field(..., min_length=1)
    feedback: str = ""


class CommitPlanRequest(BaseModel):
    commit_plan: dict[str, Any] = Field(default_factory=dict)


class ApprovalRequest(BaseModel):
    continue_after_commit: bool = True


class RegenerateRequest(BaseModel):
    feedback: str = ""


def get_candidate_repository() -> ChapterCandidateRepository:
    return ChapterCandidateRepository(api_dependencies.get_database())


def get_candidate_workflow_service() -> CandidateChapterWorkflowService:
    db = api_dependencies.get_database()
    return CandidateChapterWorkflowService(
        ChapterCandidateRepository(db),
        OutlineContractService(
            contract_repository=OutlineContractRepository(db),
            story_node_repository=api_dependencies.get_story_node_repository(),
        ),
        api_dependencies.get_auto_workflow(),
        api_dependencies.get_chapter_aftermath_pipeline(),
        dag_engine=DAGEngine(),
        dag_factory=get_default_dag,
        semantic_reviewer=api_dependencies.get_chapter_ai_review_service(),
    )


def get_generation_start_preflight(
    repository: ChapterCandidateRepository = Depends(get_candidate_repository),
) -> GenerationStartPreflight:
    db = api_dependencies.get_database()
    return GenerationStartPreflight(
        db,
        repository,
        OutlineContractService(
            contract_repository=OutlineContractRepository(db),
            story_node_repository=api_dependencies.get_story_node_repository(),
        ),
    )


def _run_to_dict(run) -> dict[str, Any]:
    return {
        "novel_id": run.novel_id,
        "run_mode": run.run_mode.value,
        "state": run.state.value,
        "generation_epoch": run.generation_epoch,
        "target_chapters": run.target_chapters,
        "current_formal_chapter": run.current_formal_chapter,
        "current_candidate_id": run.current_candidate_id,
        "current_candidate_chapter": run.current_candidate_chapter,
        "canonical_sync_status": run.canonical_sync_status,
        "next_action": run.next_action,
        "last_error": run.last_error,
        "max_pending_candidates": run.max_pending_candidates,
        "prefetch": run.prefetch,
    }


def _candidate_to_dict(candidate) -> dict[str, Any]:
    return {
        "id": candidate.id,
        "novel_id": candidate.novel_id,
        "chapter_number": candidate.chapter_number,
        "title": candidate.title,
        "generation_epoch": candidate.generation_epoch,
        "status": candidate.status.value,
        "outline_chain": candidate.outline_chain,
        "outline_chain_digest": candidate.outline_chain_digest,
        "llm_content": candidate.llm_content,
        "author_content": candidate.author_content,
        "final_content": candidate.final_content,
        "content_revision": candidate.content_revision,
        "audit_revision": candidate.audit_revision,
        "audit_is_current": candidate.audit_is_current,
        "commit_plan_revision": candidate.commit_plan_revision,
        "commit_plan_is_current": candidate.commit_plan_is_current,
        "audit": candidate.audit,
        "commit_plan": candidate.commit_plan,
        "feedback": candidate.feedback,
        "failure_reason": candidate.failure_reason,
        "continue_after_commit": candidate.continue_after_commit,
        "formal_chapter_id": candidate.formal_chapter_id,
    }


def _raise_candidate_error(exc: Exception) -> None:
    if isinstance(exc, KeyError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, CandidateGateError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, CandidateWorkflowError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, GenerationStartPreflightError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise exc


@router.post("/novels/{novel_id}/start")
def start_generation_run(
    novel_id: str,
    body: StartGenerationRequest,
    repository: ChapterCandidateRepository = Depends(get_candidate_repository),
    preflight: GenerationStartPreflight = Depends(get_generation_start_preflight),
):
    try:
        try:
            mode = RunMode(body.run_mode)
        except ValueError as exc:
            raise HTTPException(status_code=422, detail="run_mode must be continuous or chapter_review") from exc
        preflight.ensure_startable(novel_id)
        run = repository.start_run(novel_id, run_mode=mode, target_chapters=body.target_chapters)
        return {"success": True, "data": _run_to_dict(run)}
    except HTTPException:
        raise
    except Exception as exc:
        _raise_candidate_error(exc)


@router.get("/novels/{novel_id}/state")
def get_generation_state(
    novel_id: str,
    repository: ChapterCandidateRepository = Depends(get_candidate_repository),
):
    try:
        run = repository.get_run(novel_id)
        candidate = repository.get_current_candidate(novel_id)
        return {
            "success": True,
            "data": {**_run_to_dict(run), "candidate": _candidate_to_dict(candidate) if candidate else None},
        }
    except KeyError:
        return {
            "success": True,
            "data": {
                "novel_id": novel_id,
                "run_mode": RunMode.CONTINUOUS.value,
                "state": "idle",
                "generation_epoch": 0,
                "target_chapters": 0,
                "current_formal_chapter": 0,
                "current_candidate_id": None,
                "current_candidate_chapter": None,
                "canonical_sync_status": "ready",
                "next_action": "select_run_mode",
                "last_error": "",
                "max_pending_candidates": 1,
                "prefetch": 0,
                "candidate": None,
            },
        }
    except Exception as exc:
        _raise_candidate_error(exc)


@router.get("/candidates/{candidate_id}")
def get_candidate(
    candidate_id: str,
    repository: ChapterCandidateRepository = Depends(get_candidate_repository),
):
    try:
        return {"success": True, "data": _candidate_to_dict(repository.get_candidate(candidate_id))}
    except Exception as exc:
        _raise_candidate_error(exc)


@router.get("/candidates/{candidate_id}/versions")
def list_candidate_versions(
    candidate_id: str,
    repository: ChapterCandidateRepository = Depends(get_candidate_repository),
):
    """Immutable LLM/author revisions for the review desk comparison view."""

    try:
        return {"success": True, "data": repository.list_versions(candidate_id)}
    except Exception as exc:
        _raise_candidate_error(exc)


def _candidate_dag_continuation(candidate, dag_run: Optional[dict[str, Any]]) -> dict[str, Any]:
    """Describe the only safe next step without reissuing an LLM request."""

    candidate_status = candidate.status.value
    if dag_run is not None and dag_run["status"] == "running":
        return {
            "action": "observe_dag_run",
            "safe_to_start": False,
            "reason": "dag_run_still_running",
        }
    if candidate_status == "awaiting_review":
        return {
            "action": "author_review_candidate",
            "safe_to_start": False,
            "reason": "candidate_awaiting_author_review",
        }
    if candidate_status == "failed" and candidate.formal_chapter_id:
        return {
            "action": "retry_canonical_sync",
            "safe_to_start": True,
            "reason": "formal_candidate_sync_failed",
        }
    if candidate_status in {"streaming", "regenerating", "failed", "stale"}:
        return {
            "action": "restart_candidate_generation",
            "safe_to_start": True,
            "reason": "terminal_or_missing_dag_run_requires_a_fresh_candidate_generation",
        }
    return {
        "action": "inspect_candidate",
        "safe_to_start": False,
        "reason": "candidate_state_does_not_support_dag_continuation",
    }


@router.get("/candidates/{candidate_id}/dag-runs/latest")
def get_latest_candidate_dag_run(
    candidate_id: str,
    after_sequence: int = Query(0),
    repository: ChapterCandidateRepository = Depends(get_candidate_repository),
):
    """Read the durable candidate DAG trace after an ordered SQLite cursor."""

    try:
        repository.get_candidate(candidate_id)
        if after_sequence < 0:
            raise ValueError("after_sequence must be greater than or equal to zero")
        return {
            "success": True,
            "data": repository.get_latest_dag_run(
                candidate_id,
                after_sequence=after_sequence,
            ),
        }
    except Exception as exc:
        _raise_candidate_error(exc)


@router.post("/candidates/{candidate_id}/dag-runs/latest/resume")
def resume_latest_candidate_dag_run(
    candidate_id: str,
    repository: ChapterCandidateRepository = Depends(get_candidate_repository),
):
    """Reconcile a client with a durable trace without replaying candidate prose."""

    try:
        candidate = repository.get_candidate(candidate_id)
        dag_run = repository.get_latest_dag_run(candidate_id)
        return {
            "success": True,
            "data": {
                "candidate": _candidate_to_dict(candidate),
                "dag_run": dag_run,
                "continuation": _candidate_dag_continuation(candidate, dag_run),
            },
        }
    except Exception as exc:
        _raise_candidate_error(exc)


@router.patch("/candidates/{candidate_id}/content")
def edit_candidate_content(
    candidate_id: str,
    body: ContentRequest,
    repository: ChapterCandidateRepository = Depends(get_candidate_repository),
):
    try:
        return {"success": True, "data": _candidate_to_dict(repository.edit_content(candidate_id, body.content, feedback=body.feedback))}
    except Exception as exc:
        _raise_candidate_error(exc)


@router.patch("/candidates/{candidate_id}/commit-plan")
def edit_candidate_commit_plan(
    candidate_id: str,
    body: CommitPlanRequest,
    repository: ChapterCandidateRepository = Depends(get_candidate_repository),
):
    try:
        return {"success": True, "data": _candidate_to_dict(repository.update_commit_plan(candidate_id, body.commit_plan))}
    except Exception as exc:
        _raise_candidate_error(exc)


@router.post("/novels/{novel_id}/generate-next")
async def generate_next_candidate(
    novel_id: str,
    service: CandidateChapterWorkflowService = Depends(get_candidate_workflow_service),
):
    """Generate one candidate from a full synced outline chain.

    The caller gets an explicit candidate/review state; it never receives a
    guessed "writing" status nor does this endpoint skip author review.
    """

    try:
        candidate = await service.generate_next(novel_id)
        return {"success": True, "data": _candidate_to_dict(candidate) if candidate else None}
    except Exception as exc:
        _raise_candidate_error(exc)


@router.post("/novels/{novel_id}/run-continuous")
async def run_continuous_generation(
    novel_id: str,
    service: CandidateChapterWorkflowService = Depends(get_candidate_workflow_service),
):
    """Advance until target or the first authoritative pause/error/review gate."""

    try:
        candidates = await service.run_continuously(novel_id)
        return {
            "success": True,
            "data": {
                "candidates": [_candidate_to_dict(candidate) for candidate in candidates],
                "state": _run_to_dict(service.repository.get_run(novel_id)),
            },
        }
    except Exception as exc:
        _raise_candidate_error(exc)


@router.post("/candidates/{candidate_id}/approve-and-commit")
async def approve_and_commit_candidate(
    candidate_id: str,
    body: ApprovalRequest,
    service: CandidateChapterWorkflowService = Depends(get_candidate_workflow_service),
):
    """The only public approval path that also runs canonical aftermath sync."""

    try:
        return {
            "success": True,
            "data": _candidate_to_dict(
                await service.accept_candidate(
                    candidate_id, continue_after_commit=body.continue_after_commit
                )
            ),
        }
    except Exception as exc:
        _raise_candidate_error(exc)


@router.post("/candidates/{candidate_id}/regenerate")
async def regenerate_candidate(
    candidate_id: str,
    body: RegenerateRequest,
    service: CandidateChapterWorkflowService = Depends(get_candidate_workflow_service),
):
    try:
        return {
            "success": True,
            "data": _candidate_to_dict(
                await service.regenerate_candidate(candidate_id, feedback=body.feedback)
            ),
        }
    except Exception as exc:
        _raise_candidate_error(exc)


@router.post("/candidates/{candidate_id}/re-audit")
async def reaudit_candidate(
    candidate_id: str,
    service: CandidateChapterWorkflowService = Depends(get_candidate_workflow_service),
):
    """Rebuild audit + author-editable commit plan without generating new prose."""

    try:
        return {
            "success": True,
            "data": _candidate_to_dict(await service.reaudit_candidate(candidate_id)),
        }
    except Exception as exc:
        _raise_candidate_error(exc)


@router.post("/candidates/{candidate_id}/retry-sync")
async def retry_candidate_canonical_sync(
    candidate_id: str,
    service: CandidateChapterWorkflowService = Depends(get_candidate_workflow_service),
):
    try:
        return {
            "success": True,
            "data": _candidate_to_dict(await service.retry_canonical_sync(candidate_id)),
        }
    except Exception as exc:
        _raise_candidate_error(exc)


@router.post("/candidates/{candidate_id}/reject")
def reject_candidate_and_stop(
    candidate_id: str,
    repository: ChapterCandidateRepository = Depends(get_candidate_repository),
):
    try:
        return {"success": True, "data": _candidate_to_dict(repository.reject_and_stop(candidate_id))}
    except Exception as exc:
        _raise_candidate_error(exc)


@router.post("/novels/{novel_id}/stop")
def stop_generation_run(
    novel_id: str,
    repository: ChapterCandidateRepository = Depends(get_candidate_repository),
):
    try:
        return {"success": True, "data": _run_to_dict(repository.stop_run(novel_id))}
    except Exception as exc:
        _raise_candidate_error(exc)
