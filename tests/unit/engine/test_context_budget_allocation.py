from __future__ import annotations

from types import SimpleNamespace

import pytest

from application.engine.services.context_budget_allocator import (
    ContextBudgetAllocator,
    ContextBudgetExceededError,
)
from application.engine.services.context_budget_models import ContextSlot, PriorityTier
from application.engine.services.memory_engine import MemoryStateUnavailableError


def _slot(name: str, tier: PriorityTier, content: str, tokens: int, priority: int = 1):
    return ContextSlot(
        name=name,
        tier=tier,
        content=content,
        tokens=tokens,
        max_tokens=tokens,
        priority=priority,
    )


def _allocator_with_slots(monkeypatch, slots):
    allocator = ContextBudgetAllocator()
    monkeypatch.setattr(allocator, "_estimate_total_chapters", lambda _novel_id: 100)
    monkeypatch.setattr(allocator, "_collect_all_slots", lambda *_args, **_kwargs: slots)
    return allocator


def test_mixed_language_token_estimate_adds_language_costs_directly():
    allocator = ContextBudgetAllocator()

    assert allocator.estimate_tokens("中文ab") == 2


@pytest.mark.parametrize("target_chapters", [None, 0, True, 1.5, "invalid"])
def test_allocator_rejects_invalid_persisted_target_before_collecting_context(
    monkeypatch, target_chapters
):
    novel_repository = SimpleNamespace(
        get_by_id=lambda _novel_id: SimpleNamespace(target_chapters=target_chapters)
    )
    allocator = ContextBudgetAllocator(novel_repository=novel_repository)
    monkeypatch.setattr(
        allocator,
        "_collect_all_slots",
        lambda *_args, **_kwargs: pytest.fail("context slots must not be collected"),
    )

    with pytest.raises(ValueError, match="target_chapters"):
        allocator.allocate("novel-1", 1, "outline")


def test_allocator_uses_persisted_target_for_lifecycle_progress():
    allocator = ContextBudgetAllocator(
        novel_repository=SimpleNamespace(
            get_by_id=lambda _novel_id: SimpleNamespace(target_chapters=120)
        ),
        story_node_repository=SimpleNamespace(
            get_by_novel_sync=lambda _novel_id: [
                SimpleNamespace(
                    node_type=SimpleNamespace(value="part"),
                    chapter_end=30,
                )
            ]
        ),
    )

    assert allocator._estimate_total_chapters("novel-1") == 120


def test_t3_reservation_reduces_t2_content_not_only_statistics(monkeypatch):
    slots = {
        "recent": _slot("recent", PriorityTier.T2_DYNAMIC, "r" * 396, 99),
        "retrieved": _slot("retrieved", PriorityTier.T3_SACRIFICIAL, "m" * 20, 5),
    }
    allocator = _allocator_with_slots(monkeypatch, slots)
    allocator.T1_BUDGET_RATIO = 0.0
    allocator.T2_BUDGET_RATIO = 0.99
    allocator.T3_BUDGET_RATIO = 0.01

    allocation = allocator.allocate("novel-1", 2, "outline", total_budget=100)

    assert allocation.t3_allocated == 5
    assert slots["recent"].tokens == allocation.t2_allocated
    assert allocator.estimate_tokens(allocation.get_final_context()) <= 100


def test_t3_reservation_reclaims_t2_when_shortfall_equals_t2_allocation(monkeypatch):
    slots = {
        "t1": _slot("t1", PriorityTier.T1_COMPRESSIBLE, "a" * 380, 95),
        "recent": _slot("recent", PriorityTier.T2_DYNAMIC, "r" * 16, 4),
        "retrieved": _slot("retrieved", PriorityTier.T3_SACRIFICIAL, "m" * 20, 5),
    }
    allocator = _allocator_with_slots(monkeypatch, slots)
    allocator.T1_BUDGET_RATIO = 0.95
    allocator.T2_BUDGET_RATIO = 0.04
    allocator.T3_BUDGET_RATIO = 0.01

    allocation = allocator.allocate("novel-1", 2, "outline", total_budget=100)

    assert slots["retrieved"].content == "m" * 20
    assert allocation.t3_allocated == 5
    assert "m" * 20 in allocation.get_final_context()


def test_allocator_counts_headers_and_compresses_rendered_context(monkeypatch):
    slots = {
        "critical": _slot(
            "VERY LONG CRITICAL HEADER",
            PriorityTier.T0_CRITICAL,
            "事" * 30,
            20,
            priority=100,
        ),
        "tail": _slot("tail", PriorityTier.T1_COMPRESSIBLE, "尾" * 30, 20),
    }
    allocator = _allocator_with_slots(monkeypatch, slots)

    allocation = allocator.allocate("novel-1", 2, "outline", total_budget=30)

    assert allocator.estimate_tokens(allocation.get_final_context()) <= 30
    assert allocation.used_tokens == allocator.estimate_tokens(allocation.get_final_context())


def test_allocator_raises_clear_error_when_critical_header_cannot_fit(monkeypatch):
    slots = {
        "critical": _slot(
            "CRITICAL HEADER THAT CANNOT FIT",
            PriorityTier.T0_CRITICAL,
            "事实",
            2,
            priority=100,
        )
    }
    allocator = _allocator_with_slots(monkeypatch, slots)

    with pytest.raises(ContextBudgetExceededError, match="context budget"):
        allocator.allocate("novel-1", 2, "outline", total_budget=1)


def test_allocator_fails_closed_when_minimal_fact_lock_cannot_fit(monkeypatch):
    compact_fact_lock = "\n".join(
        f"[第1章] 甲硬事实行{i}: 甲的不可逆状态{i}" for i in range(1, 8)
    )
    slots = {
        "fact_lock": ContextSlot(
            name="FACT_LOCK",
            tier=PriorityTier.T0_CRITICAL,
            content=compact_fact_lock,
            tokens=100,
            max_tokens=100,
            priority=120,
        ),
    }
    allocator = _allocator_with_slots(monkeypatch, slots)

    with pytest.raises(ContextBudgetExceededError, match="context budget"):
        allocator.allocate("novel-1", 800, "甲", total_budget=20)


def test_allocator_fails_closed_when_fact_lock_budget_only_fits_header(monkeypatch):
    header = "【绝对事实边界（一旦违背即为废稿）】"
    fact_row = "   [第1章] 甲的右臂已断，不能再双手持剑。"
    allocator = ContextBudgetAllocator()
    slots = {
        "fact_lock": ContextSlot(
            name="FACT_LOCK",
            tier=PriorityTier.T0_CRITICAL,
            content=f"{header}\n{fact_row}",
            tokens=allocator.estimate_tokens(f"{header}\n{fact_row}"),
            max_tokens=allocator.estimate_tokens(header),
            priority=120,
        ),
    }
    allocator = _allocator_with_slots(monkeypatch, slots)

    with pytest.raises(ContextBudgetExceededError, match="complete FACT_LOCK fact row"):
        allocator.allocate("novel-1", 2, "甲", total_budget=1000)


def test_unrelated_completed_beats_can_be_compressed_without_removing_minimal_fact_lock(
    monkeypatch,
):
    compact_fact_lock = "\n".join(
        f"[第1章] 甲硬事实行{i}: 甲的不可逆状态{i}" for i in range(1, 8)
    )
    slots = {
        "fact_lock": ContextSlot(
            name="FACT_LOCK",
            tier=PriorityTier.T0_CRITICAL,
            content=compact_fact_lock,
            tokens=100,
            max_tokens=100,
            priority=120,
        ),
        "completed_beats": ContextSlot(
            name="COMPLETED_BEATS",
            tier=PriorityTier.T1_COMPRESSIBLE,
            content="\n".join(f"[第{i}章] 无关旧节拍{i}" for i in range(1, 801)),
            tokens=800,
            max_tokens=800,
            priority=76,
        ),
    }
    allocator = _allocator_with_slots(monkeypatch, slots)

    allocation = allocator.allocate("novel-1", 800, "甲", total_budget=210)

    assert compact_fact_lock in allocation.get_final_context()
    assert allocation.slots["completed_beats"].tokens < 800


def test_8k_budget_preserves_fact_lock_and_leaves_body_context_capacity(monkeypatch):
    compact_fact_lock = "\n".join(
        f"[第1章] 甲硬事实行{i}: 甲的不可逆状态{i}" for i in range(1, 31)
    )
    slots = {
        "lifecycle": _slot("生命周期", PriorityTier.T0_CRITICAL, "节奏引导" * 100, 600, 130),
        "anchor": _slot("主线锚点", PriorityTier.T0_CRITICAL, "主线" * 50, 300, 125),
        "promise": _slot("叙事承诺", PriorityTier.T0_CRITICAL, "承诺" * 70, 420, 123),
        "contract": _slot("创作契约", PriorityTier.T0_CRITICAL, "作者硬约束" * 200, 1400, 122),
        "fact_lock": ContextSlot(
            name="FACT_LOCK",
            tier=PriorityTier.T0_CRITICAL,
            content=compact_fact_lock,
            tokens=1000,
            max_tokens=1000,
            priority=120,
        ),
        "chapter_task": ContextSlot(
            name="CURRENT_CHAPTER_TASK",
            tier=PriorityTier.T2_DYNAMIC,
            content="本章章纲：甲必须带着代价完成选择并留下下一章钩子。",
            tokens=40,
            max_tokens=40,
            priority=50,
        ),
        "soft_character": ContextSlot(
            name="CHARACTER_SOFT_CONTEXT",
            tier=PriorityTier.T1_COMPRESSIBLE,
            content="甲的呼吸、动作和对话节奏。" * 30,
            tokens=100,
            max_tokens=100,
            priority=40,
        ),
    }
    allocator = _allocator_with_slots(monkeypatch, slots)

    allocation = allocator.allocate("novel-1", 800, "甲", total_budget=8000)

    assert compact_fact_lock in allocation.get_final_context()
    assert "本章章纲：甲必须带着代价完成选择并留下下一章钩子。" in allocation.get_final_context()
    assert "甲的呼吸、动作和对话节奏" in allocation.get_final_context()
    assert allocator.estimate_tokens(allocation.get_final_context()) <= 8000


def test_allocator_propagates_configured_memory_engine_fact_lock_failure(monkeypatch):
    class BrokenMemoryEngine:
        def build_fact_lock_section(self, novel_id, chapter_number):
            raise RuntimeError("configured fact lock unavailable")

    allocator = ContextBudgetAllocator(memory_engine=BrokenMemoryEngine())
    monkeypatch.setattr(allocator, "_estimate_total_chapters", lambda _novel_id: 100)

    with pytest.raises(RuntimeError, match="configured fact lock unavailable"):
        allocator.allocate("novel-1", 2, "outline", total_budget=1000)


def test_allocator_stops_when_completed_beats_cannot_be_read(monkeypatch):
    class BrokenMemoryEngine:
        def build_fact_lock_section(self, _novel_id, _chapter_number):
            return "FACT_LOCK"

        def get_completed_beats_section(self, _novel_id):
            raise MemoryStateUnavailableError("memory_state_unavailable: database read failed")

        def get_revealed_clues_section(self, _novel_id):
            return ""

    allocator = ContextBudgetAllocator(memory_engine=BrokenMemoryEngine())
    monkeypatch.setattr(allocator, "_estimate_total_chapters", lambda _novel_id: 100)

    with pytest.raises(RuntimeError, match="memory_state_unavailable"):
        allocator.allocate("novel-1", 2, "outline", total_budget=1000)


def test_graph_subnetwork_matches_bible_character_from_explicit_novel_id():
    class BibleRepository:
        def get_by_novel_id(self, novel_id):
            if novel_id.value != "novel-graph":
                return None
            return SimpleNamespace(
                characters=[
                    SimpleNamespace(
                        name="阿止",
                        character_id=SimpleNamespace(value="character-azhi"),
                    )
                ]
            )

    class TripleRepository:
        def get_by_entity_ids_sync(self, _novel_id, entity_ids):
            if "character-azhi" not in entity_ids:
                return []
            return [
                SimpleNamespace(
                    id="azhi-clocktower",
                    subject_id="阿止",
                    predicate="守护",
                    object_id="旧钟楼",
                    subject_type="character",
                    object_type="location",
                    confidence=1.0,
                    related_chapters=[3],
                    first_appearance=3,
                    description="阿止的当前守护地点",
                )
            ]

        def get_recent_triples_sync(self, *_args, **_kwargs):
            return []

        def get_starred_triple_ids_sync(self, _novel_id):
            return []

    allocator = ContextBudgetAllocator(
        bible_repository=BibleRepository(),
        triple_repository=TripleRepository(),
    )

    context = allocator._get_graph_subnetwork(
        "novel-graph", 5, "阿止正在前往旧钟楼"
    )

    assert "阿止 —守护→ 旧钟楼" in context


def test_context_slots_include_saved_locations_without_outline_mentions(monkeypatch):
    """SETTING-004: first-chapter prose must know the saved location catalog."""
    locations = [
        SimpleNamespace(
            name="雾港",
            description="终年被白雾遮住的盐商港口。",
            location_type="city",
        ),
        SimpleNamespace(
            name="赤岩塔",
            description="海崖上的旧烽火塔，夜间仍会发出红光。",
            location_type="landmark",
        ),
        SimpleNamespace(
            name="潮汐议会",
            description="控制航道税与港口执法的地方势力。",
            location_type="faction",
        ),
    ]
    bible = SimpleNamespace(
        characters=[],
        locations=locations,
        world_settings=[],
        style_notes=[],
    )
    bible_repository = SimpleNamespace(get_by_novel_id=lambda _novel_id: bible)
    allocator = ContextBudgetAllocator(bible_repository=bible_repository)
    monkeypatch.setattr(
        allocator,
        "_build_lifecycle_directive",
        lambda _novel_id, _chapter_number: "",
    )
    monkeypatch.setattr(
        allocator,
        "_build_anti_ai_protocol_block",
        lambda _novel_id, _chapter_number: "",
    )

    slots = allocator._collect_all_slots(
        "novel-location-catalog",
        1,
        "主角在雨棚下醒来，尚未决定前往何处。",
    )

    catalog = slots["location_catalog"]
    assert catalog.content.startswith("=== 可用地点与势力 ===")
    assert "雾港" in catalog.content
    assert "赤岩塔" in catalog.content
    assert "潮汐议会" in catalog.content
