import sqlite3
import shutil
from pathlib import Path

import pytest

from infrastructure.persistence.database.migration_runner import apply_migration_files
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.outline_contract_repository import (
    OutlineContractRepository,
    OutlineGateError,
)


def test_apply_migration_files_is_idempotent(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_create_sample.sql").write_text(
        "CREATE TABLE sample (id TEXT PRIMARY KEY);\n",
        encoding="utf-8",
    )

    conn = sqlite3.connect(":memory:")
    try:
        apply_migration_files(conn, migrations)
        apply_migration_files(conn, migrations)

        rows = conn.execute("SELECT migration_file FROM migrations_applied").fetchall()
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='sample'"
        ).fetchone()
    finally:
        conn.close()

    assert rows == [("001_create_sample.sql",)]
    assert table == ("sample",)


def test_apply_migration_files_accepts_missing_directory(tmp_path):
    conn = sqlite3.connect(":memory:")
    try:
        apply_migration_files(conn, tmp_path / "missing")
    finally:
        conn.close()


def test_apply_migration_files_orders_macro_diagnosis_table_before_its_patch(tmp_path):
    """DB-003: a new database must finish dependent migrations on first open."""
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "add_macro_diagnosis_context_patch.sql").write_text(
        "ALTER TABLE macro_diagnosis_results ADD COLUMN context_patch TEXT;\n"
        "ALTER TABLE macro_diagnosis_results ADD COLUMN total_words_at_run INTEGER DEFAULT 0;\n",
        encoding="utf-8",
    )
    (migrations / "add_macro_diagnosis_results.sql").write_text(
        "CREATE TABLE macro_diagnosis_results (id TEXT PRIMARY KEY);\n",
        encoding="utf-8",
    )

    conn = sqlite3.connect(":memory:")
    try:
        apply_migration_files(conn, migrations)
        columns = {
            row[1] for row in conn.execute("PRAGMA table_info(macro_diagnosis_results)")
        }
    finally:
        conn.close()

    assert {"context_patch", "total_words_at_run"} <= columns


def test_apply_migration_files_continues_after_a_known_duplicate_statement(tmp_path):
    """A partially upgraded database still receives later statements."""
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_partial_upgrade.sql").write_text(
        "CREATE TABLE existing_table (id INTEGER);\n"
        "CREATE TABLE later_table (id INTEGER);\n",
        encoding="utf-8",
    )

    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("CREATE TABLE existing_table (id INTEGER)")
        apply_migration_files(conn, migrations)
        marked = conn.execute(
            "SELECT migration_file FROM migrations_applied"
        ).fetchall()
        later = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'later_table'"
        ).fetchone()
    finally:
        conn.close()

    assert marked == [("001_partial_upgrade.sql",)]
    assert later == ("later_table",)


def test_apply_migration_files_does_not_mark_a_migration_after_an_unrecoverable_error(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_bad_upgrade.sql").write_text(
        "SELECT unsupported_migration_function();\n"
        "CREATE TABLE later_table (id INTEGER);\n",
        encoding="utf-8",
    )

    conn = sqlite3.connect(":memory:")
    try:
        apply_migration_files(conn, migrations)
        marked = conn.execute(
            "SELECT migration_file FROM migrations_applied"
        ).fetchall()
        later = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'later_table'"
        ).fetchone()
    finally:
        conn.close()

    assert marked == []
    assert later is None


def test_apply_migration_files_stops_after_an_unrecoverable_migration(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_bad_upgrade.sql").write_text(
        "SELECT unsupported_migration_function();\n",
        encoding="utf-8",
    )
    (migrations / "002_must_not_run.sql").write_text(
        "CREATE TABLE later_table (id INTEGER);\n",
        encoding="utf-8",
    )

    conn = sqlite3.connect(":memory:")
    try:
        apply_migration_files(conn, migrations)
        later = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'later_table'"
        ).fetchone()
    finally:
        conn.close()

    assert later is None


def test_apply_migration_files_stops_after_a_migration_file_cannot_be_read(tmp_path, monkeypatch):
    """A missing migration body must block later migrations rather than skip its schema."""
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    blocked = migrations / "001_unreadable.sql"
    blocked.write_text("CREATE TABLE blocked_table (id INTEGER);\n", encoding="utf-8")
    (migrations / "002_must_not_run.sql").write_text(
        "CREATE TABLE later_table (id INTEGER);\n",
        encoding="utf-8",
    )
    read_text = Path.read_text

    def raise_for_blocked_file(path, *args, **kwargs):
        if path == blocked:
            raise OSError("simulated read failure")
        return read_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", raise_for_blocked_file)
    conn = sqlite3.connect(":memory:")
    try:
        apply_migration_files(conn, migrations)
        later = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'later_table'"
        ).fetchone()
    finally:
        conn.close()

    assert later is None


def test_apply_migration_files_keeps_multiline_trigger_as_one_statement(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_trigger.sql").write_text(
        "CREATE TABLE sample (id INTEGER PRIMARY KEY, value TEXT);\n"
        "CREATE TABLE audit (sample_id INTEGER, value TEXT);\n"
        "CREATE TRIGGER sample_audit AFTER INSERT ON sample\n"
        "BEGIN\n"
        "  INSERT INTO audit (sample_id, value) VALUES (NEW.id, NEW.value);\n"
        "END;\n",
        encoding="utf-8",
    )

    conn = sqlite3.connect(":memory:")
    try:
        apply_migration_files(conn, migrations)
        conn.execute("INSERT INTO sample (value) VALUES ('created')")
        audit = conn.execute("SELECT sample_id, value FROM audit").fetchone()
    finally:
        conn.close()

    assert audit == (1, "created")


def test_apply_migration_files_accepts_a_trailing_comment_after_valid_sql(tmp_path):
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_comment.sql").write_text(
        "CREATE TABLE sample (id INTEGER PRIMARY KEY);\n"
        "-- the migration intentionally ends with a comment\n",
        encoding="utf-8",
    )

    conn = sqlite3.connect(":memory:")
    try:
        apply_migration_files(conn, migrations)
        marked = conn.execute("SELECT migration_file FROM migrations_applied").fetchall()
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'sample'"
        ).fetchone()
    finally:
        conn.close()

    assert marked == [("001_comment.sql",)]
    assert table == ("sample",)


def test_apply_migration_files_skips_a_comment_prefixed_diagnostic_with_parameters(tmp_path):
    """Query-plan diagnostics must not execute as part of a migration."""
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    (migrations / "001_diagnostic.sql").write_text(
        "CREATE TABLE sample (id INTEGER PRIMARY KEY);\n"
        "-- inspect the planned lookup without executing a parameterized query\n"
        "EXPLAIN QUERY PLAN SELECT * FROM sample WHERE id = ?;\n"
        "CREATE TABLE applied_after_diagnostic (id INTEGER PRIMARY KEY);\n",
        encoding="utf-8",
    )

    conn = sqlite3.connect(":memory:")
    try:
        apply_migration_files(conn, migrations)
        marked = conn.execute("SELECT migration_file FROM migrations_applied").fetchall()
        table = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table' AND name = 'applied_after_diagnostic'"
        ).fetchone()
    finally:
        conn.close()

    assert marked == [("001_diagnostic.sql",)]
    assert table == ("applied_after_diagnostic",)


def test_current_schema_installs_outline_manifest_storage_idempotently(tmp_path):
    database = DatabaseConnection(str(tmp_path / "outline-manifest.db"))
    conn = database.get_connection()
    migrations = Path("infrastructure/persistence/database/migrations")

    apply_migration_files(conn, migrations)
    apply_migration_files(conn, migrations)

    tables = {
        row[0]
        for row in conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    }
    head_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(outline_planning_heads)")
    }
    candidate_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(chapter_candidates)")
    }
    attempt_columns = {
        row[1]
        for row in conn.execute("PRAGMA table_info(outline_generation_attempts)")
    }
    run_sql = conn.execute(
        "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = 'novel_generation_runs'"
    ).fetchone()[0]
    applied_count = conn.execute(
        "SELECT COUNT(*) FROM migrations_applied "
        "WHERE migration_file = '031_outline_plan_manifests.sql'"
    ).fetchone()[0]

    assert {
        "outline_planning_heads",
        "outline_plan_revisions",
        "outline_plan_revision_items",
    } <= tables
    assert {
        "active_plan_revision_id",
        "working_plan_revision_id",
        "authority_generation",
        "projection_generation",
    } <= head_columns
    assert {
        "planning_authority_generation",
        "plan_revision_id",
        "plan_digest",
        "chapter_outline_digest",
    } <= candidate_columns
    assert {
        "plan_revision_id",
        "cohort_parent_logical_node_id",
        "cohort_level",
    } <= attempt_columns
    assert applied_count == 1
    assert "waiting_planning" in run_sql
    assert conn.execute(
        "SELECT COUNT(*) FROM migrations_applied "
        "WHERE migration_file = '032_generation_waiting_planning.sql'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT COUNT(*) FROM migrations_applied "
        "WHERE migration_file = '033_outline_manifest_immutability_hardening.sql'"
    ).fetchone()[0] == 1


def test_manifest_hardening_migration_preserves_parent_novel_cascade(tmp_path):
    database = DatabaseConnection(str(tmp_path / "manifest-delete.db"))
    conn = database.get_connection()
    conn.execute("INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'N', 'n')")
    conn.execute(
        "INSERT INTO outline_contracts (id, novel_id, level, status) "
        "VALUES ('contract-1', 'novel-1', 'outline', 'synced')"
    )
    conn.execute(
        "INSERT INTO outline_contract_versions "
        "(id, contract_id, revision, payload_json, digest, status, sealed_at) "
        "VALUES ('version-1', 'contract-1', 1, '{}', 'version-digest', "
        "'synced', CURRENT_TIMESTAMP)"
    )
    conn.execute(
        "INSERT INTO outline_plan_revisions "
        "(id, novel_id, revision, status, digest, canonical_prefix_digest, "
        "reconciliation_status) "
        "VALUES ('plan-1', 'novel-1', 1, 'draft', 'plan-digest', '', 'aligned')"
    )
    conn.execute(
        "INSERT INTO outline_plan_revision_items "
        "(id, plan_revision_id, logical_node_id, version_id, level, sibling_index) "
        "VALUES ('item-1', 'plan-1', 'contract-1', 'version-1', 'outline', 0)"
    )
    conn.execute(
        "UPDATE outline_plan_revisions SET status='ready_for_review', "
        "sealed_at=CURRENT_TIMESTAMP WHERE id='plan-1'"
    )
    conn.execute(
        "INSERT INTO outline_planning_heads "
        "(novel_id, authority_mode, authority_generation, active_plan_revision_id, "
        "active_plan_digest, projection_generation) "
        "VALUES ('novel-1', 'manifest', 1, 'plan-1', 'plan-digest', 1)"
    )
    conn.commit()

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute("DELETE FROM outline_contract_versions WHERE id='version-1'")
    conn.rollback()

    conn.execute("DELETE FROM novels WHERE id='novel-1'")
    conn.commit()

    for table in (
        "outline_planning_heads",
        "outline_plan_revision_items",
        "outline_plan_revisions",
        "outline_contract_versions",
        "outline_contracts",
    ):
        assert conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0] == 0


def test_manifest_hardening_rejects_invalid_preexisting_manifest_head(tmp_path):
    """033 must not bless a 031/032 Head that is no longer publishable."""

    conn = sqlite3.connect(str(tmp_path / "pre-033-invalid-head.db"))
    conn.row_factory = sqlite3.Row
    migrations = Path("infrastructure/persistence/database/migrations")
    staged = tmp_path / "migrations-through-032"
    staged.mkdir()
    for migration_name in (
        "031_outline_plan_manifests.sql",
        "032_generation_waiting_planning.sql",
    ):
        shutil.copy2(migrations / migration_name, staged / migration_name)

    try:
        conn.executescript(
            """
            PRAGMA foreign_keys = ON;
            CREATE TABLE novels (
                id TEXT PRIMARY KEY,
                title TEXT NOT NULL,
                slug TEXT NOT NULL
            );
            CREATE TABLE outline_contracts (
                id TEXT PRIMARY KEY,
                novel_id TEXT NOT NULL,
                level TEXT NOT NULL
            );
            CREATE TABLE outline_contract_versions (
                id TEXT PRIMARY KEY,
                contract_id TEXT NOT NULL,
                revision INTEGER NOT NULL,
                payload_json TEXT NOT NULL DEFAULT '{}',
                digest TEXT NOT NULL DEFAULT ''
            );
            CREATE TABLE chapter_candidates (
                id TEXT PRIMARY KEY,
                novel_id TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'draft'
            );
            CREATE TABLE outline_generation_attempts (
                id TEXT PRIMARY KEY
            );
            CREATE TABLE novel_generation_runs (
                novel_id TEXT PRIMARY KEY,
                run_mode TEXT NOT NULL DEFAULT 'continuous',
                state TEXT NOT NULL DEFAULT 'idle',
                generation_epoch INTEGER NOT NULL DEFAULT 0,
                target_chapters INTEGER NOT NULL DEFAULT 0,
                current_formal_chapter INTEGER NOT NULL DEFAULT 0,
                current_candidate_id TEXT,
                current_candidate_chapter INTEGER,
                canonical_sync_status TEXT NOT NULL DEFAULT 'ready',
                next_action TEXT NOT NULL DEFAULT '',
                last_error TEXT NOT NULL DEFAULT '',
                max_pending_candidates INTEGER NOT NULL DEFAULT 1,
                prefetch INTEGER NOT NULL DEFAULT 0,
                updated_at TEXT,
                created_at TEXT
            );
            """
        )
        conn.commit()
        apply_migration_files(conn, staged)
        assert conn.execute(
            "SELECT COUNT(*) FROM migrations_applied "
            "WHERE migration_file = '032_generation_waiting_planning.sql'"
        ).fetchone()[0] == 1

        conn.execute(
            "INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'N', 'n')"
        )
        conn.execute(
            """
            INSERT INTO outline_plan_revisions
                (id, novel_id, revision, status, digest, canonical_prefix_digest,
                 reconciliation_status, sealed_at)
            VALUES ('invalid-plan', 'novel-1', 1, 'ready_for_review',
                    'invalid-digest', '', 'author_decision_required',
                    CURRENT_TIMESTAMP)
            """
        )
        conn.execute(
            """
            INSERT INTO outline_planning_heads
                (novel_id, authority_mode, authority_generation,
                 active_plan_revision_id, active_plan_digest, projection_generation)
            VALUES ('novel-1', 'manifest', 1, 'invalid-plan', 'invalid-digest', 1)
            """
        )
        conn.commit()

        hardening = tmp_path / "hardening-only"
        hardening.mkdir()
        shutil.copy2(
            migrations / "033_outline_manifest_immutability_hardening.sql",
            hardening / "033_outline_manifest_immutability_hardening.sql",
        )
        apply_migration_files(conn, hardening)

        assert conn.execute(
            "SELECT COUNT(*) FROM migrations_applied "
            "WHERE migration_file = '033_outline_manifest_immutability_hardening.sql'"
        ).fetchone()[0] == 0
        assert tuple(conn.execute(
            "SELECT authority_mode, active_plan_revision_id FROM outline_planning_heads "
            "WHERE novel_id = 'novel-1'"
        ).fetchone()) == ("manifest", "invalid-plan")
        assert conn.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type = 'trigger' "
            "AND name = 'trg_outline_planning_heads_publishable_update'"
        ).fetchone()[0] == 0

        class ExistingConnectionDatabase(DatabaseConnection):
            def __init__(self, connection: sqlite3.Connection, db_path: str) -> None:
                self._connection = connection
                self.db_path = db_path

            def get_connection(self) -> sqlite3.Connection:
                return self._connection

        repository = OutlineContractRepository(
            ExistingConnectionDatabase(conn, str(tmp_path / "pre-033-invalid-head.db"))
        )
        with pytest.raises(OutlineGateError, match="not aligned"):
            repository.get_active_plan("novel-1")
    finally:
        conn.close()
