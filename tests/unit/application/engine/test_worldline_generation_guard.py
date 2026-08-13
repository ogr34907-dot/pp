"""Retired worldline vector payloads are never eligible for current retrieval."""

import pytest

from application.engine.services.worldline_generation_guard import (
    GenerationEpochUnavailableError,
    active_generation_epoch,
    is_payload_in_active_epoch,
)


def test_new_generation_filters_retired_and_untagged_vectors_until_rebuild():
    assert is_payload_in_active_epoch({"generation_epoch": 3}, active_epoch=3)
    assert not is_payload_in_active_epoch({"generation_epoch": 2}, active_epoch=3)
    assert not is_payload_in_active_epoch({}, active_epoch=3)


def test_initial_generation_keeps_legacy_untagged_vectors_compatible():
    assert is_payload_in_active_epoch({}, active_epoch=0)
    assert is_payload_in_active_epoch({"generation_epoch": 0}, active_epoch=0)


def test_epoch_read_failure_does_not_fall_back_to_epoch_zero():
    class BrokenDatabase:
        def fetch_one(self, *_args, **_kwargs):
            raise OSError("worldline database is unavailable")

    with pytest.raises(GenerationEpochUnavailableError, match="generation_epoch_unavailable"):
        active_generation_epoch("novel-1", BrokenDatabase())
