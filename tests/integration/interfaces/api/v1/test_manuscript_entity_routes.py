"""Regression coverage for the manuscript compatibility entity API."""
from __future__ import annotations

import pytest
from fastapi import HTTPException

from application.world.services.bible_service import BibleService
from domain.novel.entities.novel import Novel, NovelStage
from domain.novel.value_objects.novel_id import NovelId
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_bible_repository import SqliteBibleRepository
from infrastructure.persistence.database.sqlite_novel_repository import SqliteNovelRepository
from infrastructure.persistence.database import write_dispatch
from interfaces.api.v1.core import manuscript_entity_routes


@pytest.fixture
def canonical_bible_db(tmp_path, monkeypatch):
    db = DatabaseConnection(str(tmp_path / "plotpilot.db"))
    novel_id = "novel-manuscript-compat"
    novel_repository = SqliteNovelRepository(db)
    novel_repository.save(
        Novel(
            id=NovelId(novel_id),
            title="兼容性测试小说",
            author="测试作者",
            target_chapters=30,
            premise="测试前提",
            stage=NovelStage.PLANNING,
        )
    )
    bible_service = BibleService(
        SqliteBibleRepository(db),
        novel_repository=novel_repository,
    )
    bible_service.create_bible(f"{novel_id}-bible", novel_id)
    bible_service.add_character(
        novel_id=novel_id,
        character_id=f"{novel_id}-char-1",
        name="统一角色",
        description="通过 Bible 服务写入统一角色真源。",
    )
    monkeypatch.setattr(manuscript_entity_routes, "get_database", lambda: db)
    yield db, novel_id
    db.close()


def test_entity_lexicon_reads_canonical_characters_without_legacy_prop_table(canonical_bible_db):
    _db, novel_id = canonical_bible_db

    payload = manuscript_entity_routes.get_entity_lexicon(novel_id)

    assert payload["characters"] == [
        {"id": f"{novel_id}-char-1", "name": "统一角色", "aliases": []}
    ]
    assert payload["props"] == []


def test_manuscript_prop_accepts_canonical_bible_character_as_holder(canonical_bible_db):
    db, novel_id = canonical_bible_db
    holder_id = f"{novel_id}-char-1"

    created = manuscript_entity_routes.create_prop(
        novel_id,
        manuscript_entity_routes.PropCreateBody(
            name="验证道具",
            description="由统一角色持有。",
            aliases=["验证物"],
            holder_character_id=holder_id,
            first_chapter=2,
        ),
    )

    assert created["holder_character_id"] == holder_id
    assert created["first_chapter"] == 2
    stored = db.fetch_one(
        "SELECT holder_character_id, introduced_chapter FROM unified_props WHERE id = ?",
        (created["id"],),
    )
    assert dict(stored) == {"holder_character_id": holder_id, "introduced_chapter": 2}
    assert manuscript_entity_routes.list_props(novel_id)["props"] == [created]


def test_manuscript_prop_rejects_whitespace_holder_as_a_validation_error(canonical_bible_db):
    _db, novel_id = canonical_bible_db

    with pytest.raises(HTTPException) as exc_info:
        manuscript_entity_routes.create_prop(
            novel_id,
            manuscript_entity_routes.PropCreateBody(
                name="无效持有者道具",
                holder_character_id="   ",
            ),
        )

    assert exc_info.value.status_code == 422


def test_manuscript_prop_create_returns_committed_row_before_write_dispatch_consumes(
    canonical_bible_db, monkeypatch
):
    """Compatibility POST must not return an empty object while its write is only queued."""
    db, novel_id = canonical_bible_db
    holder_id = f"{novel_id}-char-1"
    enqueued_sql = []

    monkeypatch.setenv("PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES", "0")
    monkeypatch.delenv("AITEXT_ALLOW_DIRECT_SQLITE_WRITES", raising=False)
    monkeypatch.setattr(
        write_dispatch,
        "enqueue_execute_sql",
        lambda sql, params: enqueued_sql.append((sql, params)) or True,
    )

    created = manuscript_entity_routes.create_prop(
        novel_id,
        manuscript_entity_routes.PropCreateBody(
            name="同步确认道具",
            description="响应必须是已提交的道具。",
            aliases=["确认物"],
            holder_character_id=holder_id,
            first_chapter=3,
        ),
    )

    assert created["name"] == "同步确认道具"
    assert created["holder_character_id"] == holder_id
    assert created["first_chapter"] == 3
    assert enqueued_sql == []
    stored = db.fetch_one(
        "SELECT holder_character_id, introduced_chapter FROM unified_props WHERE id = ?",
        (created["id"],),
    )
    assert dict(stored) == {"holder_character_id": holder_id, "introduced_chapter": 3}


def test_manuscript_prop_patch_returns_updated_row_before_write_dispatch_consumes(
    canonical_bible_db, monkeypatch
):
    """Compatibility PATCH must not return the old object while its change is only queued."""
    db, novel_id = canonical_bible_db
    holder_id = f"{novel_id}-char-1"
    existing = manuscript_entity_routes.create_prop(
        novel_id,
        manuscript_entity_routes.PropCreateBody(
            name="待更新道具",
            description="初始描述。",
            holder_character_id=holder_id,
            first_chapter=1,
        ),
    )
    enqueued_sql = []

    monkeypatch.setenv("PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES", "0")
    monkeypatch.delenv("AITEXT_ALLOW_DIRECT_SQLITE_WRITES", raising=False)
    monkeypatch.setattr(
        write_dispatch,
        "enqueue_execute_sql",
        lambda sql, params: enqueued_sql.append((sql, params)) or True,
    )

    updated = manuscript_entity_routes.patch_prop(
        novel_id,
        existing["id"],
        manuscript_entity_routes.PropPatchBody(
            name="已更新道具",
            description="已确认更新。",
            aliases=["新别名"],
            first_chapter=4,
            is_key=True,
        ),
    )

    assert updated["name"] == "已更新道具"
    assert updated["description"] == "已确认更新。"
    assert updated["aliases_json"] == '["新别名"]'
    assert updated["holder_character_id"] == holder_id
    assert updated["first_chapter"] == 4
    assert updated["is_key"] == 1
    assert enqueued_sql == []
    stored = db.fetch_one(
        "SELECT name, description, introduced_chapter, attributes_json FROM unified_props WHERE id = ?",
        (existing["id"],),
    )
    assert dict(stored) == {
        "name": "已更新道具",
        "description": "已确认更新。",
        "introduced_chapter": 4,
        "attributes_json": '{"is_key": true}',
    }


def test_manuscript_prop_delete_is_visible_before_write_dispatch_consumes(
    canonical_bible_db, monkeypatch
):
    """Compatibility DELETE must make the prop unavailable before returning 204."""
    db, novel_id = canonical_bible_db
    existing = manuscript_entity_routes.create_prop(
        novel_id,
        manuscript_entity_routes.PropCreateBody(name="待删除道具"),
    )
    enqueued_sql = []

    monkeypatch.setenv("PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES", "0")
    monkeypatch.delenv("AITEXT_ALLOW_DIRECT_SQLITE_WRITES", raising=False)
    monkeypatch.setattr(
        write_dispatch,
        "enqueue_execute_sql",
        lambda sql, params: enqueued_sql.append((sql, params)) or True,
    )

    response = manuscript_entity_routes.delete_prop(novel_id, existing["id"])

    assert response.status_code == 204
    assert enqueued_sql == []
    assert db.fetch_one(
        "SELECT id FROM unified_props WHERE novel_id = ? AND id = ?",
        (novel_id, existing["id"]),
    ) is None
