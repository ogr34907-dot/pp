"""Fail-closed checks before a new candidate-first generation run starts."""

from __future__ import annotations

import time
from typing import Any

from domain.novel.target_chapters import positive_integer_or_none


class GenerationStartPreflightError(RuntimeError):
    """A user-actionable condition prevents a new generation run."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(f"generation_preflight:{reason}")


class GenerationStartPreflight:
    """Read the durable planning and recovery barriers without mutating state."""

    def __init__(self, db: Any, candidate_repository: Any, outline_service: Any) -> None:
        self.db = db
        self.candidate_repository = candidate_repository
        self.outline_service = outline_service

    def ensure_startable(self, novel_id: str) -> None:
        conn = self.db.get_connection()
        novel = conn.execute(
            "SELECT autopilot_recovery_reason, target_chapters FROM novels WHERE id = ?",
            (novel_id,),
        ).fetchone()
        if novel is None:
            raise KeyError(f"novel not found: {novel_id}")
        if positive_integer_or_none(novel["target_chapters"]) is None:
            raise GenerationStartPreflightError("target_chapters_required")
        if self._full_resync_is_active(str(novel["autopilot_recovery_reason"] or "")):
            raise GenerationStartPreflightError("canonical_resync_active")

        run = conn.execute(
            """
            SELECT state, canonical_sync_status, generation_epoch
            FROM novel_generation_runs WHERE novel_id = ?
            """,
            (novel_id,),
        ).fetchone()
        if run is not None:
            status = str(run["canonical_sync_status"] or "ready")
            if status != "ready":
                reason = "worldline_rebuild_active" if status == "rebuilding" else "canonical_sync_not_ready"
                raise GenerationStartPreflightError(reason)
            if str(run["state"] or "") == "waiting_planning":
                raise GenerationStartPreflightError("outline_expansion_required")
            if str(run["state"] or "") in {"running", "waiting_review"}:
                raise GenerationStartPreflightError("generation_run_active")
            rebuilding = conn.execute(
                """
                SELECT 1 FROM worldline_rebuild_jobs
                WHERE novel_id = ? AND generation_epoch = ? AND status IN ('pending', 'running')
                LIMIT 1
                """,
                (novel_id, int(run["generation_epoch"] or 0)),
            ).fetchone()
            if rebuilding is not None:
                raise GenerationStartPreflightError("worldline_rebuild_active")

        try:
            self.candidate_repository.assert_formal_history_is_proven(novel_id)
        except Exception as exc:
            raise GenerationStartPreflightError("unproven_formal_history") from exc

        formal_head = self.candidate_repository.formal_chapter_head(novel_id)
        try:
            self.candidate_repository.require_formal_prefix_aftermath_ready(
                novel_id, formal_head
            )
        except Exception as exc:
            raise GenerationStartPreflightError("canonical_aftermath_not_ready") from exc
        next_chapter = formal_head + 1
        if not self.candidate_repository.formal_slot_is_available(
            novel_id, next_chapter
        ):
            raise GenerationStartPreflightError("next_chapter_number_conflict")
        try:
            node, _ = self.outline_service.next_published_chapter_context(
                novel_id, after_chapter=formal_head
            )
        except Exception as exc:
            raise GenerationStartPreflightError("outline_chain_not_ready") from exc
        if int(node.number) != next_chapter:
            raise GenerationStartPreflightError("next_outline_chapter_mismatch")

    @staticmethod
    def _full_resync_is_active(marker: str) -> bool:
        marker = str(marker or "")
        prefix = "canonical_aftermath_full_resync:"
        if not marker.startswith(prefix):
            return False
        if any(f"|{status}" in marker for status in ("failed", "cancelled", "unavailable")):
            return False
        try:
            started = float(marker.split("|", 1)[0].rsplit(":", 1)[-1])
        except (TypeError, ValueError):
            return True
        return time.time() - started < 600.0
