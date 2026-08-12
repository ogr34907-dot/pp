"""Rebuild canonical derived state for a retained or restored worldline prefix."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol, Union


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
            self.db_path = db.db_path
        else:
            self._db = None
            self.db_path = str(db)
        self.aftermath_pipeline = aftermath_pipeline

    def _connection(self):
        if self._db is not None:
            return self._db.get_connection()
        from infrastructure.persistence.database.connection import get_database

        return get_database(self.db_path).get_connection()

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
        try:
            for chapter in rows:
                self._ensure_epoch(conn, novel_id, epoch)
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
