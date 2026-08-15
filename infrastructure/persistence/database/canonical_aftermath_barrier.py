"""Read-only exact identity gate for Candidate-first canonical aftermath."""

from __future__ import annotations

import sqlite3


def exact_candidate_aftermath_is_ready(
    conn: sqlite3.Connection,
    *,
    novel_id: str,
    chapter_number: int,
    content_sha256: str,
    content_revision: int,
) -> tuple[bool, str]:
    """Require the exact Formal identity to have completed aftermath.

    The narrative pipeline owns Canonical and Memory writes.  This helper is a
    read-only visibility gate shared by cursor advancement and runtime-derived
    Context caches, so neither path can invent a weaker readiness policy.
    """

    from application.world.services.chapter_narrative_sync import (
        CHAPTER_NARRATIVE_PIPELINE_VERSION,
    )

    try:
        narrative = conn.execute(
            """
            SELECT status, memory_status
            FROM chapter_narrative_commits
            WHERE novel_id = ? AND chapter_number = ?
              AND content_sha256 = ? AND content_revision = ?
              AND pipeline_version = ?
            """,
            (
                novel_id,
                int(chapter_number),
                content_sha256,
                int(content_revision),
                CHAPTER_NARRATIVE_PIPELINE_VERSION,
            ),
        ).fetchone()
    except sqlite3.OperationalError:
        return False, "narrative_commit_table_missing"
    if narrative is None:
        return False, "narrative_commit_missing"
    if str(narrative["status"] or "") != "committed":
        return False, "narrative_commit_not_committed"
    if str(narrative["memory_status"] or "") != "committed":
        return False, "memory_not_ready"

    # Candidate-first Formal publication requires a durable canonical summary
    # for the same chapter content identity. There is no per-book opt-out.
    try:
        summary = conn.execute(
            """
            SELECT 1
            FROM chapter_summaries
            WHERE knowledge_id IN (SELECT id FROM knowledge WHERE novel_id = ?)
              AND chapter_number = ?
              AND source_content_sha256 = ?
              AND source_content_revision = ?
              AND pipeline_version = ?
              AND sync_status = 'committed'
              AND summary IS NOT NULL AND TRIM(summary) <> ''
            LIMIT 1
            """,
            (
                novel_id,
                int(chapter_number),
                content_sha256,
                int(content_revision),
                CHAPTER_NARRATIVE_PIPELINE_VERSION,
            ),
        ).fetchone()
    except sqlite3.OperationalError:
        return False, "canonical_summary_table_missing"
    if summary is None:
        return False, "canonical_summary_not_ready"
    return True, ""
