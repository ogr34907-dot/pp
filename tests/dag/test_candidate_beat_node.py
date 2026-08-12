"""Candidate DAG planning reuses the published chapter outline."""

import pytest

from application.engine.dag.nodes.execution_nodes import BeatNode


@pytest.mark.asyncio
async def test_candidate_beat_node_projects_published_chapter_without_replanning(monkeypatch):
    async def unexpected_planning(*_args, **_kwargs):
        raise AssertionError("candidate execution must not invoke a second planning LLM call")

    monkeypatch.setattr(
        "application.engine.dag.plan.outline_beat_planner.build_chapter_execution_plan_async",
        unexpected_planning,
    )
    result = await BeatNode().execute(
        {"outline": "已发布章纲"},
        {
            "candidate_mode": True,
            "chapter_number": 3,
            "outline_chain": {
                "chapter": {
                    "payload": {
                        "creative_goal": "主角在雨夜交出证物",
                        "required_events": ["主角赴约", "交出证物"],
                        "forbidden_events": ["提前揭晓真相"],
                        "handoff_conditions": ["留下追兵逼近的危机"],
                    }
                }
            },
        },
    )

    beats = result.outputs["beats"]
    assert [beat["description"] for beat in beats] == ["主角赴约", "交出证物"]
    assert [beat["target_words"] for beat in beats] == [1250, 1250]
    assert all(beat["focus"] == "setup" for beat in beats)
    assert all(beat["conflict"] == "主角在雨夜交出证物" for beat in beats)
    assert all(beat["handoff_to_next"] == "留下追兵逼近的危机" for beat in beats)
    assert [beat["must_include"] for beat in beats] == [["主角赴约"], ["交出证物"]]
    assert all(beat["must_not_include"] == ["提前揭晓真相"] for beat in beats)
