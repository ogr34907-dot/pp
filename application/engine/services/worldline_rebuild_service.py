"""Rebuild canonical derived state for a retained or restored worldline prefix."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
from typing import Any, Protocol, Union

from application.world.services.chapter_narrative_sync import CHAPTER_NARRATIVE_PIPELINE_VERSION
from infrastructure.persistence.database.canonical_aftermath_barrier import (
    exact_candidate_aftermath_is_ready,
)
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
                SELECT c.number, c.content, c.content_sha256, c.content_revision, c.outline,
                       commit_record.candidate_id,
                       commit_record.content_sha256 AS authority_content_sha256,
                       commit_record.content_revision AS authority_content_revision
                FROM chapter_candidate_formal_commits AS commit_record
                JOIN chapter_candidates AS candidate ON candidate.id = commit_record.candidate_id
                JOIN chapters AS c ON c.id = commit_record.chapter_id
                WHERE commit_record.novel_id = ?
                  AND (
                    (commit_record.sync_status = 'ready' AND candidate.status = 'committed')
                    OR (
                      commit_record.provenance = 'author_rewrite'
                      AND commit_record.sync_status = 'syncing'
                      AND candidate.status = 'syncing'
                    )
                  )
                  AND c.status = 'completed'
                  AND TRIM(COALESCE(c.content, '')) <> ''
                ORDER BY c.number
                """,
                (novel_id,),
            ).fetchall()
            for row in rows:
                actual_sha256 = hashlib.sha256(
                    str(row["content"] or "").encode("utf-8")
                ).hexdigest()
                if (
                    str(row["content_sha256"] or "") != actual_sha256
                    or str(row["authority_content_sha256"] or "") != actual_sha256
                    or int(row["content_revision"] or 0)
                    != int(row["authority_content_revision"] or 0)
                ):
                    raise WorldlineRebuildError("formal chapter authority mismatch")

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
    ) -> None:
        """Require the real current-version barriers and archived state recovery."""
        for chapter in rows:
            ready, _reason = exact_candidate_aftermath_is_ready(
                conn,
                novel_id=novel_id,
                chapter_number=int(chapter["number"]),
                content_sha256=str(chapter["content_sha256"] or ""),
                content_revision=int(chapter["content_revision"] or 0),
            )
            if not ready:
                raise WorldlineRebuildError(
                    f"rebuild_output_not_ready:chapter={int(chapter['number'])}"
                )

        job = conn.execute(
            "SELECT archive_id FROM worldline_rebuild_jobs "
            "WHERE novel_id = ? AND generation_epoch = ? LIMIT 1",
            (novel_id, epoch),
        ).fetchone()
        if job is None:
            raise WorldlineRebuildError("rebuild_archive_missing")
        if not job["archive_id"]:
            author_rewrite = conn.execute(
                """
                SELECT 1
                FROM chapter_candidate_formal_commits AS formal_commit
                JOIN chapter_candidates AS candidate ON candidate.id = formal_commit.candidate_id
                JOIN chapters AS chapter ON chapter.id = formal_commit.chapter_id
                WHERE formal_commit.novel_id = ?
                  AND formal_commit.provenance = 'author_rewrite'
                  AND formal_commit.sync_status = 'syncing'
                  AND candidate.status = 'syncing'
                  AND formal_commit.content_sha256 = chapter.content_sha256
                  AND formal_commit.content_revision = chapter.content_revision
                LIMIT 1
                """,
                (novel_id,),
            ).fetchone()
            if author_rewrite is None:
                raise WorldlineRebuildError("rebuild_archive_missing")
            return
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

    @staticmethod
    def _run_matches(
        row,
        *,
        generation_epoch: int,
        state: str,
        canonical_sync_status: str,
        next_action: str,
        last_error: str,
    ) -> bool:
        return (
            row is not None
            and int(row["generation_epoch"] or 0) == int(generation_epoch)
            and str(row["state"] or "") == state
            and str(row["canonical_sync_status"] or "ready") == canonical_sync_status
            and str(row["next_action"] or "") == next_action
            and str(row["last_error"] or "") == last_error
        )

    def _cancelled_result(self, conn, novel_id: str, fallback_epoch: int) -> dict[str, Any]:
        try:
            generation_epoch = self._current_epoch(conn, novel_id)
        except WorldlineRebuildError:
            generation_epoch = int(fallback_epoch)
        return {
            "status": "cancelled",
            "generation_epoch": generation_epoch,
            "rebuilt_chapters": 0,
        }

    @staticmethod
    def _transition_all_jobs(
        conn,
        *,
        novel_id: str,
        generation_epoch: int,
        expected_statuses: tuple[str, ...],
        status: str,
        failure_reason: str,
        now: str,
    ) -> None:
        placeholders = ", ".join("?" for _ in expected_statuses)
        total = conn.execute(
            "SELECT COUNT(*) AS total FROM worldline_rebuild_jobs "
            "WHERE novel_id = ? AND generation_epoch = ?",
            (novel_id, int(generation_epoch)),
        ).fetchone()
        expected_count = int(total["total"] or 0) if total is not None else 0
        if expected_count < 1:
            raise WorldlineRebuildCancelled(int(generation_epoch))
        cursor = conn.execute(
            """
            UPDATE worldline_rebuild_jobs
            SET status = ?, failure_reason = ?, updated_at = ?
            WHERE novel_id = ? AND generation_epoch = ?
              AND status IN ({})
            """.format(placeholders),
            (
                status,
                failure_reason,
                now,
                novel_id,
                int(generation_epoch),
                *expected_statuses,
            ),
        )
        if cursor.rowcount != expected_count:
            raise WorldlineRebuildCancelled(int(generation_epoch))

    def _claim_rebuild(
        self,
        conn,
        *,
        novel_id: str,
        generation_epoch: int,
        expected_state: str,
        expected_sync_status: str,
        expected_next_action: str,
        expected_last_error: str,
        now: str,
    ) -> None:
        conn.execute("BEGIN IMMEDIATE")
        try:
            locked = conn.execute(
                "SELECT * FROM novel_generation_runs WHERE novel_id = ?",
                (novel_id,),
            ).fetchone()
            if not self._run_matches(
                locked,
                generation_epoch=generation_epoch,
                state=expected_state,
                canonical_sync_status=expected_sync_status,
                next_action=expected_next_action,
                last_error=expected_last_error,
            ):
                raise WorldlineRebuildCancelled(self._current_epoch(conn, novel_id))
            self._transition_all_jobs(
                conn,
                novel_id=novel_id,
                generation_epoch=generation_epoch,
                expected_statuses=("pending", "failed"),
                status="running",
                failure_reason="",
                now=now,
            )
            cursor = conn.execute(
                """
                UPDATE novel_generation_runs
                SET canonical_sync_status = 'rebuilding', next_action = 'rebuild_worldline',
                    last_error = '', updated_at = ?
                WHERE novel_id = ? AND generation_epoch = ? AND state = ?
                  AND canonical_sync_status = ? AND next_action = ? AND last_error = ?
                """,
                (
                    now,
                    novel_id,
                    int(generation_epoch),
                    expected_state,
                    expected_sync_status,
                    expected_next_action,
                    expected_last_error,
                ),
            )
            if cursor.rowcount != 1:
                raise WorldlineRebuildCancelled(self._current_epoch(conn, novel_id))
            conn.commit()
        except BaseException:
            conn.rollback()
            raise

    def _fail_rebuild(
        self,
        conn,
        *,
        novel_id: str,
        generation_epoch: int,
        expected_state: str,
        failure_reason: str,
        now: str,
    ) -> bool:
        """Publish a retry state only while this worker still owns the epoch."""

        conn.execute("BEGIN IMMEDIATE")
        try:
            locked = conn.execute(
                "SELECT * FROM novel_generation_runs WHERE novel_id = ?",
                (novel_id,),
            ).fetchone()
            if not self._run_matches(
                locked,
                generation_epoch=generation_epoch,
                state=expected_state,
                canonical_sync_status="rebuilding",
                next_action="rebuild_worldline",
                last_error="",
            ):
                conn.rollback()
                return False
            self._transition_all_jobs(
                conn,
                novel_id=novel_id,
                generation_epoch=generation_epoch,
                expected_statuses=("running",),
                status="failed",
                failure_reason=failure_reason,
                now=now,
            )
            cursor = conn.execute(
                """
                UPDATE novel_generation_runs
                SET state = 'paused', canonical_sync_status = 'failed',
                    next_action = 'retry_worldline_rebuild', last_error = ?, updated_at = ?
                WHERE novel_id = ? AND generation_epoch = ? AND state = ?
                  AND canonical_sync_status = 'rebuilding'
                  AND next_action = 'rebuild_worldline' AND last_error = ''
                """,
                (failure_reason, now, novel_id, int(generation_epoch), expected_state),
            )
            if cursor.rowcount != 1:
                raise WorldlineRebuildCancelled(self._current_epoch(conn, novel_id))
            conn.commit()
            return True
        except WorldlineRebuildCancelled:
            conn.rollback()
            return False
        except BaseException:
            conn.rollback()
            raise

    def _complete_rebuild(
        self,
        conn,
        *,
        novel_id: str,
        generation_epoch: int,
        expected_state: str,
        rows,
        now: str,
    ) -> bool:
        """Make a rebuilt epoch visible only with current durable aftermath proof."""

        conn.execute("BEGIN IMMEDIATE")
        try:
            locked = conn.execute(
                "SELECT * FROM novel_generation_runs WHERE novel_id = ?",
                (novel_id,),
            ).fetchone()
            if not self._run_matches(
                locked,
                generation_epoch=generation_epoch,
                state=expected_state,
                canonical_sync_status="rebuilding",
                next_action="rebuild_worldline",
                last_error="",
            ):
                conn.rollback()
                return False
            self._validate_rebuild_outputs(conn, novel_id, generation_epoch, rows)
            for chapter in rows:
                if "candidate_id" not in chapter.keys():
                    continue
                candidate_id = str(chapter["candidate_id"] or "")
                if not candidate_id:
                    continue
                conn.execute(
                    """
                    UPDATE chapter_candidate_formal_commits
                    SET sync_status = 'ready', failure_reason = '', synced_at = ?
                    WHERE candidate_id = ? AND provenance = 'author_rewrite'
                      AND sync_status = 'syncing'
                    """,
                    (now, candidate_id),
                )
                conn.execute(
                    """
                    UPDATE chapter_candidates
                    SET status = 'committed', failure_reason = '', updated_at = ?
                    WHERE id = ? AND status = 'syncing'
                    """,
                    (now, candidate_id),
                )
            self._transition_all_jobs(
                conn,
                novel_id=novel_id,
                generation_epoch=generation_epoch,
                expected_statuses=("running",),
                status="completed",
                failure_reason="",
                now=now,
            )
            cursor = conn.execute(
                """
                UPDATE novel_generation_runs
                SET state = 'paused', canonical_sync_status = 'ready',
                    next_action = 'select_run_mode', last_error = '', updated_at = ?
                WHERE novel_id = ? AND generation_epoch = ? AND state = ?
                  AND canonical_sync_status = 'rebuilding'
                  AND next_action = 'rebuild_worldline' AND last_error = ''
                """,
                (now, novel_id, int(generation_epoch), expected_state),
            )
            if cursor.rowcount != 1:
                raise WorldlineRebuildCancelled(self._current_epoch(conn, novel_id))
            conn.commit()
            return True
        except WorldlineRebuildCancelled:
            conn.rollback()
            return False
        except BaseException:
            conn.rollback()
            raise

    async def rebuild(self, novel_id: str) -> dict[str, Any]:
        conn = self._connection()
        run = conn.execute(
            "SELECT * FROM novel_generation_runs WHERE novel_id = ?", (novel_id,)
        ).fetchone()
        if run is None:
            raise WorldlineRebuildError("generation run not found")
        epoch = int(run["generation_epoch"] or 0)
        expected_state = str(run["state"] or "paused")
        sync_status = str(run["canonical_sync_status"] or "ready")
        expected_next_action = str(run["next_action"] or "")
        expected_last_error = str(run["last_error"] or "")
        if sync_status == "ready":
            return {"status": "already_ready", "generation_epoch": epoch, "rebuilt_chapters": 0}
        now = self._now()
        try:
            self._claim_rebuild(
                conn,
                novel_id=novel_id,
                generation_epoch=epoch,
                expected_state=expected_state,
                expected_sync_status=sync_status,
                expected_next_action=expected_next_action,
                expected_last_error=expected_last_error,
                now=now,
            )
        except WorldlineRebuildCancelled:
            return self._cancelled_result(conn, novel_id, epoch)
        try:
            rows = self._retained_formal_chapters(conn, novel_id)
            commit_repo = SqliteChapterNarrativeCommitRepository(self._database)
            memory_engine = getattr(self.aftermath_pipeline, "_memory_engine", None)
            if memory_engine is not None and hasattr(memory_engine, "invalidate_cached_state"):
                memory_engine.invalidate_cached_state(novel_id)
            replay_pipeline_version = CHAPTER_NARRATIVE_PIPELINE_VERSION
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
                ready, _reason = exact_candidate_aftermath_is_ready(
                    conn,
                    novel_id=novel_id,
                    chapter_number=int(chapter["number"]),
                    content_sha256=str(chapter["content_sha256"] or ""),
                    content_revision=int(chapter["content_revision"] or 0),
                )
                if not ready:
                    raise WorldlineRebuildError(
                        f"rebuild_output_not_ready:chapter={int(chapter['number'])}"
                    )
            self._validate_rebuild_outputs(
                conn,
                novel_id,
                epoch,
                rows,
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
            if not self._fail_rebuild(
                conn,
                novel_id=novel_id,
                generation_epoch=epoch,
                expected_state=expected_state,
                failure_reason=failure,
                now=now,
            ):
                return self._cancelled_result(conn, novel_id, epoch)
            raise WorldlineRebuildError(failure) from exc
        now = self._now()
        try:
            completed = self._complete_rebuild(
                conn,
                novel_id=novel_id,
                generation_epoch=epoch,
                expected_state=expected_state,
                rows=rows,
                now=now,
            )
        except Exception as exc:
            failure = str(exc)
            if not self._fail_rebuild(
                conn,
                novel_id=novel_id,
                generation_epoch=epoch,
                expected_state=expected_state,
                failure_reason=failure,
                now=self._now(),
            ):
                return self._cancelled_result(conn, novel_id, epoch)
            raise WorldlineRebuildError(failure) from exc
        if not completed:
            return self._cancelled_result(conn, novel_id, epoch)
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
