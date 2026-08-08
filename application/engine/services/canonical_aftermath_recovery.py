"""Read the durable canonical aftermath state used by autopilot status."""
from dataclasses import dataclass
import logging
from typing import Any, Dict, Optional

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
