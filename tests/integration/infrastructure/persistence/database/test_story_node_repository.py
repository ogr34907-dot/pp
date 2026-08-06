"""StoryNodeRepository integration tests."""

import asyncio
import sqlite3
from pathlib import Path

import pytest

from domain.structure.story_node import NodeType, StoryNode
from infrastructure.persistence.database.story_node_repository import StoryNodeRepository

SCHEMA_PATH = (
    Path(__file__).resolve().parents[5]
    / "infrastructure"
    / "persistence"
    / "database"
    / "schema.sql"
)


@pytest.fixture
def repo_db(tmp_path):
    db_path = tmp_path / "story-node-repo.db"
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()
    conn.close()
    return StoryNodeRepository(str(db_path)), db_path


def test_delete_cascades_to_descendant_story_nodes(repo_db):
    repo, db_path = repo_db
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("novel-1", "Novel 1", "novel-1", 10),
    )
    conn.commit()
    conn.close()

    repo.save_sync(
        StoryNode(
            id="volume-1",
            novel_id="novel-1",
            node_type=NodeType.VOLUME,
            number=1,
            title="Volume 1",
            order_index=0,
        )
    )
    repo.save_sync(
        StoryNode(
            id="act-1",
            novel_id="novel-1",
            parent_id="volume-1",
            node_type=NodeType.ACT,
            number=1,
            title="Act 1",
            order_index=0,
        )
    )

    deleted = asyncio.run(repo.delete("volume-1"))

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT id, parent_id, node_type FROM story_nodes ORDER BY id"
    ).fetchall()
    conn.close()

    assert deleted is True
    assert rows == []


def test_save_batch_updates_parent_without_deleting_existing_children(repo_db):
    """DB-001: a batch upsert must preserve the parent's foreign-key identity."""
    repo, db_path = repo_db
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("novel-1", "Novel 1", "novel-1", 10),
    )
    conn.commit()
    conn.close()

    repo.save_sync(
        StoryNode(
            id="volume-1",
            novel_id="novel-1",
            node_type=NodeType.VOLUME,
            number=1,
            title="Original volume",
            order_index=0,
        )
    )
    repo.save_sync(
        StoryNode(
            id="act-1",
            novel_id="novel-1",
            parent_id="volume-1",
            node_type=NodeType.ACT,
            number=1,
            title="Act 1",
            order_index=0,
        )
    )

    asyncio.run(
        repo.save_batch(
            [
                StoryNode(
                    id="volume-1",
                    novel_id="novel-1",
                    node_type=NodeType.VOLUME,
                    number=1,
                    title="Updated volume",
                    order_index=0,
                )
            ]
        )
    )

    conn = sqlite3.connect(db_path)
    child = conn.execute(
        "SELECT id, parent_id FROM story_nodes WHERE id = 'act-1'"
    ).fetchone()
    parent = conn.execute(
        "SELECT title FROM story_nodes WHERE id = 'volume-1'"
    ).fetchone()
    conn.close()

    assert child == ("act-1", "volume-1")
    assert parent == ("Updated volume",)


def test_new_story_nodes_reject_duplicate_parent_type_and_number(repo_db):
    """DB-001b: new planning rows need a natural-key collision guard."""
    repo, db_path = repo_db
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("novel-1", "Novel 1", "novel-1", 10),
    )
    conn.commit()
    conn.close()

    repo.save_sync(
        StoryNode(
            id="volume-1",
            novel_id="novel-1",
            node_type=NodeType.VOLUME,
            number=1,
            title="Volume 1",
            order_index=0,
        )
    )
    repo.save_sync(
        StoryNode(
            id="act-1",
            novel_id="novel-1",
            parent_id="volume-1",
            node_type=NodeType.ACT,
            number=1,
            title="Act 1",
            order_index=0,
        )
    )

    with pytest.raises(sqlite3.IntegrityError, match="natural key"):
        repo.save_sync(
            StoryNode(
                id="act-duplicate",
                novel_id="novel-1",
                parent_id="volume-1",
                node_type=NodeType.ACT,
                number=1,
                title="Duplicate act",
                order_index=1,
            )
        )


def test_apply_merge_plan_updates_persisted_suggested_chapter_capacity(repo_db):
    """DB-STRUCTURE-001: safe macro merges must not discard volume capacity."""
    repo, db_path = repo_db
    conn = sqlite3.connect(db_path)
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("novel-1", "Novel 1", "novel-1", 12),
    )
    conn.commit()
    conn.close()
    repo.save_sync(
        StoryNode(
            id="volume-1",
            novel_id="novel-1",
            node_type=NodeType.VOLUME,
            number=1,
            title="Original volume",
            order_index=0,
            suggested_chapter_count=6,
        )
    )

    asyncio.run(
        repo.apply_merge_plan(
            creates=[],
            updates=[
                {
                    "id": "volume-1",
                    "title": "Updated volume",
                    "description": "Updated contract",
                    "order_index": 0,
                    "suggested_chapter_count": 12,
                }
            ],
            deletes=[],
        )
    )

    conn = sqlite3.connect(db_path)
    row = conn.execute(
        "SELECT title, suggested_chapter_count FROM story_nodes WHERE id = 'volume-1'"
    ).fetchone()
    conn.close()

    assert row == ("Updated volume", 12)
