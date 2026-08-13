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
