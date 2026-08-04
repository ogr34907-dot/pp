"""Atomic SQLite claims for canonical chapter narrative extraction."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime, timezone

from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.write_dispatch import sqlite_writes_bypass_queue


@dataclass(frozen=True)
class NarrativeClaim:
    disposition: str
    content_revision: int
    attempt_count: int = 1
    vector_status: str = "not_started"
    failure_reason: str = ""


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
    ) -> NarrativeClaim:
        now = datetime.now(timezone.utc).isoformat()
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

                cursor = conn.execute(
                    """
                    INSERT OR IGNORE INTO chapter_narrative_commits (
                        novel_id, chapter_number, content_sha256, pipeline_version,
                        content_revision, status, failure_reason, attempt_count,
                        vector_status, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, 'in_progress', '', 1, 'not_started', ?, ?)
                    """,
                    (
                        novel_id,
                        chapter_number,
                        content_sha256,
                        pipeline_version,
                        revision,
                        now,
                        now,
                    ),
                )
                if cursor.rowcount == 1:
                    return NarrativeClaim("claimed", revision)

                row = conn.execute(
                    """
                    SELECT status, content_revision, attempt_count, vector_status,
                           failure_reason
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
                )

    def commit(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        content_sha256: str,
        pipeline_version: str,
        attempt_count: int,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with sqlite_writes_bypass_queue():
            with self._db.transaction() as conn:
                source = conn.execute(
                    "SELECT content_sha256 FROM chapters WHERE novel_id = ? AND number = ?",
                    (novel_id, chapter_number),
                ).fetchone()
                if source is None or (source[0] or "") != content_sha256:
                    raise RuntimeError("source_hash_mismatch")

                summary_cursor = conn.execute(
                    """
                    UPDATE chapter_summaries
                    SET source_content_sha256 = ?, pipeline_version = ?,
                        sync_status = 'committed', sync_error = '', sync_attempts = ?,
                        updated_at = ?
                    WHERE knowledge_id IN (SELECT id FROM knowledge WHERE novel_id = ?)
                      AND chapter_number = ?
                    """,
                    (
                        content_sha256,
                        pipeline_version,
                        attempt_count,
                        now,
                        novel_id,
                        chapter_number,
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
                      AND status = 'in_progress'
                    """,
                    (
                        now,
                        now,
                        novel_id,
                        chapter_number,
                        content_sha256,
                        pipeline_version,
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
    ) -> None:
        with sqlite_writes_bypass_queue():
            cursor = self._db.execute(
                """
                UPDATE chapter_summaries
                SET source_content_sha256 = ?, pipeline_version = ?,
                    sync_status = 'in_progress', sync_error = '', sync_attempts = ?,
                    updated_at = CURRENT_TIMESTAMP
                WHERE knowledge_id IN (SELECT id FROM knowledge WHERE novel_id = ?)
                  AND chapter_number = ?
                """,
                (
                    content_sha256,
                    pipeline_version,
                    attempt_count,
                    novel_id,
                    chapter_number,
                ),
            )
            self._db.commit()
        if cursor.rowcount != 1:
            raise RuntimeError("canonical_summary_write_missing")

    def fail(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        content_sha256: str,
        pipeline_version: str,
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
                      AND status = 'in_progress'
                    """,
                    (
                        reason,
                        now,
                        novel_id,
                        chapter_number,
                        content_sha256,
                        pipeline_version,
                    ),
                )
                conn.execute(
                    """
                    UPDATE chapter_summaries
                    SET sync_status = 'failed', sync_error = ?, updated_at = ?
                    WHERE knowledge_id IN (SELECT id FROM knowledge WHERE novel_id = ?)
                      AND chapter_number = ?
                      AND source_content_sha256 = ? AND pipeline_version = ?
                    """,
                    (
                        reason,
                        now,
                        novel_id,
                        chapter_number,
                        content_sha256,
                        pipeline_version,
                    ),
                )

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
