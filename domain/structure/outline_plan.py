"""Book-level identities for the five-level outline planning authority."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
import hashlib
import json
from typing import Any, Iterable, Mapping, Optional

from domain.structure.outline_contract import OutlineLevel
from domain.structure.outline_continuity import ContinuityReviewState


class PlanningAuthorityMode(str, Enum):
    LEGACY = "legacy"
    MANIFEST = "manifest"


class PlanRevisionStatus(str, Enum):
    DRAFT = "draft"
    GENERATING = "generating"
    VALIDATING = "validating"
    READY_FOR_REVIEW = "ready_for_review"
    PUBLISHED = "published"
    ARCHIVED = "archived"
    FAILED = "failed"
    STALE = "stale"


class PlanReconciliationStatus(str, Enum):
    ALIGNED = "aligned"
    REPAIRABLE = "repairable"
    AUTHOR_DECISION_REQUIRED = "author_decision_required"


class OutlineExpansionRequired(ValueError):
    """The next chapter is not planned yet and needs a sibling cohort."""


class BackfillStatus(str, Enum):
    MIGRATED = "migrated"
    ALREADY_BACKFILLED = "already_backfilled"
    PLANNING_MIGRATION_REQUIRED = "planning_migration_required"


@dataclass(frozen=True)
class CanonicalPrefix:
    """Deterministic formal-history prefix plus independent readiness gates."""

    novel_id: str
    requested_through: int
    formal_head: int
    digest: str
    identities: tuple[Mapping[str, Any], ...] = ()
    formal_ready: bool = False
    canonical_ready: bool = False
    memory_ready: bool = False
    blockers: tuple[str, ...] = ()

    @property
    def ready(self) -> bool:
        return self.formal_ready and self.canonical_ready and self.memory_ready


@dataclass(frozen=True)
class PlanReconciliationReport:
    """Comparison of a sealed plan boundary against persisted history."""

    plan_revision_id: str
    status: PlanReconciliationStatus
    expected_formal_head: int
    actual_formal_head: int
    expected_prefix_digest: str
    actual_prefix_digest: str
    canonical_ready: bool
    memory_ready: bool
    blockers: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanningHead:
    novel_id: str
    authority_mode: PlanningAuthorityMode = PlanningAuthorityMode.LEGACY
    authority_generation: int = 0
    active_plan_revision_id: Optional[str] = None
    active_plan_digest: str = ""
    working_plan_revision_id: Optional[str] = None
    projection_generation: int = 0
    auto_publish_repairable: bool = False

    def __post_init__(self) -> None:
        if isinstance(self.authority_mode, str):
            object.__setattr__(
                self,
                "authority_mode",
                PlanningAuthorityMode(self.authority_mode),
            )


@dataclass(frozen=True)
class OutlinePlanItem:
    logical_node_id: str
    version_id: str
    version_digest: str
    level: OutlineLevel
    sibling_index: int
    parent_logical_node_id: Optional[str] = None
    expansion_state: str = "unexpanded"
    validated_parent_digest: str = ""
    validated_previous_sibling_digest: str = ""
    is_reused: bool = False
    id: str = field(default="", compare=False)

    def __post_init__(self) -> None:
        if isinstance(self.level, str):
            object.__setattr__(self, "level", OutlineLevel(self.level))
        if self.sibling_index < 0:
            raise ValueError("sibling_index must be non-negative")


@dataclass(frozen=True)
class OutlinePlanRevision:
    id: str
    novel_id: str
    revision: int
    status: PlanRevisionStatus
    digest: str
    canonical_prefix_digest: str
    items: tuple[OutlinePlanItem, ...]
    parent_plan_revision_id: Optional[str] = None
    base_plan_digest: str = ""
    replan_start_chapter: Optional[int] = None
    canonical_boundary: Mapping[str, Any] | None = None
    reconciliation_status: PlanReconciliationStatus = (
        PlanReconciliationStatus.ALIGNED
    )
    reconciliation_report: Mapping[str, Any] | None = None
    author_intent: str = ""
    created_by: str = "system"
    publish_idempotency_key: str = ""
    narrative_review_state: ContinuityReviewState = ContinuityReviewState.NOT_REQUIRED
    narrative_review_receipt: Mapping[str, Any] | None = None
    created_at: str = ""
    updated_at: str = ""
    sealed_at: Optional[str] = None

    def __post_init__(self) -> None:
        if isinstance(self.status, str):
            object.__setattr__(self, "status", PlanRevisionStatus(self.status))
        if isinstance(self.reconciliation_status, str):
            object.__setattr__(
                self,
                "reconciliation_status",
                PlanReconciliationStatus(self.reconciliation_status),
            )
        if isinstance(self.narrative_review_state, str):
            object.__setattr__(
                self,
                "narrative_review_state",
                ContinuityReviewState(self.narrative_review_state),
            )
        object.__setattr__(self, "items", tuple(self.items))
        object.__setattr__(self, "canonical_boundary", dict(self.canonical_boundary or {}))
        object.__setattr__(
            self,
            "reconciliation_report",
            dict(self.reconciliation_report or {}),
        )
        object.__setattr__(
            self,
            "narrative_review_receipt",
            dict(self.narrative_review_receipt or {}),
        )


@dataclass(frozen=True)
class BackfillResult:
    status: BackfillStatus
    head: PlanningHead
    plan: Optional[OutlinePlanRevision] = None
    reason: str = ""


def canonical_plan_digest(
    *, canonical_prefix_digest: str, items: Iterable[OutlinePlanItem]
) -> str:
    """Hash exact plan membership and topology independent of input order."""

    level_order = {level: index for index, level in enumerate(OutlineLevel.ordered())}
    ordered = sorted(
        items,
        key=lambda item: (
            level_order[item.level],
            item.parent_logical_node_id or "",
            item.sibling_index,
            item.logical_node_id,
        ),
    )
    payload = {
        "canonical_prefix_digest": canonical_prefix_digest,
        "items": [
            {
                "logical_node_id": item.logical_node_id,
                "version_id": item.version_id,
                "version_digest": item.version_digest,
                "parent_logical_node_id": item.parent_logical_node_id,
                "level": item.level.value,
                "sibling_index": item.sibling_index,
                "expansion_state": item.expansion_state,
                "validated_parent_digest": item.validated_parent_digest,
                "validated_previous_sibling_digest": (
                    item.validated_previous_sibling_digest
                ),
                "is_reused": item.is_reused,
            }
            for item in ordered
        ],
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def canonical_history_digest(identities: Iterable[Mapping[str, Any]]) -> str:
    """Hash only stable formal/canonical identities, never runtime epochs."""

    ordered = sorted(
        (dict(identity) for identity in identities),
        key=lambda identity: (
            int(identity.get("chapter_number") or 0),
            str(identity.get("chapter_id") or ""),
        ),
    )
    encoded = json.dumps(ordered, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()
