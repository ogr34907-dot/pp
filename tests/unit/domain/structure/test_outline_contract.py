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


def test_payload_normalizes_malformed_llm_list_and_mapping_fields():
    payload = OutlinePayload.from_dict(
        {
            "title": ["总纲", "误返回数组"],
            "required_events": "主角离开故乡",
            "forbidden_events": {"first": "提前解决冲突"},
            "foreshadowing": ["雨夜的信"],
            "state_changes": "人物关系改变",
            "chapter_start": "3",
            "chapter_end": "not-a-number",
        }
    )

    assert payload.title == "总纲\n误返回数组"
    assert payload.required_events == ["主角离开故乡"]
    assert payload.forbidden_events == ["提前解决冲突"]
    assert payload.foreshadowing == {"items": ["雨夜的信"]}
    assert payload.state_changes == {}
    assert payload.chapter_start == 3
    assert payload.chapter_end is None


def test_later_sibling_payload_requires_a_complete_narrative_handoff():
    payload = OutlinePayload(
        creative_goal="推进下一阶段",
        entry_state="承接上一阶段结局",
        exit_state="留下新的危机",
        conflicts=["新的冲突"],
        state_changes={"protagonist": [{"change": "承担后果"}]},
        handoff_conditions=["下一阶段必须回应危机"],
    )

    assert payload.sibling_continuity_blockers() == ()

    payload.handoff_conditions = []
    assert payload.sibling_continuity_blockers() == ("handoff_conditions:missing",)
