"""Novel clone behavior for immutable outline manifests."""

from pathlib import Path

from domain.structure.outline_contract import OutlinePayload, OutlineSource
from domain.structure.outline_plan import OutlinePlanItem
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.outline_contract_repository import (
    OutlineContractRepository,
)
from scripts.backup_novel import backup_novel


def _seed_manifest_source(database: DatabaseConnection) -> tuple[str, str, str]:
    source_id = "novel-source"
    conn = database.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        (source_id, "Source", source_id, 80),
    )
    conn.commit()
    repository = OutlineContractRepository(database)
    root = repository.ensure_root(source_id)
    draft = repository.save_draft(
        root.id,
        OutlinePayload(
            title="总纲",
            narrative_text="一条完整的长篇故事线。",
            creative_goal="抵达作者锁定的结局",
            entry_state="旧世界仍在",
            exit_state="新世界建立",
        ),
        source=OutlineSource.AUTHOR,
    )
    published = repository.publish_and_sync(
        root.id,
        expected_revision=draft.draft.revision,
    )
    version = conn.execute(
        """
        SELECT version.id, version.digest
        FROM outline_contracts AS contract
        JOIN outline_contract_versions AS version
          ON version.id = contract.active_version_id
        WHERE contract.id = ?
        """,
        (published.id,),
    ).fetchone()
    plan_draft = repository.create_plan_draft(
        novel_id=source_id,
        items=(
            OutlinePlanItem(
                logical_node_id=published.id,
                version_id=str(version["id"]),
                version_digest=str(version["digest"]),
                level=published.level,
                sibling_index=0,
            ),
        ),
        canonical_prefix_digest="source-history-digest",
        canonical_boundary={"formal_head": 3},
        author_intent="保持原定结局",
        created_by="author",
    )
    plan = repository.seal_plan_revision(plan_draft.id)
    conn.execute(
        """
        UPDATE outline_planning_heads
        SET authority_mode = 'manifest', authority_generation = 4,
            active_plan_revision_id = ?, active_plan_digest = ?,
            projection_generation = 4
        WHERE novel_id = ?
        """,
        (plan.id, plan.digest, source_id),
    )
    conn.execute(
        """
        INSERT INTO outline_generation_attempts
            (id, contract_id, status, plan_revision_id)
        VALUES ('source-attempt', ?, 'failed', ?)
        """,
        (published.id, plan.id),
    )
    conn.execute(
        """
        INSERT INTO chapter_candidates
            (id, novel_id, chapter_number, generation_epoch, status,
             plan_revision_id, plan_digest)
        VALUES ('source-candidate', ?, 4, 4, 'stale', ?, ?)
        """,
        (source_id, plan.id, plan.digest),
    )
    conn.execute(
        """
        INSERT INTO outline_operation_keys
            (novel_id, idempotency_key, operation, contract_id, revision)
        VALUES (?, 'source-publish-key', 'publish_and_sync', ?, 1)
        """,
        (source_id, published.id),
    )
    conn.execute(
        """
        INSERT INTO worldline_archives
            (id, novel_id, old_generation_epoch, start_chapter, end_chapter,
             retained_through, target_chapters)
        VALUES ('source-lineage', ?, 3, 1, 3, 0, 80)
        """,
        (source_id,),
    )
    conn.commit()
    return source_id, plan.id, published.id


def test_clone_remaps_active_manifest_and_excludes_runtime_history(tmp_path):
    db_path = Path(tmp_path / "clone-manifest.db")
    database = DatabaseConnection(str(db_path))
    source_id, source_plan_id, source_contract_id = _seed_manifest_source(database)
    source_plan = database.fetch_one(
        "SELECT digest FROM outline_plan_revisions WHERE id = ?",
        (source_plan_id,),
    )
    database.close()

    assert backup_novel(db_path, source_id, "novel-clone") is True

    conn = DatabaseConnection(str(db_path)).get_connection()
    head = conn.execute(
        "SELECT * FROM outline_planning_heads WHERE novel_id = 'novel-clone'"
    ).fetchone()
    plan = conn.execute(
        "SELECT * FROM outline_plan_revisions WHERE id = ?",
        (head["active_plan_revision_id"],),
    ).fetchone()
    item = conn.execute(
        "SELECT * FROM outline_plan_revision_items WHERE plan_revision_id = ?",
        (plan["id"],),
    ).fetchone()
    contract = conn.execute(
        "SELECT * FROM outline_contracts WHERE id = ?",
        (item["logical_node_id"],),
    ).fetchone()

    assert head["authority_mode"] == "manifest"
    assert head["authority_generation"] == 1
    assert head["projection_generation"] == 1
    assert head["working_plan_revision_id"] is None
    assert plan["id"] != source_plan_id
    assert plan["digest"] != source_plan["digest"]
    assert plan["canonical_prefix_digest"] == ""
    assert plan["canonical_boundary_json"] == '{"formal_head": 0}'
    assert plan["publish_idempotency_key"] == ""
    assert contract["id"] != source_contract_id
    assert contract["novel_id"] == "novel-clone"
    assert item["version_id"] != conn.execute(
        "SELECT version_id FROM outline_plan_revision_items "
        "WHERE plan_revision_id = ?",
        (source_plan_id,),
    ).fetchone()[0]

    for table in (
        "outline_generation_attempts",
        "chapter_candidates",
        "outline_operation_keys",
        "worldline_archives",
    ):
        assert conn.execute(
            f"SELECT COUNT(*) FROM {table} WHERE novel_id = 'novel-clone'"
            if "novel_id" in {
                row[1] for row in conn.execute(f"PRAGMA table_info({table})")
            }
            else (
                "SELECT COUNT(*) FROM outline_generation_attempts AS attempt "
                "JOIN outline_contracts AS contract ON contract.id = attempt.contract_id "
                "WHERE contract.novel_id = 'novel-clone'"
            )
        ).fetchone()[0] == 0, table

    conn.execute("PRAGMA foreign_keys = ON")
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_clone_converts_a_synced_legacy_projection_to_one_fresh_manifest(tmp_path):
    db_path = Path(tmp_path / "clone-legacy-outline.db")
    database = DatabaseConnection(str(db_path))
    conn = database.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("novel-legacy", "Legacy", "novel-legacy", 60),
    )
    conn.commit()
    repository = OutlineContractRepository(database)
    root = repository.ensure_root("novel-legacy")
    draft = repository.save_draft(
        root.id,
        OutlinePayload(
            title="旧总纲",
            narrative_text="仍然有效的旧规划。",
            creative_goal="完成结局",
            entry_state="开始",
            exit_state="结束",
        ),
        source=OutlineSource.AUTHOR,
    )
    repository.publish_and_sync(root.id, expected_revision=draft.draft.revision)
    database.close()

    assert backup_novel(db_path, "novel-legacy", "novel-legacy-clone") is True

    cloned = DatabaseConnection(str(db_path)).get_connection()
    head = cloned.execute(
        "SELECT * FROM outline_planning_heads WHERE novel_id = ?",
        ("novel-legacy-clone",),
    ).fetchone()
    assert head["authority_mode"] == "manifest"
    assert head["active_plan_revision_id"]
    assert cloned.execute(
        "SELECT COUNT(*) FROM outline_plan_revision_items WHERE plan_revision_id = ?",
        (head["active_plan_revision_id"],),
    ).fetchone()[0] == 1
