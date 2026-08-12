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
    def _now() -> str:
        return datetime.now().isoformat()

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
            previous_sibling_digest=str(values.get("previous_sibling_digest") or ""),
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

    def _previous_synced_sibling(self, slot: OutlineContractSlot) -> Optional[OutlineContract]:
        """Return the preceding physical sibling's active plan, if one exists."""

        if not slot.parent_contract_id or not slot.story_node_id:
            return None
        conn = self._connection()
        node = conn.execute(
            "SELECT parent_id, order_index, number FROM story_nodes WHERE id = ?",
            (slot.story_node_id,),
        ).fetchone()
        if node is None:
            return None
        previous = conn.execute(
            """
            SELECT c.*
            FROM story_nodes AS n
            JOIN outline_contracts AS c ON c.story_node_id = n.id AND c.novel_id = ?
            WHERE n.novel_id = ?
              AND COALESCE(n.parent_id, '') = COALESCE(?, '')
              AND n.node_type = ?
              AND (n.order_index < ? OR (n.order_index = ? AND n.number < ?))
            ORDER BY n.order_index DESC, n.number DESC, n.id DESC
            LIMIT 1
            """,
            (
                slot.novel_id,
                slot.novel_id,
                node["parent_id"],
                slot.level.value,
                int(node["order_index"]),
                int(node["order_index"]),
                int(node["number"]),
            ),
        ).fetchone()
        if previous is None:
            return None
        sibling = self._slot_from_row(previous)
        if sibling.active is None or sibling.active.status != OutlineStatus.SYNCED:
            raise OutlineGateError("previous sibling must be synced before publishing this outline")
        return sibling.active

    def previous_synced_sibling(self, contract_id: str) -> Optional[OutlineContract]:
        """Expose the immediate published sibling handoff for draft prompting."""

        return self._previous_synced_sibling(self.get_slot(contract_id))

    @staticmethod
    def _validate_sibling_continuity(
        draft: OutlineContract, previous: Optional[OutlineContract]
    ) -> str:
        if previous is None:
            return ""
        blockers = draft.payload.sibling_continuity_blockers()
        if blockers:
            raise OutlineGateError("sibling continuity requires " + ", ".join(blockers))
        previous_end = previous.payload.chapter_end
        next_start = draft.payload.chapter_start
        if previous_end is not None and next_start is not None and next_start != previous_end + 1:
            raise OutlineGateError(
                f"sibling continuity requires chapter_start {previous_end + 1}, got {next_start}"
            )
        if previous_end is not None and draft.payload.chapter_end is not None:
            if draft.payload.chapter_end < previous_end + 1:
                raise OutlineGateError("sibling continuity requires a forward chapter range")
        return previous.published_digest or previous.digest

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

        previous_sibling_digest = self._validate_sibling_continuity(
            draft, self._previous_synced_sibling(slot)
        )

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
                "UPDATE outline_contract_versions SET previous_sibling_digest = ? WHERE id = ?",
                (previous_sibling_digest, self._draft_version_id(contract_id)),
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

    def start_generation_attempt(
        self,
        contract_id: str,
        *,
        prompt_snapshot: dict[str, Any],
        context_digest: str,
        retry_of_attempt_id: Optional[str] = None,
    ) -> dict[str, Any]:
        """Create one durable streamed-draft attempt before calling an LLM."""

        self.get_slot(contract_id)
        snapshot = dict(prompt_snapshot or {})
        if retry_of_attempt_id:
            previous = self.get_generation_attempt(retry_of_attempt_id)
            if previous["contract_id"] != contract_id:
                raise OutlineGateError("retry attempt belongs to another outline contract")
            if previous["status"] not in {"failed", "cancelled"}:
                raise OutlineGateError("only failed or cancelled outline attempts can be retried")
            if previous["context_digest"] != context_digest:
                raise OutlineGateError("outline context changed; start a new generation attempt")
            snapshot = dict(previous["prompt_snapshot"])

        attempt_id = f"outline-attempt-{uuid4()}"
        now = self._now()
        conn = self._connection()
        try:
            conn.execute(
                """
                INSERT INTO outline_generation_attempts
                    (id, contract_id, status, retry_of_attempt_id, context_digest,
                     prompt_snapshot_json, created_at, updated_at)
                VALUES (?, ?, 'running', ?, ?, ?, ?, ?)
                """,
                (
                    attempt_id,
                    contract_id,
                    retry_of_attempt_id,
                    context_digest,
                    json.dumps(snapshot, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )
            conn.commit()
        except sqlite3.IntegrityError as exc:
            raise OutlineGateError("an outline generation attempt is already running") from exc
        self._append_generation_attempt_event(
            attempt_id,
            {"type": "started", "contract_id": contract_id, "retry_of_attempt_id": retry_of_attempt_id},
        )
        return self.get_generation_attempt(attempt_id)

    def _append_generation_attempt_event(
        self, attempt_id: str, event: dict[str, Any]
    ) -> None:
        conn = self._connection()
        row = conn.execute(
            "SELECT 1 FROM outline_generation_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"outline generation attempt not found: {attempt_id}")
        sequence = int(
            conn.execute(
                "SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence "
                "FROM outline_generation_attempt_events WHERE attempt_id = ?",
                (attempt_id,),
            ).fetchone()["sequence"]
        )
        conn.execute(
            """
            INSERT INTO outline_generation_attempt_events
                (id, attempt_id, sequence, event_json, created_at)
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                f"outline-attempt-event-{uuid4()}",
                attempt_id,
                sequence,
                json.dumps(event, ensure_ascii=False, sort_keys=True),
                self._now(),
            ),
        )
        conn.commit()

    def append_generation_attempt_delta(self, attempt_id: str, text: str) -> dict[str, Any]:
        """Persist a streamed delta before it is exposed to a reconnecting client."""

        if not text:
            return self.get_generation_attempt(attempt_id)
        conn = self._connection()
        row = conn.execute(
            "SELECT status FROM outline_generation_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"outline generation attempt not found: {attempt_id}")
        if row["status"] != "running":
            raise OutlineGateError("outline generation attempt is no longer running")
        conn.execute(
            """
            UPDATE outline_generation_attempts
            SET accumulated_text = accumulated_text || ?, updated_at = ?
            WHERE id = ?
            """,
            (text, self._now(), attempt_id),
        )
        conn.commit()
        self._append_generation_attempt_event(attempt_id, {"type": "delta", "text": text})
        return self.get_generation_attempt(attempt_id)

    def complete_generation_attempt(
        self, attempt_id: str, *, draft_revision: int
    ) -> dict[str, Any]:
        return self._finish_generation_attempt(
            attempt_id,
            status="completed",
            draft_revision=draft_revision,
        )

    def fail_generation_attempt(self, attempt_id: str, error: str) -> dict[str, Any]:
        return self._finish_generation_attempt(attempt_id, status="failed", error=error)

    def cancel_generation_attempt(self, attempt_id: str) -> dict[str, Any]:
        return self._finish_generation_attempt(attempt_id, status="cancelled")

    def _finish_generation_attempt(
        self,
        attempt_id: str,
        *,
        status: str,
        draft_revision: Optional[int] = None,
        error: str = "",
    ) -> dict[str, Any]:
        if status not in {"completed", "failed", "cancelled"}:
            raise ValueError("invalid outline generation attempt status")
        conn = self._connection()
        row = conn.execute(
            "SELECT status FROM outline_generation_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"outline generation attempt not found: {attempt_id}")
        if row["status"] != "running":
            return self.get_generation_attempt(attempt_id)
        now = self._now()
        conn.execute(
            """
            UPDATE outline_generation_attempts
            SET status = ?, draft_revision = ?, error = ?, completed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, draft_revision, error, now, now, attempt_id),
        )
        conn.commit()
        event = {"type": "completed" if status == "completed" else ("cancelled" if status == "cancelled" else "error")}
        if draft_revision is not None:
            event["draft_revision"] = draft_revision
        if error:
            event["message"] = error
        self._append_generation_attempt_event(attempt_id, event)
        return self.get_generation_attempt(attempt_id)

    def get_generation_attempt(
        self, attempt_id: str, *, after_sequence: int = 0
    ) -> dict[str, Any]:
        conn = self._connection()
        row = conn.execute(
            "SELECT * FROM outline_generation_attempts WHERE id = ?", (attempt_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"outline generation attempt not found: {attempt_id}")
        events = conn.execute(
            """
            SELECT sequence, event_json FROM outline_generation_attempt_events
            WHERE attempt_id = ? AND sequence > ?
            ORDER BY sequence
            """,
            (attempt_id, int(after_sequence)),
        ).fetchall()
        return {
            "id": str(row["id"]),
            "contract_id": str(row["contract_id"]),
            "status": str(row["status"]),
            "retry_of_attempt_id": row["retry_of_attempt_id"],
            "context_digest": str(row["context_digest"] or ""),
            "prompt_snapshot": json.loads(row["prompt_snapshot_json"] or "{}"),
            "accumulated_text": str(row["accumulated_text"] or ""),
            "draft_revision": row["draft_revision"],
            "error": str(row["error"] or ""),
            "events": [
                {"sequence": int(event["sequence"]), **json.loads(event["event_json"] or "{}")}
                for event in events
            ],
        }

    def get_latest_generation_attempt(
        self, contract_id: str, *, after_sequence: int = 0
    ) -> Optional[dict[str, Any]]:
        row = self._connection().execute(
            """
            SELECT id FROM outline_generation_attempts
            WHERE contract_id = ?
            ORDER BY created_at DESC, id DESC
            LIMIT 1
            """,
            (contract_id,),
        ).fetchone()
        return self.get_generation_attempt(str(row["id"]), after_sequence=after_sequence) if row else None
