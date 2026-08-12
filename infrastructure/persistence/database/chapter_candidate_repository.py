"""Durable candidate-prose state machine with strict one-candidate backpressure."""

from __future__ import annotations

from datetime import datetime
import hashlib
import json
import sqlite3
from typing import Any, Optional, Union
from uuid import uuid4

from domain.novel.candidate_chapter import (
    CandidateStatus,
    ChapterCandidate,
    GenerationRun,
    GenerationRunState,
    RunMode,
)


class CandidateGateError(ValueError):
    """Raised when a candidate transition would bypass author/commit gates."""


_OPEN_CANDIDATE_STATUSES = {
    CandidateStatus.STREAMING,
    CandidateStatus.AUDITING,
    CandidateStatus.AWAITING_REVIEW,
    CandidateStatus.COMMITTING,
    CandidateStatus.SYNCING,
    CandidateStatus.REGENERATING,
}


class ChapterCandidateRepository:
    """Own candidate persistence; formal chapter writes occur only at commit."""

    def __init__(self, db: Union[str, "DatabaseConnection"]):
        from infrastructure.persistence.database.connection import DatabaseConnection

        if isinstance(db, DatabaseConnection):
            self._db = db
            self.db_path = db.db_path
        else:
            self._db = None
            self.db_path = str(db)

    def _connection(self) -> sqlite3.Connection:
        if self._db is not None:
            return self._db.get_connection()
        from infrastructure.persistence.database.connection import get_database

        return get_database(self.db_path).get_connection()

    @staticmethod
    def _now() -> str:
        return datetime.now().isoformat()

    @staticmethod
    def _run_from_row(row: sqlite3.Row) -> GenerationRun:
        return GenerationRun(
            novel_id=str(row["novel_id"]),
            run_mode=RunMode(str(row["run_mode"])),
            state=GenerationRunState(str(row["state"])),
            generation_epoch=int(row["generation_epoch"] or 0),
            target_chapters=int(row["target_chapters"] or 0),
            current_formal_chapter=int(row["current_formal_chapter"] or 0),
            current_candidate_id=row["current_candidate_id"],
            current_candidate_chapter=row["current_candidate_chapter"],
            canonical_sync_status=str(row["canonical_sync_status"] or "ready"),
            next_action=str(row["next_action"] or ""),
            last_error=str(row["last_error"] or ""),
            max_pending_candidates=int(row["max_pending_candidates"] or 1),
            prefetch=int(row["prefetch"] or 0),
        )

    @staticmethod
    def _candidate_from_row(row: sqlite3.Row) -> ChapterCandidate:
        return ChapterCandidate(
            id=str(row["id"]),
            novel_id=str(row["novel_id"]),
            chapter_number=int(row["chapter_number"]),
            title=str(row["title"] or ""),
            generation_epoch=int(row["generation_epoch"] or 0),
            status=CandidateStatus(str(row["status"])),
            outline_chain=json.loads(row["outline_chain_json"] or "{}"),
            outline_chain_digest=str(row["outline_chain_digest"] or ""),
            llm_content=str(row["llm_content"] or ""),
            author_content=row["author_content"],
            content_revision=int(row["content_revision"] or 0),
            audit_revision=int(row["audit_revision"] or 0),
            commit_plan_revision=int(row["commit_plan_revision"] or 0),
            commit_plan_content_revision=int(row["commit_plan_content_revision"] or 0),
            audit=json.loads(row["audit_json"] or "{}"),
            commit_plan=json.loads(row["commit_plan_json"] or "{}"),
            feedback=str(row["feedback"] or ""),
            failure_reason=str(row["failure_reason"] or ""),
            continue_after_commit=bool(row["continue_after_commit"]),
            formal_chapter_id=row["formal_chapter_id"],
        )

    def get_run(self, novel_id: str) -> GenerationRun:
        row = self._connection().execute(
            "SELECT * FROM novel_generation_runs WHERE novel_id = ?", (novel_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"generation run not found: {novel_id}")
        return self._run_from_row(row)

    def formal_chapter_head(self, novel_id: str) -> int:
        """Return the continuous Candidate-first Canonical chapter head."""

        rows = self._connection().execute(
            """
            SELECT c.number
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
        head = 0
        for row in rows:
            if int(row["number"]) != head + 1:
                break
            head += 1
        return head

    def start_run(
        self,
        novel_id: str,
        *,
        run_mode: RunMode,
        target_chapters: int,
    ) -> GenerationRun:
        conn = self._connection()
        existing = conn.execute(
            "SELECT * FROM novel_generation_runs WHERE novel_id = ?", (novel_id,)
        ).fetchone()
        persisted_chapter_head = self.formal_chapter_head(novel_id)
        if existing is not None:
            current = self._run_from_row(existing)
            if current.canonical_sync_status not in {"ready", ""}:
                raise CandidateGateError(
                    "canonical rebuild or sync is not ready; resolve the current recovery action first"
                )
            if current.current_candidate_id:
                raise CandidateGateError("cannot start a new run while a pending candidate exists")
            epoch = current.generation_epoch
            current_formal_chapter = persisted_chapter_head
        else:
            epoch = 0
            current_formal_chapter = persisted_chapter_head
        now = self._now()
        conn.execute(
            """
            INSERT INTO novel_generation_runs
                (novel_id, run_mode, state, generation_epoch, target_chapters,
                 current_formal_chapter, max_pending_candidates, prefetch,
                 canonical_sync_status, next_action, updated_at)
            VALUES (?, ?, 'running', ?, ?, ?, 1, 0, 'ready', 'generate_candidate', ?)
            ON CONFLICT(novel_id) DO UPDATE SET
                run_mode = excluded.run_mode,
                state = 'running',
                target_chapters = excluded.target_chapters,
                current_formal_chapter = excluded.current_formal_chapter,
                max_pending_candidates = 1,
                prefetch = 0,
                canonical_sync_status = 'ready',
                next_action = 'generate_candidate',
                last_error = '',
                updated_at = excluded.updated_at
            """,
            (novel_id, run_mode.value, epoch, target_chapters, current_formal_chapter, now),
        )
        conn.commit()
        return self.get_run(novel_id)

    def get_candidate(self, candidate_id: str) -> ChapterCandidate:
        row = self._connection().execute(
            "SELECT * FROM chapter_candidates WHERE id = ?", (candidate_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"candidate not found: {candidate_id}")
        return self._candidate_from_row(row)

    def list_versions(self, candidate_id: str) -> list[dict[str, Any]]:
        """Return immutable candidate revisions for the author comparison UI."""

        # Check the aggregate first so callers get the same not-found semantics
        # as all other candidate operations.
        self.get_candidate(candidate_id)
        rows = self._connection().execute(
            """
            SELECT id, content_revision, content, source, feedback, created_at
            FROM chapter_candidate_versions
            WHERE candidate_id = ?
            ORDER BY content_revision DESC, created_at DESC
            """,
            (candidate_id,),
        ).fetchall()
        return [dict(row) for row in rows]

    def get_current_candidate(self, novel_id: str) -> Optional[ChapterCandidate]:
        row = self._connection().execute(
            """
            SELECT c.*
            FROM novel_generation_runs AS r
            JOIN chapter_candidates AS c ON c.id = r.current_candidate_id
            WHERE r.novel_id = ?
            """,
            (novel_id,),
        ).fetchone()
        return self._candidate_from_row(row) if row is not None else None

    def start_dag_run(self, candidate_id: str, *, content_revision: int) -> dict[str, Any]:
        """Create one durable DAG trace for a candidate content revision."""

        candidate = self.get_candidate(candidate_id)
        self._ensure_current_generation(candidate)
        run_id = f"candidate-dag-{uuid4()}"
        now = self._now()
        conn = self._connection()
        conn.execute(
            """
            INSERT INTO candidate_dag_runs
                (id, candidate_id, content_revision, status, started_at, updated_at)
            VALUES (?, ?, ?, 'running', ?, ?)
            """,
            (run_id, candidate_id, int(content_revision), now, now),
        )
        conn.commit()
        return self.get_dag_run(run_id)

    def get_dag_run(self, dag_run_id: str, *, after_sequence: int = 0) -> dict[str, Any]:
        conn = self._connection()
        row = conn.execute(
            "SELECT * FROM candidate_dag_runs WHERE id = ?", (dag_run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"candidate DAG run not found: {dag_run_id}")
        attempts = conn.execute(
            """
            SELECT node_id, node_type, status, duration_ms
            FROM candidate_dag_node_attempts
            WHERE dag_run_id = ?
            ORDER BY started_at, attempt
            """,
            (dag_run_id,),
        ).fetchall()
        events = conn.execute(
            """
            SELECT sequence, event_json FROM candidate_dag_events
            WHERE dag_run_id = ? AND sequence > ?
            ORDER BY sequence
            """,
            (dag_run_id, int(after_sequence)),
        ).fetchall()
        return {
            "id": str(row["id"]),
            "candidate_id": str(row["candidate_id"]),
            "content_revision": int(row["content_revision"]),
            "status": str(row["status"]),
            "current_node_id": str(row["current_node_id"] or ""),
            "final_state": json.loads(row["final_state_json"] or "{}"),
            "failure_reason": str(row["failure_reason"] or ""),
            "node_attempts": [dict(item) for item in attempts],
            "events": [
                {"sequence": int(item["sequence"]), **json.loads(item["event_json"] or "{}")}
                for item in events
            ],
        }

    def get_latest_dag_run(
        self, candidate_id: str, *, after_sequence: int = 0
    ) -> Optional[dict[str, Any]]:
        row = self._connection().execute(
            """
            SELECT id FROM candidate_dag_runs
            WHERE candidate_id = ?
            ORDER BY started_at DESC
            LIMIT 1
            """,
            (candidate_id,),
        ).fetchone()
        return self.get_dag_run(str(row["id"]), after_sequence=after_sequence) if row else None

    def record_dag_event(self, dag_run_id: str, event: dict[str, Any]) -> None:
        """Append one ordered event and maintain the node-attempt read model."""

        conn = self._connection()
        dag_run = conn.execute(
            "SELECT candidate_id FROM candidate_dag_runs WHERE id = ?", (dag_run_id,)
        ).fetchone()
        if dag_run is None:
            raise KeyError(f"candidate DAG run not found: {dag_run_id}")
        candidate = self.get_candidate(str(dag_run["candidate_id"]))
        self._ensure_current_generation(candidate)
        event_type = str(event.get("type") or "")
        node_id = str(event.get("node_id") or "")
        node_type = str(event.get("node_type") or "")
        now = self._now()
        sequence_row = conn.execute(
            "SELECT COALESCE(MAX(sequence), 0) + 1 AS sequence FROM candidate_dag_events WHERE dag_run_id = ?",
            (dag_run_id,),
        ).fetchone()
        sequence = int(sequence_row["sequence"])
        try:
            conn.execute("BEGIN")
            conn.execute(
                """
                INSERT INTO candidate_dag_events (id, dag_run_id, sequence, event_json, created_at)
                VALUES (?, ?, ?, ?, ?)
                """,
                (f"candidate-dag-event-{uuid4()}", dag_run_id, sequence, json.dumps(event, ensure_ascii=False, sort_keys=True), now),
            )
            if node_id and event_type == "node_started":
                attempt_row = conn.execute(
                    """
                    SELECT COALESCE(MAX(attempt), 0) + 1 AS attempt
                    FROM candidate_dag_node_attempts WHERE dag_run_id = ? AND node_id = ?
                    """,
                    (dag_run_id, node_id),
                ).fetchone()
                conn.execute(
                    """
                    INSERT INTO candidate_dag_node_attempts
                        (id, dag_run_id, node_id, node_type, attempt, status, started_at)
                    VALUES (?, ?, ?, ?, ?, 'running', ?)
                    """,
                    (f"candidate-dag-attempt-{uuid4()}", dag_run_id, node_id, node_type, int(attempt_row["attempt"]), now),
                )
                conn.execute(
                    "UPDATE candidate_dag_runs SET current_node_id = ?, updated_at = ? WHERE id = ?",
                    (node_id, now, dag_run_id),
                )
            elif node_id and event_type in {"node_completed", "node_failed", "node_skipped"}:
                status = {"node_completed": "completed", "node_failed": "failed", "node_skipped": "skipped"}[event_type]
                conn.execute(
                    """
                    UPDATE candidate_dag_node_attempts
                    SET status = ?, duration_ms = ?, outputs_json = ?, error = ?, completed_at = ?
                    WHERE id = (
                        SELECT id FROM candidate_dag_node_attempts
                        WHERE dag_run_id = ? AND node_id = ?
                        ORDER BY attempt DESC LIMIT 1
                    )
                    """,
                    (
                        status,
                        int(event.get("duration_ms") or 0),
                        json.dumps(event.get("outputs") or {}, ensure_ascii=False, sort_keys=True),
                        str(event.get("error") or ""),
                        now,
                        dag_run_id,
                        node_id,
                    ),
                )
            conn.commit()
        except Exception:
            conn.rollback()
            raise

    def finish_dag_run(
        self,
        dag_run_id: str,
        *,
        status: str,
        final_state: dict[str, Any],
        failure_reason: str = "",
    ) -> dict[str, Any]:
        if status not in {"completed", "failed", "cancelled"}:
            raise CandidateGateError("DAG run must finish as completed, failed, or cancelled")
        now = self._now()
        conn = self._connection()
        conn.execute(
            """
            UPDATE candidate_dag_runs
            SET status = ?, final_state_json = ?, failure_reason = ?, completed_at = ?, updated_at = ?
            WHERE id = ?
            """,
            (status, json.dumps(final_state, ensure_ascii=False, sort_keys=True), failure_reason, now, now, dag_run_id),
        )
        conn.commit()
        return self.get_dag_run(dag_run_id)

    def _ensure_current_generation(self, candidate: ChapterCandidate) -> GenerationRun:
        """Reject delayed worker/review writes from a retired worldline epoch."""

        run = self.get_run(candidate.novel_id)
        if candidate.generation_epoch != run.generation_epoch:
            raise CandidateGateError("candidate belongs to a retired generation epoch")
        return run

    def create_streaming_candidate(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        title: str,
        outline_chain: dict[str, Any],
        llm_content: str = "",
    ) -> ChapterCandidate:
        run = self.get_run(novel_id)
        if run.state != GenerationRunState.RUNNING:
            raise CandidateGateError(f"generation run is {run.state.value}; cannot generate next candidate")
        if run.current_candidate_id:
            raise CandidateGateError("pending candidate blocks next chapter generation")
        if chapter_number != run.current_formal_chapter + 1:
            raise CandidateGateError("candidate chapter must be the immediate next chapter after the formal cursor")
        conn = self._connection()
        open_row = conn.execute(
            """
            SELECT id FROM chapter_candidates
            WHERE novel_id = ?
              AND status IN ('streaming', 'auditing', 'awaiting_review', 'committing', 'syncing', 'regenerating')
            LIMIT 1
            """,
            (novel_id,),
        ).fetchone()
        if open_row is not None:
            raise CandidateGateError("pending candidate blocks next chapter generation")
        candidate_id = f"candidate-{uuid4()}"
        outline_json = json.dumps(outline_chain, ensure_ascii=False, sort_keys=True)
        outline_digest = hashlib.sha256(outline_json.encode("utf-8")).hexdigest()
        content_revision = 1 if llm_content else 0
        now = self._now()
        try:
            conn.execute("BEGIN")
            conn.execute(
                """
                INSERT INTO chapter_candidates
                    (id, novel_id, chapter_number, title, generation_epoch, status,
                     outline_chain_json, outline_chain_digest, llm_content, content_revision,
                     created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, 'streaming', ?, ?, ?, ?, ?, ?)
                """,
                (
                    candidate_id,
                    novel_id,
                    chapter_number,
                    title,
                    run.generation_epoch,
                    outline_json,
                    outline_digest,
                    llm_content,
                    content_revision,
                    now,
                    now,
                ),
            )
            if content_revision:
                conn.execute(
                    """
                    INSERT INTO chapter_candidate_versions
                        (id, candidate_id, content_revision, content, source, created_at)
                    VALUES (?, ?, ?, ?, 'llm', ?)
                    """,
                    (f"candidate-version-{uuid4()}", candidate_id, content_revision, llm_content, now),
                )
            conn.execute(
                """
                UPDATE novel_generation_runs
                SET current_candidate_id = ?, current_candidate_chapter = ?, next_action = 'finish_candidate',
                    updated_at = ?
                WHERE novel_id = ?
                """,
                (candidate_id, chapter_number, now, novel_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return self.get_candidate(candidate_id)

    def set_generated_content(self, candidate_id: str, llm_content: str) -> ChapterCandidate:
        candidate = self.get_candidate(candidate_id)
        self._ensure_current_generation(candidate)
        if candidate.status not in {CandidateStatus.STREAMING, CandidateStatus.REGENERATING}:
            raise CandidateGateError("generated content can only be written while candidate is streaming")
        revision = candidate.content_revision + 1
        now = self._now()
        conn = self._connection()
        conn.execute(
            """
            UPDATE chapter_candidates
            SET llm_content = ?, author_content = NULL, content_revision = ?, updated_at = ?
            WHERE id = ?
            """,
            (llm_content, revision, now, candidate_id),
        )
        conn.execute(
            """
            INSERT INTO chapter_candidate_versions
                (id, candidate_id, content_revision, content, source, created_at)
            VALUES (?, ?, ?, ?, 'llm', ?)
            """,
            (f"candidate-version-{uuid4()}", candidate_id, revision, llm_content, now),
        )
        conn.commit()
        return self.get_candidate(candidate_id)

    def mark_auditing(self, candidate_id: str) -> ChapterCandidate:
        candidate = self.get_candidate(candidate_id)
        self._ensure_current_generation(candidate)
        if candidate.status not in {
            CandidateStatus.STREAMING,
            CandidateStatus.REGENERATING,
            CandidateStatus.AWAITING_REVIEW,
        }:
            raise CandidateGateError("only a streamed or stale-reviewed candidate can enter auditing")
        if not candidate.final_content.strip():
            raise CandidateGateError("candidate has no content to audit")
        now = self._now()
        conn = self._connection()
        conn.execute(
            "UPDATE chapter_candidates SET status = 'auditing', updated_at = ? WHERE id = ?",
            (now, candidate_id),
        )
        conn.execute(
            "UPDATE novel_generation_runs SET next_action = 'audit_candidate', updated_at = ? WHERE novel_id = ?",
            (now, candidate.novel_id),
        )
        conn.commit()
        return self.get_candidate(candidate_id)

    def finish_audit(
        self,
        candidate_id: str,
        *,
        audit: dict[str, Any],
        commit_plan: dict[str, Any],
        require_author_review: bool = False,
    ) -> ChapterCandidate:
        candidate = self.get_candidate(candidate_id)
        self._ensure_current_generation(candidate)
        if candidate.status != CandidateStatus.AUDITING:
            raise CandidateGateError("candidate must be auditing before an audit can finish")
        if not candidate.final_content.strip():
            raise CandidateGateError("candidate has no content to audit")
        run = self.get_run(candidate.novel_id)
        auto_commit = run.run_mode == RunMode.CONTINUOUS and not require_author_review
        next_status = CandidateStatus.COMMITTING if auto_commit else CandidateStatus.AWAITING_REVIEW
        next_run_state = GenerationRunState.RUNNING if auto_commit else GenerationRunState.WAITING_REVIEW
        next_action = "commit_candidate" if auto_commit else "author_review_candidate"
        now = self._now()
        conn = self._connection()
        conn.execute(
            """
            UPDATE chapter_candidates
            SET status = ?, audit_revision = ?, audit_json = ?,
                commit_plan_revision = commit_plan_revision + 1,
                commit_plan_content_revision = ?, commit_plan_json = ?,
                continue_after_commit = ?, failure_reason = '', updated_at = ?
            WHERE id = ?
            """,
            (
                next_status.value,
                candidate.content_revision,
                json.dumps(audit, ensure_ascii=False, sort_keys=True),
                candidate.content_revision,
                json.dumps(commit_plan, ensure_ascii=False, sort_keys=True),
                int(auto_commit),
                now,
                candidate_id,
            ),
        )
        conn.execute(
            """
            UPDATE novel_generation_runs
            SET state = ?, next_action = ?, updated_at = ?
            WHERE novel_id = ?
            """,
            (next_run_state.value, next_action, now, candidate.novel_id),
        )
        conn.commit()
        return self.get_candidate(candidate_id)

    def edit_content(self, candidate_id: str, content: str, *, feedback: str = "") -> ChapterCandidate:
        candidate = self.get_candidate(candidate_id)
        self._ensure_current_generation(candidate)
        if candidate.status not in {CandidateStatus.AWAITING_REVIEW, CandidateStatus.FAILED, CandidateStatus.STALE}:
            raise CandidateGateError("candidate content can only be edited during review or recovery")
        if not content.strip():
            raise CandidateGateError("candidate content cannot be empty")
        revision = candidate.content_revision + 1
        now = self._now()
        conn = self._connection()
        conn.execute(
            """
            UPDATE chapter_candidates
            SET author_content = ?, content_revision = ?, audit_revision = 0,
                commit_plan_content_revision = 0, feedback = ?, status = 'awaiting_review',
                failure_reason = '', updated_at = ?
            WHERE id = ?
            """,
            (content, revision, feedback, now, candidate_id),
        )
        conn.execute(
            """
            INSERT INTO chapter_candidate_versions
                (id, candidate_id, content_revision, content, source, feedback, created_at)
            VALUES (?, ?, ?, ?, 'author', ?, ?)
            """,
            (f"candidate-version-{uuid4()}", candidate_id, revision, content, feedback, now),
        )
        conn.execute(
            """
            UPDATE novel_generation_runs
            SET state = 'waiting_review', next_action = 're_audit_candidate', updated_at = ?
            WHERE novel_id = ?
            """,
            (now, candidate.novel_id),
        )
        conn.commit()
        return self.get_candidate(candidate_id)

    def request_regeneration(self, candidate_id: str, *, feedback: str = "") -> ChapterCandidate:
        """Discard the active review result and regenerate this one candidate.

        Historical text remains in ``chapter_candidate_versions``.  No formal
        chapter, fact, memory or next-chapter worker is touched by this path.
        """

        candidate = self.get_candidate(candidate_id)
        self._ensure_current_generation(candidate)
        if candidate.status not in {
            CandidateStatus.AWAITING_REVIEW,
            CandidateStatus.STALE,
            CandidateStatus.FAILED,
        }:
            raise CandidateGateError("candidate can only be regenerated from review, stale, or failed state")
        now = self._now()
        conn = self._connection()
        conn.execute(
            """
            UPDATE chapter_candidates
            SET status = 'regenerating', llm_content = '', author_content = NULL,
                audit_revision = 0, commit_plan_content_revision = 0,
                feedback = ?, failure_reason = '', updated_at = ?
            WHERE id = ?
            """,
            (feedback, now, candidate_id),
        )
        conn.execute(
            """
            UPDATE novel_generation_runs
            SET state = 'running', current_candidate_id = ?, current_candidate_chapter = ?,
                canonical_sync_status = 'ready', next_action = 'regenerate_candidate',
                last_error = '', updated_at = ?
            WHERE novel_id = ?
            """,
            (candidate_id, candidate.chapter_number, now, candidate.novel_id),
        )
        conn.commit()
        return self.get_candidate(candidate_id)

    def begin_dag_revision(self, candidate_id: str, *, feedback: str = "") -> ChapterCandidate:
        """Move an in-flight candidate to its next DAG-controlled revision.

        This is intentionally not the author-facing regeneration transition:
        the candidate has not yet entered review, and the bounded loop remains
        inside one server-owned generation request.
        """

        candidate = self.get_candidate(candidate_id)
        self._ensure_current_generation(candidate)
        if candidate.status not in {CandidateStatus.STREAMING, CandidateStatus.REGENERATING}:
            raise CandidateGateError("DAG revision can only continue an in-flight candidate")
        now = self._now()
        conn = self._connection()
        conn.execute(
            """
            UPDATE chapter_candidates
            SET status = 'regenerating', feedback = ?, audit_revision = 0,
                commit_plan_content_revision = 0, updated_at = ?
            WHERE id = ?
            """,
            (feedback, now, candidate_id),
        )
        conn.execute(
            """
            UPDATE novel_generation_runs
            SET state = 'running', next_action = 'retry_candidate_revision', updated_at = ?
            WHERE novel_id = ?
            """,
            (now, candidate.novel_id),
        )
        conn.commit()
        return self.get_candidate(candidate_id)

    def reject_and_stop(self, candidate_id: str) -> ChapterCandidate:
        """Reject an uncommitted candidate and end this run without side effects."""

        candidate = self.get_candidate(candidate_id)
        self._ensure_current_generation(candidate)
        if candidate.status not in {
            CandidateStatus.STREAMING,
            CandidateStatus.AUDITING,
            CandidateStatus.AWAITING_REVIEW,
            CandidateStatus.STALE,
            CandidateStatus.REGENERATING,
            CandidateStatus.FAILED,
        }:
            raise CandidateGateError("only an uncommitted candidate can be rejected")
        now = self._now()
        conn = self._connection()
        conn.execute(
            "UPDATE chapter_candidates SET status = 'rejected', updated_at = ? WHERE id = ?",
            (now, candidate_id),
        )
        conn.execute(
            """
            UPDATE novel_generation_runs
            SET state = 'stopped', current_candidate_id = NULL, current_candidate_chapter = NULL,
                next_action = 'idle', last_error = '', updated_at = ?
            WHERE novel_id = ?
            """,
            (now, candidate.novel_id),
        )
        conn.commit()
        return self.get_candidate(candidate_id)

    def stop_run(self, novel_id: str) -> GenerationRun:
        """Stop safely and retire an in-flight token task.

        A completed review candidate is preserved for the author.  A streaming
        or auditing one is retired by generation epoch so a late LLM response
        cannot write prose after the author has stopped the run.
        """

        run = self.get_run(novel_id)
        current = self.get_current_candidate(novel_id)
        now = self._now()
        conn = self._connection()
        try:
            conn.execute("BEGIN")
            if current is not None and current.status == CandidateStatus.SYNCING:
                conn.execute(
                    """
                    UPDATE novel_generation_runs
                    SET state = 'paused', next_action = 'finish_sync_then_pause', updated_at = ?
                    WHERE novel_id = ?
                    """,
                    (now, novel_id),
                )
            else:
                active = current is not None and current.status in {
                CandidateStatus.STREAMING,
                CandidateStatus.AUDITING,
                CandidateStatus.REGENERATING,
                CandidateStatus.COMMITTING,
                }
                if active and current is not None:
                    conn.execute(
                        """
                        UPDATE chapter_candidates
                        SET status = 'cancelled', failure_reason = 'stopped_by_author', updated_at = ?
                        WHERE id = ?
                        """,
                        (now, current.id),
                    )
                    conn.execute(
                        """
                        UPDATE novel_generation_runs
                        SET state = 'stopped', generation_epoch = generation_epoch + 1,
                            current_candidate_id = NULL, current_candidate_chapter = NULL,
                            next_action = 'idle', last_error = '', updated_at = ?
                        WHERE novel_id = ?
                        """,
                        (now, novel_id),
                    )
                else:
                    conn.execute(
                        """
                        UPDATE novel_generation_runs
                        SET state = 'stopped', next_action = 'idle', updated_at = ?
                        WHERE novel_id = ?
                        """,
                        (now, novel_id),
                    )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return self.get_run(novel_id)

    def fail_candidate(self, candidate_id: str, reason: str) -> ChapterCandidate:
        """Persist an expected worker failure and stop further token use."""

        candidate = self.get_candidate(candidate_id)
        self._ensure_current_generation(candidate)
        if candidate.status not in {
            CandidateStatus.STREAMING,
            CandidateStatus.REGENERATING,
            CandidateStatus.AUDITING,
            CandidateStatus.COMMITTING,
        }:
            raise CandidateGateError("only an active candidate can be marked failed")
        now = self._now()
        conn = self._connection()
        conn.execute(
            """
            UPDATE chapter_candidates SET status = 'failed', failure_reason = ?, updated_at = ?
            WHERE id = ?
            """,
            (reason, now, candidate_id),
        )
        conn.execute(
            """
            UPDATE novel_generation_runs
            SET state = 'error', next_action = 'regenerate_candidate', last_error = ?, updated_at = ?
            WHERE novel_id = ?
            """,
            (reason, now, candidate.novel_id),
        )
        conn.commit()
        return self.get_candidate(candidate_id)

    def fail_run(self, novel_id: str, reason: str) -> GenerationRun:
        """Record a pre-candidate plan/context failure without inferring writing."""

        self.get_run(novel_id)
        now = self._now()
        conn = self._connection()
        conn.execute(
            """
            UPDATE novel_generation_runs
            SET state = 'error', next_action = 'resolve_generation_error', last_error = ?, updated_at = ?
            WHERE novel_id = ?
            """,
            (reason, now, novel_id),
        )
        conn.commit()
        return self.get_run(novel_id)

    def complete_run(self, novel_id: str) -> GenerationRun:
        """Mark a target-complete run only after all formal sync is ready."""

        run = self.get_run(novel_id)
        if run.current_candidate_id or run.canonical_sync_status != "ready":
            raise CandidateGateError("cannot complete a run with a pending candidate or canonical sync")
        if run.current_formal_chapter < run.target_chapters:
            raise CandidateGateError("cannot complete before the target chapter count")
        now = self._now()
        conn = self._connection()
        conn.execute(
            """
            UPDATE novel_generation_runs
            SET state = 'completed', next_action = 'idle', last_error = '', updated_at = ?
            WHERE novel_id = ?
            """,
            (now, novel_id),
        )
        conn.commit()
        return self.get_run(novel_id)

    def update_commit_plan(self, candidate_id: str, commit_plan: dict[str, Any]) -> ChapterCandidate:
        candidate = self.get_candidate(candidate_id)
        self._ensure_current_generation(candidate)
        if candidate.status != CandidateStatus.AWAITING_REVIEW or not candidate.audit_is_current:
            raise CandidateGateError("a current audit is required before editing the commit plan")
        now = self._now()
        conn = self._connection()
        conn.execute(
            """
            UPDATE chapter_candidates
            SET commit_plan_json = ?, commit_plan_revision = commit_plan_revision + 1,
                commit_plan_content_revision = ?, updated_at = ?
            WHERE id = ?
            """,
            (json.dumps(commit_plan, ensure_ascii=False, sort_keys=True), candidate.content_revision, now, candidate_id),
        )
        conn.commit()
        return self.get_candidate(candidate_id)

    def approve_for_commit(self, candidate_id: str, *, continue_after_commit: bool) -> ChapterCandidate:
        candidate = self.get_candidate(candidate_id)
        self._ensure_current_generation(candidate)
        if candidate.status != CandidateStatus.AWAITING_REVIEW:
            raise CandidateGateError("candidate is not awaiting author review")
        if not candidate.audit_is_current or not candidate.commit_plan_is_current:
            raise CandidateGateError("re-audit is required before formal commit")
        now = self._now()
        conn = self._connection()
        conn.execute(
            """
            UPDATE chapter_candidates
            SET status = 'committing', continue_after_commit = ?, updated_at = ?
            WHERE id = ?
            """,
            (int(continue_after_commit), now, candidate_id),
        )
        conn.execute(
            """
            UPDATE novel_generation_runs
            SET state = 'running', next_action = 'commit_candidate', updated_at = ?
            WHERE novel_id = ?
            """,
            (now, candidate.novel_id),
        )
        conn.commit()
        return self.get_candidate(candidate_id)

    def commit_formal(self, candidate_id: str) -> ChapterCandidate:
        """Atomically write the final human-approved prose, then block on sync."""

        candidate = self.get_candidate(candidate_id)
        if candidate.status in {CandidateStatus.SYNCING, CandidateStatus.COMMITTED} and candidate.formal_chapter_id:
            return candidate
        if candidate.status != CandidateStatus.COMMITTING:
            raise CandidateGateError("candidate must be approved before formal commit")
        if not candidate.audit_is_current or not candidate.commit_plan_is_current:
            raise CandidateGateError("candidate audit or commit plan is stale")
        self._ensure_current_generation(candidate)
        content = candidate.final_content
        if not content.strip():
            raise CandidateGateError("cannot commit empty candidate prose")
        conn = self._connection()
        existing = conn.execute(
            "SELECT id FROM chapters WHERE novel_id = ? AND number = ?",
            (candidate.novel_id, candidate.chapter_number),
        ).fetchone()
        if existing is not None:
            raise CandidateGateError("formal chapter already exists for this chapter number")
        formal_chapter_id = f"chapter-{uuid4()}"
        content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
        now = self._now()
        try:
            conn.execute("BEGIN")
            conn.execute(
                """
                INSERT INTO chapters
                    (id, novel_id, number, title, content, content_sha256, content_revision, outline, status, created_at, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'completed', ?, ?)
                """,
                (
                    formal_chapter_id,
                    candidate.novel_id,
                    candidate.chapter_number,
                    candidate.title,
                    content,
                    content_sha256,
                    candidate.content_revision,
                    json.dumps(candidate.commit_plan, ensure_ascii=False, sort_keys=True),
                    now,
                    now,
                ),
            )
            conn.execute(
                """
                INSERT INTO chapter_candidate_formal_commits
                    (candidate_id, novel_id, chapter_number, chapter_id, sync_status, committed_at)
                VALUES (?, ?, ?, ?, 'syncing', ?)
                """,
                (candidate_id, candidate.novel_id, candidate.chapter_number, formal_chapter_id, now),
            )
            conn.execute(
                """
                UPDATE chapter_candidates
                SET status = 'syncing', formal_chapter_id = ?, updated_at = ?
                WHERE id = ?
                """,
                (formal_chapter_id, now, candidate_id),
            )
            conn.execute(
                """
                UPDATE novel_generation_runs
                SET canonical_sync_status = 'syncing', next_action = 'sync_candidate', updated_at = ?
                WHERE novel_id = ?
                """,
                (now, candidate.novel_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return self.get_candidate(candidate_id)

    def mark_sync_succeeded(self, candidate_id: str) -> ChapterCandidate:
        candidate = self.get_candidate(candidate_id)
        if candidate.status != CandidateStatus.SYNCING or not candidate.formal_chapter_id:
            raise CandidateGateError("candidate has no formal chapter awaiting sync")
        run = self._ensure_current_generation(candidate)
        pause_after_sync = run.next_action == "finish_sync_then_pause"
        next_state = (
            GenerationRunState.PAUSED
            if pause_after_sync or not candidate.continue_after_commit
            else GenerationRunState.RUNNING
        )
        next_action = "resume_generation" if next_state == GenerationRunState.PAUSED else "generate_candidate"
        now = self._now()
        conn = self._connection()
        try:
            conn.execute("BEGIN")
            conn.execute(
                """
                UPDATE chapter_candidate_formal_commits
                SET sync_status = 'ready', failure_reason = '', synced_at = ?
                WHERE candidate_id = ?
                """,
                (now, candidate_id),
            )
            conn.execute(
                "UPDATE chapter_candidates SET status = 'committed', updated_at = ? WHERE id = ?",
                (now, candidate_id),
            )
            conn.execute(
                """
                UPDATE novel_generation_runs
                SET state = ?, current_formal_chapter = ?, current_candidate_id = NULL,
                    current_candidate_chapter = NULL, canonical_sync_status = 'ready',
                    next_action = ?, last_error = '', updated_at = ?
                WHERE novel_id = ?
                """,
                (next_state.value, candidate.chapter_number, next_action, now, candidate.novel_id),
            )
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        return self.get_candidate(candidate_id)

    def begin_sync_retry(self, candidate_id: str) -> ChapterCandidate:
        """Retry canonical aftermath for an already-committed formal chapter."""

        candidate = self.get_candidate(candidate_id)
        self._ensure_current_generation(candidate)
        if candidate.status != CandidateStatus.FAILED or not candidate.formal_chapter_id:
            raise CandidateGateError("only a formally committed candidate with failed sync can retry")
        now = self._now()
        conn = self._connection()
        conn.execute(
            """
            UPDATE chapter_candidate_formal_commits
            SET sync_status = 'syncing', failure_reason = '' WHERE candidate_id = ?
            """,
            (candidate_id,),
        )
        conn.execute(
            """
            UPDATE chapter_candidates
            SET status = 'syncing', failure_reason = '', updated_at = ? WHERE id = ?
            """,
            (now, candidate_id),
        )
        conn.execute(
            """
            UPDATE novel_generation_runs
            SET state = 'paused', canonical_sync_status = 'syncing', next_action = 'retry_sync',
                last_error = '', updated_at = ?
            WHERE novel_id = ?
            """,
            (now, candidate.novel_id),
        )
        conn.commit()
        return self.get_candidate(candidate_id)

    def mark_sync_failed(self, candidate_id: str, reason: str) -> ChapterCandidate:
        candidate = self.get_candidate(candidate_id)
        self._ensure_current_generation(candidate)
        if candidate.status != CandidateStatus.SYNCING:
            raise CandidateGateError("candidate is not awaiting canonical sync")
        now = self._now()
        conn = self._connection()
        conn.execute(
            """
            UPDATE chapter_candidate_formal_commits
            SET sync_status = 'failed', failure_reason = ? WHERE candidate_id = ?
            """,
            (reason, candidate_id),
        )
        conn.execute(
            """
            UPDATE chapter_candidates
            SET status = 'failed', failure_reason = ?, updated_at = ? WHERE id = ?
            """,
            (reason, now, candidate_id),
        )
        conn.execute(
            """
            UPDATE novel_generation_runs
            SET state = 'paused', canonical_sync_status = 'failed', next_action = 'retry_sync',
                last_error = ?, updated_at = ?
            WHERE novel_id = ?
            """,
            (reason, now, candidate.novel_id),
        )
        conn.commit()
        return self.get_candidate(candidate_id)

    def stale_candidates_for_outline_contract(
        self, novel_id: str, contract_id: str, active_digest: str
    ) -> int:
        """Invalidate open candidates generated against an old plan revision."""

        conn = self._connection()
        rows = conn.execute(
            """
            SELECT * FROM chapter_candidates
            WHERE novel_id = ?
              AND status IN ('streaming', 'auditing', 'awaiting_review', 'committing', 'regenerating')
            """,
            (novel_id,),
        ).fetchall()
        stale_ids: list[str] = []
        for row in rows:
            try:
                chain = json.loads(row["outline_chain_json"] or "{}")
            except (TypeError, ValueError):
                chain = {}
            for item in chain.values() if isinstance(chain, dict) else ():
                if isinstance(item, dict) and str(item.get("contract_id")) == contract_id:
                    if str(item.get("digest") or "") != active_digest:
                        stale_ids.append(str(row["id"]))
                    break
        if not stale_ids:
            return 0
        now = self._now()
        placeholders = ",".join("?" for _ in stale_ids)
        conn.execute(
            f"UPDATE chapter_candidates SET status = 'stale', updated_at = ? WHERE id IN ({placeholders})",
            (now, *stale_ids),
        )
        conn.execute(
            """
            UPDATE novel_generation_runs
            SET state = 'paused', next_action = 'regenerate_stale_candidate',
                last_error = 'outline_revision_changed', updated_at = ?
            WHERE novel_id = ? AND current_candidate_id IN ({})
            """.format(placeholders),
            (now, novel_id, *stale_ids),
        )
        conn.commit()
        return len(stale_ids)
