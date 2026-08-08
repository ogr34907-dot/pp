"""Ordered, resumable canonical aftermath synchronization for completed chapters."""
from __future__ import annotations

import hashlib
import asyncio
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable

from application.world.services.chapter_narrative_sync import CHAPTER_NARRATIVE_PIPELINE_VERSION
from infrastructure.persistence.database.sqlite_chapter_narrative_commit_repository import (
    SqliteChapterNarrativeCommitRepository,
)

_RUN_LOCK = threading.Lock()
_ACTIVE_RUNS: set[str] = set()


@dataclass
class FullResyncResult:
    run_id: str
    total_chapters: int
    processed_count: int = 0
    synced_count: int = 0
    skipped_count: int = 0
    vector_failed_chapters: list[int] = field(default_factory=list)
    failed_chapter: int | None = None
    failure_reason: str = ""
    status: str = "completed"
    remains_paused: bool = True


async def resync_all_completed_chapters(
    *,
    novel_id: str,
    database: Any,
    aftermath_pipeline: Any,
    emit: Callable[[dict[str, Any]], Awaitable[None]] | None = None,
) -> FullResyncResult:
    run_id = str(uuid.uuid4())
    commit_repository = SqliteChapterNarrativeCommitRepository(database)
    rows = database.fetch_all(
        "SELECT number, content, content_sha256, content_revision FROM chapters "
        "WHERE novel_id = ? AND status = 'completed' AND trim(COALESCE(content, '')) != '' "
        "ORDER BY number ASC",
        (novel_id,),
    )
    result = FullResyncResult(run_id=run_id, total_chapters=len(rows))

    with _RUN_LOCK:
        if novel_id in _ACTIVE_RUNS:
            result.status = "conflict"
            return result
        _ACTIVE_RUNS.add(novel_id)
    claimed = False
    try:
        claimed = commit_repository.claim_full_resync(novel_id=novel_id, run_id=run_id)
        if not claimed:
            result.status = "conflict"
            return result
        await _emit(emit, {"type": "started", "run_id": run_id, "total": result.total_chapters,
                           "pending_chapters": result.total_chapters})

        for raw in rows:
            number = int(raw["number"])
            content = str(raw.get("content") or "")
            expected_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
            stored_hash = str(raw.get("content_sha256") or "")
            revision = int(raw.get("content_revision") or 0)
            if stored_hash != expected_hash or revision < 1:
                result = _fail(result, number, "source_version_mismatch")
                commit_repository.mark_full_resync_failure(novel_id=novel_id, run_id=run_id, reason=result.failure_reason)
                await _emit_failure(emit, result)
                break

            await _pause(database, novel_id)
            ready = commit_repository.is_current_version_ready(
                novel_id=novel_id,
                chapter_number=number,
                pipeline_version=CHAPTER_NARRATIVE_PIPELINE_VERSION,
                require_memory_sync=True,
            )
            commit = database.fetch_one(
                "SELECT status, vector_status, memory_status, attempt_count FROM chapter_narrative_commits "
                "WHERE novel_id = ? AND chapter_number = ? AND content_sha256 = ? AND pipeline_version = ? "
                "AND content_revision = ?",
                (novel_id, number, expected_hash, CHAPTER_NARRATIVE_PIPELINE_VERSION, revision),
            )
            vector_status = str(commit.get("vector_status") or "not_started") if commit else "not_started"
            if ready and vector_status == "stored":
                result.skipped_count += 1
                result.processed_count += 1
                await _emit_chapter(emit, result, number, "skipped")
                commit_repository.renew_full_resync(novel_id=novel_id, run_id=run_id)
                continue

            if commit:
                if commit.get("status") == "failed":
                    commit_repository.reclaim_terminal_failure(
                        novel_id=novel_id, chapter_number=number, content_sha256=expected_hash,
                        pipeline_version=CHAPTER_NARRATIVE_PIPELINE_VERSION, content_revision=revision,
                    )
                elif commit.get("status") == "committed" and commit.get("memory_status") == "failed":
                    commit_repository.reclaim_terminal_memory_failure(
                        novel_id=novel_id, chapter_number=number, content_sha256=expected_hash,
                        pipeline_version=CHAPTER_NARRATIVE_PIPELINE_VERSION, content_revision=revision,
                    )

            try:
                outcome = await aftermath_pipeline.run_after_chapter_saved(
                    novel_id, number, content,
                    expected_content_sha256=expected_hash,
                    expected_content_revision=revision,
                )
            except asyncio.CancelledError:
                result.status = "cancelled"
                raise
            except Exception as exc:
                outcome = {"narrative_sync_ok": False, "failure_reason": str(exc)}

            current = database.fetch_one(
                "SELECT content, content_sha256, content_revision FROM chapters WHERE novel_id = ? AND number = ?",
                (novel_id, number),
            )
            current_hash = hashlib.sha256(str(current.get("content") or "").encode()).hexdigest() if current else ""
            if not current or current_hash != expected_hash or int(current.get("content_revision") or 0) != revision:
                result = _fail(result, number, "source_version_mismatch")
                commit_repository.mark_full_resync_failure(novel_id=novel_id, run_id=run_id, reason=result.failure_reason)
                await _emit_failure(emit, result)
                break
            ready_after = commit_repository.is_current_version_ready(
                novel_id=novel_id, chapter_number=number,
                pipeline_version=CHAPTER_NARRATIVE_PIPELINE_VERSION, require_memory_sync=True,
            )
            latest = database.fetch_one(
                "SELECT vector_status FROM chapter_narrative_commits WHERE novel_id = ? AND chapter_number = ? "
                "AND content_sha256 = ? AND content_revision = ?",
                (novel_id, number, expected_hash, revision),
            )
            latest_vector = str((latest or {}).get("vector_status") or "not_started")
            if not bool(outcome.get("narrative_sync_ok")) or not ready_after:
                reason = str(outcome.get("failure_reason") or "canonical_aftermath_not_ready")
                result = _fail(result, number, reason)
                commit_repository.mark_full_resync_failure(novel_id=novel_id, run_id=run_id, reason=result.failure_reason)
                await _emit_failure(emit, result)
                break
            result.synced_count += 1
            result.processed_count += 1
            if latest_vector != "stored":
                result.vector_failed_chapters.append(number)
                await _emit(emit, {"type": "vector", "run_id": run_id, "chapter_number": number,
                                   "status": latest_vector, "processed": result.processed_count, "total": result.total_chapters})
            await _emit_chapter(emit, result, number, "synced")
            commit_repository.renew_full_resync(novel_id=novel_id, run_id=run_id)

        if result.status == "completed":
            commit_repository.finish_full_resync(novel_id=novel_id, run_id=run_id)
            await _emit(emit, {"type": "completed", "run_id": run_id, "processed": result.processed_count,
                               "synced": result.synced_count, "skipped": result.skipped_count,
                               "vector_failed": result.vector_failed_chapters, "total": result.total_chapters,
                               "remains_paused": True})
        return result
    finally:
        with _RUN_LOCK:
            _ACTIVE_RUNS.discard(novel_id)


def _fail(result: FullResyncResult, chapter: int, reason: str) -> FullResyncResult:
    result.status = "failed"
    result.failed_chapter = chapter
    result.failure_reason = reason
    return result


async def _pause(database: Any, novel_id: str) -> None:
    database.execute("UPDATE novels SET current_stage='paused_for_review', autopilot_status='paused' WHERE id=?", (novel_id,))
    database.commit()


async def _emit(emit: Any, event: dict[str, Any]) -> None:
    if emit is not None:
        await emit(event)


async def _emit_chapter(emit: Any, result: FullResyncResult, number: int, action: str) -> None:
    await _emit(emit, {"type": "chapter", "run_id": result.run_id, "chapter_number": number,
                       "action": action, "processed": result.processed_count, "synced": result.synced_count,
                       "skipped": result.skipped_count, "total": result.total_chapters})


async def _emit_failure(emit: Any, result: FullResyncResult) -> None:
    await _emit(emit, {"type": "failed", "run_id": result.run_id, "chapter_number": result.failed_chapter,
                       "processed": result.processed_count, "synced": result.synced_count,
                       "skipped": result.skipped_count, "total": result.total_chapters,
                       "failure_reason": result.failure_reason})
