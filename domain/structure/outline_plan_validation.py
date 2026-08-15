"""Deterministic validation for a complete direct-child planning cohort."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Sequence

from domain.structure.outline_contract import OutlineLevel, OutlinePayload
from domain.structure.outline_plan import OutlinePlanItem


@dataclass(frozen=True)
class CohortValidationResult:
    blockers: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.blockers


@dataclass(frozen=True)
class ReplanImpactClosure:
    """Logical nodes that cannot be reused after a sibling-chain change."""

    invalidated_logical_node_ids: tuple[str, ...]
    ancestor_logical_node_ids: tuple[str, ...]


def compute_replan_impact_closure(
    items: Sequence[OutlinePlanItem], changed_logical_node_id: str
) -> ReplanImpactClosure:
    """Fail closed over the changed node, later siblings, and descendants."""

    by_id = {item.logical_node_id: item for item in items}
    changed = by_id.get(changed_logical_node_id)
    if changed is None:
        raise ValueError("changed logical node is not in the plan")
    sibling_roots = {
        item.logical_node_id
        for item in items
        if item.parent_logical_node_id == changed.parent_logical_node_id
        and item.level == changed.level
        and item.sibling_index >= changed.sibling_index
    }
    invalidated = set(sibling_roots)
    changed_any = True
    while changed_any:
        changed_any = False
        for item in items:
            if (
                item.parent_logical_node_id in invalidated
                and item.logical_node_id not in invalidated
            ):
                invalidated.add(item.logical_node_id)
                changed_any = True
    ancestors: list[str] = []
    cursor = changed.parent_logical_node_id
    while cursor:
        parent = by_id.get(cursor)
        if parent is None:
            raise ValueError("plan contains a missing ancestor")
        ancestors.append(parent.logical_node_id)
        cursor = parent.parent_logical_node_id
    ordered_invalidated = tuple(
        item.logical_node_id for item in items if item.logical_node_id in invalidated
    )
    return ReplanImpactClosure(
        invalidated_logical_node_ids=ordered_invalidated,
        ancestor_logical_node_ids=tuple(reversed(ancestors)),
    )


def merge_author_locked_payload(
    authored: OutlinePayload, inferred: OutlinePayload
) -> tuple[OutlinePayload, tuple[str, ...]]:
    """Preserve author fields while allowing AI to fill missing structure."""

    authored_values = authored.canonical_dict()
    inferred_values = inferred.canonical_dict()
    extra = dict(authored_values.get("extra") or {})
    provenance = dict(extra.get("_field_provenance") or {})
    conflicts: list[str] = []
    for field, inferred_value in inferred_values.items():
        if field == "extra":
            continue
        authored_value = authored_values.get(field)
        rule = provenance.get(field)
        locked = isinstance(rule, dict) and bool(rule.get("locked"))
        if locked:
            if inferred_value not in (None, "", [], {}) and inferred_value != authored_value:
                conflicts.append(field)
            continue
        if authored_value in (None, "", [], {}):
            authored_values[field] = inferred_value
            if inferred_value not in (None, "", [], {}):
                provenance[field] = {"source": "ai", "locked": False}
    extra["_field_provenance"] = provenance
    authored_values["extra"] = extra
    return OutlinePayload.from_dict(authored_values), tuple(conflicts)


def validate_sibling_cohort(
    *,
    level: OutlineLevel,
    parent_payload: OutlinePayload,
    siblings: Sequence[OutlinePayload],
) -> CohortValidationResult:
    """Validate the hard continuity rules that prose review cannot replace."""

    blockers: list[str] = []
    if not siblings:
        return CohortValidationResult(("cohort:empty",))

    expected_start = parent_payload.chapter_start
    expected_end = parent_payload.chapter_end
    previous: OutlinePayload | None = None
    for index, payload in enumerate(siblings):
        blockers.extend(f"sibling:{index}:{blocker}" for blocker in payload.sibling_continuity_blockers())
        if expected_start is not None and payload.chapter_start is None:
            blockers.append(f"chapter_range:{index}:start_missing")
        if expected_end is not None and payload.chapter_end is None:
            blockers.append(f"chapter_range:{index}:end_missing")
        if payload.chapter_start is not None and payload.chapter_end is not None:
            if payload.chapter_start > payload.chapter_end:
                blockers.append(f"chapter_range:{index}:inverted")
            if index == 0 and expected_start is not None and payload.chapter_start != expected_start:
                blockers.append(
                    f"chapter_range:first_start_mismatch:expected={expected_start}:actual={payload.chapter_start}"
                )
            if previous and previous.chapter_end is not None:
                expected_next = previous.chapter_end + 1
                if payload.chapter_start != expected_next:
                    if payload.chapter_start < expected_next:
                        blockers.append(
                            f"chapter_range:overlap:expected={expected_next}:actual={payload.chapter_start}"
                        )
                    else:
                        blockers.append(
                            f"chapter_range:gap:expected={expected_next}:actual={payload.chapter_start}"
                        )
        if previous is None:
            if parent_payload.entry_state and payload.entry_state != parent_payload.entry_state:
                blockers.append("handoff:first_entry_state_mismatch")
        elif payload.entry_state != previous.exit_state:
            blockers.append(f"handoff:{index}:entry_state_mismatch")
        previous = payload

    last = siblings[-1]
    if expected_end is not None and last.chapter_end != expected_end:
        blockers.append(
            f"chapter_range:last_end_mismatch:expected={expected_end}:actual={last.chapter_end}"
        )
    if parent_payload.exit_state and last.exit_state != parent_payload.exit_state:
        blockers.append("handoff:last_exit_state_mismatch")
    return CohortValidationResult(tuple(blockers))
