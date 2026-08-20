"""SQLite persistence for immutable outline continuity review records."""

from __future__ import annotations

from datetime import datetime
import json
import sqlite3
from typing import Any, Callable, Mapping, Optional, Union
from uuid import uuid4

from domain.structure.outline_continuity import (
    ContinuityDecision,
    ContinuityReviewReport,
    ContinuityReviewScope,
)


class OutlineContinuityReviewRepository:
    """Store review attempts separately from immutable outline content versions."""

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
    def _json(value: Any) -> str:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

    @staticmethod
    def _json_object(value: str) -> dict[str, Any]:
        try:
            parsed = json.loads(value or "{}")
        except json.JSONDecodeError:
            return {}
        return parsed if isinstance(parsed, dict) else {}

    @staticmethod
    def _json_list(value: str) -> list[Any]:
        try:
            parsed = json.loads(value or "[]")
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []

    def _run_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        values = dict(row)
        return {
            "id": str(values["id"]),
            "novel_id": str(values["novel_id"]),
            "plan_revision_id": str(values["plan_revision_id"]),
            "scope_parent_logical_node_id": str(
                values["scope_parent_logical_node_id"]
            ),
            "level": str(values["level"]),
            "plan_digest": str(values["plan_digest"]),
            "scope_fingerprint": str(values["scope_fingerprint"]),
            "context_digest": str(values["context_digest"] or ""),
            "prompt_node_version_id": str(values["prompt_node_version_id"] or ""),
            "prompt_hash": str(values["prompt_hash"] or ""),
            "schema_version": str(values["schema_version"] or ""),
            "ruleset_version": str(values["ruleset_version"] or ""),
            "model": str(values["model"] or ""),
            "state": str(values["state"]),
            "decision": str(values["decision"]),
            "confidence": values["confidence"],
            "report": self._json_object(str(values["report_json"] or "{}")),
            "raw_response": str(values["raw_response"] or ""),
            "error": str(values["error"] or ""),
            "created_at": str(values["created_at"] or ""),
            "completed_at": values["completed_at"],
        }

    def _override_from_row(self, row: sqlite3.Row) -> dict[str, Any]:
        values = dict(row)
        return {
            "id": str(values["id"]),
            "novel_id": str(values["novel_id"]),
            "plan_revision_id": str(values["plan_revision_id"]),
            "plan_digest": str(values["plan_digest"]),
            "action": str(values["action"]),
            "idempotency_key": str(values["idempotency_key"] or ""),
            "actor": str(values["actor"]),
            "reason": str(values["reason"]),
            "scope_fingerprints": self._json_list(
                str(values["scope_fingerprints_json"] or "[]")
            ),
            "review_ids": self._json_list(str(values["review_ids_json"] or "[]")),
            "created_at": str(values["created_at"] or ""),
        }

    def _write(
        self,
        operation: Callable[[sqlite3.Connection], dict[str, Any]],
        *,
        _connection: Optional[sqlite3.Connection] = None,
    ) -> dict[str, Any]:
        if _connection is not None:
            return operation(_connection)

        conn = self._connection()
        if conn.in_transaction:
            raise RuntimeError("continuity review write requires a caller-owned connection")
        conn.execute("BEGIN IMMEDIATE")
        try:
            result = operation(conn)
            conn.commit()
            return result
        except BaseException:
            conn.rollback()
            raise

    def begin(
        self,
        *,
        novel_id: str,
        plan_revision_id: str,
        scope: ContinuityReviewScope,
        plan_digest: str,
        scope_fingerprint: str,
        context_digest: str = "",
        prompt_node_version_id: str = "",
        prompt_hash: str = "",
        schema_version: str = "",
        ruleset_version: str = "",
        model: str = "",
        force: bool = False,
        _connection: Optional[sqlite3.Connection] = None,
    ) -> dict[str, Any]:
        """Create a run or return the reusable running/succeeded history row."""

        if not scope_fingerprint:
            raise ValueError("scope_fingerprint is required")

        def operation(conn: sqlite3.Connection) -> dict[str, Any]:
            running = conn.execute(
                """
                SELECT * FROM outline_continuity_review_runs
                WHERE plan_revision_id = ? AND scope_fingerprint = ?
                  AND state = 'running'
                ORDER BY created_at DESC, id DESC LIMIT 1
                """,
                (plan_revision_id, scope_fingerprint),
            ).fetchone()
            if running is not None:
                if force:
                    raise ValueError("continuity review run is already running")
                return self._run_from_row(running)

            if not force:
                succeeded = conn.execute(
                    """
                    SELECT * FROM outline_continuity_review_runs
                    WHERE plan_revision_id = ? AND scope_fingerprint = ?
                      AND state = 'succeeded'
                    ORDER BY completed_at DESC, created_at DESC, id DESC LIMIT 1
                    """,
                    (plan_revision_id, scope_fingerprint),
                ).fetchone()
                if succeeded is not None:
                    return self._run_from_row(succeeded)

            run_id = f"outline-continuity-review-{uuid4()}"
            now = self._now()
            try:
                conn.execute(
                    """
                    INSERT INTO outline_continuity_review_runs
                        (id, novel_id, plan_revision_id, scope_parent_logical_node_id,
                         level, plan_digest, scope_fingerprint, context_digest,
                         prompt_node_version_id, prompt_hash, schema_version,
                         ruleset_version, model, state, decision, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'running',
                            'unavailable', ?)
                    """,
                    (
                        run_id,
                        novel_id,
                        plan_revision_id,
                        scope.parent_logical_node_id,
                        scope.level,
                        plan_digest,
                        scope_fingerprint,
                        context_digest,
                        prompt_node_version_id,
                        prompt_hash,
                        schema_version,
                        ruleset_version,
                        model,
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                running = conn.execute(
                    """
                    SELECT * FROM outline_continuity_review_runs
                    WHERE plan_revision_id = ? AND scope_fingerprint = ?
                      AND state = 'running'
                    ORDER BY created_at DESC, id DESC LIMIT 1
                    """,
                    (plan_revision_id, scope_fingerprint),
                ).fetchone()
                if running is not None and not force:
                    return self._run_from_row(running)
                raise
            return self._run_from_row(
                conn.execute(
                    "SELECT * FROM outline_continuity_review_runs WHERE id = ?", (run_id,)
                ).fetchone()
            )

        return self._write(operation, _connection=_connection)

    def begin_in_transaction(
        self, connection: sqlite3.Connection, **kwargs: Any
    ) -> dict[str, Any]:
        return self.begin(_connection=connection, **kwargs)

    def get(
        self, review_id: str, *, _connection: Optional[sqlite3.Connection] = None
    ) -> dict[str, Any]:
        conn = _connection or self._connection()
        row = conn.execute(
            "SELECT * FROM outline_continuity_review_runs WHERE id = ?", (review_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"continuity review run not found: {review_id}")
        return self._run_from_row(row)

    def get_in_transaction(
        self, connection: sqlite3.Connection, review_id: str
    ) -> dict[str, Any]:
        return self.get(review_id, _connection=connection)

    def latest(
        self,
        *,
        plan_revision_id: str,
        scope_parent_logical_node_id: Optional[str] = None,
        _connection: Optional[sqlite3.Connection] = None,
    ) -> Optional[dict[str, Any]]:
        conn = _connection or self._connection()
        filters = ["plan_revision_id = ?"]
        params: list[Any] = [plan_revision_id]
        if scope_parent_logical_node_id is not None:
            filters.append("scope_parent_logical_node_id = ?")
            params.append(scope_parent_logical_node_id)
        row = conn.execute(
            "SELECT * FROM outline_continuity_review_runs WHERE "
            + " AND ".join(filters)
            + " ORDER BY created_at DESC, id DESC LIMIT 1",
            tuple(params),
        ).fetchone()
        return None if row is None else self._run_from_row(row)

    def latest_in_transaction(
        self, connection: sqlite3.Connection, **kwargs: Any
    ) -> Optional[dict[str, Any]]:
        return self.latest(_connection=connection, **kwargs)

    def current(
        self,
        *,
        plan_revision_id: str,
        plan_digest: str,
        scope_fingerprint: str,
        _connection: Optional[sqlite3.Connection] = None,
    ) -> Optional[dict[str, Any]]:
        """Return a completed report when its semantic scope identity still matches.

        ``plan_digest`` is retained in the public signature for callers that
        carry the plan CAS value, but it is intentionally not part of report
        currentness.  A Working edit outside this scope must not invalidate a
        report whose range, ancestors, evidence, rules, and model fingerprint
        are unchanged.
        """

        conn = _connection or self._connection()
        row = conn.execute(
            """
            SELECT * FROM outline_continuity_review_runs
            WHERE plan_revision_id = ? AND scope_fingerprint = ?
              AND state = 'succeeded'
            ORDER BY completed_at DESC, created_at DESC, id DESC LIMIT 1
            """,
            (plan_revision_id, scope_fingerprint),
        ).fetchone()
        return None if row is None else self._run_from_row(row)

    def current_in_transaction(
        self, connection: sqlite3.Connection, **kwargs: Any
    ) -> Optional[dict[str, Any]]:
        return self.current(_connection=connection, **kwargs)

    def complete(
        self,
        review_id: str,
        *,
        report: ContinuityReviewReport,
        raw_response: str = "",
        _connection: Optional[sqlite3.Connection] = None,
    ) -> dict[str, Any]:
        """Finalize exactly one running record without changing its fingerprint."""

        def operation(conn: sqlite3.Connection) -> dict[str, Any]:
            existing = self.get(review_id, _connection=conn)
            if existing["state"] != "running":
                return existing
            report_payload = report.to_dict()
            report_fingerprint = str(report_payload.get("scope_fingerprint") or "")
            if report_fingerprint and report_fingerprint != existing["scope_fingerprint"]:
                raise ValueError("continuity report scope fingerprint does not match run")
            report_payload["scope_fingerprint"] = existing["scope_fingerprint"]
            now = self._now()
            updated = conn.execute(
                """
                UPDATE outline_continuity_review_runs
                SET state = 'succeeded', decision = ?, confidence = ?, report_json = ?,
                    raw_response = ?, error = '', completed_at = ?
                WHERE id = ? AND state = 'running'
                """,
                (
                    report.decision.value,
                    report.confidence,
                    self._json(report_payload),
                    raw_response,
                    now,
                    review_id,
                ),
            )
            if updated.rowcount != 1:
                raise RuntimeError("continuity review run changed during completion")
            return self.get(review_id, _connection=conn)

        return self._write(operation, _connection=_connection)

    def complete_in_transaction(
        self, connection: sqlite3.Connection, review_id: str, **kwargs: Any
    ) -> dict[str, Any]:
        return self.complete(review_id, _connection=connection, **kwargs)

    def fail(
        self,
        review_id: str,
        *,
        error: str,
        raw_response: str = "",
        decision: ContinuityDecision = ContinuityDecision.UNAVAILABLE,
        _connection: Optional[sqlite3.Connection] = None,
    ) -> dict[str, Any]:
        """Persist a terminal unavailable/error result without raising an LLM concern."""

        if isinstance(decision, str):
            decision = ContinuityDecision(decision)

        def operation(conn: sqlite3.Connection) -> dict[str, Any]:
            existing = self.get(review_id, _connection=conn)
            if existing["state"] != "running":
                return existing
            now = self._now()
            if decision is ContinuityDecision.UNAVAILABLE:
                report = ContinuityReviewReport(
                    decision=ContinuityDecision.UNAVAILABLE,
                    scope_fingerprint=existing["scope_fingerprint"],
                    model=existing["model"],
                    schema_version=existing["schema_version"],
                    ruleset_version=existing["ruleset_version"],
                    error=error,
                ).to_dict()
                updated = conn.execute(
                    """
                    UPDATE outline_continuity_review_runs
                    SET state = 'succeeded', decision = 'unavailable', report_json = ?,
                        raw_response = ?, error = ?, completed_at = ?
                    WHERE id = ? AND state = 'running'
                    """,
                    (self._json(report), raw_response, error, now, review_id),
                )
            else:
                updated = conn.execute(
                    """
                    UPDATE outline_continuity_review_runs
                    SET state = 'failed', decision = ?, raw_response = ?, error = ?,
                        completed_at = ?
                    WHERE id = ? AND state = 'running'
                    """,
                    (decision.value, raw_response, error, now, review_id),
                )
            if updated.rowcount != 1:
                raise RuntimeError("continuity review run changed during failure")
            return self.get(review_id, _connection=conn)

        return self._write(operation, _connection=_connection)

    def fail_in_transaction(
        self, connection: sqlite3.Connection, review_id: str, **kwargs: Any
    ) -> dict[str, Any]:
        return self.fail(review_id, _connection=connection, **kwargs)

    def acknowledge(
        self,
        *,
        novel_id: str,
        plan_revision_id: str,
        plan_digest: str,
        action: str,
        idempotency_key: str,
        actor: str,
        reason: str,
        require_reason: bool = True,
        scope_fingerprints: tuple[str, ...] | list[str],
        review_ids: tuple[str, ...] | list[str],
        _connection: Optional[sqlite3.Connection] = None,
    ) -> dict[str, Any]:
        """Create an immutable author acknowledgement bound to exact audit inputs."""

        actor = actor.strip()
        reason = reason.strip()
        if not actor:
            raise ValueError("actor is required for continuity acknowledgement")
        if require_reason and not reason:
            raise ValueError("reason is required for continuity acknowledgement")
        if not action.strip():
            raise ValueError("action is required for continuity acknowledgement")
        scope_fingerprints = tuple(str(value) for value in scope_fingerprints)
        review_ids = tuple(str(value) for value in review_ids)

        def operation(conn: sqlite3.Connection) -> dict[str, Any]:
            if idempotency_key:
                existing = conn.execute(
                    """
                    SELECT * FROM outline_continuity_review_overrides
                    WHERE novel_id = ? AND action = ? AND idempotency_key = ?
                    """,
                    (novel_id, action, idempotency_key),
                ).fetchone()
                if existing is not None:
                    return self._override_from_row(existing)

            receipt_id = f"outline-continuity-override-{uuid4()}"
            now = self._now()
            try:
                conn.execute(
                    """
                    INSERT INTO outline_continuity_review_overrides
                        (id, novel_id, plan_revision_id, plan_digest, action,
                         idempotency_key, actor, reason, scope_fingerprints_json,
                         review_ids_json, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        receipt_id,
                        novel_id,
                        plan_revision_id,
                        plan_digest,
                        action,
                        idempotency_key,
                        actor,
                        reason,
                        self._json(scope_fingerprints),
                        self._json(review_ids),
                        now,
                    ),
                )
            except sqlite3.IntegrityError:
                if not idempotency_key:
                    raise
                existing = conn.execute(
                    """
                    SELECT * FROM outline_continuity_review_overrides
                    WHERE novel_id = ? AND action = ? AND idempotency_key = ?
                    """,
                    (novel_id, action, idempotency_key),
                ).fetchone()
                if existing is None:
                    raise
                return self._override_from_row(existing)
            return self._override_from_row(
                conn.execute(
                    "SELECT * FROM outline_continuity_review_overrides WHERE id = ?",
                    (receipt_id,),
                ).fetchone()
            )

        return self._write(operation, _connection=_connection)

    def acknowledge_in_transaction(
        self, connection: sqlite3.Connection, **kwargs: Any
    ) -> dict[str, Any]:
        return self.acknowledge(_connection=connection, **kwargs)
