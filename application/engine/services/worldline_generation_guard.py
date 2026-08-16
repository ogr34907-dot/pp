"""Generation-epoch barrier for vector reads and writes after worldline reset."""

from __future__ import annotations

import hashlib
from typing import Any, Mapping, Optional

from infrastructure.persistence.database.chapter_candidate_repository import (
    CandidateGateError,
    ChapterCandidateRepository,
)


class GenerationEpochUnavailableError(RuntimeError):
    """Raised when the durable worldline barrier cannot be read safely."""


def is_payload_in_active_epoch(payload: Mapping[str, Any], *, active_epoch: int) -> bool:
    """Return whether a vector payload belongs to the active worldline epoch.

    Legacy untagged data is allowed only before the first destructive reset
    (epoch zero).  After a reset it is intentionally invisible until the
    rebuild job writes traceable payloads into the new epoch.
    """

    raw_epoch = payload.get("generation_epoch")
    if raw_epoch is None or raw_epoch == "":
        return active_epoch == 0
    try:
        return int(raw_epoch) == int(active_epoch)
    except (TypeError, ValueError):
        return False


def active_generation_epoch(novel_id: str, db: Optional[Any] = None) -> int:
    """Read the durable retrieval barrier; missing migration is epoch zero."""

    try:
        if db is None:
            from infrastructure.persistence.database.connection import get_database

            db = get_database()
        row = db.fetch_one(
            "SELECT active_generation_epoch FROM worldline_generation_filters WHERE novel_id = ?",
            (novel_id,),
        )
        return int(row["active_generation_epoch"] or 0) if row else 0
    except Exception as exc:
        raise GenerationEpochUnavailableError(
            f"generation_epoch_unavailable: {exc}"
        ) from exc


def visible_committed_chapters(
    novel_id: str,
    db: Optional[Any] = None,
    *,
    through_chapter: Optional[int] = None,
) -> set[int]:
    """Return chapters behind the existing narrative-commit barrier.

    The active worldline epoch is read before querying the commit table so a
    missing/failed barrier never gets interpreted as an empty history.  The
    commit row is the single visibility gate for chapter-derived facts; the
    individual canonical tables do not carry a second pending/provenance state.
    """
    if db is None:
        from infrastructure.persistence.database.connection import get_database

        db = get_database()
    try:
        active_epoch = active_generation_epoch(novel_id, db)
        conn = db.get_connection()
        run = conn.execute(
            """
            SELECT generation_epoch, current_formal_chapter, canonical_sync_status
            FROM novel_generation_runs
            WHERE novel_id = ?
            """,
            (novel_id,),
        ).fetchone()
        if run is not None:
            if (
                int(run["generation_epoch"] or 0) != active_epoch
                or str(run["canonical_sync_status"] or "ready") != "ready"
            ):
                return set()
        elif active_epoch != 0:
            return set()

        # The Candidate repository resolves the only continuous Formal head.
        # An unproven completed tail is reported separately and is deliberately
        # not made visible here.
        formal_head, _unproven_tail = ChapterCandidateRepository(db).formal_history_snapshot(
            novel_id
        )
        formal_head = int(formal_head)
        if formal_head < 1:
            return set()
        if run is not None and int(run["current_formal_chapter"] or 0) != formal_head:
            return set()

        formal_rows = conn.execute(
            """
            SELECT number, content, content_sha256, content_revision, status
            FROM chapters
            WHERE novel_id = ? AND number <= ?
            ORDER BY number
            """,
            (novel_id, formal_head),
        ).fetchall()
        identities: dict[int, tuple[str, int]] = {}
        for expected_number, row in enumerate(formal_rows, start=1):
            content = str(row["content"] or "")
            actual_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            if (
                int(row["number"] or 0) != expected_number
                or str(row["status"] or "") != "completed"
                or not content.strip()
                or str(row["content_sha256"] or "") != actual_hash
                or int(row["content_revision"] or 0) < 1
            ):
                return set()
            identities[expected_number] = (actual_hash, int(row["content_revision"] or 0))
        if len(identities) != formal_head:
            return set()

        from application.world.services.chapter_narrative_sync import (
            CHAPTER_NARRATIVE_PIPELINE_VERSION,
        )

        clauses = [
            "novel_id = ?",
            "status = 'committed'",
            "pipeline_version = ?",
            "chapter_number <= ?",
        ]
        params: list[Any] = [
            novel_id,
            CHAPTER_NARRATIVE_PIPELINE_VERSION,
            formal_head,
        ]
        if through_chapter is not None:
            clauses.append("chapter_number <= ?")
            params.append(int(through_chapter))
        rows = conn.execute(
            "SELECT chapter_number, content_sha256, content_revision "
            "FROM chapter_narrative_commits "
            f"WHERE {' AND '.join(clauses)}",
            tuple(params),
        ).fetchall()
        visible = set()
        for row in rows:
            chapter_number = int(row["chapter_number"] or 0)
            identity = identities.get(chapter_number)
            if identity is None:
                continue
            if (
                str(row["content_sha256"] or "") == identity[0]
                and int(row["content_revision"] or 0) == identity[1]
            ):
                visible.add(chapter_number)
        return visible
    except CandidateGateError:
        return set()
    except Exception as exc:
        raise GenerationEpochUnavailableError(
            f"canonical_visibility_unavailable: {exc}"
        ) from exc


def tag_payload_for_active_epoch(novel_id: str, payload: Mapping[str, Any], db: Optional[Any] = None) -> dict[str, Any]:
    """Copy a vector payload and attach its current active epoch."""

    tagged = dict(payload)
    tagged["generation_epoch"] = active_generation_epoch(novel_id, db)
    return tagged
