"""Canonical prefix and outline/history reconciliation gates."""

import hashlib
import json

from application.blueprint.services.outline_contract_service import OutlineContractService
from domain.structure.outline_plan import PlanReconciliationStatus
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.outline_contract_repository import OutlineContractRepository


def _content(value: str) -> tuple[str, str]:
    return value, hashlib.sha256(value.encode("utf-8")).hexdigest()


def _service(tmp_path):
    db = DatabaseConnection(str(tmp_path / "canonical-prefix.db"))
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("novel-1", "Prefix Novel", "prefix-novel", 20),
    )
    conn.commit()
    return db, OutlineContractService(
        contract_repository=OutlineContractRepository(db),
        story_node_repository=object(),
    )


def _insert_chapter(conn, number: int, value: str, *, revision: int = 1):
    content, digest = _content(value)
    conn.execute(
        """
        INSERT INTO chapters
            (id, novel_id, number, title, content, content_sha256, content_revision, status)
        VALUES (?, 'novel-1', ?, ?, ?, ?, ?, 'completed')
        """,
        (f"chapter-{number}", number, f"第{number}章", content, digest, revision),
    )
    return content, digest


def _insert_exact_committed_summary(conn, *, chapter_number: int, digest: str, revision: int) -> None:
    conn.execute(
        "INSERT INTO knowledge (id, novel_id) VALUES ('knowledge-1', 'novel-1') "
        "ON CONFLICT(novel_id) DO NOTHING"
    )
    conn.execute(
        """
        INSERT INTO chapter_summaries
            (id, knowledge_id, chapter_number, summary, source_content_sha256,
             source_content_revision, pipeline_version, sync_status)
        VALUES ('summary-1', 'knowledge-1', ?, '可信章节摘要', ?, ?,
                'chapter-narrative-sync:v1', 'committed')
        ON CONFLICT(knowledge_id, chapter_number)
        DO UPDATE SET summary = excluded.summary,
                      source_content_sha256 = excluded.source_content_sha256,
                      source_content_revision = excluded.source_content_revision,
                      pipeline_version = excluded.pipeline_version,
                      sync_status = excluded.sync_status
        """,
        (chapter_number, digest, revision),
    )


def _insert_exact_committed_aftermath(
    conn, *, chapter_number: int, digest: str, revision: int, memory_status: str = "committed"
) -> None:
    conn.execute(
        """
        INSERT INTO chapter_narrative_commits
            (novel_id, chapter_number, content_sha256, pipeline_version,
             content_revision, status, memory_status)
        VALUES ('novel-1', ?, ?, 'chapter-narrative-sync:v1', ?, 'committed', ?)
        """,
        (chapter_number, digest, revision, memory_status),
    )
    _insert_exact_committed_summary(
        conn, chapter_number=chapter_number, digest=digest, revision=revision
    )


def test_legacy_prefix_is_stable_and_excludes_generation_epoch(tmp_path):
    db, service = _service(tmp_path)
    conn = db.get_connection()
    content, digest = _insert_chapter(conn, 1, "旧正文")
    conn.execute(
        """
        INSERT INTO pre_candidate_formal_history
            (novel_id, chapter_number, chapter_id, content_sha256, content_revision)
        VALUES ('novel-1', 1, 'chapter-1', ?, 1)
        """,
        (digest,),
    )
    _insert_exact_committed_aftermath(
        conn, chapter_number=1, digest=digest, revision=1
    )
    conn.execute(
        """
        INSERT INTO novel_generation_runs
            (novel_id, run_mode, state, generation_epoch, target_chapters,
             current_formal_chapter, canonical_sync_status)
        VALUES ('novel-1', 'continuous', 'stopped', 1, 20, 1, 'ready')
        """
    )
    conn.commit()

    first = service.compute_canonical_prefix("novel-1", 1)
    conn.execute(
        "UPDATE novel_generation_runs SET generation_epoch = 99 WHERE novel_id = 'novel-1'"
    )
    conn.commit()
    second = service.compute_canonical_prefix("novel-1", 1)

    assert first.formal_head == 1
    assert first.digest == second.digest
    assert first.formal_ready is True
    assert first.canonical_ready is True
    assert first.memory_ready is True
    assert first.ready is True
    assert content


def test_legacy_prefix_requires_exact_durable_aftermath(tmp_path):
    db, service = _service(tmp_path)
    conn = db.get_connection()
    _, digest = _insert_chapter(conn, 1, "缺少 Aftermath 的旧正文")
    conn.execute(
        """
        INSERT INTO pre_candidate_formal_history
            (novel_id, chapter_number, chapter_id, content_sha256, content_revision)
        VALUES ('novel-1', 1, 'chapter-1', ?, 1)
        """,
        (digest,),
    )
    conn.commit()

    prefix = service.compute_canonical_prefix("novel-1", 1)

    assert prefix.formal_ready is True
    assert prefix.canonical_ready is False
    assert prefix.memory_ready is False
    assert prefix.ready is False
    assert "canonical:chapter:1" in prefix.blockers


def test_candidate_prefix_requires_matching_ready_narrative_identity_but_separates_memory(
    tmp_path,
):
    db, service = _service(tmp_path)
    conn = db.get_connection()
    _, digest = _insert_chapter(conn, 1, "候选正式正文", revision=2)
    conn.execute(
        """
        INSERT INTO chapter_candidates
            (id, novel_id, chapter_number, title, generation_epoch, status,
             llm_content, content_revision)
        VALUES ('candidate-1', 'novel-1', 1, '第一章', 7, 'committed', ?, 2)
        """,
        ("候选正式正文",),
    )
    conn.execute(
        """
        INSERT INTO chapter_candidate_formal_commits
            (candidate_id, novel_id, chapter_number, chapter_id,
             content_sha256, content_revision, provenance, sync_status)
        VALUES ('candidate-1', 'novel-1', 1, 'chapter-1', ?, 2, 'candidate_commit', 'ready')
        """,
        (digest,),
    )
    conn.execute(
        """
        INSERT INTO chapter_narrative_commits
            (novel_id, chapter_number, content_sha256, pipeline_version,
             content_revision, status, memory_status)
        VALUES ('novel-1', 1, ?, 'chapter-narrative-sync:v1', 2, 'committed', 'pending')
        """,
        (digest,),
    )
    _insert_exact_committed_summary(conn, chapter_number=1, digest=digest, revision=2)
    conn.commit()

    prefix = service.compute_canonical_prefix("novel-1", 1)
    assert prefix.formal_ready is True
    assert prefix.canonical_ready is True
    assert prefix.memory_ready is False
    assert prefix.ready is False
    digest_before_epoch_change = prefix.digest
    conn.execute("UPDATE chapter_candidates SET generation_epoch = 88 WHERE id = 'candidate-1'")
    conn.execute(
        "UPDATE chapter_narrative_commits SET memory_status = 'committed' "
        "WHERE novel_id = 'novel-1' AND chapter_number = 1"
    )
    conn.commit()
    after = service.compute_canonical_prefix("novel-1", 1)
    assert after.digest == digest_before_epoch_change
    assert after.memory_ready is True
    assert after.ready is True


def test_candidate_prefix_requires_an_exact_committed_summary_even_without_a_knowledge_row(
    tmp_path,
):
    db, service = _service(tmp_path)
    conn = db.get_connection()
    _, digest = _insert_chapter(conn, 1, "缺少摘要的候选正式正文", revision=1)
    conn.execute(
        "INSERT INTO chapter_candidates "
        "(id, novel_id, chapter_number, title, generation_epoch, status, llm_content, content_revision) "
        "VALUES ('candidate-no-summary', 'novel-1', 1, '第一章', 1, 'committed', ?, 1)",
        ("缺少摘要的候选正式正文",),
    )
    conn.execute(
        "INSERT INTO chapter_candidate_formal_commits "
        "(candidate_id, novel_id, chapter_number, chapter_id, content_sha256, content_revision, provenance, sync_status) "
        "VALUES ('candidate-no-summary', 'novel-1', 1, 'chapter-1', ?, 1, 'candidate_commit', 'ready')",
        (digest,),
    )
    conn.execute(
        "INSERT INTO chapter_narrative_commits "
        "(novel_id, chapter_number, content_sha256, pipeline_version, content_revision, status, memory_status) "
        "VALUES ('novel-1', 1, ?, 'chapter-narrative-sync:v1', 1, 'committed', 'committed')",
        (digest,),
    )
    conn.commit()

    prefix = service.compute_canonical_prefix("novel-1", 1)

    assert prefix.canonical_ready is False
    assert prefix.memory_ready is False
    assert "canonical_summary:chapter:1" in prefix.blockers


def test_candidate_prefix_rejects_not_required_memory_with_an_exact_summary(tmp_path):
    db, service = _service(tmp_path)
    conn = db.get_connection()
    _, digest = _insert_chapter(conn, 1, "not-required 不能跨过 Memory Barrier", revision=1)
    conn.execute(
        "INSERT INTO chapter_candidates "
        "(id, novel_id, chapter_number, title, generation_epoch, status, llm_content, content_revision) "
        "VALUES ('candidate-not-required', 'novel-1', 1, '第一章', 1, 'committed', ?, 1)",
        ("not-required 不能跨过 Memory Barrier",),
    )
    conn.execute(
        "INSERT INTO chapter_candidate_formal_commits "
        "(candidate_id, novel_id, chapter_number, chapter_id, content_sha256, content_revision, provenance, sync_status) "
        "VALUES ('candidate-not-required', 'novel-1', 1, 'chapter-1', ?, 1, 'candidate_commit', 'ready')",
        (digest,),
    )
    _insert_exact_committed_summary(conn, chapter_number=1, digest=digest, revision=1)
    conn.execute(
        "INSERT INTO chapter_narrative_commits "
        "(novel_id, chapter_number, content_sha256, pipeline_version, content_revision, status, memory_status) "
        "VALUES ('novel-1', 1, ?, 'chapter-narrative-sync:v1', 1, 'committed', 'not_required')",
        (digest,),
    )
    conn.commit()

    prefix = service.compute_canonical_prefix("novel-1", 1)

    assert prefix.canonical_ready is True
    assert prefix.memory_ready is False
    assert "memory:chapter:1" in prefix.blockers


def test_candidate_prefix_ignores_a_wrong_narrative_pipeline_version(tmp_path):
    db, service = _service(tmp_path)
    conn = db.get_connection()
    _, digest = _insert_chapter(conn, 1, "候选正式正文", revision=2)
    conn.execute(
        "INSERT INTO chapter_candidates "
        "(id, novel_id, chapter_number, title, generation_epoch, status, llm_content, content_revision) "
        "VALUES ('candidate-pipeline', 'novel-1', 1, '第一章', 1, 'committed', ?, 2)",
        ("候选正式正文",),
    )
    conn.execute(
        "INSERT INTO chapter_candidate_formal_commits "
        "(candidate_id, novel_id, chapter_number, chapter_id, content_sha256, content_revision, provenance, sync_status) "
        "VALUES ('candidate-pipeline', 'novel-1', 1, 'chapter-1', ?, 2, 'candidate_commit', 'ready')",
        (digest,),
    )
    _insert_exact_committed_summary(conn, chapter_number=1, digest=digest, revision=2)
    conn.execute(
        "INSERT INTO chapter_narrative_commits "
        "(novel_id, chapter_number, content_sha256, pipeline_version, content_revision, status, memory_status) "
        "VALUES ('novel-1', 1, ?, 'wrong-pipeline', 2, 'committed', 'committed')",
        (digest,),
    )
    conn.commit()

    prefix = service.compute_canonical_prefix("novel-1", 1)

    assert prefix.canonical_ready is False
    assert "canonical:chapter:1" in prefix.blockers


def test_reconciliation_fails_closed_when_formal_head_advances_after_plan_boundary(tmp_path):
    db, service = _service(tmp_path)
    conn = db.get_connection()
    _, digest1 = _insert_chapter(conn, 1, "历史正文")
    conn.execute(
        "INSERT INTO pre_candidate_formal_history "
        "(novel_id, chapter_number, chapter_id, content_sha256, content_revision) "
        "VALUES ('novel-1', 1, 'chapter-1', ?, 1)",
        (digest1,),
    )
    conn.execute(
        "INSERT INTO outline_plan_revisions "
        "(id, novel_id, revision, status, digest, canonical_prefix_digest, canonical_boundary_json, sealed_at) "
        "VALUES ('head-1-plan', 'novel-1', 1, 'ready_for_review', 'plan-head-1', ?, '{\"formal_head\": 1}', CURRENT_TIMESTAMP)",
        (service.compute_canonical_prefix("novel-1", 1).digest,),
    )
    conn.commit()
    _insert_chapter(conn, 2, "后来新增的正式正文")
    conn.execute(
        "INSERT INTO pre_candidate_formal_history "
        "(novel_id, chapter_number, chapter_id, content_sha256, content_revision) "
        "VALUES ('novel-1', 2, 'chapter-2', ?, 1)",
        (_content("后来新增的正式正文")[1],),
    )
    conn.commit()

    report = service.reconcile_plan_boundary(
        novel_id="novel-1", plan_revision_id="head-1-plan"
    )

    assert report.expected_formal_head == 1
    assert report.actual_formal_head == 2
    assert report.status == PlanReconciliationStatus.AUTHOR_DECISION_REQUIRED
    assert any("formal:head" in blocker for blocker in report.blockers)


def test_reconciliation_rejects_unproven_completed_tail_and_keeps_its_blocker(tmp_path):
    """A raw completed chapter is not a Formal authority proof."""

    db, service = _service(tmp_path)
    conn = db.get_connection()
    _, digest1 = _insert_chapter(conn, 1, "已证明历史")
    conn.execute(
        "INSERT INTO pre_candidate_formal_history "
        "(novel_id, chapter_number, chapter_id, content_sha256, content_revision) "
        "VALUES ('novel-1', 1, 'chapter-1', ?, 1)",
        (digest1,),
    )
    conn.execute(
        "INSERT INTO outline_plan_revisions "
        "(id, novel_id, revision, status, digest, canonical_prefix_digest, canonical_boundary_json, sealed_at) "
        "VALUES ('head-1-plan', 'novel-1', 1, 'ready_for_review', 'plan-head-1', ?, '{\"formal_head\": 1}', CURRENT_TIMESTAMP)",
        (service.compute_canonical_prefix("novel-1", 1).digest,),
    )
    conn.commit()
    _insert_chapter(conn, 2, "没有 Formal commit 的 completed 正文")
    conn.commit()

    report = service.reconcile_plan_boundary(
        novel_id="novel-1", plan_revision_id="head-1-plan"
    )

    assert report.status == PlanReconciliationStatus.AUTHOR_DECISION_REQUIRED
    assert "formal:chapter:2" in report.blockers


def test_reconciliation_distinguishes_aligned_repairable_and_hard_conflict(tmp_path):
    db, service = _service(tmp_path)
    conn = db.get_connection()
    _, digest = _insert_chapter(conn, 1, "历史正文")
    conn.execute(
        """
        INSERT INTO pre_candidate_formal_history
            (novel_id, chapter_number, chapter_id, content_sha256, content_revision)
        VALUES ('novel-1', 1, 'chapter-1', ?, 1)
        """,
        (digest,),
    )
    _insert_exact_committed_aftermath(
        conn, chapter_number=1, digest=digest, revision=1
    )
    conn.commit()
    prefix = service.compute_canonical_prefix("novel-1", 1)

    def insert_plan(plan_id: str, revision: int, formal_head: int, expected_digest: str):
        conn.execute(
            """
            INSERT INTO outline_plan_revisions
                (id, novel_id, revision, status, digest, canonical_prefix_digest,
                 canonical_boundary_json, sealed_at)
            VALUES (?, 'novel-1', ?, 'ready_for_review', ?, ?, ?, CURRENT_TIMESTAMP)
            """,
            (
                plan_id,
                revision,
                f"plan-{plan_id}",
                expected_digest,
                json.dumps({"formal_head": formal_head}),
            ),
        )
        conn.commit()

    insert_plan("aligned", 1, 1, prefix.digest)
    insert_plan("repairable", 2, 2, prefix.digest)
    insert_plan("conflict", 3, 1, "wrong-prefix")

    aligned = service.reconcile_plan_boundary(
        novel_id="novel-1", plan_revision_id="aligned"
    )
    repairable = service.reconcile_plan_boundary(
        novel_id="novel-1", plan_revision_id="repairable"
    )
    conflict = service.reconcile_plan_boundary(
        novel_id="novel-1", plan_revision_id="conflict"
    )

    assert aligned.status == PlanReconciliationStatus.ALIGNED
    assert repairable.status == PlanReconciliationStatus.AUTHOR_DECISION_REQUIRED
    assert conflict.status == PlanReconciliationStatus.AUTHOR_DECISION_REQUIRED
