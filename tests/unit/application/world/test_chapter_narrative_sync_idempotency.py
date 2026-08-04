import hashlib
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from domain.knowledge.chapter_summary import canonical_summary_payload_sha256
from domain.novel.entities.foreshadowing_registry import ForeshadowingRegistry
from domain.novel.entities.chapter import Chapter
from domain.novel.value_objects.novel_id import NovelId

from application.world.services.chapter_narrative_sync import (
    persist_bundle_triples_and_foreshadows,
    persist_causal_edges,
    persist_character_end_states,
    persist_character_mutations,
    sync_chapter_narrative_after_save,
    update_narrative_debts,
)
from application.world.services.knowledge_service import KnowledgeService
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_chapter_repository import (
    SqliteChapterRepository,
)
from infrastructure.persistence.database.sqlite_chapter_narrative_commit_repository import (
    SqliteChapterNarrativeCommitRepository,
)
from infrastructure.persistence.database.sqlite_knowledge_repository import (
    SqliteKnowledgeRepository,
)
from infrastructure.persistence.database.sqlite_narrative_event_repository import (
    SqliteNarrativeEventRepository,
)


class _KnowledgeRepo:
    def __init__(self):
        self.rows = {}

    def save_triple(self, novel_id, row):
        self.rows[row["id"]] = dict(row, novel_id=novel_id)


class _TripleRepo:
    def __init__(self):
        self._kr = _KnowledgeRepo()


class _ForeshadowRepo:
    def __init__(self):
        self.registry = None

    def get_by_novel_id(self, novel_id):
        if self.registry is None:
            self.registry = ForeshadowingRegistry(id=f"fr-{novel_id.value}", novel_id=novel_id)
        return self.registry

    def save(self, registry):
        self.registry = registry


class _CausalRepo:
    def __init__(self):
        self.edges = {}

    def save(self, edge):
        self.edges[edge.id] = edge

    def get_unresolved(self, novel_id):
        return [edge for edge in self.edges.values() if edge.novel_id == novel_id and not edge.is_resolved]

    def get_by_novel(self, novel_id):
        return [edge for edge in self.edges.values() if edge.novel_id == novel_id]

    def resolve(self, edge_id, chapter):
        self.edges[edge_id] = self.edges[edge_id].resolve(chapter)


class _CharacterStateRepo:
    def __init__(self):
        self.states = {}

    def get(self, character_id, novel_id):
        return self.states.get((novel_id, character_id))

    def save(self, state):
        self.states[(state.novel_id, state.character_id)] = state


class _DebtRepo:
    def __init__(self):
        self.debts = {}

    def save(self, debt):
        self.debts[debt.id] = debt

    def get_by_type(self, novel_id, debt_type):
        return [
            debt
            for debt in self.debts.values()
            if debt.novel_id == novel_id and debt.debt_type == debt_type and not debt.resolved_chapter
        ]

    def resolve(self, debt_id, chapter):
        self.debts[debt_id] = self.debts[debt_id].resolve(chapter)

    def mark_overdue_batch(self, novel_id, chapter_number):
        return 0


def _canonical_bundle(summary="章末摘要"):
    return {
        "summary": summary,
        "key_events": "关键事件",
        "open_threads": "未解线索",
        "relation_triples": [],
        "foreshadow_hints": [],
        "consumed_foreshadows": [],
        "storyline_progress": [],
        "dialogues": [],
        "timeline_events": [],
        "causal_edges": [],
        "character_mutations": [],
        "character_states": [],
    }


def _canonical_services(tmp_path, content="第一版正文"):
    db = DatabaseConnection(str(tmp_path / "canonical.db"))
    db.execute("INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'Novel', 'novel-1')")
    chapter_repo = SqliteChapterRepository(db)
    chapter = Chapter(
        id="chapter-1",
        novel_id=NovelId("novel-1"),
        number=1,
        title="Chapter",
        content=content,
    )
    chapter_repo.save(chapter)
    knowledge = KnowledgeService(SqliteKnowledgeRepository(db))
    return db, chapter_repo, chapter, knowledge


def test_chapter_narrative_artifacts_are_idempotent_across_audit_retries():
    bundle = {
        "relation_triples": [{"data": {"subject": "林澈", "predicate": "持有", "object": "铜铃"}, "status": "pending"}],
        "foreshadow_hints": [{"data": {"description": "铜铃将在雨夜响起", "suggested_resolve_offset": 3, "importance": "high"}}],
        "causal_edges": [{"data": {"source_event": "铜铃丢失", "target_event": "林澈追查真相", "causal_type": "motivates", "strength": 0.8}}],
        "character_mutations": [{"data": {"character_name": "林澈", "mutation_type": "motivation", "source_event": "铜铃丢失", "impact_or_description": "追查真相", "sensitivity_tags_or_priority": 8}}],
        "character_states": [{"character_name": "林澈", "mental_state": "警觉"}],
    }
    triple_repo = _TripleRepo()
    foreshadow_repo = _ForeshadowRepo()
    causal_repo = _CausalRepo()
    state_repo = _CharacterStateRepo()
    debt_repo = _DebtRepo()

    for _ in range(2):
        persist_bundle_triples_and_foreshadows("novel-1", 4, bundle, triple_repo, foreshadow_repo)
        persist_causal_edges("novel-1", 4, bundle, causal_repo)
        persist_character_mutations("novel-1", 4, bundle, state_repo)
        persist_character_end_states("novel-1", 4, bundle, state_repo)
        update_narrative_debts("novel-1", 4, bundle, debt_repo, causal_repo)

    assert len(triple_repo._kr.rows) == 1
    assert len(foreshadow_repo.registry.foreshadowings) == 1
    assert len(causal_repo.edges) == 1
    assert len(debt_repo.debts) == 2
    state = state_repo.states[("novel-1", "林澈")]
    assert len(state.motivations) == 1
    assert len(state.emotional_arc) == 1


def test_narrative_event_upsert_uses_stable_event_id(tmp_path):
    db = DatabaseConnection(str(tmp_path / "events.db"))
    db.execute("INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'Novel', 'novel-1')")
    repo = SqliteNarrativeEventRepository(db)

    for _ in range(2):
        repo.upsert_event(
            event_id="event-stable",
            novel_id="novel-1",
            chapter_number=2,
            event_summary="林澈: 铜铃响了",
            mutations=[],
            tags=["对话:林澈"],
        )

    rows = db.fetch_all("SELECT * FROM narrative_events WHERE novel_id = ?", ("novel-1",))
    assert len(rows) == 1
    assert rows[0]["event_id"] == "event-stable"


@pytest.mark.asyncio
async def test_canonical_sync_reuses_same_version_and_claims_changed_content(
    tmp_path, monkeypatch
):
    db, chapter_repo, chapter, knowledge = _canonical_services(tmp_path)
    extract = AsyncMock(return_value=_canonical_bundle())
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.llm_chapter_extract_bundle",
        extract,
    )

    first = await sync_chapter_narrative_after_save(
        "novel-1", 1, chapter.content, knowledge, None, SimpleNamespace(),
        chapter_repository=chapter_repo,
    )
    reused = await sync_chapter_narrative_after_save(
        "novel-1", 1, chapter.content, knowledge, None, SimpleNamespace(),
        chapter_repository=chapter_repo,
    )

    chapter.update_content("第二版正文")
    chapter_repo.save(chapter)
    changed = await sync_chapter_narrative_after_save(
        "novel-1", 1, chapter.content, knowledge, None, SimpleNamespace(),
        chapter_repository=chapter_repo,
    )

    assert first.get("commit_status") == "committed"
    assert first.get("narrative_sync_ok") is True
    assert reused.get("commit_status") == "reused"
    assert changed.get("commit_status") == "committed"
    assert extract.await_count == 2

    claims = db.fetch_all(
        "SELECT content_sha256, content_revision, status, attempt_count "
        "FROM chapter_narrative_commits ORDER BY content_revision"
    )
    assert [dict(row) for row in claims] == [
        {
            "content_sha256": hashlib.sha256("第一版正文".encode("utf-8")).hexdigest(),
            "content_revision": 1,
            "status": "committed",
            "attempt_count": 1,
        },
        {
            "content_sha256": hashlib.sha256("第二版正文".encode("utf-8")).hexdigest(),
            "content_revision": 2,
            "status": "committed",
            "attempt_count": 1,
        },
    ]
    summary = db.fetch_one(
        "SELECT summary, source_content_sha256, pipeline_version, sync_status, "
        "sync_error, sync_attempts FROM chapter_summaries"
    )
    assert summary["summary"] == "章末摘要"
    assert summary["source_content_sha256"] == claims[-1]["content_sha256"]
    assert summary["pipeline_version"] == changed.get("pipeline_version")
    assert summary["sync_status"] == "committed"
    assert summary["sync_error"] == ""
    assert summary["sync_attempts"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("extract_result", "extract_error", "expected_reason"),
    [
        (None, RuntimeError("provider unavailable"), "provider unavailable"),
        (_canonical_bundle(summary=""), None, "empty_summary"),
        (
            {**_canonical_bundle(), "relation_triples": {}},
            None,
            "invalid_structure:relation_triples",
        ),
    ],
)
async def test_canonical_sync_never_commits_llm_failure_or_empty_summary(
    tmp_path, monkeypatch, extract_result, extract_error, expected_reason
):
    db, chapter_repo, chapter, knowledge = _canonical_services(tmp_path)
    extract = AsyncMock(
        return_value=extract_result,
        side_effect=extract_error,
    )
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.llm_chapter_extract_bundle",
        extract,
    )

    result = await sync_chapter_narrative_after_save(
        "novel-1", 1, chapter.content, knowledge, None, SimpleNamespace(),
        chapter_repository=chapter_repo,
    )

    assert result.get("commit_status") == "failed"
    assert result.get("narrative_sync_ok") is False
    assert expected_reason in result.get("failure_reason")
    claim = db.fetch_one(
        "SELECT status, failure_reason FROM chapter_narrative_commits"
    )
    assert claim["status"] == "failed"
    assert expected_reason in claim["failure_reason"]
    assert db.fetch_all(
        "SELECT * FROM chapter_summaries WHERE sync_status = 'committed'"
    ) == []


@pytest.mark.asyncio
async def test_vector_runs_after_commit_with_source_provenance(tmp_path, monkeypatch):
    db, chapter_repo, chapter, knowledge = _canonical_services(tmp_path)
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.llm_chapter_extract_bundle",
        AsyncMock(return_value=_canonical_bundle()),
    )
    captured = {}

    class IndexingService:
        async def ensure_collection(self, novel_id):
            assert novel_id == "novel-1"

        async def index_chapter_summary(self, novel_id, chapter_number, text, **provenance):
            claim = db.fetch_one(
                "SELECT status FROM chapter_narrative_commits WHERE novel_id = ? "
                "AND chapter_number = ?",
                (novel_id, chapter_number),
            )
            assert claim["status"] == "committed"
            captured.update(provenance)

    result = await sync_chapter_narrative_after_save(
        "novel-1", 1, chapter.content, knowledge, IndexingService(), SimpleNamespace(),
        chapter_repository=chapter_repo,
    )

    expected_hash = hashlib.sha256(chapter.content.encode("utf-8")).hexdigest()
    assert result.get("commit_status") == "committed"
    assert result.get("vector_status") == "stored"
    assert captured == {
        "content_sha256": expected_hash,
        "content_revision": 1,
        "pipeline_version": result.get("pipeline_version"),
    }
    claim = db.fetch_one(
        "SELECT status, vector_status FROM chapter_narrative_commits"
    )
    assert dict(claim) == {"status": "committed", "vector_status": "stored"}


@pytest.mark.asyncio
async def test_canonical_summary_write_failure_is_persisted_and_returned(
    tmp_path, monkeypatch
):
    db, chapter_repo, chapter, knowledge = _canonical_services(tmp_path)
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.llm_chapter_extract_bundle",
        AsyncMock(return_value=_canonical_bundle()),
    )

    class FailingKnowledgeService:
        knowledge_repository = knowledge.knowledge_repository

        def get_knowledge(self, novel_id):
            return knowledge.get_knowledge(novel_id)

        def upsert_chapter_summary(self, **kwargs):
            raise RuntimeError("summary writer rejected batch")

    result = await sync_chapter_narrative_after_save(
        "novel-1",
        1,
        chapter.content,
        FailingKnowledgeService(),
        None,
        SimpleNamespace(),
        chapter_repository=chapter_repo,
    )

    assert result.get("commit_status") == "failed"
    assert "summary writer rejected batch" in result.get("failure_reason")
    claim = db.fetch_one(
        "SELECT status, failure_reason FROM chapter_narrative_commits"
    )
    assert claim["status"] == "failed"
    assert "summary writer rejected batch" in claim["failure_reason"]


@pytest.mark.asyncio
async def test_critical_structured_write_failure_prevents_commit(tmp_path, monkeypatch):
    db, chapter_repo, chapter, knowledge = _canonical_services(tmp_path)
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.llm_chapter_extract_bundle",
        AsyncMock(return_value=_canonical_bundle()),
    )

    def fail_structured_write(*args, **kwargs):
        raise RuntimeError("storyline write failed")

    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.persist_bundle_extras",
        fail_structured_write,
    )

    result = await sync_chapter_narrative_after_save(
        "novel-1",
        1,
        chapter.content,
        knowledge,
        None,
        SimpleNamespace(),
        chapter_repository=chapter_repo,
        storyline_repository=object(),
    )

    assert result.get("commit_status") == "failed"
    assert "storyline write failed" in result.get("failure_reason")
    claim = db.fetch_one("SELECT status FROM chapter_narrative_commits")
    assert claim["status"] == "failed"
    summary = db.fetch_one(
        "SELECT sync_status, sync_error, source_content_sha256 FROM chapter_summaries"
    )
    assert summary["sync_status"] == "failed"
    assert "storyline write failed" in summary["sync_error"]
    assert summary["source_content_sha256"] == result.get("content_sha256")


@pytest.mark.asyncio
async def test_actual_triple_repository_failure_prevents_commit(tmp_path, monkeypatch):
    db, chapter_repo, chapter, knowledge = _canonical_services(tmp_path)
    bundle = _canonical_bundle()
    bundle["relation_triples"] = [
        {"data": {"subject": "林澈", "predicate": "持有", "object": "铜铃"}}
    ]
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.llm_chapter_extract_bundle",
        AsyncMock(return_value=bundle),
    )

    class FailingTripleWriter:
        def save_triple(self, novel_id, row):
            raise RuntimeError("triple disk write failed")

    result = await sync_chapter_narrative_after_save(
        "novel-1",
        1,
        chapter.content,
        knowledge,
        None,
        SimpleNamespace(),
        chapter_repository=chapter_repo,
        triple_repository=SimpleNamespace(_kr=FailingTripleWriter()),
    )

    assert result.get("commit_status") == "failed"
    assert "triple disk write failed" in result.get("failure_reason")
    assert db.fetch_one("SELECT status FROM chapter_narrative_commits")["status"] == "failed"


@pytest.mark.asyncio
async def test_concurrent_same_version_returns_in_progress_without_duplicate_llm(
    tmp_path, monkeypatch
):
    _db, chapter_repo, chapter, knowledge = _canonical_services(tmp_path)
    started = asyncio.Event()
    release = asyncio.Event()

    async def blocked_extract(*args, **kwargs):
        started.set()
        await release.wait()
        return _canonical_bundle()

    extract = AsyncMock(side_effect=blocked_extract)
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.llm_chapter_extract_bundle",
        extract,
    )

    first_task = asyncio.create_task(
        sync_chapter_narrative_after_save(
            "novel-1", 1, chapter.content, knowledge, None, SimpleNamespace(),
            chapter_repository=chapter_repo,
        )
    )
    await started.wait()
    concurrent = await sync_chapter_narrative_after_save(
        "novel-1", 1, chapter.content, knowledge, None, SimpleNamespace(),
        chapter_repository=chapter_repo,
    )
    release.set()
    first = await first_task

    assert concurrent.get("commit_status") == "in_progress"
    assert concurrent.get("narrative_sync_ok") is False
    assert first.get("commit_status") == "committed"
    assert extract.await_count == 1


@pytest.mark.asyncio
async def test_source_hash_mismatch_fails_before_llm(tmp_path, monkeypatch):
    db, chapter_repo, _chapter, knowledge = _canonical_services(tmp_path)
    extract = AsyncMock(return_value=_canonical_bundle())
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.llm_chapter_extract_bundle",
        extract,
    )

    result = await sync_chapter_narrative_after_save(
        "novel-1", 1, "未保存的正文", knowledge, None, SimpleNamespace(),
        chapter_repository=chapter_repo,
    )

    assert result.get("commit_status") == "failed"
    assert result.get("failure_reason") == "source_hash_mismatch"
    extract.assert_not_awaited()
    assert db.fetch_all("SELECT * FROM chapter_narrative_commits") == []


@pytest.mark.asyncio
async def test_vector_failure_does_not_downgrade_canonical_commit(tmp_path, monkeypatch):
    db, chapter_repo, chapter, knowledge = _canonical_services(tmp_path)
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.llm_chapter_extract_bundle",
        AsyncMock(return_value=_canonical_bundle()),
    )

    class FailingIndexer:
        async def ensure_collection(self, novel_id):
            return None

        async def index_chapter_summary(self, *args, **kwargs):
            raise RuntimeError("vector offline")

    result = await sync_chapter_narrative_after_save(
        "novel-1", 1, chapter.content, knowledge, FailingIndexer(), SimpleNamespace(),
        chapter_repository=chapter_repo,
    )

    assert result.get("commit_status") == "committed"
    assert result.get("narrative_sync_ok") is True
    assert result.get("vector_status") == "failed"
    claim = db.fetch_one(
        "SELECT status, vector_status FROM chapter_narrative_commits"
    )
    assert dict(claim) == {"status": "committed", "vector_status": "failed"}


@pytest.mark.asyncio
async def test_later_chapter_commit_preserves_prior_summary_provenance(
    tmp_path, monkeypatch
):
    db, chapter_repo, chapter_one, knowledge = _canonical_services(tmp_path)
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.llm_chapter_extract_bundle",
        AsyncMock(return_value=_canonical_bundle()),
    )
    first = await sync_chapter_narrative_after_save(
        "novel-1", 1, chapter_one.content, knowledge, None, SimpleNamespace(),
        chapter_repository=chapter_repo,
    )
    chapter_two = Chapter(
        id="chapter-2",
        novel_id=NovelId("novel-1"),
        number=2,
        title="Chapter 2",
        content="第二章正文",
    )
    chapter_repo.save(chapter_two)
    second = await sync_chapter_narrative_after_save(
        "novel-1", 2, chapter_two.content, knowledge, None, SimpleNamespace(),
        chapter_repository=chapter_repo,
    )

    summaries = db.fetch_all(
        "SELECT chapter_number, source_content_sha256, pipeline_version, sync_status, "
        "sync_attempts FROM chapter_summaries ORDER BY chapter_number"
    )
    assert [dict(row) for row in summaries] == [
        {
            "chapter_number": 1,
            "source_content_sha256": first.get("content_sha256"),
            "pipeline_version": first.get("pipeline_version"),
            "sync_status": "committed",
            "sync_attempts": 1,
        },
        {
            "chapter_number": 2,
            "source_content_sha256": second.get("content_sha256"),
            "pipeline_version": second.get("pipeline_version"),
            "sync_status": "committed",
            "sync_attempts": 1,
        },
    ]


@pytest.mark.asyncio
async def test_canonical_commit_rejects_content_changed_during_extraction(
    tmp_path, monkeypatch
):
    db, chapter_repo, chapter, knowledge = _canonical_services(tmp_path)

    async def extract_after_out_of_band_content_change(*args, **kwargs):
        db.execute(
            "UPDATE chapters SET content = ? WHERE novel_id = ? AND number = ?",
            ("并发覆盖后的正文", "novel-1", 1),
        )
        db.commit()
        return _canonical_bundle()

    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.llm_chapter_extract_bundle",
        AsyncMock(side_effect=extract_after_out_of_band_content_change),
    )

    result = await sync_chapter_narrative_after_save(
        "novel-1", 1, chapter.content, knowledge, None, SimpleNamespace(),
        chapter_repository=chapter_repo,
    )

    assert result.get("commit_status") == "failed"
    assert result.get("failure_reason") == "source_hash_mismatch"
    assert db.fetch_one("SELECT status FROM chapter_narrative_commits")["status"] == "failed"


def test_canonical_commit_rejects_replaced_or_invalid_summary_row(tmp_path):
    db, _chapter_repo, chapter, knowledge = _canonical_services(tmp_path)
    repository = SqliteChapterNarrativeCommitRepository(db)
    content_sha256 = hashlib.sha256(chapter.content.encode("utf-8")).hexdigest()
    claim = repository.claim(
        novel_id="novel-1",
        chapter_number=1,
        content_sha256=content_sha256,
        pipeline_version="chapter-narrative-sync/v1",
    )
    knowledge.upsert_chapter_summary(
        novel_id="novel-1",
        chapter_id=1,
        summary="准备提交的规范摘要",
        key_events="事件",
        open_threads="线索",
        sync_status="in_progress",
    )
    repository.prepare_summary(
        novel_id="novel-1",
        chapter_number=1,
        content_sha256=content_sha256,
        pipeline_version="chapter-narrative-sync/v1",
        attempt_count=claim.attempt_count,
    )
    db.execute(
        "UPDATE chapter_summaries SET summary = '', source_content_sha256 = '', "
        "pipeline_version = 'replaced', sync_status = 'draft' WHERE chapter_number = 1"
    )
    db.commit()

    with pytest.raises(RuntimeError, match="canonical_summary_write_missing"):
        repository.commit(
            novel_id="novel-1",
            chapter_number=1,
            content_sha256=content_sha256,
            pipeline_version="chapter-narrative-sync/v1",
            attempt_count=claim.attempt_count,
            content_revision=claim.content_revision,
        )


def test_canonical_commit_rejects_nonempty_replaced_summary_payload(tmp_path):
    db, _chapter_repo, chapter, knowledge = _canonical_services(tmp_path)
    repository = SqliteChapterNarrativeCommitRepository(db)
    content_sha256 = hashlib.sha256(chapter.content.encode("utf-8")).hexdigest()
    claim = repository.claim(
        novel_id="novel-1",
        chapter_number=1,
        content_sha256=content_sha256,
        pipeline_version="chapter-narrative-sync/v1",
    )
    payload_sha256 = canonical_summary_payload_sha256(
        summary="准备提交的规范摘要",
        key_events="准备提交的事件",
        open_threads="准备提交的线索",
    )
    knowledge.upsert_chapter_summary(
        novel_id="novel-1",
        chapter_id=1,
        summary="准备提交的规范摘要",
        key_events="准备提交的事件",
        open_threads="准备提交的线索",
        sync_status="in_progress",
    )
    repository.prepare_summary(
        novel_id="novel-1",
        chapter_number=1,
        content_sha256=content_sha256,
        pipeline_version="chapter-narrative-sync/v1",
        attempt_count=claim.attempt_count,
        canonical_payload_sha256=payload_sha256,
    )

    # A whole-knowledge save can retain the provenance/status fields while
    # replacing the nonempty canonical narrative payload.
    db.execute(
        "UPDATE chapter_summaries SET summary = ?, key_events = ?, open_threads = ? "
        "WHERE chapter_number = 1",
        ("并发替换后的摘要", "并发替换后的事件", "并发替换后的线索"),
    )
    db.commit()

    with pytest.raises(RuntimeError, match="canonical_summary_write_missing"):
        repository.commit(
            novel_id="novel-1",
            chapter_number=1,
            content_sha256=content_sha256,
            pipeline_version="chapter-narrative-sync/v1",
            attempt_count=claim.attempt_count,
            content_revision=claim.content_revision,
            canonical_payload_sha256=payload_sha256,
        )


def test_canonical_commit_rejects_whole_knowledge_save_replacement(tmp_path):
    db, _chapter_repo, chapter, knowledge = _canonical_services(tmp_path)
    repository = SqliteChapterNarrativeCommitRepository(db)
    content_sha256 = hashlib.sha256(chapter.content.encode("utf-8")).hexdigest()
    claim = repository.claim(
        novel_id="novel-1",
        chapter_number=1,
        content_sha256=content_sha256,
        pipeline_version="chapter-narrative-sync/v1",
    )
    payload_sha256 = canonical_summary_payload_sha256(
        summary="准备提交的规范摘要",
        key_events="准备提交的事件",
        open_threads="准备提交的线索",
    )
    knowledge.upsert_chapter_summary(
        novel_id="novel-1",
        chapter_id=1,
        summary="准备提交的规范摘要",
        key_events="准备提交的事件",
        open_threads="准备提交的线索",
        sync_status="in_progress",
    )
    repository.prepare_summary(
        novel_id="novel-1",
        chapter_number=1,
        content_sha256=content_sha256,
        pipeline_version="chapter-narrative-sync/v1",
        attempt_count=claim.attempt_count,
        canonical_payload_sha256=payload_sha256,
    )

    replacement = knowledge.get_knowledge("novel-1")
    replacement_summary = replacement.get_chapter(1)
    replacement_summary.summary = "全量保存替换后的摘要"
    replacement_summary.key_events = "全量保存替换后的事件"
    replacement_summary.open_threads = "全量保存替换后的线索"
    knowledge.knowledge_repository.save(replacement)

    replaced_row = db.fetch_one(
        "SELECT source_content_sha256, pipeline_version, sync_status, sync_attempts, "
        "canonical_payload_sha256 FROM chapter_summaries WHERE chapter_number = 1"
    )
    assert dict(replaced_row) == {
        "source_content_sha256": content_sha256,
        "pipeline_version": "chapter-narrative-sync/v1",
        "sync_status": "in_progress",
        "sync_attempts": claim.attempt_count,
        "canonical_payload_sha256": canonical_summary_payload_sha256(
            summary="全量保存替换后的摘要",
            key_events="全量保存替换后的事件",
            open_threads="全量保存替换后的线索",
        ),
    }

    with pytest.raises(RuntimeError, match="canonical_summary_write_missing"):
        repository.commit(
            novel_id="novel-1",
            chapter_number=1,
            content_sha256=content_sha256,
            pipeline_version="chapter-narrative-sync/v1",
            attempt_count=claim.attempt_count,
            content_revision=claim.content_revision,
            canonical_payload_sha256=payload_sha256,
        )


@pytest.mark.asyncio
async def test_vector_status_write_failure_does_not_downgrade_canonical_commit(
    tmp_path, monkeypatch
):
    db, chapter_repo, chapter, knowledge = _canonical_services(tmp_path)
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.llm_chapter_extract_bundle",
        AsyncMock(return_value=_canonical_bundle()),
    )

    class FailingIndexer:
        async def ensure_collection(self, novel_id):
            return None

        async def index_chapter_summary(self, *args, **kwargs):
            raise RuntimeError("vector offline")

    def fail_vector_status_write(self, **kwargs):
        raise RuntimeError("vector status unavailable")

    monkeypatch.setattr(
        SqliteChapterNarrativeCommitRepository,
        "set_vector_status",
        fail_vector_status_write,
    )

    result = await sync_chapter_narrative_after_save(
        "novel-1", 1, chapter.content, knowledge, FailingIndexer(), SimpleNamespace(),
        chapter_repository=chapter_repo,
    )

    assert result.get("commit_status") == "committed"
    assert result.get("narrative_sync_ok") is True
    assert result.get("vector_status") == "failed"
    assert db.fetch_one("SELECT status FROM chapter_narrative_commits")["status"] == "committed"


@pytest.mark.asyncio
async def test_critical_tension_write_failure_prevents_canonical_commit(
    tmp_path, monkeypatch
):
    db, chapter_repo, chapter, knowledge = _canonical_services(tmp_path)
    bundle = _canonical_bundle()
    bundle["tension_score"] = 72.0
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.llm_chapter_extract_bundle",
        AsyncMock(return_value=bundle),
    )
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync._write_tension_ephemeral",
        lambda *args, **kwargs: False,
    )

    result = await sync_chapter_narrative_after_save(
        "novel-1", 1, chapter.content, knowledge, None, SimpleNamespace(),
        chapter_repository=chapter_repo,
    )

    assert result.get("commit_status") == "failed"
    assert result.get("failure_reason") == "critical_bundle_write_failed"
    assert db.fetch_one("SELECT status FROM chapter_narrative_commits")["status"] == "failed"


@pytest.mark.asyncio
async def test_canonical_summary_is_durable_when_dispatch_only_accepts_the_batch(
    tmp_path, monkeypatch
):
    db, chapter_repo, chapter, knowledge = _canonical_services(tmp_path)
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.llm_chapter_extract_bundle",
        AsyncMock(return_value=_canonical_bundle()),
    )
    monkeypatch.delenv("PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES", raising=False)
    monkeypatch.setattr(
        "infrastructure.persistence.database.write_dispatch.enqueue_txn_batch",
        lambda operations: True,
    )

    result = await sync_chapter_narrative_after_save(
        "novel-1", 1, chapter.content, knowledge, None, SimpleNamespace(),
        chapter_repository=chapter_repo,
    )

    assert result.get("commit_status") == "committed"
    summary = db.fetch_one(
        "SELECT sync_status, source_content_sha256 FROM chapter_summaries WHERE chapter_number = 1"
    )
    assert dict(summary) == {
        "sync_status": "committed",
        "source_content_sha256": result.get("content_sha256"),
    }


@pytest.mark.asyncio
async def test_canonical_dialogue_event_is_durable_when_dispatch_only_accepts_batch(
    tmp_path, monkeypatch
):
    db, chapter_repo, chapter, knowledge = _canonical_services(tmp_path)
    bundle = _canonical_bundle()
    bundle["dialogues"] = [
        {"speaker": "林澈", "content": "城门会在子时关闭。", "context": "城门"}
    ]
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.llm_chapter_extract_bundle",
        AsyncMock(return_value=bundle),
    )
    monkeypatch.delenv("PLOTPILOT_ALLOW_DIRECT_SQLITE_WRITES", raising=False)
    monkeypatch.setattr(
        "infrastructure.persistence.database.write_dispatch.enqueue_execute_sql",
        lambda sql, params: True,
    )
    monkeypatch.setattr(
        "infrastructure.persistence.database.write_dispatch.enqueue_txn_batch",
        lambda operations: True,
    )

    result = await sync_chapter_narrative_after_save(
        "novel-1", 1, chapter.content, knowledge, None, SimpleNamespace(),
        chapter_repository=chapter_repo,
        narrative_event_repository=SqliteNarrativeEventRepository(db),
    )

    assert result.get("commit_status") == "committed"
    assert db.fetch_one(
        "SELECT event_summary FROM narrative_events WHERE novel_id = ? AND chapter_number = ?",
        ("novel-1", 1),
    )["event_summary"] == "林澈: 城门会在子时关闭。"
