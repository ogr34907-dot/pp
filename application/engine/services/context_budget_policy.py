"""Budget compression policy for context slots."""
from __future__ import annotations

from typing import Dict, List

from application.engine.services.context_budget_models import (
    ContextBudgetExceededError,
    ContextSlot,
)


def _clear_slot(slot: ContextSlot) -> None:
    slot.content = ""
    slot.tokens = 0


def _cap_slot(slot: ContextSlot, limit: int, *, chars_per_token_zh: float) -> None:
    if limit <= 0:
        _clear_slot(slot)
        return
    target_chars = int(limit * chars_per_token_zh)
    slot.content = slot.content[:target_chars]
    slot.tokens = limit


def truncate_t0_slots(
    t0_slots: Dict[str, ContextSlot],
    budget: int,
    *,
    chars_per_token_zh: float,
) -> int:
    """Compress T0 slots without crossing any declared minimum floor."""
    floors = {
        id(slot): max(0, int(slot.min_tokens or 0))
        for slot in t0_slots.values()
    }
    floor_total = sum(floors.values())
    if floor_total > budget:
        raise ContextBudgetExceededError(
            f"context budget {budget} cannot fit T0 minimum floors ({floor_total} tokens)"
        )

    extra_budget = budget - floor_total
    total = 0
    for slot in t0_slots.values():
        floor = floors[id(slot)]
        current = max(0, int(slot.tokens or 0))
        if current < floor:
            raise ContextBudgetExceededError(
                f"context slot {slot.name!r} is below its minimum floor "
                f"({current} < {floor} tokens)"
            )

        extra = current - floor
        kept_extra = min(extra, extra_budget)
        target = floor + kept_extra
        if target < current:
            if target <= 0:
                _clear_slot(slot)
            else:
                _cap_slot(slot, target, chars_per_token_zh=chars_per_token_zh)
        total += target
        extra_budget -= kept_extra
    return total


def allocate_tier(
    tier_slots: Dict[str, ContextSlot],
    budget: int,
    compression_log: List[str],
    *,
    chars_per_token_zh: float,
) -> int:
    """Allocate a budget for one tier by priority, mutating overflowing slots."""
    sorted_slots = sorted(
        tier_slots.items(),
        key=lambda item: item[1].priority,
        reverse=True,
    )

    total_used = 0
    for name, slot in sorted_slots:
        if slot.max_tokens is not None and slot.max_tokens >= 0 and slot.tokens > slot.max_tokens:
            original_tokens = slot.tokens
            _cap_slot(slot, slot.max_tokens, chars_per_token_zh=chars_per_token_zh)
            compression_log.append(
                f"槽位上限 {name}: {original_tokens} → {slot.tokens} tokens"
            )

        if total_used + slot.tokens <= budget:
            total_used += slot.tokens
            continue

        remaining = budget - total_used
        original_tokens = slot.tokens
        if slot.max_tokens and slot.max_tokens > 0:
            if remaining > slot.min_tokens:
                target_chars = int(remaining * chars_per_token_zh)
                slot.content = slot.content[:target_chars] + "..."
                slot.tokens = remaining
                total_used += remaining
                compression_log.append(f"压缩 {name}: {original_tokens} → {remaining} tokens")
            else:
                _clear_slot(slot)
                compression_log.append(f"舍弃 {name}（预算不足）")
            continue

        if remaining > 0:
            target_chars = int(remaining * chars_per_token_zh)
            slot.content = slot.content[:target_chars] + "..."
            slot.tokens = remaining
            total_used += remaining
            compression_log.append(f"截断 {name}: {original_tokens} → {remaining} tokens")
        else:
            _clear_slot(slot)

    return total_used
