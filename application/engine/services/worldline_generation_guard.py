"""Generation-epoch barrier for vector reads and writes after worldline reset."""

from __future__ import annotations

from typing import Any, Mapping, Optional


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
    except Exception:
        return 0


def tag_payload_for_active_epoch(novel_id: str, payload: Mapping[str, Any], db: Optional[Any] = None) -> dict[str, Any]:
    """Copy a vector payload and attach its current active epoch."""

    tagged = dict(payload)
    tagged["generation_epoch"] = active_generation_epoch(novel_id, db)
    return tagged
