"""Strict validation for the persisted full-book chapter target."""

from __future__ import annotations

from typing import Any, Optional


def positive_integer_or_none(value: Any) -> Optional[int]:
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None
