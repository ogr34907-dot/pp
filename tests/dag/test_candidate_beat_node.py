"""Candidate DAG planning reuses the published chapter outline."""

import pytest

from application.engine.dag.nodes.execution_nodes import BeatNode, WriterNode


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
                        "rhythm": {
                            "chapter_function": "transition",
                            "intensity_curve": ["low", "medium"],
                            "chapter_goal": "完成交接并改变双方关系",
                            "chapter_delta": "双方暂时合作",
                            "ending_hook": "追兵逼近",
                        },
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
    assert result.outputs["chapter_rhythm"] == {
        "chapter_function": "transition",
        "intensity_curve": ["low", "medium"],
        "chapter_goal": "完成交接并改变双方关系",
        "chapter_delta": "双方暂时合作",
        "ending_hook": "追兵逼近",
    }
    assert all("chapter_rhythm" not in beat for beat in beats)


@pytest.mark.asyncio
async def test_candidate_writer_passes_projected_beats_to_the_real_draft_generator():
    class _Generator:
        def __init__(self):
            self.kwargs = {}

        async def generate_candidate_draft(self, **kwargs):
            self.kwargs = kwargs
            return {"content": "主角赴约后交出证物。", "script": "剧本"}

    generator = _Generator()
    beats = [
        {
            "description": "主角赴约",
            "conflict": "追兵逼近",
            "handoff_to_next": "交出证物",
        }
    ]

    result = await WriterNode().execute(
        {
            "outline": "已发布章纲",
            "beats": beats,
            "chapter_rhythm": {
                "chapter_function": "transition",
                "chapter_delta": "双方暂时合作",
            },
        },
        {
            "candidate_mode": True,
            "candidate_draft_generator": generator,
            "novel_id": "novel-1",
            "chapter_number": 3,
            "chapter_title": "雨夜赴约",
            "outline_chain": {"chapter": {"payload": {}}},
            "outline_text": "已发布章纲",
        },
    )

    assert result.outputs["content"] == "主角赴约后交出证物。"
    assert generator.kwargs["beats"] == beats
    assert generator.kwargs["chapter_rhythm"] == {
        "chapter_function": "transition",
        "chapter_delta": "双方暂时合作",
    }
