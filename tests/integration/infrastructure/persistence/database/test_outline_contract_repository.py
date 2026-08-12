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
from infrastructure.persistence.database.story_node_repository import StoryNodeRepository
from domain.structure.story_node import NodeType, StoryNode


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


def _continuity_payload(title: str, *, chapter_start: int, chapter_end: int) -> OutlinePayload:
    return OutlinePayload(
        title=title,
        narrative_text="故事沿时间向前推进。",
        creative_goal="让主角在代价下完成本阶段目标",
        entry_state="承接前一阶段留下的处境",
        exit_state="将变化交给下一阶段",
        required_events=["主角做出不可逆选择"],
        conflicts=["外部压力迫使主角选择"],
        state_changes={"protagonist": [{"change": "从逃避转为承担"}]},
        handoff_conditions=["下一阶段必须回应本阶段留下的危机"],
        chapter_start=chapter_start,
        chapter_end=chapter_end,
    )


def _part_node(node_id: str, number: int) -> StoryNode:
    return StoryNode(
        id=node_id,
        novel_id="novel-1",
        node_type=NodeType.PART,
        number=number,
        title=f"第{number}部",
        order_index=number,
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


def test_later_sibling_must_continue_the_previous_synced_plan(outline_repo):
    db = outline_repo._db
    story_nodes = StoryNodeRepository(db)
    first_node = _part_node("part-1", 1)
    second_node = _part_node("part-2", 2)
    story_nodes.save_sync(first_node)
    story_nodes.save_sync(second_node)

    root = outline_repo.ensure_root("novel-1")
    root_draft = outline_repo.save_draft(root.id, _payload("总纲"), source=OutlineSource.AUTHOR)
    outline_repo.publish_and_sync(root.id, expected_revision=root_draft.draft.revision)

    first = outline_repo.create_contract(
        novel_id="novel-1",
        level=OutlineLevel.PART,
        parent_contract_id=root.id,
        story_node_id=first_node.id,
    )
    first_draft = outline_repo.save_draft(
        first.id, _continuity_payload("第一部", chapter_start=1, chapter_end=3)
    )
    outline_repo.publish_and_sync(first.id, expected_revision=first_draft.draft.revision)

    second = outline_repo.create_contract(
        novel_id="novel-1",
        level=OutlineLevel.PART,
        parent_contract_id=root.id,
        story_node_id=second_node.id,
    )
    broken = outline_repo.save_draft(
        second.id, _continuity_payload("第二部", chapter_start=5, chapter_end=7)
    )

    with pytest.raises(OutlineGateError, match="sibling continuity"):
        outline_repo.publish_and_sync(second.id, expected_revision=broken.draft.revision)

    repaired = outline_repo.save_draft(
        second.id, _continuity_payload("第二部", chapter_start=4, chapter_end=7)
    )
    published = outline_repo.publish_and_sync(second.id, expected_revision=repaired.draft.revision)
    first_published = outline_repo.get_slot(first.id)
    assert published.active.previous_sibling_digest == first_published.active.published_digest
