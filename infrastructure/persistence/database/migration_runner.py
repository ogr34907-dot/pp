"""SQLite SQL migration runner."""
from __future__ import annotations

import logging
import sqlite3
import time
from pathlib import Path
from collections.abc import Iterator

from infrastructure.persistence.database.sqlite_retry import (
    get_sqlite_retry_settings,
    is_sqlite_lock_error,
    migration_retry_delay,
)

logger = logging.getLogger(__name__)


_MIGRATION_DEPENDENCIES = {
    "add_macro_diagnosis_context_patch.sql": (
        "add_macro_diagnosis_results.sql",
    ),
}


def ordered_migration_paths(migrations_dir: Path) -> list[Path]:
    """Return published migrations in lexical order with explicit prerequisites."""
    paths = {path.name: path for path in migrations_dir.glob("*.sql")}
    ordered: list[Path] = []
    visiting: set[str] = set()
    visited: set[str] = set()

    def visit(name: str) -> None:
        if name in visited:
            return
        if name in visiting:
            raise RuntimeError(f"Migration dependency cycle detected at {name}")
        path = paths.get(name)
        if path is None:
            return
        visiting.add(name)
        for dependency in _MIGRATION_DEPENDENCIES.get(name, ()):
            visit(dependency)
        visiting.remove(name)
        visited.add(name)
        ordered.append(path)

    for name in sorted(paths):
        visit(name)
    return ordered


def apply_migration_files(conn: sqlite3.Connection, migrations_dir: Path) -> None:
    """Apply SQL migrations idempotently using the existing tracking table."""
    retry_settings = get_sqlite_retry_settings()
    max_retries = retry_settings.migration_max_retries
    for attempt in range(max_retries):
        try:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS migrations_applied (
                    migration_file TEXT PRIMARY KEY,
                    applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
                """
            )
            conn.commit()
            break
        except sqlite3.OperationalError as exc:
            if is_sqlite_lock_error(exc) and attempt < max_retries - 1:
                logger.warning(
                    "Database locked, retrying... (attempt %s/%s)",
                    attempt + 1,
                    max_retries,
                )
                time.sleep(migration_retry_delay(attempt, retry_settings))
                continue
            if "already exists" in str(exc):
                break
            logger.warning(
                "Cannot create migrations_applied table: %s, using legacy mode",
                exc,
            )
            apply_migration_files_legacy(conn, migrations_dir)
            return

    applied = set()
    try:
        cursor = conn.execute("SELECT migration_file FROM migrations_applied")
        applied = {row[0] for row in cursor.fetchall()}
    except Exception:
        pass

    if not migrations_dir.is_dir():
        logger.warning(
            "未找到迁移目录（将仅依赖 schema.sql 与代码内补丁）: %s",
            migrations_dir,
        )
        return

    new_migrations = 0
    for migration_path in ordered_migration_paths(migrations_dir):
        migration_file = migration_path.name
        if migration_file in applied:
            continue

        try:
            migration_sql = migration_path.read_text(encoding="utf-8")
            if not _apply_one_migration(conn, migration_file, migration_sql):
                logger.warning(
                    "Migration %s failed; remaining migrations will be retried after it is resolved",
                    migration_file,
                )
                break
            logger.info("Applied migration: %s", migration_file)
            new_migrations += 1
        except OSError as exc:
            logger.warning("Failed to read migration %s: %s", migration_file, exc)
            break
        except Exception as exc:
            # Keep startup fail-closed: a failed migration is retried on the
            # next startup instead of being recorded as complete.
            logger.warning("Migration %s failed: %s", migration_file, exc)
            break

    if new_migrations == 0 and applied:
        logger.debug("All %d migrations already applied, skipped", len(applied))


def apply_migration_files_legacy(
    conn: sqlite3.Connection, migrations_dir: Path
) -> None:
    """Legacy migration mode without a tracking table."""
    if not migrations_dir.is_dir():
        logger.warning(
            "未找到迁移目录（将仅依赖 schema.sql 与代码内补丁）: %s",
            migrations_dir,
        )
        return

    for migration_path in ordered_migration_paths(migrations_dir):
        migration_file = migration_path.name
        try:
            migration_sql = migration_path.read_text(encoding="utf-8")
            if not _apply_one_migration(conn, migration_file, migration_sql, track=False):
                logger.warning(
                    "Migration %s failed; remaining legacy migrations will not run",
                    migration_file,
                )
                break
            logger.info("Applied migration: %s", migration_file)
        except OSError as exc:
            logger.warning("Failed to read migration %s: %s", migration_file, exc)
            break
        except Exception as exc:
            logger.warning("Failed to apply migration %s: %s", migration_file, exc)
            break


def _apply_one_migration(
    conn: sqlite3.Connection,
    migration_file: str,
    migration_sql: str,
    *,
    track: bool = True,
) -> bool:
    """Apply one migration atomically, tolerating only duplicate definitions."""
    savepoint = "migration_" + "".join(
        char if char.isalnum() else "_" for char in migration_file
    )
    conn.execute(f"SAVEPOINT {savepoint}")
    try:
        for statement in _iter_sql_statements(migration_sql):
            if _is_diagnostic_statement(statement):
                logger.debug("Skipping migration diagnostic statement in %s", migration_file)
                continue
            try:
                conn.execute(statement)
            except sqlite3.OperationalError as exc:
                if _is_duplicate_definition_error(exc):
                    logger.debug("Skipping duplicate statement in %s: %s", migration_file, exc)
                    continue
                raise
        if track:
            conn.execute(
                "INSERT OR IGNORE INTO migrations_applied (migration_file) VALUES (?)",
                (migration_file,),
            )
        conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        if track:
            conn.commit()
        return True
    except Exception:
        try:
            conn.execute(f"ROLLBACK TO SAVEPOINT {savepoint}")
            conn.execute(f"RELEASE SAVEPOINT {savepoint}")
        except sqlite3.Error:
            logger.exception("Could not roll back migration savepoint %s", savepoint)
        if track:
            conn.commit()
        return False


def _iter_sql_statements(sql: str) -> Iterator[str]:
    """Yield complete SQLite statements, including multi-line triggers."""
    buffer: list[str] = []
    for char in sql:
        buffer.append(char)
        candidate = "".join(buffer)
        if sqlite3.complete_statement(candidate):
            if candidate.strip().rstrip(";").strip():
                yield candidate
            buffer.clear()
    if _strip_sql_comments("".join(buffer)).strip():
        raise sqlite3.OperationalError("incomplete migration statement")


def _strip_sql_comments(sql: str) -> str:
    """Remove SQLite line/block comments for trailing-buffer validation."""
    result: list[str] = []
    index = 0
    quote = ""
    while index < len(sql):
        char = sql[index]
        next_char = sql[index + 1] if index + 1 < len(sql) else ""
        if quote:
            result.append(char)
            if char == quote:
                if next_char == quote:
                    result.append(next_char)
                    index += 1
                else:
                    quote = ""
            index += 1
            continue
        if char in {"'", '"'}:
            quote = char
            result.append(char)
            index += 1
            continue
        if char == "-" and next_char == "-":
            newline = sql.find("\n", index + 2)
            index = len(sql) if newline < 0 else newline + 1
            continue
        if char == "/" and next_char == "*":
            close = sql.find("*/", index + 2)
            index = len(sql) if close < 0 else close + 2
            continue
        result.append(char)
        index += 1
    return "".join(result)


def _is_duplicate_definition_error(exc: sqlite3.OperationalError) -> bool:
    message = str(exc).lower()
    return "already exists" in message or "duplicate column" in message


def _is_diagnostic_statement(statement: str) -> bool:
    """Migration diagnostics may contain unbound query-plan placeholders."""
    normalized = _strip_sql_comments(statement).lstrip()
    return normalized.upper().startswith("EXPLAIN QUERY PLAN")
