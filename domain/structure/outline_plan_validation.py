"""Deterministic validation for a complete direct-child planning cohort."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from domain.structure.outline_contract import OutlineLevel, OutlinePayload


@dataclass(frozen=True)
class CohortValidationResult:
    blockers: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.blockers


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
