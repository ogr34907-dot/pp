"""Novel clone behavior for immutable outline manifests."""

import hashlib
import json
from pathlib import Path

import pytest

from application.blueprint.services.outline_contract_service import OutlineContractService
from domain.structure.outline_contract import OutlineLevel, OutlinePayload, OutlineSource
from domain.structure.outline_plan import OutlinePlanItem, canonical_plan_digest
from domain.structure.story_node import NodeType, StoryNode
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.outline_contract_repository import (
    OutlineContractRepository,
)
from infrastructure.persistence.database.story_node_repository import StoryNodeRepository
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


def _seed_manifest_source_with_physical_chain(
    database: DatabaseConnection,
) -> tuple[str, set[str]]:
    source_id = "novel-physical-source"
    conn = database.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        (source_id, "Physical Source", source_id, 20),
    )
    conn.commit()
    contracts = OutlineContractRepository(database)
    nodes = StoryNodeRepository(database)
    service = OutlineContractService(
        contract_repository=contracts,
        story_node_repository=nodes,
    )
    payload = OutlinePayload(
        title="完整大纲",
        narrative_text="主角以代价推进冲突，并把后果留给下一阶段。",
        creative_goal="推进主线",
        entry_state="承接前态",
        exit_state="留下后态",
        required_events=["发生不可逆选择"],
        state_changes={"characters": [{"name": "主角", "change": "承担代价"}]},
        handoff_conditions=["后续阶段回应本阶段结局"],
        chapter_start=1,
        chapter_end=1,
    )
    root = contracts.ensure_root(source_id)
    root = contracts.save_draft(root.id, payload, source=OutlineSource.AUTHOR)
    published_root = contracts.publish_and_sync(
        root.id, expected_revision=root.draft.revision
    )
    root_version = conn.execute(
        """
        SELECT id, digest FROM outline_contract_versions
        WHERE id = (SELECT active_version_id FROM outline_contracts WHERE id = ?)
        """,
        (published_root.id,),
    ).fetchone()
    parent_item = OutlinePlanItem(
        logical_node_id=published_root.id,
        version_id=str(root_version["id"]),
        version_digest=str(root_version["digest"]),
        level=OutlineLevel.OUTLINE,
        sibling_index=0,
        expansion_state="expanded",
    )
    items = [parent_item]
    parent_node_id = None
    physical_ids: set[str] = set()
    for index, node_type in enumerate(
        (NodeType.PART, NodeType.VOLUME, NodeType.ACT, NodeType.CHAPTER), start=1
    ):
        node = StoryNode(
            id=f"{source_id}-{node_type.value}",
            novel_id=source_id,
            node_type=node_type,
            number=1,
            title=node_type.value,
            order_index=index,
            parent_id=parent_node_id,
        )
        nodes.save_sync(node)
        physical_ids.add(node.id)
        slot = service.ensure_contract_for_story_node(source_id, node.id)
        slot = contracts.save_draft(slot.id, payload, source=OutlineSource.AUTHOR)
        published = contracts.publish_and_sync(
            slot.id, expected_revision=slot.draft.revision
        )
        version = conn.execute(
            """
            SELECT id, digest FROM outline_contract_versions
            WHERE id = (SELECT active_version_id FROM outline_contracts WHERE id = ?)
            """,
            (published.id,),
        ).fetchone()
        item = OutlinePlanItem(
            logical_node_id=published.id,
            version_id=str(version["id"]),
            version_digest=str(version["digest"]),
            parent_logical_node_id=parent_item.logical_node_id,
            level=published.level,
            sibling_index=0,
            expansion_state=(
                "unexpanded" if node_type == NodeType.CHAPTER else "expanded"
            ),
            validated_parent_digest=parent_item.version_digest,
        )
        items.append(item)
        parent_item = item
        parent_node_id = node.id

    plan_draft = contracts.create_plan_draft(
        novel_id=source_id,
        items=tuple(items),
        canonical_prefix_digest="physical-source-history",
        canonical_boundary={"formal_head": 0},
    )
    plan = contracts.seal_plan_revision(plan_draft.id)
    conn.execute(
        """
        UPDATE outline_planning_heads
        SET authority_mode = 'manifest', authority_generation = 1,
            active_plan_revision_id = ?, active_plan_digest = ?,
            projection_generation = 1
        WHERE novel_id = ?
        """,
        (plan.id, plan.digest, source_id),
    )
    conn.commit()
    return source_id, physical_ids


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
    assert plan["status"] == "ready_for_review"
    assert plan["sealed_at"]
    assert contract["id"] != source_contract_id
    assert contract["novel_id"] == "novel-clone"
    assert item["version_id"] != conn.execute(
        "SELECT version_id FROM outline_plan_revision_items "
        "WHERE plan_revision_id = ?",
        (source_plan_id,),
    ).fetchone()[0]

    manifest_items = conn.execute(
        """
        SELECT item.*, version.digest AS version_digest, version.sealed_at
        FROM outline_plan_revision_items AS item
        JOIN outline_contract_versions AS version ON version.id = item.version_id
        WHERE item.plan_revision_id = ?
        """,
        (plan["id"],),
    ).fetchall()
    assert manifest_items
    assert all(row["sealed_at"] for row in manifest_items)
    logical_items = tuple(
        OutlinePlanItem(
            logical_node_id=str(row["logical_node_id"]),
            version_id=str(row["version_id"]),
            version_digest=str(row["version_digest"]),
            parent_logical_node_id=row["parent_logical_node_id"],
            level=OutlineLevel(str(row["level"])),
            sibling_index=int(row["sibling_index"]),
            expansion_state=str(row["expansion_state"]),
            validated_parent_digest=str(row["validated_parent_digest"] or ""),
            validated_previous_sibling_digest=str(
                row["validated_previous_sibling_digest"] or ""
            ),
            is_reused=bool(row["is_reused"]),
        )
        for row in manifest_items
    )
    assert canonical_plan_digest(
        canonical_prefix_digest=str(plan["canonical_prefix_digest"]),
        items=logical_items,
    ) == plan["digest"] == head["active_plan_digest"]
    active_projections = conn.execute(
        """
        SELECT contract_id, version_id, digest
        FROM outline_plan_projections
        WHERE novel_id = ? AND is_active = 1
        """,
        ("novel-clone",),
    ).fetchall()
    assert {
        (row["contract_id"], row["version_id"], row["digest"])
        for row in active_projections
    } == {
        (row.logical_node_id, row.version_id, row.version_digest)
        for row in logical_items
    }
    clone_binding = conn.execute(
        """
        SELECT binding.*
        FROM outline_plan_projection_bindings AS binding
        JOIN outline_plan_revision_items AS item
          ON item.id = binding.plan_revision_item_id
        WHERE item.plan_revision_id = ?
        """,
        (plan["id"],),
    ).fetchone()
    assert clone_binding is not None
    assert tuple(clone_binding[1:]) == (None, None, None, None)

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


def test_clone_remaps_manifest_projection_bindings_to_cloned_story_nodes(tmp_path):
    db_path = Path(tmp_path / "clone-physical-manifest.db")
    database = DatabaseConnection(str(db_path))
    source_id, source_physical_ids = _seed_manifest_source_with_physical_chain(
        database
    )
    source_item_rows = database.get_connection().execute(
        """
        SELECT item.id FROM outline_plan_revision_items AS item
        JOIN outline_planning_heads AS head
          ON head.active_plan_revision_id = item.plan_revision_id
        WHERE head.novel_id = ?
        """,
        (source_id,),
    ).fetchall()
    source_item_ids = {row[0] for row in source_item_rows}
    database.close()

    assert backup_novel(db_path, source_id, "novel-physical-clone") is True

    conn = DatabaseConnection(str(db_path)).get_connection()
    clone_plan_id = conn.execute(
        "SELECT active_plan_revision_id FROM outline_planning_heads WHERE novel_id = ?",
        ("novel-physical-clone",),
    ).fetchone()[0]
    rows = conn.execute(
        """
        SELECT item.level, binding.plan_revision_item_id, binding.story_node_id,
               binding.parent_story_node_id, binding.number, binding.order_index,
               contract.story_node_id AS contract_story_node_id
        FROM outline_plan_projection_bindings AS binding
        JOIN outline_plan_revision_items AS item
          ON item.id = binding.plan_revision_item_id
        JOIN outline_contracts AS contract ON contract.id = item.logical_node_id
        WHERE item.plan_revision_id = ?
        ORDER BY CASE item.level
            WHEN 'outline' THEN 0 WHEN 'part' THEN 1 WHEN 'volume' THEN 2
            WHEN 'act' THEN 3 ELSE 4 END
        """,
        (clone_plan_id,),
    ).fetchall()
    clone_story_node_ids = {
        row[0]
        for row in conn.execute(
            "SELECT id FROM story_nodes WHERE novel_id = ?",
            ("novel-physical-clone",),
        ).fetchall()
    }

    assert len(rows) == 5
    assert tuple(rows[0][2:6]) == (None, None, None, None)
    assert rows[0][6] is None
    assert all(row[1] not in source_item_ids for row in rows)
    bound_rows = rows[1:]
    assert {row[2] for row in bound_rows}.isdisjoint(source_physical_ids)
    assert {row[2] for row in bound_rows} <= clone_story_node_ids
    assert {
        row[3] for row in bound_rows if row[3] is not None
    } <= clone_story_node_ids
    assert all(row[2] == row[6] for row in bound_rows)
    assert conn.execute("PRAGMA foreign_key_check").fetchall() == []


def test_clone_uses_frozen_manifest_topology_and_version_over_cache_drift(tmp_path):
    db_path = Path(tmp_path / "clone-manifest-cache-drift.db")
    database = DatabaseConnection(str(db_path))
    source_id, source_physical_ids = _seed_manifest_source_with_physical_chain(
        database
    )
    source_conn = database.get_connection()
    source_items = source_conn.execute(
        """
        SELECT item.logical_node_id, item.parent_logical_node_id, item.version_id,
               item.level
        FROM outline_plan_revision_items AS item
        JOIN outline_planning_heads AS head
          ON head.active_plan_revision_id = item.plan_revision_id
        WHERE head.novel_id = ?
        """,
        (source_id,),
    ).fetchall()
    source_by_level = {str(row["level"]): row for row in source_items}
    chapter_item = source_by_level[OutlineLevel.CHAPTER.value]
    root_item = source_by_level[OutlineLevel.OUTLINE.value]
    assert chapter_item["parent_logical_node_id"] != root_item["logical_node_id"]

    source_conn.execute(
        "UPDATE outline_contracts SET parent_contract_id = ? WHERE id = ?",
        (root_item["logical_node_id"], chapter_item["logical_node_id"]),
    )
    frozen_version = source_conn.execute(
        "SELECT revision, payload_json FROM outline_contract_versions WHERE id = ?",
        (chapter_item["version_id"],),
    ).fetchone()
    assert frozen_version is not None
    drift_payload = OutlinePayload.from_dict(
        json.loads(str(frozen_version["payload_json"]))
    )
    drift_payload.extra = {**drift_payload.extra, "cache_only_drift": True}
    drift_version_id = "source-cache-only-version"
    next_revision = source_conn.execute(
        "SELECT COALESCE(MAX(revision), 0) + 1 FROM outline_contract_versions "
        "WHERE contract_id = ?",
        (chapter_item["logical_node_id"],),
    ).fetchone()[0]
    source_conn.execute(
        """
        INSERT INTO outline_contract_versions
            (id, contract_id, revision, payload_json, digest,
             parent_revision_digest, source, status)
        VALUES (?, ?, ?, ?, ?, '', 'author', 'synced')
        """,
        (
            drift_version_id,
            chapter_item["logical_node_id"],
            next_revision,
            json.dumps(
                drift_payload.canonical_dict(),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ),
            drift_payload.digest,
        ),
    )
    source_conn.execute(
        "UPDATE outline_contracts SET active_version_id = ? WHERE id = ?",
        (drift_version_id, chapter_item["logical_node_id"]),
    )
    source_conn.commit()
    assert source_conn.execute(
        "SELECT COUNT(*) FROM outline_plan_projection_bindings AS binding "
        "JOIN outline_plan_revision_items AS item "
        "ON item.id = binding.plan_revision_item_id "
        "JOIN outline_planning_heads AS head "
        "ON head.active_plan_revision_id = item.plan_revision_id "
        "WHERE head.novel_id = ?",
        (source_id,),
    ).fetchone()[0] == len(source_items)
    database.close()

    assert backup_novel(db_path, source_id, "novel-cache-drift-clone") is True

    clone_conn = DatabaseConnection(str(db_path)).get_connection()
    clone_plan_id = clone_conn.execute(
        "SELECT active_plan_revision_id FROM outline_planning_heads WHERE novel_id = ?",
        ("novel-cache-drift-clone",),
    ).fetchone()[0]
    cloned_rows = clone_conn.execute(
        """
        SELECT item.logical_node_id, item.parent_logical_node_id, item.version_id,
               item.level, contract.parent_contract_id, contract.active_version_id,
               binding.story_node_id, binding.parent_story_node_id
        FROM outline_plan_revision_items AS item
        JOIN outline_contracts AS contract ON contract.id = item.logical_node_id
        JOIN outline_plan_projection_bindings AS binding
          ON binding.plan_revision_item_id = item.id
        WHERE item.plan_revision_id = ?
        """,
        (clone_plan_id,),
    ).fetchall()
    clone_by_level = {str(row["level"]): row for row in cloned_rows}
    clone_id_by_source_logical_id = {
        str(source_by_level[level]["logical_node_id"]): row["logical_node_id"]
        for level, row in clone_by_level.items()
    }
    for level, cloned_row in clone_by_level.items():
        source_item = source_by_level[level]
        source_parent = source_item["parent_logical_node_id"]
        expected_parent = (
            clone_id_by_source_logical_id[str(source_parent)]
            if source_parent is not None
            else None
        )
        assert cloned_row["parent_contract_id"] == expected_parent
        assert cloned_row["active_version_id"] == cloned_row["version_id"]

    clone_physical_ids = {
        row[0]
        for row in clone_conn.execute(
            "SELECT id FROM story_nodes WHERE novel_id = ?",
            ("novel-cache-drift-clone",),
        ).fetchall()
    }
    bound_rows = [row for row in cloned_rows if row["story_node_id"] is not None]
    assert {row["story_node_id"] for row in bound_rows}.isdisjoint(source_physical_ids)
    assert {row["story_node_id"] for row in bound_rows} <= clone_physical_ids
    assert {
        row["parent_story_node_id"]
        for row in bound_rows
        if row["parent_story_node_id"] is not None
    } <= clone_physical_ids


def test_clone_rejects_manifest_source_with_mismatched_active_projection(tmp_path):
    db_path = Path(tmp_path / "clone-invalid-manifest.db")
    database = DatabaseConnection(str(db_path))
    source_id, _, _ = _seed_manifest_source(database)
    conn = database.get_connection()
    conn.execute(
        "UPDATE outline_plan_projections SET digest = 'tampered' "
        "WHERE novel_id = ? AND is_active = 1",
        (source_id,),
    )
    conn.commit()
    database.close()

    with pytest.raises(ValueError, match="projection"):
        backup_novel(db_path, source_id, "novel-clone")

    clone = DatabaseConnection(str(db_path)).get_connection()
    assert clone.execute(
        "SELECT COUNT(*) FROM novels WHERE id = 'novel-clone'"
    ).fetchone()[0] == 0


def test_clone_rejects_manifest_source_with_version_payload_digest_mismatch(tmp_path):
    """A sealed version digest must cover the payload copied into a clone."""
    db_path = Path(tmp_path / "clone-invalid-version-payload.db")
    database = DatabaseConnection(str(db_path))
    source_id, source_plan_id, source_contract_id = _seed_manifest_source(database)
    conn = database.get_connection()
    source_item = conn.execute(
        """
        SELECT item.version_id, version.digest
        FROM outline_plan_revision_items AS item
        JOIN outline_contract_versions AS version ON version.id = item.version_id
        WHERE item.plan_revision_id = ?
        """,
        (source_plan_id,),
    ).fetchone()
    malformed_version_id = "source-malformed-version"
    conn.execute(
        """
        INSERT INTO outline_contract_versions
            (id, contract_id, revision, payload_json, digest,
             parent_revision_digest, source, status, sealed_at)
        VALUES (?, ?, 99, ?, ?, '', 'author', 'synced', ?)
        """,
        (
            malformed_version_id,
            source_contract_id,
            json.dumps({"title": "tampered", "narrative_text": "tampered"}),
            source_item["digest"],
            "2026-08-16T00:00:00",
        ),
    )
    malformed_item = OutlinePlanItem(
        logical_node_id=source_contract_id,
        version_id=malformed_version_id,
        version_digest=str(source_item["digest"]),
        level=OutlineLevel.OUTLINE,
        sibling_index=0,
    )
    malformed_plan_id = "source-malformed-plan"
    malformed_digest = canonical_plan_digest(
        canonical_prefix_digest="",
        items=(malformed_item,),
    )
    conn.execute(
        """
        INSERT INTO outline_plan_revisions
            (id, novel_id, revision, status, digest, canonical_prefix_digest,
             canonical_boundary_json, reconciliation_status, created_by,
             created_at, updated_at)
        VALUES (?, ?, 99, 'draft', ?, '', '{"formal_head": 0}', 'aligned',
                'test', ?, ?)
        """,
        (malformed_plan_id, source_id, malformed_digest, "2026-08-16T00:00:00", "2026-08-16T00:00:00"),
    )
    conn.execute(
        """
        INSERT INTO outline_plan_revision_items
            (id, plan_revision_id, logical_node_id, version_id, level,
             sibling_index, validated_parent_digest,
             validated_previous_sibling_digest)
        VALUES ('source-malformed-item', ?, ?, ?, 'outline', 0, '', '')
        """,
        (malformed_plan_id, source_contract_id, malformed_version_id),
    )
    conn.execute(
        """
        INSERT INTO outline_plan_projection_bindings
            (plan_revision_item_id, story_node_id, parent_story_node_id,
             number, order_index)
        VALUES ('source-malformed-item', NULL, NULL, NULL, NULL)
        """
    )
    conn.execute(
        """
        UPDATE outline_plan_revisions
        SET status = 'ready_for_review', sealed_at = ?, updated_at = ?
        WHERE id = ?
        """,
        ("2026-08-16T00:00:00", "2026-08-16T00:00:00", malformed_plan_id),
    )
    conn.execute(
        "UPDATE outline_plan_projections SET is_active = 0 WHERE novel_id = ?",
        (source_id,),
    )
    conn.execute(
        "UPDATE outline_contracts SET active_version_id = ? WHERE id = ?",
        (malformed_version_id, source_contract_id),
    )
    conn.execute(
        """
        INSERT INTO outline_plan_projections
            (id, novel_id, contract_id, version_id, digest, payload_json,
             is_active, synced_at, created_at)
        VALUES ('source-malformed-projection', ?, ?, ?, ?, ?, 1, ?, ?)
        """,
        (
            source_id,
            source_contract_id,
            malformed_version_id,
            source_item["digest"],
            json.dumps({"title": "tampered", "narrative_text": "tampered"}),
            "2026-08-16T00:00:00",
            "2026-08-16T00:00:00",
        ),
    )
    conn.execute(
        """
        UPDATE outline_planning_heads
        SET authority_generation = authority_generation + 1,
            projection_generation = projection_generation + 1,
            active_plan_revision_id = ?, active_plan_digest = ?
        WHERE novel_id = ?
        """,
        (malformed_plan_id, malformed_digest, source_id),
    )
    conn.commit()
    database.close()

    with pytest.raises(ValueError, match="payload"):
        backup_novel(db_path, source_id, "novel-clone")

    clone = DatabaseConnection(str(db_path)).get_connection()
    assert clone.execute(
        "SELECT COUNT(*) FROM novels WHERE id = 'novel-clone'"
    ).fetchone()[0] == 0


@pytest.mark.parametrize(
    ("column", "value", "message"),
    [
        ("status", "published", "ready_for_review"),
        ("reconciliation_status", "repairable", "aligned"),
    ],
)
def test_clone_rejects_manifest_source_without_ready_aligned_head(
    tmp_path, column, value, message
):
    """Clone only accepts the same active Manifest contract as generation."""
    db_path = Path(tmp_path / f"clone-invalid-{column}.db")
    database = DatabaseConnection(str(db_path))
    source_id, plan_id, _ = _seed_manifest_source(database)
    conn = database.get_connection()
    # Simulate a damaged/pre-hardening database snapshot.  The clone validator
    # must still refuse it even when the current immutable-row trigger was not
    # present when the bad Head was written.
    conn.execute("DROP TRIGGER trg_outline_plan_revisions_sealed_update")
    conn.execute(
        f"UPDATE outline_plan_revisions SET {column} = ? WHERE id = ?",
        (value, plan_id),
    )
    conn.commit()
    database.close()

    with pytest.raises(ValueError, match=message):
        backup_novel(db_path, source_id, "novel-clone")

    clone = DatabaseConnection(str(db_path)).get_connection()
    assert clone.execute(
        "SELECT COUNT(*) FROM novels WHERE id = 'novel-clone'"
    ).fetchone()[0] == 0


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


def test_clone_strips_runtime_and_canonical_summary_provenance(tmp_path):
    db_path = Path(tmp_path / "clone-runtime-summary.db")
    database = DatabaseConnection(str(db_path))
    source_id, _, _ = _seed_manifest_source(database)
    conn = database.get_connection()
    root_node_id = "source-part"
    conn.execute(
        """
        INSERT INTO story_nodes
            (id, novel_id, node_type, number, title, order_index)
        VALUES (?, ?, 'part', 1, 'Part', 0)
        """,
        (root_node_id, source_id),
    )
    conn.execute(
        "UPDATE story_nodes SET metadata = ? WHERE id = ?",
        (
            json.dumps(
                {
                    "author.note": "keep this planning annotation",
                    "summary": "old canonical summary",
                    "summary_state": {"status": "committed"},
                    "checkpoint_summary": "old checkpoint",
                    "runtime.summary": "old runtime summary",
                    "runtime.summary_state": {"status": "committed"},
                    "runtime.continuity_hint": "old runtime hint",
                    "summary_provenance": {"status": "committed"},
                },
                ensure_ascii=False,
            ),
            root_node_id,
        ),
    )
    conn.execute(
        """
        INSERT INTO story_nodes
            (id, novel_id, parent_id, node_type, number, title, order_index)
        VALUES ('source-chapter-1', ?, ?, 'chapter', 1, 'Chapter', 0)
        """,
        (source_id, root_node_id),
    )
    conn.execute(
        """
        INSERT INTO chapters
            (id, novel_id, number, title, content, content_sha256,
             content_revision, status)
        VALUES ('source-chapter-1', ?, 1, 'Chapter', 'old formal body',
                'old-formal-hash', 9, 'completed')
        """,
        (source_id,),
    )
    conn.execute(
        "INSERT INTO knowledge (id, novel_id, premise_lock) VALUES (?, ?, ?)",
        ("source-knowledge", source_id, "保留作者设定"),
    )
    conn.execute(
        """
        INSERT INTO chapter_summaries
            (id, knowledge_id, chapter_number, summary, source_content_sha256,
             source_content_revision, pipeline_version, sync_status, sync_error,
             sync_attempts, canonical_payload_sha256)
        VALUES (?, ?, 1, ?, ?, 9, ?, 'committed', ?, 4, ?)
        """,
        (
            "source-summary-1",
            "source-knowledge",
            "old canonical summary",
            "old-formal-hash",
            "old-summary-pipeline",
            "old failure provenance",
            "old-canonical-payload",
        ),
    )
    conn.commit()
    database.close()

    assert backup_novel(db_path, source_id, "novel-clone") is True

    clone = DatabaseConnection(str(db_path)).get_connection()
    metadata = json.loads(
        clone.execute(
            "SELECT metadata FROM story_nodes WHERE novel_id = ? AND node_type = 'part'",
            ("novel-clone",),
        ).fetchone()[0]
    )
    assert metadata == {"author.note": "keep this planning annotation"}
    chapter = clone.execute(
        """
        SELECT content, content_sha256, content_revision, status
        FROM chapters WHERE novel_id = ? AND number = 1
        """,
        ("novel-clone",),
    ).fetchone()
    assert tuple(chapter) == (
        "",
        hashlib.sha256(b"").hexdigest(),
        1,
        "draft",
    )
    summary = clone.execute(
        """
        SELECT summary.source_content_sha256, summary.source_content_revision,
               summary.pipeline_version, summary.sync_status, summary.sync_error,
               summary.sync_attempts, summary.canonical_payload_sha256,
               knowledge.premise_lock
        FROM chapter_summaries AS summary
        JOIN knowledge ON knowledge.id = summary.knowledge_id
        WHERE knowledge.novel_id = ? AND summary.chapter_number = 1
        """,
        ("novel-clone",),
    ).fetchone()
    assert tuple(summary) == (
        "",
        0,
        "clone-reset",
        "draft",
        "",
        0,
        "",
        "保留作者设定",
    )


def test_clone_resets_history_derived_character_and_storyline_progress(tmp_path):
    db_path = Path(tmp_path / "clone-history-derived-state.db")
    database = DatabaseConnection(str(db_path))
    source_id, _, _ = _seed_manifest_source(database)
    conn = database.get_connection()

    conn.execute(
        """
        INSERT INTO unified_characters
            (id, novel_id, name, description, public_profile, hidden_profile,
             verbal_tic, voice_style, active_wounds_json, mental_state,
             mental_state_reason, emotional_arc_json, current_state_summary,
             last_updated_chapter, created_at, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "unified-character-source-1",
            source_id,
            "林澈",
            "出身钟楼镇的修缮师。",
            "她擅长修复遗迹。",
            "她知道钟楼的真实来历。",
            "嗯。",
            "冷静克制",
            '["钟楼坍塌留下的旧伤"]',
            "ANXIOUS",
            "第九章后担心同伴被困。",
            '["从警惕到信任"]',
            "仍被困在钟楼地窖。",
            9,
            "2026-08-15T00:00:00",
            "2026-08-15T00:00:00",
        ),
    )

    conn.execute(
        """
        INSERT INTO character_states
            (character_id, novel_id, base_traits, scars, motivations,
             emotional_arc, current_state_summary, last_updated_chapter)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "character-source-1",
            source_id,
            '["勇敢", "谨慎"]',
            '["钟楼旧伤"]',
            '["找到失踪的兄长"]',
            '["从怀疑到信任"]',
            "第九章后仍被困在钟楼",
            9,
        ),
    )
    conn.execute(
        """
        INSERT INTO character_states
            (character_id, novel_id, base_traits, scars, motivations,
             emotional_arc, current_state_summary, last_updated_chapter)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "unified-character-source-1",
            source_id,
            '["精通遗迹修缮"]',
            '[]',
            '[]',
            '[]',
            "不应遗留的统一角色状态",
            9,
        ),
    )

    storyline_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(storylines)")
    }
    for column, declaration in (
        ("last_active_chapter", "INTEGER DEFAULT 0"),
        ("progress_summary", "TEXT DEFAULT ''"),
    ):
        if column not in storyline_columns:
            conn.execute(f"ALTER TABLE storylines ADD COLUMN {column} {declaration}")
    storyline_columns = {
        row[1] for row in conn.execute("PRAGMA table_info(storylines)")
    }
    storyline_values = {
        "id": "source-storyline-1",
        "novel_id": source_id,
        "storyline_type": "main",
        "status": "active",
        "estimated_chapter_start": 1,
        "estimated_chapter_end": 20,
        "current_milestone_index": 3,
        "extensions": '{"author_goal":"守住旧城"}',
        "last_active_chapter": 9,
        "progress_summary": "第九章已完成关键转折",
    }
    insert_columns = [
        column for column in storyline_values if column in storyline_columns
    ]
    conn.execute(
        f"INSERT INTO storylines ({', '.join(insert_columns)}) "
        f"VALUES ({', '.join('?' for _ in insert_columns)})",
        [storyline_values[column] for column in insert_columns],
    )
    conn.execute(
        """
        INSERT INTO storyline_milestones
            (id, storyline_id, milestone_order, title, description,
             target_chapter_start, target_chapter_end, prerequisite_list,
             milestone_triggers)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            "source-milestone-1",
            "source-storyline-1",
            0,
            "钟楼会面",
            "主角与线人会面。",
            2,
            3,
            "[]",
            "[]",
        ),
    )
    conn.commit()
    database.close()

    assert backup_novel(db_path, source_id, "novel-clone") is True

    clone = DatabaseConnection(str(db_path)).get_connection()
    character_state = clone.execute(
        """
        SELECT base_traits, scars, motivations, emotional_arc,
               current_state_summary, last_updated_chapter
        FROM character_states
        WHERE novel_id = ? AND character_id = ?
        """,
        ("novel-clone", "character-source-1"),
    ).fetchone()
    assert tuple(character_state) == (
        '["勇敢", "谨慎"]',
        "[]",
        "[]",
        "[]",
        "",
        0,
    )
    unified_character = clone.execute(
        """
        SELECT id, name, description, public_profile, hidden_profile, verbal_tic,
               voice_style, active_wounds_json, mental_state,
               mental_state_reason, emotional_arc_json, current_state_summary,
               last_updated_chapter
        FROM unified_characters
        WHERE novel_id = ?
        """,
        ("novel-clone",),
    ).fetchone()
    assert tuple(unified_character) == (
        unified_character[0],
        "林澈",
        "出身钟楼镇的修缮师。",
        "她擅长修复遗迹。",
        "她知道钟楼的真实来历。",
        "嗯。",
        "冷静克制",
        "[]",
        "NORMAL",
        "",
        "[]",
        "",
        0,
    )
    remapped_state = clone.execute(
        """
        SELECT character_id, base_traits, scars, motivations, emotional_arc,
               current_state_summary, last_updated_chapter
        FROM character_states
        WHERE novel_id = ? AND character_id = ?
        """,
        ("novel-clone", unified_character[0]),
    ).fetchone()
    assert tuple(remapped_state) == (
        unified_character[0],
        '["精通遗迹修缮"]',
        "[]",
        "[]",
        "[]",
        "",
        0,
    )
    storyline = clone.execute(
        """
        SELECT id, storyline_type, status, estimated_chapter_start,
               estimated_chapter_end, current_milestone_index, extensions,
               last_active_chapter, progress_summary
        FROM storylines WHERE novel_id = ?
        """,
        ("novel-clone",),
    ).fetchone()
    assert storyline[1:7] == (
        "main",
        "active",
        1,
        20,
        0,
        '{"author_goal":"守住旧城"}',
    )
    assert tuple(storyline[7:]) == (0, "")
    milestone = clone.execute(
        """
        SELECT milestone_order, title, description,
               target_chapter_start, target_chapter_end, prerequisite_list,
               milestone_triggers
        FROM storyline_milestones WHERE storyline_id = ?
        """,
        (storyline[0],),
    ).fetchone()
    assert tuple(milestone) == (
        0,
        "钟楼会面",
        "主角与线人会面。",
        2,
        3,
        "[]",
        "[]",
    )
