"""Coordinate safe chapter rewrites through the existing persistence stack."""
from __future__ import annotations

import hashlib
import json
import logging
from dataclasses import dataclass
from typing import Any, Iterable, Optional

from application.core.async_bridge import run_coroutine_sync
from application.manuscript.reindex_job import reindex_chapter_entity_mentions
from domain.novel.value_objects.novel_id import NovelId
from infrastructure.persistence.database.write_dispatch import sqlite_writes_bypass_queue

logger = logging.getLogger(__name__)

SAFE_SNAPSHOT = "safe_snapshot"
RETAIN_PROSE = "retain_prose"
_REWRITE_MODES = {SAFE_SNAPSHOT, RETAIN_PROSE}


class ChapterRewriteConflictError(RuntimeError):
    """The chapter changed again while a rewrite was being coordinated."""


class ChapterReplayError(RuntimeError):
    """Retained prose could not be rebuilt into canonical memory."""


@dataclass(frozen=True)
class ChapterRewriteResult:
    chapter: Any
    rewrite_mode: str
    requires_rebuild: bool
    replay_completed: bool
    checkpoint_id: str = ""


def _value(value: Any) -> str:
    return str(getattr(value, "value", value))


class ChapterRewriteCoordinator:
    """Single safe-overwrite path for chapters which already contain prose.

    The coordinator deliberately creates the recoverable pre-rewrite assets
    before changing prose. The content write, invalidation set, and mainline
    pause then share one SQLite transaction so a new body is never visible
    together with active derived state from its predecessor.
    """

    def __init__(
        self,
        *,
        db: Any,
        chapter_repository: Any,
        chapter_draft_repository: Any = None,
        checkpoint_service: Any = None,
        vector_store: Any = None,
        aftermath_pipeline: Any = None,
    ) -> None:
        self._db = db
        self._chapters = chapter_repository
        self._drafts = chapter_draft_repository
        self._checkpoints = checkpoint_service
        self._vectors = vector_store
        self._aftermath = aftermath_pipeline

    @classmethod
    def for_chapter_repository(
        cls,
        chapter_repository: Any,
        *,
        vector_store: Any = None,
        aftermath_pipeline: Any = None,
    ) -> "ChapterRewriteCoordinator | None":
        """Build the existing-service composition when a runtime has no DI hook."""
        db = getattr(chapter_repository, "db", None)
        if db is None:
            return None
        from application.checkpoint.services.unified_checkpoint_service import (
            UnifiedCheckpointService,
        )
        from infrastructure.persistence.database.chapter_draft_repository import (
            ChapterDraftRepository,
        )

        return cls(
            db=db,
            chapter_repository=chapter_repository,
            chapter_draft_repository=ChapterDraftRepository(db),
            checkpoint_service=UnifiedCheckpointService(db, chapter_repository),
            vector_store=vector_store,
            aftermath_pipeline=aftermath_pipeline,
        )

    def rewrite(
        self,
        chapter: Any,
        content: str,
        *,
        rewrite_mode: str = SAFE_SNAPSHOT,
    ) -> ChapterRewriteResult:
        rewrite_mode = str(rewrite_mode or SAFE_SNAPSHOT).strip().lower()
        if rewrite_mode not in _REWRITE_MODES:
            raise ValueError(f"unsupported rewrite_mode: {rewrite_mode}")

        old_content = str(getattr(chapter, "content", "") or "")
        if old_content == content:
            return ChapterRewriteResult(chapter, rewrite_mode, False, False)

        novel_id = _value(getattr(chapter, "novel_id", ""))
        chapter_number = int(getattr(chapter, "number", 0) or 0)
        if not novel_id or chapter_number < 1:
            raise ValueError("chapter rewrite requires novel_id and chapter_number")

        # Empty planned chapter nodes have no committed prose to invalidate.
        if not old_content.strip():
            updated = self._overwrite_without_invalidation(chapter, content)
            return ChapterRewriteResult(updated, rewrite_mode, False, False)

        head = self._chapter_head(novel_id)
        checkpoint_id = self._save_recoverable_pre_rewrite_state(
            chapter,
            novel_id,
            chapter_number,
            rewrite_mode,
        )
        updated = self._overwrite_and_invalidate(
            chapter,
            novel_id,
            chapter_number,
            content,
            head,
        )
        self._invalidate_vectors(novel_id, chapter_number)

        if rewrite_mode == RETAIN_PROSE:
            self._replay_retained_prose(novel_id, chapter_number, head)
            return ChapterRewriteResult(
                updated,
                rewrite_mode,
                False,
                True,
                checkpoint_id,
            )

        return ChapterRewriteResult(
            updated,
            rewrite_mode,
            True,
            False,
            checkpoint_id,
        )

    def _save_recoverable_pre_rewrite_state(
        self,
        chapter: Any,
        novel_id: str,
        chapter_number: int,
        rewrite_mode: str,
    ) -> str:
        if self._drafts is not None:
            self._drafts.save_draft(
                novel_id=novel_id,
                chapter_id=_value(getattr(chapter, "id", "")),
                chapter_number=chapter_number,
                content=str(getattr(chapter, "content", "") or ""),
                outline=str(getattr(chapter, "outline", "") or ""),
                source="pre_rewrite",
            )

        if self._checkpoints is None:
            return ""
        anchor = max(0, chapter_number - 1)
        return str(
            self._checkpoints.create_checkpoint(
                novel_id=novel_id,
                trigger_type="PRE_RESET",
                name=f"第{chapter_number}章重写前快照",
                description="章节正文覆盖前的可恢复锚点",
                branch_name="main",
                story_state={
                    "chapter": anchor,
                    "rewrite_boundary": chapter_number,
                    "rewrite_mode": rewrite_mode,
                },
                anchor_chapter=anchor,
            )
        )

    def _overwrite_without_invalidation(self, chapter: Any, content: str) -> Any:
        chapter.update_content(content)
        self._chapters.save(chapter)
        return self._load_chapter(
            _value(getattr(chapter, "novel_id", "")),
            int(getattr(chapter, "number", 0) or 0),
            chapter,
        )

    def _overwrite_and_invalidate(
        self,
        chapter: Any,
        novel_id: str,
        chapter_number: int,
        content: str,
        head: int,
    ) -> Any:
        chapter_id = _value(getattr(chapter, "id", ""))
        expected_hash = hashlib.sha256(
            str(getattr(chapter, "content", "") or "").encode("utf-8")
        ).hexdigest()
        expected_revision = max(1, int(getattr(chapter, "content_revision", 0) or 0))
        new_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()

        with sqlite_writes_bypass_queue():
            with self._db.transaction() as conn:
                source = conn.execute(
                    "SELECT content, content_sha256, content_revision FROM chapters WHERE id = ?",
                    (chapter_id,),
                ).fetchone()
                if source is None:
                    raise ChapterRewriteConflictError("chapter no longer exists")
                actual_hash = hashlib.sha256((source[0] or "").encode("utf-8")).hexdigest()
                actual_revision = max(1, int(source[2] or 0))
                if actual_hash != expected_hash or actual_revision != expected_revision:
                    raise ChapterRewriteConflictError("chapter changed before rewrite commit")

                cursor = conn.execute(
                    """
                    UPDATE chapters
                    SET content = ?, content_sha256 = ?, content_revision = ?,
                        updated_at = CURRENT_TIMESTAMP
                    WHERE id = ? AND content_sha256 = ? AND content_revision = ?
                    """,
                    (
                        content,
                        new_hash,
                        actual_revision + 1,
                        chapter_id,
                        expected_hash,
                        expected_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    raise ChapterRewriteConflictError("chapter changed during rewrite commit")

                changed_ids = self._chapter_ids_from(conn, novel_id, chapter_number)
                self._invalidate_downstream(conn, novel_id, chapter_number, changed_ids)
                self._pause_mainline(conn, novel_id)

        return self._load_chapter(novel_id, chapter_number, chapter)

    def _chapter_head(self, novel_id: str) -> int:
        row = self._db.fetch_one(
            "SELECT MAX(number) AS head FROM chapters "
            "WHERE novel_id = ? AND TRIM(COALESCE(content, '')) <> ''",
            (novel_id,),
        )
        return int(row["head"] or 0) if row else 0

    @staticmethod
    def _chapter_ids_from(conn: Any, novel_id: str, chapter_number: int) -> set[str]:
        rows = conn.execute(
            "SELECT id FROM chapters WHERE novel_id = ? AND number >= ?",
            (novel_id, chapter_number),
        ).fetchall()
        return {str(row[0]) for row in rows}

    def _load_chapter(self, novel_id: str, chapter_number: int, fallback: Any) -> Any:
        try:
            loaded = self._chapters.get_by_novel_and_number(
                NovelId(novel_id), chapter_number
            )
            if loaded is not None:
                return loaded
        except Exception:
            logger.exception("failed to reload rewritten chapter")
        return fallback

    @staticmethod
    def _table_columns(conn: Any, table: str) -> set[str]:
        row = conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = ?", (table,)
        ).fetchone()
        if row is None:
            return set()
        return {str(item[1]) for item in conn.execute(f"PRAGMA table_info({table})")}

    def _invalidate_downstream(
        self,
        conn: Any,
        novel_id: str,
        chapter_number: int,
        changed_chapter_ids: set[str],
    ) -> None:
        # Canonical records are kept for auditability, but are never readable
        # after their originating prose has moved behind this boundary.
        if self._table_columns(conn, "chapter_summaries"):
            conn.execute(
                """
                UPDATE chapter_summaries
                SET sync_status = 'stale', sync_error = 'chapter_rewritten',
                    updated_at = CURRENT_TIMESTAMP
                WHERE knowledge_id IN (SELECT id FROM knowledge WHERE novel_id = ?)
                  AND chapter_number >= ?
                """,
                (novel_id, chapter_number),
            )
        if self._table_columns(conn, "chapter_narrative_commits"):
            conn.execute(
                """
                UPDATE chapter_narrative_commits
                SET status = 'stale', failure_reason = 'chapter_rewritten',
                    updated_at = CURRENT_TIMESTAMP
                WHERE novel_id = ? AND chapter_number >= ? AND status != 'stale'
                """,
                (novel_id, chapter_number),
            )
        if self._table_columns(conn, "chapter_evolution_snapshots"):
            conn.execute(
                """
                UPDATE chapter_evolution_snapshots
                SET status = 'stale', updated_at = CURRENT_TIMESTAMP
                WHERE novel_id = ? AND branch_id = 'main' AND chapter_number >= ?
                """,
                (novel_id, chapter_number),
            )

        self._delete_chapter_rows(
            conn,
            novel_id,
            chapter_number,
            (
                "narrative_events",
                "chapter_entity_mentions",
                "chapter_guardrail_snapshots",
                "chapter_style_scores",
                "voice_vault",
                "chapter_evolution_action_log",
                "chapter_evolution_conflicts",
                "chapter_snapshots",
            ),
        )
        self._invalidate_memory(conn, novel_id, chapter_number)
        self._delete_chapter_rows(
            conn,
            novel_id,
            chapter_number,
            ("triples", "triple_more_chapters"),
            chapter_columns=("chapter_number",),
        )
        self._delete_chapter_rows(
            conn,
            novel_id,
            chapter_number,
            ("causal_edges",),
            chapter_columns=("source_chapter", "target_chapter", "chapter_number"),
        )
        self._invalidate_character_state(conn, novel_id, chapter_number)
        self._invalidate_debts(conn, novel_id, chapter_number)
        self._invalidate_foreshadows(conn, novel_id, chapter_number)
        self._invalidate_storylines(conn, novel_id, chapter_number)
        self._invalidate_story_nodes(conn, novel_id, chapter_number)
        self._invalidate_checkpoints(conn, novel_id, chapter_number)
        self._invalidate_legacy_snapshots(conn, novel_id, changed_chapter_ids)

    def _delete_chapter_rows(
        self,
        conn: Any,
        novel_id: str,
        chapter_number: int,
        tables: Iterable[str],
        *,
        chapter_columns: tuple[str, ...] = ("chapter_number",),
    ) -> None:
        for table in tables:
            columns = self._table_columns(conn, table)
            if "novel_id" not in columns:
                continue
            matching = [column for column in chapter_columns if column in columns]
            if not matching:
                continue
            predicate = " OR ".join(f"COALESCE({column}, 0) >= ?" for column in matching)
            conn.execute(
                f"DELETE FROM {table} WHERE novel_id = ? AND ({predicate})",
                (novel_id, *([chapter_number] * len(matching))),
            )

    def _invalidate_memory(self, conn: Any, novel_id: str, chapter_number: int) -> None:
        atom_columns = self._table_columns(conn, "memory_atoms")
        atom_source = (
            "SELECT id FROM memory_atoms WHERE novel_id = ? "
            "AND COALESCE(chapter_number, 0) >= ?"
        )
        entity_source = (
            "SELECT DISTINCT entity_id FROM memory_atoms WHERE novel_id = ? "
            "AND COALESCE(chapter_number, 0) >= ?"
        )
        if {"id", "novel_id", "entity_id", "chapter_number"} <= atom_columns:
            link_columns = self._table_columns(conn, "memory_atom_links")
            if {"novel_id", "source_atom_id", "target_atom_id"} <= link_columns:
                conn.execute(
                    "DELETE FROM memory_atom_links WHERE novel_id = ? AND ("
                    "source_atom_id IN (" + atom_source + ") OR "
                    "target_atom_id IN (" + atom_source + "))",
                    (novel_id, novel_id, chapter_number, novel_id, chapter_number),
                )
            calibration_columns = self._table_columns(conn, "memory_calibration_actions")
            if {"novel_id", "atom_id"} <= calibration_columns:
                conn.execute(
                    "DELETE FROM memory_calibration_actions WHERE novel_id = ? "
                    "AND atom_id IN (" + atom_source + ")",
                    (novel_id, novel_id, chapter_number),
                )
            projection_columns = self._table_columns(conn, "memory_projections")
            if {"novel_id", "entity_id"} <= projection_columns:
                conn.execute(
                    "DELETE FROM memory_projections WHERE novel_id = ? "
                    "AND entity_id IN (" + entity_source + ")",
                    (novel_id, novel_id, chapter_number),
                )
            conn.execute(
                "DELETE FROM memory_atoms WHERE novel_id = ? "
                "AND COALESCE(chapter_number, 0) >= ?",
                (novel_id, chapter_number),
            )

        state_columns = self._table_columns(conn, "memory_engine_state")
        if "novel_id" in state_columns:
            conn.execute(
                "DELETE FROM memory_engine_state WHERE novel_id = ?", (novel_id,)
            )

    def _invalidate_character_state(self, conn: Any, novel_id: str, chapter_number: int) -> None:
        columns = self._table_columns(conn, "character_states")
        if "novel_id" not in columns:
            return
        source_column = next(
            (key for key in ("last_updated_chapter", "chapter_number") if key in columns),
            None,
        )
        if source_column:
            conn.execute(
                f"DELETE FROM character_states WHERE novel_id = ? AND COALESCE({source_column}, 0) >= ?",
                (novel_id, chapter_number),
            )

    def _invalidate_debts(self, conn: Any, novel_id: str, chapter_number: int) -> None:
        columns = self._table_columns(conn, "narrative_debts")
        if "novel_id" not in columns:
            return
        if "planted_chapter" in columns:
            conn.execute(
                "DELETE FROM narrative_debts WHERE novel_id = ? AND COALESCE(planted_chapter, 0) >= ?",
                (novel_id, chapter_number),
            )
        if "resolved_chapter" in columns:
            assignments = ["resolved_chapter = NULL"]
            if "is_overdue" in columns:
                assignments.append("is_overdue = 0")
            conn.execute(
                f"UPDATE narrative_debts SET {', '.join(assignments)} "
                "WHERE novel_id = ? AND COALESCE(resolved_chapter, 0) >= ?",
                (novel_id, chapter_number),
            )

    def _invalidate_foreshadows(self, conn: Any, novel_id: str, chapter_number: int) -> None:
        columns = self._table_columns(conn, "foreshadows")
        if "novel_id" in columns:
            if "planted_chapter" in columns:
                conn.execute(
                    "DELETE FROM foreshadows WHERE novel_id = ? AND COALESCE(planted_chapter, 0) >= ?",
                    (novel_id, chapter_number),
                )
            if "resolved_chapter" in columns:
                assignments = ["resolved_chapter = NULL"]
                if "status" in columns:
                    assignments.append("status = 'planted'")
                if "updated_at" in columns:
                    assignments.append("updated_at = CURRENT_TIMESTAMP")
                conn.execute(
                    f"UPDATE foreshadows SET {', '.join(assignments)} "
                    "WHERE novel_id = ? AND COALESCE(resolved_chapter, 0) >= ?",
                    (novel_id, chapter_number),
                )

        registry_columns = self._table_columns(conn, "novel_foreshadow_registry")
        if {"novel_id", "payload"} <= registry_columns:
            row = conn.execute(
                "SELECT payload FROM novel_foreshadow_registry WHERE novel_id = ?", (novel_id,)
            ).fetchone()
            if row and row[0]:
                try:
                    payload = json.loads(row[0])
                    cleaned = self._clean_foreshadow_payload(payload, chapter_number)
                    conn.execute(
                        "UPDATE novel_foreshadow_registry SET payload = ?, updated_at = CURRENT_TIMESTAMP WHERE novel_id = ?",
                        (json.dumps(cleaned, ensure_ascii=False), novel_id),
                    )
                except (TypeError, json.JSONDecodeError):
                    # An unreadable legacy registry is not safe continuity input.
                    conn.execute(
                        "DELETE FROM novel_foreshadow_registry WHERE novel_id = ?", (novel_id,)
                    )

    def _clean_foreshadow_payload(self, value: Any, chapter_number: int) -> Any:
        if isinstance(value, list):
            cleaned = []
            for item in value:
                candidate = self._clean_foreshadow_payload(item, chapter_number)
                if candidate is not None:
                    cleaned.append(candidate)
            return cleaned
        if not isinstance(value, dict):
            return value

        planted = value.get("planted_chapter")
        if (
            isinstance(planted, int)
            and planted >= chapter_number
            and ("description" in value or "foreshadow_id" in value or "id" in value)
        ):
            return None

        cleaned = {
            key: self._clean_foreshadow_payload(item, chapter_number)
            for key, item in value.items()
        }
        resolved = cleaned.get("resolved_chapter")
        if isinstance(resolved, int) and resolved >= chapter_number:
            cleaned["resolved_chapter"] = None
            if "status" in cleaned:
                cleaned["status"] = "planted"
        return cleaned

    def _invalidate_storylines(self, conn: Any, novel_id: str, chapter_number: int) -> None:
        columns = self._table_columns(conn, "storylines")
        if "novel_id" not in columns or "last_active_chapter" not in columns:
            return
        assignments = [
            "last_active_chapter = CASE WHEN last_active_chapter >= ? THEN ? ELSE last_active_chapter END"
        ]
        if "progress_summary" in columns:
            assignments.append("progress_summary = ''")
        if "updated_at" in columns:
            assignments.append("updated_at = CURRENT_TIMESTAMP")
        conn.execute(
            f"UPDATE storylines SET {', '.join(assignments)} "
            "WHERE novel_id = ? AND COALESCE(last_active_chapter, 0) >= ?",
            (chapter_number, max(0, chapter_number - 1), novel_id, chapter_number),
        )

    def _invalidate_story_nodes(self, conn: Any, novel_id: str, chapter_number: int) -> None:
        columns = self._table_columns(conn, "story_nodes")
        if not {"id", "novel_id", "node_type", "metadata"} <= columns:
            return
        range_columns = {"chapter_start", "chapter_end"} <= columns
        query = "SELECT id, metadata, chapter_start, chapter_end FROM story_nodes WHERE novel_id = ? AND node_type IN ('act', 'volume')" if range_columns else "SELECT id, metadata FROM story_nodes WHERE novel_id = ? AND node_type IN ('act', 'volume')"
        for row in conn.execute(query, (novel_id,)).fetchall():
            if range_columns:
                chapter_end = row[3]
                if chapter_end is not None and int(chapter_end) < chapter_number:
                    continue
            try:
                metadata = json.loads(row[1] or "{}")
            except (TypeError, json.JSONDecodeError):
                metadata = {}
            metadata["summary_status"] = "stale"
            metadata["summary_stale_from_chapter"] = chapter_number
            for key in (
                "summary_provenance",
                "act_summary_provenance",
                "volume_summary_provenance",
                "checkpoint_summary_provenance",
            ):
                provenance = metadata.get(key)
                if isinstance(provenance, dict):
                    provenance["status"] = "stale"
            assignments = ["metadata = ?"]
            if "updated_at" in columns:
                assignments.append("updated_at = CURRENT_TIMESTAMP")
            conn.execute(
                f"UPDATE story_nodes SET {', '.join(assignments)} WHERE id = ?",
                (json.dumps(metadata, ensure_ascii=False), row[0]),
            )

    def _invalidate_checkpoints(self, conn: Any, novel_id: str, chapter_number: int) -> None:
        columns = self._table_columns(conn, "novel_checkpoints")
        if {"novel_id", "is_active"} <= columns:
            anchor_clause = ""
            params: tuple[Any, ...] = (novel_id,)
            if "anchor_chapter" in columns:
                anchor_clause = " AND (anchor_chapter IS NULL OR anchor_chapter >= ?)"
                params = (novel_id, chapter_number)
            conn.execute(
                "UPDATE novel_checkpoints SET is_active = 0 WHERE novel_id = ?" + anchor_clause,
                params,
            )

    def _invalidate_legacy_snapshots(
        self,
        conn: Any,
        novel_id: str,
        changed_chapter_ids: set[str],
    ) -> None:
        columns = self._table_columns(conn, "novel_snapshots")
        if not {"id", "novel_id", "chapter_pointers"} <= columns or not changed_chapter_ids:
            return
        rows = conn.execute(
            "SELECT id, chapter_pointers FROM novel_snapshots WHERE novel_id = ?", (novel_id,)
        ).fetchall()
        for row in rows:
            try:
                pointers = {str(item) for item in json.loads(row[1] or "[]")}
            except (TypeError, json.JSONDecodeError):
                pointers = set(changed_chapter_ids)
            if pointers & changed_chapter_ids:
                conn.execute("DELETE FROM novel_snapshots WHERE id = ?", (row[0],))

    def _pause_mainline(self, conn: Any, novel_id: str) -> None:
        columns = self._table_columns(conn, "novels")
        if "id" not in columns:
            return
        assignments = []
        if "autopilot_status" in columns:
            assignments.append("autopilot_status = 'stopped'")
        if "current_stage" in columns:
            assignments.append("current_stage = 'paused_for_review'")
        if "active_pipeline_step" in columns:
            assignments.append("active_pipeline_step = ''")
        if "updated_at" in columns:
            assignments.append("updated_at = CURRENT_TIMESTAMP")
        if assignments:
            conn.execute(
                f"UPDATE novels SET {', '.join(assignments)} WHERE id = ?", (novel_id,)
            )

    def _invalidate_vectors(self, novel_id: str, chapter_number: int) -> None:
        if self._vectors is None:
            return
        invalidate = getattr(self._vectors, "invalidate_from_chapter", None)
        if not callable(invalidate):
            invalidate = getattr(self._vectors, "invalidate_chapter_metadata_from", None)
        if not callable(invalidate):
            logger.warning("vector store has no rewrite invalidation hook novel=%s", novel_id)
            return
        try:
            invalidate(novel_id, chapter_number)
        except Exception:
            # Provenance filtering remains the hard read barrier if physical
            # cleanup is temporarily unavailable.
            logger.exception("vector rewrite invalidation failed novel=%s", novel_id)

    def _replay_retained_prose(
        self,
        novel_id: str,
        chapter_number: int,
        head: int,
    ) -> None:
        if self._aftermath is None:
            raise ChapterReplayError("retain_prose_requires_aftermath_pipeline")
        self._set_rebuild_stage(novel_id, "auditing")
        try:
            for number in range(chapter_number, head + 1):
                chapter = self._chapters.get_by_novel_and_number(NovelId(novel_id), number)
                if chapter is None or not str(getattr(chapter, "content", "") or "").strip():
                    raise ChapterReplayError(f"chapter_{number}_content_unavailable")
                outcome = run_coroutine_sync(
                    lambda chapter=chapter: self._aftermath.run_after_chapter_saved(
                        novel_id,
                        number,
                        chapter.content,
                        expected_content_sha256=getattr(chapter, "content_sha256", ""),
                        expected_content_revision=getattr(chapter, "content_revision", 0),
                    )
                )
                if not bool(outcome.get("narrative_sync_ok", False)):
                    raise ChapterReplayError(
                        f"chapter_{number}_canonical_replay_failed: {outcome.get('failure_reason', '')}"
                    )
                reindex_chapter_entity_mentions(
                    novel_id,
                    number,
                    chapter.content,
                    expected_content_sha256=getattr(chapter, "content_sha256", ""),
                    expected_content_revision=getattr(chapter, "content_revision", 0),
                )
        except Exception:
            self._set_rebuild_stage(novel_id, "paused_for_review")
            raise
        self._set_rebuild_stage(novel_id, "paused_for_review")

    def _set_rebuild_stage(self, novel_id: str, stage: str) -> None:
        with sqlite_writes_bypass_queue():
            columns = self._table_columns(self._db.get_connection(), "novels")
            assignments = []
            if "autopilot_status" in columns:
                assignments.append("autopilot_status = 'stopped'")
            if "current_stage" in columns:
                assignments.append("current_stage = ?")
            if "updated_at" in columns:
                assignments.append("updated_at = CURRENT_TIMESTAMP")
            if assignments:
                params: list[Any] = [stage] if "current_stage" in columns else []
                params.append(novel_id)
                self._db.execute(
                    f"UPDATE novels SET {', '.join(assignments)} WHERE id = ?", tuple(params)
                )
                self._db.commit()
