import sqlite3

from infrastructure.persistence.database.migration_runner import apply_migration_files


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
