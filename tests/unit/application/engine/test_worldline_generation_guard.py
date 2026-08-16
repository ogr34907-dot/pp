"""Retired worldline payloads are never eligible for current retrieval."""

import hashlib

import pytest

from application.engine.services.worldline_generation_guard import (
    GenerationEpochUnavailableError,
    active_generation_epoch,
    is_payload_in_active_epoch,
    visible_committed_chapters,
)
from application.world.services.chapter_narrative_sync import CHAPTER_NARRATIVE_PIPELINE_VERSION
from infrastructure.persistence.database.chapter_candidate_repository import (
    ChapterCandidateRepository,
)
from infrastructure.persistence.database.connection import DatabaseConnection


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


def test_visible_committed_chapters_requires_current_formal_identity_and_ready_epoch(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline-visible-formal-identity.db"))
    conn = db.get_connection()
    current_content = "current formal prose"
    current_hash = hashlib.sha256(current_content.encode("utf-8")).hexdigest()
    retired_content = "retired tail prose"
    retired_hash = hashlib.sha256(retired_content.encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) "
        "VALUES ('novel-1', 'Worldline', 'worldline-visible', 3)"
    )
    conn.execute(
        "INSERT INTO chapters (id, novel_id, number, title, content, content_sha256, "
        "content_revision, status) VALUES ('chapter-1', 'novel-1', 1, 'Chapter 1', ?, ?, 1, 'completed')",
        (current_content, current_hash),
    )
    conn.commit()
    ChapterCandidateRepository(db).import_legacy_formal_history("novel-1")
    conn.execute(
        "INSERT INTO chapters (id, novel_id, number, title, content, content_sha256, "
        "content_revision, status) VALUES ('chapter-2', 'novel-1', 2, 'Chapter 2', ?, ?, 1, 'completed')",
        (retired_content, retired_hash),
    )
    conn.execute(
        "INSERT INTO novel_generation_runs "
        "(novel_id, run_mode, state, generation_epoch, target_chapters, current_formal_chapter, "
        "canonical_sync_status, next_action) "
        "VALUES ('novel-1', 'continuous', 'paused', 2, 3, 1, 'ready', 'select_run_mode')"
    )
    conn.execute(
        "INSERT INTO worldline_generation_filters (novel_id, active_generation_epoch) "
        "VALUES ('novel-1', 2)"
    )
    conn.execute(
        "INSERT INTO chapter_narrative_commits "
        "(novel_id, chapter_number, content_sha256, pipeline_version, content_revision, status) "
        "VALUES ('novel-1', 1, ?, ?, 1, 'committed')",
        (current_hash, CHAPTER_NARRATIVE_PIPELINE_VERSION),
    )
    conn.execute(
        "INSERT INTO chapter_narrative_commits "
        "(novel_id, chapter_number, content_sha256, pipeline_version, content_revision, status) "
        "VALUES ('novel-1', 1, 'old-revision', ?, 1, 'committed')",
        (CHAPTER_NARRATIVE_PIPELINE_VERSION,),
    )
    conn.execute(
        "INSERT INTO chapter_narrative_commits "
        "(novel_id, chapter_number, content_sha256, pipeline_version, content_revision, status) "
        "VALUES ('novel-1', 2, ?, ?, 1, 'committed')",
        (retired_hash, CHAPTER_NARRATIVE_PIPELINE_VERSION),
    )
    conn.commit()

    assert visible_committed_chapters("novel-1", db) == {1}

    conn.execute(
        "UPDATE novel_generation_runs SET canonical_sync_status = 'rebuilding' "
        "WHERE novel_id = 'novel-1'"
    )
    conn.commit()
    assert visible_committed_chapters("novel-1", db) == set()
