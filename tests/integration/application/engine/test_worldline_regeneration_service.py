"""Tail regeneration archives all chapter-derived facts before a new epoch starts."""

import hashlib
import json
import pytest

from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.chapter_candidate_repository import (
    ChapterCandidateRepository,
)
from application.engine.services.worldline_regeneration_service import (
    WorldlineRegenerationError,
    WorldlineRegenerationService,
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
