"""Continuity-review status, execution, and author confirmation endpoints."""

from __future__ import annotations

from typing import Any, Mapping, Optional

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from application.blueprint.services.outline_continuity_review_service import (
    OutlineContinuityReviewService,
)
from domain.structure.outline_continuity import ContinuityReviewState
from domain.structure.outline_contract import OutlinePayload, OutlineSource
from infrastructure.persistence.database.outline_continuity_review_repository import (
    OutlineContinuityReviewRepository,
)
from infrastructure.persistence.database.outline_contract_repository import (
    NarrativeConfirmationRequired,
    OutlineContractRepository,
    OutlineGateError,
)
from interfaces.api import dependencies as api_dependencies


router = APIRouter(prefix="/outline", tags=["outline-continuity-review"])


class ContinuityReviewRequest(BaseModel):
    parent_logical_node_id: str = Field(..., min_length=1)
    expected_plan_digest: str = Field(..., min_length=1)
    force: bool = False


class ContinuityReceiptRequest(BaseModel):
    expected_plan_digest: str = Field(..., min_length=1)
    state: str = "acknowledged"
    review_ids: list[str] = Field(default_factory=list)
    scope_fingerprints: list[str] = Field(default_factory=list)
    actor: str = "author"
    reason: str = ""
    idempotency_key: str = ""


class ContinuitySuggestionApplyRequest(BaseModel):
    logical_node_id: str = Field(..., min_length=1)
    expected_plan_digest: str = Field(..., min_length=1)
    expected_version_digest: str = Field(..., min_length=1)
    scope_fingerprint: str = Field(..., min_length=1)


def _dependencies() -> tuple[OutlineContractRepository, OutlineContinuityReviewRepository]:
    db = api_dependencies.get_database()
    return OutlineContractRepository(db), OutlineContinuityReviewRepository(db)


def get_outline_continuity_review_service() -> OutlineContinuityReviewService:
    db = api_dependencies.get_database()
    repository = OutlineContractRepository(db)
    return OutlineContinuityReviewService(
        repository,
        OutlineContinuityReviewRepository(db),
        api_dependencies.get_llm_service(),
        db,
    )


def _run_view(run: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
    if run is None:
        return None
    return dict(run)


def _status(
    service: OutlineContinuityReviewService,
    plan_revision_id: str,
    parent_logical_node_id: Optional[str] = None,
) -> dict[str, Any]:
    repository = service.repository
    reviews = service.review_repository
    plan = repository.get_plan_revision(plan_revision_id)
    latest = reviews.latest(
        plan_revision_id=plan_revision_id,
        scope_parent_logical_node_id=parent_logical_node_id,
    )
    current = None
    if latest is not None:
        scope_parent = parent_logical_node_id or str(
            latest.get("scope_parent_logical_node_id") or ""
        )
        try:
            current = service.current(plan_revision_id, scope_parent, plan.digest)
        except (KeyError, ValueError):
            # A damaged historical snapshot can still expose its latest row,
            # but valid plans always resolve currentness from fresh context.
            current = reviews.current(
                plan_revision_id=plan_revision_id,
                plan_digest=plan.digest,
                scope_fingerprint=str(latest.get("scope_fingerprint") or ""),
            )
    return {
        "plan_revision_id": plan.id,
        "plan_digest": plan.digest,
        "state": plan.narrative_review_state.value,
        "receipt": dict(plan.narrative_review_receipt or {}),
        "latest": _run_view(latest),
        "current": _run_view(current),
        "technical_blockers": list(
            repository.technical_blockers_for_plan(plan.id)
        ),
    }


def _raise(exc: Exception) -> None:
    if isinstance(exc, NarrativeConfirmationRequired):
        raise HTTPException(status_code=409, detail=exc.detail) from exc
    if isinstance(exc, KeyError):
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    if isinstance(exc, OutlineGateError):
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if isinstance(exc, ValueError):
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    raise exc


@router.get("/plan-revisions/{plan_revision_id}/continuity-review")
@router.get("/plan-revisions/{plan_revision_id}/continuity-reviews/latest")
def get_continuity_review_status(
    plan_revision_id: str,
    parent_logical_node_id: Optional[str] = Query(default=None),
    service: OutlineContinuityReviewService = Depends(
        get_outline_continuity_review_service
    ),
):
    try:
        return {
            "success": True,
            "data": _status(service, plan_revision_id, parent_logical_node_id),
        }
    except Exception as exc:
        _raise(exc)


@router.post("/plan-revisions/{plan_revision_id}/continuity-review")
@router.post("/plan-revisions/{plan_revision_id}/continuity-reviews")
async def request_continuity_review(
    plan_revision_id: str,
    body: ContinuityReviewRequest,
    service: OutlineContinuityReviewService = Depends(get_outline_continuity_review_service),
):
    try:
        run = await service.review(
            plan_revision_id=plan_revision_id,
            parent_logical_node_id=body.parent_logical_node_id,
            expected_plan_digest=body.expected_plan_digest,
            force=body.force,
        )
        return {
            "success": True,
            "data": {
                **_status(service, plan_revision_id, body.parent_logical_node_id),
                "run": run,
            },
        }
    except Exception as exc:
        _raise(exc)


_SUGGESTION_PATCH_FIELDS = frozenset(
    {
        "title",
        "narrative_text",
        "creative_goal",
        "entry_state",
        "exit_state",
        "required_events",
        "forbidden_events",
        "state_changes",
        "foreshadowing",
        "handoff_conditions",
        "pov",
        "scenes",
        "beats",
        "conflicts",
        "ending_hook",
        "word_budget",
    }
)


def _is_locked_field(payload: Mapping[str, Any], field: str) -> bool:
    extra = payload.get("extra")
    extra = extra if isinstance(extra, Mapping) else {}
    provenance = extra.get("_field_provenance")
    provenance = provenance if isinstance(provenance, Mapping) else {}
    rule = provenance.get(field)
    if isinstance(rule, Mapping) and bool(rule.get("locked")):
        return True
    locks = extra.get("field_locks")
    return isinstance(locks, Mapping) and bool(locks.get(field))


@router.post("/continuity-reviews/{review_id}/suggestions/{suggestion_id}/apply")
def apply_continuity_suggestion(
    review_id: str,
    suggestion_id: str,
    body: ContinuitySuggestionApplyRequest,
    service: OutlineContinuityReviewService = Depends(
        get_outline_continuity_review_service
    ),
):
    repository = service.repository
    reviews = service.review_repository
    try:
        run = reviews.get(review_id)
        if run["state"] != "succeeded":
            raise OutlineGateError("continuity review is not complete")
        plan = repository.get_plan_revision(str(run["plan_revision_id"]))
        if plan.digest != body.expected_plan_digest or run["plan_digest"] != plan.digest:
            raise OutlineGateError("continuity review is stale")
        if run["scope_fingerprint"] != body.scope_fingerprint:
            raise OutlineGateError("continuity review is stale")
        current_review = service.current(
            plan.id,
            str(run["scope_parent_logical_node_id"]),
            plan.digest,
        )
        if current_review is None or current_review["id"] != run["id"]:
            raise OutlineGateError("continuity review is stale")
        issues = (run.get("report") or {}).get("issues") or []
        issue = next(
            (
                item
                for item in issues
                if isinstance(item, Mapping) and str(item.get("id") or "") == suggestion_id
            ),
            None,
        )
        if issue is None:
            raise KeyError(f"continuity suggestion not found: {suggestion_id}")
        patch = issue.get("suggested_patch")
        if not isinstance(patch, Mapping):
            raise OutlineGateError("continuity suggestion has no applicable patch")
        patch = dict(patch)
        if set(patch) - _SUGGESTION_PATCH_FIELDS:
            raise OutlineGateError("continuity suggestion changes a protected field")
        if not patch:
            raise OutlineGateError("continuity suggestion has no applicable patch")
        issue_scope = issue.get("scope")
        if isinstance(issue_scope, Mapping):
            scoped_node = str(issue_scope.get("logical_node_id") or "")
            if scoped_node and scoped_node != body.logical_node_id:
                raise OutlineGateError("continuity suggestion belongs to another outline node")
        rows = repository.working_plan_items_with_payload(plan.novel_id)
        current = next(
            (row for row in rows if row["logical_node_id"] == body.logical_node_id),
            None,
        )
        if current is None or current["plan_revision_id"] != plan.id:
            raise KeyError(f"working outline item not found: {body.logical_node_id}")
        payload = dict(current.get("payload") or {})
        if any(_is_locked_field(payload, field) for field in patch):
            raise OutlineGateError("continuity suggestion changes an author-locked field")
        if repository.get_slot(body.logical_node_id).author_locked:
            raise OutlineGateError("continuity suggestion changes an author-locked outline")
        updated = repository.update_working_plan_item(
            plan_revision_id=plan.id,
            logical_node_id=body.logical_node_id,
            payload=OutlinePayload.from_dict({**payload, **patch}),
            expected_plan_digest=body.expected_plan_digest,
            expected_version_digest=body.expected_version_digest,
            source=OutlineSource.AI,
        )
        item = next(
            item for item in updated.items if item.logical_node_id == body.logical_node_id
        )
        return {
            "success": True,
            "data": {
                "plan_revision_id": updated.id,
                "plan_digest": updated.digest,
                "logical_node_id": item.logical_node_id,
                "version_digest": item.version_digest,
            },
        }
    except Exception as exc:
        _raise(exc)


@router.post("/plan-revisions/{plan_revision_id}/continuity-review/acknowledge")
@router.post("/plan-revisions/{plan_revision_id}/continuity-review/apply")
def apply_continuity_receipt(
    plan_revision_id: str,
    body: ContinuityReceiptRequest,
    service: OutlineContinuityReviewService = Depends(
        get_outline_continuity_review_service
    ),
):
    repository = service.repository
    reviews = service.review_repository
    try:
        plan = repository.get_plan_revision(plan_revision_id)
        try:
            state = ContinuityReviewState(body.state)
        except ValueError as exc:
            raise ValueError("state must be pass or acknowledged") from exc
        review_ids = tuple(body.review_ids)
        fingerprints = tuple(body.scope_fingerprints)
        if not review_ids:
            latest = reviews.latest(plan_revision_id=plan_revision_id)
            if latest is not None:
                review_ids = (str(latest["id"]),)
                fingerprints = (str(latest["scope_fingerprint"]),)
        try:
            referenced_reviews = tuple(reviews.get(review_id) for review_id in review_ids)
        except KeyError as exc:
            raise NarrativeConfirmationRequired(
                plan_digest=plan.digest,
                reason_required=True,
            ) from exc
        required_current = service.required_current_reports(
            plan_revision_id, body.expected_plan_digest
        )
        if required_current:
            expected_reviews = tuple(
                report for report in required_current.values() if report is not None
            )
            if (
                len(expected_reviews) != len(required_current)
                or any(
                    str(report["id"]) not in review_ids
                    or str(report["scope_fingerprint"]) not in fingerprints
                    for report in expected_reviews
                )
            ):
                raise NarrativeConfirmationRequired(
                    plan_digest=plan.digest,
                    decisions=tuple(
                        str(review.get("decision") or "")
                        for review in referenced_reviews
                    ),
                    reviews=referenced_reviews,
                    reason_required=any(
                        report is None
                        or str(report.get("decision") or "")
                        in {"conflict", "unavailable"}
                        for report in required_current.values()
                    ),
                )
        if (
            state is ContinuityReviewState.ACKNOWLEDGED
            and (
                not referenced_reviews
                or any(
                    str(review.get("decision") or "")
                    in {"conflict", "unavailable"}
                    for review in referenced_reviews
                )
            )
            and not body.reason.strip()
        ):
            raise ValueError("reason is required for conflict or unavailable narrative review")
        if not body.idempotency_key:
            idempotency_key = f"{plan_revision_id}:{body.expected_plan_digest}:{state.value}"
        else:
            idempotency_key = body.idempotency_key
        conn = repository._connection()
        if conn.in_transaction:
            raise OutlineGateError("narrative receipt requires a clean connection")
        conn.execute("BEGIN IMMEDIATE")
        try:
            override_id = ""
            if state is ContinuityReviewState.ACKNOWLEDGED:
                override = reviews.acknowledge_in_transaction(
                    conn,
                    novel_id=plan.novel_id,
                    plan_revision_id=plan.id,
                    plan_digest=body.expected_plan_digest,
                    action="acknowledge_narrative_risk",
                    idempotency_key=idempotency_key,
                    actor=body.actor,
                    reason=body.reason,
                    require_reason=bool(body.reason.strip()),
                    scope_fingerprints=fingerprints,
                    review_ids=review_ids,
                )
                override_id = str(override["id"])
            updated = repository.set_narrative_review_receipt(
                plan_revision_id=plan.id,
                expected_plan_digest=body.expected_plan_digest,
                state=state,
                receipt={
                    "action": state.value,
                    "actor": body.actor,
                    "reason": body.reason,
                    "override_id": override_id,
                },
                review_ids=review_ids,
                scope_fingerprints=fingerprints,
                _connection=conn,
            )
            conn.commit()
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise
        return {"success": True, "data": _status(service, updated.id)}
    except Exception as exc:
        _raise(exc)
