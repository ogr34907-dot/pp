from __future__ import annotations

import pytest

from application.engine.services.context_budget_allocator import (
    ContextBudgetAllocator,
    ContextBudgetExceededError,
)
from application.engine.services.context_budget_models import ContextSlot, PriorityTier


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
