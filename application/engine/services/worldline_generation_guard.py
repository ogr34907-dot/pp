"""Generation-epoch barrier for vector reads and writes after worldline reset."""

from __future__ import annotations

from typing import Any, Mapping, Optional


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
    active_generation_epoch(novel_id, db)
    clauses = ["novel_id = ?", "status = 'committed'"]
    params: list[Any] = [novel_id]
    if through_chapter is not None:
        clauses.append("chapter_number <= ?")
        params.append(int(through_chapter))
    try:
        rows = db.fetch_all(
            "SELECT DISTINCT chapter_number FROM chapter_narrative_commits "
            f"WHERE {' AND '.join(clauses)}",
            tuple(params),
        )
        return {int(row["chapter_number"]) for row in rows}
    except Exception as exc:
        raise GenerationEpochUnavailableError(
            f"canonical_visibility_unavailable: {exc}"
        ) from exc


def tag_payload_for_active_epoch(novel_id: str, payload: Mapping[str, Any], db: Optional[Any] = None) -> dict[str, Any]:
    """Copy a vector payload and attach its current active epoch."""

    tagged = dict(payload)
    tagged["generation_epoch"] = active_generation_epoch(novel_id, db)
    return tagged
