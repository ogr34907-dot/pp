"""A candidate chapter must never become a formal chapter before approval + sync."""

from concurrent.futures import ThreadPoolExecutor
import hashlib
import sqlite3
import threading

import pytest

from application.world.services.chapter_narrative_sync import (
    CHAPTER_NARRATIVE_PIPELINE_VERSION,
)
from domain.novel.candidate_chapter import CandidateStatus, GenerationRunState, RunMode
from infrastructure.persistence.database.chapter_candidate_repository import (
    CandidateGateError,
    ChapterCandidateRepository,
)
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_chapter_narrative_commit_repository import (
    SqliteChapterNarrativeCommitRepository,
)


@pytest.fixture
def candidates(tmp_path):
    db = DatabaseConnection(str(tmp_path / "candidates.db"))
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("novel-1", "Candidate Novel", "candidate-novel", 20),
    )
    conn.commit()
    repo = ChapterCandidateRepository(db)
    repo.start_run("novel-1", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=20)
    return repo, db


def _chain():
    return {"outline": {"contract_id": "root", "revision": 1, "digest": "root-v1"}}


def _insert_legacy_chapter(conn, novel_id: str, number: int, content: str, *, status: str = "completed") -> None:
    conn.execute(
        """
        INSERT INTO chapters (id, novel_id, number, title, content, status)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (f"legacy-{novel_id}-{number}", novel_id, number, f"第{number}章", content, status),
    )


class _BeginBarrierConnection:
    """Force two importers to reach their first write transaction together."""

    def __init__(self, connection, barrier: threading.Barrier):
        self._connection = connection
        self._barrier = barrier

    def execute(self, sql, params=()):
        if sql.strip().upper() in {"BEGIN", "BEGIN IMMEDIATE"}:
            self._barrier.wait(timeout=5)
        return self._connection.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._connection, name)


def test_new_book_has_no_pre_candidate_head(tmp_path):
    db = DatabaseConnection(str(tmp_path / "new-book.db"))
    db.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("new-book", "New Book", "new-book", 20),
    )
    db.get_connection().commit()

    assert ChapterCandidateRepository(db).formal_chapter_head("new-book") == 0


@pytest.mark.parametrize("run_mode", [RunMode.CHAPTER_REVIEW, RunMode.CONTINUOUS])
def test_start_run_rejects_unproven_completed_legacy_history(tmp_path, run_mode):
    db = DatabaseConnection(str(tmp_path / "unproven-legacy-start.db"))
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("legacy-start", "Legacy Start", "legacy-start", 20),
    )
    for number in range(1, 3):
        _insert_legacy_chapter(conn, "legacy-start", number, f"旧正文 {number}")
    conn.commit()

    with pytest.raises(CandidateGateError, match="unproven completed chapter"):
        ChapterCandidateRepository(db).start_run(
            "legacy-start", run_mode=run_mode, target_chapters=20
        )

    assert conn.execute(
        "SELECT COUNT(*) FROM novel_generation_runs WHERE novel_id = ?",
        ("legacy-start",),
    ).fetchone()[0] == 0


@pytest.mark.parametrize("run_mode", [RunMode.CHAPTER_REVIEW, RunMode.CONTINUOUS])
def test_explicit_legacy_baseline_continues_at_next_chapter_in_both_modes(tmp_path, run_mode):
    db = DatabaseConnection(str(tmp_path / "legacy-baseline.db"))
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("legacy-book", "Legacy Book", "legacy-book", 20),
    )
    for number in range(1, 11):
        _insert_legacy_chapter(conn, "legacy-book", number, f"旧正文 {number}")
    conn.commit()
    repo = ChapterCandidateRepository(db)

    baseline = repo.import_legacy_formal_history("legacy-book")

    assert baseline == {"head": 10, "imported": 10, "idempotent": False}
    assert repo.formal_chapter_head("legacy-book") == 10
    assert conn.execute("SELECT COUNT(*) FROM chapter_candidate_formal_commits").fetchone()[0] == 0
    assert conn.execute("SELECT COUNT(*) FROM chapter_candidates").fetchone()[0] == 0
    run = repo.start_run("legacy-book", run_mode=run_mode, target_chapters=1)
    assert run.current_formal_chapter == 10
    candidate = repo.create_streaming_candidate(
        novel_id="legacy-book",
        chapter_number=11,
        title="第十一章",
        outline_chain=_chain(),
        llm_content="第十一章候选正文",
    )
    assert candidate.chapter_number == 11
    repo.mark_auditing(candidate.id)
    candidate = repo.finish_audit(candidate.id, audit={}, commit_plan={})
    if run_mode == RunMode.CHAPTER_REVIEW:
        assert candidate.status == CandidateStatus.AWAITING_REVIEW
        repo.approve_for_commit(candidate.id, continue_after_commit=False)
    else:
        assert candidate.status == CandidateStatus.COMMITTING
    repo.commit_formal(candidate.id)
    repo.mark_sync_succeeded(candidate.id)
    assert repo.formal_chapter_head("legacy-book") == 11


@pytest.mark.parametrize(
    "sql, params",
    [
        (
            "UPDATE chapters SET content = ? WHERE novel_id = ? AND number = ?",
            ("被篡改的旧正文", "legacy-create-guard", 2),
        ),
        (
            "UPDATE chapters SET content_revision = content_revision + 1 WHERE novel_id = ? AND number = ?",
            ("legacy-create-guard", 2),
        ),
    ],
)
def test_create_candidate_revalidates_legacy_baseline_before_next_slot(tmp_path, sql, params):
    db = DatabaseConnection(str(tmp_path / "legacy-create-guard.db"))
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("legacy-create-guard", "Legacy Create Guard", "legacy-create-guard", 20),
    )
    for number in range(1, 3):
        _insert_legacy_chapter(conn, "legacy-create-guard", number, f"旧正文 {number}")
    conn.commit()
    repo = ChapterCandidateRepository(db)
    repo.import_legacy_formal_history("legacy-create-guard")
    repo.start_run(
        "legacy-create-guard", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=20
    )
    conn.execute(sql, params)
    conn.commit()

    with pytest.raises(CandidateGateError, match="legacy formal history integrity mismatch"):
        repo.create_streaming_candidate(
            novel_id="legacy-create-guard",
            chapter_number=3,
            title="第三章",
            outline_chain=_chain(),
        )

    assert conn.execute(
        "SELECT COUNT(*) FROM chapter_candidates WHERE novel_id = ?",
        ("legacy-create-guard",),
    ).fetchone()[0] == 0


def test_create_candidate_rejects_a_stale_run_formal_cursor(tmp_path):
    db = DatabaseConnection(str(tmp_path / "legacy-stale-cursor.db"))
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("legacy-stale-cursor", "Legacy Stale Cursor", "legacy-stale-cursor", 20),
    )
    for number in range(1, 3):
        _insert_legacy_chapter(conn, "legacy-stale-cursor", number, f"旧正文 {number}")
    conn.commit()
    repo = ChapterCandidateRepository(db)
    repo.import_legacy_formal_history("legacy-stale-cursor")
    repo.start_run(
        "legacy-stale-cursor", run_mode=RunMode.CONTINUOUS, target_chapters=20
    )
    conn.execute(
        "UPDATE novel_generation_runs SET current_formal_chapter = 1 WHERE novel_id = ?",
        ("legacy-stale-cursor",),
    )
    conn.commit()

    with pytest.raises(CandidateGateError, match="formal cursor"):
        repo.create_streaming_candidate(
            novel_id="legacy-stale-cursor",
            chapter_number=2,
            title="第二章",
            outline_chain=_chain(),
        )


def test_legacy_baseline_repeat_import_rejects_a_new_unproven_completed_chapter(tmp_path):
    db = DatabaseConnection(str(tmp_path / "legacy-unproven-extra.db"))
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("legacy-unproven-extra", "Legacy Unproven Extra", "legacy-unproven-extra", 20),
    )
    for number in range(1, 3):
        _insert_legacy_chapter(conn, "legacy-unproven-extra", number, f"旧正文 {number}")
    conn.commit()
    repo = ChapterCandidateRepository(db)
    repo.import_legacy_formal_history("legacy-unproven-extra")
    _insert_legacy_chapter(conn, "legacy-unproven-extra", 3, "未经候选流程的完成正文")
    conn.commit()

    with pytest.raises(CandidateGateError, match="unproven completed chapter"):
        repo.import_legacy_formal_history("legacy-unproven-extra")


def test_create_candidate_rejects_an_unproven_completed_chapter_after_legacy_baseline(tmp_path):
    db = DatabaseConnection(str(tmp_path / "legacy-unproven-candidate.db"))
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("legacy-unproven-candidate", "Legacy Unproven Candidate", "legacy-unproven-candidate", 20),
    )
    for number in range(1, 3):
        _insert_legacy_chapter(conn, "legacy-unproven-candidate", number, f"旧正文 {number}")
    conn.commit()
    repo = ChapterCandidateRepository(db)
    repo.import_legacy_formal_history("legacy-unproven-candidate")
    repo.start_run(
        "legacy-unproven-candidate", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=20
    )
    _insert_legacy_chapter(conn, "legacy-unproven-candidate", 3, "未经候选流程的完成正文")
    conn.commit()

    with pytest.raises(CandidateGateError, match="unproven completed chapter"):
        repo.create_streaming_candidate(
            novel_id="legacy-unproven-candidate",
            chapter_number=3,
            title="第三章",
            outline_chain=_chain(),
        )


def test_legacy_baseline_restricts_deleting_imported_chapter(tmp_path):
    db = DatabaseConnection(str(tmp_path / "legacy-delete-restrict.db"))
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("legacy-delete-restrict", "Legacy Delete Restrict", "legacy-delete-restrict", 20),
    )
    _insert_legacy_chapter(conn, "legacy-delete-restrict", 1, "受保护的旧正文")
    conn.commit()
    repo = ChapterCandidateRepository(db)
    repo.import_legacy_formal_history("legacy-delete-restrict")

    foreign_keys = conn.execute(
        "PRAGMA foreign_key_list(pre_candidate_formal_history)"
    ).fetchall()
    assert any(
        row["from"] == "chapter_id" and row["on_delete"] == "RESTRICT"
        for row in foreign_keys
    )

    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(
            "DELETE FROM chapters WHERE novel_id = ? AND number = 1",
            ("legacy-delete-restrict",),
        )


def test_concurrent_legacy_imports_are_exactly_idempotent(tmp_path):
    db_path = str(tmp_path / "legacy-import-race.db")
    setup = DatabaseConnection(db_path)
    setup_conn = setup.get_connection()
    setup_conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("legacy-import-race", "Legacy Import Race", "legacy-import-race", 20),
    )
    for number in range(1, 3):
        _insert_legacy_chapter(setup_conn, "legacy-import-race", number, f"旧正文 {number}")
    setup_conn.commit()

    first_db = DatabaseConnection(db_path)
    second_db = DatabaseConnection(db_path)
    barrier = threading.Barrier(2)
    first = ChapterCandidateRepository(first_db)
    second = ChapterCandidateRepository(second_db)
    first._connection = lambda: _BeginBarrierConnection(first_db.get_connection(), barrier)
    second._connection = lambda: _BeginBarrierConnection(second_db.get_connection(), barrier)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = [
            future.result(timeout=10)
            for future in (
                pool.submit(first.import_legacy_formal_history, "legacy-import-race"),
                pool.submit(second.import_legacy_formal_history, "legacy-import-race"),
            )
        ]

    assert sorted(result["imported"] for result in results) == [0, 2]
    assert sorted(result["idempotent"] for result in results) == [False, True]


def test_legacy_baseline_rejects_non_contiguous_or_incomplete_history(tmp_path):
    db = DatabaseConnection(str(tmp_path / "legacy-gap.db"))
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("legacy-gap", "Legacy Gap", "legacy-gap", 20),
    )
    for number in (1, 2, 3, 5):
        _insert_legacy_chapter(conn, "legacy-gap", number, f"旧正文 {number}")
    conn.commit()

    with pytest.raises(CandidateGateError, match="contiguous"):
        ChapterCandidateRepository(db).import_legacy_formal_history("legacy-gap")


def test_legacy_baseline_is_idempotent_but_tampering_blocks_future_starts(tmp_path):
    db = DatabaseConnection(str(tmp_path / "legacy-integrity.db"))
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("legacy-integrity", "Legacy Integrity", "legacy-integrity", 20),
    )
    for number in range(1, 3):
        _insert_legacy_chapter(conn, "legacy-integrity", number, f"旧正文 {number}")
    conn.commit()
    repo = ChapterCandidateRepository(db)

    assert repo.import_legacy_formal_history("legacy-integrity")["idempotent"] is False
    assert repo.import_legacy_formal_history("legacy-integrity") == {
        "head": 2,
        "imported": 0,
        "idempotent": True,
    }
    conn.execute(
        "UPDATE chapters SET content = '被篡改的旧正文' WHERE novel_id = ? AND number = 2",
        ("legacy-integrity",),
    )
    conn.commit()

    with pytest.raises(CandidateGateError, match="integrity"):
        repo.start_run("legacy-integrity", run_mode=RunMode.CONTINUOUS, target_chapters=20)


def test_legacy_baseline_rejects_candidate_first_history(tmp_path):
    db = DatabaseConnection(str(tmp_path / "candidate-history.db"))
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("candidate-history", "Candidate History", "candidate-history", 20),
    )
    conn.commit()
    repo = ChapterCandidateRepository(db)
    repo.start_run("candidate-history", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=20)
    candidate = repo.create_streaming_candidate(
        novel_id="candidate-history", chapter_number=1, title="第一章", outline_chain=_chain(), llm_content="候选正文"
    )
    repo.mark_auditing(candidate.id)
    repo.finish_audit(candidate.id, audit={}, commit_plan={})
    repo.approve_for_commit(candidate.id, continue_after_commit=False)
    repo.commit_formal(candidate.id)
    repo.mark_sync_succeeded(candidate.id)

    with pytest.raises(CandidateGateError, match="Candidate-first"):
        repo.import_legacy_formal_history("candidate-history")


@pytest.mark.parametrize("status", [CandidateStatus.REJECTED.value, CandidateStatus.CANCELLED.value])
def test_first_legacy_import_rejects_any_prior_candidate_history(tmp_path, status):
    db = DatabaseConnection(str(tmp_path / f"candidate-history-{status}.db"))
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("candidate-history", "Candidate History", "candidate-history", 20),
    )
    _insert_legacy_chapter(conn, "candidate-history", 1, "旧书正式正文")
    conn.execute(
        """
        INSERT INTO chapter_candidates (id, novel_id, chapter_number, generation_epoch, status)
        VALUES (?, 'candidate-history', 2, 0, ?)
        """,
        (f"candidate-{status}", status),
    )
    conn.commit()

    with pytest.raises(CandidateGateError, match="Candidate-first"):
        ChapterCandidateRepository(db).import_legacy_formal_history("candidate-history")


def test_exact_legacy_baseline_reimport_remains_idempotent_after_rejected_candidate(tmp_path):
    db = DatabaseConnection(str(tmp_path / "legacy-reimport-rejected.db"))
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("legacy-reimport", "Legacy Reimport", "legacy-reimport", 20),
    )
    _insert_legacy_chapter(conn, "legacy-reimport", 1, "旧书正式正文")
    conn.commit()
    repo = ChapterCandidateRepository(db)
    assert repo.import_legacy_formal_history("legacy-reimport")["idempotent"] is False
    conn.execute(
        """
        INSERT INTO chapter_candidates (id, novel_id, chapter_number, generation_epoch, status)
        VALUES ('later-rejected', 'legacy-reimport', 2, 0, 'rejected')
        """
    )
    conn.commit()

    assert repo.import_legacy_formal_history("legacy-reimport") == {
        "head": 1,
        "imported": 0,
        "idempotent": True,
    }


def test_legacy_import_uses_only_the_continuous_completed_prefix(tmp_path):
    db = DatabaseConnection(str(tmp_path / "legacy-prefix-only.db"))
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("legacy-prefix", "Legacy Prefix", "legacy-prefix", 20),
    )
    _insert_legacy_chapter(conn, "legacy-prefix", 1, "旧正文一")
    _insert_legacy_chapter(conn, "legacy-prefix", 2, "旧正文二")
    _insert_legacy_chapter(conn, "legacy-prefix", 3, "", status="draft")
    _insert_legacy_chapter(conn, "legacy-prefix", 4, "后续人工草稿", status="draft")
    conn.commit()

    baseline = ChapterCandidateRepository(db).import_legacy_formal_history("legacy-prefix")

    assert baseline == {"head": 2, "imported": 2, "idempotent": False}


def test_create_candidate_rejects_a_nonempty_draft_in_the_next_formal_slot(tmp_path):
    db = DatabaseConnection(str(tmp_path / "nonempty-draft-slot.db"))
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("draft-slot", "Draft Slot", "draft-slot", 20),
    )
    _insert_legacy_chapter(conn, "draft-slot", 1, "作者已经写下的草稿", status="draft")
    conn.commit()
    repo = ChapterCandidateRepository(db)
    repo.start_run("draft-slot", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=1)

    with pytest.raises(CandidateGateError, match="formal slot"):
        repo.create_streaming_candidate(
            novel_id="draft-slot", chapter_number=1, title="第一章", outline_chain=_chain()
        )


def test_formal_commit_replaces_only_an_empty_draft_placeholder(tmp_path):
    db = DatabaseConnection(str(tmp_path / "empty-draft-placeholder.db"))
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("draft-placeholder", "Draft Placeholder", "draft-placeholder", 20),
    )
    conn.execute(
        """
        INSERT INTO chapters (id, novel_id, number, title, content, status)
        VALUES ('placeholder-1', 'draft-placeholder', 1, '占位章', '', 'draft')
        """
    )
    conn.commit()
    repo = ChapterCandidateRepository(db)
    repo.start_run(
        "draft-placeholder", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=1
    )
    candidate = repo.create_streaming_candidate(
        novel_id="draft-placeholder",
        chapter_number=1,
        title="第一章",
        outline_chain=_chain(),
        llm_content="候选正式正文",
    )
    repo.mark_auditing(candidate.id)
    repo.finish_audit(candidate.id, audit={}, commit_plan={})
    repo.approve_for_commit(candidate.id, continue_after_commit=False)

    syncing = repo.commit_formal(candidate.id)

    assert syncing.formal_chapter_id == "placeholder-1"
    chapter = conn.execute(
        "SELECT content, status FROM chapters WHERE id = 'placeholder-1'"
    ).fetchone()
    assert dict(chapter) == {"content": "候选正式正文", "status": "completed"}


def test_formal_commit_rechecks_legacy_baseline_inside_its_write_transaction(tmp_path):
    db = DatabaseConnection(str(tmp_path / "legacy-commit-authority.db"))
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("legacy-commit", "Legacy Commit", "legacy-commit", 20),
    )
    _insert_legacy_chapter(conn, "legacy-commit", 1, "旧书正式正文")
    conn.commit()
    repo = ChapterCandidateRepository(db)
    repo.import_legacy_formal_history("legacy-commit")
    repo.start_run("legacy-commit", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=2)
    candidate = repo.create_streaming_candidate(
        novel_id="legacy-commit",
        chapter_number=2,
        title="第二章",
        outline_chain=_chain(),
        llm_content="候选正式正文",
    )
    repo.mark_auditing(candidate.id)
    repo.finish_audit(candidate.id, audit={}, commit_plan={})
    repo.approve_for_commit(candidate.id, continue_after_commit=False)
    conn.execute(
        "UPDATE chapters SET content_revision = content_revision + 1 WHERE novel_id = 'legacy-commit' AND number = 1"
    )
    conn.commit()

    with pytest.raises(CandidateGateError, match="legacy formal history integrity mismatch"):
        repo.commit_formal(candidate.id)

    assert conn.execute(
        "SELECT COUNT(*) FROM chapters WHERE novel_id = 'legacy-commit' AND number = 2"
    ).fetchone()[0] == 0


def test_review_mode_enforces_one_pending_candidate_and_never_prefetches(candidates):
    repo, _ = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1", chapter_number=1, title="第一章", outline_chain=_chain()
    )
    assert candidate.status == CandidateStatus.STREAMING
    assert repo.get_run("novel-1").max_pending_candidates == 1
    assert repo.get_run("novel-1").prefetch == 0

    with pytest.raises(CandidateGateError, match="pending candidate"):
        repo.create_streaming_candidate(
            novel_id="novel-1", chapter_number=2, title="第二章", outline_chain=_chain()
        )

    repo.set_generated_content(candidate.id, "AI 完整候选稿")
    repo.mark_auditing(candidate.id)
    reviewed = repo.finish_audit(
        candidate.id,
        audit={"alignment": "pass"},
        commit_plan={"summary": "主角决定离乡", "facts": [{"type": "event"}]},
    )
    assert reviewed.status == CandidateStatus.AWAITING_REVIEW
    assert repo.get_run("novel-1").state == GenerationRunState.WAITING_REVIEW

    # Waiting for a human review is a strict token boundary: no Chapter 2 work.
    with pytest.raises(CandidateGateError, match="waiting_review"):
        repo.create_streaming_candidate(
            novel_id="novel-1", chapter_number=2, title="第二章", outline_chain=_chain()
        )


def test_start_run_resumes_formal_cursor_after_synced_candidate_commits(tmp_path):
    db = DatabaseConnection(str(tmp_path / "existing-chapters.db"))
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("novel-existing", "Existing Novel", "existing-novel", 20),
    )
    conn.commit()

    repo = ChapterCandidateRepository(db)
    repo.start_run(
        "novel-existing", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=20
    )
    for number in (1, 2):
        candidate = repo.create_streaming_candidate(
            novel_id="novel-existing",
            chapter_number=number,
            title=f"第{number}章",
            outline_chain=_chain(),
            llm_content=f"正文{number}",
        )
        repo.mark_auditing(candidate.id)
        repo.finish_audit(candidate.id, audit={}, commit_plan={})
        repo.approve_for_commit(candidate.id, continue_after_commit=number == 1)
        repo.commit_formal(candidate.id)
        repo.mark_sync_succeeded(candidate.id)

    run = repo.start_run(
        "novel-existing", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=20
    )

    assert run.current_formal_chapter == 2
    candidate = repo.create_streaming_candidate(
        novel_id="novel-existing", chapter_number=3, title="第三章", outline_chain=_chain()
    )
    assert candidate.chapter_number == 3


def test_formal_authority_persists_exact_version_and_rejects_tampering(candidates):
    repo, db = candidates
    content = "候选正式正文"
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1",
        chapter_number=1,
        title="第一章",
        outline_chain=_chain(),
        llm_content=content,
    )
    repo.mark_auditing(candidate.id)
    repo.finish_audit(candidate.id, audit={}, commit_plan={})
    repo.approve_for_commit(candidate.id, continue_after_commit=False)
    formal = repo.commit_formal(candidate.id)
    repo.mark_sync_succeeded(candidate.id)

    authority = db.fetch_one(
        "SELECT content_sha256, content_revision, provenance "
        "FROM chapter_candidate_formal_commits WHERE candidate_id = ?",
        (candidate.id,),
    )
    assert dict(authority) == {
        "content_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
        "content_revision": formal.content_revision,
        "provenance": "candidate_commit",
    }

    db.execute(
        "UPDATE chapters SET content = ?, content_sha256 = ?, content_revision = content_revision + 1 "
        "WHERE id = ?",
        (
            "越权改写正文",
            hashlib.sha256("越权改写正文".encode("utf-8")).hexdigest(),
            formal.formal_chapter_id,
        ),
    )
    db.get_connection().commit()

    assert repo.formal_chapter_head("novel-1") == 0
    with pytest.raises(CandidateGateError, match="formal chapter authority mismatch"):
        repo.assert_formal_history_is_proven("novel-1")


def test_start_run_uses_persisted_novel_target_chapters(tmp_path):
    db = DatabaseConnection(str(tmp_path / "persisted-target.db"))
    db.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("novel-target", "Target Novel", "target-novel", 20),
    )
    db.get_connection().commit()

    run = ChapterCandidateRepository(db).start_run(
        "novel-target", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=3
    )

    assert run.target_chapters == 20


def test_start_run_ignores_empty_and_uncommitted_draft_chapters(tmp_path):
    """Draft placeholders must never move the durable formal cursor."""

    db = DatabaseConnection(str(tmp_path / "draft-placeholders.db"))
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("novel-drafts", "Draft Novel", "draft-novel", 20),
    )
    for number, content in ((1, ""), (2, "临时草稿"), (100, "")):
        conn.execute(
            """
            INSERT INTO chapters (id, novel_id, number, title, content, status)
            VALUES (?, 'novel-drafts', ?, ?, ?, 'draft')
            """,
            (f"draft-{number}", number, f"第{number}章", content),
        )
    conn.commit()

    repo = ChapterCandidateRepository(db)
    run = repo.start_run(
        "novel-drafts", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=20
    )

    assert run.current_formal_chapter == 0
    candidate = repo.create_streaming_candidate(
        novel_id="novel-drafts", chapter_number=1, title="第一章", outline_chain=_chain()
    )
    assert candidate.chapter_number == 1


def test_create_candidate_requires_the_immediate_next_formal_chapter(candidates):
    repo, db = candidates
    db.execute(
        "UPDATE novel_generation_runs SET current_formal_chapter = 2 WHERE novel_id = 'novel-1'"
    )
    db.get_connection().commit()

    with pytest.raises(CandidateGateError, match="immediate next chapter"):
        repo.create_streaming_candidate(
            novel_id="novel-1", chapter_number=4, title="第四章", outline_chain=_chain()
        )


def test_stopped_review_candidate_must_be_resolved_before_a_new_run(candidates):
    repo, _db = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1", chapter_number=1, title="第一章", outline_chain=_chain(), llm_content="候选"
    )
    repo.mark_auditing(candidate.id)
    repo.finish_audit(candidate.id, audit={}, commit_plan={})
    stopped = repo.stop_run("novel-1")

    assert stopped.state == GenerationRunState.STOPPED
    assert stopped.current_candidate_id == candidate.id
    with pytest.raises(CandidateGateError, match="pending candidate"):
        repo.start_run("novel-1", run_mode=RunMode.CHAPTER_REVIEW, target_chapters=3)
    assert repo.get_run("novel-1").state == GenerationRunState.STOPPED


def test_author_edit_stales_audit_and_commit_plan_until_reaudited(candidates):
    repo, db = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1", chapter_number=1, title="第一章", outline_chain=_chain(), llm_content="AI 初稿"
    )
    repo.mark_auditing(candidate.id)
    repo.finish_audit(candidate.id, audit={"alignment": "pass"}, commit_plan={"summary": "旧摘要"})

    edited = repo.edit_content(candidate.id, "作者修订稿", feedback="删掉巧合")
    assert edited.content_revision == 2
    assert edited.audit_is_current is False
    assert edited.commit_plan_is_current is False
    assert db.fetch_one("SELECT COUNT(*) AS total FROM chapters WHERE novel_id = ?", ("novel-1",))["total"] == 0

    with pytest.raises(CandidateGateError, match="re-audit"):
        repo.approve_for_commit(candidate.id, continue_after_commit=True)

    repo.mark_auditing(candidate.id)
    repo.finish_audit(candidate.id, audit={"alignment": "pass"}, commit_plan={"summary": "新摘要"})
    repo.approve_for_commit(candidate.id, continue_after_commit=True)
    syncing = repo.commit_formal(candidate.id)
    assert syncing.status == CandidateStatus.SYNCING
    assert db.fetch_one("SELECT content FROM chapters WHERE novel_id = ? AND number = 1", ("novel-1",))["content"] == "作者修订稿"

    committed = repo.mark_sync_succeeded(candidate.id)
    assert committed.status == CandidateStatus.COMMITTED
    assert repo.get_run("novel-1").state == GenerationRunState.RUNNING
    assert repo.get_run("novel-1").current_formal_chapter == 1


def test_approve_for_commit_rejects_current_audit_hard_blocks(candidates):
    repo, db = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1",
        chapter_number=1,
        title="第一章",
        outline_chain=_chain(),
        llm_content="正文没有发生必发生事件",
    )
    repo.mark_auditing(candidate.id)
    repo.finish_audit(
        candidate.id,
        audit={
            "hard_blocks": [{"type": "forbidden_event", "event": "不得杀人"}],
            "required_events_complete": False,
        },
        commit_plan={"timeline_events": ["正文没有发生的必发生事件"]},
    )

    with pytest.raises(CandidateGateError):
        repo.approve_for_commit(candidate.id, continue_after_commit=True)

    assert db.fetch_one(
        "SELECT status FROM chapter_candidates WHERE id = ?", (candidate.id,)
    )["status"] == "awaiting_review"


def test_retired_generation_cannot_apply_late_candidate_worker_writes(candidates):
    repo, db = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1", chapter_number=1, title="第一章", outline_chain=_chain()
    )
    db.execute(
        "UPDATE novel_generation_runs SET generation_epoch = generation_epoch + 1 WHERE novel_id = 'novel-1'"
    )
    db.get_connection().commit()

    with pytest.raises(CandidateGateError, match="retired generation epoch"):
        repo.set_generated_content(candidate.id, "迟到的旧任务正文")

    assert repo.get_candidate(candidate.id).llm_content == ""


def test_review_candidate_can_be_regenerated_or_rejected_without_formal_side_effects(candidates):
    repo, db = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1", chapter_number=1, title="第一章", outline_chain=_chain(), llm_content="旧候选"
    )
    repo.mark_auditing(candidate.id)
    repo.finish_audit(candidate.id, audit={"alignment": "warn"}, commit_plan={"summary": "旧"})

    regenerating = repo.request_regeneration(candidate.id, feedback="重写冲突段")
    assert regenerating.status == CandidateStatus.REGENERATING
    assert repo.get_run("novel-1").state == GenerationRunState.RUNNING
    regenerated = repo.set_generated_content(candidate.id, "新候选")
    assert regenerated.final_content == "新候选"

    repo.mark_auditing(candidate.id)
    repo.finish_audit(candidate.id, audit={"alignment": "pass"}, commit_plan={"summary": "新"})
    rejected = repo.reject_and_stop(candidate.id)
    assert rejected.status == CandidateStatus.REJECTED
    assert repo.get_run("novel-1").state == GenerationRunState.STOPPED
    assert db.fetch_one("SELECT COUNT(*) AS total FROM chapters WHERE novel_id = ?", ("novel-1",))["total"] == 0


def test_rejecting_an_inflight_candidate_cancels_its_dag_trace(candidates):
    repo, db = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1", chapter_number=1, title="第一章", outline_chain=_chain()
    )
    trace = repo.start_dag_run(candidate.id, content_revision=0)

    rejected = repo.reject_and_stop(candidate.id)

    assert rejected.status == CandidateStatus.REJECTED
    assert repo.get_run("novel-1").generation_epoch == 1
    assert db.fetch_one(
        "SELECT status FROM candidate_dag_runs WHERE id = ?", (trace["id"],)
    )["status"] == "cancelled"
    with pytest.raises(CandidateGateError, match="not active"):
        repo.finish_dag_run(trace["id"], status="completed", final_state={})


def test_formal_candidate_with_failed_sync_can_retry_without_duplicate_chapter(candidates):
    repo, db = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1", chapter_number=1, title="第一章", outline_chain=_chain(), llm_content="候选"
    )
    repo.mark_auditing(candidate.id)
    repo.finish_audit(candidate.id, audit={}, commit_plan={})
    repo.approve_for_commit(candidate.id, continue_after_commit=False)
    repo.commit_formal(candidate.id)
    failed = repo.mark_sync_failed(candidate.id, "canonical_aftermath_not_ready")
    assert failed.status == CandidateStatus.FAILED

    retrying = repo.begin_sync_retry(candidate.id)
    assert retrying.status == CandidateStatus.SYNCING
    repo.mark_sync_succeeded(candidate.id)
    assert db.fetch_one("SELECT COUNT(*) AS total FROM chapters WHERE novel_id = ?", ("novel-1",))["total"] == 1


def test_formal_candidate_with_failed_sync_cannot_be_rejected(candidates):
    repo, _db = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1", chapter_number=1, title="第一章", outline_chain=_chain(), llm_content="候选"
    )
    repo.mark_auditing(candidate.id)
    repo.finish_audit(candidate.id, audit={}, commit_plan={})
    repo.approve_for_commit(candidate.id, continue_after_commit=False)
    repo.commit_formal(candidate.id)
    repo.mark_sync_failed(candidate.id, "canonical_aftermath_not_ready")

    with pytest.raises(CandidateGateError, match="formal candidate"):
        repo.reject_and_stop(candidate.id)

    assert repo.get_candidate(candidate.id).formal_chapter_id is not None
    assert repo.get_run("novel-1").next_action == "retry_sync"


def test_formal_candidate_with_failed_sync_cannot_be_edited_or_regenerated(candidates):
    repo, db = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1",
        chapter_number=1,
        title="第一章",
        outline_chain=_chain(),
        llm_content="候选",
    )
    repo.mark_auditing(candidate.id)
    repo.finish_audit(candidate.id, audit={}, commit_plan={})
    repo.approve_for_commit(candidate.id, continue_after_commit=False)
    repo.commit_formal(candidate.id)
    failed = repo.mark_sync_failed(candidate.id, "canonical_aftermath_not_ready")

    with pytest.raises(CandidateGateError, match="formal candidate"):
        repo.edit_content(candidate.id, "错误改稿")
    with pytest.raises(CandidateGateError, match="formal candidate"):
        repo.request_regeneration(candidate.id)

    preserved = repo.get_candidate(candidate.id)
    assert preserved.status == CandidateStatus.FAILED
    assert preserved.content_revision == failed.content_revision
    assert preserved.final_content == "候选"
    assert repo.get_run("novel-1").next_action == "retry_sync"
    assert db.fetch_one(
        "SELECT sync_status FROM chapter_candidate_formal_commits WHERE candidate_id = ?",
        (candidate.id,),
    )["sync_status"] == "failed"


def test_sync_ready_publication_rejects_candidate_formal_version_mismatch(candidates):
    repo, db = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1",
        chapter_number=1,
        title="第一章",
        outline_chain=_chain(),
        llm_content="候选旧版本",
    )
    repo.mark_auditing(candidate.id)
    repo.finish_audit(candidate.id, audit={}, commit_plan={})
    repo.approve_for_commit(candidate.id, continue_after_commit=True)
    syncing = repo.commit_formal(candidate.id)
    rewritten = "作者并发改写版本"
    rewritten_sha = hashlib.sha256(rewritten.encode("utf-8")).hexdigest()
    db.execute(
        "UPDATE chapters SET content = ?, content_sha256 = ?, content_revision = ? WHERE id = ?",
        (rewritten, rewritten_sha, syncing.content_revision + 1, syncing.formal_chapter_id),
    )
    db.execute(
        "UPDATE chapter_candidate_formal_commits "
        "SET content_sha256 = ?, content_revision = ?, provenance = 'author_rewrite' "
        "WHERE candidate_id = ?",
        (rewritten_sha, syncing.content_revision + 1, candidate.id),
    )
    db.get_connection().commit()

    with pytest.raises(CandidateGateError, match="formal version"):
        repo.mark_sync_succeeded(candidate.id)

    assert repo.get_run("novel-1").current_formal_chapter == 0
    assert repo.get_candidate(candidate.id).status == CandidateStatus.SYNCING
    assert db.fetch_one(
        "SELECT sync_status FROM chapter_candidate_formal_commits WHERE candidate_id = ?",
        (candidate.id,),
    )["sync_status"] == "syncing"


def test_stopping_during_canonical_sync_finishes_sync_then_pauses(candidates):
    repo, _db = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1", chapter_number=1, title="第一章", outline_chain=_chain(), llm_content="候选"
    )
    repo.mark_auditing(candidate.id)
    repo.finish_audit(candidate.id, audit={}, commit_plan={})
    repo.approve_for_commit(candidate.id, continue_after_commit=True)
    repo.commit_formal(candidate.id)

    stopped = repo.stop_run("novel-1")

    assert stopped.state == GenerationRunState.PAUSED
    assert stopped.current_candidate_id == candidate.id
    assert stopped.next_action == "finish_sync_then_pause"
    assert repo.get_candidate(candidate.id).status == CandidateStatus.SYNCING

    committed = repo.mark_sync_succeeded(candidate.id)
    assert committed.status == CandidateStatus.COMMITTED
    resumed = repo.get_run("novel-1")
    assert resumed.state == GenerationRunState.PAUSED
    assert resumed.current_formal_chapter == 1
    assert resumed.current_candidate_id is None


def test_stopping_an_inflight_candidate_retires_late_worker_writes(candidates):
    repo, db = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1", chapter_number=1, title="第一章", outline_chain=_chain()
    )
    trace = repo.start_dag_run(candidate.id, content_revision=0)

    stopped = repo.stop_run("novel-1")

    assert stopped.state == GenerationRunState.STOPPED
    assert stopped.current_candidate_id is None
    assert repo.get_candidate(candidate.id).status == CandidateStatus.CANCELLED
    assert db.fetch_one(
        "SELECT status FROM candidate_dag_runs WHERE id = ?", (trace["id"],)
    )["status"] == "cancelled"
    with pytest.raises(CandidateGateError, match="retired generation epoch"):
        repo.set_generated_content(candidate.id, "终止后迟到的正文")


def test_candidate_dag_trace_persists_node_attempts_and_resumable_events(candidates):
    repo, _db = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1", chapter_number=1, title="第一章", outline_chain=_chain()
    )

    trace = repo.start_dag_run(candidate.id, content_revision=1)
    repo.record_dag_event(
        trace["id"],
        {"type": "node_started", "node_id": "exec_writer", "node_type": "exec_writer"},
    )
    repo.record_dag_event(
        trace["id"],
        {
            "type": "node_completed",
            "node_id": "exec_writer",
            "node_type": "exec_writer",
            "duration_ms": 12,
            "outputs": {"content": "候选正文"},
        },
    )
    repo.finish_dag_run(
        trace["id"],
        status="completed",
        final_state={"content": "候选正文", "review_required": True},
    )

    restored = repo.get_latest_dag_run(candidate.id)

    assert restored["status"] == "completed"
    assert restored["current_node_id"] == "exec_writer"
    assert restored["final_state"]["content"] == "候选正文"
    assert restored["node_attempts"] == [{
        "node_id": "exec_writer",
        "node_type": "exec_writer",
        "status": "completed",
        "duration_ms": 12,
    }]
    assert [event["sequence"] for event in restored["events"]] == [1, 2]


def test_completed_dag_trace_rejects_late_events_and_second_completion(candidates):
    repo, _db = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1", chapter_number=1, title="第一章", outline_chain=_chain()
    )
    trace = repo.start_dag_run(candidate.id, content_revision=0)
    repo.finish_dag_run(trace["id"], status="completed", final_state={"content": "候选正文"})

    with pytest.raises(CandidateGateError, match="not active"):
        repo.record_dag_event(
            trace["id"], {"type": "node_completed", "node_id": "late_writer"}
        )
    with pytest.raises(CandidateGateError, match="not active"):
        repo.finish_dag_run(trace["id"], status="failed", final_state={})

    restored = repo.get_dag_run(trace["id"])
    assert restored["status"] == "completed"
    assert restored["events"] == []


def test_service_restart_cancels_active_candidate_and_dag_trace(candidates):
    repo, db = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1", chapter_number=1, title="第一章", outline_chain=_chain()
    )
    trace = repo.start_dag_run(candidate.id, content_revision=0)

    recovered = repo.recover_after_service_restart("novel-1")

    assert recovered.state == GenerationRunState.STOPPED
    assert recovered.current_candidate_id is None
    assert recovered.generation_epoch == 1
    assert repo.get_candidate(candidate.id).status == CandidateStatus.CANCELLED
    assert db.fetch_one(
        "SELECT status FROM candidate_dag_runs WHERE id = ?", (trace["id"],)
    )["status"] == "cancelled"

    with pytest.raises(CandidateGateError, match="not active"):
        repo.finish_dag_run(trace["id"], status="completed", final_state={"content": "迟到"})

    with pytest.raises(CandidateGateError, match="retired generation epoch"):
        repo.set_generated_content(candidate.id, "迟到正文")


def test_service_restart_preserves_candidate_waiting_for_author_review(candidates):
    repo, _db = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1", chapter_number=1, title="第一章", outline_chain=_chain(), llm_content="候选"
    )
    repo.mark_auditing(candidate.id)
    repo.finish_audit(candidate.id, audit={}, commit_plan={})
    before = repo.get_run("novel-1")

    recovered = repo.recover_after_service_restart("novel-1")

    assert recovered.state == GenerationRunState.WAITING_REVIEW
    assert recovered.current_candidate_id == candidate.id
    assert recovered.generation_epoch == before.generation_epoch
    assert repo.get_candidate(candidate.id).status == CandidateStatus.AWAITING_REVIEW


def test_service_restart_marks_syncing_candidate_retryable_without_retiring_epoch(candidates):
    repo, db = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1", chapter_number=1, title="第一章", outline_chain=_chain(), llm_content="候选"
    )
    repo.mark_auditing(candidate.id)
    repo.finish_audit(candidate.id, audit={}, commit_plan={})
    repo.approve_for_commit(candidate.id, continue_after_commit=True)
    syncing = repo.commit_formal(candidate.id)
    content_sha256 = hashlib.sha256(syncing.final_content.encode("utf-8")).hexdigest()
    db.execute(
        "INSERT INTO chapter_narrative_commits "
        "(novel_id, chapter_number, content_sha256, pipeline_version, "
        "content_revision, status, memory_status) "
        "VALUES (?, ?, ?, ?, ?, 'in_progress', 'pending')",
        (
            "novel-1",
            1,
            content_sha256,
            CHAPTER_NARRATIVE_PIPELINE_VERSION,
            syncing.content_revision,
        ),
    )
    db.get_connection().commit()
    before = repo.get_run("novel-1")

    recovered = repo.recover_after_service_restart("novel-1")

    assert recovered.state == GenerationRunState.PAUSED
    assert recovered.canonical_sync_status == "failed"
    assert recovered.next_action == "retry_sync"
    assert recovered.generation_epoch == before.generation_epoch
    assert repo.get_candidate(candidate.id).status == CandidateStatus.FAILED
    assert repo.begin_sync_retry(candidate.id).status == CandidateStatus.SYNCING
    claim = SqliteChapterNarrativeCommitRepository(db).claim(
        novel_id="novel-1",
        chapter_number=1,
        content_sha256=content_sha256,
        pipeline_version=CHAPTER_NARRATIVE_PIPELINE_VERSION,
        expected_content_revision=syncing.content_revision,
        require_memory_sync=True,
    )
    assert claim.disposition == "claimed"


def test_service_restart_releases_abandoned_memory_claim(candidates):
    repo, db = candidates
    candidate = repo.create_streaming_candidate(
        novel_id="novel-1",
        chapter_number=1,
        title="第一章",
        outline_chain=_chain(),
        llm_content="候选",
    )
    repo.mark_auditing(candidate.id)
    repo.finish_audit(candidate.id, audit={}, commit_plan={})
    repo.approve_for_commit(candidate.id, continue_after_commit=True)
    syncing = repo.commit_formal(candidate.id)
    content_sha256 = hashlib.sha256(syncing.final_content.encode("utf-8")).hexdigest()
    db.execute(
        "INSERT INTO chapter_narrative_commits "
        "(novel_id, chapter_number, content_sha256, pipeline_version, "
        "content_revision, status, memory_status, memory_attempt_count) "
        "VALUES (?, ?, ?, ?, ?, 'committed', 'in_progress', 1)",
        (
            "novel-1",
            1,
            content_sha256,
            CHAPTER_NARRATIVE_PIPELINE_VERSION,
            syncing.content_revision,
        ),
    )
    db.get_connection().commit()

    repo.recover_after_service_restart("novel-1")
    repo.begin_sync_retry(candidate.id)

    commits = SqliteChapterNarrativeCommitRepository(db)
    claim = commits.claim(
        novel_id="novel-1",
        chapter_number=1,
        content_sha256=content_sha256,
        pipeline_version=CHAPTER_NARRATIVE_PIPELINE_VERSION,
        expected_content_revision=syncing.content_revision,
        require_memory_sync=True,
    )
    assert claim.disposition == "reused"
    assert commits.claim_memory_sync(
        novel_id="novel-1",
        chapter_number=1,
        content_sha256=content_sha256,
        pipeline_version=CHAPTER_NARRATIVE_PIPELINE_VERSION,
        content_revision=syncing.content_revision,
    ) == "claimed"
