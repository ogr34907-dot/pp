"""Read the durable canonical aftermath state used by autopilot status."""
import asyncio
from dataclasses import dataclass
import hashlib
import logging
from typing import Any, Dict, Optional

from application.ai.llm_retry_policy import is_retryable_llm_error
from application.world.services.chapter_narrative_sync import (
    CHAPTER_NARRATIVE_PIPELINE_VERSION,
)
from infrastructure.persistence.database.sqlite_chapter_narrative_commit_repository import (
    SqliteChapterNarrativeCommitRepository,
)


logger = logging.getLogger(__name__)

_CANONICAL_FIELDS = (
    "canonical_aftermath_chapter_number",
    "canonical_aftermath_failure_reason",
)
_CANONICAL_PAUSE_REASON = "canonical_aftermath_not_ready"
_MAX_CANONICAL_ATTEMPTS = 3


@dataclass(frozen=True)
class CanonicalAftermathStatus:
    chapter_number: Optional[int] = None
    failure_reason: str = ""


@dataclass(frozen=True)
class CanonicalAftermathRecoveryResult:
    disposition: str
    chapter_number: Optional[int] = None
    content_sha256: str = ""
    content_revision: int = 0
    failure_reason: str = ""
    recovery_marker: str = ""


@dataclass(frozen=True)
class _CanonicalRecoverySource:
    chapter_number: int
    content: str
    content_sha256: str
    content_revision: int
    status: str
    attempt_count: int
    failure_reason: str


def _automatic_recovery_marker(source: _CanonicalRecoverySource) -> str:
    return (
        "canonical_aftermath_auto_recovery:v1:"
        f"{source.chapter_number}:{source.content_revision}:"
        f"{source.content_sha256}:{CHAPTER_NARRATIVE_PIPELINE_VERSION}"
    )


def _latest_recovery_source(
    novel_id: str,
    *,
    database: Any,
) -> Optional[_CanonicalRecoverySource]:
    row = database.fetch_one(
        """
        SELECT chapters.number, chapters.content, chapters.content_sha256,
               chapters.content_revision, commits.status, commits.attempt_count,
               commits.failure_reason
        FROM chapters
        LEFT JOIN chapter_narrative_commits AS commits
          ON commits.novel_id = chapters.novel_id
         AND commits.chapter_number = chapters.number
         AND commits.content_sha256 = chapters.content_sha256
         AND commits.content_revision = chapters.content_revision
         AND commits.pipeline_version = ?
        WHERE chapters.novel_id = ? AND chapters.status = 'completed'
        ORDER BY chapters.number DESC
        LIMIT 1
        """,
        (CHAPTER_NARRATIVE_PIPELINE_VERSION, novel_id),
    )
    if row is None:
        return None

    content = str(row["content"] or "")
    content_sha256 = str(row["content_sha256"] or "")
    actual_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    content_revision = int(row["content_revision"] or 0)
    if not content.strip() or content_sha256 != actual_sha256 or content_revision < 1:
        return _CanonicalRecoverySource(
            chapter_number=int(row["number"]),
            content=content,
            content_sha256=content_sha256,
            content_revision=content_revision,
            status="source_version_mismatch",
            attempt_count=0,
            failure_reason="source_version_mismatch",
        )
    return _CanonicalRecoverySource(
        chapter_number=int(row["number"]),
        content=content,
        content_sha256=content_sha256,
        content_revision=content_revision,
        status=str(row["status"] or ""),
        attempt_count=int(row["attempt_count"] or 0),
        failure_reason=str(row["failure_reason"] or ""),
    )


def _restore_terminal_failure_safely(
    commits: SqliteChapterNarrativeCommitRepository,
    *,
    novel_id: str,
    source: _CanonicalRecoverySource,
    failure_reason: str,
) -> None:
    try:
        commits.restore_terminal_failure(
            novel_id=novel_id,
            chapter_number=source.chapter_number,
            content_sha256=source.content_sha256,
            pipeline_version=CHAPTER_NARRATIVE_PIPELINE_VERSION,
            content_revision=source.content_revision,
            failure_reason=failure_reason,
        )
    except Exception as exc:
        logger.warning(
            "恢复规范终态失败记录失败 novel=%s ch=%s: %s",
            novel_id,
            source.chapter_number,
            exc,
        )


async def _run_claimed_canonical_recovery(
    *,
    novel_id: str,
    source: _CanonicalRecoverySource,
    commits: SqliteChapterNarrativeCommitRepository,
    aftermath_pipeline: Any,
    recovery_marker: str,
    failure_prefix: str,
) -> CanonicalAftermathRecoveryResult:
    base = dict(
        chapter_number=source.chapter_number,
        content_sha256=source.content_sha256,
        content_revision=source.content_revision,
        failure_reason=source.failure_reason,
        recovery_marker=recovery_marker,
    )
    try:
        result = await aftermath_pipeline.run_after_chapter_saved(
            novel_id,
            source.chapter_number,
            source.content,
            expected_content_sha256=source.content_sha256,
            expected_content_revision=source.content_revision,
        )
    except asyncio.CancelledError:
        _restore_terminal_failure_safely(
            commits,
            novel_id=novel_id,
            source=source,
            failure_reason=f"{failure_prefix}_cancelled",
        )
        raise
    except Exception as exc:
        failure_reason = f"{failure_prefix}_failed:{exc}"
        _restore_terminal_failure_safely(
            commits,
            novel_id=novel_id,
            source=source,
            failure_reason=failure_reason,
        )
        return CanonicalAftermathRecoveryResult(
            "failed",
            **{**base, "failure_reason": failure_reason},
        )

    try:
        recovered = commits.is_current_version_ready(
            novel_id=novel_id,
            chapter_number=source.chapter_number,
            pipeline_version=CHAPTER_NARRATIVE_PIPELINE_VERSION,
            require_memory_sync=True,
        )
    except Exception as exc:
        failure_reason = f"{failure_prefix}_failed:{exc}"
        _restore_terminal_failure_safely(
            commits,
            novel_id=novel_id,
            source=source,
            failure_reason=failure_reason,
        )
        return CanonicalAftermathRecoveryResult(
            "failed",
            **{**base, "failure_reason": failure_reason},
        )
    if recovered:
        return CanonicalAftermathRecoveryResult(
            "recovered",
            **{**base, "failure_reason": ""},
        )

    failure_reason = f"{failure_prefix}_failed"
    if isinstance(result, dict):
        failure_reason = str(
            result.get("failure_reason")
            or result.get("memory_failure_reason")
            or failure_reason
        )
    _restore_terminal_failure_safely(
        commits,
        novel_id=novel_id,
        source=source,
        failure_reason=failure_reason,
    )
    return CanonicalAftermathRecoveryResult(
        "failed",
        **{**base, "failure_reason": failure_reason},
    )


async def attempt_automatic_canonical_aftermath_recovery(
    *,
    novel_id: str,
    database: Any,
    aftermath_pipeline: Any,
) -> CanonicalAftermathRecoveryResult:
    """Run at most one extra three-attempt cycle for a transient exact version."""
    if database is None or aftermath_pipeline is None:
        return CanonicalAftermathRecoveryResult("unavailable")

    try:
        source = _latest_recovery_source(novel_id, database=database)
    except Exception as exc:
        logger.debug("读取自动规范恢复源失败 novel=%s: %s", novel_id, exc)
        return CanonicalAftermathRecoveryResult(
            "unavailable",
            failure_reason=str(exc),
        )
    if source is None:
        return CanonicalAftermathRecoveryResult("no_completed_chapter")

    marker = _automatic_recovery_marker(source)
    base = dict(
        chapter_number=source.chapter_number,
        content_sha256=source.content_sha256,
        content_revision=source.content_revision,
        failure_reason=source.failure_reason,
        recovery_marker=marker,
    )
    commits = SqliteChapterNarrativeCommitRepository(database)
    try:
        already_ready = commits.is_current_version_ready(
            novel_id=novel_id,
            chapter_number=source.chapter_number,
            pipeline_version=CHAPTER_NARRATIVE_PIPELINE_VERSION,
            require_memory_sync=True,
        )
    except Exception as exc:
        logger.debug("核验自动规范恢复状态失败 novel=%s: %s", novel_id, exc)
        return CanonicalAftermathRecoveryResult(
            "unavailable",
            **{**base, "failure_reason": str(exc)},
        )
    if already_ready:
        return CanonicalAftermathRecoveryResult("ready", **base)
    if source.status != "failed" or source.attempt_count < _MAX_CANONICAL_ATTEMPTS:
        return CanonicalAftermathRecoveryResult("not_terminal", **base)
    if not is_retryable_llm_error(source.failure_reason):
        return CanonicalAftermathRecoveryResult("not_retryable", **base)

    try:
        claim = commits.claim_bounded_automatic_recovery(
            novel_id=novel_id,
            chapter_number=source.chapter_number,
            content_sha256=source.content_sha256,
            pipeline_version=CHAPTER_NARRATIVE_PIPELINE_VERSION,
            content_revision=source.content_revision,
            expected_failure_reason=source.failure_reason,
            recovery_marker=marker,
        )
    except Exception as exc:
        logger.warning("领取自动规范恢复失败 novel=%s ch=%s: %s", novel_id, source.chapter_number, exc)
        return CanonicalAftermathRecoveryResult(
            "conflict",
            **{**base, "failure_reason": str(exc)},
        )
    if claim != "claimed":
        disposition = "exhausted" if claim == "exhausted" else claim
        return CanonicalAftermathRecoveryResult(disposition, **base)

    return await _run_claimed_canonical_recovery(
        novel_id=novel_id,
        source=source,
        commits=commits,
        aftermath_pipeline=aftermath_pipeline,
        recovery_marker=marker,
        failure_prefix="canonical_aftermath_auto_recovery",
    )


async def attempt_manual_canonical_aftermath_recovery(
    *,
    novel_id: str,
    database: Any,
    aftermath_pipeline: Any,
) -> CanonicalAftermathRecoveryResult:
    """Explicitly retry the latest exact terminal version without resuming prose."""
    if database is None or aftermath_pipeline is None:
        return CanonicalAftermathRecoveryResult("unavailable")
    try:
        source = _latest_recovery_source(novel_id, database=database)
    except Exception as exc:
        return CanonicalAftermathRecoveryResult(
            "unavailable",
            failure_reason=str(exc),
        )
    if source is None:
        return CanonicalAftermathRecoveryResult("no_completed_chapter")

    base = dict(
        chapter_number=source.chapter_number,
        content_sha256=source.content_sha256,
        content_revision=source.content_revision,
        failure_reason=source.failure_reason,
        recovery_marker="",
    )
    commits = SqliteChapterNarrativeCommitRepository(database)
    try:
        if commits.is_current_version_ready(
            novel_id=novel_id,
            chapter_number=source.chapter_number,
            pipeline_version=CHAPTER_NARRATIVE_PIPELINE_VERSION,
            require_memory_sync=True,
        ):
            return CanonicalAftermathRecoveryResult("ready", **base)
        reclaimed = commits.reclaim_terminal_failure(
            novel_id=novel_id,
            chapter_number=source.chapter_number,
            content_sha256=source.content_sha256,
            pipeline_version=CHAPTER_NARRATIVE_PIPELINE_VERSION,
            content_revision=source.content_revision,
        )
    except Exception as exc:
        return CanonicalAftermathRecoveryResult(
            "unavailable",
            **{**base, "failure_reason": str(exc)},
        )
    if not reclaimed:
        return CanonicalAftermathRecoveryResult("not_terminal", **base)

    return await _run_claimed_canonical_recovery(
        novel_id=novel_id,
        source=source,
        commits=commits,
        aftermath_pipeline=aftermath_pipeline,
        recovery_marker="",
        failure_prefix="canonical_aftermath_manual_recovery",
    )


def resolve_canonical_aftermath_status(
    novel_id: str,
    *,
    database: Any = None,
) -> Optional[CanonicalAftermathStatus]:
    """Return the terminal failure for the latest completed durable version.

    ``None`` means the durable store could not be read, so callers must retain
    their shared-state view. An empty status is a successful durable read with
    no terminal canonical failure.
    """
    try:
        from infrastructure.persistence.database.connection import get_database

        db = database if database is not None else get_database()
        chapter = db.fetch_one(
            """
            SELECT number
            FROM chapters
            WHERE novel_id = ? AND status = 'completed'
            ORDER BY number DESC
            LIMIT 1
            """,
            (novel_id,),
        )
        if chapter is None:
            return CanonicalAftermathStatus()

        chapter_number = int(chapter["number"])
        commits = SqliteChapterNarrativeCommitRepository(db)
        if commits.is_current_version_ready(
            novel_id=novel_id,
            chapter_number=chapter_number,
            pipeline_version=CHAPTER_NARRATIVE_PIPELINE_VERSION,
            require_memory_sync=True,
        ):
            return CanonicalAftermathStatus()

        failure = db.fetch_one(
            """
            SELECT CASE
                     WHEN commits.status = 'failed' THEN commits.failure_reason
                     ELSE commits.memory_failure_reason
                   END AS failure_reason
            FROM chapter_narrative_commits AS commits
            JOIN chapters AS chapters
              ON chapters.novel_id = commits.novel_id
             AND chapters.number = commits.chapter_number
            WHERE commits.novel_id = ?
              AND commits.chapter_number = ?
              AND commits.content_sha256 = chapters.content_sha256
              AND commits.content_revision = chapters.content_revision
              AND commits.pipeline_version = ?
              AND (
                    (commits.status = 'failed' AND commits.attempt_count >= ?)
                    OR (
                        commits.status = 'committed'
                        AND commits.memory_status = 'failed'
                        AND commits.memory_attempt_count >= ?
                    )
              )
            LIMIT 1
            """,
            (
                novel_id,
                chapter_number,
                CHAPTER_NARRATIVE_PIPELINE_VERSION,
                _MAX_CANONICAL_ATTEMPTS,
                _MAX_CANONICAL_ATTEMPTS,
            ),
        )
        if failure is None:
            return CanonicalAftermathStatus()
        return CanonicalAftermathStatus(
            chapter_number=chapter_number,
            failure_reason=str(failure["failure_reason"] or _CANONICAL_PAUSE_REASON),
        )
    except Exception as exc:
        logger.debug("读取规范章后状态失败，保留共享状态 novel=%s: %s", novel_id, exc)
        return None


def reconcile_canonical_aftermath_status(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Overlay readable durable canonical state on a shared status payload."""
    novel_id = str(payload.get("novel_id") or "").strip()
    if not novel_id:
        return payload

    durable = resolve_canonical_aftermath_status(novel_id)
    if durable is None:
        return payload

    for field in _CANONICAL_FIELDS:
        payload.pop(field, None)
    if payload.get("autopilot_pause_reason") == _CANONICAL_PAUSE_REASON:
        payload["autopilot_pause_reason"] = ""

    if durable.chapter_number is not None:
        payload["canonical_aftermath_chapter_number"] = durable.chapter_number
        payload["canonical_aftermath_failure_reason"] = durable.failure_reason
        payload["autopilot_pause_reason"] = _CANONICAL_PAUSE_REASON
    return payload
