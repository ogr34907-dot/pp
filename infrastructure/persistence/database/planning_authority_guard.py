"""Runtime guards for the book-level planning authority.

The manifest Head is deliberately queried at the write boundary. This keeps
legacy repositories and direct application SQL fail-closed after cutover,
while leaving read-only compatibility projections available to existing
consumers.
"""

from __future__ import annotations

import sqlite3
from typing import Optional


class PlanningAuthorityError(RuntimeError):
    """Raised when a legacy planning writer bypasses a manifest transaction."""


class ProjectionWriteCapability:
    """Retired compatibility type for a withdrawn projection API."""

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("ProjectionWriteCapability is disabled")


def _begin_projection_writer_session(
    conn: sqlite3.Connection,
) -> object:
    """Projection sessions are unavailable without a sealed physical batch."""

    del conn
    raise PlanningAuthorityError("StoryNode projection permits are disabled")


def _end_projection_writer_session(
    conn: sqlite3.Connection,
    session: object,
) -> None:
    del conn, session


def _mint_projection_capability(
    conn: sqlite3.Connection,
    *,
    novel_id: str,
    plan_revision_id: str,
    designated_operation: str,
    authority_generation: int,
    projection_generation: int,
    expected_active_plan_revision_id: Optional[str],
    expected_active_plan_digest: str,
    _session: Optional[object] = None,
) -> ProjectionWriteCapability:
    """Fail closed until the sealed projection declaration exists."""

    del (
        conn,
        novel_id,
        plan_revision_id,
        designated_operation,
        authority_generation,
        projection_generation,
        expected_active_plan_revision_id,
        expected_active_plan_digest,
        _session,
    )
    raise PlanningAuthorityError("StoryNode projection permits are disabled")


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?",
            (table,),
        ).fetchone()
    except sqlite3.OperationalError:
        return False
    return row is not None


def _value(row: object, key: str, index: int = 0) -> object:
    if row is None:
        return None
    try:
        return row[key]  # type: ignore[index]
    except (KeyError, IndexError, TypeError):
        return row[index]  # type: ignore[index]


def manifest_head(
    conn: sqlite3.Connection, novel_id: str
) -> Optional[dict[str, object]]:
    """Return the current manifest Head, or ``None`` for legacy/unmigrated DBs."""

    head = _raw_planning_head(conn, novel_id)
    if head is None or head["authority_mode"] != "manifest":
        return None
    return head


def _raw_planning_head(
    conn: sqlite3.Connection, novel_id: str
) -> Optional[dict[str, object]]:
    if not _table_exists(conn, "outline_planning_heads"):
        return None
    row = conn.execute(
        "SELECT * FROM outline_planning_heads WHERE novel_id = ?",
        (novel_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "novel_id": str(_value(row, "novel_id") or novel_id),
        "authority_mode": str(_value(row, "authority_mode", 1) or "legacy"),
        "authority_generation": int(_value(row, "authority_generation", 2) or 0),
        "active_plan_revision_id": _value(row, "active_plan_revision_id", 3),
        "active_plan_digest": str(_value(row, "active_plan_digest", 4) or ""),
        "projection_generation": int(_value(row, "projection_generation", 6) or 0),
    }


def is_manifest_authority(conn: sqlite3.Connection, novel_id: str) -> bool:
    return manifest_head(conn, novel_id) is not None


def _validate_projection_capability(
    conn: sqlite3.Connection,
    novel_id: str,
    capability: ProjectionWriteCapability,
) -> None:
    """No capability can authorize mutable StoryNode planning writes today."""

    del conn, novel_id, capability
    raise PlanningAuthorityError("StoryNode projection permits are disabled")


def assert_story_node_write_allowed(
    conn: sqlite3.Connection,
    novel_id: str,
    *,
    operation: str,
    capability: Optional[ProjectionWriteCapability] = None,
) -> None:
    """Reject protected StoryNode writes unless the writer owns the transaction."""

    if capability is not None:
        _validate_projection_capability(conn, novel_id, capability)
        return
    if not is_manifest_authority(conn, novel_id):
        return
    raise PlanningAuthorityError(
        f"manifest planning authority forbids legacy StoryNode write: {operation}"
    )


def assert_legacy_planning_mutation_allowed(
    conn: sqlite3.Connection,
    novel_id: str,
    *,
    operation: str,
) -> None:
    """Reject node-level OutlineContract mutators after manifest cutover."""

    if is_manifest_authority(conn, novel_id):
        raise PlanningAuthorityError(
            f"manifest planning authority requires PlanRevision transaction: {operation}"
        )
