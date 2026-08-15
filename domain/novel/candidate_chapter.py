"""Candidate prose and server-authoritative dual-mode generation state."""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Optional


class RunMode(str, Enum):
    CONTINUOUS = "continuous"
    CHAPTER_REVIEW = "chapter_review"


class GenerationRunState(str, Enum):
    IDLE = "idle"
    RUNNING = "running"
    WAITING_PLANNING = "waiting_planning"
    WAITING_REVIEW = "waiting_review"
    PAUSED = "paused"
    STOPPED = "stopped"
    COMPLETED = "completed"
    ERROR = "error"


class CandidateStatus(str, Enum):
    STREAMING = "streaming"
    AUDITING = "auditing"
    AWAITING_REVIEW = "awaiting_review"
    COMMITTING = "committing"
    SYNCING = "syncing"
    COMMITTED = "committed"
    STALE = "stale"
    REGENERATING = "regenerating"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"


@dataclass(frozen=True)
class GenerationRun:
    novel_id: str
    run_mode: RunMode
    state: GenerationRunState
    generation_epoch: int
    target_chapters: int
    current_formal_chapter: int = 0
    current_candidate_id: Optional[str] = None
    current_candidate_chapter: Optional[int] = None
    canonical_sync_status: str = "ready"
    next_action: str = ""
    last_error: str = ""
    max_pending_candidates: int = 1
    prefetch: int = 0


@dataclass(frozen=True)
class ChapterCandidate:
    id: str
    novel_id: str
    chapter_number: int
    title: str
    generation_epoch: int
    status: CandidateStatus
    outline_chain: dict[str, Any] = field(default_factory=dict)
    outline_chain_digest: str = ""
    planning_authority_generation: int = 0
    plan_revision_id: Optional[str] = None
    plan_digest: str = ""
    chapter_outline_digest: str = ""
    plan_pin_fingerprint: str = ""
    llm_content: str = ""
    author_content: Optional[str] = None
    content_revision: int = 0
    audit_revision: int = 0
    commit_plan_revision: int = 0
    commit_plan_content_revision: int = 0
    audit: dict[str, Any] = field(default_factory=dict)
    commit_plan: dict[str, Any] = field(default_factory=dict)
    feedback: str = ""
    failure_reason: str = ""
    continue_after_commit: bool = False
    formal_chapter_id: Optional[str] = None

    @property
    def final_content(self) -> str:
        return self.author_content if self.author_content is not None else self.llm_content

    @property
    def audit_is_current(self) -> bool:
        return self.content_revision > 0 and self.audit_revision == self.content_revision

    @property
    def commit_plan_is_current(self) -> bool:
        return self.audit_is_current and self.commit_plan_content_revision == self.content_revision
