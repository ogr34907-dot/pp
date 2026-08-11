"""SQLite storage for draft/published five-level outline contracts."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
import json
import sqlite3
from typing import Any, Optional, Union
from uuid import uuid4

from domain.structure.outline_contract import (
    OutlineContract,
    OutlineLevel,
    OutlinePayload,
    OutlineSource,
    OutlineStatus,
)


class OutlineGateError(ValueError):
    """Raised when an operation would bypass the published plan gate."""


@dataclass(frozen=True)
class OutlineContractSlot:
    """A logical outline node with independent draft and active revisions."""

    id: str
    novel_id: str
    level: OutlineLevel
    parent_contract_id: Optional[str]
    story_node_id: Optional[str]
    active: Optional[OutlineContract]
    draft: Optional[OutlineContract]
    author_locked: bool = False
    has_author_edits: bool = False

    @property
    def active_status(self) -> Optional[OutlineStatus]:
        return self.active.status if self.active else None


class OutlineContractRepository:
    """Persist immutable plan revisions and expose the active projection only."""

    def __init__(self, db: Union[str, "DatabaseConnection"]):
        from infrastructure.persistence.database.connection import DatabaseConnection

        if isinstance(db, DatabaseConnection):
            self._db = db
            self.db_path = db.db_path
        else:
            self._db = None
            self.db_path = str(db)

    def _connection(self) -> sqlite3.Connection:
        if self._db is not None:
            return self._db.get_connection()
        from infrastructure.persistence.database.connection import get_database

        return get_database(self.db_path).get_connection()

    @staticmethod
    def _as_mapping(row: sqlite3.Row | dict[str, Any]) -> dict[str, Any]:
        return dict(row)

    @staticmethod
    def _revision_from_row(
        row: Optional[sqlite3.Row], contract_row: dict[str, Any]
    ) -> Optional[OutlineContract]:
        if row is None:
            return None
        values = dict(row)
        payload = OutlinePayload.from_dict(json.loads(values["payload_json"] or "{}"))
        return OutlineContract(
            id=str(contract_row["id"]),
            novel_id=str(contract_row["novel_id"]),
            level=OutlineLevel(str(contract_row["level"])),
            revision=int(values["revision"]),
            payload=payload,
            status=OutlineStatus(str(values["status"])),
            parent_id=contract_row.get("parent_contract_id"),
            story_node_id=contract_row.get("story_node_id"),
            parent_revision_digest=str(values["parent_revision_digest"] or ""),
            source=OutlineSource(str(values["source"])),
            author_locked=bool(contract_row.get("author_locked")),
            has_author_edits=bool(contract_row.get("has_author_edits")),
            published_digest=str(values["digest"] or ""),
        )

    def _slot_from_row(self, row: sqlite3.Row) -> OutlineContractSlot:
        conn = self._connection()
        contract = self._as_mapping(row)
        active_id = contract.get("active_version_id")
        draft_id = contract.get("draft_version_id")
        active_row = (
            conn.execute("SELECT * FROM outline_contract_versions WHERE id = ?", (active_id,)).fetchone()
            if active_id
            else None
        )
        draft_row = (
            conn.execute("SELECT * FROM outline_contract_versions WHERE id = ?", (draft_id,)).fetchone()
            if draft_id
            else None
        )
        return OutlineContractSlot(
            id=str(contract["id"]),
            novel_id=str(contract["novel_id"]),
            level=OutlineLevel(str(contract["level"])),
            parent_contract_id=contract.get("parent_contract_id"),
            story_node_id=contract.get("story_node_id"),
            active=self._revision_from_row(active_row, contract),
            draft=self._revision_from_row(draft_row, contract),
            author_locked=bool(contract.get("author_locked")),
            has_author_edits=bool(contract.get("has_author_edits")),
        )

    def get_slot(self, contract_id: str) -> OutlineContractSlot:
        row = self._connection().execute(
            "SELECT * FROM outline_contracts WHERE id = ?", (contract_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"outline contract not found: {contract_id}")
        return self._slot_from_row(row)

    def get_slot_by_story_node(
        self, novel_id: str, story_node_id: str
    ) -> Optional[OutlineContractSlot]:
        row = self._connection().execute(
            """
            SELECT * FROM outline_contracts
            WHERE novel_id = ? AND story_node_id = ?
            """,
            (novel_id, story_node_id),
        ).fetchone()
        return self._slot_from_row(row) if row is not None else None

    def ensure_root(self, novel_id: str) -> OutlineContractSlot:
        conn = self._connection()
        row = conn.execute(
            """
            SELECT * FROM outline_contracts
            WHERE novel_id = ? AND level = 'outline' AND parent_contract_id IS NULL
            """,
            (novel_id,),
        ).fetchone()
        if row is not None:
            return self._slot_from_row(row)
        contract_id = f"outline-{uuid4()}"
        now = datetime.now().isoformat()
        conn.execute(
            """
            INSERT INTO outline_contracts
                (id, novel_id, level, status, created_at, updated_at)
            VALUES (?, ?, 'outline', 'draft', ?, ?)
            """,
            (contract_id, novel_id, now, now),
        )
        conn.commit()
        return self.get_slot(contract_id)

    def create_contract(
        self,
        *,
        novel_id: str,
        level: OutlineLevel,
        parent_contract_id: str,
        story_node_id: Optional[str] = None,
    ) -> OutlineContractSlot:
        parent = self.get_slot(parent_contract_id)
        if parent.novel_id != novel_id:
            raise OutlineGateError("parent outline belongs to another novel")
        if parent.level.child_level != level:
            expected = parent.level.child_level.value if parent.level.child_level else "none"
            raise OutlineGateError(f"{parent.level.value} can only create {expected}")
        if parent.active is None or parent.active.status != OutlineStatus.SYNCED:
            raise OutlineGateError(
                f"{parent.level.value}:{parent.active_status.value if parent.active_status else 'missing'} must be synced before generating {level.value}"
            )
        contract_id = f"outline-{uuid4()}"
        now = datetime.now().isoformat()
        conn = self._connection()
        conn.execute(
            """
            INSERT INTO outline_contracts
                (id, novel_id, level, story_node_id, parent_contract_id, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, 'draft', ?, ?)
            """,
            (contract_id, novel_id, level.value, story_node_id, parent_contract_id, now, now),
        )
        conn.commit()
        return self.get_slot(contract_id)

    def save_draft(
        self,
        contract_id: str,
        payload: OutlinePayload,
        *,
        source: OutlineSource = OutlineSource.AI,
    ) -> OutlineContractSlot:
        slot = self.get_slot(contract_id)
        parent_digest = ""
        if slot.parent_contract_id:
            parent = self.get_slot(slot.parent_contract_id)
            if parent.active is None or parent.active.status != OutlineStatus.SYNCED:
                raise OutlineGateError(
                    f"{parent.level.value}:{parent.active_status.value if parent.active_status else 'missing'} must be synced before drafting {slot.level.value}"
                )
            parent_digest = parent.active.published_digest or parent.active.digest
        conn = self._connection()
        row = conn.execute(
            "SELECT COALESCE(MAX(revision), 0) AS current_revision FROM outline_contract_versions WHERE contract_id = ?",
            (contract_id,),
        ).fetchone()
        revision = int(row["current_revision"] or 0) + 1
        version_id = f"outline-version-{uuid4()}"
        now = datetime.now().isoformat()
        payload_json = json.dumps(payload.canonical_dict(), ensure_ascii=False, sort_keys=True)
        conn.execute(
            """
            INSERT INTO outline_contract_versions
                (id, contract_id, revision, payload_json, digest, parent_revision_digest, source, status, created_at, updated_at)
            VALUES (?, ?, ?, ?, ?, ?, ?, 'draft', ?, ?)
            """,
            (version_id, contract_id, revision, payload_json, payload.digest, parent_digest, source.value, now, now),
        )
        conn.execute(
            """
            UPDATE outline_contracts
            SET draft_version_id = ?,
                status = CASE WHEN active_version_id IS NULL THEN 'draft' ELSE status END,
                has_author_edits = CASE WHEN ? = 'author' THEN 1 ELSE has_author_edits END,
                updated_at = ?
            WHERE id = ?
            """,
            (version_id, source.value, now, contract_id),
        )
        conn.commit()
        return self.get_slot(contract_id)

    def publish_and_sync(
        self,
        contract_id: str,
        *,
        expected_revision: int,
        idempotency_key: str = "",
        author_locked: Optional[bool] = None,
    ) -> OutlineContractSlot:
        """Atomically make the chosen draft current and regenerate its projection."""

        slot = self.get_slot(contract_id)
        conn = self._connection()
        if idempotency_key:
            operation = "publish_and_sync"
            existing = conn.execute(
                """
                SELECT contract_id, revision FROM outline_operation_keys
                WHERE novel_id = ? AND idempotency_key = ? AND operation = ?
                """,
                (slot.novel_id, idempotency_key, operation),
            ).fetchone()
            if existing is not None:
                if str(existing["contract_id"]) != contract_id:
                    raise OutlineGateError("idempotency key belongs to another outline contract")
                return self.get_slot(contract_id)

        draft = slot.draft
        if draft is None:
            raise OutlineGateError("no draft revision is available to publish")
        if draft.revision != expected_revision:
            raise OutlineGateError(
                f"revision conflict: expected {expected_revision}, current draft is {draft.revision}"
            )
        if slot.parent_contract_id:
            parent = self.get_slot(slot.parent_contract_id)
            if parent.active is None or parent.active.status != OutlineStatus.SYNCED:
                raise OutlineGateError("parent outline must be synced before publishing a child")
            parent_digest = parent.active.published_digest or parent.active.digest
            if draft.parent_revision_digest != parent_digest:
                raise OutlineGateError("parent outline changed; regenerate or reconcile this child draft")

        now = datetime.now().isoformat()
        locked_value = slot.author_locked if author_locked is None else bool(author_locked)
        try:
            conn.execute("BEGIN")
            if slot.active is not None:
                conn.execute(
                    "UPDATE outline_contract_versions SET status = 'superseded', updated_at = ? WHERE id = ?",
                    (now, self._active_version_id(contract_id)),
                )
            conn.execute(
                "UPDATE outline_contract_versions SET status = 'syncing', updated_at = ? WHERE id = ?",
                (now, self._draft_version_id(contract_id)),
            )
            conn.execute(
                """
                UPDATE outline_contracts
                SET active_version_id = draft_version_id,
                    draft_version_id = NULL,
                    status = 'syncing',
                    author_locked = ?,
                    updated_at = ?
                WHERE id = ?
                """,
                (int(locked_value), now, contract_id),
            )
            active_version_id = self._active_version_id(contract_id)
            active_row = conn.execute(
                "SELECT digest, payload_json FROM outline_contract_versions WHERE id = ?", (active_version_id,)
            ).fetchone()
            conn.execute(
                "UPDATE outline_plan_projections SET is_active = 0 WHERE contract_id = ?", (contract_id,)
            )
            conn.execute(
                """
                INSERT INTO outline_plan_projections
                    (id, novel_id, contract_id, version_id, digest, payload_json, is_active, synced_at, created_at)
                VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                ON CONFLICT(contract_id, version_id) DO UPDATE SET
                    digest = excluded.digest,
                    payload_json = excluded.payload_json,
                    is_active = 1,
                    synced_at = excluded.synced_at
                """,
                (
                    f"outline-projection-{uuid4()}",
                    slot.novel_id,
                    contract_id,
                    active_version_id,
                    active_row["digest"],
                    active_row["payload_json"],
                    now,
                    now,
                ),
            )
            conn.execute(
                "UPDATE outline_contract_versions SET status = 'synced', updated_at = ? WHERE id = ?",
                (now, active_version_id),
            )
            conn.execute(
                "UPDATE outline_contracts SET status = 'synced', updated_at = ? WHERE id = ?",
                (now, contract_id),
            )
            self._invalidate_descendants(conn, contract_id, now)
            if idempotency_key:
                conn.execute(
                    """
                    INSERT INTO outline_operation_keys
                        (novel_id, idempotency_key, operation, contract_id, revision, created_at)
                    VALUES (?, ?, 'publish_and_sync', ?, ?, ?)
                    """,
                    (slot.novel_id, idempotency_key, contract_id, draft.revision, now),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return self.get_slot(contract_id)

    def _active_version_id(self, contract_id: str) -> str:
        row = self._connection().execute(
            "SELECT active_version_id FROM outline_contracts WHERE id = ?", (contract_id,)
        ).fetchone()
        if row is None or not row["active_version_id"]:
            raise OutlineGateError("active outline version is missing")
        return str(row["active_version_id"])

    def _draft_version_id(self, contract_id: str) -> str:
        row = self._connection().execute(
            "SELECT draft_version_id FROM outline_contracts WHERE id = ?", (contract_id,)
        ).fetchone()
        if row is None or not row["draft_version_id"]:
            raise OutlineGateError("draft outline version is missing")
        return str(row["draft_version_id"])

    @staticmethod
    def _invalidate_descendants(conn: sqlite3.Connection, contract_id: str, now: str) -> None:
        rows = conn.execute(
            """
            WITH RECURSIVE descendants(id, author_locked, has_author_edits, active_version_id) AS (
                SELECT id, author_locked, has_author_edits, active_version_id
                FROM outline_contracts WHERE parent_contract_id = ?
                UNION ALL
                SELECT c.id, c.author_locked, c.has_author_edits, c.active_version_id
                FROM outline_contracts AS c
                JOIN descendants AS d ON c.parent_contract_id = d.id
            )
            SELECT * FROM descendants WHERE active_version_id IS NOT NULL
            """,
            (contract_id,),
        ).fetchall()
        for row in rows:
            status = "conflict" if bool(row["author_locked"]) or bool(row["has_author_edits"]) else "stale"
            conn.execute(
                "UPDATE outline_contracts SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, row["id"]),
            )
            conn.execute(
                "UPDATE outline_contract_versions SET status = ?, updated_at = ? WHERE id = ?",
                (status, now, row["active_version_id"]),
            )
            conn.execute(
                "UPDATE outline_plan_projections SET is_active = 0 WHERE contract_id = ?",
                (row["id"],),
            )

    def list_versions(self, contract_id: str) -> list[OutlineContract]:
        slot = self.get_slot(contract_id)
        contract_row = {
            "id": slot.id,
            "novel_id": slot.novel_id,
            "level": slot.level.value,
            "parent_contract_id": slot.parent_contract_id,
            "story_node_id": slot.story_node_id,
            "author_locked": int(slot.author_locked),
            "has_author_edits": int(slot.has_author_edits),
        }
        rows = self._connection().execute(
            "SELECT * FROM outline_contract_versions WHERE contract_id = ? ORDER BY revision",
            (contract_id,),
        ).fetchall()
        return [self._revision_from_row(row, contract_row) for row in rows if row is not None]

    def list_active_projections(self, novel_id: str) -> list[dict[str, Any]]:
        """Context assembly calls this, never draft/history revision queries."""

        rows = self._connection().execute(
            """
            SELECT p.contract_id, c.level, p.version_id, p.digest, p.payload_json
            FROM outline_plan_projections AS p
            JOIN outline_contracts AS c ON c.id = p.contract_id
            WHERE p.novel_id = ? AND p.is_active = 1 AND c.status = 'synced'
            ORDER BY CASE c.level
                WHEN 'outline' THEN 0 WHEN 'part' THEN 1 WHEN 'volume' THEN 2
                WHEN 'act' THEN 3 ELSE 4 END
            """,
            (novel_id,),
        ).fetchall()
        return [
            {
                "contract_id": row["contract_id"],
                "level": row["level"],
                "version_id": row["version_id"],
                "digest": row["digest"],
                "payload": json.loads(row["payload_json"] or "{}"),
            }
            for row in rows
        ]
