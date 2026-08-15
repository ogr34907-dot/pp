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
    assert repairable.status == PlanReconciliationStatus.REPAIRABLE
    assert conflict.status == PlanReconciliationStatus.AUTHOR_DECISION_REQUIRED
