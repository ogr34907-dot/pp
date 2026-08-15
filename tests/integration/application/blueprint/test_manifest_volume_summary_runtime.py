"""Canonical-derived node summaries remain available after manifest cutover."""

import asyncio
import hashlib
import json
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from application.blueprint.services.volume_summary_service import VolumeSummaryService
from application.blueprint.services.continuous_planning_service import (
    ContinuousPlanningService,
)
from application.core.services.chapter_rewrite_coordinator import ChapterRewriteCoordinator
from application.engine.services.context_budget_allocator import ContextBudgetAllocator
from application.engine.services.hierarchical_narrative_alignment_gate import (
    HierarchicalNarrativeAlignmentGate,
)
from application.governance.service import NarrativeGovernanceService
from application.world.services.chapter_narrative_sync import (
    CHAPTER_NARRATIVE_PIPELINE_VERSION,
)
from domain.structure.outline_contract import OutlinePayload
from domain.structure.outline_plan import OutlinePlanItem
from domain.novel.value_objects.novel_id import NovelId
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.outline_contract_repository import (
    OutlineContractRepository,
)
from infrastructure.persistence.database.sqlite_chapter_repository import (
    SqliteChapterRepository,
)
from infrastructure.persistence.database.story_node_repository import StoryNodeRepository


def _manifest_book_with_act(tmp_path):
    database = DatabaseConnection(str(tmp_path / "manifest-volume-summary.db"))
    conn = database.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("novel-1", "Manifest", "manifest", 20),
    )
    conn.commit()
    contracts = OutlineContractRepository(database)
    root = contracts.ensure_root("novel-1")
    draft = contracts.save_draft(
        root.id,
        OutlinePayload(
            title="Root",
            narrative_text="A complete premise",
            creative_goal="Reach the ending",
            entry_state="start",
            exit_state="end",
        ),
    )
    published = contracts.publish_and_sync(root.id, expected_revision=draft.draft.revision)
    version = conn.execute(
        """
        SELECT version.id, version.digest
        FROM outline_contracts AS contract
        JOIN outline_contract_versions AS version ON version.id = contract.active_version_id
        WHERE contract.id = ?
        """,
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
        canonical_boundary={"formal_head": 0},
    )
    plan = contracts.seal_plan_revision(plan.id)
    conn.execute(
        """
        UPDATE outline_planning_heads
        SET authority_mode = 'manifest', authority_generation = 1,
            active_plan_revision_id = ?, active_plan_digest = ?,
            projection_generation = 1
        WHERE novel_id = ?
        """,
        (plan.id, plan.digest, "novel-1"),
    )
    conn.execute(
        """
        INSERT INTO story_nodes
            (id, novel_id, parent_id, node_type, number, title, order_index,
             chapter_start, chapter_end)
        VALUES ('act-1', 'novel-1', NULL, 'act', 1, 'Act', 0, 1, 1)
        """
    )
    conn.execute(
        """
        INSERT INTO story_nodes
            (id, novel_id, parent_id, node_type, number, title, order_index)
        VALUES ('chapter-1', 'novel-1', 'act-1', 'chapter', 1, 'Chapter', 0)
        """
    )
    conn.execute(
        """
        INSERT INTO chapters
            (id, novel_id, number, title, content, status, content_sha256,
             content_revision)
        VALUES ('chapter-1', 'novel-1', 1, 'Chapter', 'Formal text', 'completed',
                ?, 1)
        """
        ,
        (hashlib.sha256(b"Formal text").hexdigest(),),
    )
    content_sha256 = hashlib.sha256(b"Formal text").hexdigest()
    conn.execute(
        """
        INSERT INTO chapter_candidates
            (id, novel_id, chapter_number, generation_epoch, status)
        VALUES ('candidate-1', 'novel-1', 1, 0, 'committed')
        """
    )
    conn.execute(
        """
        INSERT INTO chapter_candidate_formal_commits
            (candidate_id, novel_id, chapter_number, chapter_id,
             content_sha256, content_revision, sync_status)
        VALUES ('candidate-1', 'novel-1', 1, 'chapter-1', ?, 1, 'ready')
        """,
        (content_sha256,),
    )
    conn.execute(
        "INSERT INTO knowledge (id, novel_id) VALUES ('knowledge-1', 'novel-1')"
    )
    conn.execute(
        """
        INSERT INTO chapter_narrative_commits
            (novel_id, chapter_number, content_sha256, pipeline_version,
             content_revision, status, memory_status)
        VALUES ('novel-1', 1, ?, ?, 1, 'committed', 'committed')
        """,
        (content_sha256, CHAPTER_NARRATIVE_PIPELINE_VERSION),
    )
    conn.execute(
        """
        INSERT INTO chapter_summaries
            (id, knowledge_id, chapter_number, summary, source_content_sha256,
             source_content_revision, pipeline_version, sync_status)
        VALUES ('chapter-summary-1', 'knowledge-1', 1, 'Canonical summary', ?, 1, ?, 'committed')
        """,
        (content_sha256, CHAPTER_NARRATIVE_PIPELINE_VERSION),
    )
    conn.commit()
    return database


@pytest.mark.asyncio
async def test_delayed_summary_cannot_restore_source_captured_before_manifest_rewrite(tmp_path):
    database = _manifest_book_with_act(tmp_path)
    conn = database.get_connection()
    source_hash = hashlib.sha256(b"Formal text").hexdigest()
    old_state = {
        "status": "committed",
        "chapter_start": 1,
        "chapter_end": 1,
        "source_chapter_numbers": [1],
        "source_version": hashlib.sha256(
            f"1:{source_hash}:1".encode("utf-8")
        ).hexdigest(),
        "pipeline_version": "node-summary/v1",
    }
    conn.execute(
        "UPDATE story_nodes SET metadata = ? WHERE id = 'act-1'",
        (
            json.dumps(
                {
                    "runtime.summary": "cache before rewrite",
                    "runtime.summary_state": old_state,
                }
            ),
        ),
    )
    conn.commit()
    started = asyncio.Event()
    release = asyncio.Event()

    async def delayed_generate(*_args, **_kwargs):
        started.set()
        await release.wait()
        return SimpleNamespace(content="summary generated from pre-rewrite prose")

    story_nodes = StoryNodeRepository(database)
    chapters = SqliteChapterRepository(database)
    service = VolumeSummaryService(
        llm_service=SimpleNamespace(generate=delayed_generate),
        story_node_repository=story_nodes,
        chapter_repository=chapters,
    )

    pending_summary = asyncio.create_task(
        service.generate_act_summary("novel-1", "act-1")
    )
    await started.wait()
    ChapterRewriteCoordinator(
        db=database,
        chapter_repository=chapters,
    ).rewrite(
        chapters.get_by_novel_and_number(NovelId("novel-1"), 1),
        "Formal text after rewrite",
    )
    release.set()
    summary_result = await pending_summary

    assert summary_result.success is False
    metadata = json.loads(
        conn.execute("SELECT metadata FROM story_nodes WHERE id = 'act-1'").fetchone()[0]
    )
    assert "runtime.summary" not in metadata
    assert "runtime.summary_state" not in metadata
    assert metadata["runtime.summary_invalidated_from_chapter"] == 1


@pytest.mark.asyncio
async def test_manifest_summary_uses_runtime_patch_and_reaches_context(tmp_path):
    database = _manifest_book_with_act(tmp_path)
    database.get_connection().execute(
        "UPDATE story_nodes SET metadata = ? WHERE id = 'act-1'",
        (json.dumps({"runtime.summary_invalidated_from_chapter": 1}),),
    )
    database.get_connection().commit()
    story_nodes = StoryNodeRepository(database)
    chapters = SqliteChapterRepository(database)
    service = VolumeSummaryService(
        llm_service=SimpleNamespace(
            generate=AsyncMock(return_value=SimpleNamespace(content="runtime manifest summary"))
        ),
        story_node_repository=story_nodes,
        chapter_repository=chapters,
    )

    result = await service.generate_act_summary("novel-1", "act-1")

    assert result.success is True
    metadata = json.loads(
        database.get_connection().execute(
            "SELECT metadata FROM story_nodes WHERE id = 'act-1'"
        ).fetchone()[0]
    )
    assert metadata["runtime.summary"] == "runtime manifest summary"
    assert metadata["runtime.summary_state"]["status"] == "committed"
    assert "runtime.summary_invalidated_from_chapter" not in metadata
    assert "summary" not in metadata

    context = ContextBudgetAllocator(
        story_node_repository=story_nodes,
        chapter_repository=chapters,
    )._get_current_act_summary("novel-1", 1)
    assert "runtime manifest summary" in context


def _committed_single_chapter_summary_state(content_sha256: str) -> dict[str, object]:
    return {
        "status": "committed",
        "chapter_start": 1,
        "chapter_end": 1,
        "source_chapter_numbers": [1],
        "source_version": hashlib.sha256(
            f"1:{content_sha256}:1".encode("utf-8")
        ).hexdigest(),
        "pipeline_version": "node-summary/v1",
    }


def _summary_readers(database):
    story_nodes = StoryNodeRepository(database)
    chapters = SqliteChapterRepository(database)
    return (
        story_nodes,
        VolumeSummaryService(
            llm_service=SimpleNamespace(),
            story_node_repository=story_nodes,
            chapter_repository=chapters,
        ),
        ContextBudgetAllocator(
            story_node_repository=story_nodes,
            chapter_repository=chapters,
        ),
        ContinuousPlanningService(
            story_node_repo=story_nodes,
            chapter_element_repo=SimpleNamespace(),
            llm_service=SimpleNamespace(),
            chapter_repository=chapters,
        ),
    )


def test_manifest_summary_readers_reject_hash_matching_legacy_cache(tmp_path):
    """Manifest reads require Formal/Canonical/Memory evidence, not a hash match."""
    database = _manifest_book_with_act(tmp_path)
    conn = database.get_connection()
    content_sha256 = hashlib.sha256(b"Formal text").hexdigest()
    conn.execute(
        "UPDATE story_nodes SET metadata = ? WHERE id = 'act-1'",
        (
            json.dumps(
                {
                    "summary": "unproven legacy cache",
                    "summary_state": _committed_single_chapter_summary_state(
                        content_sha256
                    ),
                    "committed_metadata": {"summary": "unproven committed cache"},
                }
            ),
        ),
    )
    conn.commit()
    story_nodes, volume_summaries, context, continuous = _summary_readers(database)
    act = next(
        node
        for node in story_nodes.get_by_novel_sync("novel-1")
        if node.id == "act-1"
    )

    assert volume_summaries.get_act_summary("novel-1", 1) is None
    assert context._get_valid_node_summary("novel-1", act) == ""
    assert continuous._get_current_node_summary(act) == ""
    snapshot = HierarchicalNarrativeAlignmentGate(
        story_node_repository=story_nodes
    ).build_snapshot("chapter-1", story_nodes.get_by_novel_sync("novel-1"))
    assert snapshot.ancestry["act"]["description"] == ""


def test_manifest_governance_preview_hides_unproven_runtime_summary(tmp_path):
    """Governance preview must use the same summary visibility policy as Context."""

    database = _manifest_book_with_act(tmp_path)
    conn = database.get_connection()
    conn.execute(
        "UPDATE story_nodes SET metadata = ? WHERE id = 'act-1'",
        (json.dumps({"runtime.summary": "unproven runtime summary"}),),
    )
    conn.commit()
    story_nodes = StoryNodeRepository(database)
    service = NarrativeGovernanceService(
        SimpleNamespace(append_event=lambda *_args: None),
        story_node_repository=story_nodes,
    )
    assert service._hierarchy_gate().story_node_repository is story_nodes

    preview = service.preview_hierarchy_alignment(
        "novel-1", "chapter-1", {"contract_digests": {}}
    )

    assert preview["snapshot"]["ancestry"]["act"]["description"] == ""


def test_manifest_summary_readers_admit_durable_candidate_runtime_cache(tmp_path):
    """All readers retain a runtime summary backed by the exact aftermath fact."""
    database = _manifest_book_with_act(tmp_path)
    conn = database.get_connection()
    content_sha256 = hashlib.sha256(b"Formal text").hexdigest()
    conn.execute(
        "UPDATE story_nodes SET metadata = ? WHERE id = 'act-1'",
        (
            json.dumps(
                {
                    "runtime.summary": "durable candidate runtime cache",
                    "runtime.summary_state": _committed_single_chapter_summary_state(
                        content_sha256
                    ),
                }
            ),
        ),
    )
    conn.commit()
    story_nodes, volume_summaries, context, continuous = _summary_readers(database)
    act = next(
        node
        for node in story_nodes.get_by_novel_sync("novel-1")
        if node.id == "act-1"
    )

    assert volume_summaries.get_act_summary("novel-1", 1) == "durable candidate runtime cache"
    assert context._get_valid_node_summary("novel-1", act) == "durable candidate runtime cache"
    assert continuous._get_current_node_summary(act) == "durable candidate runtime cache"
    snapshot = HierarchicalNarrativeAlignmentGate(
        story_node_repository=story_nodes
    ).build_snapshot("chapter-1", story_nodes.get_by_novel_sync("novel-1"))
    assert snapshot.ancestry["act"]["description"] == "durable candidate runtime cache"


def test_manifest_summary_readers_keep_exact_pre_candidate_legacy_baseline(tmp_path):
    """A migrated pre-Candidate cache remains visible only with durable aftermath."""
    database = _manifest_book_with_act(tmp_path)
    conn = database.get_connection()
    content_sha256 = hashlib.sha256(b"Formal text").hexdigest()
    conn.execute(
        """
        INSERT INTO pre_candidate_formal_history
            (novel_id, chapter_number, chapter_id, content_sha256, content_revision)
        VALUES ('novel-1', 1, 'chapter-1', ?, 1)
        """,
        (content_sha256,),
    )
    conn.execute(
        "UPDATE story_nodes SET metadata = ? WHERE id = 'act-1'",
        (
            json.dumps(
                {
                    "summary": "proven pre-candidate legacy cache",
                    "summary_state": _committed_single_chapter_summary_state(
                        content_sha256
                    ),
                }
            ),
        ),
    )
    conn.commit()
    story_nodes, volume_summaries, context, continuous = _summary_readers(database)
    act = next(
        node
        for node in story_nodes.get_by_novel_sync("novel-1")
        if node.id == "act-1"
    )

    assert volume_summaries.get_act_summary("novel-1", 1) == "proven pre-candidate legacy cache"
    assert context._get_valid_node_summary("novel-1", act) == "proven pre-candidate legacy cache"
    assert continuous._get_current_node_summary(act) == "proven pre-candidate legacy cache"
    snapshot = HierarchicalNarrativeAlignmentGate(
        story_node_repository=story_nodes
    ).build_snapshot("chapter-1", story_nodes.get_by_novel_sync("novel-1"))
    assert snapshot.ancestry["act"]["description"] == "proven pre-candidate legacy cache"


def test_manifest_legacy_summary_requires_complete_durable_aftermath(tmp_path):
    """Legacy baseline compatibility cannot bypass Narrative, summary, or Memory."""
    database = _manifest_book_with_act(tmp_path)
    conn = database.get_connection()
    content_sha256 = hashlib.sha256(b"Formal text").hexdigest()
    conn.execute(
        """
        INSERT INTO pre_candidate_formal_history
            (novel_id, chapter_number, chapter_id, content_sha256, content_revision)
        VALUES ('novel-1', 1, 'chapter-1', ?, 1)
        """,
        (content_sha256,),
    )
    conn.execute("DELETE FROM chapter_narrative_commits WHERE novel_id = 'novel-1'")
    conn.execute(
        "DELETE FROM chapter_summaries WHERE knowledge_id IN "
        "(SELECT id FROM knowledge WHERE novel_id = 'novel-1')"
    )
    conn.execute(
        "UPDATE story_nodes SET metadata = ? WHERE id = 'act-1'",
        (
            json.dumps(
                {
                    "summary": "legacy summary needs durable aftermath",
                    "summary_state": _committed_single_chapter_summary_state(
                        content_sha256
                    ),
                }
            ),
        ),
    )
    conn.commit()
    story_nodes = StoryNodeRepository(database)
    act = next(
        node
        for node in story_nodes.get_by_novel_sync("novel-1")
        if node.id == "act-1"
    )

    assert story_nodes.visible_summary_metadata_pairs(
        act, summary_key="summary", state_key="summary_state"
    ) == ()

    conn.execute(
        """
        INSERT INTO chapter_narrative_commits
            (novel_id, chapter_number, content_sha256, pipeline_version,
             content_revision, status, memory_status)
        VALUES ('novel-1', 1, ?, ?, 1, 'committed', 'committed')
        """,
        (content_sha256, CHAPTER_NARRATIVE_PIPELINE_VERSION),
    )
    conn.commit()
    assert story_nodes.visible_summary_metadata_pairs(
        act, summary_key="summary", state_key="summary_state"
    ) == ()

    conn.execute(
        """
        INSERT INTO chapter_summaries
            (id, knowledge_id, chapter_number, summary, source_content_sha256,
             source_content_revision, pipeline_version, sync_status)
        VALUES ('legacy-summary-1', 'knowledge-1', 1, 'Legacy canonical summary',
                ?, 1, ?, 'committed')
        """,
        (content_sha256, CHAPTER_NARRATIVE_PIPELINE_VERSION),
    )
    conn.commit()
    assert story_nodes.visible_summary_metadata_pairs(
        act, summary_key="summary", state_key="summary_state"
    ) == (("legacy summary needs durable aftermath", _committed_single_chapter_summary_state(content_sha256)),)


@pytest.mark.parametrize(
    "runtime_metadata",
    [
        {"runtime.summary": "summary without provenance"},
        {
            "runtime.summary": "summary with malformed provenance",
            "runtime.summary_state": {},
        },
        {"runtime.checkpoint_summary": "checkpoint without provenance"},
    ],
)
def test_manifest_runtime_summary_write_requires_exact_provenance(
    tmp_path, runtime_metadata
):
    """A raw runtime patch cannot make an unproven cache Context-visible."""
    database = _manifest_book_with_act(tmp_path)
    repository = StoryNodeRepository(database)

    assert repository.update_runtime_fields(
        "act-1", runtime_metadata=runtime_metadata
    ) is False
    metadata = json.loads(
        database.get_connection()
        .execute("SELECT metadata FROM story_nodes WHERE id = 'act-1'")
        .fetchone()[0]
    )
    assert metadata == {}

    assert repository.update_runtime_fields(
        "act-1", runtime_metadata={"runtime.progress": 0.5}
    ) is True
