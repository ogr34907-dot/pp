from unittest.mock import AsyncMock

import pytest

from application.engine.services.hierarchical_narrative_alignment_gate import (
    HierarchicalNarrativeAlignmentGate,
    OneShotOverride,
)
from domain.structure.story_node import NodeType, StoryNode


def node(node_id, node_type, number, title, parent_id=None, **kwargs):
    return StoryNode(
        id=node_id,
        novel_id="novel-1",
        node_type=node_type,
        number=number,
        title=title,
        order_index=number,
        parent_id=parent_id,
        **kwargs,
    )


@pytest.fixture
def chain():
    return [
        node("part-1", NodeType.PART, 1, "王朝崛起", metadata={"contract_digest": "part-d"}),
        node(
            "vol-1", NodeType.VOLUME, 1, "粮船过瓦埠", "part-1",
            chapter_start=1, chapter_end=10, themes=["争霸"],
            metadata={"contract_digest": "volume-d", "narrative_goal": "夺取粮道"},
        ),
        node(
            "act-1", NodeType.ACT, 1, "狼烟裂塔", "vol-1",
            chapter_start=1, chapter_end=10, key_events=["夺粮"],
            narrative_arc="从试探到夺取粮道", conflicts=["守军阻击"],
            metadata={"contract_digest": "act-d", "narrative_goal": "突破守军"},
        ),
        node(
            "chapter-1", NodeType.CHAPTER, 1, "夜渡", "act-1",
            outline="主角率队夜渡并承担损失",
            metadata={"contract_digest": "chapter-d", "key_characters": ["hero"]},
        ),
    ]


def candidate():
    return {
        "contract_digests": {"chapter": "chapter-d", "act": "act-d", "volume": "volume-d"},
        "serves_volume_commitments": ["夺取粮道"],
        "serves_act_commitments": ["突破守军", "夺粮"],
        "character_agency": [{"character": "hero", "goal": "夺粮", "choice": "夜渡", "cost": "失去船只"}],
        "references": ["hero"],
    }


def test_valid_chain_passes_and_digest_is_stable(chain):
    gate = HierarchicalNarrativeAlignmentGate()
    first = gate.build_snapshot("chapter-1", chain)
    second = gate.build_snapshot("chapter-1", chain)

    assert first.digest == second.digest
    assert first.to_dict()["ancestry"]["volume"]["id"] == "vol-1"
    report = gate.check(first, candidate())
    assert report.decision == "pass"


def test_act_plan_contradicting_volume_is_blocked(chain):
    gate = HierarchicalNarrativeAlignmentGate()
    snapshot = gate.build_snapshot("chapter-1", chain)
    plan = candidate() | {"serves_volume_commitments": ["调查古墓"]}

    report = gate.check(snapshot, plan)

    assert report.decision == "block"
    assert any(v.scope == "volume" for v in report.violations)


def test_missing_ancestry_blocks_before_llm_callback(chain):
    callback = AsyncMock()
    gate = HierarchicalNarrativeAlignmentGate(llm_evaluator=callback)
    snapshot = gate.build_snapshot("chapter-1", [chain[-1]])

    report = gate.check(snapshot, candidate())

    assert report.decision == "block"
    callback.assert_not_awaited()


def test_missing_agency_is_blocking(chain):
    gate = HierarchicalNarrativeAlignmentGate()
    snapshot = gate.build_snapshot("chapter-1", chain)
    plan = candidate() | {"character_agency": [{"character": "hero", "goal": "", "choice": "", "cost": ""}]}

    report = gate.check(snapshot, plan)

    assert report.decision == "block"
    assert any(v.scope == "character" for v in report.violations)


@pytest.mark.asyncio
async def test_llm_review_low_confidence_and_vector_degraded_never_pass(chain):
    snapshot = HierarchicalNarrativeAlignmentGate().build_snapshot(
        "chapter-1", chain, vector_evidence={"degraded": True, "error": "offline"}
    )
    callback = AsyncMock(return_value={"decision": "review", "confidence": 0.99})
    gate = HierarchicalNarrativeAlignmentGate(llm_evaluator=callback)

    report = await gate.evaluate(snapshot, candidate())

    assert report.decision == "review"
    assert report.evidence_degraded is True


def test_override_requires_reason_and_exact_digest(chain):
    gate = HierarchicalNarrativeAlignmentGate()
    snapshot = gate.build_snapshot("chapter-1", chain)
    blocked = gate.check(snapshot, candidate() | {"serves_volume_commitments": ["调查古墓"]})

    with pytest.raises(ValueError):
        gate.apply_override(blocked, OneShotOverride(candidate_digest="wrong", reason="x"))
    with pytest.raises(ValueError):
        gate.apply_override(blocked, OneShotOverride(candidate_digest=blocked.candidate_digest, reason=""))

    overridden = gate.apply_override(
        blocked,
        OneShotOverride(candidate_digest=blocked.candidate_digest, reason="作者确认本章越过卷目标"),
    )
    assert overridden.decision == "pass"
    assert overridden.overridden is True
