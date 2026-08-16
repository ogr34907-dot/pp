"""A regenerated worldline cannot resume until its retained prefix is canonical again."""

import asyncio
import hashlib
import json
from types import SimpleNamespace
import pytest

from application.engine.services.chapter_aftermath_pipeline import ChapterAftermathPipeline
from application.engine.services.memory_engine import MemoryEngine
from application.engine.services.worldline_rebuild_service import (
    WorldlineRebuildError,
    WorldlineRebuildService,
)
from application.engine.services.worldline_regeneration_service import WorldlineRegenerationService
from application.world.services.knowledge_service import KnowledgeService
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.chapter_candidate_repository import (
    ChapterCandidateRepository,
)
from infrastructure.persistence.database.sqlite_chapter_narrative_commit_repository import (
    SqliteChapterNarrativeCommitRepository,
)
from infrastructure.persistence.database.sqlite_chapter_repository import (
    SqliteChapterRepository,
)
from infrastructure.persistence.database.sqlite_character_state_repository import (
    SqliteCharacterStateRepository,
)
from infrastructure.persistence.database.sqlite_knowledge_repository import (
    SqliteKnowledgeRepository,
)
from application.world.services.chapter_narrative_sync import CHAPTER_NARRATIVE_PIPELINE_VERSION
from domain.knowledge.chapter_summary import canonical_summary_payload_sha256
from domain.novel.value_objects.novel_id import NovelId


class _Aftermath:
    def __init__(self, db, ok: bool = True, memory_ready: bool = True):
        self.db = db
        self.ok = ok
        self.memory_ready = memory_ready
        self.chapters: list[int] = []

    async def run_after_chapter_saved(self, novel_id, chapter_number, content, **kwargs):
        self.chapters.append(chapter_number)
        if not self.ok:
            return {"narrative_sync_ok": False, "failure_reason": "fake-sync-failed"}
        content_sha256 = str(kwargs["expected_content_sha256"])
        content_revision = int(kwargs["expected_content_revision"])
        conn = self.db.get_connection()
        conn.execute(
            "INSERT OR IGNORE INTO knowledge (id, novel_id) VALUES ('knowledge-1', ?)",
            (novel_id,),
        )
        summary = {
            "summary": content,
            "key_events": content,
            "open_threads": "",
            "consistency_note": "",
            "beat_sections": [],
            "micro_beats": [],
        }
        payload_sha256 = canonical_summary_payload_sha256(**summary)
        conn.execute(
            "INSERT INTO chapter_summaries "
            "(id, knowledge_id, chapter_number, summary, key_events, open_threads, "
            "consistency_note, beat_sections, micro_beats, source_content_sha256, "
            "source_content_revision, pipeline_version, sync_status, sync_attempts, "
            "canonical_payload_sha256) VALUES (?, 'knowledge-1', ?, ?, ?, ?, ?, '[]', '[]', ?, ?, ?, 'draft', 0, ?)",
            (
                f"summary-{chapter_number}", chapter_number, summary["summary"],
                summary["key_events"], summary["open_threads"], summary["consistency_note"],
                content_sha256, content_revision, CHAPTER_NARRATIVE_PIPELINE_VERSION,
                payload_sha256,
            ),
        )
        conn.commit()
        repo = SqliteChapterNarrativeCommitRepository(self.db)
        claim = repo.claim(
            novel_id=novel_id,
            chapter_number=chapter_number,
            content_sha256=content_sha256,
            pipeline_version=CHAPTER_NARRATIVE_PIPELINE_VERSION,
            expected_content_revision=content_revision,
        )
        repo.prepare_summary(
            novel_id=novel_id,
            chapter_number=chapter_number,
            content_sha256=content_sha256,
            pipeline_version=CHAPTER_NARRATIVE_PIPELINE_VERSION,
            attempt_count=claim.attempt_count,
            canonical_payload_sha256=payload_sha256,
        )
        repo.commit(
            novel_id=novel_id,
            chapter_number=chapter_number,
            content_sha256=content_sha256,
            pipeline_version=CHAPTER_NARRATIVE_PIPELINE_VERSION,
            attempt_count=claim.attempt_count,
            content_revision=content_revision,
            canonical_payload_sha256=payload_sha256,
        )
        if self.memory_ready:
            assert repo.finish_memory_sync(
                novel_id=novel_id,
                chapter_number=chapter_number,
                content_sha256=content_sha256,
                pipeline_version=CHAPTER_NARRATIVE_PIPELINE_VERSION,
                content_revision=content_revision,
            )
        return {
            "narrative_sync_ok": True,
            "commit_status": "committed",
            "pipeline_version": CHAPTER_NARRATIVE_PIPELINE_VERSION,
            "content_revision": content_revision,
        }


class _BlockingAftermath(_Aftermath):
    def __init__(self, db):
        super().__init__(db)
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def run_after_chapter_saved(self, novel_id, chapter_number, content, **kwargs):
        self.started.set()
        await self.release.wait()
        return await super().run_after_chapter_saved(novel_id, chapter_number, content, **kwargs)


def _seed(db, *, import_legacy: bool = True):
    conn = db.get_connection()
    conn.execute("INSERT INTO novels (id, title, slug, target_chapters) VALUES ('novel-1', '重建小说', 'rebuild', 5)")
    for number in (1, 2):
        conn.execute(
            """
            INSERT INTO chapters (id, novel_id, number, title, content, content_sha256, content_revision, status)
            VALUES (?, 'novel-1', ?, ?, ?, ?, 1, 'completed')
            """,
            (
                f"chapter-{number}",
                number,
                f"第{number}章",
                f"正文{number}",
                hashlib.sha256(f"正文{number}".encode("utf-8")).hexdigest(),
            ),
        )
    conn.commit()
    if import_legacy:
        ChapterCandidateRepository(db).import_legacy_formal_history("novel-1")


@pytest.mark.asyncio
async def test_rebuild_replays_retained_prefix_before_mode_can_resume(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline-rebuild.db"))
    _seed(db)
    reset = WorldlineRegenerationService(db)
    preview = reset.preview("novel-1", start_chapter=2, target_chapters=5)
    reset.execute("novel-1", preview_token=preview.token, run_mode="continuous")
    aftermath = _Aftermath(db)

    result = await WorldlineRebuildService(db, aftermath).rebuild("novel-1")

    assert result["status"] == "completed"
    assert aftermath.chapters == [1]
    run = db.fetch_one(
        "SELECT state, canonical_sync_status, next_action FROM novel_generation_runs WHERE novel_id = 'novel-1'"
    )
    assert (run["state"], run["canonical_sync_status"], run["next_action"]) == (
        "paused",
        "ready",
        "select_run_mode",
    )
    assert db.fetch_one(
        "SELECT COUNT(*) AS total FROM worldline_rebuild_jobs WHERE novel_id = 'novel-1' AND status = 'completed'"
    )["total"] == 5


def test_candidate_first_rebuild_rejects_chapter_version_outside_formal_authority(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline-authority-mismatch.db"))
    _seed(db)
    conn = db.get_connection()
    accepted_hash = hashlib.sha256("正文1".encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO chapter_candidates "
        "(id, novel_id, chapter_number, generation_epoch, status, llm_content) "
        "VALUES ('candidate-1', 'novel-1', 1, 0, 'committed', '正文1')"
    )
    conn.execute(
        "INSERT INTO chapter_candidate_formal_commits "
        "(candidate_id, novel_id, chapter_number, chapter_id, content_sha256, "
        "content_revision, provenance, sync_status) "
        "VALUES ('candidate-1', 'novel-1', 1, 'chapter-1', ?, 1, 'candidate_commit', 'ready')",
        (accepted_hash,),
    )
    conn.execute(
        "UPDATE chapters SET content = '越权正文', content_sha256 = ?, content_revision = 2 "
        "WHERE id = 'chapter-1'",
        (hashlib.sha256("越权正文".encode("utf-8")).hexdigest(),),
    )
    conn.commit()

    with pytest.raises(WorldlineRebuildError, match="formal chapter authority mismatch"):
        WorldlineRebuildService._retained_formal_chapters(conn, "novel-1")


@pytest.mark.asyncio
async def test_rebuild_promotes_author_rewrite_authority_only_after_replay(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline-author-rewrite.db"))
    _seed(db)
    conn = db.get_connection()
    content_hash = hashlib.sha256("正文1".encode("utf-8")).hexdigest()
    conn.execute(
        "INSERT INTO chapter_candidates "
        "(id, novel_id, chapter_number, generation_epoch, status, llm_content) "
        "VALUES ('candidate-1', 'novel-1', 1, 0, 'syncing', '旧候选正文')"
    )
    conn.execute(
        "INSERT INTO chapter_candidate_formal_commits "
        "(candidate_id, novel_id, chapter_number, chapter_id, content_sha256, "
        "content_revision, provenance, sync_status) "
        "VALUES ('candidate-1', 'novel-1', 1, 'chapter-1', ?, 1, "
        "'author_rewrite', 'syncing')",
        (content_hash,),
    )
    conn.commit()
    reset = WorldlineRegenerationService(db)
    preview = reset.preview("novel-1", start_chapter=2, target_chapters=5)
    reset.execute("novel-1", preview_token=preview.token, run_mode="chapter_review")

    result = await WorldlineRebuildService(db, _Aftermath(db)).rebuild("novel-1")

    assert result["status"] == "completed"
    assert db.fetch_one(
        "SELECT sync_status FROM chapter_candidate_formal_commits "
        "WHERE candidate_id = 'candidate-1'"
    )["sync_status"] == "ready"
    assert db.fetch_one(
        "SELECT status FROM chapter_candidates WHERE id = 'candidate-1'"
    )["status"] == "committed"


@pytest.mark.asyncio
async def test_cancelled_rebuild_cannot_resume_or_overwrite_the_new_epoch(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline-rebuild-cancel.db"))
    _seed(db)
    reset = WorldlineRegenerationService(db)
    preview = reset.preview("novel-1", start_chapter=2, target_chapters=5)
    reset.execute("novel-1", preview_token=preview.token, run_mode="continuous")
    aftermath = _BlockingAftermath(db)
    service = WorldlineRebuildService(db, aftermath)

    task = asyncio.create_task(service.rebuild("novel-1"))
    await asyncio.wait_for(aftermath.started.wait(), timeout=1)
    cancelled = service.cancel("novel-1")
    aftermath.release.set()

    result = await task

    assert result["status"] == "cancelled"
    assert result["generation_epoch"] == cancelled["generation_epoch"]
    assert aftermath.chapters == [1]
    run = db.fetch_one(
        "SELECT state, canonical_sync_status, next_action, generation_epoch "
        "FROM novel_generation_runs WHERE novel_id = 'novel-1'"
    )
    assert (run["state"], run["canonical_sync_status"], run["next_action"]) == (
        "stopped",
        "failed",
        "restart_worldline_rebuild",
    )
    assert run["generation_epoch"] == cancelled["generation_epoch"]


def test_cancel_prelock_epoch_race_does_not_overwrite_new_epoch_run_or_jobs(tmp_path):
    db_path = tmp_path / "worldline-rebuild-cancel-epoch-race.db"
    db = DatabaseConnection(str(db_path))
    _seed(db)
    reset = WorldlineRegenerationService(db)
    preview = reset.preview("novel-1", start_chapter=2, target_chapters=5)
    reset.execute("novel-1", preview_token=preview.token, run_mode="continuous")
    service = WorldlineRebuildService(db, _Aftermath(db))
    old_epoch = int(
        db.fetch_one(
            "SELECT generation_epoch FROM novel_generation_runs WHERE novel_id = 'novel-1'"
        )["generation_epoch"]
    )
    new_epoch = old_epoch + 1
    racing_db = DatabaseConnection(str(db_path))
    original_now = service._now
    raced = False

    def _switch_to_new_epoch_between_precheck_and_lock():
        nonlocal raced
        if not raced:
            raced = True
            conn = racing_db.get_connection()
            conn.execute(
                "UPDATE novel_generation_runs SET generation_epoch = ?, state = 'paused', "
                "canonical_sync_status = 'rebuilding', next_action = 'rebuild_worldline', "
                "last_error = 'new epoch owner' WHERE novel_id = 'novel-1'",
                (new_epoch,),
            )
            conn.execute(
                "UPDATE worldline_generation_filters SET active_generation_epoch = ?, "
                "updated_at = 'new epoch owner' "
                "WHERE novel_id = 'novel-1'",
                (new_epoch,),
            )
            conn.execute(
                "INSERT INTO worldline_rebuild_jobs "
                "(id, novel_id, generation_epoch, job_type, status) "
                "VALUES ('new-epoch-job', 'novel-1', ?, 'canonical_facts', 'running')",
                (new_epoch,),
            )
            conn.commit()
        return original_now()

    service._now = _switch_to_new_epoch_between_precheck_and_lock
    try:
        result = service.cancel("novel-1")
    finally:
        racing_db.close()

    assert result == {
        "status": "cancelled",
        "generation_epoch": new_epoch,
        "rebuilt_chapters": 0,
    }
    run = db.fetch_one(
        "SELECT generation_epoch, state, canonical_sync_status, next_action, last_error "
        "FROM novel_generation_runs WHERE novel_id = 'novel-1'"
    )
    assert tuple(
        run[field]
        for field in (
            "generation_epoch",
            "state",
            "canonical_sync_status",
            "next_action",
            "last_error",
        )
    ) == (new_epoch, "paused", "rebuilding", "rebuild_worldline", "new epoch owner")
    assert db.fetch_one(
        "SELECT status FROM worldline_rebuild_jobs WHERE id = 'new-epoch-job'"
    )["status"] == "running"
    generation_filter = db.fetch_one(
        "SELECT active_generation_epoch, updated_at FROM worldline_generation_filters "
        "WHERE novel_id = 'novel-1'"
    )
    assert tuple(generation_filter[field] for field in ("active_generation_epoch", "updated_at")) == (
        new_epoch,
        "new epoch owner",
    )
    assert db.fetch_one(
        "SELECT COUNT(*) AS total FROM worldline_rebuild_jobs "
        "WHERE novel_id = 'novel-1' AND generation_epoch = ? AND status = 'pending'",
        (old_epoch,),
    )["total"] == 5


@pytest.mark.asyncio
async def test_rebuild_start_epoch_race_cannot_mutate_new_epoch_run_or_jobs(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline-rebuild-start-epoch-race.db"))
    _seed(db)
    reset = WorldlineRegenerationService(db)
    preview = reset.preview("novel-1", start_chapter=2, target_chapters=5)
    reset.execute("novel-1", preview_token=preview.token, run_mode="continuous")
    service = WorldlineRebuildService(db, _Aftermath(db))
    original_now = service._now
    raced = False

    def _race_after_worker_read():
        nonlocal raced
        if not raced:
            raced = True
            conn = db.get_connection()
            conn.execute(
                "UPDATE novel_generation_runs SET generation_epoch = 2, state = 'stopped', "
                "canonical_sync_status = 'failed', next_action = 'restart_worldline_rebuild', "
                "last_error = 'new epoch owner' WHERE novel_id = 'novel-1'"
            )
            conn.execute(
                "UPDATE worldline_generation_filters SET active_generation_epoch = 2 "
                "WHERE novel_id = 'novel-1'"
            )
            conn.commit()
        return original_now()

    service._now = _race_after_worker_read
    result = await service.rebuild("novel-1")

    assert result == {"status": "cancelled", "generation_epoch": 2, "rebuilt_chapters": 0}
    run = db.fetch_one(
        "SELECT generation_epoch, state, canonical_sync_status, next_action, last_error "
        "FROM novel_generation_runs WHERE novel_id = 'novel-1'"
    )
    assert tuple(
        run[field]
        for field in (
            "generation_epoch",
            "state",
            "canonical_sync_status",
            "next_action",
            "last_error",
        )
    ) == (2, "stopped", "failed", "restart_worldline_rebuild", "new epoch owner")
    assert db.fetch_one(
        "SELECT COUNT(*) AS total FROM worldline_rebuild_jobs "
        "WHERE novel_id = 'novel-1' AND generation_epoch = 1 AND status = 'pending'"
    )["total"] == 5


@pytest.mark.asyncio
async def test_rebuild_keeps_retry_state_when_exact_aftermath_memory_is_not_ready(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline-rebuild-memory-barrier.db"))
    _seed(db)
    reset = WorldlineRegenerationService(db)
    preview = reset.preview("novel-1", start_chapter=2, target_chapters=5)
    reset.execute("novel-1", preview_token=preview.token, run_mode="continuous")
    service = WorldlineRebuildService(db, _Aftermath(db, memory_ready=False))

    with pytest.raises(WorldlineRebuildError):
        await service.rebuild("novel-1")

    run = db.fetch_one(
        "SELECT state, canonical_sync_status, next_action FROM novel_generation_runs "
        "WHERE novel_id = 'novel-1'"
    )
    assert tuple(
        run[field] for field in ("state", "canonical_sync_status", "next_action")
    ) == ("paused", "failed", "retry_worldline_rebuild")
    assert db.fetch_one(
        "SELECT memory_status FROM chapter_narrative_commits "
        "WHERE novel_id = 'novel-1' AND chapter_number = 1"
    )["memory_status"] == "not_required"


@pytest.mark.asyncio
async def test_rebuild_with_no_retained_prefix_does_not_require_old_derived_state(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline-rebuild-empty-prefix.db"))
    _seed(db)
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO memory_engine_state (novel_id, state_json, last_updated_chapter) "
        "VALUES ('novel-1', '{}', 2)"
    )
    conn.execute(
        "INSERT INTO character_states "
        "(character_id, novel_id, current_state_summary, last_updated_chapter) "
        "VALUES ('tail-hero', 'novel-1', '尾部状态', 2)"
    )
    conn.commit()
    reset = WorldlineRegenerationService(db)
    preview = reset.preview("novel-1", start_chapter=1, target_chapters=5)
    reset_result = reset.execute("novel-1", preview_token=preview.token, run_mode="continuous")

    result = await WorldlineRebuildService(db, _Aftermath(db)).rebuild("novel-1")

    assert reset_result.retained_through == 0
    assert result["status"] == "completed"
    assert db.fetch_one("SELECT 1 FROM memory_engine_state WHERE novel_id = 'novel-1'") is None
    assert db.fetch_one("SELECT 1 FROM character_states WHERE novel_id = 'novel-1'") is None
    assert db.fetch_one(
        "SELECT COUNT(*) AS total FROM worldline_rebuild_jobs "
        "WHERE novel_id = 'novel-1' AND status = 'completed'"
    )["total"] == 5


@pytest.mark.asyncio
async def test_rebuild_does_not_require_tail_only_character_state(tmp_path):
    db = DatabaseConnection(str(tmp_path / "worldline-rebuild-tail-character.db"))
    _seed(db)
    conn = db.get_connection()
    conn.execute(
        "INSERT INTO character_states "
        "(character_id, novel_id, motivations, current_state_summary, last_updated_chapter) "
        "VALUES ('tail-hero', 'novel-1', ?, '尾章首次出现', 2)",
        (json.dumps([{"description": "尾部动机", "source_chapter": 2}], ensure_ascii=False),),
    )
    conn.commit()
    reset = WorldlineRegenerationService(db)
    preview = reset.preview("novel-1", start_chapter=2, target_chapters=5)
    reset.execute("novel-1", preview_token=preview.token, run_mode="continuous")

    result = await WorldlineRebuildService(db, _Aftermath(db)).rebuild("novel-1")

    assert result["status"] == "completed"
    assert db.fetch_one(
        "SELECT 1 FROM character_states WHERE novel_id = 'novel-1' AND character_id = 'tail-hero'"
    ) is None


@pytest.mark.asyncio
async def test_rebuild_real_aftermath_restores_prefix_state_after_reset(tmp_path, monkeypatch):
    """Reset must not turn a reused claim into a completed rebuild."""

    db = DatabaseConnection(str(tmp_path / "worldline-rebuild-real-aftermath.db"))
    _seed(db, import_legacy=False)
    conn = db.get_connection()
    for number, content in ((1, "第1章关键选择；hero前缀状态"), (2, "第2章关键选择；hero尾部状态")):
        content_hash = hashlib.sha256(content.encode("utf-8")).hexdigest()
        conn.execute(
            "UPDATE chapters SET content = ?, content_sha256 = ?, content_revision = 1 "
            "WHERE novel_id = 'novel-1' AND number = ?",
            (content, content_hash, number),
        )
        candidate_id = f"candidate-{number}"
        conn.execute(
            "INSERT INTO chapter_candidates "
            "(id, novel_id, chapter_number, generation_epoch, status, llm_content) "
            "VALUES (?, 'novel-1', ?, 0, 'committed', ?)",
            (candidate_id, number, content),
        )
        conn.execute(
            "INSERT INTO chapter_candidate_formal_commits "
            "(candidate_id, novel_id, chapter_number, chapter_id, content_sha256, "
            "content_revision, provenance, sync_status) "
            "VALUES (?, 'novel-1', ?, ?, ?, 1, 'candidate_commit', 'ready')",
            (candidate_id, number, f"chapter-{number}", content_hash),
        )
    conn.commit()

    class _Bible:
        def get_by_novel_id(self, _novel_id):
            return SimpleNamespace(
                characters=[
                    SimpleNamespace(
                        character_id=SimpleNamespace(value="hero"),
                        name="hero",
                        description="",
                        relationships=[],
                        public_profile="",
                        hidden_profile="",
                        reveal_chapter=None,
                        mental_state="NORMAL",
                    )
                ],
                timeline_notes=[],
                world_settings=[],
                locations=[],
                style_notes=[],
            )

    class _LLM:
        def __init__(self):
            self.calls = 0

        async def generate(self, _prompt, _config):
            self.calls += 1
            is_tail = self.calls == 2
            chapter = 2 if is_tail else 1
            prefix = "tail" if is_tail else "prefix"
            return SimpleNamespace(
                content=json.dumps(
                    {
                        "completed_beats": [
                            {
                                "beat_id": f"{prefix}-beat",
                                "summary": f"第{chapter}章关键选择",
                                "chapter": chapter,
                                "evidence_text": f"第{chapter}章关键选择",
                            }
                        ],
                        "revealed_clues": [
                            {
                                "clue_id": f"{prefix}-clue",
                                "content": f"第{chapter}章关键选择",
                                "revealed_at_chapter": chapter,
                                "category": "truth",
                                "evidence_text": f"第{chapter}章关键选择",
                            }
                        ],
                        "fact_violations": [],
                    },
                    ensure_ascii=False,
                )
            )

    async def _bundle(_llm, content, chapter_number, **_kwargs):
        return {
            "summary": content,
            "key_events": content,
            "open_threads": "继续处理后果",
            "relation_triples": [],
            "foreshadow_hints": [],
            "consumed_foreshadows": [],
            "storyline_progress": [],
            "dialogues": [],
            "timeline_events": [],
            "causal_edges": [],
            "character_mutations": [
                {
                    "character_name": "hero",
                    "mutation_type": "motivation",
                    "source_event": f"第{chapter_number}章关键选择",
                    "impact_or_description": content.split("；", 1)[-1],
                    "sensitivity_tags_or_priority": 8,
                    "evidence_text": content,
                }
            ],
            "character_states": [],
        }

    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.llm_chapter_extract_bundle",
        _bundle,
    )
    async def _no_bridge(*_args, **_kwargs):
        return None
    monkeypatch.setattr(
        "application.engine.services.chapter_aftermath_pipeline.ChapterAftermathPipeline._extract_chapter_bridge",
        _no_bridge,
    )
    async def _no_auxiliary(*_args, **_kwargs):
        return None
    monkeypatch.setattr(
        "application.engine.services.chapter_aftermath_pipeline.ChapterAftermathPipeline._run_auxiliary_stages",
        _no_auxiliary,
    )
    monkeypatch.setattr(
        "application.engine.services.memory_engine.get_prompt_gateway",
        lambda: SimpleNamespace(
            render=lambda *_args, **_kwargs: SimpleNamespace(prompt="memory prompt")
        ),
    )

    chapter_repository = SqliteChapterRepository(db)
    knowledge = KnowledgeService(SqliteKnowledgeRepository(db))
    memory = MemoryEngine(_LLM(), _Bible(), db)
    pipeline = ChapterAftermathPipeline(
        knowledge_service=knowledge,
        chapter_indexing_service=None,
        llm_service=_LLM(),
        chapter_repository=chapter_repository,
        character_state_repository=SqliteCharacterStateRepository(db),
        bible_repository=_Bible(),
        memory_engine=memory,
    )
    for number in (1, 2):
        chapter = chapter_repository.get_by_novel_and_number(NovelId("novel-1"), number)
        result = await pipeline.run_after_chapter_saved(
            "novel-1",
            number,
            chapter.content,
            expected_content_sha256=chapter.content_sha256,
            expected_content_revision=chapter.content_revision,
        )
        assert result["narrative_sync_ok"] is True, result
        assert result["memory_engine_ok"] is True, result

    assert db.fetch_one(
        "SELECT last_updated_chapter FROM character_states WHERE novel_id = 'novel-1'"
    )["last_updated_chapter"] == 2
    assert db.fetch_one(
        "SELECT last_updated_chapter FROM memory_engine_state WHERE novel_id = 'novel-1'"
    )["last_updated_chapter"] == 2
    memory_before_reset = json.loads(
        db.fetch_one(
            "SELECT state_json FROM memory_engine_state WHERE novel_id = 'novel-1'"
        )["state_json"]
    )
    assert {item["beat_id"] for item in memory_before_reset["completed_beats"]} == {
        "prefix-beat",
        "tail-beat",
    }

    reset = WorldlineRegenerationService(db)
    preview = reset.preview("novel-1", start_chapter=2, target_chapters=5)
    reset_result = reset.execute("novel-1", preview_token=preview.token, run_mode="continuous")
    assert reset_result.retained_through == 1
    assert db.fetch_one("SELECT 1 FROM character_states WHERE novel_id = 'novel-1'") is None
    assert db.fetch_one("SELECT 1 FROM memory_engine_state WHERE novel_id = 'novel-1'") is None

    claim_dispositions = []
    original_claim = SqliteChapterNarrativeCommitRepository.claim

    def _record_claim(self, **kwargs):
        result = original_claim(self, **kwargs)
        if kwargs["chapter_number"] == 1:
            claim_dispositions.append(result.disposition)
        return result

    monkeypatch.setattr(SqliteChapterNarrativeCommitRepository, "claim", _record_claim)
    result = await WorldlineRebuildService(db, pipeline).rebuild("novel-1")

    state_row = db.fetch_one(
        "SELECT current_state_summary, last_updated_chapter FROM character_states "
        "WHERE novel_id = 'novel-1'"
    )
    memory_row = db.fetch_one(
        "SELECT state_json, last_updated_chapter FROM memory_engine_state "
        "WHERE novel_id = 'novel-1'"
    )
    assert "claimed" in claim_dispositions
    assert state_row is not None
    assert state_row["last_updated_chapter"] == 1
    assert memory_row is not None
    assert memory_row["last_updated_chapter"] == 1
    memory_after_rebuild = json.loads(memory_row["state_json"])
    assert [item["beat_id"] for item in memory_after_rebuild["completed_beats"]] == [
        "prefix-beat"
    ]
    summary_row = db.fetch_one(
        "SELECT source_content_sha256, source_content_revision, sync_status "
        "FROM chapter_summaries JOIN knowledge ON knowledge.id = chapter_summaries.knowledge_id "
        "WHERE knowledge.novel_id = 'novel-1' AND chapter_number = 1"
    )
    chapter_one = db.fetch_one(
        "SELECT content_sha256, content_revision FROM chapters "
        "WHERE novel_id = 'novel-1' AND number = 1"
    )
    assert summary_row is not None
    assert summary_row["source_content_sha256"] == chapter_one["content_sha256"]
    assert summary_row["source_content_revision"] == chapter_one["content_revision"] == 1
    assert summary_row["sync_status"] == "committed"
    assert result["status"] == "completed"

    conn.execute(
        "UPDATE novel_generation_runs SET canonical_sync_status = 'failed', state = 'paused' "
        "WHERE novel_id = 'novel-1'"
    )
    conn.execute(
        "UPDATE worldline_rebuild_jobs SET status = 'failed', failure_reason = 'retry' "
        "WHERE novel_id = 'novel-1' AND generation_epoch = 1"
    )
    conn.commit()
    retry_result = await WorldlineRebuildService(db, pipeline).rebuild("novel-1")
    assert retry_result["status"] == "completed"
    assert db.fetch_one(
        "SELECT COUNT(*) AS total FROM character_states WHERE novel_id = 'novel-1' AND character_id = 'hero'"
    )["total"] == 1
    assert db.fetch_one(
        "SELECT COUNT(*) AS total FROM memory_engine_state WHERE novel_id = 'novel-1'"
    )["total"] == 1
    retry_memory = json.loads(
        db.fetch_one(
            "SELECT state_json FROM memory_engine_state WHERE novel_id = 'novel-1'"
        )["state_json"]
    )
    assert [item["beat_id"] for item in retry_memory["completed_beats"]].count("prefix-beat") == 1

    assert db.fetch_one(
        "SELECT COUNT(*) AS total FROM worldline_archive_entries "
        "WHERE archive_id = ? AND source_table = 'chapter_candidate_formal_commits'",
        (reset_result.archive_id,),
    )["total"] == 1
    restored = reset.restore(
        "novel-1",
        archive_id=reset_result.archive_id,
        run_mode="chapter_review",
    )
    assert restored.generation_epoch == 2
    assert db.fetch_one(
        "SELECT COUNT(*) AS total FROM chapter_candidate_formal_commits "
        "WHERE novel_id = 'novel-1' AND chapter_number = 2"
    )["total"] == 1

    memory.llm_service.calls = 0
    restored_result = await WorldlineRebuildService(db, pipeline).rebuild("novel-1")

    assert restored_result["status"] == "completed"
    assert db.fetch_one(
        "SELECT last_updated_chapter FROM memory_engine_state WHERE novel_id = 'novel-1'"
    )["last_updated_chapter"] == 2
    restored_run = db.fetch_one(
        "SELECT state, canonical_sync_status, next_action FROM novel_generation_runs "
        "WHERE novel_id = 'novel-1'"
    )
    assert (
        restored_run["state"],
        restored_run["canonical_sync_status"],
        restored_run["next_action"],
    ) == ("paused", "ready", "select_run_mode")
