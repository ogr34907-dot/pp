"""Five-level outline contracts are the gate before any prose generation."""

from domain.structure.outline_contract import (
    OutlineChain,
    OutlineContract,
    OutlineLevel,
    OutlinePayload,
    OutlineStatus,
)
from domain.structure.story_node import NodeType


def _contract(level: OutlineLevel, *, status: OutlineStatus = OutlineStatus.DRAFT):
    return OutlineContract(
        id=f"{level.value}-1",
        novel_id="novel-1",
        level=level,
        revision=1,
        payload=OutlinePayload(
            title=f"{level.value} title",
            creative_goal="推进主角在代价下做出选择",
            required_events=["主角必须做出不可逆选择"],
            forbidden_events=["不得无代价解决危机"],
        ),
        status=status,
    )


def test_outline_root_is_a_first_class_logical_structure_level():
    assert NodeType.OUTLINE.value == "outline"
    assert OutlineLevel.OUTLINE.child_level == OutlineLevel.PART


def test_payload_digest_tracks_contract_not_presentation_order():
    first = OutlinePayload(
        title="总纲",
        creative_goal="建立复仇主线",
        required_events=["失去家园", "查到真相"],
        forbidden_events=["主角提前原谅仇人"],
    )
    reordered = OutlinePayload(
        title="总纲",
        creative_goal="建立复仇主线",
        required_events=["失去家园", "查到真相"],
        forbidden_events=["主角提前原谅仇人"],
    )
    changed = OutlinePayload(
        title="总纲",
        creative_goal="建立复仇主线",
        required_events=["失去家园", "查到真相"],
        forbidden_events=["主角可提前原谅仇人"],
    )

    assert first.digest == reordered.digest
    assert first.digest != changed.digest


def test_only_published_and_synced_parent_can_open_the_next_level_gate():
    root = _contract(OutlineLevel.OUTLINE, status=OutlineStatus.PUBLISHED)
    assert root.can_generate_child is False

    root.status = OutlineStatus.SYNCED
    assert root.can_generate_child is True
    assert root.next_generatable_level == OutlineLevel.PART


def test_prose_requires_the_complete_synced_five_level_chain_without_conflict():
    chain = OutlineChain(
        [_contract(level, status=OutlineStatus.SYNCED) for level in OutlineLevel.ordered()]
    )
    assert chain.ready_for_prose is True
    assert chain.blockers == ()

    chain.contract_for(OutlineLevel.ACT).status = OutlineStatus.CONFLICT
    assert chain.ready_for_prose is False
    assert chain.blockers == ("act:conflict",)
