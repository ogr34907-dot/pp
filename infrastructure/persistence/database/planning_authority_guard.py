"""Runtime guards for the book-level planning authority.

The manifest Head is deliberately queried at the write boundary.  This keeps
legacy repositories and direct application SQL fail-closed after cutover,
while leaving read-only compatibility projections available to existing
consumers.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import sqlite3
from typing import Optional


class PlanningAuthorityError(RuntimeError):
    """Raised when a legacy planning writer bypasses a manifest transaction."""


class _ProjectionCapabilityToken:
    pass


_PROJECTION_CAPABILITY_TOKEN = _ProjectionCapabilityToken()


@dataclass(frozen=True)
class ProjectionWriteCapability:
    """Opaque capability bound to one connection, book, generation and plan."""

    novel_id: str
    plan_revision_id: str
    authority_generation: int
    connection_identity: int
    _token: object = field(
        default=_PROJECTION_CAPABILITY_TOKEN,
        repr=False,
        compare=False,
    )


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

    if not _table_exists(conn, "outline_planning_heads"):
        return None
    row = conn.execute(
        "SELECT * FROM outline_planning_heads WHERE novel_id = ?",
        (novel_id,),
    ).fetchone()
    if row is None or str(_value(row, "authority_mode", 1) or "legacy") != "manifest":
        return None
    return {
        "novel_id": str(_value(row, "novel_id") or novel_id),
        "authority_generation": int(_value(row, "authority_generation", 2) or 0),
        "active_plan_revision_id": _value(row, "active_plan_revision_id", 3),
    }


def is_manifest_authority(conn: sqlite3.Connection, novel_id: str) -> bool:
    return manifest_head(conn, novel_id) is not None


def _validate_projection_capability(
    conn: sqlite3.Connection,
    novel_id: str,
    capability: ProjectionWriteCapability,
) -> None:
    if capability._token is not _PROJECTION_CAPABILITY_TOKEN:
        raise PlanningAuthorityError("invalid StoryNode projection capability")
    if capability.novel_id != novel_id or capability.connection_identity != id(conn):
        raise PlanningAuthorityError("StoryNode projection capability is out of scope")
    head = manifest_head(conn, novel_id)
    if head is None or int(head["authority_generation"]) != capability.authority_generation:
        raise PlanningAuthorityError("planning Head changed during StoryNode projection")
    if not _table_exists(conn, "outline_plan_revisions"):
        raise PlanningAuthorityError("outline plan manifest is unavailable")
    row = conn.execute(
        """
        SELECT 1 FROM outline_plan_revisions
        WHERE id = ? AND novel_id = ? AND sealed_at IS NOT NULL
        """,
        (capability.plan_revision_id, novel_id),
    ).fetchone()
    if row is None:
        raise PlanningAuthorityError("projection plan is not a sealed revision")


def _issue_projection_capability(
    conn: sqlite3.Connection,
    *,
    novel_id: str,
    plan_revision_id: str,
) -> ProjectionWriteCapability:
    """Issue a capability for ``PlanProjectionWriter`` only.

    The function is intentionally named as an internal boundary.  Callers
    should obtain capabilities through ``PlanProjectionWriter`` so they are
    created on the same SQLite connection as the publish transaction.
    """

    head = manifest_head(conn, novel_id)
    if head is None:
        raise PlanningAuthorityError("projection capability requires manifest authority")
    capability = ProjectionWriteCapability(
        novel_id=novel_id,
        plan_revision_id=plan_revision_id,
        authority_generation=int(head["authority_generation"]),
        connection_identity=id(conn),
    )
    _validate_projection_capability(conn, novel_id, capability)
    return capability


def assert_story_node_write_allowed(
    conn: sqlite3.Connection,
    novel_id: str,
    *,
    operation: str,
    capability: Optional[ProjectionWriteCapability] = None,
) -> None:
    """Reject protected StoryNode writes unless they belong to a projection TXN."""

    if not is_manifest_authority(conn, novel_id):
        return
    if capability is None:
        raise PlanningAuthorityError(
            f"manifest planning authority forbids legacy StoryNode write: {operation}"
        )
    _validate_projection_capability(conn, novel_id, capability)


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
