"""Atomic SQLite claims for canonical chapter narrative extraction."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone

from domain.knowledge.chapter_summary import canonical_summary_payload_sha256
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.write_dispatch import sqlite_writes_bypass_queue


@dataclass(frozen=True)
class NarrativeClaim:
    disposition: str
    content_revision: int
    attempt_count: int = 1
    vector_status: str = "not_started"
    failure_reason: str = ""
    memory_status: str = "not_required"


@dataclass(frozen=True)
class StoryPipelineAdvance:
    disposition: str
    chapter_number: int
    content_revision: int
    current_auto_chapters: int
    current_chapter_in_act: int
    current_stage: str
    failure_reason: str = ""


def _payload_sha256_from_summary_row(row) -> str:
    try:
        beat_sections = json.loads(row["beat_sections"]) if row["beat_sections"] else []
        micro_beats = json.loads(row["micro_beats"]) if row["micro_beats"] else []
    except (TypeError, json.JSONDecodeError) as exc:
        raise RuntimeError("canonical_summary_write_missing") from exc

    if not isinstance(beat_sections, list) or not isinstance(micro_beats, list):
        raise RuntimeError("canonical_summary_write_missing")

    return canonical_summary_payload_sha256(
        summary=row["summary"] or "",
        key_events=row["key_events"] or "",
        open_threads=row["open_threads"] or "",
        consistency_note=row["consistency_note"] or "",
        beat_sections=beat_sections,
        micro_beats=micro_beats,
    )


class SqliteChapterNarrativeCommitRepository:
    def __init__(self, db: DatabaseConnection):
        self._db = db

    def claim(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        content_sha256: str,
        pipeline_version: str,
        expected_content_revision: int | None = None,
        require_memory_sync: bool = False,
    ) -> NarrativeClaim:
        now = datetime.now(timezone.utc).isoformat()
        memory_status = "pending" if require_memory_sync else "not_required"
        with sqlite_writes_bypass_queue():
            with self._db.transaction() as conn:
                source = conn.execute(
                    "SELECT content, content_sha256, content_revision FROM chapters "
                    "WHERE novel_id = ? AND number = ?",
                    (novel_id, chapter_number),
                ).fetchone()
                if source is None:
                    return NarrativeClaim("failed", 0, failure_reason="chapter_not_found")

                actual_hash = hashlib.sha256((source[0] or "").encode("utf-8")).hexdigest()
                stored_hash = source[1] or ""
                revision = int(source[2] or 0)
                if stored_hash != actual_hash or revision < 1:
                    revision = max(1, revision + (1 if stored_hash and stored_hash != actual_hash else 0))
                    conn.execute(
                        "UPDATE chapters SET content_sha256 = ?, content_revision = ? "
                        "WHERE novel_id = ? AND number = ?",
                        (actual_hash, revision, novel_id, chapter_number),
                    )

                if actual_hash != content_sha256:
                    return NarrativeClaim(
                        "failed",
                        revision,
                        failure_reason="source_hash_mismatch",
                    )
                if (
                    expected_content_revision is not None
                    and revision != int(expected_content_revision)
                ):
                    return NarrativeClaim(
                        "failed",
                        revision,
                        failure_reason="source_revision_mismatch",
                    )

                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO chapter_narrative_commits (
                        novel_id, chapter_number, content_sha256, pipeline_version,
                        content_revision, status, failure_reason, attempt_count,
                        vector_status, memory_status, memory_failure_reason,
                        created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'in_progress', '', 1, 'not_started', ?, '', ?, ?)
                    """,
                    (
                        novel_id,
                        chapter_number,
                        content_sha256,
                        pipeline_version,
                        revision,
                        memory_status,
                        now,
                        now,
                    ),
                )
                if cursor.rowcount == 1:
                    return NarrativeClaim(
                        "claimed",
                        revision,
                        memory_status=memory_status,
                    )

                row = conn.execute(
                    """
                    SELECT status, content_revision, attempt_count, vector_status,
                           failure_reason, memory_status
                    FROM chapter_narrative_commits
                    WHERE novel_id = ? AND chapter_number = ?
                      AND content_sha256 = ? AND pipeline_version = ?
                    """,
                    (novel_id, chapter_number, content_sha256, pipeline_version),
                ).fetchone()
                if int(row[1]) != revision:
                    reclaim_cursor = conn.execute(
                        """
                        UPDATE chapter_narrative_commits
                        SET content_revision = ?, status = 'in_progress',
                            failure_reason = '', attempt_count = 1,
                            vector_status = 'not_started', advance_status = 'pending',
                            memory_status = ?, memory_failure_reason = '',
                            advance_applied_at = NULL, committed_at = NULL,
                            updated_at = ?
                        WHERE novel_id = ? AND chapter_number = ?
                          AND content_sha256 = ? AND pipeline_version = ?
                          AND content_revision = ?
                        """,
                        (
                            revision,
                            memory_status,
                            now,
                            novel_id,
                            chapter_number,
                            content_sha256,
                            pipeline_version,
                            int(row[1]),
                        ),
                    )
                    if reclaim_cursor.rowcount == 1:
                        return NarrativeClaim(
                            "claimed",
                            revision,
                            memory_status=memory_status,
                        )
                    row = conn.execute(
                        """
                        SELECT status, content_revision, attempt_count, vector_status,
                               failure_reason, memory_status
                        FROM chapter_narrative_commits
                        WHERE novel_id = ? AND chapter_number = ?
                          AND content_sha256 = ? AND pipeline_version = ?
                        """,
                        (novel_id, chapter_number, content_sha256, pipeline_version),
                    ).fetchone()
                if row[0] == "stale":
                    reclaim_cursor = conn.execute(
                        """
                        UPDATE chapter_narrative_commits
                        SET status = 'in_progress', failure_reason = '', attempt_count = 1,
                            vector_status = 'not_started', advance_status = 'pending',
                            memory_status = ?, memory_failure_reason = '',
                            advance_applied_at = NULL, committed_at = NULL, updated_at = ?
                        WHERE novel_id = ? AND chapter_number = ?
                          AND content_sha256 = ? AND pipeline_version = ?
                          AND status = 'stale'
                        """,
                        (
                            memory_status,
                            now,
                            novel_id,
                            chapter_number,
                            content_sha256,
                            pipeline_version,
                        ),
                    )
                    if reclaim_cursor.rowcount == 1:
                        return NarrativeClaim(
                            "claimed",
                            int(row[1]),
                            memory_status=memory_status,
                        )
                    row = conn.execute(
                        """
                        SELECT status, content_revision, attempt_count, vector_status,
                               failure_reason, memory_status
                        FROM chapter_narrative_commits
                        WHERE novel_id = ? AND chapter_number = ?
                          AND content_sha256 = ? AND pipeline_version = ?
                        """,
                        (novel_id, chapter_number, content_sha256, pipeline_version),
                    ).fetchone()
                if row[0] == "failed" and int(row[2]) < 3:
                    next_attempt = int(row[2]) + 1
                    retry_cursor = conn.execute(
                        """
                        UPDATE chapter_narrative_commits
                        SET status = 'in_progress', failure_reason = '',
                            attempt_count = ?, memory_status = ?,
                            memory_failure_reason = '', updated_at = ?
                        WHERE novel_id = ? AND chapter_number = ?
                          AND content_sha256 = ? AND pipeline_version = ?
                          AND status = 'failed' AND attempt_count = ?
                        """,
                        (
                            next_attempt,
                            memory_status,
                            now,
                            novel_id,
                            chapter_number,
                            content_sha256,
                            pipeline_version,
                            int(row[2]),
                        ),
                    )
                    if retry_cursor.rowcount == 1:
                        return NarrativeClaim(
                            "claimed",
                            int(row[1]),
                            next_attempt,
                            row[3] or "not_started",
                            "",
                            memory_status,
                        )
                    row = conn.execute(
                        """
                        SELECT status, content_revision, attempt_count, vector_status,
                               failure_reason, memory_status
                        FROM chapter_narrative_commits
                        WHERE novel_id = ? AND chapter_number = ?
                          AND content_sha256 = ? AND pipeline_version = ?
                        """,
                        (novel_id, chapter_number, content_sha256, pipeline_version),
                    ).fetchone()

                if (
                    require_memory_sync
                    and row[0] == "committed"
                    and (row[5] or "not_required") == "not_required"
                ):
                    promote_cursor = conn.execute(
                        """
                        UPDATE chapter_narrative_commits
                        SET memory_status = 'pending', memory_failure_reason = '',
                            updated_at = ?
                        WHERE novel_id = ? AND chapter_number = ?
                          AND content_sha256 = ? AND pipeline_version = ?
                          AND content_revision = ? AND status = 'committed'
                          AND memory_status = 'not_required'
                        """,
                        (
                            now,
                            novel_id,
                            chapter_number,
                            content_sha256,
                            pipeline_version,
                            revision,
                        ),
                    )
                    if promote_cursor.rowcount == 1:
                        row = conn.execute(
                            """
                            SELECT status, content_revision, attempt_count, vector_status,
                                   failure_reason, memory_status
                            FROM chapter_narrative_commits
                            WHERE novel_id = ? AND chapter_number = ?
                              AND content_sha256 = ? AND pipeline_version = ?
                            """,
                            (novel_id, chapter_number, content_sha256, pipeline_version),
                        ).fetchone()

                disposition = "reused" if row[0] == "committed" else row[0]
                return NarrativeClaim(
                    disposition,
                    int(row[1]),
                    int(row[2]),
                    row[3] or "not_started",
                    row[4] or "",
                    row[5] or "not_required",
                )

    def is_current_claim_in_progress(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        content_sha256: str,
        pipeline_version: str,
        content_revision: int,
    ) -> bool:
        """Return whether a claimed extraction still owns the current prose version.

        This is the write-side counterpart to ``is_current_version_ready``.
        It is intentionally checked after an LLM round-trip and before the
        first derived-memory write, so an older task cannot overwrite the
        newer canonical version while it was waiting on the model.
        """
        source = self._db.fetch_one(
            "SELECT content, content_sha256, content_revision FROM chapters "
            "WHERE novel_id = ? AND number = ?",
            (novel_id, chapter_number),
        )
        if source is None:
            return False

        actual_sha256 = hashlib.sha256(
            (source["content"] or "").encode("utf-8")
        ).hexdigest()
        if (
            actual_sha256 != content_sha256
            or source["content_sha256"] != content_sha256
            or int(source["content_revision"] or 0) != int(content_revision)
        ):
            return False

        claim = self._db.fetch_one(
            """
            SELECT 1
            FROM chapter_narrative_commits
            WHERE novel_id = ? AND chapter_number = ?
              AND content_sha256 = ? AND pipeline_version = ?
              AND content_revision = ? AND status = 'in_progress'
            """,
            (
                novel_id,
                chapter_number,
                content_sha256,
                pipeline_version,
                int(content_revision),
            ),
        )
        return claim is not None

    def commit(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        content_sha256: str,
        pipeline_version: str,
        attempt_count: int,
        content_revision: int,
        canonical_payload_sha256: str | None = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with sqlite_writes_bypass_queue():
            with self._db.transaction() as conn:
                source = conn.execute(
                    "SELECT content, content_sha256, content_revision FROM chapters "
                    "WHERE novel_id = ? AND number = ?",
                    (novel_id, chapter_number),
                ).fetchone()
                actual_hash = hashlib.sha256(
                    (source[0] or "").encode("utf-8")
                ).hexdigest() if source is not None else ""
                if (
                    source is None
                    or actual_hash != content_sha256
                    or (source[1] or "") != content_sha256
                    or int(source[2] or 0) != content_revision
                ):
                    raise RuntimeError("source_hash_mismatch")

                summary_row = conn.execute(
                    """
                    SELECT id, summary, key_events, open_threads, consistency_note,
                           beat_sections, micro_beats, canonical_payload_sha256
                    FROM chapter_summaries
                    WHERE knowledge_id IN (SELECT id FROM knowledge WHERE novel_id = ?)
                      AND chapter_number = ?
                      AND summary IS NOT NULL AND TRIM(summary) != ''
                      AND source_content_sha256 = ?
                      AND pipeline_version = ?
                      AND sync_status = 'in_progress'
                      AND sync_attempts = ?
                    """,
                    (
                        novel_id,
                        chapter_number,
                        content_sha256,
                        pipeline_version,
                        attempt_count,
                    ),
                ).fetchone()
                if summary_row is None:
                    raise RuntimeError("canonical_summary_write_missing")

                actual_payload_sha256 = _payload_sha256_from_summary_row(summary_row)
                expected_payload_sha256 = (
                    canonical_payload_sha256
                    or summary_row["canonical_payload_sha256"]
                    or ""
                )
                if (
                    not expected_payload_sha256
                    or actual_payload_sha256 != expected_payload_sha256
                    or summary_row["canonical_payload_sha256"] != expected_payload_sha256
                ):
                    raise RuntimeError("canonical_summary_write_missing")

                summary_cursor = conn.execute(
                    """
                    UPDATE chapter_summaries
                    SET source_content_sha256 = ?, pipeline_version = ?,
                        sync_status = 'committed', sync_error = '', sync_attempts = ?,
                        updated_at = ?
                    WHERE id = ?
                      AND summary IS ?
                      AND key_events IS ?
                      AND open_threads IS ?
                      AND consistency_note IS ?
                      AND beat_sections IS ?
                      AND micro_beats IS ?
                      AND canonical_payload_sha256 = ?
                      AND source_content_sha256 = ?
                      AND pipeline_version = ?
                      AND sync_status = 'in_progress'
                      AND sync_attempts = ?
                    """,
                    (
                        content_sha256,
                        pipeline_version,
                        attempt_count,
                        now,
                        summary_row["id"],
                        summary_row["summary"],
                        summary_row["key_events"],
                        summary_row["open_threads"],
                        summary_row["consistency_note"],
                        summary_row["beat_sections"],
                        summary_row["micro_beats"],
                        expected_payload_sha256,
                        content_sha256,
                        pipeline_version,
                        attempt_count,
                    ),
                )
                if summary_cursor.rowcount != 1:
                    raise RuntimeError("canonical_summary_write_missing")

                claim_cursor = conn.execute(
                    """
                    UPDATE chapter_narrative_commits
                    SET status = 'committed', failure_reason = '', updated_at = ?,
                        committed_at = ?
                    WHERE novel_id = ? AND chapter_number = ?
                      AND content_sha256 = ? AND pipeline_version = ?
                      AND content_revision = ?
                      AND status = 'in_progress'
                    """,
                    (
                        now,
                        now,
                        novel_id,
                        chapter_number,
                        content_sha256,
                        pipeline_version,
                        content_revision,
                    ),
                )
                if claim_cursor.rowcount != 1:
                    raise RuntimeError("canonical_claim_not_in_progress")

    def prepare_summary(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        content_sha256: str,
        pipeline_version: str,
        attempt_count: int,
        canonical_payload_sha256: str | None = None,
    ) -> None:
        with sqlite_writes_bypass_queue():
            with self._db.transaction() as conn:
                claim = conn.execute(
                    """
                    SELECT content_revision
                    FROM chapter_narrative_commits
                    WHERE novel_id = ? AND chapter_number = ?
                      AND content_sha256 = ? AND pipeline_version = ?
                      AND status = 'in_progress'
                    """,
                    (
                        novel_id,
                        chapter_number,
                        content_sha256,
                        pipeline_version,
                    ),
                ).fetchone()
                source = conn.execute(
                    "SELECT content, content_sha256, content_revision FROM chapters "
                    "WHERE novel_id = ? AND number = ?",
                    (novel_id, chapter_number),
                ).fetchone()
                actual_sha256 = hashlib.sha256(
                    (source[0] or "").encode("utf-8")
                ).hexdigest() if source is not None else ""
                if (
                    claim is None
                    or source is None
                    or actual_sha256 != content_sha256
                    or (source[1] or "") != content_sha256
                    or int(source[2] or 0) != int(claim[0])
                ):
                    raise RuntimeError("source_hash_mismatch")

                summary_row = conn.execute(
                    """
                    SELECT id, summary, key_events, open_threads, consistency_note,
                           beat_sections, micro_beats
                    FROM chapter_summaries
                    WHERE knowledge_id IN (SELECT id FROM knowledge WHERE novel_id = ?)
                      AND chapter_number = ?
                    """,
                    (novel_id, chapter_number),
                ).fetchone()
                if summary_row is None:
                    raise RuntimeError("canonical_summary_write_missing")

                actual_payload_sha256 = _payload_sha256_from_summary_row(summary_row)
                expected_payload_sha256 = canonical_payload_sha256 or actual_payload_sha256
                if actual_payload_sha256 != expected_payload_sha256:
                    raise RuntimeError("canonical_summary_write_missing")

                cursor = conn.execute(
                    """
                    UPDATE chapter_summaries
                    SET source_content_sha256 = ?, pipeline_version = ?,
                        sync_status = 'in_progress', sync_error = '', sync_attempts = ?,
                        canonical_payload_sha256 = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        content_sha256,
                        pipeline_version,
                        attempt_count,
                        expected_payload_sha256,
                        summary_row["id"],
                    ),
                )
        if cursor.rowcount != 1:
            raise RuntimeError("canonical_summary_write_missing")

    def fail(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        content_sha256: str,
        pipeline_version: str,
        expected_content_revision: int | None = None,
        content_revision: int,
        failure_reason: str,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        reason = (failure_reason or "canonical_sync_failed")[:1000]
        with sqlite_writes_bypass_queue():
            with self._db.transaction() as conn:
                conn.execute(
                    """
                    UPDATE chapter_narrative_commits
                    SET status = 'failed', failure_reason = ?, updated_at = ?
                    WHERE novel_id = ? AND chapter_number = ?
                      AND content_sha256 = ? AND pipeline_version = ?
                      AND content_revision = ?
                      AND status = 'in_progress'
                    """,
                    (
                        reason,
                        now,
                        novel_id,
                        chapter_number,
                        content_sha256,
                        pipeline_version,
                        content_revision,
                    ),
                )
                conn.execute(
                    """
                    UPDATE chapter_summaries
                    SET sync_status = 'failed', sync_error = ?, updated_at = ?
                    WHERE knowledge_id IN (SELECT id FROM knowledge WHERE novel_id = ?)
                      AND chapter_number = ?
                      AND source_content_sha256 = ? AND pipeline_version = ?
                      AND EXISTS (
                          SELECT 1
                          FROM chapter_narrative_commits
                          WHERE novel_id = ? AND chapter_number = ?
                            AND content_sha256 = ? AND pipeline_version = ?
                            AND content_revision = ?
                      )
                    """,
                    (
                        reason,
                        now,
                        novel_id,
                        chapter_number,
                        content_sha256,
                        pipeline_version,
                        novel_id,
                        chapter_number,
                        content_sha256,
                        pipeline_version,
                        content_revision,
                    ),
                )

    def advance_story_pipeline_once(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        pipeline_version: str,
        require_memory_sync: bool = False,
    ) -> StoryPipelineAdvance:
        """Advance the durable novel cursor once for the current canonical chapter."""
        now = datetime.now(timezone.utc).isoformat()
        with sqlite_writes_bypass_queue():
            with self._db.transaction() as conn:
                novel = conn.execute(
                    """
                    SELECT current_auto_chapters, current_chapter_in_act, current_stage
                    FROM novels WHERE id = ?
                    """,
                    (novel_id,),
                ).fetchone()
                if novel is None:
                    return StoryPipelineAdvance(
                        "novel_not_found", chapter_number, 0, 0, 0, "",
                        "novel_not_found",
                    )

                current_auto_chapters = int(novel[0] or 0)
                current_chapter_in_act = int(novel[1] or 0)
                current_stage = str(novel[2] or "")
                source = conn.execute(
                    """
                    SELECT content, content_sha256, content_revision
                    FROM chapters WHERE novel_id = ? AND number = ?
                    """,
                    (novel_id, chapter_number),
                ).fetchone()
                if source is None:
                    return StoryPipelineAdvance(
                        "chapter_not_found",
                        chapter_number,
                        0,
                        current_auto_chapters,
                        current_chapter_in_act,
                        current_stage,
                        "chapter_not_found",
                    )

                actual_sha256 = hashlib.sha256(
                    (source[0] or "").encode("utf-8")
                ).hexdigest()
                content_revision = int(source[2] or 0)
                if source[1] != actual_sha256 or content_revision < 1:
                    return StoryPipelineAdvance(
                        "source_version_mismatch",
                        chapter_number,
                        content_revision,
                        current_auto_chapters,
                        current_chapter_in_act,
                        current_stage,
                        "source_version_mismatch",
                    )

                claim = conn.execute(
                    """
                    SELECT advance_status, memory_status
                    FROM chapter_narrative_commits
                    WHERE novel_id = ? AND chapter_number = ?
                      AND content_sha256 = ? AND pipeline_version = ?
                      AND content_revision = ? AND status = 'committed'
                    """,
                    (
                        novel_id,
                        chapter_number,
                        actual_sha256,
                        pipeline_version,
                        content_revision,
                    ),
                ).fetchone()
                if claim is None:
                    return StoryPipelineAdvance(
                        "source_version_mismatch",
                        chapter_number,
                        content_revision,
                        current_auto_chapters,
                        current_chapter_in_act,
                        current_stage,
                        "canonical_commit_not_current",
                    )
                summary = conn.execute(
                    """
                    SELECT 1
                    FROM knowledge
                    JOIN chapter_summaries
                      ON chapter_summaries.knowledge_id = knowledge.id
                    WHERE knowledge.novel_id = ?
                      AND chapter_summaries.chapter_number = ?
                      AND chapter_summaries.source_content_sha256 = ?
                      AND chapter_summaries.pipeline_version = ?
                      AND chapter_summaries.sync_status = 'committed'
                      AND chapter_summaries.summary IS NOT NULL
                      AND TRIM(chapter_summaries.summary) != ''
                    """,
                    (
                        novel_id,
                        chapter_number,
                        actual_sha256,
                        pipeline_version,
                    ),
                ).fetchone()
                if summary is None:
                    return StoryPipelineAdvance(
                        "source_version_mismatch",
                        chapter_number,
                        content_revision,
                        current_auto_chapters,
                        current_chapter_in_act,
                        current_stage,
                        "canonical_summary_not_current",
                    )
                if require_memory_sync and (claim[1] or "not_required") != "committed":
                    return StoryPipelineAdvance(
                        "memory_sync_pending",
                        chapter_number,
                        content_revision,
                        current_auto_chapters,
                        current_chapter_in_act,
                        current_stage,
                        f"memory_status={claim[1] or 'not_required'}",
                    )
                if claim[0] == "applied":
                    return StoryPipelineAdvance(
                        "already_applied",
                        chapter_number,
                        content_revision,
                        current_auto_chapters,
                        current_chapter_in_act,
                        current_stage,
                    )
                if claim[0] != "pending":
                    return StoryPipelineAdvance(
                        "advance_not_pending",
                        chapter_number,
                        content_revision,
                        current_auto_chapters,
                        current_chapter_in_act,
                        current_stage,
                        f"advance_status={claim[0]}",
                    )
                if current_auto_chapters >= chapter_number:
                    cursor = conn.execute(
                        """
                        UPDATE chapter_narrative_commits
                        SET advance_status = 'applied', advance_applied_at = ?,
                            updated_at = ?
                        WHERE novel_id = ? AND chapter_number = ?
                          AND content_sha256 = ? AND pipeline_version = ?
                          AND content_revision = ? AND status = 'committed'
                          AND advance_status = 'pending'
                        """,
                        (
                            now,
                            now,
                            novel_id,
                            chapter_number,
                            actual_sha256,
                            pipeline_version,
                            content_revision,
                        ),
                    )
                    return StoryPipelineAdvance(
                        "already_applied" if cursor.rowcount == 1 else "advance_conflict",
                        chapter_number,
                        content_revision,
                        current_auto_chapters,
                        current_chapter_in_act,
                        current_stage,
                    )
                if current_auto_chapters + 1 != chapter_number:
                    return StoryPipelineAdvance(
                        "advance_out_of_sequence",
                        chapter_number,
                        content_revision,
                        current_auto_chapters,
                        current_chapter_in_act,
                        current_stage,
                        "chapter_number_is_not_next",
                    )

                cursor = conn.execute(
                    """
                    UPDATE chapter_narrative_commits
                    SET advance_status = 'applied', advance_applied_at = ?,
                        updated_at = ?
                    WHERE novel_id = ? AND chapter_number = ?
                      AND content_sha256 = ? AND pipeline_version = ?
                      AND content_revision = ? AND status = 'committed'
                      AND advance_status = 'pending'
                    """,
                    (
                        now,
                        now,
                        novel_id,
                        chapter_number,
                        actual_sha256,
                        pipeline_version,
                        content_revision,
                    ),
                )
                if cursor.rowcount != 1:
                    return StoryPipelineAdvance(
                        "advance_conflict",
                        chapter_number,
                        content_revision,
                        current_auto_chapters,
                        current_chapter_in_act,
                        current_stage,
                        "advance_claim_changed",
                    )

                next_auto_chapters = current_auto_chapters + 1
                next_chapter_in_act = current_chapter_in_act + 1
                conn.execute(
                    """
                    UPDATE novels
                    SET current_auto_chapters = ?, current_chapter_in_act = ?,
                        current_beat_index = 0, beats_completed = 0,
                        current_stage = 'auditing', updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        next_auto_chapters,
                        next_chapter_in_act,
                        now,
                        novel_id,
                    ),
                )
                return StoryPipelineAdvance(
                    "applied",
                    chapter_number,
                    content_revision,
                    next_auto_chapters,
                    next_chapter_in_act,
                    "auditing",
                )

    def recover_pending_story_pipeline_advances(
        self,
        *,
        novel_id: str,
        pipeline_version: str,
        require_memory_sync: bool = False,
    ) -> list[StoryPipelineAdvance]:
        """Replay consecutive pending advances without regenerating canonical prose."""
        novel = self._db.fetch_one(
            "SELECT current_auto_chapters FROM novels WHERE id = ?",
            (novel_id,),
        )
        if novel is None:
            return []
        current_auto_chapters = int(novel["current_auto_chapters"] or 0)
        memory_clause = (
            " AND memory_status = 'committed'" if require_memory_sync else ""
        )
        rows = self._db.fetch_all(
            f"""
            SELECT chapter_number
            FROM chapter_narrative_commits
            WHERE novel_id = ? AND pipeline_version = ?
              AND status = 'committed' AND advance_status = 'pending'
              {memory_clause}
              AND chapter_number > ?
            ORDER BY chapter_number ASC
            """,
            (novel_id, pipeline_version, current_auto_chapters),
        )
        recovered: list[StoryPipelineAdvance] = []
        for row in rows:
            advance = self.advance_story_pipeline_once(
                novel_id=novel_id,
                chapter_number=int(row["chapter_number"]),
                pipeline_version=pipeline_version,
                require_memory_sync=require_memory_sync,
            )
            recovered.append(advance)
            if advance.disposition not in {"applied", "already_applied"}:
                break
        return recovered

    def set_vector_status(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        content_sha256: str,
        pipeline_version: str,
        vector_status: str,
    ) -> None:
        with sqlite_writes_bypass_queue():
            self._db.execute(
                """
                UPDATE chapter_narrative_commits
                SET vector_status = ?, updated_at = CURRENT_TIMESTAMP
                WHERE novel_id = ? AND chapter_number = ?
                  AND content_sha256 = ? AND pipeline_version = ?
                  AND status = 'committed'
                """,
                (
                    vector_status,
                    novel_id,
                    chapter_number,
                    content_sha256,
                    pipeline_version,
                ),
            )
            self._db.commit()

    def is_current_version_ready(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        pipeline_version: str,
        require_memory_sync: bool = False,
    ) -> bool:
        source = self._db.fetch_one(
            "SELECT content, content_sha256, content_revision FROM chapters "
            "WHERE novel_id = ? AND number = ?",
            (novel_id, chapter_number),
        )
        if source is None:
            return False

        content_sha256 = hashlib.sha256(
            (source["content"] or "").encode("utf-8")
        ).hexdigest()
        content_revision = int(source["content_revision"] or 0)
        if source["content_sha256"] != content_sha256 or content_revision < 1:
            return False

        memory_clause = (
            " AND memory_status = 'committed'" if require_memory_sync else ""
        )
        claim = self._db.fetch_one(
            f"""
            SELECT 1
            FROM chapter_narrative_commits
            WHERE novel_id = ? AND chapter_number = ?
              AND content_sha256 = ? AND pipeline_version = ?
              AND content_revision = ? AND status = 'committed'
              {memory_clause}
            """,
            (
                novel_id,
                chapter_number,
                content_sha256,
                pipeline_version,
                content_revision,
            ),
        )
        if claim is None:
            return False

        summary = self._db.fetch_one(
            """
            SELECT 1
            FROM knowledge
            JOIN chapter_summaries
              ON chapter_summaries.knowledge_id = knowledge.id
            WHERE knowledge.novel_id = ?
              AND chapter_summaries.chapter_number = ?
              AND chapter_summaries.source_content_sha256 = ?
              AND chapter_summaries.pipeline_version = ?
              AND chapter_summaries.sync_status = 'committed'
              AND chapter_summaries.summary IS NOT NULL
              AND TRIM(chapter_summaries.summary) != ''
            """,
            (
                novel_id,
                chapter_number,
                content_sha256,
                pipeline_version,
            ),
        )
        return summary is not None

    def set_memory_sync_status(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        content_sha256: str,
        pipeline_version: str,
        content_revision: int,
        memory_status: str,
        failure_reason: str = "",
    ) -> bool:
        """Persist the MemoryEngine barrier for one current canonical version."""
        if memory_status not in {"pending", "committed", "failed"}:
            raise ValueError(f"unsupported memory sync status: {memory_status}")

        now = datetime.now(timezone.utc).isoformat()
        reason = "" if memory_status == "committed" else str(failure_reason or "")[:1000]
        with sqlite_writes_bypass_queue():
            with self._db.transaction() as conn:
                source = conn.execute(
                    "SELECT content, content_sha256, content_revision FROM chapters "
                    "WHERE novel_id = ? AND number = ?",
                    (novel_id, chapter_number),
                ).fetchone()
                if source is None:
                    return False
                actual_sha256 = hashlib.sha256((source[0] or "").encode("utf-8")).hexdigest()
                if (
                    actual_sha256 != content_sha256
                    or (source[1] or "") != content_sha256
                    or int(source[2] or 0) != int(content_revision)
                ):
                    return False

                cursor = conn.execute(
                    """
                    UPDATE chapter_narrative_commits
                    SET memory_status = ?, memory_failure_reason = ?, updated_at = ?
                    WHERE novel_id = ? AND chapter_number = ?
                      AND content_sha256 = ? AND pipeline_version = ?
                      AND content_revision = ? AND status = 'committed'
                    """,
                    (
                        memory_status,
                        reason,
                        now,
                        novel_id,
                        chapter_number,
                        content_sha256,
                        pipeline_version,
                        int(content_revision),
                    ),
                )
                return cursor.rowcount == 1

    def get_committed_summary(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        content_sha256: str,
        pipeline_version: str,
    ) -> str | None:
        """Return only the summary proven by the same committed canonical version."""
        with sqlite_writes_bypass_queue():
            row = self._db.fetch_one(
                """
                SELECT summaries.summary
                FROM chapter_narrative_commits AS commits
                JOIN knowledge
                  ON knowledge.novel_id = commits.novel_id
                JOIN chapter_summaries AS summaries
                  ON summaries.knowledge_id = knowledge.id
                 AND summaries.chapter_number = commits.chapter_number
                WHERE commits.novel_id = ? AND commits.chapter_number = ?
                  AND commits.content_sha256 = ? AND commits.pipeline_version = ?
                  AND commits.status = 'committed'
                  AND summaries.source_content_sha256 = ?
                  AND summaries.pipeline_version = ?
                  AND summaries.sync_status = 'committed'
                  AND summaries.summary IS NOT NULL AND TRIM(summaries.summary) != ''
                """,
                (
                    novel_id,
                    chapter_number,
                    content_sha256,
                    pipeline_version,
                    content_sha256,
                    pipeline_version,
                ),
            )
        return str(row["summary"]) if row is not None else None
