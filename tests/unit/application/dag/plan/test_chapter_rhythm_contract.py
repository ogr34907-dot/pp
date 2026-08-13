"""Chapter rhythm is a single plan-level contract, not a Beat mirror."""

from application.engine.dag.models import get_default_dag
from application.engine.dag.nodes.planning_chapter_outline_node import (
    PlanningOutlinePartitionNode,
)
from application.engine.dag.plan.outline_beat_planner import (
    build_chapter_execution_plan_sync,
)
from application.engine.services.beat_projection import beats_from_execution_plan


def _outline_payload():
    return {
        "creative_goal": "夺回密钥并迫使敌人暴露代价",
        "rhythm": {
            "chapter_function": "climax",
            "intensity_curve": ["medium", "high", "peak"],
            "chapter_goal": "夺回密钥",
            "decisive_choice": "烧毁最后的退路",
            "cost_or_risk": "失去身份掩护",
            "chapter_delta": "密钥归还但身份暴露",
            "turn_or_payoff": "前置钟声伏笔兑现",
            "ending_hook": "追兵封锁城门",
        },
    }


def test_explicit_outline_rhythm_is_normalized_into_one_execution_plan():
    plan = build_chapter_execution_plan_sync(
        "主角在城门前夺回密钥。",
        novel_id="novel-1",
        chapter_number=12,
        outline_payload=_outline_payload(),
    )

    assert plan.rhythm is not None
    assert plan.rhythm.chapter_function == "climax"
    assert plan.rhythm.intensity_curve == ["medium", "high", "peak"]
    assert plan.rhythm.cost_or_risk == "失去身份掩护"
    assert plan.model_dump(mode="json")["rhythm"]["ending_hook"] == "追兵封锁城门"

    beats = beats_from_execution_plan(
        plan,
        outline="主角在城门前夺回密钥。",
        target_chapter_words=2500,
        infer_focus=lambda _outline: "setup",
        build_expansion_hints=lambda _focus, _target: [],
    )
    assert beats
    assert all(not hasattr(beat, "rhythm") for beat in beats)
    assert all(not hasattr(beat, "chapter_rhythm") for beat in beats)


def test_outline_without_explicit_rhythm_keeps_legacy_plan_compatible():
    plan = build_chapter_execution_plan_sync(
        "主角在雨夜赴约。",
        novel_id="novel-1",
        chapter_number=1,
        outline_payload={
            "creative_goal": "完成一次交接",
            "ending_hook": "远处传来脚步",
        },
    )

    assert plan.rhythm is None


def test_default_dag_carries_plan_rhythm_as_a_separate_edge():
    dag = get_default_dag()
    assert any(
        edge.source == "exec_beat"
        and edge.source_port == "chapter_rhythm"
        and edge.target == "exec_writer"
        and edge.target_port == "chapter_rhythm"
        for edge in dag.edges
    )


def test_planning_node_projects_published_payload_rhythm_without_an_extra_planner():
    import asyncio

    result = asyncio.run(
        PlanningOutlinePartitionNode().execute(
            {
                "outline": "主角在城门前夺回密钥。",
                "outline_payload": _outline_payload(),
                "use_llm": False,
            },
            {"novel_id": "novel-1", "chapter_number": 12},
        )
    )

    assert result.outputs["chapter_plan_json"]["rhythm"]["chapter_function"] == "climax"
    assert result.outputs["chapter_plan_json"]["rhythm"]["cost_or_risk"] == "失去身份掩护"


def test_plan_rhythm_serialization_round_trips_without_beats():
    plan = build_chapter_execution_plan_sync(
        "主角完成交接。",
        outline_payload={"rhythm": {"chapter_function": "aftermath"}},
    )

    restored = type(plan).model_validate(plan.model_dump(mode="json"))
    assert restored.rhythm is not None
    assert restored.rhythm.chapter_function == "aftermath"
