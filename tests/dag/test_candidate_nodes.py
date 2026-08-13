"""Candidate-only DAG analysis must never write Canonical facts."""

import pytest

from application.engine.dag.nodes.validation_nodes import (
    AntiAINode,
    ForeshadowCheckNode,
    KGInferNode,
    NarrativeNode,
    StyleNode,
    TensionNode,
)
from application.engine.dag.nodes.gateway_nodes import ReviewNode
from application.engine.dag.nodes.context_nodes import (
    BlueprintNode,
    DebtNode,
    ForeshadowNode,
    MemoryNode,
    VoiceNode,
)


def _context():
    return {
        "novel_id": "novel-1",
        "chapter_number": 1,
        "candidate_mode": True,
        "candidate_id": "candidate-1",
        # This is the persisted prose revision, not the bounded DAG retry
        # counter. Candidate proposals must be isolated by this value.
        "content_revision": 2,
        "candidate_revision": 0,
        "outline_chain": {
            "chapter": {
                "payload": {"foreshadowing": {"advance": ["旧承诺"]}},
            },
        },
        "shared_state": {"content": "主角兑现旧承诺，转身面对代价。"},
    }


@pytest.mark.asyncio
async def test_candidate_context_nodes_are_read_only_and_preserve_published_outline():
    context = _context()
    context["outline_text"] = "已发布五级大纲"

    outputs = {}
    for node in (BlueprintNode(), ForeshadowNode(), VoiceNode(), MemoryNode(), DebtNode()):
        result = await node.execute({}, {**context, "shared_state": {**context["shared_state"], **outputs}})
        assert result.status.value in {"success", "warning"}
        outputs.update(result.outputs)

    assert outputs["world_rules"] == "已发布五级大纲"
    assert "旧承诺" in outputs["foreshadowing_block"]
    assert outputs["fact_lock"] == "已发布五级大纲"


@pytest.mark.asyncio
async def test_candidate_analysis_nodes_emit_scoped_proposals_without_canonical_writes():
    context = _context()
    narrative = await NarrativeNode().execute({"content": context["shared_state"]["content"]}, context)
    context["shared_state"].update(narrative.outputs)
    foreshadow = await ForeshadowCheckNode().execute({"content": context["shared_state"]["content"]}, context)
    context["shared_state"].update(foreshadow.outputs)
    knowledge = await KGInferNode().execute({}, context)

    proposals = knowledge.outputs["candidate_proposals"]
    assert proposals["scope"] == {"candidate_id": "candidate-1", "content_revision": 2}
    assert proposals["narrative"]["summary"] == "主角兑现旧承诺，转身面对代价。"
    assert proposals["foreshadowing"]["matched"] == ["旧承诺"]
    assert proposals["knowledge_graph"]["status"] == "candidate_only"


@pytest.mark.asyncio
async def test_candidate_machine_audit_detects_anti_ai_hits_and_retains_a_neutral_tension_baseline():
    context = _context()
    style = await StyleNode().execute({"content": context["shared_state"]["content"]}, context)
    anti_ai = await AntiAINode().execute({"content": "他嘴角上扬，眼中闪过一丝笑意。"}, context)
    tension = await TensionNode().execute({"content": context["shared_state"]["content"]}, context)

    assert style.outputs == {"drift_score": 0.0, "drift_alert": False}
    assert anti_ai.outputs["severity_score"] >= 20
    assert anti_ai.outputs["hits"]
    assert tension.outputs["composite"] >= 30


@pytest.mark.asyncio
async def test_candidate_review_gateway_uses_the_injected_structured_reviewer():
    class _Reviewer:
        def __init__(self):
            self.kwargs = {}

        async def review(self, **kwargs):
            self.kwargs = kwargs
            return type(
                "Review",
                (),
                {
                    "status": "approved",
                    "score": 88,
                    "summary": "冲突推进和人物选择都有正文证据。",
                    "suggestions": ["保留结尾的追兵钩子。"],
                    "issues": [],
                    "event_coverage": [
                        {
                            "event": "主角交出证物",
                            "status": "completed",
                            "evidence": "把证物塞进了接头人的掌心",
                        }
                    ],
                    "rhythm_assessment": {
                        "status": "complete",
                        "chapter_function": "transition",
                        "evidence": {},
                    },
                },
            )()

    reviewer = _Reviewer()
    result = await ReviewNode().execute(
        {"content": "他把证物塞进了接头人的掌心，转身撞进雨幕。", "run_mode": "continuous"},
        {
            "candidate_mode": True,
            "run_mode": "continuous",
            "chapter_number": 7,
            "chapter_title": "雨夜交接",
            "candidate_semantic_reviewer": reviewer,
            "outline_chain": {
                "chapter": {
                    "payload": {
                        "creative_goal": "主角带着代价完成交接",
                        "required_events": ["主角交出证物"],
                        "rhythm": {
                            "chapter_function": "transition",
                            "chapter_delta": "双方暂时合作",
                            "ending_hook": "追兵逼近",
                        },
                    }
                }
            },
        },
    )

    assert result.outputs["approved"] is True
    assert result.outputs["semantic_review"]["status"] == "approved"
    assert result.outputs["semantic_review"]["machine_review_incomplete"] is False
    assert reviewer.kwargs["required_events"] == ["主角交出证物"]
    assert "主角带着代价完成交接" in reviewer.kwargs["chapter_outline"]
    assert '"chapter_function": "transition"' in reviewer.kwargs["chapter_outline"]


@pytest.mark.asyncio
async def test_candidate_review_gateway_fails_closed_without_a_structured_reviewer():
    result = await ReviewNode().execute(
        {"content": "主角在雨夜作出选择。", "run_mode": "continuous"},
        {"candidate_mode": True, "run_mode": "continuous"},
    )

    assert result.outputs["approved"] is False
    assert result.outputs["review_required"] is True
    assert result.outputs["semantic_review"]["status"] == "unavailable"
    assert result.outputs["semantic_review"]["machine_review_failed"] is True


@pytest.mark.asyncio
async def test_candidate_review_gateway_blocks_explicit_rhythm_without_machine_evidence():
    class _Reviewer:
        async def review(self, **_kwargs):
            return type(
                "Review",
                (),
                {
                    "status": "approved",
                    "score": 90,
                    "summary": "模型没有返回节奏证据。",
                    "suggestions": ["补回节奏证据。"],
                    "issues": [
                        type(
                            "Issue",
                            (),
                            {
                                "severity": "critical",
                                "location": "章级节奏合同",
                                "description": "显式章级节奏合同没有完整的机器语义证据。",
                                "suggestion": "暂停自动推进，补审或转作者审核。",
                            },
                        )()
                    ],
                    "event_coverage": [],
                    "rhythm_assessment": {
                        "status": "incomplete",
                        "chapter_function": "transition",
                        "evidence": {},
                    },
                },
            )()

    result = await ReviewNode().execute(
        {"content": "二人交接后暂时合作。", "run_mode": "continuous"},
        {
            "candidate_mode": True,
            "run_mode": "continuous",
            "candidate_semantic_reviewer": _Reviewer(),
            "outline_chain": {
                "chapter": {
                    "payload": {
                        "rhythm": {
                            "chapter_function": "transition",
                            "chapter_delta": "双方暂时合作",
                        }
                    }
                }
            },
        },
    )

    assert result.outputs["approved"] is False
    assert result.outputs["semantic_review"]["status"] == "draft"
    assert result.outputs["semantic_review"]["machine_review_incomplete"] is True
    rhythm_issues = [
        issue
        for issue in result.outputs["semantic_review"]["issues"]
        if issue["location"] == "章级节奏合同"
    ]
    assert len(rhythm_issues) == 1
    assert rhythm_issues[0]["severity"] == "critical"
