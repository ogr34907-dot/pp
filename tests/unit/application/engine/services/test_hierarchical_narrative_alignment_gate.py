from unittest.mock import AsyncMock
import json
import math

import pytest

from application.engine.services.hierarchical_narrative_alignment_gate import (
    HierarchicalNarrativeAlignmentGate,
    OneShotOverride,
)
from application.engine.services.narrative_gate_guard import evaluate_chapter_candidate
from domain.ai.services.vector_store import VectorStore
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


def test_repair_plan_is_derived_from_violations(chain):
    snapshot = HierarchicalNarrativeAlignmentGate().build_snapshot("chapter-1", chain)
    report = HierarchicalNarrativeAlignmentGate().check(
        snapshot, candidate() | {"serves_volume_commitments": ["无关目标"]}
    )

    assert report.repair_plan
    assert any("具体服务动作" in repair for repair in report.repair_plan)


@pytest.mark.asyncio
@pytest.mark.parametrize("result", [{"decision": "wat"}, {"decision": "pass", "confidence": "bad"}, {"decision": "pass", "confidence": math.nan}])
async def test_malformed_or_unknown_llm_result_requires_review(chain, result):
    snapshot = HierarchicalNarrativeAlignmentGate().build_snapshot("chapter-1", chain)
    gate = HierarchicalNarrativeAlignmentGate(llm_evaluator=AsyncMock(return_value=result))

    report = await gate.evaluate(snapshot, candidate())

    assert report.decision == "review"


def test_sync_vector_retriever_is_invoked_when_evidence_absent(chain):
    calls = []

    def retriever(*args):
        calls.append(args)
        return [{"id": "memory-1"}]

    snapshot = HierarchicalNarrativeAlignmentGate(vector_retriever=retriever).build_snapshot("chapter-1", chain)

    assert calls
    assert snapshot.vector_evidence == ({"id": "memory-1"},)
    assert snapshot.evidence_degraded is False


@pytest.mark.asyncio
async def test_async_vector_retriever_is_awaited_during_evaluation(chain):
    calls = []

    async def retriever(*args):
        calls.append(args)
        return [{"id": "memory-async"}]

    gate = HierarchicalNarrativeAlignmentGate(vector_retriever=retriever)
    snapshot = gate.build_snapshot("chapter-1", chain)

    report = await gate.evaluate(snapshot, candidate())

    assert report.decision == "pass"
    assert calls


@pytest.mark.asyncio
async def test_raw_vector_store_is_not_treated_as_chapter_evidence_retriever(chain):
    """A storage backend needs embeddings and collection metadata, not novel/chapter ids."""

    class RawVectorStore(VectorStore):
        def __init__(self):
            self.search_calls = []

        async def insert(self, collection, id, vector, payload):
            return None

        async def search(self, collection, query_vector, limit):
            self.search_calls.append((collection, query_vector, limit))
            return []

        async def delete(self, collection, id):
            return None

        async def create_collection(self, collection, dimension):
            return None

        async def delete_collection(self, collection):
            return None

        async def list_collections(self):
            return []

    store = RawVectorStore()
    gate = HierarchicalNarrativeAlignmentGate(vector_retriever=store)

    snapshot = gate.build_snapshot("chapter-1", chain)
    report = await gate.evaluate(snapshot, candidate())

    assert report.decision == "pass"
    assert report.evidence_degraded is False
    assert store.search_calls == []


def test_unknown_candidate_reference_blocks_even_without_known_references(chain):
    chain[-1].metadata = {"contract_digest": "chapter-d"}
    snapshot = HierarchicalNarrativeAlignmentGate().build_snapshot("chapter-1", chain)

    report = HierarchicalNarrativeAlignmentGate().check(snapshot, candidate() | {"references": ["new-place"]})

    assert report.decision == "block"
    assert any(v.actual == "new-place" for v in report.violations)


def test_snapshot_and_report_serialization_accept_arbitrary_values(chain):
    class Odd:
        def __str__(self):
            return "odd-value"

    odd = Odd()
    cycle = []
    cycle.append(cycle)
    chain[-1].metadata = {"contract_digest": "chapter-d", "arbitrary": odd, "cycle": cycle}
    snapshot = HierarchicalNarrativeAlignmentGate().build_snapshot("chapter-1", chain, memory_state={"odd": odd})
    report = HierarchicalNarrativeAlignmentGate().check(snapshot, candidate())

    json.dumps(snapshot.to_dict(), ensure_ascii=False)
    json.dumps(report.to_dict(), ensure_ascii=False)


def test_snapshot_preserves_story_node_planning_and_chapter_fields(chain):
    chapter = chain[-1]
    chapter.planning_status = "confirmed"
    chapter.planning_source = "ai_act"
    chapter.status = "ready"
    chapter.chapter_count = 3
    chapter.pov_character_id = "hero"
    snapshot = HierarchicalNarrativeAlignmentGate().build_snapshot("chapter-1", chain)

    record = snapshot.ancestry["chapter"]
    assert record["planning_status"] == "confirmed"
    assert record["planning_source"] == "ai_act"
    assert record["status"] == "ready"
    assert record["chapter_count"] == 3
    assert record["pov_character_id"] == "hero"


def test_cross_novel_ancestor_is_blocking(chain):
    chain[2].novel_id = "other-novel"
    snapshot = HierarchicalNarrativeAlignmentGate().build_snapshot("chapter-1", chain)

    assert any(v.scope == "act" and v.actual == "other-novel" for v in snapshot.structural_violations)
    assert snapshot.ancestry["act"] is None


@pytest.mark.asyncio
async def test_guard_rebinds_legacy_stale_contract_map_to_canonical_ancestry(chain):
    """NARRATIVE-ALIGNMENT-001: old planned chapters must not self-block on stale digest copies."""
    chain[2].metadata = {"narrative_goal": "突破守军"}
    chain[-1].metadata = {
        "contract_digest": "persisted-chapter-contract",
        "contract_digests": {
            "chapter": "pre-normalization-contract",
            "act": "missing-legacy-act-contract",
            "volume": "wrong-volume-contract",
        },
        "key_characters": ["hero"],
    }
    repository = type(
        "StoryNodeRepository",
        (),
        {"get_by_novel": AsyncMock(return_value=chain)},
    )()
    stale_candidate = candidate() | {
        "contract_digests": {
            "chapter": "pre-normalization-contract",
            "act": "missing-legacy-act-contract",
            "volume": "wrong-volume-contract",
        }
    }

    report = await evaluate_chapter_candidate(
        HierarchicalNarrativeAlignmentGate(),
        story_node_repo=repository,
        novel_id="novel-1",
        chapter_number=1,
        chapter_node=chain[-1],
        candidate=stale_candidate,
    )

    assert report is not None
    assert report.decision == "pass"
