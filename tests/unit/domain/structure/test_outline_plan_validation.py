from domain.structure.outline_contract import OutlineLevel, OutlinePayload
import pytest

from domain.structure.outline_plan import OutlinePlanItem
from domain.structure.outline_plan_validation import (
    compute_replan_impact_closure,
    merge_author_locked_payload,
    validate_prose_input_readiness,
    validate_sibling_cohort,
)


def _payload(*, start: int, end: int, entry: str, exit: str) -> OutlinePayload:
    return OutlinePayload(
        title="阶段",
        narrative_text="人物作出选择并承担代价，故事进入下一阶段。",
        creative_goal="推动父级阶段目标",
        entry_state=entry,
        exit_state=exit,
        conflicts=["主要冲突升级"],
        state_changes={"主角": [{"change": "承担后果"}]},
        handoff_conditions=["下一阶段承接未完成任务"],
        chapter_start=start,
        chapter_end=end,
    )


def test_sibling_cohort_accepts_contiguous_ranges_and_explicit_handoff():
    result = validate_sibling_cohort(
        level=OutlineLevel.PART,
        parent_payload=_payload(start=1, end=6, entry="起点", exit="终点"),
        siblings=(
            _payload(start=1, end=3, entry="起点", exit="中点"),
            _payload(start=4, end=6, entry="中点", exit="终点"),
        ),
    )

    assert result.blockers == ()


def test_sibling_cohort_rejects_range_gap_but_records_handoff_as_a_diagnostic():
    result = validate_sibling_cohort(
        level=OutlineLevel.VOLUME,
        parent_payload=_payload(start=1, end=6, entry="起点", exit="终点"),
        siblings=(
            _payload(start=1, end=2, entry="起点", exit="中点"),
            _payload(start=4, end=6, entry="另一条线", exit="终点"),
        ),
    )

    assert "chapter_range:gap:expected=3:actual=4" in result.blockers
    assert "handoff:1:entry_state_mismatch" in result.diagnostics


def test_sibling_cohort_requires_parent_boundary_coverage():
    result = validate_sibling_cohort(
        level=OutlineLevel.ACT,
        parent_payload=_payload(start=1, end=6, entry="起点", exit="终点"),
        siblings=(
            _payload(start=2, end=6, entry="错误起点", exit="终点"),
        ),
    )

    assert "chapter_range:first_start_mismatch:expected=1:actual=2" in result.blockers
    assert "handoff:first_entry_state_mismatch" in result.diagnostics


def test_sibling_cohort_treats_empty_narrative_fields_as_diagnostics():
    result = validate_sibling_cohort(
        level=OutlineLevel.PART,
        parent_payload=_payload(start=1, end=6, entry="起点", exit="终点"),
        siblings=(
            OutlinePayload(chapter_start=1, chapter_end=3),
            OutlinePayload(chapter_start=4, chapter_end=6),
        ),
    )

    assert result.blockers == ()
    assert "sibling:0:creative_goal:missing" in result.diagnostics
    assert "sibling:1:handoff_conditions:missing" in result.diagnostics
    assert "handoff:first_entry_state_mismatch" in result.diagnostics
    assert "handoff:last_exit_state_mismatch" in result.diagnostics


@pytest.mark.parametrize(
    ("siblings", "blocker"),
    (
        (
            (
                _payload(start=1, end=4, entry="起点", exit="中点"),
                _payload(start=4, end=6, entry="中点", exit="终点"),
            ),
            "chapter_range:overlap:expected=5:actual=4",
        ),
        (
            (_payload(start=7, end=6, entry="起点", exit="终点"),),
            "chapter_range:0:inverted",
        ),
    ),
)
def test_sibling_cohort_retains_overlap_and_inverted_range_blockers(siblings, blocker):
    result = validate_sibling_cohort(
        level=OutlineLevel.ACT,
        parent_payload=_payload(start=1, end=6, entry="起点", exit="终点"),
        siblings=siblings,
    )

    assert blocker in result.blockers


def test_replan_impact_closure_includes_downstream_and_later_siblings():
    items = (
        OutlinePlanItem(
            logical_node_id="root", version_id="vr", version_digest="dr",
            level=OutlineLevel.OUTLINE, sibling_index=0,
        ),
        OutlinePlanItem(
            logical_node_id="part-a", version_id="va", version_digest="da",
            level=OutlineLevel.PART, sibling_index=0, parent_logical_node_id="root",
            validated_parent_digest="dr",
        ),
        OutlinePlanItem(
            logical_node_id="part-b", version_id="vb", version_digest="db",
            level=OutlineLevel.PART, sibling_index=1, parent_logical_node_id="root",
            validated_parent_digest="dr", validated_previous_sibling_digest="da",
        ),
        OutlinePlanItem(
            logical_node_id="volume-a", version_id="vva", version_digest="dva",
            level=OutlineLevel.VOLUME, sibling_index=0, parent_logical_node_id="part-a",
            validated_parent_digest="da",
        ),
        OutlinePlanItem(
            logical_node_id="volume-b", version_id="vvb", version_digest="dvb",
            level=OutlineLevel.VOLUME, sibling_index=0, parent_logical_node_id="part-b",
            validated_parent_digest="db",
        ),
    )

    impact = compute_replan_impact_closure(items, "part-b")

    assert impact.invalidated_logical_node_ids == ("part-b", "volume-b")
    assert impact.ancestor_logical_node_ids == ("root",)


def test_author_locked_payload_preserves_author_text_and_ai_fills_only_unlocked_fields():
    authored = OutlinePayload.from_dict(
        {
            "title": "作者标题",
            "narrative_text": "作者逐字梗概",
            "extra": {"_field_provenance": {"title": {"source": "author", "locked": True}, "narrative_text": {"source": "author", "locked": True}}},
        }
    )
    ai = OutlinePayload(title="AI 标题", narrative_text="AI 改写", creative_goal="AI 补全目标")

    merged, conflicts = merge_author_locked_payload(authored, ai)

    assert merged.title == "作者标题"
    assert merged.narrative_text == "作者逐字梗概"
    assert merged.creative_goal == "AI 补全目标"
    assert conflicts == ("title", "narrative_text")


def test_prose_input_readiness_accepts_normalized_required_events_or_creative_goal():
    assert validate_prose_input_readiness(
        OutlinePayload(required_events=["  ", "  不可逆选择  ", ""])
    ) == ()
    assert validate_prose_input_readiness(
        OutlinePayload(required_events=["", "  "], creative_goal="  推进人物变化  ")
    ) == ()


def test_prose_input_readiness_requires_events_or_creative_goal():
    assert validate_prose_input_readiness(
        OutlinePayload(required_events=[" ", ""], creative_goal="  ")
    ) == ("prose_input:missing_required_events_or_creative_goal",)
