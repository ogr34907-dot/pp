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
