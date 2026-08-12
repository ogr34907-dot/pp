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
