"""Tail regeneration archives all chapter-derived facts before a new epoch starts."""

import asyncio
import hashlib
import json
import sqlite3
import pytest

from domain.novel.candidate_chapter import RunMode
from domain.structure.outline_contract import OutlinePayload
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.chapter_candidate_repository import (
    ChapterCandidateRepository,
)
from infrastructure.persistence.database.outline_contract_repository import (
    OutlineContractRepository,
)
from infrastructure.persistence.database.plan_projection_writer import (
    PlanProjectionWriter,
)
from infrastructure.persistence.database.story_node_repository import StoryNodeRepository
from application.blueprint.services.outline_continuity_review_service import (
    OutlineContinuityReviewService,
)
from application.engine.services.worldline_regeneration_service import (
    WorldlineRegenerationError,
    WorldlineRegenerationService,
)
from domain.structure.outline_continuity import (
    ContinuityDecision,
    ContinuityReviewReport,
    ContinuityReviewScope,
    ContinuityReviewState,
)
from infrastructure.persistence.database.outline_continuity_review_repository import (
    OutlineContinuityReviewRepository,
)
from application.world.services.chapter_narrative_sync import (
    CHAPTER_NARRATIVE_PIPELINE_VERSION,
)


def _seed(db):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("novel-1", "Worldline Novel", "worldline-novel", 8),
    )
    for number in (1, 2, 3):
        conn.execute(
            """
            INSERT INTO chapters (id, novel_id, number, title, content, content_sha256, content_revision, status)
            VALUES (?, 'novel-1', ?, ?, ?, ?, 1, 'completed')
            """,
            (
                f"chapter-{number}",
                number,
                f"第{number}章",
                f"正文 {number}",
                hashlib.sha256(f"正文 {number}".encode("utf-8")).hexdigest(),
            ),
        )
        conn.execute(
            """
            INSERT INTO narrative_events (event_id, novel_id, chapter_number, event_summary)
            VALUES (?, 'novel-1', ?, ?)
            """,
            (f"event-{number}", number, f"事件 {number}"),
        )
        conn.execute(
            """
            INSERT INTO memory_atoms (id, novel_id, entity_id, chapter_number, status, payload_json)
            VALUES (?, 'novel-1', 'hero', ?, 'canonical', '{}')
            """,
            (f"atom-{number}", number),
        )
    conn.commit()
    # These fixtures model an explicitly imported pre-Candidate formal prefix,
    # rather than silently treating arbitrary completed rows as authority.
    ChapterCandidateRepository(db).import_legacy_formal_history("novel-1")


def _replace_last_legacy_formal_with_candidate(db) -> None:
    """Model a mixed legacy/Candidate Formal prefix with immutable candidate prose."""

    conn = db.get_connection()
    content = "正文 3"
    content_sha256 = hashlib.sha256(content.encode("utf-8")).hexdigest()
    conn.execute(
        "DELETE FROM pre_candidate_formal_history "
        "WHERE novel_id = 'novel-1' AND chapter_number = 3"
    )
    conn.execute(
        """
        INSERT INTO chapter_candidates
            (id, novel_id, chapter_number, title, generation_epoch, status,
             llm_content, content_revision, formal_chapter_id)
        VALUES ('candidate-3', 'novel-1', 3, '第3章', 0, 'committed', ?, 1, 'chapter-3')
        """,
        (content,),
    )
    conn.execute(
        """
        INSERT INTO chapter_candidate_versions
            (id, candidate_id, content_revision, content, source)
        VALUES ('candidate-version-3', 'candidate-3', 1, ?, 'llm')
        """,
        (content,),
    )
    conn.execute(
        """
        INSERT INTO chapter_candidate_formal_commits
            (candidate_id, novel_id, chapter_number, chapter_id, sync_status,
             content_sha256, content_revision)
        VALUES ('candidate-3', 'novel-1', 3, 'chapter-3', 'ready', ?, 1)
        """,
        (content_sha256,),
    )
    conn.commit()


def _mark_manifest_authority(db) -> None:
    """Make a legal minimal manifest Head for Worldline cutover gates."""
    from domain.structure.outline_contract import OutlinePayload
    from domain.structure.outline_plan import OutlinePlanItem
    from infrastructure.persistence.database.outline_contract_repository import (
        OutlineContractRepository,
    )

    contracts = OutlineContractRepository(db)
    root = contracts.ensure_root("novel-1")
    draft = contracts.save_draft(
        root.id,
        OutlinePayload(
            title="Worldline root",
            narrative_text="A complete premise",
            creative_goal="Reach the irreversible ending",
            entry_state="start",
            exit_state="end",
        ),
    )
    published = contracts.publish_and_sync(
        root.id, expected_revision=draft.draft.revision, idempotency_key="worldline-root"
    )
    conn = db.get_connection()
    version = conn.execute(
        "SELECT active_version_id, digest FROM outline_contracts "
        "JOIN outline_contract_versions ON outline_contract_versions.id = active_version_id "
        "WHERE outline_contracts.id = ?",
        (published.id,),
    ).fetchone()
    plan = contracts.create_plan_draft(
        novel_id="novel-1",
        items=(
            OutlinePlanItem(
                logical_node_id=published.id,
                version_id=str(version[0]),
                version_digest=str(version[1]),
                level=published.level,
                sibling_index=0,
            ),
        ),
        canonical_prefix_digest="",
        canonical_boundary={"formal_head": 3},
    )
    plan = contracts.seal_plan_revision(plan.id)
    conn.execute(
        "UPDATE outline_planning_heads SET authority_mode='manifest', "
        "authority_generation=1, projection_generation=1, "
        "active_plan_revision_id=?, active_plan_digest=? WHERE novel_id=?",
        (plan.id, plan.digest, "novel-1"),
    )
    conn.commit()


def _persist_manifest_aftermath(db, through_chapter: int) -> None:
    """Persist exact Canonical and Memory evidence for the retained fixture prefix."""

    conn = db.get_connection()
    conn.execute(
        "INSERT INTO knowledge (id, novel_id) VALUES ('worldline-knowledge', 'novel-1')"
    )
    for chapter_number in range(1, through_chapter + 1):
        chapter = conn.execute(
            "SELECT content_sha256, content_revision FROM chapters "
            "WHERE novel_id = 'novel-1' AND number = ?",
            (chapter_number,),
        ).fetchone()
        assert chapter is not None
        conn.execute(
            """
            INSERT INTO chapter_narrative_commits
                (novel_id, chapter_number, content_sha256, pipeline_version,
                 content_revision, status, memory_status)
            VALUES (?, ?, ?, ?, ?, 'committed', 'committed')
            """,
            (
                "novel-1",
                chapter_number,
                chapter["content_sha256"],
                CHAPTER_NARRATIVE_PIPELINE_VERSION,
                chapter["content_revision"],
            ),
        )
        conn.execute(
            """
            INSERT INTO chapter_summaries
                (id, knowledge_id, chapter_number, summary, source_content_sha256,
                 source_content_revision, pipeline_version, sync_status)
            VALUES (?, 'worldline-knowledge', ?, 'trusted summary', ?, ?, ?, 'committed')
            """,
            (
                f"worldline-summary-{chapter_number}",
                chapter_number,
                chapter["content_sha256"],
                chapter["content_revision"],
                CHAPTER_NARRATIVE_PIPELINE_VERSION,
            ),
        )
    conn.commit()


def _materialize_manifest_worldline(
    db,
    *,
    prepare_aftermath: bool = True,
    include_future_branch: bool = False,
):
    """Build a complete bound Manifest whose chapter tail can be rebased."""

    if prepare_aftermath:
        _persist_manifest_aftermath(db, through_chapter=3)
    _mark_manifest_authority(db)
    contracts = OutlineContractRepository(db)
    draft = contracts.clone_active_plan_draft("novel-1", replan_start_chapter=1)
    root = next(item for item in draft.items if item.level.value == "outline")
    part_payloads = [
        OutlinePayload(
            title="Part",
            narrative_text="Part narrative",
            creative_goal="Part goal",
            entry_state="start",
            exit_state="part-end",
            conflicts=["part-conflict"],
            state_changes={"hero": [{"change": "part-end"}]},
            handoff_conditions=["part-end-ready"],
            chapter_start=1,
            chapter_end=3,
        )
    ]
    if include_future_branch:
        part_payloads.append(
            OutlinePayload(
                title="Future Part",
                narrative_text="Future Part narrative",
                creative_goal="Future Part goal",
                entry_state="part-end",
                exit_state="end",
                conflicts=["future-part-conflict"],
                state_changes={"hero": [{"change": "end"}]},
                handoff_conditions=["end-ready"],
                chapter_start=4,
                chapter_end=4,
            )
        )
    expanded = contracts.replace_draft_cohort_payloads(
        plan_revision_id=draft.id,
        parent_logical_node_id=root.logical_node_id,
        payloads=tuple(part_payloads),
    )
    parent = next(
        item
        for item in expanded.items
        if item.parent_logical_node_id == root.logical_node_id
        and item.sibling_index == 0
    )
    for title, entry_state, exit_state in (
        ("Volume", "part-end", "volume-end"),
        ("Act", "volume-end", "act-end"),
    ):
        expanded = contracts.replace_draft_cohort_payloads(
            plan_revision_id=expanded.id,
            parent_logical_node_id=parent.logical_node_id,
            payloads=(
                OutlinePayload(
                    title=title,
                    narrative_text=f"{title} narrative",
                    creative_goal=f"{title} goal",
                    entry_state=entry_state,
                    exit_state=exit_state,
                    chapter_start=1,
                    chapter_end=3,
                ),
            ),
        )
        parent = next(
            item
            for item in expanded.items
            if item.parent_logical_node_id == parent.logical_node_id
        )
    expanded = contracts.replace_draft_cohort_payloads(
        plan_revision_id=expanded.id,
        parent_logical_node_id=parent.logical_node_id,
        payloads=(
            OutlinePayload(
                title="Chapter 1",
                narrative_text="Chapter 1 narrative",
                creative_goal="Chapter 1 goal",
                entry_state="volume-end",
                exit_state="turn-1",
                conflicts=["chapter-1-conflict"],
                state_changes={"hero": [{"change": "turn-1"}]},
                handoff_conditions=["turn-1-ready"],
                chapter_start=1,
                chapter_end=1,
            ),
            OutlinePayload(
                title="Chapter 2",
                narrative_text="Chapter 2 narrative",
                creative_goal="Chapter 2 goal",
                entry_state="turn-1",
                exit_state="turn-2",
                conflicts=["chapter-2-conflict"],
                state_changes={"hero": [{"change": "turn-2"}]},
                handoff_conditions=["turn-2-ready"],
                chapter_start=2,
                chapter_end=2,
            ),
            OutlinePayload(
                title="Chapter 3",
                narrative_text="Chapter 3 narrative",
                creative_goal="Chapter 3 goal",
                entry_state="turn-2",
                exit_state="act-end",
                conflicts=["chapter-3-conflict"],
                state_changes={"hero": [{"change": "act-end"}]},
                handoff_conditions=["act-end-ready"],
                chapter_start=3,
                chapter_end=3,
            ),
        ),
    )
    if include_future_branch:
        future_parent = next(
            item
            for item in expanded.items
            if item.parent_logical_node_id == root.logical_node_id
            and item.sibling_index == 1
        )
        for title, entry_state, exit_state in (
            ("Future Volume", "part-end", "future-volume-end"),
            ("Future Act", "future-volume-end", "future-act-end"),
        ):
            expanded = contracts.replace_draft_cohort_payloads(
                plan_revision_id=expanded.id,
                parent_logical_node_id=future_parent.logical_node_id,
                payloads=(
                    OutlinePayload(
                        title=title,
                        narrative_text=f"{title} narrative",
                        creative_goal=f"{title} goal",
                        entry_state=entry_state,
                        exit_state=exit_state,
                        chapter_start=4,
                        chapter_end=4,
                    ),
                ),
            )
            future_parent = next(
                item
                for item in expanded.items
                if item.parent_logical_node_id == future_parent.logical_node_id
            )
        expanded = contracts.replace_draft_cohort_payloads(
            plan_revision_id=expanded.id,
            parent_logical_node_id=future_parent.logical_node_id,
            payloads=(
                OutlinePayload(
                    title="Future Chapter 4",
                    narrative_text="Future Chapter 4 narrative",
                    creative_goal="Future Chapter 4 goal",
                    entry_state="future-volume-end",
                    exit_state="future-act-end",
                    conflicts=["future-chapter-conflict"],
                    state_changes={"hero": [{"change": "future-act-end"}]},
                    handoff_conditions=["future-act-end-ready"],
                    chapter_start=4,
                    chapter_end=4,
                ),
            ),
        )
    _install_required_pass_receipt(db, contracts, expanded)
    conn = db.get_connection()
    conn.execute("BEGIN IMMEDIATE")
    try:
        target = contracts._seal_plan_revision_locked(
            conn, expanded.id, clear_working_plan=False
        )
        head = conn.execute(
            """
            SELECT active_plan_revision_id, active_plan_digest,
                   authority_generation, projection_generation
            FROM outline_planning_heads WHERE novel_id = 'novel-1'
            """
        ).fetchone()
        asyncio.run(
            PlanProjectionWriter(StoryNodeRepository(db)).apply_bound_projection(
                conn,
                novel_id="novel-1",
                plan_revision_id=target.id,
                expected_active_plan_revision_id=head["active_plan_revision_id"],
                expected_active_plan_digest=str(head["active_plan_digest"] or ""),
                expected_authority_generation=int(head["authority_generation"] or 0),
                expected_projection_generation=int(head["projection_generation"] or 0),
                expected_working_plan_revision_id=target.id,
            )
        )
        conn.commit()
    except BaseException:
        conn.rollback()
        raise
    return target


def _install_required_pass_receipt(db, contracts, plan) -> None:
    reviews = OutlineContinuityReviewRepository(db)
    service = OutlineContinuityReviewService(contracts, reviews, object(), db)
    review_ids = []
    scope_fingerprints = []
    for parent_logical_node_id in contracts.required_narrative_review_scope_parents(
        plan.id
    ):
        parent = next(
            item
            for item in plan.items
            if item.logical_node_id == parent_logical_node_id
        )
        children = tuple(
            item
            for item in plan.items
            if item.parent_logical_node_id == parent.logical_node_id
        )
        scope = ContinuityReviewScope(
            parent_logical_node_id=parent.logical_node_id,
            level=parent.level.child_level.value,
            parent_version_id=parent.version_id,
            parent_version_digest=parent.version_digest,
            children=tuple(
                {
                    "logical_node_id": item.logical_node_id,
                    "version_id": item.version_id,
                    "version_digest": item.version_digest,
                    "sibling_index": item.sibling_index,
                }
                for item in children
            ),
        )
        scope_fingerprint = service.current_scope_fingerprint(
            plan.id, parent.logical_node_id, plan.digest
        )
        run = reviews.begin(
            novel_id=plan.novel_id,
            plan_revision_id=plan.id,
            scope=scope,
            plan_digest=plan.digest,
            scope_fingerprint=scope_fingerprint,
        )
        completed = reviews.complete(
            run["id"],
            report=ContinuityReviewReport(
                decision=ContinuityDecision.PASS,
                confidence=1.0,
                scope_fingerprint=scope_fingerprint,
            ),
        )
        review_ids.append(completed["id"])
        scope_fingerprints.append(completed["scope_fingerprint"])
    contracts.set_narrative_review_receipt(
        plan_revision_id=plan.id,
        expected_plan_digest=plan.digest,
        state=ContinuityReviewState.PASS,
        receipt={"action": "pass", "actor": "test"},
        review_ids=tuple(review_ids),
        scope_fingerprints=tuple(scope_fingerprints),
    )


def _seed_canonical_tail(db):
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO bible_timeline_notes "
        "(id, novel_id, event, time_point, description, source_type, chapter_number) "
        "VALUES ('timeline-2', 'novel-1', '旧时间线', '第2章', '旧事实', 'chapter_aftermath', 2)"
    )
    conn.execute(
        "INSERT INTO bible_timeline_notes "
        "(id, novel_id, event, time_point, description, source_type, chapter_number) "
        "VALUES ('timeline-authored-2', 'novel-1', '作者设定', '第2章', '固定背景', 'bible', 2)"
    )
    conn.execute(
        "INSERT INTO character_states "
        "(character_id, novel_id, current_state_summary, last_updated_chapter) "
        "VALUES ('hero', 'novel-1', '旧状态', 2)"
    )
    conn.commit()


def _seed_runtime_summary_caches(db):
    conn = db.get_connection()
    affected_state = {
        "status": "committed",
        "chapter_start": 1,
        "chapter_end": 3,
        "source_chapter_numbers": [1, 2, 3],
        "source_version": "affected-source",
        "pipeline_version": "node-summary/v1",
    }
    retained_state = {
        "status": "committed",
        "chapter_start": 1,
        "chapter_end": 1,
        "source_chapter_numbers": [1],
        "source_version": "retained-source",
        "pipeline_version": "node-summary/v1",
    }
    for node_id, number, metadata in (
        (
            "act-affected",
            1,
            {"runtime.summary": "retired tail facts", "runtime.summary_state": affected_state},
        ),
        (
            "act-retained",
            2,
            {"runtime.summary": "retained prefix facts", "runtime.summary_state": retained_state},
        ),
    ):
        conn.execute(
            "INSERT INTO story_nodes "
            "(id, novel_id, node_type, number, title, order_index, chapter_start, chapter_end, metadata) "
            "VALUES (?, 'novel-1', 'act', ?, ?, ?, 1, 3, ?)",
            (node_id, number, node_id, number, json.dumps(metadata)),
        )
    conn.commit()


def test_worldline_archive_stales_only_runtime_caches_with_retired_sources(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline-runtime-summary-cache.db"))
    _seed(db)
    _seed_runtime_summary_caches(db)
    service = WorldlineRegenerationService(db)

    preview = service.preview("novel-1", start_chapter=2, target_chapters=6)
    service.execute("novel-1", preview_token=preview.token, run_mode="chapter_review")

    conn = db.get_connection()
    affected = json.loads(
        conn.execute("SELECT metadata FROM story_nodes WHERE id = 'act-affected'").fetchone()[0]
    )
    retained = json.loads(
        conn.execute("SELECT metadata FROM story_nodes WHERE id = 'act-retained'").fetchone()[0]
    )
    assert "runtime.summary" not in affected
    assert "runtime.summary_state" not in affected
    assert affected["runtime.summary_invalidated_from_chapter"] == 2
    assert retained["runtime.summary_state"]["status"] == "committed"
    assert "runtime.summary_invalidated_from_chapter" not in retained


def test_manifest_worldline_preview_is_rejected_without_creating_a_preview(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline-manifest-preview.db"))
    _seed(db)
    _mark_manifest_authority(db)

    with pytest.raises(WorldlineRegenerationError, match="manifest-aware"):
        WorldlineRegenerationService(db).preview(
            "novel-1", start_chapter=2, target_chapters=6
        )

    assert db.get_connection().execute(
        "SELECT COUNT(*) FROM worldline_regeneration_previews WHERE novel_id = 'novel-1'"
    ).fetchone()[0] == 0


def test_manifest_worldline_execute_rejects_a_legacy_preview_without_archiving(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline-manifest-execute.db"))
    _seed(db)
    service = WorldlineRegenerationService(db)
    preview = service.preview("novel-1", start_chapter=2, target_chapters=6)
    _mark_manifest_authority(db)

    with pytest.raises(WorldlineRegenerationError, match="manifest-aware"):
        service.execute(
            "novel-1", preview_token=preview.token, run_mode="chapter_review"
        )

    conn = db.get_connection()
    assert conn.execute(
        "SELECT COUNT(*) FROM chapters WHERE novel_id = 'novel-1'"
    ).fetchone()[0] == 3
    assert conn.execute(
        "SELECT consumed_at FROM worldline_regeneration_previews WHERE token = ?",
        (preview.token,),
    ).fetchone()[0] is None
    assert conn.execute(
        "SELECT COUNT(*) FROM worldline_archives WHERE novel_id = 'novel-1'"
    ).fetchone()[0] == 0


def test_manifest_worldline_restore_rejects_a_legacy_archive_without_mutating_tail(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline-manifest-restore.db"))
    _seed(db)
    service = WorldlineRegenerationService(db)
    preview = service.preview("novel-1", start_chapter=2, target_chapters=6)
    archived = service.execute(
        "novel-1", preview_token=preview.token, run_mode="chapter_review"
    )
    _mark_manifest_authority(db)

    with pytest.raises(WorldlineRegenerationError, match="manifest-aware"):
        service.restore(
            "novel-1", archive_id=archived.archive_id, run_mode="chapter_review"
        )

    conn = db.get_connection()
    assert conn.execute(
        "SELECT GROUP_CONCAT(number, ',') FROM chapters WHERE novel_id = 'novel-1'"
    ).fetchone()[0] == "1"
    assert conn.execute(
        "SELECT status FROM worldline_archives WHERE id = ?", (archived.archive_id,)
    ).fetchone()[0] == "archived"
    assert conn.execute(
        "SELECT COUNT(*) FROM worldline_archives WHERE novel_id = 'novel-1'"
    ).fetchone()[0] == 1


def test_legacy_worldline_remains_available_before_manifest_rebase_exists(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline-legacy-control.db"))
    _seed(db)
    service = WorldlineRegenerationService(db)

    preview = service.preview("novel-1", start_chapter=2, target_chapters=6)
    result = service.execute(
        "novel-1", preview_token=preview.token, run_mode="chapter_review"
    )

    assert result.operation == "regenerate"
    assert db.get_connection().execute(
        "SELECT COUNT(*) FROM chapters WHERE novel_id = 'novel-1'"
    ).fetchone()[0] == 1


def test_manifest_worldline_rebases_and_restores_immutable_head_snapshot(tmp_path):
    """Manifest reset must switch bindings before deleting a physical tail."""

    db = DatabaseConnection(str(tmp_path / "worldline-manifest-rebase.db"))
    _seed(db)
    original = _materialize_manifest_worldline(db)
    service = WorldlineRegenerationService(db)

    preview = service.preview("novel-1", start_chapter=2, target_chapters=6)
    assert preview.operation == "regenerate"
    result = service.execute(
        "novel-1", preview_token=preview.token, run_mode="chapter_review"
    )

    conn = db.get_connection()
    rebased_head = conn.execute(
        """
        SELECT active_plan_revision_id, active_plan_digest,
               authority_generation, projection_generation
        FROM outline_planning_heads WHERE novel_id = 'novel-1'
        """
    ).fetchone()
    assert result.archive_id
    assert rebased_head["active_plan_revision_id"] != original.id
    assert int(rebased_head["authority_generation"]) == 3
    assert int(rebased_head["projection_generation"]) == 3
    rebased_plan = conn.execute(
        "SELECT reconciliation_status, reconciliation_report_json "
        "FROM outline_plan_revisions WHERE id = ?",
        (rebased_head["active_plan_revision_id"],),
    ).fetchone()
    assert rebased_plan["reconciliation_status"] == "aligned"
    reconciliation = json.loads(rebased_plan["reconciliation_report_json"])
    assert reconciliation["status"] == "aligned"
    assert reconciliation["canonical_ready"] is True
    assert reconciliation["memory_ready"] is True
    assert conn.execute(
        "SELECT COUNT(*) FROM worldline_manifest_head_snapshots "
        "WHERE archive_id = ? AND active_plan_revision_id = ?",
        (result.archive_id, original.id),
    ).fetchone()[0] == 1
    lineage_tables = {
        row["source_table"]
        for row in conn.execute(
            "SELECT source_table FROM worldline_archive_entries "
            "WHERE archive_id = ?",
            (result.archive_id,),
        ).fetchall()
    }
    assert {
        "outline_plan_revisions",
        "outline_plan_revision_items",
        "outline_plan_projection_bindings",
        "outline_contracts",
        "outline_contract_versions",
        "outline_plan_projections",
    } <= lineage_tables
    archive_metadata = json.loads(
        conn.execute(
            "SELECT metadata_json FROM worldline_archives WHERE id = ?",
            (result.archive_id,),
        ).fetchone()[0]
    )
    assert archive_metadata["source_lineage_digest"]
    assert conn.execute(
        "SELECT GROUP_CONCAT(number, ',') FROM chapters WHERE novel_id = 'novel-1'"
    ).fetchone()[0] == "1"
    assert conn.execute(
        "SELECT GROUP_CONCAT(number, ',') FROM story_nodes "
        "WHERE novel_id = 'novel-1' AND node_type = 'chapter'"
    ).fetchone()[0] == "1"

    restored = service.restore(
        "novel-1", archive_id=result.archive_id, run_mode="chapter_review"
    )
    restored_head = conn.execute(
        "SELECT active_plan_revision_id, active_plan_digest, authority_generation "
        "FROM outline_planning_heads WHERE novel_id = 'novel-1'"
    ).fetchone()
    assert restored.operation == "restore"
    assert restored_head["active_plan_revision_id"] == original.id
    assert restored_head["active_plan_digest"] == original.digest
    assert int(restored_head["authority_generation"]) == 4
    replacement_metadata = json.loads(
        conn.execute(
            "SELECT metadata_json FROM worldline_archives WHERE id = ?",
            (restored.archive_id,),
        ).fetchone()[0]
    )
    assert replacement_metadata["source_lineage_digest"]
    assert conn.execute(
        "SELECT GROUP_CONCAT(number, ',') FROM chapters WHERE novel_id = 'novel-1'"
    ).fetchone()[0] == "1,2,3"
    assert conn.execute(
        "SELECT GROUP_CONCAT(number, ',') FROM story_nodes "
        "WHERE novel_id = 'novel-1' AND node_type = 'chapter'"
    ).fetchone()[0] == "1,2,3"


def test_manifest_worldline_reuses_an_identical_prior_rebase_after_restore(tmp_path):
    """Revisiting one retained boundary must reactivate its sealed revision."""

    db = DatabaseConnection(str(tmp_path / "worldline-manifest-repeat-rebase.db"))
    _seed(db)
    _materialize_manifest_worldline(db)
    service = WorldlineRegenerationService(db)

    first = service.execute(
        "novel-1",
        preview_token=service.preview(
            "novel-1", start_chapter=2, target_chapters=6
        ).token,
        run_mode="chapter_review",
    )
    conn = db.get_connection()
    first_rebased_plan_id = conn.execute(
        "SELECT active_plan_revision_id FROM outline_planning_heads "
        "WHERE novel_id = 'novel-1'"
    ).fetchone()[0]
    service.restore(
        "novel-1", archive_id=first.archive_id, run_mode="chapter_review"
    )

    second = service.execute(
        "novel-1",
        preview_token=service.preview(
            "novel-1", start_chapter=2, target_chapters=6
        ).token,
        run_mode="chapter_review",
    )

    assert second.operation == "regenerate"
    assert conn.execute(
        "SELECT active_plan_revision_id FROM outline_planning_heads "
        "WHERE novel_id = 'novel-1'"
    ).fetchone()[0] == first_rebased_plan_id


def test_manifest_worldline_rebase_keeps_only_retained_chapter_ancestor_closure(
    tmp_path,
):
    db = DatabaseConnection(str(tmp_path / "worldline-manifest-topology-closure.db"))
    _seed(db)
    _materialize_manifest_worldline(db, include_future_branch=True)
    conn = db.get_connection()
    future_nodes = {
        row["id"]
        for row in conn.execute(
            "SELECT id FROM story_nodes WHERE novel_id = 'novel-1' "
            "AND title LIKE 'Future %'"
        ).fetchall()
    }
    assert len(future_nodes) == 4

    service = WorldlineRegenerationService(db)
    result = service.execute(
        "novel-1",
        preview_token=service.preview(
            "novel-1", start_chapter=2, target_chapters=6
        ).token,
        run_mode="chapter_review",
    )

    active_id = conn.execute(
        "SELECT active_plan_revision_id FROM outline_planning_heads "
        "WHERE novel_id = 'novel-1'"
    ).fetchone()[0]
    active_titles = {
        json.loads(row["payload_json"])["title"]
        for row in conn.execute(
            """
            SELECT version.payload_json
            FROM outline_plan_revision_items AS item
            JOIN outline_contract_versions AS version ON version.id = item.version_id
            WHERE item.plan_revision_id = ?
            """,
            (active_id,),
        ).fetchall()
    }
    assert active_titles == {"Worldline root", "Part", "Volume", "Act", "Chapter 1"}
    assert conn.execute(
        """
        SELECT COUNT(*)
        FROM outline_plan_projection_bindings AS binding
        JOIN outline_plan_revision_items AS item ON item.id = binding.plan_revision_item_id
        WHERE item.plan_revision_id = ? AND binding.story_node_id IN (?, ?, ?, ?)
        """,
        (active_id, *sorted(future_nodes)),
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT COUNT(*) FROM story_nodes WHERE id IN (?, ?, ?, ?)",
        tuple(sorted(future_nodes)),
    ).fetchone()[0] == 0
    assert result.archive_id


def test_manifest_worldline_rebase_rejects_an_unready_retained_aftermath(tmp_path):
    """A rebase cannot claim an aligned Manifest boundary before aftermath is ready."""

    db = DatabaseConnection(str(tmp_path / "worldline-manifest-unready-aftermath.db"))
    _seed(db)
    original = _materialize_manifest_worldline(db, prepare_aftermath=False)
    service = WorldlineRegenerationService(db)
    preview = service.preview("novel-1", start_chapter=2, target_chapters=6)

    with pytest.raises(
        WorldlineRegenerationError,
        match="aligned formal boundary",
    ):
        service.execute(
            "novel-1", preview_token=preview.token, run_mode="chapter_review"
        )

    conn = db.get_connection()
    assert conn.execute(
        "SELECT active_plan_revision_id FROM outline_planning_heads "
        "WHERE novel_id = 'novel-1'"
    ).fetchone()[0] == original.id
    assert conn.execute(
        "SELECT GROUP_CONCAT(number, ',') FROM chapters "
        "WHERE novel_id = 'novel-1'"
    ).fetchone()[0] == "1,2,3"
    assert conn.execute(
        "SELECT COUNT(*) FROM worldline_archives WHERE novel_id = 'novel-1'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT consumed_at FROM worldline_regeneration_previews WHERE token = ?",
        (preview.token,),
    ).fetchone()[0] is None


def test_manifest_worldline_rebases_without_legacy_projection_cache(tmp_path):
    """A bound Manifest remains authoritative after its legacy cache is absent."""

    db = DatabaseConnection(str(tmp_path / "worldline-manifest-no-cache.db"))
    _seed(db)
    _materialize_manifest_worldline(db)
    conn = db.get_connection()
    conn.execute("DELETE FROM outline_plan_projections WHERE novel_id = 'novel-1'")
    conn.commit()

    service = WorldlineRegenerationService(db)
    preview = service.preview("novel-1", start_chapter=2, target_chapters=6)
    result = service.execute(
        "novel-1", preview_token=preview.token, run_mode="chapter_review"
    )

    assert result.archive_id
    assert conn.execute(
        "SELECT COUNT(*) FROM worldline_archive_entries "
        "WHERE archive_id = ? AND source_table = 'outline_plan_projections'",
        (result.archive_id,),
    ).fetchone()[0] == 0
    metadata = json.loads(
        conn.execute(
            "SELECT metadata_json FROM worldline_archives WHERE id = ?",
            (result.archive_id,),
        ).fetchone()[0]
    )
    assert metadata["source_lineage_digest"]


def test_manifest_restore_records_replacement_lineage_without_legacy_projection_cache(
    tmp_path,
):
    """Replacement archives retain stable Manifest evidence without a legacy cache."""

    db = DatabaseConnection(str(tmp_path / "worldline-manifest-restore-no-cache.db"))
    _seed(db)
    _materialize_manifest_worldline(db)
    conn = db.get_connection()
    conn.execute("DELETE FROM outline_plan_projections WHERE novel_id = 'novel-1'")
    conn.commit()

    service = WorldlineRegenerationService(db)
    archived = service.execute(
        "novel-1",
        preview_token=service.preview(
            "novel-1", start_chapter=2, target_chapters=6
        ).token,
        run_mode="chapter_review",
    )
    restored = service.restore(
        "novel-1", archive_id=archived.archive_id, run_mode="chapter_review"
    )

    metadata = json.loads(
        conn.execute(
            "SELECT metadata_json FROM worldline_archives WHERE id = ?",
            (restored.archive_id,),
        ).fetchone()[0]
    )
    assert metadata["source_lineage_digest"]
    assert conn.execute(
        "SELECT COUNT(*) FROM worldline_archive_entries "
        "WHERE archive_id = ? AND source_table = 'outline_plan_projections'",
        (restored.archive_id,),
    ).fetchone()[0] == 0


def test_manifest_archive_head_snapshot_cannot_be_mutated_before_restore(tmp_path):
    """Archive Head evidence must reject direct updates before restore."""

    db = DatabaseConnection(str(tmp_path / "worldline-manifest-immutable-head.db"))
    _seed(db)
    original = _materialize_manifest_worldline(db)
    service = WorldlineRegenerationService(db)
    archived = service.execute(
        "novel-1",
        preview_token=service.preview(
            "novel-1", start_chapter=2, target_chapters=6
        ).token,
        run_mode="chapter_review",
    )
    conn = db.get_connection()

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        conn.execute(
            "UPDATE worldline_manifest_head_snapshots "
            "SET authority_generation = authority_generation + 1 "
            "WHERE archive_id = ?",
            (archived.archive_id,),
        )
    conn.rollback()

    restored = service.restore(
        "novel-1", archive_id=archived.archive_id, run_mode="chapter_review"
    )
    assert restored.operation == "restore"
    assert conn.execute(
        "SELECT active_plan_revision_id FROM outline_planning_heads "
        "WHERE novel_id = 'novel-1'"
    ).fetchone()[0] == original.id


@pytest.mark.parametrize("replacement", ("delete", "replace"))
def test_manifest_archive_head_snapshot_cannot_be_replaced_before_restore(
    tmp_path, replacement
):
    """Immutable Head evidence cannot be removed and inserted back under one key."""

    db = DatabaseConnection(
        str(tmp_path / f"worldline-manifest-snapshot-replace-{replacement}.db")
    )
    _seed(db)
    _materialize_manifest_worldline(db)
    service = WorldlineRegenerationService(db)
    archived = service.execute(
        "novel-1",
        preview_token=service.preview(
            "novel-1", start_chapter=2, target_chapters=6
        ).token,
        run_mode="chapter_review",
    )
    conn = db.get_connection()

    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        if replacement == "delete":
            conn.execute(
                "DELETE FROM worldline_manifest_head_snapshots WHERE archive_id = ?",
                (archived.archive_id,),
            )
        else:
            conn.execute(
                """
                INSERT OR REPLACE INTO worldline_manifest_head_snapshots
                    (archive_id, novel_id, active_plan_revision_id,
                     active_plan_digest, authority_generation,
                     projection_generation, binding_digest, created_at)
                SELECT archive_id, novel_id, active_plan_revision_id,
                       active_plan_digest, authority_generation,
                       projection_generation, binding_digest, created_at
                FROM worldline_manifest_head_snapshots
                WHERE archive_id = ?
                """,
                (archived.archive_id,),
            )
    conn.rollback()

    assert conn.execute(
        "SELECT COUNT(*) FROM worldline_manifest_head_snapshots WHERE archive_id = ?",
        (archived.archive_id,),
    ).fetchone()[0] == 1


def test_manifest_head_snapshot_cascades_when_its_archive_is_deleted(tmp_path):
    """Snapshot immutability must not block its declared archive lifecycle."""

    db = DatabaseConnection(str(tmp_path / "worldline-manifest-snapshot-cascade.db"))
    _seed(db)
    _materialize_manifest_worldline(db)
    service = WorldlineRegenerationService(db)
    archived = service.execute(
        "novel-1",
        preview_token=service.preview(
            "novel-1", start_chapter=2, target_chapters=6
        ).token,
        run_mode="chapter_review",
    )
    conn = db.get_connection()

    conn.execute("DELETE FROM worldline_archives WHERE id = ?", (archived.archive_id,))
    conn.commit()

    assert conn.execute(
        "SELECT COUNT(*) FROM worldline_manifest_head_snapshots WHERE archive_id = ?",
        (archived.archive_id,),
    ).fetchone()[0] == 0


def test_manifest_restore_without_immutable_head_snapshot_fails_closed(tmp_path):
    """A Manifest archive cannot fall back to raw legacy StoryNode payloads."""

    db = DatabaseConnection(str(tmp_path / "worldline-manifest-missing-snapshot.db"))
    _seed(db)
    original = _materialize_manifest_worldline(db)
    service = WorldlineRegenerationService(db)
    preview = service.preview("novel-1", start_chapter=2, target_chapters=6)
    archived = service.execute(
        "novel-1", preview_token=preview.token, run_mode="chapter_review"
    )
    conn = db.get_connection()
    conn.execute(
        "DROP TRIGGER trg_worldline_manifest_head_snapshot_immutable_delete"
    )
    conn.execute(
        "DELETE FROM worldline_manifest_head_snapshots WHERE archive_id = ?",
        (archived.archive_id,),
    )
    conn.commit()

    with pytest.raises(WorldlineRegenerationError, match="immutable Head snapshot"):
        service.restore(
            "novel-1", archive_id=archived.archive_id, run_mode="chapter_review"
        )

    assert conn.execute(
        "SELECT active_plan_revision_id FROM outline_planning_heads "
        "WHERE novel_id = 'novel-1'"
    ).fetchone()[0] != original.id
    assert conn.execute(
        "SELECT status FROM worldline_archives WHERE id = ?",
        (archived.archive_id,),
    ).fetchone()[0] == "archived"


@pytest.mark.parametrize(
    "source_table",
    (
        "chapters",
        "chapter_candidate_versions",
        "chapter_candidate_formal_commits",
    ),
)
def test_manifest_restore_rejects_tampered_archived_formal_source_data(
    tmp_path, source_table
):
    """Restore must verify archived prose and Candidate Formal evidence."""

    db = DatabaseConnection(str(tmp_path / "worldline-manifest-tampered-lineage.db"))
    _seed(db)
    _replace_last_legacy_formal_with_candidate(db)
    original = _materialize_manifest_worldline(db)
    service = WorldlineRegenerationService(db)
    archived = service.execute(
        "novel-1",
        preview_token=service.preview(
            "novel-1", start_chapter=2, target_chapters=6
        ).token,
        run_mode="chapter_review",
    )
    conn = db.get_connection()
    entry = conn.execute(
        """
        SELECT id, payload_json FROM worldline_archive_entries
        WHERE archive_id = ? AND source_table = ?
        ORDER BY source_key LIMIT 1
        """,
        (archived.archive_id, source_table),
    ).fetchone()
    payload = json.loads(entry["payload_json"])
    payload["updated_at"] = "tampered-lineage"
    conn.execute(
        "UPDATE worldline_archive_entries SET payload_json = ? WHERE id = ?",
        (json.dumps(payload, ensure_ascii=False, sort_keys=True), entry["id"]),
    )
    conn.commit()

    with pytest.raises(WorldlineRegenerationError, match="lineage"):
        service.restore(
            "novel-1", archive_id=archived.archive_id, run_mode="chapter_review"
        )

    assert conn.execute(
        "SELECT active_plan_revision_id FROM outline_planning_heads "
        "WHERE novel_id = 'novel-1'"
    ).fetchone()[0] != original.id


def test_manifest_restore_rejects_lineage_tamper_even_when_metadata_digest_is_recomputed(
    tmp_path,
):
    """Mutable archive metadata cannot authorize a newly recomputed lineage."""

    db = DatabaseConnection(str(tmp_path / "worldline-manifest-recomputed-lineage.db"))
    _seed(db)
    original = _materialize_manifest_worldline(db)
    service = WorldlineRegenerationService(db)
    archived = service.execute(
        "novel-1",
        preview_token=service.preview(
            "novel-1", start_chapter=2, target_chapters=6
        ).token,
        run_mode="chapter_review",
    )
    conn = db.get_connection()
    entry = conn.execute(
        """
        SELECT id, payload_json FROM worldline_archive_entries
        WHERE archive_id = ? AND source_table = 'outline_contracts'
        ORDER BY source_key LIMIT 1
        """,
        (archived.archive_id,),
    ).fetchone()
    payload = json.loads(entry["payload_json"])
    payload["updated_at"] = "recomputed-lineage-tamper"
    conn.execute(
        "UPDATE worldline_archive_entries SET payload_json = ? WHERE id = ?",
        (json.dumps(payload, ensure_ascii=False, sort_keys=True), entry["id"]),
    )

    lineage: dict[str, list[dict]] = {}
    for table in service._MANIFEST_LINEAGE_DIGEST_TABLES:
        rows = conn.execute(
            "SELECT payload_json FROM worldline_archive_entries "
            "WHERE archive_id = ? AND source_table = ?",
            (archived.archive_id, table),
        ).fetchall()
        payloads = [json.loads(row["payload_json"] or "{}") for row in rows]
        lineage[table] = sorted(
            payloads,
            key=lambda value: json.dumps(
                value, ensure_ascii=False, sort_keys=True, default=str
            ),
        )
    recomputed_digest = hashlib.sha256(
        json.dumps(
            lineage,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            default=str,
        ).encode("utf-8")
    ).hexdigest()
    metadata = json.loads(
        conn.execute(
            "SELECT metadata_json FROM worldline_archives WHERE id = ?",
            (archived.archive_id,),
        ).fetchone()[0]
    )
    metadata["source_lineage_digest"] = recomputed_digest
    conn.execute(
        "UPDATE worldline_archives SET metadata_json = ? WHERE id = ?",
        (
            json.dumps(metadata, ensure_ascii=False, sort_keys=True),
            archived.archive_id,
        ),
    )
    conn.commit()

    with pytest.raises(WorldlineRegenerationError, match="lineage"):
        service.restore(
            "novel-1", archive_id=archived.archive_id, run_mode="chapter_review"
        )

    assert conn.execute(
        "SELECT active_plan_revision_id FROM outline_planning_heads "
        "WHERE novel_id = 'novel-1'"
    ).fetchone()[0] != original.id


def test_manifest_restore_rejects_metadata_that_disagrees_with_head_snapshot(tmp_path):
    """Restore must compare immutable snapshot fields against archive metadata."""

    db = DatabaseConnection(str(tmp_path / "worldline-manifest-pretampered-head.db"))
    _seed(db)
    original = _materialize_manifest_worldline(db)
    service = WorldlineRegenerationService(db)
    archived = service.execute(
        "novel-1",
        preview_token=service.preview(
            "novel-1", start_chapter=2, target_chapters=6
        ).token,
        run_mode="chapter_review",
    )
    conn = db.get_connection()
    metadata = json.loads(
        conn.execute(
            "SELECT metadata_json FROM worldline_archives WHERE id = ?",
            (archived.archive_id,),
        ).fetchone()[0]
    )
    metadata["manifest_head"]["authority_generation"] += 1
    metadata["manifest_head"]["projection_generation"] += 1
    conn.execute(
        """
        UPDATE worldline_archives SET metadata_json = ? WHERE id = ?
        """,
        (json.dumps(metadata, ensure_ascii=False, sort_keys=True), archived.archive_id),
    )
    conn.commit()

    with pytest.raises(WorldlineRegenerationError, match="immutable Head snapshot"):
        service.restore(
            "novel-1", archive_id=archived.archive_id, run_mode="chapter_review"
        )

    assert conn.execute(
        "SELECT active_plan_revision_id FROM outline_planning_heads "
        "WHERE novel_id = 'novel-1'"
    ).fetchone()[0] != original.id


def test_manifest_restore_rejects_metadata_changed_before_its_write_lock(tmp_path):
    """An archive evidence race cannot cross restore's write-lock boundary."""

    db_path = tmp_path / "worldline-manifest-snapshot-race.db"
    db = DatabaseConnection(str(db_path))
    _seed(db)
    _materialize_manifest_worldline(db)
    service = WorldlineRegenerationService(db)
    preview = service.preview("novel-1", start_chapter=2, target_chapters=6)
    archived = service.execute(
        "novel-1", preview_token=preview.token, run_mode="chapter_review"
    )
    competing = DatabaseConnection(str(db_path))

    def replace_archived_metadata() -> None:
        conn = competing.get_connection()
        metadata = json.loads(
            conn.execute(
                "SELECT metadata_json FROM worldline_archives WHERE id = ?",
                (archived.archive_id,),
            ).fetchone()[0]
        )
        metadata["manifest_head"]["binding_digest"] = "changed-before-lock"
        conn.execute(
            """
            UPDATE worldline_archives SET metadata_json = ? WHERE id = ?
            """,
            (
                json.dumps(metadata, ensure_ascii=False, sort_keys=True),
                archived.archive_id,
            ),
        )
        conn.commit()

    wrapped = _BeforeBeginConnection(
        db.get_connection(), replace_archived_metadata
    )
    service._connection = lambda: wrapped

    with pytest.raises(WorldlineRegenerationError, match="archive changed"):
        service.restore(
            "novel-1", archive_id=archived.archive_id, run_mode="chapter_review"
        )

    competing.close()


def test_regenerate_from_any_chapter_archives_tail_and_preserves_prefix_hash(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline.db"))
    _seed(db)
    _seed_canonical_tail(db)
    service = WorldlineRegenerationService(db)

    preview = service.preview("novel-1", start_chapter=2, target_chapters=6)
    assert preview.current_generated_chapters == 3
    assert preview.retained_through == 1
    assert preview.archive_from == 2

    result = service.execute(
        "novel-1",
        preview_token=preview.token,
        run_mode="chapter_review",
        idempotency_key="regenerate-from-2",
    )
    conn = db.get_connection()
    assert result.archive_id
    assert conn.execute(
        "SELECT content_sha256 FROM chapters WHERE novel_id = 'novel-1' AND number = 1"
    ).fetchone()[0] == hashlib.sha256("正文 1".encode("utf-8")).hexdigest()
    assert conn.execute("SELECT COUNT(*) FROM chapters WHERE novel_id = 'novel-1'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM narrative_events WHERE novel_id = 'novel-1'").fetchone()[0] == 1
    assert conn.execute("SELECT COUNT(*) FROM memory_atoms WHERE novel_id = 'novel-1'").fetchone()[0] == 1
    assert [
        tuple(row)
        for row in conn.execute(
            "SELECT id, source_type FROM bible_timeline_notes WHERE novel_id = 'novel-1'"
        ).fetchall()
    ] == [("timeline-authored-2", "bible")]
    assert conn.execute(
        "SELECT COUNT(*) FROM character_states WHERE novel_id = 'novel-1'"
    ).fetchone()[0] == 0
    assert [
        tuple(row)
        for row in conn.execute(
            "SELECT source_table, COUNT(*) FROM worldline_archive_entries "
            "WHERE archive_id = ? AND source_table IN ('bible_timeline_notes', 'character_states') "
            "GROUP BY source_table ORDER BY source_table",
            (result.archive_id,),
        ).fetchall()
    ] == [("bible_timeline_notes", 1), ("character_states", 1)]
    assert conn.execute(
        "SELECT COUNT(*) FROM worldline_archive_entries WHERE archive_id = ? AND source_table = 'chapters'",
        (result.archive_id,),
    ).fetchone()[0] == 2
    run = conn.execute(
        """
        SELECT generation_epoch, state, run_mode, current_formal_chapter,
               canonical_sync_status, next_action
        FROM novel_generation_runs WHERE novel_id = 'novel-1'
        """
    ).fetchone()
    assert tuple(run) == (1, "paused", "chapter_review", 1, "rebuilding", "rebuild_worldline")


def test_regeneration_preserves_prefix_foreshadows_in_registry(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline-foreshadow-prefix.db"))
    _seed(db)
    db.execute(
        "INSERT INTO novel_foreshadow_registry (novel_id, payload) VALUES (?, ?)",
        (
            "novel-1",
            json.dumps(
                {
                    "id": "registry-1",
                    "novel_id": "novel-1",
                    "foreshadowings": [
                        {
                            "id": "manual-prefix",
                            "planted_in_chapter": 1,
                            "description": "作者 Bible 中的开篇暗线",
                            "importance": 4,
                            "status": "planted",
                            "suggested_resolve_chapter": 8,
                            "resolved_in_chapter": None,
                        },
                        {
                            "id": "auto-tail",
                            "planted_in_chapter": 2,
                            "description": "重写点之后自动种下的暗线",
                            "importance": 2,
                            "status": "planted",
                            "suggested_resolve_chapter": 5,
                            "resolved_in_chapter": None,
                        },
                    ],
                    "subtext_entries": [],
                },
                ensure_ascii=False,
            ),
        ),
    )
    db.commit()

    service = WorldlineRegenerationService(db)
    preview = service.preview("novel-1", start_chapter=2, target_chapters=6)
    service.execute("novel-1", preview_token=preview.token, run_mode="chapter_review")

    payload = json.loads(
        db.fetch_one(
            "SELECT payload FROM novel_foreshadow_registry WHERE novel_id = 'novel-1'"
        )["payload"]
    )
    assert [item["id"] for item in payload["foreshadowings"]] == ["manual-prefix"]


def test_regeneration_target_must_include_the_restart_chapter(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline-target-boundary.db"))
    _seed(db)
    service = WorldlineRegenerationService(db)

    with pytest.raises(
        WorldlineRegenerationError,
        match="target_chapters must be at least start_chapter",
    ):
        service.preview("novel-1", start_chapter=2, target_chapters=1)


def test_reset_past_the_current_tail_is_regular_continuation_not_destructive(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline-continue.db"))
    _seed(db)
    service = WorldlineRegenerationService(db)

    preview = service.preview("novel-1", start_chapter=4, target_chapters=8)
    assert preview.operation == "continue"
    result = service.execute("novel-1", preview_token=preview.token, run_mode="continuous")

    assert result.operation == "continue"
    assert db.get_connection().execute("SELECT COUNT(*) FROM chapters WHERE novel_id = 'novel-1'").fetchone()[0] == 3


def test_empty_draft_does_not_extend_worldline_formal_head(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline-empty-draft.db"))
    _seed(db)
    db.execute(
        "INSERT INTO chapters (id, novel_id, number, title, content, status) "
        "VALUES ('draft-4', 'novel-1', 4, '第四章', '', 'draft')"
    )
    db.get_connection().commit()
    service = WorldlineRegenerationService(db)

    preview = service.preview("novel-1", start_chapter=4, target_chapters=8)

    assert preview.operation == "continue"
    assert preview.current_generated_chapters == 3
    assert preview.retained_through == 3
    assert preview.archive_from is None
    assert preview.archive_to is None
    result = service.execute(
        "novel-1",
        preview_token=preview.token,
        run_mode="chapter_review",
    )
    assert result.operation == "continue"
    assert result.retained_through == 3


def test_continue_preview_is_consumed_after_execution(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline-continue-once.db"))
    _seed(db)
    service = WorldlineRegenerationService(db)
    preview = service.preview("novel-1", start_chapter=4, target_chapters=8)

    service.execute("novel-1", preview_token=preview.token, run_mode="continuous")

    with pytest.raises(WorldlineRegenerationError, match="already consumed"):
        service.execute("novel-1", preview_token=preview.token, run_mode="chapter_review")


def test_continue_preview_revalidates_the_existing_chapter_prefix(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline-continue-stale.db"))
    _seed(db)
    service = WorldlineRegenerationService(db)
    preview = service.preview("novel-1", start_chapter=4, target_chapters=8)
    db.execute(
        "UPDATE chapters SET content = '已修改正文', content_sha256 = 'changed' "
        "WHERE novel_id = 'novel-1' AND number = 1"
    )
    db.get_connection().commit()

    with pytest.raises(WorldlineRegenerationError, match="chapter prefix changed"):
        service.execute("novel-1", preview_token=preview.token, run_mode="continuous")


def test_restore_archived_worldline_replaces_new_tail_and_creates_a_new_epoch(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline-restore.db"))
    _seed(db)
    service = WorldlineRegenerationService(db)

    preview = service.preview("novel-1", start_chapter=2, target_chapters=6)
    archived = service.execute(
        "novel-1",
        preview_token=preview.token,
        run_mode="continuous",
        idempotency_key="archive-original-tail",
    )
    conn = db.get_connection()
    conn.execute(
        """
        INSERT INTO chapters (id, novel_id, number, title, content, content_sha256, content_revision, status)
        VALUES ('replacement-2', 'novel-1', 2, '新世界线第2章', '新正文', 'new-hash-2', 1, 'completed')
        """
    )
    conn.commit()

    restored = service.restore("novel-1", archive_id=archived.archive_id, run_mode="chapter_review")

    assert restored.archive_id != archived.archive_id
    assert restored.generation_epoch == 2
    rows = conn.execute(
        "SELECT number, content_sha256 FROM chapters WHERE novel_id = 'novel-1' ORDER BY number"
    ).fetchall()
    assert [tuple(row) for row in rows] == [
        (number, hashlib.sha256(f"正文 {number}".encode("utf-8")).hexdigest())
        for number in (1, 2, 3)
    ]
    assert conn.execute(
        "SELECT status FROM worldline_archives WHERE id = ?", (archived.archive_id,)
    ).fetchone()[0] == "restored"


def test_restore_keeps_archived_checkpoint_summary_inactive_until_rebuilt(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline-restore-runtime-checkpoint.db"))
    _seed(db)
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO story_nodes "
        "(id, novel_id, node_type, number, title, order_index, metadata) "
        "VALUES ('chapter-2', 'novel-1', 'chapter', 2, 'Chapter 2', 2, ?)",
        (
            json.dumps(
                {
                    "runtime.checkpoint_summary": "old checkpoint cache",
                    "runtime.checkpoint_summary_state": {
                        "status": "committed",
                        "chapter_start": 2,
                        "chapter_end": 2,
                        "source_chapter_numbers": [2],
                        "source_version": "old-source",
                        "pipeline_version": "node-summary/v1",
                    },
                }
            ),
        ),
    )
    conn.commit()
    service = WorldlineRegenerationService(db)

    preview = service.preview("novel-1", start_chapter=2, target_chapters=6)
    archived = service.execute("novel-1", preview_token=preview.token, run_mode="continuous")
    conn.execute(
        "INSERT INTO chapters "
        "(id, novel_id, number, title, content, content_sha256, content_revision, status) "
        "VALUES ('replacement-2', 'novel-1', 2, 'Replacement', 'new prose', "
        "'replacement-hash', 1, 'completed')"
    )
    conn.commit()

    service.restore("novel-1", archive_id=archived.archive_id, run_mode="chapter_review")

    metadata = json.loads(
        conn.execute("SELECT metadata FROM story_nodes WHERE id = 'chapter-2'").fetchone()[0]
    )
    assert "runtime.checkpoint_summary" not in metadata
    assert "runtime.checkpoint_summary_state" not in metadata
    assert metadata["runtime.checkpoint_summary_invalidated_from_chapter"] == 2


def test_restore_strips_pre_fix_archived_runtime_summary_payload(tmp_path):
    """A historical archive must not revive runtime caches created before invalidation.

    Archives made before the manifest/runtime-cache correction contain the
    original StoryNode metadata.  Simulate that persisted payload directly
    rather than relying on a new archive, which is already sanitized by the
    current writer.
    """

    db = DatabaseConnection(str(tmp_path / "worldline-restore-legacy-runtime.db"))
    _seed(db)
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO story_nodes "
        "(id, novel_id, node_type, number, title, order_index, metadata) "
        "VALUES ('chapter-2', 'novel-1', 'chapter', 2, 'Chapter 2', 2, ?)",
        (json.dumps({}),),
    )
    conn.commit()
    service = WorldlineRegenerationService(db)

    preview = service.preview("novel-1", start_chapter=2, target_chapters=6)
    archived = service.execute("novel-1", preview_token=preview.token, run_mode="continuous")
    archive_entry = conn.execute(
        """
        SELECT id, payload_json
        FROM worldline_archive_entries
        WHERE archive_id = ? AND source_table = 'story_nodes'
          AND json_extract(payload_json, '$.id') = 'chapter-2'
        """,
        (archived.archive_id,),
    ).fetchone()
    assert archive_entry is not None
    archive_payload = json.loads(archive_entry[1])
    archive_payload["metadata"] = json.dumps(
        {
            "runtime.checkpoint_summary": "pre-fix checkpoint cache",
            "runtime.checkpoint_summary_state": {
                "status": "committed",
                "chapter_start": 2,
                "chapter_end": 2,
                "source_chapter_numbers": [2],
                "source_version": "pre-fix-source",
                "pipeline_version": "node-summary/v1",
            },
            "checkpoint_summary": "pre-fix legacy fallback",
            "checkpoint_summary_state": {
                "status": "committed",
                "chapter_start": 2,
                "chapter_end": 2,
                "source_version": "pre-fix-source",
            },
        }
    )
    conn.execute(
        "UPDATE worldline_archive_entries SET payload_json = ? WHERE id = ?",
        (json.dumps(archive_payload), archive_entry[0]),
    )
    conn.execute(
        "INSERT INTO chapters "
        "(id, novel_id, number, title, content, content_sha256, content_revision, status) "
        "VALUES ('replacement-2', 'novel-1', 2, 'Replacement', 'new prose', "
        "'replacement-hash', 1, 'completed')"
    )
    conn.commit()

    service.restore("novel-1", archive_id=archived.archive_id, run_mode="chapter_review")

    metadata = json.loads(
        conn.execute("SELECT metadata FROM story_nodes WHERE id = 'chapter-2'").fetchone()[0]
    )
    assert "runtime.checkpoint_summary" not in metadata
    assert "runtime.checkpoint_summary_state" not in metadata
    assert metadata["runtime.checkpoint_summary_invalidated_from_chapter"] == 2
    # Legacy payload is retained only for compatibility and cannot be read
    # while the explicit runtime invalidation marker is present.
    assert metadata["checkpoint_summary"] == "pre-fix legacy fallback"


def test_execute_rechecks_formal_identity_inside_write_transaction_before_archiving(tmp_path):
    """A Formal change after preview validation must roll back the whole reset."""

    db = DatabaseConnection(str(tmp_path / "worldline-execute-inner-formal-race.db"))
    _seed(db)
    preview = WorldlineRegenerationService(db).preview(
        "novel-1", start_chapter=2, target_chapters=6
    )

    class _FormalRaceWorldlineService(WorldlineRegenerationService):
        def _ensure_run(self, conn, novel_id, target_chapters):
            super()._ensure_run(conn, novel_id, target_chapters)
            conn.execute(
                "UPDATE chapters SET content = 'raced formal prose', content_sha256 = 'raced-hash' "
                "WHERE novel_id = ? AND number = 1",
                (novel_id,),
            )

    service = _FormalRaceWorldlineService(db)
    conn = db.get_connection()
    original = conn.execute(
        "SELECT content, content_sha256, content_revision FROM chapters "
        "WHERE novel_id = 'novel-1' AND number = 1"
    ).fetchone()

    with pytest.raises(WorldlineRegenerationError, match="chapter prefix changed"):
        service.execute("novel-1", preview_token=preview.token, run_mode="continuous")

    assert tuple(
        conn.execute(
            "SELECT content, content_sha256, content_revision FROM chapters "
            "WHERE novel_id = 'novel-1' AND number = 1"
        ).fetchone()
    ) == tuple(original)
    assert conn.execute(
        "SELECT COUNT(*) FROM chapters WHERE novel_id = 'novel-1'"
    ).fetchone()[0] == 3
    assert conn.execute(
        "SELECT consumed_at FROM worldline_regeneration_previews WHERE token = ?",
        (preview.token,),
    ).fetchone()[0] is None
    assert conn.execute(
        "SELECT COUNT(*) FROM worldline_archives WHERE novel_id = 'novel-1'"
    ).fetchone()[0] == 0
    assert conn.execute(
        "SELECT 1 FROM novel_generation_runs WHERE novel_id = 'novel-1'"
    ).fetchone() is None
    assert conn.execute(
        "SELECT 1 FROM outline_planning_heads WHERE novel_id = 'novel-1'"
    ).fetchone() is None


def test_restore_rechecks_generation_epoch_inside_write_transaction_before_archiving(tmp_path):
    """A newer epoch cannot be overwritten by a restore worker that read stale state."""

    db = DatabaseConnection(str(tmp_path / "worldline-restore-inner-epoch-race.db"))
    _seed(db)
    initial = WorldlineRegenerationService(db)
    preview = initial.preview("novel-1", start_chapter=2, target_chapters=6)
    archived = initial.execute("novel-1", preview_token=preview.token, run_mode="continuous")

    class _EpochRaceWorldlineService(WorldlineRegenerationService):
        def _ensure_run(self, conn, novel_id, target_chapters):
            super()._ensure_run(conn, novel_id, target_chapters)
            conn.execute(
                "UPDATE novel_generation_runs SET generation_epoch = generation_epoch + 10 "
                "WHERE novel_id = ?",
                (novel_id,),
            )

    conn = db.get_connection()
    before_run = tuple(
        conn.execute(
            "SELECT generation_epoch, state, canonical_sync_status, next_action "
            "FROM novel_generation_runs WHERE novel_id = 'novel-1'"
        ).fetchone()
    )

    with pytest.raises(WorldlineRegenerationError, match="generation epoch changed"):
        _EpochRaceWorldlineService(db).restore(
            "novel-1", archive_id=archived.archive_id, run_mode="chapter_review"
        )

    assert tuple(
        conn.execute(
            "SELECT generation_epoch, state, canonical_sync_status, next_action "
            "FROM novel_generation_runs WHERE novel_id = 'novel-1'"
        ).fetchone()
    ) == before_run
    assert conn.execute(
        "SELECT COUNT(*) FROM worldline_archives WHERE novel_id = 'novel-1'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT status FROM worldline_archives WHERE id = ?", (archived.archive_id,)
    ).fetchone()[0] == "archived"
    assert conn.execute(
        "SELECT COUNT(*) FROM chapters WHERE novel_id = 'novel-1'"
    ).fetchone()[0] == 1
    assert conn.execute(
        "SELECT 1 FROM outline_planning_heads WHERE novel_id = 'novel-1'"
    ).fetchone() is None


class _BeforeBeginConnection:
    """Commit one competing authority change immediately before the write lock."""

    def __init__(self, connection, before_begin):
        self._connection = connection
        self._before_begin = before_begin
        self._called = False

    def execute(self, sql, params=()):
        if not self._called and sql.strip().upper() == "BEGIN IMMEDIATE":
            self._called = True
            self._before_begin()
        return self._connection.execute(sql, params)

    def __getattr__(self, name):
        return getattr(self._connection, name)


@pytest.mark.parametrize("race", ("formal", "run"))
def test_continue_rechecks_preview_authority_under_its_single_write_lock(tmp_path, race):
    """A stale continue preview cannot consume itself or start/alter a replacement run."""

    db = DatabaseConnection(str(tmp_path / f"worldline-continue-{race}-race.db"))
    _seed(db)
    service = WorldlineRegenerationService(db)
    preview = service.preview("novel-1", start_chapter=4, target_chapters=8)
    competing = DatabaseConnection(db.db_path)

    def change_authority() -> None:
        conn = competing.get_connection()
        if race == "formal":
            content = "competing formal prefix"
            conn.execute(
                "UPDATE chapters SET content = ?, content_sha256 = ? "
                "WHERE novel_id = 'novel-1' AND number = 1",
                (content, hashlib.sha256(content.encode("utf-8")).hexdigest()),
            )
        else:
            ChapterCandidateRepository(competing).start_run(
                "novel-1", run_mode=RunMode.CONTINUOUS, target_chapters=8
            )
        conn.commit()

    wrapped = _BeforeBeginConnection(db.get_connection(), change_authority)
    service._connection = lambda: wrapped

    with pytest.raises(WorldlineRegenerationError, match="prefix|generation run|chapter tail"):
        service.execute("novel-1", preview_token=preview.token, run_mode="continuous")

    conn = db.get_connection()
    assert conn.execute(
        "SELECT consumed_at FROM worldline_regeneration_previews WHERE token = ?",
        (preview.token,),
    ).fetchone()[0] is None
    assert conn.execute(
        "SELECT COUNT(*) FROM worldline_archives WHERE novel_id = 'novel-1'"
    ).fetchone()[0] == 0
    if race == "formal":
        assert conn.execute(
            "SELECT 1 FROM novel_generation_runs WHERE novel_id = 'novel-1'"
        ).fetchone() is None
    else:
        assert tuple(
            conn.execute(
                """
                SELECT generation_epoch, state, current_formal_chapter,
                       current_candidate_id, canonical_sync_status, next_action
                FROM novel_generation_runs WHERE novel_id = 'novel-1'
                """
            ).fetchone()
        ) == (0, "running", 3, None, "ready", "generate_candidate")


def test_continue_commits_run_preview_and_idempotency_record_exactly_once(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline-continue-exactly-once.db"))
    _seed(db)
    service = WorldlineRegenerationService(db)
    preview = service.preview("novel-1", start_chapter=4, target_chapters=8)

    first = service.execute(
        "novel-1",
        preview_token=preview.token,
        run_mode="chapter_review",
        idempotency_key="continue-once",
    )
    repeated = service.execute(
        "novel-1",
        preview_token=preview.token,
        run_mode="chapter_review",
        idempotency_key="continue-once",
    )

    assert repeated == first
    conn = db.get_connection()
    assert conn.execute(
        "SELECT consumed_at IS NOT NULL FROM worldline_regeneration_previews WHERE token = ?",
        (preview.token,),
    ).fetchone()[0] == 1
    assert conn.execute(
        """
        SELECT COUNT(*) FROM worldline_regeneration_operations
        WHERE novel_id = 'novel-1' AND idempotency_key = 'continue-once' AND operation = 'continue'
        """
    ).fetchone()[0] == 1
    assert tuple(
        conn.execute(
            "SELECT state, generation_epoch, current_formal_chapter, next_action "
            "FROM novel_generation_runs WHERE novel_id = 'novel-1'"
        ).fetchone()
    ) == ("running", 0, 3, "generate_candidate")


def test_manifest_continue_rechecks_idempotency_after_waiting_for_its_write_lock(
    tmp_path,
):
    """A concurrent same-key success must be replayed after acquiring the lock."""

    db = DatabaseConnection(str(tmp_path / "worldline-manifest-continue-idempotency-race.db"))
    _seed(db)
    _materialize_manifest_worldline(db)
    service = WorldlineRegenerationService(db)
    preview = service.preview("novel-1", start_chapter=4, target_chapters=8)
    competing = DatabaseConnection(db.db_path)
    first_result = []

    def complete_first_request() -> None:
        first_result.append(
            WorldlineRegenerationService(competing).execute(
                "novel-1",
                preview_token=preview.token,
                run_mode="chapter_review",
                idempotency_key="manifest-continue-once",
            )
        )

    service._connection = lambda: _BeforeBeginConnection(
        db.get_connection(), complete_first_request
    )

    repeated = service.execute(
        "novel-1",
        preview_token=preview.token,
        run_mode="chapter_review",
        idempotency_key="manifest-continue-once",
    )

    assert repeated == first_result[0]
    assert db.get_connection().execute(
        "SELECT COUNT(*) FROM worldline_regeneration_operations "
        "WHERE novel_id = 'novel-1' AND idempotency_key = 'manifest-continue-once' "
        "AND operation = 'continue'"
    ).fetchone()[0] == 1


def test_manifest_regenerate_rechecks_idempotency_after_waiting_for_its_write_lock(
    tmp_path,
):
    """A same-key rebase must replay after the first request consumes its preview."""

    db = DatabaseConnection(
        str(tmp_path / "worldline-manifest-regenerate-idempotency-race.db")
    )
    _seed(db)
    _materialize_manifest_worldline(db)
    service = WorldlineRegenerationService(db)
    preview = service.preview("novel-1", start_chapter=2, target_chapters=6)
    competing = DatabaseConnection(db.db_path)
    first_result = []

    def complete_first_request() -> None:
        first_result.append(
            WorldlineRegenerationService(competing).execute(
                "novel-1",
                preview_token=preview.token,
                run_mode="chapter_review",
                idempotency_key="manifest-regenerate-once",
            )
        )

    service._connection = lambda: _BeforeBeginConnection(
        db.get_connection(), complete_first_request
    )
    try:
        repeated = service.execute(
            "novel-1",
            preview_token=preview.token,
            run_mode="chapter_review",
            idempotency_key="manifest-regenerate-once",
        )
    finally:
        competing.close()

    assert repeated == first_result[0]
    assert db.get_connection().execute(
        "SELECT COUNT(*) FROM worldline_regeneration_operations "
        "WHERE novel_id = 'novel-1' AND idempotency_key = 'manifest-regenerate-once' "
        "AND operation = 'regenerate'"
    ).fetchone()[0] == 1


def test_manifest_restore_rechecks_idempotency_after_waiting_for_its_write_lock(
    tmp_path,
):
    """A same-key restore must replay after the first request changes its archive."""

    db = DatabaseConnection(
        str(tmp_path / "worldline-manifest-restore-idempotency-race.db")
    )
    _seed(db)
    _materialize_manifest_worldline(db)
    initial = WorldlineRegenerationService(db)
    preview = initial.preview("novel-1", start_chapter=2, target_chapters=6)
    archived = initial.execute(
        "novel-1", preview_token=preview.token, run_mode="chapter_review"
    )
    service = WorldlineRegenerationService(db)
    competing = DatabaseConnection(db.db_path)
    first_result = []

    def complete_first_request() -> None:
        first_result.append(
            WorldlineRegenerationService(competing).restore(
                "novel-1",
                archive_id=archived.archive_id,
                run_mode="chapter_review",
                idempotency_key="manifest-restore-once",
            )
        )

    service._connection = lambda: _BeforeBeginConnection(
        db.get_connection(), complete_first_request
    )
    try:
        repeated = service.restore(
            "novel-1",
            archive_id=archived.archive_id,
            run_mode="chapter_review",
            idempotency_key="manifest-restore-once",
        )
    finally:
        competing.close()

    assert repeated == first_result[0]
    assert db.get_connection().execute(
        "SELECT COUNT(*) FROM worldline_regeneration_operations "
        "WHERE novel_id = 'novel-1' AND idempotency_key = 'manifest-restore-once' "
        "AND operation = 'restore'"
    ).fetchone()[0] == 1


def test_continue_rolls_back_started_run_when_preview_consumption_fails(tmp_path):
    """No crash window may leave a started run with its preview still reusable."""

    class _FailPreviewConsumptionConnection:
        def __init__(self, connection):
            self._connection = connection

        def execute(self, sql, params=()):
            if "UPDATE worldline_regeneration_previews" in sql:
                raise sqlite3.OperationalError("forced preview consume failure")
            return self._connection.execute(sql, params)

        def __getattr__(self, name):
            return getattr(self._connection, name)

    db = DatabaseConnection(str(tmp_path / "worldline-continue-rollback.db"))
    _seed(db)
    service = WorldlineRegenerationService(db)
    preview = service.preview("novel-1", start_chapter=4, target_chapters=8)
    service._connection = lambda: _FailPreviewConsumptionConnection(db.get_connection())

    with pytest.raises(sqlite3.OperationalError, match="forced preview consume failure"):
        service.execute("novel-1", preview_token=preview.token, run_mode="continuous")

    conn = db.get_connection()
    assert conn.execute(
        "SELECT 1 FROM novel_generation_runs WHERE novel_id = 'novel-1'"
    ).fetchone() is None
    assert conn.execute(
        "SELECT consumed_at FROM worldline_regeneration_previews WHERE token = ?",
        (preview.token,),
    ).fetchone()[0] is None
    assert conn.execute(
        "SELECT COUNT(*) FROM worldline_regeneration_operations WHERE novel_id = 'novel-1'"
    ).fetchone()[0] == 0
