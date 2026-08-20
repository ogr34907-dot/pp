"""Archive-and-regenerate service for a novel tail beginning at any chapter."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import asyncio
import hashlib
import json
import sqlite3
from typing import Any, Iterable, Optional, Union
from uuid import uuid4

from infrastructure.persistence.database.chapter_candidate_repository import (
    CandidateGateError,
    ChapterCandidateRepository,
)
from infrastructure.persistence.database.planning_authority_guard import (
    PlanningAuthorityError,
    is_manifest_authority,
)
from infrastructure.persistence.database.outline_contract_repository import (
    OutlineContractRepository,
    OutlineGateError,
)
from infrastructure.persistence.database.plan_projection_writer import (
    PlanProjectionWriter,
)
from infrastructure.persistence.database.story_node_repository import StoryNodeRepository
from application.blueprint.services.outline_contract_service import (
    OutlineContractService,
)
from domain.novel.candidate_chapter import RunMode
from domain.structure.outline_plan import (
    OutlinePlanItem,
    PlanRevisionStatus,
    PlanReconciliationStatus,
    canonical_history_digest,
    canonical_plan_digest,
)


@dataclass(frozen=True)
class WorldlinePreview:
    token: str
    novel_id: str
    operation: str
    start_chapter: int
    target_chapters: int
    current_generated_chapters: int
    retained_through: int
    archive_from: Optional[int]
    archive_to: Optional[int]
    generation_epoch: int
    prefix_digest: str
    counts: dict[str, int]


@dataclass(frozen=True)
class WorldlineRegenerationResult:
    operation: str
    novel_id: str
    archive_id: Optional[str]
    generation_epoch: int
    retained_through: int
    next_action: str


class WorldlineRegenerationError(ValueError):
    pass


class WorldlineRegenerationService:
    """Move the active tail to a read-only archive before starting a new epoch."""

    _MANIFEST_LINEAGE_DIGEST_TABLES = (
        "outline_plan_revisions",
        "outline_plan_revision_items",
        "outline_plan_projection_bindings",
        "outline_contracts",
        "outline_contract_versions",
        "outline_plan_projections",
    )

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
    def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        return row is not None

    @classmethod
    def _columns(cls, conn: sqlite3.Connection, table: str) -> set[str]:
        if not cls._table_exists(conn, table):
            return set()
        return {str(row[1]) for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}

    def _manifest_binding_digest(
        self,
        conn: sqlite3.Connection,
        novel_id: str,
        plan_revision_id: str,
        *,
        require_live_projection: bool,
    ) -> str:
        """Hash a sealed plan's logical items and immutable binding coordinates."""

        repository = OutlineContractRepository(self._db or self.db_path)
        try:
            plan = repository.get_plan_revision(plan_revision_id, _connection=conn)
            if (
                plan.novel_id != novel_id
                or not plan.sealed_at
                or plan.status != PlanRevisionStatus.READY_FOR_REVIEW
                or plan.reconciliation_status != PlanReconciliationStatus.ALIGNED
            ):
                raise OutlineGateError("Manifest Head targets a non-publishable plan")
            if require_live_projection:
                repository.validate_projection_bindings(plan.id, conn)
            rows = repository.projection_bindings_for_revision(
                plan.id, _connection=conn
            )
        except (KeyError, OutlineGateError, sqlite3.Error) as exc:
            raise WorldlineRegenerationError(
                "Manifest planning Head projection is unavailable"
            ) from exc
        normalized = [
            {
                key: row.get(key)
                for key in (
                    "plan_revision_item_id",
                    "logical_node_id",
                    "parent_logical_node_id",
                    "level",
                    "sibling_index",
                    "expansion_state",
                    "version_id",
                    "version_digest",
                    "story_node_id",
                    "parent_story_node_id",
                    "number",
                    "order_index",
                )
            }
            for row in rows
        ]
        return self._stable_digest(
            {
                "plan_revision_id": plan.id,
                "plan_digest": plan.digest,
                "bindings": normalized,
            }
        )

    def _manifest_head_snapshot(
        self, conn: sqlite3.Connection, novel_id: str
    ) -> Optional[dict[str, Any]]:
        """Read and strictly validate the current Manifest Head."""

        if not is_manifest_authority(conn, novel_id):
            return None
        row = conn.execute(
            """
            SELECT authority_mode, authority_generation, projection_generation,
                   active_plan_revision_id, active_plan_digest,
                   working_plan_revision_id
            FROM outline_planning_heads WHERE novel_id = ?
            """,
            (novel_id,),
        ).fetchone()
        if row is None or str(row["authority_mode"] or "") != "manifest":
            raise WorldlineRegenerationError("Manifest planning Head is missing")
        plan_id = row["active_plan_revision_id"]
        digest = str(row["active_plan_digest"] or "")
        if (
            not plan_id
            or not digest
            or int(row["authority_generation"] or 0)
            != int(row["projection_generation"] or 0)
            or row["working_plan_revision_id"] is not None
        ):
            raise WorldlineRegenerationError(
                "Manifest planning Head projection is out of sync"
            )
        binding_digest = self._manifest_binding_digest(
            conn,
            novel_id,
            str(plan_id),
            require_live_projection=True,
        )
        plan = OutlineContractRepository(self._db or self.db_path).get_plan_revision(
            str(plan_id), _connection=conn
        )
        if plan.digest != digest:
            raise WorldlineRegenerationError(
                "Manifest planning Head digest does not match its revision"
            )
        return {
            "authority_mode": "manifest",
            "authority_generation": int(row["authority_generation"] or 0),
            "projection_generation": int(row["projection_generation"] or 0),
            "active_plan_revision_id": str(plan_id),
            "active_plan_digest": digest,
            "binding_digest": binding_digest,
        }

    def _manifest_prefix_digest(
        self, conn: sqlite3.Connection, novel_id: str, through_chapter: int
    ) -> str:
        """Derive the canonical identity digest without requiring rebuilt caches."""

        through = int(through_chapter)
        if through <= 0:
            return canonical_history_digest(())
        candidates = ChapterCandidateRepository(self._db or self.db_path)
        identities: list[dict[str, Any]] = []
        for chapter_number in range(1, through + 1):
            try:
                identity = candidates.resolve_formal_identity(
                    novel_id,
                    chapter_number,
                    connection=conn,
                )
            except CandidateGateError as exc:
                raise WorldlineRegenerationError(
                    "formal chapter identity is unavailable for Manifest rebase"
                ) from exc
            item: dict[str, Any] = {
                "source": identity.source,
                "chapter_number": identity.chapter_number,
                "chapter_id": identity.chapter_id,
                "content_sha256": identity.content_sha256,
                "content_revision": identity.content_revision,
            }
            if identity.candidate_id:
                item["candidate_id"] = identity.candidate_id
            identities.append(item)
        return canonical_history_digest(identities)

    def _record_manifest_head_snapshot(
        self,
        conn: sqlite3.Connection,
        *,
        archive_id: str,
        novel_id: str,
        snapshot: dict[str, Any],
        source_lineage_digest: str,
        source_archive_digest: str,
    ) -> None:
        if not source_lineage_digest or not source_archive_digest:
            raise WorldlineRegenerationError(
                "Manifest source archive evidence could not be recorded"
            )
        inserted = conn.execute(
            """
            INSERT INTO worldline_manifest_head_snapshots
                (archive_id, novel_id, active_plan_revision_id,
                 active_plan_digest, authority_generation,
                 projection_generation, binding_digest, source_lineage_digest,
                 source_archive_digest)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                archive_id,
                novel_id,
                snapshot["active_plan_revision_id"],
                snapshot["active_plan_digest"],
                int(snapshot["authority_generation"]),
                int(snapshot["projection_generation"]),
                snapshot["binding_digest"],
                source_lineage_digest,
                source_archive_digest,
            ),
        )
        if inserted.rowcount != 1:
            raise WorldlineRegenerationError(
                "Manifest Head snapshot could not be recorded"
            )

    def _archived_manifest_source_digest(
        self, conn: sqlite3.Connection, archive_id: str
    ) -> str:
        """Hash all immutable source payloads captured for a Manifest archive."""

        rows = conn.execute(
            """
            SELECT source_table, source_key, chapter_number, payload_json
            FROM worldline_archive_entries
            WHERE archive_id = ?
            ORDER BY source_table, source_key, chapter_number, payload_json
            """,
            (archive_id,),
        ).fetchall()
        if not rows:
            raise WorldlineRegenerationError("Manifest source archive is empty")
        return self._stable_digest([dict(row) for row in rows])

    def _archive_manifest_source_lineage(
        self,
        conn: sqlite3.Connection,
        *,
        archive_id: str,
        novel_id: str,
        plan_revision_id: str,
        expected_plan_digest: str,
    ) -> str:
        """Archive immutable evidence for the Manifest Head being rebased."""

        required_columns = {
            "outline_plan_revisions": {
                "id",
                "novel_id",
                "status",
                "digest",
                "sealed_at",
                "reconciliation_status",
            },
            "outline_plan_revision_items": {
                "id",
                "plan_revision_id",
                "logical_node_id",
                "version_id",
            },
            "outline_plan_projection_bindings": {"plan_revision_item_id"},
            "outline_contracts": {"id", "novel_id"},
            "outline_contract_versions": {"id", "contract_id"},
            "outline_plan_projections": {
                "id",
                "novel_id",
                "contract_id",
                "version_id",
            },
        }
        if any(
            not columns <= self._columns(conn, table)
            for table, columns in required_columns.items()
        ):
            raise WorldlineRegenerationError(
                "Manifest source lineage is unavailable"
            )

        plan_row = conn.execute(
            """
            SELECT * FROM outline_plan_revisions
            WHERE id = ? AND novel_id = ?
            """,
            (plan_revision_id, novel_id),
        ).fetchone()
        if (
            plan_row is None
            or not plan_row["sealed_at"]
            or str(plan_row["status"] or "")
            != PlanRevisionStatus.READY_FOR_REVIEW.value
            or str(plan_row["reconciliation_status"] or "")
            != PlanReconciliationStatus.ALIGNED.value
            or str(plan_row["digest"] or "") != expected_plan_digest
        ):
            raise WorldlineRegenerationError(
                "Manifest Head targets a non-publishable source plan"
            )

        # This also validates every declared physical coordinate against the
        # live projection before we rely on the archive as restore evidence.
        self._manifest_binding_digest(
            conn,
            novel_id,
            plan_revision_id,
            require_live_projection=True,
        )
        item_rows = conn.execute(
            """
            SELECT * FROM outline_plan_revision_items
            WHERE plan_revision_id = ?
            ORDER BY id
            """,
            (plan_revision_id,),
        ).fetchall()
        if not item_rows:
            raise WorldlineRegenerationError(
                "Manifest source plan has no revision items"
            )
        item_ids = {str(row["id"]) for row in item_rows}
        contract_ids = {str(row["logical_node_id"]) for row in item_rows}
        expected_version_contracts = {
            str(row["version_id"]): str(row["logical_node_id"])
            for row in item_rows
        }
        if len(item_ids) != len(item_rows) or len(contract_ids) != len(item_rows):
            raise WorldlineRegenerationError(
                "Manifest source plan has duplicate logical identities"
            )

        placeholders = ", ".join("?" for _ in contract_ids)
        contract_rows = conn.execute(
            """
            SELECT * FROM outline_contracts
            WHERE novel_id = ? AND id IN ({})
            ORDER BY id
            """.format(placeholders),
            (novel_id, *sorted(contract_ids)),
        ).fetchall()
        if {str(row["id"]) for row in contract_rows} != contract_ids:
            raise WorldlineRegenerationError(
                "Manifest source plan has missing logical contracts"
            )

        version_rows = conn.execute(
            """
            SELECT * FROM outline_contract_versions
            WHERE contract_id IN ({})
            ORDER BY contract_id, revision, id
            """.format(placeholders),
            tuple(sorted(contract_ids)),
        ).fetchall()
        versions_by_id = {str(row["id"]): row for row in version_rows}
        if any(
            version_id not in versions_by_id
            or str(versions_by_id[version_id]["contract_id"])
            != expected_contract_id
            for version_id, expected_contract_id in expected_version_contracts.items()
        ):
            raise WorldlineRegenerationError(
                "Manifest source plan has missing content versions"
            )

        binding_rows = conn.execute(
            """
            SELECT * FROM outline_plan_projection_bindings
            WHERE plan_revision_item_id IN ({})
            ORDER BY plan_revision_item_id
            """.format(", ".join("?" for _ in item_ids)),
            tuple(sorted(item_ids)),
        ).fetchall()
        if {str(row["plan_revision_item_id"]) for row in binding_rows} != item_ids:
            raise WorldlineRegenerationError(
                "Manifest source plan has incomplete projection bindings"
            )

        projection_rows = conn.execute(
            """
            SELECT projection.*
            FROM outline_plan_projections AS projection
            JOIN outline_contract_versions AS version
              ON version.id = projection.version_id
            WHERE projection.novel_id = ?
              AND projection.contract_id IN ({})
              AND version.contract_id = projection.contract_id
            ORDER BY projection.contract_id, projection.version_id, projection.id
            """.format(placeholders),
            (novel_id, *sorted(contract_ids)),
        ).fetchall()

        lineage_rows = {
            "outline_plan_revisions": [plan_row],
            "outline_plan_revision_items": item_rows,
            "outline_plan_projection_bindings": binding_rows,
            "outline_contracts": contract_rows,
            "outline_contract_versions": version_rows,
            "outline_plan_projections": projection_rows,
        }
        for table, rows in lineage_rows.items():
            self._archive_rows(
                conn,
                archive_id,
                table,
                rows,
                chapter_column=None,
            )
        return self._archived_manifest_lineage_digest(conn, archive_id)

    def _archived_manifest_lineage_digest(
        self, conn: sqlite3.Connection, archive_id: str
    ) -> str:
        lineage: dict[str, list[dict[str, Any]]] = {}
        for table in self._MANIFEST_LINEAGE_DIGEST_TABLES:
            rows = conn.execute(
                """
                SELECT payload_json FROM worldline_archive_entries
                WHERE archive_id = ? AND source_table = ?
                """,
                (archive_id, table),
            ).fetchall()
            payloads: list[dict[str, Any]] = []
            for row in rows:
                try:
                    payload = json.loads(row["payload_json"] or "{}")
                except (TypeError, ValueError, json.JSONDecodeError) as exc:
                    raise WorldlineRegenerationError(
                        "Manifest source lineage is malformed"
                    ) from exc
                if not isinstance(payload, dict):
                    raise WorldlineRegenerationError(
                        "Manifest source lineage is malformed"
                    )
                payloads.append(payload)
            if not payloads and table != "outline_plan_projections":
                raise WorldlineRegenerationError(
                    "Manifest source lineage is incomplete"
                )
            lineage[table] = sorted(
                payloads,
                key=lambda payload: json.dumps(
                    payload, ensure_ascii=False, sort_keys=True, default=str
                ),
            )
        return self._stable_digest(lineage)

    def _validate_manifest_archive_evidence(
        self,
        conn: sqlite3.Connection,
        *,
        source: sqlite3.Row,
        archive_id: str,
        head_snapshot: dict[str, Any],
    ) -> None:
        try:
            metadata = json.loads(source["metadata_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise WorldlineRegenerationError(
                "manifest-aware restore requires immutable archive evidence"
            ) from exc
        archived_head = metadata.get("manifest_head") if isinstance(metadata, dict) else None
        head_fields = (
            "active_plan_revision_id",
            "active_plan_digest",
            "authority_generation",
            "projection_generation",
            "binding_digest",
        )
        if not isinstance(archived_head, dict) or any(
            archived_head.get(field) != head_snapshot.get(field)
            for field in head_fields
        ):
            raise WorldlineRegenerationError(
                "manifest-aware restore requires an immutable Head snapshot"
            )
        expected_lineage = str(head_snapshot.get("source_lineage_digest") or "")
        expected_archive = str(head_snapshot.get("source_archive_digest") or "")
        if (
            not expected_lineage
            or not expected_archive
            or str(metadata.get("source_lineage_digest") or "") != expected_lineage
            or str(metadata.get("source_archive_digest") or "") != expected_archive
            or self._archived_manifest_lineage_digest(conn, archive_id)
            != expected_lineage
            or self._archived_manifest_source_digest(conn, archive_id)
            != expected_archive
        ):
            raise WorldlineRegenerationError("Manifest source lineage changed before restore")

    def _load_manifest_head_snapshot(
        self,
        conn: sqlite3.Connection,
        *,
        archive_id: str,
        novel_id: str,
    ) -> dict[str, Any]:
        row = conn.execute(
            """
            SELECT archive_id, novel_id, active_plan_revision_id,
                   active_plan_digest, authority_generation,
                   projection_generation, binding_digest, source_lineage_digest,
                   source_archive_digest
            FROM worldline_manifest_head_snapshots
            WHERE archive_id = ? AND novel_id = ?
            """,
            (archive_id, novel_id),
        ).fetchone()
        if row is None:
            raise WorldlineRegenerationError(
                "Manifest Worldline archive has no immutable Head snapshot"
            )
        snapshot = dict(row)
        if (
            int(snapshot["authority_generation"] or 0)
            != int(snapshot["projection_generation"] or 0)
            or not snapshot["active_plan_revision_id"]
            or not snapshot["active_plan_digest"]
            or not snapshot["binding_digest"]
            or not snapshot["source_lineage_digest"]
            or not snapshot["source_archive_digest"]
        ):
            raise WorldlineRegenerationError(
                "Manifest Worldline Head snapshot is malformed"
            )
        actual_digest = self._manifest_binding_digest(
            conn,
            novel_id,
            str(snapshot["active_plan_revision_id"]),
            require_live_projection=False,
        )
        if (
            actual_digest != str(snapshot["binding_digest"])
            or str(
                OutlineContractRepository(self._db or self.db_path)
                .get_plan_revision(
                    str(snapshot["active_plan_revision_id"]), _connection=conn
                )
                .digest
            )
            != str(snapshot["active_plan_digest"])
        ):
            raise WorldlineRegenerationError(
                "Manifest Worldline Head snapshot does not match its sealed plan"
            )
        return snapshot

    @staticmethod
    def _row_dict(row: sqlite3.Row) -> dict[str, Any]:
        return dict(row)

    def _prefix_digest(self, conn: sqlite3.Connection, novel_id: str, retained_through: int) -> str:
        rows = conn.execute(
            """
            SELECT number, content_sha256, content_revision
            FROM chapters
            WHERE novel_id = ? AND number <= ?
            ORDER BY number
            """,
            (novel_id, retained_through),
        ).fetchall()
        encoded = json.dumps([dict(row) for row in rows], ensure_ascii=False, sort_keys=True)
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _generation_epoch(self, conn: sqlite3.Connection, novel_id: str) -> int:
        row = conn.execute(
            "SELECT generation_epoch FROM novel_generation_runs WHERE novel_id = ?", (novel_id,)
        ).fetchone()
        return int(row["generation_epoch"] or 0) if row else 0

    @staticmethod
    def _stable_digest(value: Any) -> str:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def _generation_filter_snapshot(
        self, conn: sqlite3.Connection, novel_id: str
    ) -> dict[str, Any]:
        try:
            row = conn.execute(
                "SELECT active_generation_epoch FROM worldline_generation_filters WHERE novel_id = ?",
                (novel_id,),
            ).fetchone()
        except sqlite3.Error as exc:
            raise WorldlineRegenerationError(
                "worldline generation filter is unavailable"
            ) from exc
        if row is None:
            return {"exists": False, "active_generation_epoch": None}
        return {
            "exists": True,
            "active_generation_epoch": int(row["active_generation_epoch"] or 0),
        }

    def _generation_run_snapshot(
        self, conn: sqlite3.Connection, novel_id: str
    ) -> Optional[dict[str, Any]]:
        row = conn.execute(
            """
            SELECT run_mode, state, generation_epoch, target_chapters,
                   current_formal_chapter, current_candidate_id,
                   current_candidate_chapter, canonical_sync_status,
                   next_action, last_error, max_pending_candidates, prefetch
            FROM novel_generation_runs
            WHERE novel_id = ?
            """,
            (novel_id,),
        ).fetchone()
        if row is None:
            return None
        return {
            "run_mode": str(row["run_mode"] or "continuous"),
            "state": str(row["state"] or "stopped"),
            "generation_epoch": int(row["generation_epoch"] or 0),
            "target_chapters": int(row["target_chapters"] or 0),
            "current_formal_chapter": int(row["current_formal_chapter"] or 0),
            "current_candidate_id": (
                str(row["current_candidate_id"])
                if row["current_candidate_id"] is not None
                else None
            ),
            "current_candidate_chapter": (
                int(row["current_candidate_chapter"])
                if row["current_candidate_chapter"] is not None
                else None
            ),
            "canonical_sync_status": str(row["canonical_sync_status"] or "ready"),
            "next_action": str(row["next_action"] or ""),
            "last_error": str(row["last_error"] or ""),
            "max_pending_candidates": int(row["max_pending_candidates"] or 1),
            "prefetch": int(row["prefetch"] or 0),
        }

    def _formal_identity_snapshot(
        self,
        conn: sqlite3.Connection,
        novel_id: str,
        *,
        allow_unproven_tail: bool = False,
    ) -> dict[str, Any]:
        """Capture the unique, continuous Formal prefix by exact identity."""

        candidates = ChapterCandidateRepository(self._db or self.db_path)
        try:
            formal_head, blockers = candidates.formal_history_snapshot(novel_id)
            if blockers and not allow_unproven_tail:
                raise CandidateGateError("formal history has an unproven completed tail")
            rows = conn.execute(
                """
                SELECT id, number, content, content_sha256, content_revision, status
                FROM chapters
                WHERE novel_id = ? AND number <= ?
                ORDER BY number, id
                """,
                (novel_id, int(formal_head)),
            ).fetchall()
            identities: list[dict[str, Any]] = []
            for expected_number, row in enumerate(rows, start=1):
                content = str(row["content"] or "")
                actual_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
                if (
                    int(row["number"] or 0) != expected_number
                    or str(row["status"] or "") != "completed"
                    or not content.strip()
                    or str(row["content_sha256"] or "") != actual_hash
                    or int(row["content_revision"] or 0) < 1
                ):
                    raise CandidateGateError("formal chapter authority mismatch")
                identities.append(
                    {
                        "chapter_number": expected_number,
                        "chapter_id": str(row["id"]),
                        "content_sha256": actual_hash,
                        "content_revision": int(row["content_revision"] or 0),
                    }
                )
            if len(identities) != int(formal_head):
                raise CandidateGateError("formal history is not continuous")
        except CandidateGateError as exc:
            raise WorldlineRegenerationError(
                "chapter prefix changed; request a new worldline preview"
            ) from exc
        return {
            "formal_head": int(formal_head),
            "formal_identity_digest": self._stable_digest(identities),
        }

    def _chapter_identity_digest(self, conn: sqlite3.Connection, novel_id: str) -> str:
        rows = conn.execute(
            """
            SELECT id, number, content, content_sha256, content_revision, status
            FROM chapters
            WHERE novel_id = ?
            ORDER BY number, id
            """,
            (novel_id,),
        ).fetchall()
        identities = [
            {
                "id": str(row["id"]),
                "number": int(row["number"]),
                "content_sha256": hashlib.sha256(
                    str(row["content"] or "").encode("utf-8")
                ).hexdigest(),
                "stored_content_sha256": str(row["content_sha256"] or ""),
                "content_revision": int(row["content_revision"] or 0),
                "status": str(row["status"] or ""),
            }
            for row in rows
        ]
        return self._stable_digest(identities)

    def _worldline_authority_snapshot(
        self,
        conn: sqlite3.Connection,
        novel_id: str,
        *,
        retained_through: int,
        allow_unproven_tail: bool = False,
    ) -> dict[str, Any]:
        formal = self._formal_identity_snapshot(
            conn,
            novel_id,
            allow_unproven_tail=allow_unproven_tail,
        )
        epoch = self._generation_epoch(conn, novel_id)
        generation_filter = self._generation_filter_snapshot(conn, novel_id)
        return {
            **formal,
            "retained_through": int(retained_through),
            "prefix_digest": self._prefix_digest(conn, novel_id, retained_through),
            "chapter_identity_digest": self._chapter_identity_digest(conn, novel_id),
            "generation_epoch": epoch,
            "worldline_filter": generation_filter,
            "run": self._generation_run_snapshot(conn, novel_id),
            "manifest_head": self._manifest_head_snapshot(conn, novel_id),
        }

    @staticmethod
    def _assert_snapshot_filter_is_consistent(snapshot: dict[str, Any]) -> None:
        generation_filter = snapshot["worldline_filter"]
        epoch = int(snapshot["generation_epoch"])
        if generation_filter["exists"]:
            if int(generation_filter["active_generation_epoch"] or 0) != epoch:
                raise WorldlineRegenerationError("worldline generation filter changed")
        elif epoch != 0:
            raise WorldlineRegenerationError("worldline generation filter is unavailable")

    @staticmethod
    def _is_default_created_run(
        run: Optional[dict[str, Any]], *, expected_epoch: int, target_chapters: int
    ) -> bool:
        return run == {
            "run_mode": "continuous",
            "state": "stopped",
            "generation_epoch": int(expected_epoch),
            "target_chapters": int(target_chapters),
            "current_formal_chapter": 0,
            "current_candidate_id": None,
            "current_candidate_chapter": None,
            "canonical_sync_status": "ready",
            "next_action": "",
            "last_error": "",
            "max_pending_candidates": 1,
            "prefetch": 0,
        }

    def _assert_authority_snapshot_matches(
        self,
        expected: dict[str, Any],
        actual: dict[str, Any],
        *,
        allow_created_run: bool = False,
        target_chapters: int = 0,
    ) -> None:
        if int(actual["generation_epoch"]) != int(expected["generation_epoch"]):
            raise WorldlineRegenerationError(
                "generation epoch changed; request a new worldline preview"
            )
        if actual["worldline_filter"] != expected["worldline_filter"]:
            raise WorldlineRegenerationError(
                "worldline generation filter changed; request a new worldline preview"
            )
        if actual.get("manifest_head") != expected.get("manifest_head"):
            raise WorldlineRegenerationError(
                "Manifest planning Head changed; request a new worldline preview"
            )
        if int(actual["retained_through"]) != int(expected["retained_through"]):
            raise WorldlineRegenerationError("worldline preview is invalid")
        if str(actual["prefix_digest"]) != str(expected["prefix_digest"]):
            raise WorldlineRegenerationError(
                "chapter prefix changed; request a new worldline preview"
            )
        if (
            int(actual["formal_head"]) != int(expected["formal_head"])
            or str(actual["formal_identity_digest"])
            != str(expected["formal_identity_digest"])
            or str(actual["chapter_identity_digest"])
            != str(expected["chapter_identity_digest"])
        ):
            raise WorldlineRegenerationError(
                "chapter tail changed; request a new worldline preview"
            )
        expected_run = expected.get("run")
        actual_run = actual.get("run")
        if expected_run == actual_run:
            return
        if (
            expected_run is None
            and allow_created_run
            and self._is_default_created_run(
                actual_run,
                expected_epoch=int(expected["generation_epoch"]),
                target_chapters=int(target_chapters),
            )
        ):
            return
        raise WorldlineRegenerationError(
            "generation run changed; request a new worldline preview"
        )

    @staticmethod
    def _validate_preview_row(preview_row: sqlite3.Row, preview: dict[str, Any]) -> None:
        try:
            matches = (
                str(preview["novel_id"]) == str(preview_row["novel_id"])
                and int(preview["start_chapter"]) == int(preview_row["start_chapter"])
                and int(preview["target_chapters"]) == int(preview_row["target_chapters"])
                and int(preview["current_generated_chapters"])
                == int(preview_row["current_generated_chapters"])
                and int(preview["retained_through"]) == int(preview_row["retained_through"])
                and str(preview["operation"]) == str(preview_row["operation"])
                and int(preview["generation_epoch"]) == int(preview_row["generation_epoch"])
                and str(preview["prefix_digest"]) == str(preview_row["prefix_digest"])
                and isinstance(preview.get("authority_snapshot"), dict)
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise WorldlineRegenerationError(
                "worldline preview is incomplete; request a new preview"
            ) from exc
        if not matches:
            raise WorldlineRegenerationError(
                "worldline preview changed; request a new preview"
            )

    def _assert_preview_authority_current(
        self,
        conn: sqlite3.Connection,
        novel_id: str,
        preview_row: sqlite3.Row,
        preview: dict[str, Any],
        *,
        allow_created_run: bool = False,
    ) -> dict[str, Any]:
        self._validate_preview_row(preview_row, preview)
        expected = preview["authority_snapshot"]
        if (
            int(expected.get("retained_through", -1))
            != int(preview["retained_through"])
            or int(expected.get("formal_head", -1))
            != int(preview["current_generated_chapters"])
            or int(expected.get("generation_epoch", -1))
            != int(preview["generation_epoch"])
            or str(expected.get("prefix_digest", "")) != str(preview["prefix_digest"])
        ):
            raise WorldlineRegenerationError("worldline preview is invalid")
        self._assert_snapshot_filter_is_consistent(expected)
        actual = self._worldline_authority_snapshot(
            conn,
            novel_id,
            retained_through=int(preview["retained_through"]),
        )
        self._assert_authority_snapshot_matches(
            expected,
            actual,
            allow_created_run=allow_created_run,
            target_chapters=int(preview["target_chapters"]),
        )
        return actual

    def _archive_source_snapshot(
        self, conn: sqlite3.Connection, archive_id: str, novel_id: str
    ) -> tuple[sqlite3.Row, str]:
        source = conn.execute(
            "SELECT * FROM worldline_archives WHERE id = ? AND novel_id = ?",
            (archive_id, novel_id),
        ).fetchone()
        if source is None:
            raise WorldlineRegenerationError("worldline archive was not found for this novel")
        entries = conn.execute(
            """
            SELECT source_table, source_key, chapter_number, payload_json, created_at
            FROM worldline_archive_entries
            WHERE archive_id = ?
            ORDER BY source_table, source_key, chapter_number, created_at
            """,
            (archive_id,),
        ).fetchall()
        snapshot = {
            "archive": dict(source),
            "entries": [dict(row) for row in entries],
        }
        return source, self._stable_digest(snapshot)

    def _update_run_for_rebuild(
        self,
        conn: sqlite3.Connection,
        *,
        novel_id: str,
        expected_run: dict[str, Any],
        run_mode: str,
        generation_epoch: int,
        target_chapters: int,
        current_formal_chapter: int,
        now: str,
    ) -> None:
        cursor = conn.execute(
            """
            UPDATE novel_generation_runs
            SET run_mode = ?, state = 'paused', generation_epoch = ?, target_chapters = ?,
                current_formal_chapter = ?, current_candidate_id = NULL, current_candidate_chapter = NULL,
                canonical_sync_status = 'rebuilding', next_action = 'rebuild_worldline', last_error = '',
                max_pending_candidates = 1, prefetch = 0, updated_at = ?
            WHERE novel_id = ? AND run_mode = ? AND state = ? AND generation_epoch = ?
              AND target_chapters = ? AND current_formal_chapter = ?
              AND current_candidate_id IS ? AND current_candidate_chapter IS ?
              AND canonical_sync_status = ? AND next_action = ? AND last_error = ?
              AND max_pending_candidates = ? AND prefetch = ?
            """,
            (
                run_mode,
                int(generation_epoch),
                int(target_chapters),
                int(current_formal_chapter),
                now,
                novel_id,
                expected_run["run_mode"],
                expected_run["state"],
                int(expected_run["generation_epoch"]),
                int(expected_run["target_chapters"]),
                int(expected_run["current_formal_chapter"]),
                expected_run["current_candidate_id"],
                expected_run["current_candidate_chapter"],
                expected_run["canonical_sync_status"],
                expected_run["next_action"],
                expected_run["last_error"],
                int(expected_run["max_pending_candidates"]),
                int(expected_run["prefetch"]),
            ),
        )
        if cursor.rowcount != 1:
            raise WorldlineRegenerationError("generation run changed during worldline reset")

    def _advance_generation_filter(
        self,
        conn: sqlite3.Connection,
        *,
        novel_id: str,
        expected_filter: dict[str, Any],
        generation_epoch: int,
        now: str,
    ) -> None:
        if expected_filter.get("exists"):
            cursor = conn.execute(
                """
                UPDATE worldline_generation_filters
                SET active_generation_epoch = ?, updated_at = ?
                WHERE novel_id = ? AND active_generation_epoch = ?
                """,
                (
                    int(generation_epoch),
                    now,
                    novel_id,
                    int(expected_filter["active_generation_epoch"]),
                ),
            )
            if cursor.rowcount != 1:
                raise WorldlineRegenerationError(
                    "worldline generation filter changed during worldline reset"
                )
            return
        try:
            cursor = conn.execute(
                """
                INSERT INTO worldline_generation_filters
                    (novel_id, active_generation_epoch, updated_at)
                VALUES (?, ?, ?)
                """,
                (novel_id, int(generation_epoch), now),
            )
        except sqlite3.IntegrityError as exc:
            raise WorldlineRegenerationError(
                "worldline generation filter changed during worldline reset"
            ) from exc
        if cursor.rowcount != 1:
            raise WorldlineRegenerationError(
                "worldline generation filter changed during worldline reset"
            )

    def _formal_chapter_head(self, novel_id: str) -> int:
        candidates = ChapterCandidateRepository(self._db or self.db_path)
        candidates.assert_formal_history_is_proven(novel_id)
        return candidates.formal_chapter_head(novel_id)

    def _ensure_run(self, conn: sqlite3.Connection, novel_id: str, target_chapters: int) -> None:
        conn.execute(
            """
            INSERT INTO novel_generation_runs
                (novel_id, run_mode, state, target_chapters, max_pending_candidates, prefetch, updated_at)
            VALUES (?, 'continuous', 'stopped', ?, 1, 0, ?)
            ON CONFLICT(novel_id) DO NOTHING
            """,
            (novel_id, target_chapters, self._now()),
        )

    @staticmethod
    def _assert_legacy_worldline_allowed(
        conn: sqlite3.Connection, novel_id: str
    ) -> None:
        """Block the legacy archive algorithm after Manifest cutover.

        The legacy implementation deletes contracts by mutable physical
        StoryNode identity.  A sealed manifest needs a dedicated rebase
        transaction, so any public Worldline mutation must remain closed
        until that transaction exists.
        """

        if is_manifest_authority(conn, novel_id):
            raise WorldlineRegenerationError(
                "manifest-authority Worldline requires manifest-aware rebase support"
            )

    def _run_manifest_projection_sync(self, coroutine) -> None:
        """Run the binding writer from this service's synchronous API surface."""

        try:
            asyncio.get_running_loop()
        except RuntimeError:
            asyncio.run(coroutine)
            return
        raise WorldlineRegenerationError(
            "Manifest Worldline mutation cannot run inside an active event loop"
        )

    def _create_manifest_rebase_plan(
        self,
        conn: sqlite3.Connection,
        *,
        novel_id: str,
        head: sqlite3.Row,
        start_chapter: int,
        retained_through: int,
        archive_id: str,
    ) -> tuple[Any, bool] | None:
        """Create a sealed target whose bindings contain only the retained tree."""

        repository = OutlineContractRepository(self._db or self.db_path)
        active_id = str(head["active_plan_revision_id"] or "")
        if not active_id:
            raise WorldlineRegenerationError("Manifest planning Head has no active revision")
        active = repository.get_plan_revision(active_id, _connection=conn)
        repository.validate_projection_bindings(active.id, conn)
        bindings = repository.projection_bindings_for_revision(
            active.id, _connection=conn
        )
        binding_by_logical = {
            str(row["logical_node_id"]): row for row in bindings
        }
        tail_logical_ids = {
            str(row["logical_node_id"])
            for row in bindings
            if str(row["level"] or "") == "chapter"
            and row["story_node_id"] is not None
            and int(row["number"]) >= int(start_chapter)
        }
        if not tail_logical_ids:
            return None

        item_by_logical = {item.logical_node_id: item for item in active.items}
        retained_ids = {
            str(row["logical_node_id"])
            for row in bindings
            if str(row["level"] or "") == "chapter"
            and row["story_node_id"] is not None
            and str(row["logical_node_id"]) not in tail_logical_ids
        }
        retained_ids.update(
            item.logical_node_id
            for item in active.items
            if item.level.value == "outline"
        )
        pending = list(retained_ids)
        while pending:
            item = item_by_logical[pending.pop()]
            parent_id = item.parent_logical_node_id
            if parent_id and parent_id not in retained_ids:
                retained_ids.add(parent_id)
                pending.append(parent_id)
        kept_source = [
            item for item in active.items if item.logical_node_id in retained_ids
        ]
        if not kept_source:
            raise WorldlineRegenerationError(
                "Manifest rebase would remove the immutable outline root"
            )
        kept_ids = {item.logical_node_id for item in kept_source}
        if any(
            item.parent_logical_node_id
            and item.parent_logical_node_id not in kept_ids
            for item in kept_source
        ):
            raise WorldlineRegenerationError(
                "Manifest rebase would leave an orphaned logical node"
            )

        removed_parents = {
            item.parent_logical_node_id
            for item in active.items
            if item.logical_node_id not in retained_ids
            and item.parent_logical_node_id is not None
        }
        groups: dict[tuple[str, str], list[Any]] = {}
        for item in kept_source:
            groups.setdefault(
                (item.parent_logical_node_id or "", item.level.value), []
            ).append(item)
        normalized: list[OutlinePlanItem] = []
        for siblings in groups.values():
            siblings.sort(key=lambda item: (item.sibling_index, item.logical_node_id))
            previous_digest = ""
            for sibling_index, item in enumerate(siblings):
                has_children = any(
                    child.parent_logical_node_id == item.logical_node_id
                    for child in kept_source
                )
                expansion_state = item.expansion_state
                if item.logical_node_id in removed_parents and not has_children:
                    expansion_state = "unexpanded"
                normalized.append(
                    OutlinePlanItem(
                        id=f"outline-plan-item-{uuid4()}",
                        logical_node_id=item.logical_node_id,
                        version_id=item.version_id,
                        version_digest=item.version_digest,
                        level=item.level,
                        sibling_index=sibling_index,
                        parent_logical_node_id=item.parent_logical_node_id,
                        expansion_state=expansion_state,
                        validated_parent_digest=item.validated_parent_digest,
                        validated_previous_sibling_digest=previous_digest,
                        is_reused=True,
                    )
                )
                previous_digest = item.version_digest

        # Reindexing can change sibling handoffs; recompute parent validation
        # after the full retained topology is known.
        by_id = {item.logical_node_id: item for item in normalized}
        normalized = [
            OutlinePlanItem(
                id=item.id,
                logical_node_id=item.logical_node_id,
                version_id=item.version_id,
                version_digest=item.version_digest,
                level=item.level,
                sibling_index=item.sibling_index,
                parent_logical_node_id=item.parent_logical_node_id,
                expansion_state=item.expansion_state,
                validated_parent_digest=(
                    by_id[item.parent_logical_node_id].version_digest
                    if item.parent_logical_node_id
                    else ""
                ),
                validated_previous_sibling_digest=item.validated_previous_sibling_digest,
                is_reused=item.is_reused,
            )
            for item in normalized
        ]
        prefix_digest = self._manifest_prefix_digest(
            conn, novel_id, retained_through
        )
        digest = canonical_plan_digest(
            canonical_prefix_digest=prefix_digest,
            items=normalized,
        )
        existing = conn.execute(
            """
            SELECT id FROM outline_plan_revisions
            WHERE novel_id = ? AND digest = ? AND sealed_at IS NOT NULL
              AND status = 'ready_for_review' AND reconciliation_status = 'aligned'
            ORDER BY revision DESC
            LIMIT 1
            """,
            (novel_id, digest),
        ).fetchone()
        if existing is not None:
            prior = repository.get_plan_revision(str(existing["id"]), _connection=conn)
            prior_bindings = {
                str(row["logical_node_id"]): row
                for row in repository.projection_bindings_for_revision(
                    prior.id, _connection=conn
                )
            }
            expected_ids = {item.logical_node_id for item in normalized}
            if (
                prior.canonical_prefix_digest != prefix_digest
                or set(prior_bindings) != expected_ids
                or any(
                    (source := binding_by_logical.get(logical_id)) is None
                    or any(
                        prior_bindings[logical_id].get(field) != source.get(field)
                        for field in (
                            "story_node_id",
                            "parent_story_node_id",
                            "number",
                            "order_index",
                        )
                    )
                    for logical_id in expected_ids
                )
            ):
                raise WorldlineRegenerationError(
                    "Manifest rebase digest collides with incompatible bindings"
                )
            repository.validate_projection_bindings(prior.id, conn)
            return prior, True
        revision = int(
            conn.execute(
                "SELECT COALESCE(MAX(revision), 0) + 1 AS revision "
                "FROM outline_plan_revisions WHERE novel_id = ?",
                (novel_id,),
            ).fetchone()["revision"]
        )
        plan_id = f"outline-plan-{uuid4()}"
        now = self._now()
        conn.execute(
            """
            INSERT INTO outline_plan_revisions
                (id, novel_id, revision, parent_plan_revision_id, status,
                 digest, base_plan_digest, replan_start_chapter,
                 canonical_prefix_digest, canonical_boundary_json,
                 reconciliation_status, reconciliation_report_json,
                 author_intent, created_by, created_at, updated_at)
            VALUES (?, ?, ?, ?, 'draft', ?, ?, ?, ?, ?, 'author_decision_required', ?, ?, ?, ?, ?)
            """,
            (
                plan_id,
                novel_id,
                revision,
                active.id,
                digest,
                active.digest,
                int(start_chapter),
                prefix_digest,
                json.dumps(
                    {
                        "formal_head": int(retained_through),
                        "worldline_archive_id": archive_id,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                json.dumps(
                    {
                        "status": "author_decision_required",
                        "worldline_rebase": True,
                        "archive_id": archive_id,
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                "worldline rebase",
                "worldline_regeneration",
                now,
                now,
            ),
        )
        repository._insert_plan_items(conn, plan_id, normalized, now)
        for item in normalized:
            source_binding = binding_by_logical.get(item.logical_node_id)
            if source_binding is None:
                raise WorldlineRegenerationError(
                    "Manifest rebase binding set is incomplete"
                )
            conn.execute(
                """
                INSERT INTO outline_plan_projection_bindings
                    (plan_revision_item_id, story_node_id, parent_story_node_id,
                     number, order_index)
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    item.id,
                    source_binding["story_node_id"],
                    source_binding["parent_story_node_id"],
                    source_binding["number"],
                    source_binding["order_index"],
                ),
            )
        working = conn.execute(
            """
            UPDATE outline_planning_heads
            SET working_plan_revision_id = ?, updated_at = ?
            WHERE novel_id = ? AND authority_mode = 'manifest'
              AND authority_generation = ? AND projection_generation = ?
              AND active_plan_revision_id = ? AND active_plan_digest = ?
              AND working_plan_revision_id IS NULL
            """,
            (
                plan_id,
                now,
                novel_id,
                int(head["authority_generation"]),
                int(head["projection_generation"]),
                active.id,
                active.digest,
            ),
        )
        if working.rowcount != 1:
            raise WorldlineRegenerationError(
                "Manifest planning Head changed during Worldline rebase"
            )
        report = OutlineContractService(
            contract_repository=repository,
            story_node_repository=StoryNodeRepository(self._db or self.db_path),
        ).reconcile_plan_boundary(
            novel_id=novel_id,
            plan_revision_id=plan_id,
            connection=conn,
        )
        if report.status != PlanReconciliationStatus.ALIGNED:
            raise WorldlineRegenerationError(
                "Manifest Worldline rebase requires an aligned formal boundary"
            )
        report_payload = {
            "plan_revision_id": plan_id,
            "status": report.status.value,
            "expected_formal_head": report.expected_formal_head,
            "actual_formal_head": report.actual_formal_head,
            "expected_prefix_digest": report.expected_prefix_digest,
            "actual_prefix_digest": report.actual_prefix_digest,
            "canonical_ready": report.canonical_ready,
            "memory_ready": report.memory_ready,
            "blockers": list(report.blockers),
            "worldline_rebase": {
                "archive_id": archive_id,
                "source_plan_revision_id": active.id,
                "source_plan_digest": active.digest,
                "start_chapter": int(start_chapter),
                "retained_through": int(retained_through),
                "authority_generation": int(head["authority_generation"] or 0),
                "projection_generation": int(head["projection_generation"] or 0),
            },
        }
        reconciled = conn.execute(
            """
            UPDATE outline_plan_revisions
            SET reconciliation_status = ?, reconciliation_report_json = ?,
                updated_at = ?
            WHERE id = ? AND sealed_at IS NULL AND status = 'draft' AND digest = ?
              AND reconciliation_status = 'author_decision_required'
            """,
            (
                PlanReconciliationStatus.ALIGNED.value,
                json.dumps(report_payload, ensure_ascii=False, sort_keys=True),
                now,
                plan_id,
                digest,
            ),
        )
        if reconciled.rowcount != 1:
            raise WorldlineRegenerationError(
                "Manifest Worldline rebase changed before reconciliation was recorded"
            )
        technical_blockers = repository.technical_blockers_for_plan(
            plan_id, _connection=conn
        )
        if technical_blockers:
            raise WorldlineRegenerationError(
                "Manifest Worldline rebase has technical blockers: "
                + "; ".join(technical_blockers)
            )
        try:
            return (
                repository._seal_plan_revision_locked(
                    conn, plan_id, clear_working_plan=False
                ),
                False,
            )
        except OutlineGateError as exc:
            raise WorldlineRegenerationError(
                "Manifest Worldline rebase plan is not publishable"
            ) from exc

    async def _apply_manifest_restore_projection(
        self,
        conn: sqlite3.Connection,
        *,
        novel_id: str,
        target_plan_revision_id: str,
        expected_active_plan_revision_id: str,
        expected_active_plan_digest: str,
        expected_authority_generation: int,
        expected_projection_generation: int,
    ) -> None:
        """Restore a historical binding set through the existing restore permit."""
        try:
            await PlanProjectionWriter(
                StoryNodeRepository(self._db or self.db_path)
            ).apply_bound_projection(
                conn,
                novel_id=novel_id,
                plan_revision_id=target_plan_revision_id,
                expected_active_plan_revision_id=expected_active_plan_revision_id,
                expected_active_plan_digest=expected_active_plan_digest,
                expected_authority_generation=int(expected_authority_generation),
                expected_projection_generation=int(expected_projection_generation),
                expected_working_plan_revision_id=None,
                operation="restore",
            )
        except (PlanningAuthorityError, sqlite3.Error) as exc:
            raise WorldlineRegenerationError(
                f"Manifest Worldline restore projection failed: {exc}"
            ) from exc

    def preview(self, novel_id: str, *, start_chapter: int, target_chapters: int) -> WorldlinePreview:
        if start_chapter < 1:
            raise WorldlineRegenerationError("start_chapter must be at least 1")
        if target_chapters < 1:
            raise WorldlineRegenerationError("target_chapters must be at least 1")
        if target_chapters < start_chapter:
            raise WorldlineRegenerationError("target_chapters must be at least start_chapter")
        conn = self._connection()
        manifest_mode = is_manifest_authority(conn, novel_id)
        manifest_head: Optional[dict[str, Any]] = None
        if manifest_mode:
            manifest_head = self._manifest_head_snapshot(conn, novel_id)
        else:
            self._assert_legacy_worldline_allowed(conn, novel_id)
        exists = conn.execute("SELECT 1 FROM novels WHERE id = ?", (novel_id,)).fetchone()
        if exists is None:
            raise KeyError(f"novel not found: {novel_id}")
        formal = self._formal_identity_snapshot(conn, novel_id)
        generated = int(formal["formal_head"])
        operation = "continue" if start_chapter > generated else "regenerate"
        retained = generated if operation == "continue" else max(0, start_chapter - 1)
        archive_from = start_chapter if operation == "regenerate" else None
        archive_to = generated if operation == "regenerate" else None
        if manifest_mode and operation == "regenerate":
            try:
                bound_tail = conn.execute(
                    """
                    SELECT 1
                    FROM outline_plan_revision_items AS item
                    JOIN outline_plan_projection_bindings AS binding
                      ON binding.plan_revision_item_id = item.id
                    WHERE item.plan_revision_id = ?
                      AND item.level = 'chapter'
                      AND binding.story_node_id IS NOT NULL
                      AND binding.number >= ?
                    LIMIT 1
                    """,
                    (manifest_head["active_plan_revision_id"], int(start_chapter)),
                ).fetchone()
            except sqlite3.Error as exc:
                raise WorldlineRegenerationError(
                    "manifest-aware Worldline projection is unavailable"
                ) from exc
            if bound_tail is None:
                raise WorldlineRegenerationError(
                    "manifest-aware Worldline requires a bound chapter tail"
                )
        counts: dict[str, int] = {}
        if operation == "regenerate":
            for table, chapter_column in (
                ("chapters", "number"),
                ("narrative_events", "chapter_number"),
                ("memory_atoms", "chapter_number"),
                ("chapter_candidates", "chapter_number"),
                ("story_nodes", "number"),
            ):
                cols = self._columns(conn, table)
                if {"novel_id", chapter_column} <= cols:
                    where = "node_type = 'chapter' AND " if table == "story_nodes" else ""
                    item = conn.execute(
                        f"SELECT COUNT(*) AS total FROM {table} WHERE novel_id = ? AND {where}{chapter_column} >= ?",
                        (novel_id, start_chapter),
                    ).fetchone()
                    counts[table] = int(item["total"] or 0)
        authority_snapshot = self._worldline_authority_snapshot(
            conn,
            novel_id,
            retained_through=retained,
        )
        self._assert_snapshot_filter_is_consistent(authority_snapshot)
        if int(authority_snapshot["formal_head"]) != generated:
            raise WorldlineRegenerationError(
                "chapter tail changed; request a new worldline preview"
            )
        prefix_digest = str(authority_snapshot["prefix_digest"])
        epoch = int(authority_snapshot["generation_epoch"])
        token = f"worldline-preview-{uuid4()}"
        preview = WorldlinePreview(
            token=token,
            novel_id=novel_id,
            operation=operation,
            start_chapter=start_chapter,
            target_chapters=target_chapters,
            current_generated_chapters=generated,
            retained_through=retained,
            archive_from=archive_from,
            archive_to=archive_to,
            generation_epoch=epoch,
            prefix_digest=prefix_digest,
            counts=counts,
        )
        conn.execute(
            """
            INSERT INTO worldline_regeneration_previews
                (token, novel_id, start_chapter, target_chapters, current_generated_chapters,
                 retained_through, operation, generation_epoch, prefix_digest, payload_json)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                token,
                novel_id,
                start_chapter,
                target_chapters,
                generated,
                retained,
                operation,
                epoch,
                prefix_digest,
                json.dumps(
                    {**asdict(preview), "authority_snapshot": authority_snapshot},
                    ensure_ascii=False,
                    sort_keys=True,
                ),
            ),
        )
        conn.commit()
        return preview

    @staticmethod
    def _require_manifest_preview(preview: dict[str, Any]) -> None:
        authority = preview.get("authority_snapshot")
        if not isinstance(authority, dict) or not isinstance(
            authority.get("manifest_head"), dict
        ):
            raise WorldlineRegenerationError(
                "manifest-aware Worldline preview is stale; create a new preview"
            )

    def _execute_manifest(
        self,
        novel_id: str,
        *,
        preview_token: str,
        run_mode: str,
        idempotency_key: str,
    ) -> WorldlineRegenerationResult:
        """Execute a Manifest preview without ever writing caller-supplied nodes."""

        conn = self._connection()
        preview_row = conn.execute(
            "SELECT * FROM worldline_regeneration_previews "
            "WHERE token = ? AND novel_id = ?",
            (preview_token, novel_id),
        ).fetchone()
        if preview_row is None:
            raise WorldlineRegenerationError("preview token is invalid for this novel")
        try:
            preview = json.loads(preview_row["payload_json"] or "{}")
        except (TypeError, ValueError, json.JSONDecodeError) as exc:
            raise WorldlineRegenerationError(
                "worldline preview is incomplete; request a new preview"
            ) from exc
        if not isinstance(preview, dict):
            raise WorldlineRegenerationError(
                "worldline preview is incomplete; request a new preview"
            )
        self._require_manifest_preview(preview)
        operation = str(preview.get("operation") or "")
        if operation not in {"continue", "regenerate"}:
            raise WorldlineRegenerationError(
                "manifest-aware Worldline preview has an unsupported operation"
            )
        if idempotency_key:
            existing = conn.execute(
                """
                SELECT result_json FROM worldline_regeneration_operations
                WHERE novel_id = ? AND idempotency_key = ? AND operation = ?
                """,
                (novel_id, idempotency_key, operation),
            ).fetchone()
            if existing is not None:
                return WorldlineRegenerationResult(
                    **json.loads(existing["result_json"] or "{}")
                )
        if preview_row["consumed_at"] is not None:
            raise WorldlineRegenerationError("preview token was already consumed")
        preview_row_digest = self._stable_digest(dict(preview_row))

        if operation == "continue":
            try:
                conn.execute("BEGIN IMMEDIATE")
                if idempotency_key:
                    existing = conn.execute(
                        """
                        SELECT result_json FROM worldline_regeneration_operations
                        WHERE novel_id = ? AND idempotency_key = ?
                          AND operation = 'continue'
                        """,
                        (novel_id, idempotency_key),
                    ).fetchone()
                    if existing is not None:
                        conn.commit()
                        return WorldlineRegenerationResult(
                            **json.loads(existing["result_json"] or "{}")
                        )
                locked_row = conn.execute(
                    "SELECT * FROM worldline_regeneration_previews "
                    "WHERE token = ? AND novel_id = ?",
                    (preview_token, novel_id),
                ).fetchone()
                if locked_row is None:
                    raise WorldlineRegenerationError(
                        "preview token is invalid for this novel"
                    )
                if self._stable_digest(dict(locked_row)) != preview_row_digest:
                    raise WorldlineRegenerationError(
                        "worldline preview changed; request a new preview"
                    )
                if locked_row["consumed_at"] is not None:
                    raise WorldlineRegenerationError("preview token was already consumed")
                locked_preview = json.loads(locked_row["payload_json"] or "{}")
                self._require_manifest_preview(locked_preview)
                self._assert_preview_authority_current(
                    conn, novel_id, locked_row, locked_preview
                )
                run = ChapterCandidateRepository(
                    self._db or self.db_path
                )._start_run_in_transaction(
                    conn,
                    novel_id,
                    run_mode=RunMode(run_mode),
                    target_chapters=int(locked_preview["target_chapters"]),
                )
                now = self._now()
                consumed = conn.execute(
                    """
                    UPDATE worldline_regeneration_previews
                    SET consumed_at = ?
                    WHERE token = ? AND novel_id = ? AND consumed_at IS NULL
                    """,
                    (now, preview_token, novel_id),
                )
                if consumed.rowcount != 1:
                    raise WorldlineRegenerationError("preview token was already consumed")
                result = WorldlineRegenerationResult(
                    operation="continue",
                    novel_id=novel_id,
                    archive_id=None,
                    generation_epoch=run.generation_epoch,
                    retained_through=int(locked_preview["retained_through"]),
                    next_action="generate_candidate",
                )
                if idempotency_key:
                    conn.execute(
                        """
                        INSERT INTO worldline_regeneration_operations
                            (novel_id, idempotency_key, operation, archive_id, result_json)
                        VALUES (?, ?, 'continue', NULL, ?)
                        """,
                        (
                            novel_id,
                            idempotency_key,
                            json.dumps(asdict(result), ensure_ascii=False),
                        ),
                    )
                conn.commit()
                return result
            except BaseException:
                if conn.in_transaction:
                    conn.rollback()
                raise

        start = int(preview["start_chapter"])
        retained = int(preview["retained_through"])
        old_epoch = int(preview["generation_epoch"])
        archive_id = f"worldline-{uuid4()}"
        try:
            conn.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                existing = conn.execute(
                    """
                    SELECT result_json FROM worldline_regeneration_operations
                    WHERE novel_id = ? AND idempotency_key = ?
                      AND operation = 'regenerate'
                    """,
                    (novel_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    conn.commit()
                    return WorldlineRegenerationResult(
                        **json.loads(existing["result_json"] or "{}")
                    )
            locked_row = conn.execute(
                "SELECT * FROM worldline_regeneration_previews "
                "WHERE token = ? AND novel_id = ?",
                (preview_token, novel_id),
            ).fetchone()
            if locked_row is None:
                raise WorldlineRegenerationError(
                    "preview token is invalid for this novel"
                )
            if self._stable_digest(dict(locked_row)) != preview_row_digest:
                raise WorldlineRegenerationError(
                    "worldline preview changed; request a new preview"
                )
            if locked_row["consumed_at"] is not None:
                raise WorldlineRegenerationError("preview token was already consumed")
            locked_preview = json.loads(locked_row["payload_json"] or "{}")
            self._require_manifest_preview(locked_preview)
            self._assert_preview_authority_current(
                conn, novel_id, locked_row, locked_preview
            )
            self._ensure_run(conn, novel_id, int(locked_preview["target_chapters"]))
            locked_authority = self._assert_preview_authority_current(
                conn,
                novel_id,
                locked_row,
                locked_preview,
                allow_created_run=True,
            )
            expected_run = locked_authority.get("run")
            if expected_run is None:
                raise WorldlineRegenerationError("generation run was not initialized")
            target_chapters = int(locked_preview["target_chapters"])
            start = int(locked_preview["start_chapter"])
            retained = int(locked_preview["retained_through"])
            old_epoch = int(locked_preview["generation_epoch"])
            end = int(locked_preview["current_generated_chapters"])
            new_epoch = old_epoch + 1
            now = self._now()
            head = conn.execute(
                """
                SELECT authority_mode, authority_generation, projection_generation,
                       active_plan_revision_id, active_plan_digest,
                       working_plan_revision_id
                FROM outline_planning_heads WHERE novel_id = ?
                """,
                (novel_id,),
            ).fetchone()
            if head is None or str(head["authority_mode"] or "") != "manifest":
                raise WorldlineRegenerationError("Manifest planning Head is missing")
            if head["working_plan_revision_id"] is not None:
                raise WorldlineRegenerationError(
                    "Manifest planning Head has an unfinished working revision"
                )
            manifest_head = locked_authority.get("manifest_head")
            if not isinstance(manifest_head, dict):
                raise WorldlineRegenerationError(
                    "manifest-aware Worldline preview is stale; create a new preview"
                )
            if (
                str(head["active_plan_revision_id"] or "")
                != str(manifest_head["active_plan_revision_id"])
                or str(head["active_plan_digest"] or "")
                != str(manifest_head["active_plan_digest"])
            ):
                raise WorldlineRegenerationError(
                    "Manifest planning Head changed during Worldline rebase"
                )
            archive_metadata = {
                "preview_token": preview_token,
                "counts": locked_preview.get("counts", {}),
                "manifest_head": manifest_head,
            }
            conn.execute(
                """
                INSERT INTO worldline_archives
                    (id, novel_id, old_generation_epoch, start_chapter, end_chapter,
                     retained_through, target_chapters, status, prefix_digest, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'archiving', ?, ?, ?)
                """,
                (
                    archive_id,
                    novel_id,
                    old_epoch,
                    start,
                    end,
                    retained,
                    target_chapters,
                    str(locked_preview["prefix_digest"]),
                    json.dumps(archive_metadata, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            source_lineage_digest = self._archive_manifest_source_lineage(
                conn,
                archive_id=archive_id,
                novel_id=novel_id,
                plan_revision_id=str(manifest_head["active_plan_revision_id"]),
                expected_plan_digest=str(manifest_head["active_plan_digest"]),
            )
            archive_metadata["source_lineage_digest"] = source_lineage_digest
            self._archive_tail(
                conn, archive_id, novel_id, start, manifest_rebase=True
            )
            source_archive_digest = self._archived_manifest_source_digest(
                conn, archive_id
            )
            archive_metadata["source_archive_digest"] = source_archive_digest
            metadata_updated = conn.execute(
                """
                UPDATE worldline_archives
                SET metadata_json = ?
                WHERE id = ? AND novel_id = ? AND status = 'archiving'
                """,
                (
                    json.dumps(archive_metadata, ensure_ascii=False, sort_keys=True),
                    archive_id,
                    novel_id,
                ),
            )
            if metadata_updated.rowcount != 1:
                raise WorldlineRegenerationError(
                    "Manifest source archive evidence could not be recorded"
                )
            self._record_manifest_head_snapshot(
                conn,
                archive_id=archive_id,
                novel_id=novel_id,
                snapshot=manifest_head,
                source_lineage_digest=source_lineage_digest,
                source_archive_digest=source_archive_digest,
            )
            rebase_target = self._create_manifest_rebase_plan(
                conn,
                novel_id=novel_id,
                head=head,
                start_chapter=start,
                retained_through=retained,
                archive_id=archive_id,
            )
            if rebase_target is None:
                raise WorldlineRegenerationError(
                    "manifest-aware Worldline requires a bound chapter tail"
                )
            target, reuses_sealed_plan = rebase_target
            self._run_manifest_projection_sync(
                PlanProjectionWriter(
                    StoryNodeRepository(self._db or self.db_path)
                ).apply_bound_projection(
                    conn,
                    novel_id=novel_id,
                    plan_revision_id=target.id,
                    expected_active_plan_revision_id=str(
                        head["active_plan_revision_id"]
                    ),
                    expected_active_plan_digest=str(head["active_plan_digest"] or ""),
                    expected_authority_generation=int(head["authority_generation"] or 0),
                    expected_projection_generation=int(
                        head["projection_generation"] or 0
                    ),
                    expected_working_plan_revision_id=(
                        None if reuses_sealed_plan else target.id
                    ),
                    operation="restore" if reuses_sealed_plan else "publish",
                )
            )
            self._update_run_for_rebuild(
                conn,
                novel_id=novel_id,
                expected_run=expected_run,
                run_mode=run_mode,
                generation_epoch=new_epoch,
                target_chapters=target_chapters,
                current_formal_chapter=retained,
                now=now,
            )
            self._advance_generation_filter(
                conn,
                novel_id=novel_id,
                expected_filter=locked_authority["worldline_filter"],
                generation_epoch=new_epoch,
                now=now,
            )
            self._queue_rebuild_jobs(conn, novel_id, new_epoch, archive_id, now)
            self._pause_legacy_autopilot(conn, novel_id)
            archived = conn.execute(
                "UPDATE worldline_archives SET status = 'archived' "
                "WHERE id = ? AND status = 'archiving'",
                (archive_id,),
            )
            if archived.rowcount != 1:
                raise WorldlineRegenerationError(
                    "worldline archive changed during reset"
                )
            consumed = conn.execute(
                """
                UPDATE worldline_regeneration_previews
                SET consumed_at = ?
                WHERE token = ? AND novel_id = ? AND consumed_at IS NULL
                """,
                (now, preview_token, novel_id),
            )
            if consumed.rowcount != 1:
                raise WorldlineRegenerationError("preview token was already consumed")
            result = WorldlineRegenerationResult(
                operation="regenerate",
                novel_id=novel_id,
                archive_id=archive_id,
                generation_epoch=new_epoch,
                retained_through=retained,
                next_action="rebuild_worldline",
            )
            if idempotency_key:
                conn.execute(
                    """
                    INSERT INTO worldline_regeneration_operations
                        (novel_id, idempotency_key, operation, archive_id, result_json)
                    VALUES (?, ?, 'regenerate', ?, ?)
                    """,
                    (
                        novel_id,
                        idempotency_key,
                        archive_id,
                        json.dumps(asdict(result), ensure_ascii=False),
                    ),
                )
            conn.commit()
            return result
        except (PlanningAuthorityError, OutlineGateError, sqlite3.Error) as exc:
            if conn.in_transaction:
                conn.rollback()
            raise WorldlineRegenerationError(
                f"Manifest Worldline rebase failed: {exc}"
            ) from exc
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise

    def execute(
        self,
        novel_id: str,
        *,
        preview_token: str,
        run_mode: str,
        idempotency_key: str = "",
    ) -> WorldlineRegenerationResult:
        if run_mode not in {"continuous", "chapter_review"}:
            raise WorldlineRegenerationError("run_mode must be continuous or chapter_review")
        conn = self._connection()
        if is_manifest_authority(conn, novel_id):
            return self._execute_manifest(
                novel_id,
                preview_token=preview_token,
                run_mode=run_mode,
                idempotency_key=idempotency_key,
            )
        self._assert_legacy_worldline_allowed(conn, novel_id)
        preview_row = conn.execute(
            "SELECT * FROM worldline_regeneration_previews WHERE token = ? AND novel_id = ?",
            (preview_token, novel_id),
        ).fetchone()
        if preview_row is None:
            raise WorldlineRegenerationError("preview token is invalid for this novel")
        preview = json.loads(preview_row["payload_json"] or "{}")
        operation = str(preview["operation"])
        if idempotency_key:
            existing = conn.execute(
                """
                SELECT result_json FROM worldline_regeneration_operations
                WHERE novel_id = ? AND idempotency_key = ? AND operation = ?
                """,
                (novel_id, idempotency_key, operation),
            ).fetchone()
            if existing is not None:
                payload = json.loads(existing["result_json"] or "{}")
                return WorldlineRegenerationResult(**payload)

        if preview_row["consumed_at"] is not None:
            raise WorldlineRegenerationError("preview token was already consumed")
        self._assert_preview_authority_current(conn, novel_id, preview_row, preview)
        preview_row_digest = self._stable_digest(dict(preview_row))
        retained = int(preview["retained_through"])

        if operation == "continue":
            try:
                conn.execute("BEGIN IMMEDIATE")
                self._assert_legacy_worldline_allowed(conn, novel_id)
                if idempotency_key:
                    existing = conn.execute(
                        """
                        SELECT result_json FROM worldline_regeneration_operations
                        WHERE novel_id = ? AND idempotency_key = ? AND operation = 'continue'
                        """,
                        (novel_id, idempotency_key),
                    ).fetchone()
                    if existing is not None:
                        conn.commit()
                        return WorldlineRegenerationResult(
                            **json.loads(existing["result_json"] or "{}")
                        )
                locked_preview_row = conn.execute(
                    "SELECT * FROM worldline_regeneration_previews WHERE token = ? AND novel_id = ?",
                    (preview_token, novel_id),
                ).fetchone()
                if locked_preview_row is None:
                    raise WorldlineRegenerationError("preview token is invalid for this novel")
                if self._stable_digest(dict(locked_preview_row)) != preview_row_digest:
                    raise WorldlineRegenerationError(
                        "worldline preview changed; request a new preview"
                    )
                if locked_preview_row["consumed_at"] is not None:
                    raise WorldlineRegenerationError("preview token was already consumed")
                locked_preview = json.loads(locked_preview_row["payload_json"] or "{}")
                self._assert_preview_authority_current(
                    conn,
                    novel_id,
                    locked_preview_row,
                    locked_preview,
                )
                run = ChapterCandidateRepository(self._db or self.db_path)._start_run_in_transaction(
                    conn,
                    novel_id,
                    run_mode=RunMode(run_mode),
                    target_chapters=int(locked_preview["target_chapters"]),
                )
                now = self._now()
                consumed = conn.execute(
                    """
                    UPDATE worldline_regeneration_previews
                    SET consumed_at = ?
                    WHERE token = ? AND novel_id = ? AND consumed_at IS NULL
                    """,
                    (now, preview_token, novel_id),
                )
                if consumed.rowcount != 1:
                    raise WorldlineRegenerationError("preview token was already consumed")
                result = WorldlineRegenerationResult(
                    operation="continue",
                    novel_id=novel_id,
                    archive_id=None,
                    generation_epoch=run.generation_epoch,
                    retained_through=int(locked_preview["retained_through"]),
                    next_action="generate_candidate",
                )
                if idempotency_key:
                    conn.execute(
                        """
                        INSERT INTO worldline_regeneration_operations
                            (novel_id, idempotency_key, operation, archive_id, result_json)
                        VALUES (?, ?, 'continue', NULL, ?)
                        """,
                        (
                            novel_id,
                            idempotency_key,
                            json.dumps(asdict(result), ensure_ascii=False),
                        ),
                    )
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            return result

        start = int(preview["start_chapter"])
        end = int(preview["current_generated_chapters"])
        old_epoch = int(preview["generation_epoch"])
        new_epoch = old_epoch + 1
        archive_id = f"worldline-{uuid4()}"
        now = self._now()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._assert_legacy_worldline_allowed(conn, novel_id)
            locked_preview_row = conn.execute(
                "SELECT * FROM worldline_regeneration_previews WHERE token = ? AND novel_id = ?",
                (preview_token, novel_id),
            ).fetchone()
            if locked_preview_row is None:
                raise WorldlineRegenerationError("preview token is invalid for this novel")
            if self._stable_digest(dict(locked_preview_row)) != preview_row_digest:
                raise WorldlineRegenerationError(
                    "worldline preview changed; request a new preview"
                )
            if locked_preview_row["consumed_at"] is not None:
                raise WorldlineRegenerationError("preview token was already consumed")
            locked_preview = json.loads(locked_preview_row["payload_json"] or "{}")
            self._assert_preview_authority_current(
                conn,
                novel_id,
                locked_preview_row,
                locked_preview,
            )
            self._ensure_run(conn, novel_id, int(preview["target_chapters"]))
            locked_authority = self._assert_preview_authority_current(
                conn,
                novel_id,
                locked_preview_row,
                locked_preview,
                allow_created_run=True,
            )
            expected_run = locked_authority.get("run")
            if expected_run is None:
                raise WorldlineRegenerationError("generation run was not initialized")
            conn.execute(
                """
                INSERT INTO worldline_archives
                    (id, novel_id, old_generation_epoch, start_chapter, end_chapter,
                     retained_through, target_chapters, status, prefix_digest, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'archiving', ?, ?, ?)
                """,
                (
                    archive_id,
                    novel_id,
                    old_epoch,
                    start,
                    end,
                    retained,
                    int(preview["target_chapters"]),
                    str(preview["prefix_digest"]),
                    json.dumps({"preview_token": preview_token, "counts": preview.get("counts", {})}, ensure_ascii=False),
                    now,
                ),
            )
            self._archive_tail(conn, archive_id, novel_id, start)
            self._update_run_for_rebuild(
                conn,
                novel_id=novel_id,
                expected_run=expected_run,
                run_mode=run_mode,
                generation_epoch=new_epoch,
                target_chapters=int(preview["target_chapters"]),
                current_formal_chapter=retained,
                now=now,
            )
            self._advance_generation_filter(
                conn,
                novel_id=novel_id,
                expected_filter=locked_authority["worldline_filter"],
                generation_epoch=new_epoch,
                now=now,
            )
            for job_type in ("canonical_facts", "memory", "vectors", "foreshadowing", "macro_summaries"):
                conn.execute(
                    """
                    INSERT INTO worldline_rebuild_jobs
                        (id, novel_id, generation_epoch, archive_id, job_type, status, created_at, updated_at)
                    VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                    ON CONFLICT(novel_id, generation_epoch, job_type) DO NOTHING
                    """,
                    (f"worldline-job-{uuid4()}", novel_id, new_epoch, archive_id, job_type, now, now),
                )
            self._pause_legacy_autopilot(conn, novel_id)
            archived = conn.execute(
                "UPDATE worldline_archives SET status = 'archived' WHERE id = ? AND status = 'archiving'",
                (archive_id,),
            )
            if archived.rowcount != 1:
                raise WorldlineRegenerationError("worldline archive changed during reset")
            consumed = conn.execute(
                """
                UPDATE worldline_regeneration_previews
                SET consumed_at = ?
                WHERE token = ? AND novel_id = ? AND consumed_at IS NULL
                """,
                (now, preview_token, novel_id),
            )
            if consumed.rowcount != 1:
                raise WorldlineRegenerationError("preview token was already consumed")
            result = WorldlineRegenerationResult(
                operation="regenerate",
                novel_id=novel_id,
                archive_id=archive_id,
                generation_epoch=new_epoch,
                retained_through=retained,
                next_action="rebuild_worldline",
            )
            if idempotency_key:
                conn.execute(
                    """
                    INSERT INTO worldline_regeneration_operations
                        (novel_id, idempotency_key, operation, archive_id, result_json)
                    VALUES (?, ?, 'regenerate', ?, ?)
                    """,
                    (novel_id, idempotency_key, archive_id, json.dumps(asdict(result), ensure_ascii=False)),
                )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return result

    def list_archives(self, novel_id: str) -> list[dict[str, Any]]:
        """Return read-only worldline archive metadata, newest first."""

        conn = self._connection()
        rows = conn.execute(
            """
            SELECT id, novel_id, old_generation_epoch, start_chapter, end_chapter,
                   retained_through, target_chapters, status, prefix_digest,
                   metadata_json, created_at, restored_at
            FROM worldline_archives
            WHERE novel_id = ?
            ORDER BY created_at DESC
            """,
            (novel_id,),
        ).fetchall()
        archives: list[dict[str, Any]] = []
        for row in rows:
            item = dict(row)
            try:
                item["metadata"] = json.loads(item.pop("metadata_json") or "{}")
            except (TypeError, ValueError):
                item["metadata"] = {}
                item.pop("metadata_json", None)
            archives.append(item)
        return archives

    def _restore_manifest(
        self,
        novel_id: str,
        *,
        archive_id: str,
        run_mode: str,
        idempotency_key: str,
    ) -> WorldlineRegenerationResult:
        """Restore Manifest source rows only after a binding projection cutover."""

        conn = self._connection()
        source, source_snapshot_digest = self._archive_source_snapshot(
            conn, archive_id, novel_id
        )
        if str(source["status"]) not in {"archived", "restored"}:
            raise WorldlineRegenerationError("worldline archive is not ready to restore")
        try:
            source_head_snapshot = self._load_manifest_head_snapshot(
                conn, archive_id=archive_id, novel_id=novel_id
            )
        except WorldlineRegenerationError as exc:
            raise WorldlineRegenerationError(
                "manifest-aware restore requires an immutable Head snapshot"
            ) from exc
        self._validate_manifest_archive_evidence(
            conn,
            source=source,
            archive_id=archive_id,
            head_snapshot=source_head_snapshot,
        )
        if idempotency_key:
            existing = conn.execute(
                """
                SELECT result_json FROM worldline_regeneration_operations
                WHERE novel_id = ? AND idempotency_key = ? AND operation = 'restore'
                """,
                (novel_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                return WorldlineRegenerationResult(
                    **json.loads(existing["result_json"] or "{}")
                )

        start = int(source["start_chapter"])
        retained = int(source["retained_through"])
        source_prefix = str(source["prefix_digest"] or "")
        authority_snapshot = self._worldline_authority_snapshot(
            conn,
            novel_id,
            retained_through=retained,
            allow_unproven_tail=True,
        )
        self._assert_snapshot_filter_is_consistent(authority_snapshot)
        if source_prefix and authority_snapshot["prefix_digest"] != source_prefix:
            raise WorldlineRegenerationError(
                "the retained prefix changed; create a new regeneration plan instead"
            )
        current_max_row = conn.execute(
            "SELECT COALESCE(MAX(number), 0) AS max_number FROM chapters "
            "WHERE novel_id = ?",
            (novel_id,),
        ).fetchone()
        current_max = int(current_max_row["max_number"] or 0)
        current_epoch = self._generation_epoch(conn, novel_id)
        new_epoch = current_epoch + 1
        replacement_archive_id = f"worldline-{uuid4()}"
        try:
            conn.execute("BEGIN IMMEDIATE")
            if idempotency_key:
                existing = conn.execute(
                    """
                    SELECT result_json FROM worldline_regeneration_operations
                    WHERE novel_id = ? AND idempotency_key = ?
                      AND operation = 'restore'
                    """,
                    (novel_id, idempotency_key),
                ).fetchone()
                if existing is not None:
                    conn.commit()
                    return WorldlineRegenerationResult(
                        **json.loads(existing["result_json"] or "{}")
                    )
            locked_source, locked_source_snapshot_digest = self._archive_source_snapshot(
                conn, archive_id, novel_id
            )
            if locked_source_snapshot_digest != source_snapshot_digest:
                raise WorldlineRegenerationError(
                    "worldline archive changed; create a new regeneration plan instead"
                )
            if str(locked_source["status"]) not in {"archived", "restored"}:
                raise WorldlineRegenerationError(
                    "worldline archive is not ready to restore"
                )
            source_head = self._load_manifest_head_snapshot(
                conn, archive_id=archive_id, novel_id=novel_id
            )
            if source_head != source_head_snapshot:
                raise WorldlineRegenerationError(
                    "worldline archive changed; create a new regeneration plan instead"
                )
            self._validate_manifest_archive_evidence(
                conn,
                source=locked_source,
                archive_id=archive_id,
                head_snapshot=source_head,
            )
            self._assert_authority_snapshot_matches(
                authority_snapshot,
                self._worldline_authority_snapshot(
                    conn,
                    novel_id,
                    retained_through=retained,
                    allow_unproven_tail=True,
                ),
            )
            self._ensure_run(conn, novel_id, int(source["target_chapters"]))
            locked_authority = self._worldline_authority_snapshot(
                conn,
                novel_id,
                retained_through=retained,
                allow_unproven_tail=True,
            )
            self._assert_authority_snapshot_matches(
                authority_snapshot,
                locked_authority,
                allow_created_run=True,
                target_chapters=int(source["target_chapters"]),
            )
            expected_run = locked_authority.get("run")
            if expected_run is None:
                raise WorldlineRegenerationError("generation run was not initialized")
            current_head = locked_authority.get("manifest_head")
            if not isinstance(current_head, dict):
                raise WorldlineRegenerationError("Manifest planning Head is missing")
            now = self._now()
            replacement_metadata = {
                "restoring_archive_id": archive_id,
                "manifest_head": current_head,
            }
            conn.execute(
                """
                INSERT INTO worldline_archives
                    (id, novel_id, old_generation_epoch, start_chapter, end_chapter,
                     retained_through, target_chapters, status, prefix_digest, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'archiving', ?, ?, ?)
                """,
                (
                    replacement_archive_id,
                    novel_id,
                    current_epoch,
                    start,
                    current_max,
                    retained,
                    int(source["target_chapters"]),
                    source_prefix,
                    json.dumps(replacement_metadata, ensure_ascii=False, sort_keys=True),
                    now,
                ),
            )
            source_lineage_digest = self._archive_manifest_source_lineage(
                conn,
                archive_id=replacement_archive_id,
                novel_id=novel_id,
                plan_revision_id=str(current_head["active_plan_revision_id"]),
                expected_plan_digest=str(current_head["active_plan_digest"] or ""),
            )
            replacement_metadata["source_lineage_digest"] = source_lineage_digest
            self._archive_tail(
                conn,
                replacement_archive_id,
                novel_id,
                start,
                manifest_rebase=True,
            )
            source_archive_digest = self._archived_manifest_source_digest(
                conn, replacement_archive_id
            )
            replacement_metadata["source_archive_digest"] = source_archive_digest
            metadata_updated = conn.execute(
                """
                UPDATE worldline_archives
                SET metadata_json = ?
                WHERE id = ? AND novel_id = ? AND status = 'archiving'
                """,
                (
                    json.dumps(replacement_metadata, ensure_ascii=False, sort_keys=True),
                    replacement_archive_id,
                    novel_id,
                ),
            )
            if metadata_updated.rowcount != 1:
                raise WorldlineRegenerationError(
                    "Manifest replacement archive evidence could not be recorded"
                )
            self._record_manifest_head_snapshot(
                conn,
                archive_id=replacement_archive_id,
                novel_id=novel_id,
                snapshot=current_head,
                source_lineage_digest=source_lineage_digest,
                source_archive_digest=source_archive_digest,
            )
            self._run_manifest_projection_sync(
                self._apply_manifest_restore_projection(
                    conn,
                    novel_id=novel_id,
                    target_plan_revision_id=str(source_head["active_plan_revision_id"]),
                    expected_active_plan_revision_id=str(
                        current_head["active_plan_revision_id"]
                    ),
                    expected_active_plan_digest=str(
                        current_head["active_plan_digest"] or ""
                    ),
                    expected_authority_generation=int(
                        current_head["authority_generation"]
                    ),
                    expected_projection_generation=int(
                        current_head["projection_generation"]
                    ),
                )
            )
            self._restore_source_rows(conn, archive_id, manifest=True)
            self._invalidate_runtime_summary_caches(conn, novel_id, start)
            restored_max_row = conn.execute(
                "SELECT COALESCE(MAX(number), 0) AS max_number FROM chapters "
                "WHERE novel_id = ?",
                (novel_id,),
            ).fetchone()
            restored_max = int(restored_max_row["max_number"] or 0)
            self._update_run_for_rebuild(
                conn,
                novel_id=novel_id,
                expected_run=expected_run,
                run_mode=run_mode,
                generation_epoch=new_epoch,
                target_chapters=int(source["target_chapters"]),
                current_formal_chapter=restored_max,
                now=now,
            )
            self._advance_generation_filter(
                conn,
                novel_id=novel_id,
                expected_filter=locked_authority["worldline_filter"],
                generation_epoch=new_epoch,
                now=now,
            )
            self._queue_rebuild_jobs(
                conn, novel_id, new_epoch, replacement_archive_id, now
            )
            self._pause_legacy_autopilot(conn, novel_id)
            replacement_archived = conn.execute(
                "UPDATE worldline_archives SET status = 'archived' "
                "WHERE id = ? AND status = 'archiving'",
                (replacement_archive_id,),
            )
            if replacement_archived.rowcount != 1:
                raise WorldlineRegenerationError(
                    "worldline archive changed during restore"
                )
            source_restored = conn.execute(
                """
                UPDATE worldline_archives
                SET status = 'restored', restored_at = ?
                WHERE id = ? AND novel_id = ? AND status = ?
                """,
                (now, archive_id, novel_id, str(locked_source["status"])),
            )
            if source_restored.rowcount != 1:
                raise WorldlineRegenerationError(
                    "worldline archive changed during restore"
                )
            result = WorldlineRegenerationResult(
                operation="restore",
                novel_id=novel_id,
                archive_id=replacement_archive_id,
                generation_epoch=new_epoch,
                retained_through=retained,
                next_action="rebuild_worldline",
            )
            if idempotency_key:
                conn.execute(
                    """
                    INSERT INTO worldline_regeneration_operations
                        (novel_id, idempotency_key, operation, archive_id, result_json)
                    VALUES (?, ?, 'restore', ?, ?)
                    """,
                    (
                        novel_id,
                        idempotency_key,
                        replacement_archive_id,
                        json.dumps(asdict(result), ensure_ascii=False),
                    ),
                )
            conn.commit()
            return result
        except (PlanningAuthorityError, OutlineGateError, sqlite3.Error) as exc:
            if conn.in_transaction:
                conn.rollback()
            raise WorldlineRegenerationError(
                f"Manifest Worldline restore failed: {exc}"
            ) from exc
        except BaseException:
            if conn.in_transaction:
                conn.rollback()
            raise

    def restore(
        self,
        novel_id: str,
        *,
        archive_id: str,
        run_mode: str,
        idempotency_key: str = "",
    ) -> WorldlineRegenerationResult:
        """Restore an archived tail after archiving the current active tail.

        The archived prose, plan and chapter-provenance facts come back into the
        active branch.  Read models and vectors are deliberately rebuilt from
        that restored source of truth rather than copied as stale projections.
        """

        if run_mode not in {"continuous", "chapter_review"}:
            raise WorldlineRegenerationError("run_mode must be continuous or chapter_review")
        conn = self._connection()
        if is_manifest_authority(conn, novel_id):
            return self._restore_manifest(
                novel_id,
                archive_id=archive_id,
                run_mode=run_mode,
                idempotency_key=idempotency_key,
            )
        self._assert_legacy_worldline_allowed(conn, novel_id)
        source, source_snapshot_digest = self._archive_source_snapshot(
            conn,
            archive_id,
            novel_id,
        )
        if str(source["status"]) not in {"archived", "restored"}:
            raise WorldlineRegenerationError("worldline archive is not ready to restore")
        if idempotency_key:
            existing = conn.execute(
                """
                SELECT result_json FROM worldline_regeneration_operations
                WHERE novel_id = ? AND idempotency_key = ? AND operation = 'restore'
                """,
                (novel_id, idempotency_key),
            ).fetchone()
            if existing is not None:
                return WorldlineRegenerationResult(**json.loads(existing["result_json"] or "{}"))

        start = int(source["start_chapter"])
        retained = int(source["retained_through"])
        source_prefix = str(source["prefix_digest"] or "")
        authority_snapshot = self._worldline_authority_snapshot(
            conn,
            novel_id,
            retained_through=retained,
            allow_unproven_tail=True,
        )
        self._assert_snapshot_filter_is_consistent(authority_snapshot)
        if source_prefix and authority_snapshot["prefix_digest"] != source_prefix:
            raise WorldlineRegenerationError("the retained prefix changed; create a new regeneration plan instead")

        current_max_row = conn.execute(
            "SELECT COALESCE(MAX(number), 0) AS max_number FROM chapters WHERE novel_id = ?",
            (novel_id,),
        ).fetchone()
        current_max = int(current_max_row["max_number"] or 0)
        current_epoch = self._generation_epoch(conn, novel_id)
        new_epoch = current_epoch + 1
        replacement_archive_id = f"worldline-{uuid4()}"
        now = self._now()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._assert_legacy_worldline_allowed(conn, novel_id)
            locked_source, locked_source_snapshot_digest = self._archive_source_snapshot(
                conn,
                archive_id,
                novel_id,
            )
            if locked_source_snapshot_digest != source_snapshot_digest:
                raise WorldlineRegenerationError(
                    "worldline archive changed; create a new regeneration plan instead"
                )
            if str(locked_source["status"]) not in {"archived", "restored"}:
                raise WorldlineRegenerationError("worldline archive is not ready to restore")
            self._assert_authority_snapshot_matches(
                authority_snapshot,
                self._worldline_authority_snapshot(
                    conn,
                    novel_id,
                    retained_through=retained,
                    allow_unproven_tail=True,
                ),
            )
            self._ensure_run(conn, novel_id, int(source["target_chapters"]))
            locked_authority = self._worldline_authority_snapshot(
                conn,
                novel_id,
                retained_through=retained,
                allow_unproven_tail=True,
            )
            self._assert_authority_snapshot_matches(
                authority_snapshot,
                locked_authority,
                allow_created_run=True,
                target_chapters=int(source["target_chapters"]),
            )
            expected_run = locked_authority.get("run")
            if expected_run is None:
                raise WorldlineRegenerationError("generation run was not initialized")
            conn.execute(
                """
                INSERT INTO worldline_archives
                    (id, novel_id, old_generation_epoch, start_chapter, end_chapter,
                     retained_through, target_chapters, status, prefix_digest, metadata_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'archiving', ?, ?, ?)
                """,
                (
                    replacement_archive_id,
                    novel_id,
                    current_epoch,
                    start,
                    current_max,
                    retained,
                    int(source["target_chapters"]),
                    source_prefix,
                    json.dumps({"restoring_archive_id": archive_id}, ensure_ascii=False),
                    now,
                ),
            )
            self._archive_tail(conn, replacement_archive_id, novel_id, start)
            self._restore_source_rows(conn, archive_id)
            # Archived node metadata can contain pre-fix runtime summaries.
            # Rebuild jobs own their next visible version after the epoch switch.
            self._invalidate_runtime_summary_caches(conn, novel_id, start)
            restored_max_row = conn.execute(
                "SELECT COALESCE(MAX(number), 0) AS max_number FROM chapters WHERE novel_id = ?",
                (novel_id,),
            ).fetchone()
            restored_max = int(restored_max_row["max_number"] or 0)
            self._update_run_for_rebuild(
                conn,
                novel_id=novel_id,
                expected_run=expected_run,
                run_mode=run_mode,
                generation_epoch=new_epoch,
                target_chapters=int(source["target_chapters"]),
                current_formal_chapter=restored_max,
                now=now,
            )
            self._advance_generation_filter(
                conn,
                novel_id=novel_id,
                expected_filter=locked_authority["worldline_filter"],
                generation_epoch=new_epoch,
                now=now,
            )
            self._queue_rebuild_jobs(conn, novel_id, new_epoch, replacement_archive_id, now)
            self._pause_legacy_autopilot(conn, novel_id)
            replacement_archived = conn.execute(
                "UPDATE worldline_archives SET status = 'archived' WHERE id = ? AND status = 'archiving'",
                (replacement_archive_id,),
            )
            if replacement_archived.rowcount != 1:
                raise WorldlineRegenerationError("worldline archive changed during restore")
            source_restored = conn.execute(
                """
                UPDATE worldline_archives
                SET status = 'restored', restored_at = ?
                WHERE id = ? AND novel_id = ? AND status = ?
                """,
                (now, archive_id, novel_id, str(locked_source["status"])),
            )
            if source_restored.rowcount != 1:
                raise WorldlineRegenerationError("worldline archive changed during restore")
            result = WorldlineRegenerationResult(
                operation="restore",
                novel_id=novel_id,
                archive_id=replacement_archive_id,
                generation_epoch=new_epoch,
                retained_through=retained,
                next_action="rebuild_worldline",
            )
            if idempotency_key:
                conn.execute(
                    """
                    INSERT INTO worldline_regeneration_operations
                        (novel_id, idempotency_key, operation, archive_id, result_json)
                    VALUES (?, ?, 'restore', ?, ?)
                    """,
                    (novel_id, idempotency_key, replacement_archive_id, json.dumps(asdict(result), ensure_ascii=False)),
                )
            conn.commit()
        except BaseException:
            conn.rollback()
            raise
        return result

    def _restore_source_rows(
        self,
        conn: sqlite3.Connection,
        archive_id: str,
        *,
        manifest: bool = False,
    ) -> None:
        """Restore source-of-truth rows in FK-safe order, never old read models."""

        entries = conn.execute(
            """
            SELECT source_table, payload_json FROM worldline_archive_entries
            WHERE archive_id = ?
            ORDER BY created_at, id
            """,
            (archive_id,),
        ).fetchall()
        by_table: dict[str, list[dict[str, Any]]] = {}
        for entry in entries:
            try:
                payload = json.loads(entry["payload_json"] or "{}")
            except (TypeError, ValueError):
                continue
            if isinstance(payload, dict):
                by_table.setdefault(str(entry["source_table"]), []).append(payload)

        # Read models are safely reconstructed from formal chapters and source
        # facts.  Only candidates with immutable formal-commit evidence belong
        # to the restored authority chain; unfinished review artifacts stay archived.
        excluded = {
            "memory_projections",
            "memory_engine_state",
            "novel_foreshadow_registry",
            "novel_snapshots",
            "novel_checkpoints",
            "checkpoints",
        }
        if manifest:
            # Immutable Manifest contracts and physical StoryNodes are
            # restored only by the binding writer, never by archive payload
            # INSERT OR REPLACE.
            excluded.update(
                {
                    "story_nodes",
                    "outline_contracts",
                    "outline_contract_versions",
                    "outline_plan_projections",
                }
            )
        formal_candidate_ids = {
            str(payload.get("candidate_id") or "")
            for payload in by_table.get("chapter_candidate_formal_commits", [])
            if payload.get("candidate_id")
        }
        by_table["chapter_candidates"] = [
            payload
            for payload in by_table.get("chapter_candidates", [])
            if str(payload.get("id") or "") in formal_candidate_ids
        ]
        by_table["chapter_candidate_versions"] = [
            payload
            for payload in by_table.get("chapter_candidate_versions", [])
            if str(payload.get("candidate_id") or "") in formal_candidate_ids
        ]
        order = (
            "chapters",
            "pre_candidate_formal_history",
            "chapter_candidates",
            "chapter_candidate_versions",
            "chapter_candidate_formal_commits",
            "story_nodes",
            "outline_contracts",
            "outline_contract_versions",
            "outline_plan_projections",
            "beat_sheets",
            "chapter_drafts",
            "chapter_elements",
            "chapter_scenes",
            "chapter_narrative_commits",
            "chapter_reviews",
            "narrative_events",
            "memory_atoms",
            "chapter_summaries",
            "plot_points",
            "triples",
            "triple_more_chapters",
            "causal_edges",
            "foreshadows",
            "foreshadows_reopened",
            "voice_vault",
            "chapter_style_scores",
            "anti_ai_audits",
            "chapter_bridges",
            "chapter_entity_mentions",
            "prop_events",
            "governance_reports",
            "reader_simulations",
            "chapter_evolution_snapshots",
            "chapter_evolution_action_log",
            "chapter_evolution_conflicts",
            "character_voice_samples",
        )
        for table in order:
            if table in excluded:
                continue
            destination = "foreshadows" if table == "foreshadows_reopened" else table
            for payload in by_table.get(table, []):
                self._restore_row(conn, destination, payload)

    def _restore_row(self, conn: sqlite3.Connection, table: str, payload: dict[str, Any]) -> None:
        columns = self._columns(conn, table)
        fields = [key for key in payload if key in columns]
        if not fields:
            return
        placeholders = ", ".join("?" for _ in fields)
        quoted = ", ".join(fields)
        conn.execute(
            f"INSERT OR REPLACE INTO {table} ({quoted}) VALUES ({placeholders})",
            tuple(payload[field] for field in fields),
        )

    def _queue_rebuild_jobs(
        self,
        conn: sqlite3.Connection,
        novel_id: str,
        generation_epoch: int,
        archive_id: str,
        now: str,
    ) -> None:
        for job_type in ("canonical_facts", "memory", "vectors", "foreshadowing", "macro_summaries"):
            conn.execute(
                """
                INSERT INTO worldline_rebuild_jobs
                    (id, novel_id, generation_epoch, archive_id, job_type, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'pending', ?, ?)
                ON CONFLICT(novel_id, generation_epoch, job_type) DO NOTHING
                """,
                (f"worldline-job-{uuid4()}", novel_id, generation_epoch, archive_id, job_type, now, now),
            )

    def _archive_tail(
        self,
        conn: sqlite3.Connection,
        archive_id: str,
        novel_id: str,
        start: int,
        *,
        manifest_rebase: bool = False,
    ) -> None:
        """Archive known chapter-provenance rows first, then remove the active tail."""

        if not manifest_rebase:
            self._assert_legacy_worldline_allowed(conn, novel_id)

        # Macro/checkpoint summaries are derived runtime caches.  They must be
        # invalid before the new Worldline epoch becomes observable, while the
        # immutable planning projection remains untouched.
        self._invalidate_runtime_summary_caches(conn, novel_id, start)

        tail_chapters = self._select_rows(
            conn, "chapters", "novel_id = ? AND number >= ?", (novel_id, start)
        )
        chapter_ids = [str(row["id"]) for row in tail_chapters]
        tail_node_rows = self._select_rows(
            conn,
            "story_nodes",
            "novel_id = ? AND node_type = 'chapter' AND number >= ?",
            (novel_id, start),
        )
        node_ids = [str(row["id"]) for row in tail_node_rows]

        # Candidate aggregate and its immutable content revisions.
        candidate_rows = self._archive_rows_query(
            conn,
            archive_id,
            "chapter_candidates",
            "SELECT * FROM chapter_candidates WHERE novel_id = ? AND chapter_number >= ?",
            (novel_id, start),
        )
        candidate_ids = [str(row["id"]) for row in candidate_rows]
        self._archive_rows_by_ids(conn, archive_id, "chapter_candidate_versions", "candidate_id", candidate_ids)
        self._archive_rows_by_ids(conn, archive_id, "chapter_candidate_formal_commits", "candidate_id", candidate_ids)
        self._delete_rows_by_ids(conn, "chapter_candidates", "id", candidate_ids)

        # Chapter row children that refer to a chapter DB id rather than a number.
        self._archive_rows_by_ids(conn, archive_id, "beat_sheets", "chapter_id", chapter_ids)
        self._archive_rows_by_ids(conn, archive_id, "chapter_drafts", "chapter_id", chapter_ids)
        self._archive_rows_by_ids(conn, archive_id, "chapter_elements", "chapter_id", node_ids)
        self._archive_rows_by_ids(conn, archive_id, "chapter_scenes", "chapter_id", node_ids)

        # Regular provenance tables keyed by (novel_id, chapter_number).
        for table in (
            "chapter_narrative_commits",
            "chapter_reviews",
            "narrative_events",
            "voice_vault",
            "chapter_style_scores",
            "anti_ai_audits",
            "chapter_bridges",
            "chapter_entity_mentions",
            "prop_events",
            "governance_reports",
            "reader_simulations",
            "memory_atoms",
            "chapter_evolution_snapshots",
            "chapter_evolution_action_log",
            "chapter_evolution_conflicts",
            "character_voice_samples",
        ):
            self._archive_rows_by_chapter(conn, archive_id, table, novel_id, start)
        # Explicit legacy provenance has RESTRICT foreign keys to chapters;
        # archive it with the tail before deleting those chapter rows.
        self._archive_rows_by_chapter(
            conn, archive_id, "pre_candidate_formal_history", novel_id, start
        )
        self._archive_rows_by_chapter(
            conn, archive_id, "causal_edges", novel_id, start, chapter_column="source_chapter"
        )
        timeline_columns = self._columns(conn, "bible_timeline_notes")
        if {"source_type", "chapter_number"} <= timeline_columns:
            self._archive_rows_query(
                conn,
                archive_id,
                "bible_timeline_notes",
                """
                SELECT * FROM bible_timeline_notes
                WHERE novel_id = ? AND source_type = 'chapter_aftermath'
                  AND chapter_number >= ?
                """,
                (novel_id, start),
                chapter_column="chapter_number",
            )
            self._delete_query(
                conn,
                "bible_timeline_notes",
                "novel_id = ? AND source_type = 'chapter_aftermath' AND chapter_number >= ?",
                (novel_id, start),
            )
        else:
            self._archive_rows_by_chapter(
                conn,
                archive_id,
                "bible_timeline_notes",
                novel_id,
                start,
                chapter_column="chapter_number",
            )
        self._archive_rows_by_chapter(
            conn,
            archive_id,
            "character_states",
            novel_id,
            start,
            chapter_column="last_updated_chapter",
        )

        # Summaries / plot points are linked through their owning aggregate.
        self._archive_rows_query(
            conn,
            archive_id,
            "chapter_summaries",
            """
            SELECT s.* FROM chapter_summaries AS s
            JOIN knowledge AS k ON k.id = s.knowledge_id
            WHERE k.novel_id = ? AND s.chapter_number >= ?
            """,
            (novel_id, start),
            chapter_column="chapter_number",
        )
        self._delete_query(
            conn,
            "chapter_summaries",
            "knowledge_id IN (SELECT id FROM knowledge WHERE novel_id = ?) AND chapter_number >= ?",
            (novel_id, start),
        )
        self._archive_rows_query(
            conn,
            archive_id,
            "plot_points",
            """
            SELECT p.* FROM plot_points AS p
            JOIN plot_arcs AS a ON a.id = p.plot_arc_id
            WHERE a.novel_id = ? AND p.chapter_number >= ?
            """,
            (novel_id, start),
            chapter_column="chapter_number",
        )
        self._delete_query(
            conn,
            "plot_points",
            "plot_arc_id IN (SELECT id FROM plot_arcs WHERE novel_id = ?) AND chapter_number >= ?",
            (novel_id, start),
        )

        self._archive_rows_by_chapter(conn, archive_id, "triples", novel_id, start)
        self._archive_rows_by_chapter(conn, archive_id, "triple_more_chapters", novel_id, start)

        if not manifest_rebase:
            # Legacy contracts are mutable physical projections.  Manifest
            # contracts remain immutable historical evidence and are never
            # deleted by this archive path.
            self._archive_rows_query(
                conn,
                archive_id,
                "outline_contracts",
                "SELECT * FROM outline_contracts WHERE novel_id = ? AND story_node_id IN ({})".format(
                    ",".join("?" for _ in node_ids) or "''"
                ),
                (novel_id, *node_ids),
                chapter_column=None,
            )
            contract_rows = self._select_rows_query(
                conn,
                "SELECT * FROM outline_contracts WHERE novel_id = ? AND story_node_id IN ({})".format(
                    ",".join("?" for _ in node_ids) or "''"
                ),
                (novel_id, *node_ids),
            )
            contract_ids = [str(row["id"]) for row in contract_rows]
            self._archive_rows_by_ids(
                conn,
                archive_id,
                "outline_contract_versions",
                "contract_id",
                contract_ids,
            )
            self._archive_rows_by_ids(
                conn,
                archive_id,
                "outline_plan_projections",
                "contract_id",
                contract_ids,
            )
            self._delete_rows_by_ids(conn, "outline_contracts", "id", contract_ids)

        self._archive_rows(conn, archive_id, "story_nodes", tail_node_rows, chapter_column="number")
        if not manifest_rebase:
            self._delete_query(
                conn,
                "story_nodes",
                "novel_id = ? AND node_type = 'chapter' AND number >= ?",
                (novel_id, start),
            )

        # Re-open promises that were only resolved by a retired future chapter.
        self._archive_foreshadow_changes(conn, archive_id, novel_id, start)
        # Projections and checkpoint/snapshot state are derivative; rebuild them from the retained prefix.
        self._archive_and_delete_all_novel_rows(conn, archive_id, "memory_projections", novel_id)
        self._archive_and_delete_all_novel_rows(conn, archive_id, "memory_engine_state", novel_id)
        self._archive_foreshadow_registry_prefix(conn, archive_id, novel_id, start)
        self._archive_checkpoints_and_snapshots(conn, archive_id, novel_id, start, chapter_ids)

        # Formal chapters are last because multiple archived tables reference them.
        self._archive_rows(conn, archive_id, "chapters", tail_chapters, chapter_column="number")
        self._delete_query(conn, "chapters", "novel_id = ? AND number >= ?", (novel_id, start))

    def _invalidate_runtime_summary_caches(
        self,
        conn: sqlite3.Connection,
        novel_id: str,
        start_chapter: int,
    ) -> None:
        StoryNodeRepository(self._db or self.db_path).invalidate_runtime_summary_caches(
            novel_id,
            start_chapter,
            _connection=conn,
            _commit=False,
        )

    def _archive_rows_by_chapter(
        self,
        conn: sqlite3.Connection,
        archive_id: str,
        table: str,
        novel_id: str,
        start: int,
        *,
        chapter_column: str = "chapter_number",
    ) -> list[sqlite3.Row]:
        cols = self._columns(conn, table)
        if not {"novel_id", chapter_column} <= cols:
            return []
        rows = self._select_rows(
            conn, table, f"novel_id = ? AND {chapter_column} >= ?", (novel_id, start)
        )
        self._archive_rows(conn, archive_id, table, rows, chapter_column=chapter_column)
        self._delete_query(conn, table, f"novel_id = ? AND {chapter_column} >= ?", (novel_id, start))
        return rows

    def _archive_rows_by_ids(
        self,
        conn: sqlite3.Connection,
        archive_id: str,
        table: str,
        id_column: str,
        ids: Iterable[str],
    ) -> list[sqlite3.Row]:
        ids = list(ids)
        if not ids or id_column not in self._columns(conn, table):
            return []
        placeholders = ",".join("?" for _ in ids)
        rows = self._select_rows(conn, table, f"{id_column} IN ({placeholders})", tuple(ids))
        self._archive_rows(conn, archive_id, table, rows)
        self._delete_query(conn, table, f"{id_column} IN ({placeholders})", tuple(ids))
        return rows

    def _delete_rows_by_ids(self, conn: sqlite3.Connection, table: str, id_column: str, ids: Iterable[str]) -> None:
        ids = list(ids)
        if not ids or id_column not in self._columns(conn, table):
            return
        self._delete_query(conn, table, f"{id_column} IN ({','.join('?' for _ in ids)})", tuple(ids))

    def _select_rows(self, conn: sqlite3.Connection, table: str, where: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
        if not self._table_exists(conn, table):
            return []
        return list(conn.execute(f"SELECT * FROM {table} WHERE {where}", params).fetchall())

    def _select_rows_query(self, conn: sqlite3.Connection, query: str, params: tuple[Any, ...]) -> list[sqlite3.Row]:
        return list(conn.execute(query, params).fetchall())

    def _archive_rows_query(
        self,
        conn: sqlite3.Connection,
        archive_id: str,
        table: str,
        query: str,
        params: tuple[Any, ...],
        *,
        chapter_column: Optional[str] = "chapter_number",
    ) -> list[sqlite3.Row]:
        if not self._table_exists(conn, table):
            return []
        rows = self._select_rows_query(conn, query, params)
        self._archive_rows(conn, archive_id, table, rows, chapter_column=chapter_column)
        return rows

    def _archive_rows(
        self,
        conn: sqlite3.Connection,
        archive_id: str,
        source_table: str,
        rows: Iterable[sqlite3.Row],
        *,
        chapter_column: Optional[str] = "chapter_number",
    ) -> None:
        for index, row in enumerate(rows):
            payload = self._row_dict(row)
            source_key = str(
                payload.get("id")
                or payload.get("event_id")
                or payload.get("snapshot_id")
                or payload.get("candidate_id")
                or payload.get("chapter_id")
                or f"row-{index}-{hashlib.sha256(json.dumps(payload, default=str, sort_keys=True).encode()).hexdigest()[:16]}"
            )
            chapter_number = payload.get(chapter_column) if chapter_column else None
            conn.execute(
                """
                INSERT OR IGNORE INTO worldline_archive_entries
                    (id, archive_id, source_table, source_key, chapter_number, payload_json, created_at)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    f"worldline-entry-{uuid4()}",
                    archive_id,
                    source_table,
                    source_key,
                    chapter_number,
                    json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
                    self._now(),
                ),
            )

    def _delete_query(self, conn: sqlite3.Connection, table: str, where: str, params: tuple[Any, ...]) -> None:
        if self._table_exists(conn, table):
            conn.execute(f"DELETE FROM {table} WHERE {where}", params)

    def _archive_and_delete_all_novel_rows(
        self, conn: sqlite3.Connection, archive_id: str, table: str, novel_id: str
    ) -> None:
        if "novel_id" not in self._columns(conn, table):
            return
        rows = self._select_rows(conn, table, "novel_id = ?", (novel_id,))
        self._archive_rows(conn, archive_id, table, rows, chapter_column=None)
        self._delete_query(conn, table, "novel_id = ?", (novel_id,))

    def _archive_foreshadow_registry_prefix(
        self, conn: sqlite3.Connection, archive_id: str, novel_id: str, start: int
    ) -> None:
        """Archive the snapshot but retain only entries from the formal prefix."""

        if not {"novel_id", "payload"} <= self._columns(conn, "novel_foreshadow_registry"):
            return
        rows = self._select_rows(
            conn, "novel_foreshadow_registry", "novel_id = ?", (novel_id,)
        )
        if not rows:
            return
        self._archive_rows(
            conn, archive_id, "novel_foreshadow_registry", rows, chapter_column=None
        )
        try:
            payload = json.loads(rows[0]["payload"] or "{}")
            retained = self._retain_foreshadow_prefix(payload, start)
            if retained is None:
                self._delete_query(
                    conn, "novel_foreshadow_registry", "novel_id = ?", (novel_id,)
                )
                return
            conn.execute(
                "UPDATE novel_foreshadow_registry "
                "SET payload = ?, updated_at = ? WHERE novel_id = ?",
                (json.dumps(retained, ensure_ascii=False), self._now(), novel_id),
            )
        except (TypeError, json.JSONDecodeError):
            # Invalid legacy JSON is not safe continuity input.
            self._delete_query(
                conn, "novel_foreshadow_registry", "novel_id = ?", (novel_id,)
            )

    @classmethod
    def _retain_foreshadow_prefix(cls, value: Any, start: int) -> Any:
        if isinstance(value, list):
            retained = []
            for item in value:
                item = cls._retain_foreshadow_prefix(item, start)
                if item is not None:
                    retained.append(item)
            return retained
        if not isinstance(value, dict):
            return value

        planted = value.get("planted_in_chapter", value.get("planted_chapter"))
        is_foreshadow = "description" in value and (
            "planted_in_chapter" in value or "planted_chapter" in value
        )
        subtext_chapter = value.get("chapter")
        is_subtext = "question" in value and subtext_chapter is not None
        if is_foreshadow and isinstance(planted, int) and planted >= start:
            return None
        if is_subtext and isinstance(subtext_chapter, int) and subtext_chapter >= start:
            return None

        retained = {
            key: cls._retain_foreshadow_prefix(item, start)
            for key, item in value.items()
        }
        for key in ("resolved_in_chapter", "resolved_chapter"):
            resolved = retained.get(key)
            if isinstance(resolved, int) and resolved >= start:
                retained[key] = None
                if "status" in retained:
                    retained["status"] = "planted"
        return retained

    def _archive_foreshadow_changes(
        self, conn: sqlite3.Connection, archive_id: str, novel_id: str, start: int) -> None:
        cols = self._columns(conn, "foreshadows")
        if not {"novel_id", "planted_chapter", "resolved_chapter"} <= cols:
            return
        deleted = self._select_rows(
            conn, "foreshadows", "novel_id = ? AND planted_chapter >= ?", (novel_id, start)
        )
        self._archive_rows(conn, archive_id, "foreshadows", deleted, chapter_column="planted_chapter")
        self._delete_query(conn, "foreshadows", "novel_id = ? AND planted_chapter >= ?", (novel_id, start))
        reopened = self._select_rows(
            conn,
            "foreshadows",
            "novel_id = ? AND planted_chapter < ? AND resolved_chapter >= ?",
            (novel_id, start, start),
        )
        self._archive_rows(conn, archive_id, "foreshadows_reopened", reopened, chapter_column="resolved_chapter")
        if reopened:
            conn.execute(
                """
                UPDATE foreshadows
                SET resolved_chapter = NULL, status = 'planted', updated_at = ?
                WHERE novel_id = ? AND planted_chapter < ? AND resolved_chapter >= ?
                """,
                (self._now(), novel_id, start, start),
            )

    def _archive_checkpoints_and_snapshots(
        self,
        conn: sqlite3.Connection,
        archive_id: str,
        novel_id: str,
        start: int,
        chapter_ids: list[str],
    ) -> None:
        for table in ("novel_checkpoints", "checkpoints"):
            cols = self._columns(conn, table)
            if not {"novel_id", "anchor_chapter"} <= cols:
                continue
            rows = self._select_rows(
                conn, table, "novel_id = ? AND (anchor_chapter IS NULL OR anchor_chapter >= ?)", (novel_id, start)
            )
            self._archive_rows(conn, archive_id, table, rows, chapter_column="anchor_chapter")
            self._delete_query(conn, table, "novel_id = ? AND (anchor_chapter IS NULL OR anchor_chapter >= ?)", (novel_id, start))
        # Legacy snapshots carry opaque chapter pointers; archive all active read models and rebuild.
        self._archive_and_delete_all_novel_rows(conn, archive_id, "novel_snapshots", novel_id)

    def _pause_legacy_autopilot(self, conn: sqlite3.Connection, novel_id: str) -> None:
        columns = self._columns(conn, "novels")
        assignments: list[str] = []
        if "autopilot_status" in columns:
            assignments.append("autopilot_status = 'stopped'")
        if "current_stage" in columns:
            assignments.append("current_stage = 'paused_for_review'")
        if "active_pipeline_step" in columns:
            assignments.append("active_pipeline_step = ''")
        if "active_pipeline_run_id" in columns:
            assignments.append("active_pipeline_run_id = ''")
        if "autopilot_run_epoch" in columns:
            assignments.append("autopilot_run_epoch = autopilot_run_epoch + 1")
        if "updated_at" in columns:
            assignments.append("updated_at = CURRENT_TIMESTAMP")
        if assignments:
            conn.execute(f"UPDATE novels SET {', '.join(assignments)} WHERE id = ?", (novel_id,))
