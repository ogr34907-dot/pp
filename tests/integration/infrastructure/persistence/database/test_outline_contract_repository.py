"""Persistence contract for published five-level plan revisions."""

import sqlite3

import pytest

from domain.structure.outline_contract import (
    OutlineLevel,
    OutlinePayload,
    OutlineSource,
    OutlineStatus,
)
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.outline_contract_repository import (
    OutlineContractRepository,
    OutlineGateError,
)


@pytest.fixture
def outline_repo(tmp_path):
    db = DatabaseConnection(str(tmp_path / "outline-contracts.db"))
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("novel-1", "Outline Novel", "outline-novel", 80),
    )
    conn.commit()
    return OutlineContractRepository(db)


def _payload(title: str) -> OutlinePayload:
    return OutlinePayload(
        title=title,
        creative_goal="让主角在代价下完成选择",
        required_events=["选择必须改变关系"],
        forbidden_events=["不得无代价化解冲突"],
    )


def test_root_keeps_a_draft_and_a_separate_synced_published_revision(outline_repo):
    root = outline_repo.ensure_root("novel-1")
    assert root.level == OutlineLevel.OUTLINE
    assert root.active is None

    drafted = outline_repo.save_draft(
        root.id, _payload("第一版总纲"), source=OutlineSource.AUTHOR
    )
    assert drafted.draft is not None
    assert drafted.draft.status == OutlineStatus.DRAFT
    assert drafted.active is None

    published = outline_repo.publish_and_sync(
        root.id, expected_revision=drafted.draft.revision, idempotency_key="publish-root-v1"
    )
    assert published.active is not None
    assert published.active.status == OutlineStatus.SYNCED
    assert published.active.payload.title == "第一版总纲"
    assert published.draft is None

    next_draft = outline_repo.save_draft(
        root.id, _payload("第二版总纲"), source=OutlineSource.AUTHOR
    )
    assert next_draft.active is not None
    assert next_draft.active.payload.title == "第一版总纲"
    assert next_draft.draft is not None
    assert next_draft.draft.payload.title == "第二版总纲"
    assert [version.revision for version in outline_repo.list_versions(root.id)] == [1, 2]


def test_child_generation_is_blocked_until_its_parent_has_synced(outline_repo):
    root = outline_repo.ensure_root("novel-1")
    with pytest.raises(OutlineGateError, match="outline:.*synced"):
        outline_repo.create_contract(
            novel_id="novel-1", level=OutlineLevel.PART, parent_contract_id=root.id
        )

    draft = outline_repo.save_draft(root.id, _payload("总纲"), source=OutlineSource.AUTHOR)
    outline_repo.publish_and_sync(root.id, expected_revision=draft.draft.revision)
    part = outline_repo.create_contract(
        novel_id="novel-1", level=OutlineLevel.PART, parent_contract_id=root.id
    )

    assert part.level == OutlineLevel.PART
    assert part.parent_contract_id == root.id


def test_publishing_a_parent_marks_non_locked_children_stale_and_locked_children_conflicting(outline_repo):
    root = outline_repo.ensure_root("novel-1")
    root_draft = outline_repo.save_draft(root.id, _payload("总纲 v1"), source=OutlineSource.AUTHOR)
    outline_repo.publish_and_sync(root.id, expected_revision=root_draft.draft.revision)

    disposable_part = outline_repo.create_contract(
        novel_id="novel-1", level=OutlineLevel.PART, parent_contract_id=root.id
    )
    locked_part = outline_repo.create_contract(
        novel_id="novel-1", level=OutlineLevel.PART, parent_contract_id=root.id
    )
    for contract, title, locked in (
        (disposable_part, "AI 子纲", False),
        (locked_part, "作者子纲", True),
    ):
        draft = outline_repo.save_draft(contract.id, _payload(title), source=OutlineSource.AUTHOR if locked else OutlineSource.AI)
        outline_repo.publish_and_sync(
            contract.id,
            expected_revision=draft.draft.revision,
            author_locked=locked,
        )

    root_v2 = outline_repo.save_draft(root.id, _payload("总纲 v2"), source=OutlineSource.AUTHOR)
    outline_repo.publish_and_sync(root.id, expected_revision=root_v2.draft.revision)

    assert outline_repo.get_slot(disposable_part.id).active.status == OutlineStatus.STALE
    assert outline_repo.get_slot(locked_part.id).active.status == OutlineStatus.CONFLICT
