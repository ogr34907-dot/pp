"""Current five-level contracts can be shadowed as one manifest."""

from domain.structure.outline_contract import (
    OutlineLevel,
    OutlinePayload,
    OutlineSource,
)
from domain.structure.story_node import NodeType, StoryNode
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.outline_contract_repository import (
    OutlineContractRepository,
)
from infrastructure.persistence.database.story_node_repository import StoryNodeRepository


def _payload(level: OutlineLevel) -> OutlinePayload:
    return OutlinePayload(
        title=f"{level.value} plan",
        narrative_text="人物在连续冲突中付出代价并进入下一阶段。",
        creative_goal="完成本级阶段目标",
        entry_state="承接上一级和前一阶段",
        exit_state="形成下一阶段必须继承的变化",
        conflicts=["主要冲突持续升级"],
        state_changes={"protagonist": [{"change": "承担更大责任"}]},
        handoff_conditions=["下一阶段继承未完成任务"],
        chapter_start=1,
        chapter_end=10,
    )


def test_complete_synced_five_level_chain_backfills_as_one_shadow_manifest(tmp_path):
    database = DatabaseConnection(str(tmp_path / "five-level-backfill.db"))
    conn = database.get_connection()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("novel-1", "Five Levels", "five-levels", 10),
    )
    conn.commit()
    contracts = OutlineContractRepository(database)
    story_nodes = StoryNodeRepository(database)

    root = contracts.ensure_root("novel-1")
    root_draft = contracts.save_draft(
        root.id,
        _payload(OutlineLevel.OUTLINE),
        source=OutlineSource.AUTHOR,
    )
    parent_contract = contracts.publish_and_sync(
        root.id,
        expected_revision=root_draft.draft.revision,
    )
    parent_node_id = None

    for index, level in enumerate(
        (OutlineLevel.PART, OutlineLevel.VOLUME, OutlineLevel.ACT, OutlineLevel.CHAPTER),
        start=1,
    ):
        node = StoryNode(
            id=f"node-{level.value}",
            novel_id="novel-1",
            parent_id=parent_node_id,
            node_type=NodeType(level.value),
            number=1,
            title=f"{level.value} node",
            order_index=index,
            chapter_start=1,
            chapter_end=10,
            chapter_count=10,
        )
        story_nodes.save_sync(node)
        child = contracts.create_contract(
            novel_id="novel-1",
            level=level,
            parent_contract_id=parent_contract.id,
            story_node_id=node.id,
        )
        child_draft = contracts.save_draft(
            child.id,
            _payload(level),
            source=OutlineSource.AUTHOR,
        )
        parent_contract = contracts.publish_and_sync(
            child.id,
            expected_revision=child_draft.draft.revision,
        )
        parent_node_id = node.id

    result = contracts.backfill_initial_plan("novel-1")
    plan = result.plan
    by_level = {item.level: item for item in plan.items}

    assert tuple(by_level) == OutlineLevel.ordered()
    assert by_level[OutlineLevel.OUTLINE].parent_logical_node_id is None
    for parent_level, child_level in zip(
        OutlineLevel.ordered(), OutlineLevel.ordered()[1:]
    ):
        assert (
            by_level[child_level].parent_logical_node_id
            == by_level[parent_level].logical_node_id
        )
        assert by_level[parent_level].expansion_state == "expanded"
    assert by_level[OutlineLevel.CHAPTER].expansion_state == "unexpanded"
    assert result.head.authority_mode.value == "legacy"
