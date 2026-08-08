"""Atomic SQLite claims for canonical chapter narrative extraction."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from domain.knowledge.chapter_summary import canonical_summary_payload_sha256
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.write_dispatch import sqlite_writes_bypass_queue


MAX_NARRATIVE_SYNC_ATTEMPTS = 3
MAX_MEMORY_SYNC_ATTEMPTS = 3
# The daemon allows five minutes for the complete aftermath stage.  Keep the
# lease longer so a live writer cannot be reclaimed by a concurrent process.
MEMORY_SYNC_LEASE_SECONDS = 600
CANONICAL_RECOVERY_LEASE_SECONDS = 600


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

    def claim_full_resync(
        self, *, novel_id: str, run_id: str, lease_seconds: int = 600
    ) -> bool:
        """Claim the durable per-novel full-resync marker with a SQLite CAS."""
        if not run_id:
            raise ValueError("run_id_required")
        now = datetime.now(timezone.utc)
        marker = f"canonical_aftermath_full_resync:{run_id}:{now.timestamp():.6f}"
        with sqlite_writes_bypass_queue():
            with self._db.transaction() as conn:
                row = conn.execute(
                    "SELECT autopilot_recovery_reason FROM novels WHERE id = ?",
                    (novel_id,),
                ).fetchone()
                if row is None:
                    return False
                current = str(row[0] or "")
                if current.startswith("canonical_aftermath_full_resync:"):
                    if "|failed" in current or "|cancelled" in current:
                        started = 0.0
                    else:
                        started = None
                    try:
                        if started is None:
                            started = float(current.split(":")[-1].split("|", 1)[0])
                    except (TypeError, ValueError):
                        started = now.timestamp()
                    if started > now.timestamp() - max(1, int(lease_seconds)):
                        return current.startswith(f"canonical_aftermath_full_resync:{run_id}:")
                elif current and current not in {
                    "canonical_aftermath_not_ready",
                    "paused_for_review_preserved",
                }:
                    return False
                cursor = conn.execute(
                    "UPDATE novels SET autopilot_recovery_reason = ?, "
                    "current_stage = 'paused_for_review', autopilot_status = 'paused', "
                    "updated_at = CURRENT_TIMESTAMP WHERE id = ? AND autopilot_recovery_reason = ?",
                    (marker, novel_id, current),
                )
                return cursor.rowcount == 1

    def mark_full_resync_failure(
        self, *, novel_id: str, run_id: str, reason: str, status: str = "failed"
    ) -> bool:
        prefix = f"canonical_aftermath_full_resync:{run_id}:"
        with sqlite_writes_bypass_queue():
            with self._db.transaction() as conn:
                row = conn.execute(
                    "SELECT autopilot_recovery_reason FROM novels WHERE id = ?", (novel_id,)
                ).fetchone()
                current = str(row[0] or "") if row else ""
                if not current.startswith(prefix):
                    return False
                marker = f"{current.split('|', 1)[0]}|{status}:{reason[:240]}"
                cursor = conn.execute(
                    "UPDATE novels SET autopilot_recovery_reason = ?, current_stage='paused_for_review', "
                    "autopilot_status='paused', updated_at=CURRENT_TIMESTAMP WHERE id=? AND autopilot_recovery_reason=?",
                    (marker, novel_id, current),
                )
                return cursor.rowcount == 1

    def renew_full_resync(self, *, novel_id: str, run_id: str) -> bool:
        """Refresh a run marker while retaining ownership."""
        prefix = f"canonical_aftermath_full_resync:{run_id}:"
        now = datetime.now(timezone.utc)
        marker = f"{prefix}{now.timestamp():.6f}"
        with sqlite_writes_bypass_queue():
            with self._db.transaction() as conn:
                row = conn.execute(
                    "SELECT autopilot_recovery_reason FROM novels WHERE id = ?", (novel_id,)
                ).fetchone()
                current = str(row[0] or "") if row else ""
                if not current.startswith(prefix):
                    return False
                cursor = conn.execute(
                    "UPDATE novels SET autopilot_recovery_reason = ?, updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ? AND autopilot_recovery_reason = ?",
                    (marker, novel_id, current),
                )
                return cursor.rowcount == 1

    def finish_full_resync(self, *, novel_id: str, run_id: str) -> bool:
        """Clear the marker only when this run still owns it."""
        prefix = f"canonical_aftermath_full_resync:{run_id}:"
        with sqlite_writes_bypass_queue():
            with self._db.transaction() as conn:
                row = conn.execute(
                    "SELECT autopilot_recovery_reason FROM novels WHERE id = ?", (novel_id,)
                ).fetchone()
                current = str(row[0] or "") if row else ""
                if not current.startswith(prefix):
                    return False
                cursor = conn.execute(
                    "UPDATE novels SET autopilot_recovery_reason = '', updated_at = CURRENT_TIMESTAMP "
                    "WHERE id = ? AND autopilot_recovery_reason = ?",
                    (novel_id, current),
                )
                return cursor.rowcount == 1

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

    def reclaim_terminal_failure(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        content_sha256: str,
        pipeline_version: str,
        content_revision: int,
    ) -> bool:
        """Make an abandoned current recovery claim available for one manual cycle.

        Automatic retries deliberately stop after three attempts.  This method is
        intentionally separate from :meth:`claim`: it can only be called by the
        explicit recovery action and it verifies the persisted prose version in
        the same transaction before making the claim reclaimable again.
        """
        now = datetime.now(timezone.utc).isoformat()
        with sqlite_writes_bypass_queue():
            with self._db.transaction() as conn:
                source = conn.execute(
                    "SELECT content, content_sha256, content_revision FROM chapters "
                    "WHERE novel_id = ? AND number = ?",
                    (novel_id, chapter_number),
                ).fetchone()
                actual_sha256 = hashlib.sha256(
                    (source[0] or "").encode("utf-8")
                ).hexdigest() if source is not None else ""
                if (
                    source is None
                    or actual_sha256 != content_sha256
                    or (source[1] or "") != content_sha256
                    or int(source[2] or 0) != int(content_revision)
                ):
                    return False

                claim = conn.execute(
                    """
                    SELECT status, attempt_count, updated_at
                    FROM chapter_narrative_commits
                    WHERE novel_id = ? AND chapter_number = ?
                      AND content_sha256 = ? AND pipeline_version = ?
                      AND content_revision = ?
                    """,
                    (
                        novel_id,
                        chapter_number,
                        content_sha256,
                        pipeline_version,
                        int(content_revision),
                    ),
                ).fetchone()
                if claim is None:
                    return False

                status = str(claim[0] or "")
                attempt_count = int(claim[1] or 0)
                updated_at = claim[2]
                lease_expired = False
                if status in {"stale", "in_progress"}:
                    try:
                        modified_at = datetime.fromisoformat(
                            str(updated_at).replace("Z", "+00:00")
                        )
                        if modified_at.tzinfo is None:
                            modified_at = modified_at.replace(tzinfo=timezone.utc)
                        lease_expired = modified_at <= (
                            datetime.now(timezone.utc)
                            - timedelta(seconds=CANONICAL_RECOVERY_LEASE_SECONDS)
                        )
                    except (TypeError, ValueError):
                        lease_expired = True

                if not (
                    (status == "failed" and attempt_count >= 3)
                    or (status in {"stale", "in_progress"} and lease_expired)
                ):
                    return False

                cursor = conn.execute(
                    """
                    UPDATE chapter_narrative_commits
                    SET status = 'stale', updated_at = ?
                    WHERE novel_id = ? AND chapter_number = ?
                      AND content_sha256 = ? AND pipeline_version = ?
                      AND content_revision = ?
                      AND status = ? AND updated_at IS ?
                    """,
                    (
                        now,
                        novel_id,
                        chapter_number,
                        content_sha256,
                        pipeline_version,
                        int(content_revision),
                        status,
                        updated_at,
                    ),
                )
                if cursor.rowcount != 1:
                    return False

                # A stale failed summary must never be selected as an active
                # canonical input while the new recovery cycle is in progress.
                conn.execute(
                    """
                    UPDATE chapter_summaries
                    SET sync_status = 'stale', updated_at = ?
                    WHERE knowledge_id IN (SELECT id FROM knowledge WHERE novel_id = ?)
                      AND chapter_number = ?
                      AND source_content_sha256 = ?
                      AND source_content_revision = ?
                      AND pipeline_version = ?
                      AND sync_status = 'failed'
                    """,
                    (
                        now,
                        novel_id,
                        chapter_number,
                        content_sha256,
                        int(content_revision),
                        pipeline_version,
                    ),
                )
                return True

    def reclaim_terminal_memory_failure(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        content_sha256: str,
        pipeline_version: str,
        content_revision: int,
    ) -> bool:
        """Reopen one explicit retry cycle for an exhausted MemoryEngine sync."""
        now = datetime.now(timezone.utc).isoformat()
        with sqlite_writes_bypass_queue():
            with self._db.transaction() as conn:
                source = conn.execute(
                    "SELECT content, content_sha256, content_revision FROM chapters "
                    "WHERE novel_id = ? AND number = ?",
                    (novel_id, chapter_number),
                ).fetchone()
                actual_sha256 = (
                    hashlib.sha256((source[0] or "").encode("utf-8")).hexdigest()
                    if source is not None
                    else ""
                )
                if (
                    source is None
                    or actual_sha256 != content_sha256
                    or (source[1] or "") != content_sha256
                    or int(source[2] or 0) != int(content_revision)
                ):
                    return False

                cursor = conn.execute(
                    """
                    UPDATE chapter_narrative_commits
                    SET memory_status = 'pending', memory_failure_reason = '',
                        memory_attempt_count = 0, updated_at = ?
                    WHERE novel_id = ? AND chapter_number = ?
                      AND content_sha256 = ? AND pipeline_version = ?
                      AND content_revision = ? AND status = 'committed'
                      AND memory_status = 'failed'
                      AND memory_attempt_count >= ?
                    """,
                    (
                        now,
                        novel_id,
                        chapter_number,
                        content_sha256,
                        pipeline_version,
                        int(content_revision),
                        MAX_MEMORY_SYNC_ATTEMPTS,
                    ),
                )
                return cursor.rowcount == 1

    def claim_bounded_automatic_recovery(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        content_sha256: str,
        pipeline_version: str,
        content_revision: int,
        expected_failure_reason: str,
        recovery_marker: str,
    ) -> str:
        """Atomically consume one automatic recovery cycle for an exact version."""
        if not recovery_marker:
            raise ValueError("recovery_marker_required")

        now = datetime.now(timezone.utc).isoformat()
        allowed_reasons = {
            "",
            "canonical_aftermath_not_ready",
            "paused_for_review_preserved",
        }
        with sqlite_writes_bypass_queue():
            with self._db.transaction() as conn:
                source = conn.execute(
                    "SELECT content, content_sha256, content_revision FROM chapters "
                    "WHERE novel_id = ? AND number = ?",
                    (novel_id, chapter_number),
                ).fetchone()
                actual_sha256 = (
                    hashlib.sha256((source[0] or "").encode("utf-8")).hexdigest()
                    if source is not None
                    else ""
                )
                if (
                    source is None
                    or actual_sha256 != content_sha256
                    or (source[1] or "") != content_sha256
                    or int(source[2] or 0) != int(content_revision)
                ):
                    return "source_version_mismatch"

                novel = conn.execute(
                    "SELECT autopilot_recovery_reason FROM novels WHERE id = ?",
                    (novel_id,),
                ).fetchone()
                if novel is None:
                    return "novel_not_found"
                current_marker = str(novel[0] or "")
                if current_marker == recovery_marker:
                    return "exhausted"
                if (
                    current_marker not in allowed_reasons
                    and not current_marker.startswith(
                        "canonical_aftermath_auto_recovery:v1:"
                    )
                ):
                    return "blocked"

                claim = conn.execute(
                    """
                    SELECT status, attempt_count, failure_reason, updated_at
                    FROM chapter_narrative_commits
                    WHERE novel_id = ? AND chapter_number = ?
                      AND content_sha256 = ? AND pipeline_version = ?
                      AND content_revision = ?
                    """,
                    (
                        novel_id,
                        chapter_number,
                        content_sha256,
                        pipeline_version,
                        int(content_revision),
                    ),
                ).fetchone()
                if claim is None:
                    return "not_terminal"
                if (
                    str(claim[0] or "") != "failed"
                    or int(claim[1] or 0) < MAX_NARRATIVE_SYNC_ATTEMPTS
                    or str(claim[2] or "") != str(expected_failure_reason or "")
                ):
                    return "not_terminal"

                claim_cursor = conn.execute(
                    """
                    UPDATE chapter_narrative_commits
                    SET status = 'stale', updated_at = ?
                    WHERE novel_id = ? AND chapter_number = ?
                      AND content_sha256 = ? AND pipeline_version = ?
                      AND content_revision = ? AND status = 'failed'
                      AND attempt_count >= ? AND failure_reason = ?
                      AND updated_at IS ?
                    """,
                    (
                        now,
                        novel_id,
                        chapter_number,
                        content_sha256,
                        pipeline_version,
                        int(content_revision),
                        MAX_NARRATIVE_SYNC_ATTEMPTS,
                        str(expected_failure_reason or ""),
                        claim[3],
                    ),
                )
                if claim_cursor.rowcount != 1:
                    return "conflict"

                marker_cursor = conn.execute(
                    """
                    UPDATE novels
                    SET autopilot_recovery_reason = ?, updated_at = ?
                    WHERE id = ? AND autopilot_recovery_reason IS ?
                    """,
                    (recovery_marker, now, novel_id, novel[0]),
                )
                if marker_cursor.rowcount != 1:
                    raise RuntimeError("automatic_recovery_marker_conflict")

                conn.execute(
                    """
                    UPDATE chapter_summaries
                    SET sync_status = 'stale', updated_at = ?
                    WHERE knowledge_id IN (SELECT id FROM knowledge WHERE novel_id = ?)
                      AND chapter_number = ?
                      AND source_content_sha256 = ?
                      AND source_content_revision = ?
                      AND pipeline_version = ?
                      AND sync_status = 'failed'
                    """,
                    (
                        now,
                        novel_id,
                        chapter_number,
                        content_sha256,
                        int(content_revision),
                        pipeline_version,
                    ),
                )
                return "claimed"

    def restore_terminal_failure(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        content_sha256: str,
        pipeline_version: str,
        content_revision: int,
        failure_reason: str,
    ) -> None:
        """Return an interrupted manual recovery to a visible terminal failure."""
        now = datetime.now(timezone.utc).isoformat()
        reason = (failure_reason or "canonical_aftermath_retry_failed")[:1000]
        with sqlite_writes_bypass_queue():
            with self._db.transaction() as conn:
                source = conn.execute(
                    "SELECT content, content_sha256, content_revision FROM chapters "
                    "WHERE novel_id = ? AND number = ?",
                    (novel_id, chapter_number),
                ).fetchone()
                actual_sha256 = hashlib.sha256(
                    (source[0] or "").encode("utf-8")
                ).hexdigest() if source is not None else ""
                if (
                    source is None
                    or actual_sha256 != content_sha256
                    or (source[1] or "") != content_sha256
                    or int(source[2] or 0) != int(content_revision)
                ):
                    return
                conn.execute(
                    """
                    UPDATE chapter_narrative_commits
                    SET status = 'failed', attempt_count = 3, failure_reason = ?, updated_at = ?
                    WHERE novel_id = ? AND chapter_number = ?
                      AND content_sha256 = ? AND pipeline_version = ?
                      AND content_revision = ?
                      AND status IN ('stale', 'in_progress')
                    """,
                    (
                        reason,
                        now,
                        novel_id,
                        chapter_number,
                        content_sha256,
                        pipeline_version,
                        int(content_revision),
                    ),
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
                      AND source_content_revision = ?
                      AND pipeline_version = ?
                      AND sync_status = 'in_progress'
                      AND sync_attempts = ?
                    """,
                    (
                        novel_id,
                        chapter_number,
                        content_sha256,
                        content_revision,
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
                    SET source_content_sha256 = ?, source_content_revision = ?,
                        pipeline_version = ?,
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
                      AND source_content_revision = ?
                      AND pipeline_version = ?
                      AND sync_status = 'in_progress'
                      AND sync_attempts = ?
                    """,
                    (
                        content_sha256,
                        content_revision,
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
                        content_revision,
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
                    SET source_content_sha256 = ?, source_content_revision = ?,
                        pipeline_version = ?,
                        sync_status = 'in_progress', sync_error = '', sync_attempts = ?,
                        canonical_payload_sha256 = ?, updated_at = CURRENT_TIMESTAMP
                    WHERE id = ?
                    """,
                    (
                        content_sha256,
                        int(claim[0]),
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
                      AND source_content_revision = ?
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
                        content_revision,
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
                      AND chapter_summaries.source_content_revision = ?
                      AND chapter_summaries.pipeline_version = ?
                      AND chapter_summaries.sync_status = 'committed'
                      AND chapter_summaries.summary IS NOT NULL
                      AND TRIM(chapter_summaries.summary) != ''
                    """,
                    (
                        novel_id,
                        chapter_number,
                        actual_sha256,
                        content_revision,
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
              AND chapter_summaries.source_content_revision = ?
              AND chapter_summaries.pipeline_version = ?
              AND chapter_summaries.sync_status = 'committed'
              AND chapter_summaries.summary IS NOT NULL
              AND TRIM(chapter_summaries.summary) != ''
            """,
            (
                novel_id,
                chapter_number,
                content_sha256,
                content_revision,
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
                      AND (? = 'committed' OR memory_status != 'committed')
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
                        memory_status,
                    ),
                )
                return cursor.rowcount == 1

    def claim_memory_sync(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        content_sha256: str,
        pipeline_version: str,
        content_revision: int,
    ) -> str:
        """Atomically reserve one committed narrative version for MemoryEngine."""
        now = datetime.now(timezone.utc).isoformat()
        lease_cutoff = (
            datetime.now(timezone.utc)
            - timedelta(seconds=MEMORY_SYNC_LEASE_SECONDS)
        ).isoformat()
        with sqlite_writes_bypass_queue():
            with self._db.transaction() as conn:
                source = conn.execute(
                    "SELECT content, content_sha256, content_revision FROM chapters "
                    "WHERE novel_id = ? AND number = ?",
                    (novel_id, chapter_number),
                ).fetchone()
                actual_sha256 = hashlib.sha256(
                    (source[0] or "").encode("utf-8")
                ).hexdigest() if source is not None else ""
                if (
                    source is None
                    or actual_sha256 != content_sha256
                    or (source[1] or "") != content_sha256
                    or int(source[2] or 0) != int(content_revision)
                ):
                    return "source_version_mismatch"

                cursor = conn.execute(
                    """
                    UPDATE chapter_narrative_commits
                    SET memory_status = 'in_progress', memory_failure_reason = '',
                        memory_attempt_count = memory_attempt_count + 1, updated_at = ?
                    WHERE novel_id = ? AND chapter_number = ?
                      AND content_sha256 = ? AND pipeline_version = ?
                      AND content_revision = ? AND status = 'committed'
                      AND memory_attempt_count < ?
                      AND (
                          memory_status IN ('pending', 'failed')
                          OR (
                              memory_status = 'in_progress'
                              AND datetime(updated_at) <= datetime(?)
                          )
                      )
                    """,
                    (
                        now,
                        novel_id,
                        chapter_number,
                        content_sha256,
                        pipeline_version,
                        int(content_revision),
                        MAX_MEMORY_SYNC_ATTEMPTS,
                        lease_cutoff,
                    ),
                )
                if cursor.rowcount == 1:
                    return "claimed"

                row = conn.execute(
                    """
                    SELECT status, memory_status, memory_attempt_count
                    FROM chapter_narrative_commits
                    WHERE novel_id = ? AND chapter_number = ?
                      AND content_sha256 = ? AND pipeline_version = ?
                      AND content_revision = ?
                    """,
                    (
                        novel_id,
                        chapter_number,
                        content_sha256,
                        pipeline_version,
                        int(content_revision),
                    ),
                ).fetchone()
                if row is None or (row[0] or "") != "committed":
                    return "source_version_mismatch"
                if (row[1] or "") == "committed":
                    return "reused"
                if int(row[2] or 0) >= MAX_MEMORY_SYNC_ATTEMPTS:
                    return "exhausted"
                if (row[1] or "") == "in_progress":
                    return "in_progress"
                return "source_version_mismatch"

    def finish_memory_sync(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        content_sha256: str,
        pipeline_version: str,
        content_revision: int,
    ) -> bool:
        """Mark a claimed MemoryEngine update readable after its state is durable."""
        return self.set_memory_sync_status(
            novel_id=novel_id,
            chapter_number=chapter_number,
            content_sha256=content_sha256,
            pipeline_version=pipeline_version,
            content_revision=content_revision,
            memory_status="committed",
        )

    def fail_memory_sync(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        content_sha256: str,
        pipeline_version: str,
        content_revision: int,
        failure_reason: str,
    ) -> bool:
        """Record a failed claimed MemoryEngine update without touching a newer version."""
        now = datetime.now(timezone.utc).isoformat()
        reason = str(failure_reason or "memory_engine_update_failed")[:1000]
        with sqlite_writes_bypass_queue():
            with self._db.transaction() as conn:
                source = conn.execute(
                    "SELECT content, content_sha256, content_revision FROM chapters "
                    "WHERE novel_id = ? AND number = ?",
                    (novel_id, chapter_number),
                ).fetchone()
                actual_sha256 = hashlib.sha256(
                    (source[0] or "").encode("utf-8")
                ).hexdigest() if source is not None else ""
                if (
                    source is None
                    or actual_sha256 != content_sha256
                    or (source[1] or "") != content_sha256
                    or int(source[2] or 0) != int(content_revision)
                ):
                    return False
                cursor = conn.execute(
                    """
                    UPDATE chapter_narrative_commits
                    SET memory_status = 'failed', memory_failure_reason = ?, updated_at = ?
                    WHERE novel_id = ? AND chapter_number = ?
                      AND content_sha256 = ? AND pipeline_version = ?
                      AND content_revision = ? AND status = 'committed'
                      AND memory_status = 'in_progress'
                    """,
                    (
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

    def get_memory_sync_status(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        content_sha256: str,
        pipeline_version: str,
        content_revision: int,
    ) -> str | None:
        """Return the barrier state only for the current canonical prose version."""
        source = self._db.fetch_one(
            "SELECT content, content_sha256, content_revision FROM chapters "
            "WHERE novel_id = ? AND number = ?",
            (novel_id, chapter_number),
        )
        if source is None:
            return None
        actual_sha256 = hashlib.sha256(
            (source["content"] or "").encode("utf-8")
        ).hexdigest()
        if (
            actual_sha256 != content_sha256
            or (source["content_sha256"] or "") != content_sha256
            or int(source["content_revision"] or 0) != int(content_revision)
        ):
            return None

        row = self._db.fetch_one(
            """
            SELECT memory_status
            FROM chapter_narrative_commits
            WHERE novel_id = ? AND chapter_number = ?
              AND content_sha256 = ? AND pipeline_version = ?
              AND content_revision = ? AND status = 'committed'
            """,
            (
                novel_id,
                chapter_number,
                content_sha256,
                pipeline_version,
                int(content_revision),
            ),
        )
        return str(row["memory_status"] or "not_required") if row else None

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
                JOIN chapters AS source
                  ON source.novel_id = commits.novel_id
                 AND source.number = commits.chapter_number
                 AND source.content_sha256 = commits.content_sha256
                 AND source.content_revision = commits.content_revision
                JOIN knowledge
                  ON knowledge.novel_id = commits.novel_id
                JOIN chapter_summaries AS summaries
                  ON summaries.knowledge_id = knowledge.id
                 AND summaries.chapter_number = commits.chapter_number
                WHERE commits.novel_id = ? AND commits.chapter_number = ?
                  AND commits.content_sha256 = ? AND commits.pipeline_version = ?
                  AND commits.status = 'committed'
                  AND summaries.source_content_sha256 = ?
                  AND summaries.source_content_revision = commits.content_revision
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
