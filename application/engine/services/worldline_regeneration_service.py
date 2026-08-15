"""Archive-and-regenerate service for a novel tail beginning at any chapter."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime
import hashlib
import json
import sqlite3
from typing import Any, Iterable, Optional, Union
from uuid import uuid4

from infrastructure.persistence.database.chapter_candidate_repository import (
    ChapterCandidateRepository,
)
from domain.novel.candidate_chapter import RunMode


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

    def preview(self, novel_id: str, *, start_chapter: int, target_chapters: int) -> WorldlinePreview:
        if start_chapter < 1:
            raise WorldlineRegenerationError("start_chapter must be at least 1")
        if target_chapters < 1:
            raise WorldlineRegenerationError("target_chapters must be at least 1")
        if target_chapters < start_chapter:
            raise WorldlineRegenerationError("target_chapters must be at least start_chapter")
        conn = self._connection()
        exists = conn.execute("SELECT 1 FROM novels WHERE id = ?", (novel_id,)).fetchone()
        if exists is None:
            raise KeyError(f"novel not found: {novel_id}")
        row = conn.execute(
            "SELECT COALESCE(MAX(number), 0) AS max_number FROM chapters WHERE novel_id = ?",
            (novel_id,),
        ).fetchone()
        generated = int(row["max_number"] or 0)
        operation = "continue" if start_chapter > generated else "regenerate"
        retained = generated if operation == "continue" else max(0, start_chapter - 1)
        archive_from = start_chapter if operation == "regenerate" else None
        archive_to = generated if operation == "regenerate" else None
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
        prefix_digest = self._prefix_digest(conn, novel_id, retained)
        epoch = self._generation_epoch(conn, novel_id)
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
                json.dumps(asdict(preview), ensure_ascii=False, sort_keys=True),
            ),
        )
        conn.commit()
        return preview

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
        current_generated = conn.execute(
            "SELECT COALESCE(MAX(number), 0) AS max_number FROM chapters WHERE novel_id = ?",
            (novel_id,),
        ).fetchone()
        if int(current_generated["max_number"] or 0) != int(preview["current_generated_chapters"]):
            raise WorldlineRegenerationError("chapter tail changed; request a new worldline preview")
        if self._generation_epoch(conn, novel_id) != int(preview["generation_epoch"]):
            raise WorldlineRegenerationError("generation epoch changed; request a new worldline preview")
        retained = int(preview["retained_through"])
        if self._prefix_digest(conn, novel_id, retained) != str(preview["prefix_digest"]):
            raise WorldlineRegenerationError("chapter prefix changed; request a new worldline preview")

        if operation == "continue":
            run = ChapterCandidateRepository(self._db or self.db_path).start_run(
                novel_id,
                run_mode=RunMode(run_mode),
                target_chapters=int(preview["target_chapters"]),
            )
            result = WorldlineRegenerationResult(
                operation="continue",
                novel_id=novel_id,
                archive_id=None,
                generation_epoch=run.generation_epoch,
                retained_through=int(preview["retained_through"]),
                next_action="generate_candidate",
            )
            conn.execute(
                "UPDATE worldline_regeneration_previews SET consumed_at = ? WHERE token = ?",
                (self._now(), preview_token),
            )
            if idempotency_key:
                conn.execute(
                    """
                    INSERT INTO worldline_regeneration_operations
                        (novel_id, idempotency_key, operation, archive_id, result_json)
                    VALUES (?, ?, ?, NULL, ?)
                    """,
                    (novel_id, idempotency_key, operation, json.dumps(asdict(result), ensure_ascii=False)),
                )
            conn.commit()
            return result

        start = int(preview["start_chapter"])
        end = int(preview["current_generated_chapters"])
        old_epoch = int(preview["generation_epoch"])
        new_epoch = old_epoch + 1
        archive_id = f"worldline-{uuid4()}"
        now = self._now()
        try:
            conn.execute("BEGIN IMMEDIATE")
            self._ensure_run(conn, novel_id, int(preview["target_chapters"]))
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
            conn.execute(
                """
                UPDATE novel_generation_runs
                SET run_mode = ?, state = 'paused', generation_epoch = ?, target_chapters = ?,
                    current_formal_chapter = ?, current_candidate_id = NULL, current_candidate_chapter = NULL,
                    canonical_sync_status = 'rebuilding', next_action = 'rebuild_worldline', last_error = '',
                    max_pending_candidates = 1, prefetch = 0, updated_at = ?
                WHERE novel_id = ?
                """,
                (run_mode, new_epoch, int(preview["target_chapters"]), retained, now, novel_id),
            )
            conn.execute(
                """
                INSERT INTO worldline_generation_filters (novel_id, active_generation_epoch, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(novel_id) DO UPDATE SET
                    active_generation_epoch = excluded.active_generation_epoch,
                    updated_at = excluded.updated_at
                """,
                (novel_id, new_epoch, now),
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
            conn.execute(
                "UPDATE worldline_archives SET status = 'archived' WHERE id = ?", (archive_id,)
            )
            conn.execute(
                "UPDATE worldline_regeneration_previews SET consumed_at = ? WHERE token = ?", (now, preview_token)
            )
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
        except Exception:
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
        source = conn.execute(
            "SELECT * FROM worldline_archives WHERE id = ? AND novel_id = ?",
            (archive_id, novel_id),
        ).fetchone()
        if source is None:
            raise WorldlineRegenerationError("worldline archive was not found for this novel")
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
        if source_prefix and self._prefix_digest(conn, novel_id, retained) != source_prefix:
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
            self._ensure_run(conn, novel_id, int(source["target_chapters"]))
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
            restored_max_row = conn.execute(
                "SELECT COALESCE(MAX(number), 0) AS max_number FROM chapters WHERE novel_id = ?",
                (novel_id,),
            ).fetchone()
            restored_max = int(restored_max_row["max_number"] or 0)
            conn.execute(
                """
                UPDATE novel_generation_runs
                SET run_mode = ?, state = 'paused', generation_epoch = ?, target_chapters = ?,
                    current_formal_chapter = ?, current_candidate_id = NULL, current_candidate_chapter = NULL,
                    canonical_sync_status = 'rebuilding', next_action = 'rebuild_worldline', last_error = '',
                    max_pending_candidates = 1, prefetch = 0, updated_at = ?
                WHERE novel_id = ?
                """,
                (run_mode, new_epoch, int(source["target_chapters"]), restored_max, now, novel_id),
            )
            conn.execute(
                """
                INSERT INTO worldline_generation_filters (novel_id, active_generation_epoch, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT(novel_id) DO UPDATE SET
                    active_generation_epoch = excluded.active_generation_epoch,
                    updated_at = excluded.updated_at
                """,
                (novel_id, new_epoch, now),
            )
            self._queue_rebuild_jobs(conn, novel_id, new_epoch, replacement_archive_id, now)
            self._pause_legacy_autopilot(conn, novel_id)
            conn.execute(
                "UPDATE worldline_archives SET status = 'archived' WHERE id = ?",
                (replacement_archive_id,),
            )
            conn.execute(
                "UPDATE worldline_archives SET status = 'restored', restored_at = ? WHERE id = ?",
                (now, archive_id),
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
                    (novel_id, idempotency_key, replacement_archive_id, json.dumps(asdict(result), ensure_ascii=False)),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return result

    def _restore_source_rows(self, conn: sqlite3.Connection, archive_id: str) -> None:
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

    def _archive_tail(self, conn: sqlite3.Connection, archive_id: str, novel_id: str, start: int) -> None:
        """Archive known chapter-provenance rows first, then remove the active tail."""

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

        # Chapter outline nodes and their plan contracts leave the active tree.
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
        self._archive_rows_by_ids(conn, archive_id, "outline_contract_versions", "contract_id", contract_ids)
        self._archive_rows_by_ids(conn, archive_id, "outline_plan_projections", "contract_id", contract_ids)
        self._delete_rows_by_ids(conn, "outline_contracts", "id", contract_ids)

        self._archive_rows(conn, archive_id, "story_nodes", tail_node_rows, chapter_column="number")
        self._delete_query(
            conn, "story_nodes", "novel_id = ? AND node_type = 'chapter' AND number >= ?", (novel_id, start)
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
