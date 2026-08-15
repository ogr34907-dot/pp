from domain.structure.outline_contract import OutlineLevel, OutlinePayload
from domain.structure.outline_plan_validation import validate_sibling_cohort


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


def test_sibling_cohort_rejects_range_gap_and_handoff_mismatch():
    result = validate_sibling_cohort(
        level=OutlineLevel.VOLUME,
        parent_payload=_payload(start=1, end=6, entry="起点", exit="终点"),
        siblings=(
            _payload(start=1, end=2, entry="起点", exit="中点"),
            _payload(start=4, end=6, entry="另一条线", exit="终点"),
        ),
    )

    assert "chapter_range:gap:expected=3:actual=4" in result.blockers
    assert "handoff:1:entry_state_mismatch" in result.blockers


def test_sibling_cohort_requires_parent_boundary_coverage():
    result = validate_sibling_cohort(
        level=OutlineLevel.ACT,
        parent_payload=_payload(start=1, end=6, entry="起点", exit="终点"),
        siblings=(
            _payload(start=2, end=6, entry="错误起点", exit="终点"),
        ),
    )

    assert "chapter_range:first_start_mismatch:expected=1:actual=2" in result.blockers
    assert "handoff:first_entry_state_mismatch" in result.blockers

