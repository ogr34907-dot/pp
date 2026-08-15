import pytest

from application.engine.services.context_budget_models import (
    ContextBudgetExceededError,
    ContextSlot,
    PriorityTier,
)
from application.engine.services.context_budget_policy import allocate_tier, truncate_t0_slots


def _slot(content, tokens, priority=0, max_tokens=100, min_tokens=0):
    return ContextSlot(
        name="slot",
        tier=PriorityTier.T1_COMPRESSIBLE,
        content=content,
        tokens=tokens,
        max_tokens=max_tokens,
        min_tokens=min_tokens,
        priority=priority,
    )


def test_allocate_tier_keeps_high_priority_and_logs_original_tokens():
    slots = {
        "high": _slot("高" * 20, tokens=10, priority=100),
        "low": _slot("低" * 30, tokens=30, priority=1, max_tokens=30),
    }
    log = []

    used = allocate_tier(slots, 20, log, chars_per_token_zh=1.0)

    assert used == 20
    assert slots["high"].tokens == 10
    assert slots["low"].tokens == 10
    assert log == ["压缩 low: 30 → 10 tokens"]


def test_allocate_tier_discards_when_remaining_below_minimum():
    slots = {
        "high": _slot("高" * 20, tokens=10, priority=100),
        "low": _slot("低" * 30, tokens=30, priority=1, max_tokens=30, min_tokens=12),
    }
    log = []

    used = allocate_tier(slots, 20, log, chars_per_token_zh=1.0)

    assert used == 10
    assert slots["low"].content == ""
    assert slots["low"].tokens == 0
    assert log == ["舍弃 low（预算不足）"]


def test_allocate_tier_applies_slot_maximum_before_tier_budget():
    slots = {
        "capped": _slot("限" * 30, tokens=30, priority=100, max_tokens=10),
    }
    log = []

    used = allocate_tier(slots, 100, log, chars_per_token_zh=1.0)

    assert used == 10
    assert slots["capped"].tokens == 10
    assert slots["capped"].content == "限" * 10


def test_truncate_t0_slots_allocates_remaining_budget_to_floor_free_slots():
    slots = {
        "first": ContextSlot(
            name="first",
            tier=PriorityTier.T0_CRITICAL,
            content="一" * 20,
            tokens=10,
        ),
        "second": ContextSlot(
            name="second",
            tier=PriorityTier.T0_CRITICAL,
            content="二" * 20,
            tokens=10,
        ),
        "third": ContextSlot(
            name="third",
            tier=PriorityTier.T0_CRITICAL,
            content="三" * 20,
            tokens=10,
        ),
    }

    used = truncate_t0_slots(slots, 15, chars_per_token_zh=1.0)

    assert used == 15
    assert slots["first"].tokens == 10
    assert slots["second"].tokens == 5
    assert slots["second"].content == "二" * 5
    assert slots["third"].tokens == 0
    assert slots["third"].content == ""


def test_truncate_t0_slots_fails_closed_when_declared_floors_exceed_budget():
    slots = {
        "fact_lock": ContextSlot(
            name="fact_lock",
            tier=PriorityTier.T0_CRITICAL,
            content="甲" * 10,
            tokens=10,
            min_tokens=10,
        ),
        "required_bridge": ContextSlot(
            name="required_bridge",
            tier=PriorityTier.T0_CRITICAL,
            content="乙" * 10,
            tokens=10,
            min_tokens=10,
        ),
    }

    with pytest.raises(ContextBudgetExceededError, match="cannot fit T0 minimum floors"):
        truncate_t0_slots(slots, 15, chars_per_token_zh=1.0)
