"""Rebuild canonical derived state for a retained or restored worldline prefix."""

from __future__ import annotations

from datetime import datetime
import json
from typing import Any, Protocol, Union

from application.world.services.chapter_narrative_sync import CHAPTER_NARRATIVE_PIPELINE_VERSION
from infrastructure.persistence.database.sqlite_chapter_narrative_commit_repository import (
    SqliteChapterNarrativeCommitRepository,
)


class WorldlineAftermathPipeline(Protocol):
    async def run_after_chapter_saved(self, *args: Any, **kwargs: Any) -> dict[str, Any]: ...


class WorldlineRebuildError(RuntimeError):
    pass


class WorldlineRebuildCancelled(WorldlineRebuildError):
    """The rebuild task lost ownership of its generation epoch."""

    def __init__(self, generation_epoch: int):
        super().__init__("worldline rebuild was cancelled or superseded")
        self.generation_epoch = generation_epoch


class WorldlineRebuildService:
    """Replay only formal retained chapters through the canonical aftermath path."""

    def __init__(self, db: Union[str, "DatabaseConnection"], aftermath_pipeline: WorldlineAftermathPipeline):
        from infrastructure.persistence.database.connection import DatabaseConnection

        if isinstance(db, DatabaseConnection):
            self._db = db
            self._database = db
            self.db_path = db.db_path
        else:
            from infrastructure.persistence.database.connection import get_database

            self.db_path = str(db)
            self._db = None
            self._database = get_database(self.db_path)
        self.aftermath_pipeline = aftermath_pipeline

    def _connection(self):
        if self._db is not None:
            return self._db.get_connection()
        return self._database.get_connection()

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat()

    @staticmethod
    def _current_epoch(conn, novel_id: str) -> int:
        row = conn.execute(
            "SELECT generation_epoch FROM novel_generation_runs WHERE novel_id = ?",
            (novel_id,),
        ).fetchone()
        if row is None:
            raise WorldlineRebuildError("generation run not found")
        return int(row["generation_epoch"] or 0)

    @classmethod
    def _ensure_epoch(cls, conn, novel_id: str, expected_epoch: int) -> None:
        current_epoch = cls._current_epoch(conn, novel_id)
        if current_epoch != expected_epoch:
            raise WorldlineRebuildCancelled(current_epoch)

    @staticmethod
    def _retained_formal_chapters(conn, novel_id: str):
        """Return the contiguous formal prefix that is safe to replay."""

        has_candidate_commits = conn.execute(
            "SELECT 1 FROM chapter_candidate_formal_commits WHERE novel_id = ? LIMIT 1",
            (novel_id,),
        ).fetchone()
        if has_candidate_commits is None:
            # Existing worldline archives predate candidate-first authority.
            rows = conn.execute(
                """
                SELECT number, content, content_sha256, content_revision, outline
                FROM chapters
                WHERE novel_id = ? AND status = 'completed'
                  AND TRIM(COALESCE(content, '')) <> ''
                ORDER BY number
                """,
                (novel_id,),
            ).fetchall()
        else:
            rows = conn.execute(
                """
                SELECT c.number, c.content, c.content_sha256, c.content_revision, c.outline
                FROM chapter_candidate_formal_commits AS commit_record
                JOIN chapter_candidates AS candidate ON candidate.id = commit_record.candidate_id
                JOIN chapters AS c ON c.id = commit_record.chapter_id
                WHERE commit_record.novel_id = ?
                  AND commit_record.sync_status = 'ready'
                  AND candidate.status = 'committed'
                  AND c.status = 'completed'
                  AND TRIM(COALESCE(c.content, '')) <> ''
                ORDER BY c.number
                """,
                (novel_id,),
            ).fetchall()

        prefix = []
        for row in rows:
            if int(row["number"]) != len(prefix) + 1:
                break
            prefix.append(row)
        return prefix

    def _validate_rebuild_outputs(
        self,
        conn,
        novel_id: str,
        epoch: int,
        rows,
        pipeline_version: str,
        require_memory_sync: bool,
    ) -> None:
        """Require the real current-version barriers and archived state recovery."""
        repo = SqliteChapterNarrativeCommitRepository(self._database)
        for chapter in rows:
            if not repo.is_current_version_ready(
                novel_id=novel_id,
                chapter_number=int(chapter["number"]),
                pipeline_version=pipeline_version,
                require_memory_sync=require_memory_sync,
            ):
                raise WorldlineRebuildError(
                    f"rebuild_output_not_ready:chapter={int(chapter['number'])}"
                )

        job = conn.execute(
            "SELECT archive_id FROM worldline_rebuild_jobs "
            "WHERE novel_id = ? AND generation_epoch = ? "
            "AND archive_id IS NOT NULL LIMIT 1",
            (novel_id, epoch),
        ).fetchone()
        if job is None or not job["archive_id"]:
            raise WorldlineRebuildError("rebuild_archive_missing")
        archive_id = str(job["archive_id"])
        retained = max((int(row["number"]) for row in rows), default=0)
        if retained == 0:
            old_state = conn.execute(
                "SELECT 1 FROM memory_engine_state WHERE novel_id = ?",
                (novel_id,),
            ).fetchone()
            old_characters = conn.execute(
                "SELECT 1 FROM character_states WHERE novel_id = ? LIMIT 1",
                (novel_id,),
            ).fetchone()
            if old_state is not None or old_characters is not None:
                raise WorldlineRebuildError("retained_zero_has_active_derived_state")
            return

        def _state_list(value: Any) -> tuple[list[Any], bool]:
            if value is None:
                return [], True
            if isinstance(value, list):
                return value, True
            if isinstance(value, str):
                text = value.strip()
                if not text:
                    return [], True
                try:
                    decoded = json.loads(text)
                except (TypeError, ValueError):
                    return [], False
                return (decoded, True) if isinstance(decoded, list) else ([], False)
            return [], False

        archived = conn.execute(
            "SELECT source_table, payload_json FROM worldline_archive_entries "
            "WHERE archive_id = ? AND source_table IN ('memory_engine_state', 'character_states')",
            (archive_id,),
        ).fetchall()
        for entry in archived:
            try:
                payload = json.loads(entry["payload_json"] or "{}")
            except (TypeError, ValueError) as exc:
                raise WorldlineRebuildError(
                    f"rebuild_archive_payload_invalid:{entry['source_table']}"
                ) from exc
            if entry["source_table"] == "memory_engine_state":
                state = conn.execute(
                    "SELECT last_updated_chapter FROM memory_engine_state WHERE novel_id = ?",
                    (novel_id,),
                ).fetchone()
                if state is None or int(state["last_updated_chapter"] or 0) != retained:
                    raise WorldlineRebuildError("memory_engine_state_not_recovered")
            else:
                character_id = str(payload.get("character_id") or "")
                if not character_id:
                    raise WorldlineRebuildError("character_state_archive_missing_id")
                prefix_evidence = False
                legacy_aggregate_nonempty = bool(str(payload.get("current_state_summary") or "").strip())
                source_evidence_count = 0
                source_evidence_unresolved = False
                for field in ("base_traits", "scars", "motivations", "emotional_arc"):
                    raw_value = payload.get(field)
                    values, parseable = _state_list(raw_value)
                    raw_nonempty = isinstance(raw_value, str) and bool(raw_value.strip()) and raw_value.strip() not in {"[]", "{}"}
                    if not parseable:
                        source_evidence_unresolved = source_evidence_unresolved or raw_nonempty
                    elif field == "base_traits" and values:
                        legacy_aggregate_nonempty = True
                for field in ("scars", "motivations", "emotional_arc"):
                    values, parseable = _state_list(payload.get(field))
                    if not parseable:
                        continue
                    for value in values:
                        if value:
                            if not isinstance(value, dict):
                                source_evidence_unresolved = True
                                continue
                            source_key = "chapter" if field == "emotional_arc" else "source_chapter"
                            source_chapter = value.get(source_key)
                            if source_chapter is None:
                                source_evidence_unresolved = True
                                continue
                            try:
                                source_chapter = int(source_chapter)
                            except (TypeError, ValueError):
                                source_evidence_unresolved = True
                                continue
                            source_evidence_count += 1
                            if source_chapter <= retained:
                                prefix_evidence = True
                            continue
                        # Empty list items do not constitute legacy aggregate content.
                    if not values:
                        continue
                if not prefix_evidence:
                    archived_last_updated = int(payload.get("last_updated_chapter") or 0)
                    if (
                        source_evidence_count > 0
                        and not source_evidence_unresolved
                        and archived_last_updated > retained
                    ):
                        continue
                    if (
                        not source_evidence_unresolved
                        and not legacy_aggregate_nonempty
                        and archived_last_updated > retained
                    ):
                        continue
                    raise WorldlineRebuildError(
                        f"character_state_prefix_evidence_missing:{character_id}"
                    )
                state = conn.execute(
                    "SELECT last_updated_chapter FROM character_states "
                    "WHERE novel_id = ? AND character_id = ?",
                    (novel_id, character_id),
                ).fetchone()
                if state is None or not (1 <= int(state["last_updated_chapter"] or 0) <= retained):
                    raise WorldlineRebuildError(
                        f"character_state_not_recovered:{character_id}"
                    )

    async def rebuild(self, novel_id: str) -> dict[str, Any]:
        conn = self._connection()
        run = conn.execute(
            "SELECT * FROM novel_generation_runs WHERE novel_id = ?", (novel_id,)
        ).fetchone()
        if run is None:
            raise WorldlineRebuildError("generation run not found")
        epoch = int(run["generation_epoch"] or 0)
        sync_status = str(run["canonical_sync_status"] or "ready")
        if sync_status == "ready":
            return {"status": "already_ready", "generation_epoch": epoch, "rebuilt_chapters": 0}
        now = self._now()
        conn.execute(
            """
            UPDATE worldline_rebuild_jobs
            SET status = 'running', failure_reason = '', updated_at = ?
            WHERE novel_id = ? AND generation_epoch = ? AND status IN ('pending', 'failed')
            """,
            (now, novel_id, epoch),
        )
        conn.execute(
            """
            UPDATE novel_generation_runs
            SET canonical_sync_status = 'rebuilding', next_action = 'rebuild_worldline', last_error = ?, updated_at = ?
            WHERE novel_id = ?
            """,
            ("", now, novel_id),
        )
        conn.commit()
        rows = self._retained_formal_chapters(conn, novel_id)
        commit_repo = SqliteChapterNarrativeCommitRepository(self._database)
        require_memory_sync = bool(getattr(self.aftermath_pipeline, "_memory_engine", None))
        memory_engine = getattr(self.aftermath_pipeline, "_memory_engine", None)
        if memory_engine is not None and hasattr(memory_engine, "invalidate_cached_state"):
            memory_engine.invalidate_cached_state(novel_id)
        replay_pipeline_version = CHAPTER_NARRATIVE_PIPELINE_VERSION
        try:
            for chapter in rows:
                self._ensure_epoch(conn, novel_id, epoch)
                prepared = commit_repo.prepare_worldline_replay(
                    novel_id=novel_id,
                    chapter_number=int(chapter["number"]),
                    content_sha256=str(chapter["content_sha256"] or ""),
                    content_revision=int(chapter["content_revision"] or 0),
                    pipeline_version=replay_pipeline_version,
                )
                if prepared != "prepared":
                    raise WorldlineRebuildError(
                        f"worldline_replay_prepare_{prepared}:chapter={int(chapter['number'])}"
                    )
                result = await self.aftermath_pipeline.run_after_chapter_saved(
                    novel_id,
                    int(chapter["number"]),
                    str(chapter["content"] or ""),
                    expected_content_sha256=str(chapter["content_sha256"] or ""),
                    expected_content_revision=int(chapter["content_revision"] or 0),
                    outline=str(chapter["outline"] or ""),
                )
                self._ensure_epoch(conn, novel_id, epoch)
                if not isinstance(result, dict) or not result.get("narrative_sync_ok"):
                    reason = str((result or {}).get("failure_reason") or "canonical_aftermath_not_ready")
                    raise WorldlineRebuildError(reason)
                commit_status = str(result.get("commit_status") or "")
                if commit_status == "reused":
                    raise WorldlineRebuildError("worldline_replay_reused_claim")
                if commit_status != "committed":
                    raise WorldlineRebuildError("worldline_replay_missing_committed_claim")
                result_pipeline_version = str(result.get("pipeline_version") or "")
                if result_pipeline_version != replay_pipeline_version:
                    raise WorldlineRebuildError("worldline_replay_missing_pipeline_version")
                if not commit_repo.is_current_version_ready(
                    novel_id=novel_id,
                    chapter_number=int(chapter["number"]),
                    pipeline_version=result_pipeline_version,
                    require_memory_sync=require_memory_sync,
                ):
                    raise WorldlineRebuildError(
                        f"rebuild_output_not_ready:chapter={int(chapter['number'])}"
                    )
            self._validate_rebuild_outputs(
                conn,
                novel_id,
                epoch,
                rows,
                replay_pipeline_version,
                require_memory_sync,
            )
        except WorldlineRebuildCancelled as exc:
            return {
                "status": "cancelled",
                "generation_epoch": exc.generation_epoch,
                "rebuilt_chapters": 0,
            }
        except Exception as exc:
            failure = str(exc)
            now = self._now()
            conn.execute(
                """
                UPDATE worldline_rebuild_jobs
                SET status = 'failed', failure_reason = ?, updated_at = ?
                WHERE novel_id = ? AND generation_epoch = ?
                """,
                (failure, now, novel_id, epoch),
            )
            conn.execute(
                """
                UPDATE novel_generation_runs
                SET state = 'paused', canonical_sync_status = 'failed',
                    next_action = 'retry_worldline_rebuild', last_error = ?, updated_at = ?
                WHERE novel_id = ? AND generation_epoch = ?
                """,
                (failure, now, novel_id, epoch),
            )
            conn.commit()
            if self._current_epoch(conn, novel_id) != epoch:
                current_epoch = self._current_epoch(conn, novel_id)
                return {
                    "status": "cancelled",
                    "generation_epoch": current_epoch,
                    "rebuilt_chapters": 0,
                }
            raise WorldlineRebuildError(failure) from exc
        now = self._now()
        self._ensure_epoch(conn, novel_id, epoch)
        conn.execute(
            """
            UPDATE worldline_rebuild_jobs
            SET status = 'completed', failure_reason = '', updated_at = ?
            WHERE novel_id = ? AND generation_epoch = ?
            """,
            (now, novel_id, epoch),
        )
        conn.execute(
            """
            UPDATE novel_generation_runs
            SET state = 'paused', canonical_sync_status = 'ready',
                next_action = 'select_run_mode', last_error = '', updated_at = ?
            WHERE novel_id = ? AND generation_epoch = ?
            """,
            (now, novel_id, epoch),
        )
        conn.commit()
        return {"status": "completed", "generation_epoch": epoch, "rebuilt_chapters": len(rows)}

    def status(self, novel_id: str) -> dict[str, Any]:
        conn = self._connection()
        run = conn.execute(
            """
            SELECT run_mode, state, generation_epoch, canonical_sync_status,
                   next_action, last_error, current_formal_chapter, target_chapters
            FROM novel_generation_runs WHERE novel_id = ?
            """,
            (novel_id,),
        ).fetchone()
        if run is None:
            raise WorldlineRebuildError("generation run not found")
        jobs = conn.execute(
            """
            SELECT id, job_type, status, failure_reason, created_at, updated_at
            FROM worldline_rebuild_jobs
            WHERE novel_id = ? AND generation_epoch = ?
            ORDER BY job_type
            """,
            (novel_id, int(run["generation_epoch"] or 0)),
        ).fetchall()
        return {"run": dict(run), "jobs": [dict(job) for job in jobs]}

    def cancel(self, novel_id: str) -> dict[str, Any]:
        """Cancel a pending rebuild and retire delayed work by advancing epoch."""

        conn = self._connection()
        run = conn.execute(
            "SELECT generation_epoch FROM novel_generation_runs WHERE novel_id = ?", (novel_id,)
        ).fetchone()
        if run is None:
            raise WorldlineRebuildError("generation run not found")
        old_epoch = int(run["generation_epoch"] or 0)
        new_epoch = old_epoch + 1
        now = self._now()
        conn.execute("BEGIN IMMEDIATE")
        try:
            conn.execute(
                """
                UPDATE worldline_rebuild_jobs
                SET status = 'failed', failure_reason = 'cancelled_by_author', updated_at = ?
                WHERE novel_id = ? AND generation_epoch = ? AND status IN ('pending', 'running')
                """,
                (now, novel_id, old_epoch),
            )
            conn.execute(
                """
                UPDATE novel_generation_runs
                SET generation_epoch = ?, state = 'stopped', canonical_sync_status = 'failed',
                    next_action = 'restart_worldline_rebuild', last_error = 'cancelled_by_author', updated_at = ?
                WHERE novel_id = ?
                """,
                (new_epoch, now, novel_id),
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
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return {"status": "cancelled", "generation_epoch": new_epoch}
