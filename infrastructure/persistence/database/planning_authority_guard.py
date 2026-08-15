"""Runtime guards for the book-level planning authority.

The manifest Head is the only planning authority after cutover. Physical
``story_nodes`` projection writes therefore require an opaque capability
minted by :class:`PlanProjectionWriter` while it owns one ``BEGIN IMMEDIATE``
transaction. The capability expires as soon as that transaction ends.
"""

from __future__ import annotations

import sqlite3
from typing import Optional
from uuid import uuid4


class PlanningAuthorityError(RuntimeError):
    """Raised when a planning writer bypasses the active authority."""


class _ProjectionSession:
    """Private transaction heartbeat used by projection capabilities."""

    __slots__ = ("connection", "connection_identity", "savepoint", "active")

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.connection = conn
        self.connection_identity = id(conn)
        self.savepoint = f"pp_projection_{uuid4().hex}"
        self.active = True
        conn.execute(f"SAVEPOINT {self.savepoint}")

    def heartbeat(self, conn: sqlite3.Connection) -> None:
        if not self.active or conn is not self.connection:
            raise PlanningAuthorityError("StoryNode projection capability is out of scope")
        if not conn.in_transaction:
            self.active = False
            raise PlanningAuthorityError("StoryNode projection transaction has ended")
        try:
            conn.execute(f"RELEASE SAVEPOINT {self.savepoint}")
        except sqlite3.OperationalError as exc:
            self.active = False
            raise PlanningAuthorityError("StoryNode projection transaction has ended") from exc
        conn.execute(f"SAVEPOINT {self.savepoint}")

    def close(self, conn: sqlite3.Connection) -> None:
        if not self.active:
            return
        try:
            if conn.in_transaction:
                conn.execute(f"RELEASE SAVEPOINT {self.savepoint}")
        except sqlite3.OperationalError:
            pass
        finally:
            self.active = False


class ProjectionWriteCapability:
    """Opaque permit bound to one live projection transaction."""

    __slots__ = (
        "_connection_identity", "_session", "_novel_id", "_plan_revision_id",
        "_operation", "_expected_active_plan_revision_id", "_expected_active_plan_digest",
        "_expected_authority_generation", "_expected_projection_generation",
        "_target_digest", "_activated", "_active",
    )

    def __init__(self, *args: object, **kwargs: object) -> None:
        del args, kwargs
        raise TypeError("ProjectionWriteCapability is private")

    @classmethod
    def _create(
        cls, *, connection_identity: int, session: _ProjectionSession,
        novel_id: str, plan_revision_id: str, operation: str,
        expected_active_plan_revision_id: Optional[str], expected_active_plan_digest: str,
        expected_authority_generation: int, expected_projection_generation: int,
        target_digest: str,
    ) -> "ProjectionWriteCapability":
        capability = object.__new__(cls)
        capability._connection_identity = connection_identity
        capability._session = session
        capability._novel_id = novel_id
        capability._plan_revision_id = plan_revision_id
        capability._operation = operation
        capability._expected_active_plan_revision_id = expected_active_plan_revision_id
        capability._expected_active_plan_digest = expected_active_plan_digest
        capability._expected_authority_generation = int(expected_authority_generation)
        capability._expected_projection_generation = int(expected_projection_generation)
        capability._target_digest = target_digest
        capability._activated = False
        capability._active = True
        return capability

    @property
    def novel_id(self) -> str: return self._novel_id
    @property
    def plan_revision_id(self) -> str: return self._plan_revision_id
    @property
    def designated_operation(self) -> str: return self._operation
    @property
    def target_digest(self) -> str: return self._target_digest
    @property
    def activated(self) -> bool: return self._activated

    def _expire(self) -> None:
        self._active = False
        self._session.active = False


def _begin_projection_writer_session(conn: sqlite3.Connection) -> _ProjectionSession:
    if conn.in_transaction:
        raise PlanningAuthorityError("projection transaction requires a clean connection")
    conn.execute("BEGIN IMMEDIATE")
    try:
        return _ProjectionSession(conn)
    except BaseException:
        conn.rollback()
        raise


def _end_projection_writer_session(conn: sqlite3.Connection, session: _ProjectionSession) -> None:
    session.close(conn)


def _mint_projection_capability(
    conn: sqlite3.Connection, *, novel_id: str, plan_revision_id: str,
    designated_operation: str, authority_generation: int, projection_generation: int,
    expected_active_plan_revision_id: Optional[str], expected_active_plan_digest: str,
    _session: Optional[_ProjectionSession] = None,
) -> ProjectionWriteCapability:
    if designated_operation == "projection":
        raise PlanningAuthorityError("caller-supplied projection batches require a designated Head operation")
    if designated_operation not in {"publish", "restore", "cutover"}:
        raise PlanningAuthorityError("projection capability requires publish, restore, or cutover")
    if _session is None:
        raise PlanningAuthorityError("projection capability requires a private session")
    if conn is not _session.connection or not conn.in_transaction:
        raise PlanningAuthorityError("projection capability requires a live transaction")
    _session.heartbeat(conn)
    head = _raw_planning_head(conn, novel_id)
    if head is None:
        raise PlanningAuthorityError("planning Head is missing")
    if int(head["authority_generation"]) != int(authority_generation):
        raise PlanningAuthorityError("planning Head generation changed")
    if int(head["projection_generation"]) != int(projection_generation):
        raise PlanningAuthorityError("planning projection generation changed")
    if head["active_plan_revision_id"] != expected_active_plan_revision_id:
        raise PlanningAuthorityError("planning Head changed before projection")
    if str(head["active_plan_digest"] or "") != str(expected_active_plan_digest or ""):
        raise PlanningAuthorityError("planning Head digest changed before projection")
    if designated_operation == "cutover" and head["authority_mode"] != "legacy":
        raise PlanningAuthorityError("cutover requires legacy planning authority")
    if designated_operation != "cutover" and head["authority_mode"] != "manifest":
        raise PlanningAuthorityError("publish/restore requires manifest authority")
    target = conn.execute(
        "SELECT novel_id, digest, sealed_at, status, reconciliation_status "
        "FROM outline_plan_revisions WHERE id = ?", (plan_revision_id,)
    ).fetchone()
    if (target is None or str(target["novel_id"]) != novel_id or not target["sealed_at"]
        or str(target["status"] or "") != "ready_for_review"
        or str(target["reconciliation_status"] or "") != "aligned"
        or not str(target["digest"] or "")):
        raise PlanningAuthorityError("projection target must be a sealed aligned ready_for_review plan")
    item_count = conn.execute(
        "SELECT COUNT(*) FROM outline_plan_revision_items WHERE plan_revision_id = ?", (plan_revision_id,)
    ).fetchone()[0]
    if int(item_count or 0) < 1:
        raise PlanningAuthorityError("projection target has no sealed plan items")
    return ProjectionWriteCapability._create(
        connection_identity=id(conn), session=_session, novel_id=novel_id,
        plan_revision_id=plan_revision_id, operation=designated_operation,
        expected_active_plan_revision_id=expected_active_plan_revision_id,
        expected_active_plan_digest=str(expected_active_plan_digest or ""),
        expected_authority_generation=int(authority_generation),
        expected_projection_generation=int(projection_generation),
        target_digest=str(target["digest"]),
    )


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
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


def _raw_planning_head(conn: sqlite3.Connection, novel_id: str) -> Optional[dict[str, object]]:
    if not _table_exists(conn, "outline_planning_heads"):
        return None
    row = conn.execute("SELECT * FROM outline_planning_heads WHERE novel_id = ?", (novel_id,)).fetchone()
    if row is None:
        return None
    return {
        "novel_id": str(_value(row, "novel_id") or novel_id),
        "authority_mode": str(_value(row, "authority_mode", 1) or "legacy"),
        "authority_generation": int(_value(row, "authority_generation", 2) or 0),
        "active_plan_revision_id": _value(row, "active_plan_revision_id", 3),
        "active_plan_digest": str(_value(row, "active_plan_digest", 4) or ""),
        "working_plan_revision_id": _value(row, "working_plan_revision_id", 5),
        "projection_generation": int(_value(row, "projection_generation", 6) or 0),
    }


def manifest_head(conn: sqlite3.Connection, novel_id: str) -> Optional[dict[str, object]]:
    head = _raw_planning_head(conn, novel_id)
    if head is None or head["authority_mode"] != "manifest":
        return None
    return head


def is_manifest_authority(conn: sqlite3.Connection, novel_id: str) -> bool:
    return manifest_head(conn, novel_id) is not None


def _validate_projection_capability(conn: sqlite3.Connection, novel_id: str, capability: ProjectionWriteCapability) -> None:
    if not isinstance(capability, ProjectionWriteCapability) or not capability._active:
        raise PlanningAuthorityError("invalid or expired StoryNode projection capability")
    if capability._novel_id != novel_id or conn is not capability._session.connection:
        raise PlanningAuthorityError("StoryNode projection capability is out of scope")
    capability._session.heartbeat(conn)
    head = _raw_planning_head(conn, novel_id)
    if head is None:
        raise PlanningAuthorityError("planning Head is missing")
    if capability._activated:
        expected_mode = "manifest"
        expected_id = capability._plan_revision_id
        expected_digest = capability._target_digest
        expected_generation = capability._expected_authority_generation + 1
        expected_projection = expected_generation
    else:
        expected_mode = "legacy" if capability._operation == "cutover" else "manifest"
        expected_id = capability._expected_active_plan_revision_id
        expected_digest = capability._expected_active_plan_digest
        expected_generation = capability._expected_authority_generation
        expected_projection = capability._expected_projection_generation
    if (head["authority_mode"] != expected_mode or head["active_plan_revision_id"] != expected_id
        or str(head["active_plan_digest"] or "") != str(expected_digest or "")
        or int(head["authority_generation"]) != expected_generation
        or int(head["projection_generation"]) != expected_projection):
        raise PlanningAuthorityError("planning Head changed during StoryNode projection")


def _mark_projection_activated(conn: sqlite3.Connection, capability: ProjectionWriteCapability) -> None:
    if capability._activated:
        raise PlanningAuthorityError("projection Head has already been activated")
    capability._activated = True
    try:
        _validate_projection_capability(conn, capability._novel_id, capability)
    except BaseException:
        capability._activated = False
        raise


def assert_story_node_write_allowed(conn: sqlite3.Connection, novel_id: str, *, operation: str, capability: Optional[ProjectionWriteCapability] = None) -> None:
    if capability is not None:
        _validate_projection_capability(conn, novel_id, capability)
        return
    if not is_manifest_authority(conn, novel_id):
        return
    raise PlanningAuthorityError(f"manifest planning authority forbids legacy StoryNode write: {operation}")


def assert_legacy_planning_mutation_allowed(conn: sqlite3.Connection, novel_id: str, *, operation: str) -> None:
    if is_manifest_authority(conn, novel_id):
        raise PlanningAuthorityError(f"manifest planning authority requires PlanRevision transaction: {operation}")
