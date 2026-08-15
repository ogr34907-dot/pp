import hashlib
import asyncio
import threading
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from domain.knowledge.chapter_summary import canonical_summary_payload_sha256
from domain.novel.entities.foreshadowing_registry import ForeshadowingRegistry
from domain.novel.entities.chapter import Chapter
from domain.novel.value_objects.novel_id import NovelId

from application.world.services.chapter_narrative_sync import (
    _try_resolve_causal_edges,
    persist_bundle_extras,
    persist_bundle_triples_and_foreshadows,
    persist_causal_edges,
    persist_character_end_states,
    persist_character_mutations,
    sync_chapter_narrative_after_save,
    update_narrative_debts,
)
from application.world.services.knowledge_service import KnowledgeService
from application.core.services.chapter_rewrite_coordinator import ChapterRewriteCoordinator
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


class _StorylineRepo:
    def __init__(self):
        self.storylines = []

    def get_by_novel_id(self, _novel_id):
        return list(self.storylines)

    def save(self, storyline):
        if storyline not in self.storylines:
            self.storylines.append(storyline)


class _NarrativeEventRepo:
    def __init__(self):
        self.events = {}

    def upsert_event(self, **event):
        self.events[event["event_id"]] = event


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


def _rewrite(chapter_repo, chapter, content):
    return ChapterRewriteCoordinator(
        db=chapter_repo.db,
        chapter_repository=chapter_repo,
    ).rewrite(chapter, content, rewrite_mode="safe_snapshot").chapter


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


def test_terminal_failure_can_only_be_reclaimed_for_current_content_version(tmp_path):
    db, _chapter_repo, chapter, _knowledge = _canonical_services(tmp_path)
    content_sha256 = hashlib.sha256(chapter.content.encode("utf-8")).hexdigest()
    db.execute(
        "INSERT INTO chapter_narrative_commits "
        "(novel_id, chapter_number, content_sha256, pipeline_version, "
        "content_revision, status, failure_reason, attempt_count) "
        "VALUES (?, ?, ?, 'chapter-narrative-sync:v1', 1, 'failed', ?, 3)",
        ("novel-1", chapter.number, content_sha256, "provider unavailable"),
    )
    db.commit()
    repository = SqliteChapterNarrativeCommitRepository(db)

    assert not repository.reclaim_terminal_failure(
        novel_id="novel-1",
        chapter_number=chapter.number,
        content_sha256="0" * 64,
        pipeline_version="chapter-narrative-sync:v1",
        content_revision=1,
    )
    assert db.fetch_one(
        "SELECT status, attempt_count FROM chapter_narrative_commits"
    )["status"] == "failed"

    assert repository.reclaim_terminal_failure(
        novel_id="novel-1",
        chapter_number=chapter.number,
        content_sha256=content_sha256,
        pipeline_version="chapter-narrative-sync:v1",
        content_revision=1,
    )
    row = db.fetch_one(
        "SELECT status, attempt_count, failure_reason FROM chapter_narrative_commits"
    )
    assert dict(row) == {"status": "stale", "attempt_count": 3, "failure_reason": "provider unavailable"}
    claim = repository.claim(
        novel_id="novel-1",
        chapter_number=chapter.number,
        content_sha256=content_sha256,
        pipeline_version="chapter-narrative-sync:v1",
        expected_content_revision=1,
    )
    assert claim.disposition == "claimed"
    assert claim.attempt_count == 1


def test_terminal_recovery_reclaims_only_expired_stale_or_in_progress_claims(tmp_path):
    db, _chapter_repo, chapter, _knowledge = _canonical_services(tmp_path)
    content_sha256 = hashlib.sha256(chapter.content.encode("utf-8")).hexdigest()
    db.execute(
        "INSERT INTO chapter_narrative_commits "
        "(novel_id, chapter_number, content_sha256, pipeline_version, "
        "content_revision, status, failure_reason, attempt_count, updated_at) "
        "VALUES (?, ?, ?, 'chapter-narrative-sync:v1', 1, 'stale', ?, 3, ?)",
        (
            "novel-1",
            chapter.number,
            content_sha256,
            "interrupted recovery",
            datetime.now(timezone.utc).isoformat(),
        ),
    )
    db.commit()
    repository = SqliteChapterNarrativeCommitRepository(db)
    kwargs = {
        "novel_id": "novel-1",
        "chapter_number": chapter.number,
        "content_sha256": content_sha256,
        "pipeline_version": "chapter-narrative-sync:v1",
        "content_revision": 1,
    }

    assert not repository.reclaim_terminal_failure(**kwargs)
    db.execute(
        "UPDATE chapter_narrative_commits SET updated_at = ?",
        ((datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat(),),
    )
    db.commit()
    assert repository.reclaim_terminal_failure(**kwargs)
    db.execute(
        "UPDATE chapter_narrative_commits SET status = 'in_progress', updated_at = ?",
        (datetime.now(timezone.utc).isoformat(),),
    )
    db.commit()
    assert not repository.reclaim_terminal_failure(**kwargs)

    db.execute(
        "UPDATE chapter_narrative_commits SET updated_at = ?",
        ((datetime.now(timezone.utc) - timedelta(minutes=11)).isoformat(),),
    )
    db.commit()
    assert repository.reclaim_terminal_failure(**kwargs)


def test_chapter_narrative_artifacts_are_idempotent_across_audit_retries():
    content = (
        "林澈持有铜铃。铜铃将在雨夜响起。铜铃丢失，林澈追查真相。"
        "林澈因铜铃丢失而执念追查真相，林澈在章末仍保持警觉。"
    )
    bundle = {
        "relation_triples": [{"data": {"subject": "林澈", "predicate": "持有", "object": "铜铃", "evidence_text": "林澈持有铜铃"}, "status": "pending"}],
        "foreshadow_hints": [{"data": {"description": "铜铃将在雨夜响起", "suggested_resolve_offset": 3, "importance": "high", "evidence_text": "铜铃将在雨夜响起"}}],
        "causal_edges": [{"data": {"source_event": "铜铃丢失", "target_event": "林澈追查真相", "causal_type": "motivates", "strength": 0.8, "evidence_text": "铜铃丢失，林澈追查真相"}}],
        "character_mutations": [{"data": {"character_name": "林澈", "mutation_type": "motivation", "source_event": "铜铃丢失", "impact_or_description": "追查真相", "sensitivity_tags_or_priority": 8, "evidence_text": "林澈因铜铃丢失而执念追查真相"}}],
        "character_states": [{"character_name": "林澈", "mental_state": "警觉", "evidence_text": "林澈在章末仍保持警觉"}],
    }
    triple_repo = _TripleRepo()
    foreshadow_repo = _ForeshadowRepo()
    causal_repo = _CausalRepo()
    state_repo = _CharacterStateRepo()
    debt_repo = _DebtRepo()

    for _ in range(2):
        persist_bundle_triples_and_foreshadows("novel-1", 4, bundle, triple_repo, foreshadow_repo, content=content)
        persist_causal_edges("novel-1", 4, bundle, causal_repo, content=content)
        persist_character_mutations("novel-1", 4, bundle, state_repo, content=content)
        persist_character_end_states("novel-1", 4, bundle, state_repo, content=content)
        update_narrative_debts("novel-1", 4, bundle, debt_repo, causal_repo, content=content)

    assert len(triple_repo._kr.rows) == 1
    assert len(foreshadow_repo.registry.foreshadowings) == 1
    assert len(causal_repo.edges) == 1
    assert len(debt_repo.debts) == 2
    state = state_repo.states[("novel-1", "林澈")]
    assert len(state.motivations) == 1
    assert len(state.emotional_arc) == 1


def test_narrative_debts_reject_negated_evidence():
    debt_repo = _DebtRepo()
    bundle = {
        "causal_edges": [
            {
                "data": {
                    "source_event": "杀死反派甲",
                    "target_event": "林澈复仇完成",
                    "strength": 0.9,
                    "evidence_text": "林澈没有杀死反派甲",
                }
            }
        ],
        "character_mutations": [
            {
                "data": {
                    "character_name": "林澈",
                    "mutation_type": "motivation",
                    "source_event": "杀死反派甲",
                    "impact_or_description": "复仇完成后的轻松",
                    "sensitivity_tags_or_priority": 9,
                    "evidence_text": "林澈没有杀死反派甲",
                }
            }
        ],
    }

    assert (
        update_narrative_debts(
            "novel-1",
            4,
            bundle,
            debt_repo,
            content="林澈没有杀死反派甲，仍把刀收回鞘中。",
        )
        == 0
    )
    assert debt_repo.debts == {}


def test_consumed_foreshadow_requires_object_evidence():
    from domain.novel.value_objects.foreshadowing import (
        Foreshadowing,
        ForeshadowingStatus,
        ImportanceLevel,
    )

    repo = _ForeshadowRepo()
    registry = repo.get_by_novel_id(NovelId("novel-1"))
    registry.register(
        Foreshadowing(
            id="rain-bell",
            planted_in_chapter=1,
            description="铜铃将在雨夜响起",
            importance=ImportanceLevel.HIGH,
            status=ForeshadowingStatus.PLANTED,
        )
    )
    content = "雨夜里，铜铃终于响起，守门人现身。"

    persist_bundle_triples_and_foreshadows(
        "novel-1",
        4,
        {
            "consumed_foreshadows": [
                {
                    "description": "铜铃将在雨夜响起",
                    "evidence_text": "雨夜里，铜铃终于响起",
                }
            ]
        },
        None,
        repo,
        content=content,
    )

    assert repo.registry.foreshadowings[0].status == ForeshadowingStatus.RESOLVED


def test_consumed_foreshadow_rejects_a_reworded_pending_description():
    """A prose quote cannot compensate for identifying the wrong pending clue."""
    from domain.novel.value_objects.foreshadowing import (
        Foreshadowing,
        ForeshadowingStatus,
        ImportanceLevel,
    )

    repo = _ForeshadowRepo()
    registry = repo.get_by_novel_id(NovelId("novel-1"))
    registry.register(
        Foreshadowing(
            id="rain-bell",
            planted_in_chapter=1,
            description="铜铃雨夜响起",
            importance=ImportanceLevel.HIGH,
            status=ForeshadowingStatus.PLANTED,
        )
    )
    content = "铜铃雨夜响起的伏笔终于应验。"

    persist_bundle_triples_and_foreshadows(
        "novel-1",
        4,
        {
            "consumed_foreshadows": [
                {
                    "description": "铜铃雨夜响起的伏笔",
                    "evidence_text": "铜铃雨夜响起的伏笔终于应验",
                }
            ]
        },
        None,
        repo,
        content=content,
    )

    assert repo.registry.foreshadowings[0].status == ForeshadowingStatus.PLANTED


def test_causal_resolution_requires_an_affirmative_final_text_match():
    from domain.novel.value_objects.causal_edge import CausalEdge

    repo = _CausalRepo()
    edge = CausalEdge(
        id="kill-edge",
        novel_id="novel-1",
        source_event_summary="反派甲威胁林澈",
        source_chapter=1,
        target_event_summary="林澈杀死反派甲",
    )
    repo.save(edge)
    bundle = {"summary": "林澈杀死反派甲", "key_events": "击杀反派甲"}

    assert (
        _try_resolve_causal_edges(
            "novel-1",
            4,
            bundle,
            repo,
            content="林澈没有杀死反派甲，仍把刀收回鞘中。",
        )
        == 0
    )
    assert not repo.edges["kill-edge"].is_resolved

    assert (
        _try_resolve_causal_edges(
            "novel-1",
            4,
            bundle,
            repo,
            content="林澈杀死反派甲，血迹很快被雨水冲淡。",
        )
        == 1
    )
    assert repo.edges["kill-edge"].is_resolved


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


def test_bundle_extras_require_final_evidence_for_storyline_and_dialogue():
    def persist(content, evidence):
        storylines = _StorylineRepo()
        events = _NarrativeEventRepo()
        persisted = persist_bundle_extras(
            "novel-1",
            2,
            {
                "storyline_progress": [
                    {
                        "type": "主线",
                        "description": "我亲手杀死了反派甲。",
                        "evidence_text": evidence,
                    }
                ],
                "dialogues": [
                    {
                        "speaker": "林澈",
                        "content": "我亲手杀死了反派甲。",
                        "context": "城门前",
                        "evidence_text": evidence,
                    }
                ],
            },
            storyline_repository=storylines,
            narrative_event_repository=events,
            content=content,
        )
        assert persisted
        return storylines, events

    for content, evidence in (
        ("林澈站在城门前。", ""),
        ("林澈站在城门前。", "林澈说：我亲手杀死了反派甲。"),
        ("林澈说：我没能杀死反派甲。", "林澈说：我没能杀死反派甲。"),
    ):
        storylines, events = persist(content, evidence)
        assert storylines.storylines == []
        assert events.events == {}

    storylines, events = persist(
        "林澈说：我亲手杀死了反派甲。",
        "林澈说：我亲手杀死了反派甲。",
    )
    assert len(storylines.storylines) == 1
    assert len(events.events) == 1


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

    chapter = _rewrite(chapter_repo, chapter, "第二版正文")
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
            "status": "stale",
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
async def test_canonical_sync_fails_when_required_character_state_write_fails(
    tmp_path,
    monkeypatch,
):
    _db, chapter_repo, chapter, knowledge = _canonical_services(
        tmp_path, content="林澈在铜铃丢失后进入警觉状态。"
    )
    bundle = _canonical_bundle()
    bundle["character_states"] = [
        {
            "character_name": "林澈",
            "mental_state": "警觉",
            "evidence_text": "林澈在铜铃丢失后进入警觉状态",
        }
    ]
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.llm_chapter_extract_bundle",
        AsyncMock(return_value=bundle),
    )

    class _FailingCharacterStateRepository:
        def get(self, *_args):
            return None

        def save(self, _state):
            raise RuntimeError("character state unavailable")

    result = await sync_chapter_narrative_after_save(
        "novel-1",
        1,
        chapter.content,
        knowledge,
        None,
        SimpleNamespace(),
        chapter_repository=chapter_repo,
        character_state_repository=_FailingCharacterStateRepository(),
    )

    assert result.commit_status == "failed"
    assert "character state unavailable" in result.failure_reason


@pytest.mark.asyncio
async def test_returning_to_prior_hash_reclaims_claim_for_current_revision(
    tmp_path, monkeypatch
):
    db, chapter_repo, chapter, knowledge = _canonical_services(tmp_path)
    extract = AsyncMock(
        side_effect=[
            _canonical_bundle("第一版摘要"),
            _canonical_bundle("第二版摘要"),
            _canonical_bundle("回到第一版摘要"),
        ]
    )
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.llm_chapter_extract_bundle",
        extract,
    )

    first = await sync_chapter_narrative_after_save(
        "novel-1", 1, chapter.content, knowledge, None, SimpleNamespace(),
        chapter_repository=chapter_repo,
    )
    chapter = _rewrite(chapter_repo, chapter, "第二版正文")
    second = await sync_chapter_narrative_after_save(
        "novel-1", 1, chapter.content, knowledge, None, SimpleNamespace(),
        chapter_repository=chapter_repo,
    )
    chapter = _rewrite(chapter_repo, chapter, "第一版正文")
    returned = await sync_chapter_narrative_after_save(
        "novel-1", 1, chapter.content, knowledge, None, SimpleNamespace(),
        chapter_repository=chapter_repo,
    )

    assert [first.content_revision, second.content_revision, returned.content_revision] == [
        1,
        2,
        3,
    ]
    assert returned.commit_status == "committed"
    assert returned.attempt_count == 1
    assert extract.await_count == 3
    current_claim = db.fetch_one(
        "SELECT content_revision, status, attempt_count "
        "FROM chapter_narrative_commits WHERE content_sha256 = ?",
        (hashlib.sha256("第一版正文".encode("utf-8")).hexdigest(),),
    )
    assert dict(current_claim) == {
        "content_revision": 3,
        "status": "committed",
        "attempt_count": 1,
    }


def test_claim_rejects_stale_expected_content_revision(tmp_path):
    db, chapter_repo, chapter, _knowledge = _canonical_services(tmp_path)
    repository = SqliteChapterNarrativeCommitRepository(db)

    chapter.update_content("第二版正文")
    chapter_repo.save(chapter)
    result = repository.claim(
        novel_id="novel-1",
        chapter_number=1,
        content_sha256=hashlib.sha256("第二版正文".encode("utf-8")).hexdigest(),
        pipeline_version="chapter-narrative-sync:v1",
        expected_content_revision=1,
    )

    assert result.disposition == "failed"
    assert result.content_revision == 2
    assert result.failure_reason == "source_revision_mismatch"
    assert db.fetch_all("SELECT * FROM chapter_narrative_commits") == []


def test_stale_failure_cannot_poison_reclaimed_hash_revision(tmp_path):
    """An old async attempt must not fail a later revision with the same hash."""
    db, chapter_repo, chapter, _knowledge = _canonical_services(tmp_path)
    repository = SqliteChapterNarrativeCommitRepository(db)
    content_sha256 = hashlib.sha256(chapter.content.encode("utf-8")).hexdigest()

    original = repository.claim(
        novel_id="novel-1",
        chapter_number=1,
        content_sha256=content_sha256,
        pipeline_version="chapter-narrative-sync:v1",
    )
    chapter = _rewrite(chapter_repo, chapter, "第二版正文")
    chapter = _rewrite(chapter_repo, chapter, "第一版正文")
    reclaimed = repository.claim(
        novel_id="novel-1",
        chapter_number=1,
        content_sha256=content_sha256,
        pipeline_version="chapter-narrative-sync:v1",
    )

    assert original.content_revision == 1
    assert reclaimed.content_revision == 3
    repository.fail(
        novel_id="novel-1",
        chapter_number=1,
        content_sha256=content_sha256,
        pipeline_version="chapter-narrative-sync:v1",
        content_revision=original.content_revision,
        failure_reason="stale provider failure",
    )

    current = db.fetch_one(
        "SELECT content_revision, status, failure_reason FROM chapter_narrative_commits"
    )
    assert dict(current) == {
        "content_revision": 3,
        "status": "in_progress",
        "failure_reason": "",
    }


@pytest.mark.asyncio
async def test_current_version_readiness_requires_exact_committed_hash_and_revision(
    tmp_path, monkeypatch
):
    db, chapter_repo, chapter, knowledge = _canonical_services(tmp_path)
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.llm_chapter_extract_bundle",
        AsyncMock(return_value=_canonical_bundle()),
    )
    committed = await sync_chapter_narrative_after_save(
        "novel-1", 1, chapter.content, knowledge, None, SimpleNamespace(),
        chapter_repository=chapter_repo,
    )
    commit_repo = SqliteChapterNarrativeCommitRepository(db)
    commit_repo.set_vector_status(
        novel_id="novel-1",
        chapter_number=1,
        content_sha256=committed.content_sha256,
        pipeline_version=committed.pipeline_version,
        vector_status="failed",
    )

    assert commit_repo.is_current_version_ready(
        novel_id="novel-1",
        chapter_number=1,
        pipeline_version=committed.pipeline_version,
    ) is True

    chapter = _rewrite(chapter_repo, chapter, "第二版正文")

    assert commit_repo.is_current_version_ready(
        novel_id="novel-1",
        chapter_number=1,
        pipeline_version=committed.pipeline_version,
    ) is False


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
    db, chapter_repo, chapter, knowledge = _canonical_services(
        tmp_path, content="林澈持有铜铃，铜铃丢失后追查真相。"
    )
    bundle = _canonical_bundle()
    bundle["relation_triples"] = [
        {
            "data": {
                "subject": "林澈",
                "predicate": "持有",
                "object": "铜铃",
                "evidence_text": "林澈持有铜铃",
            }
        }
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
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync._retry_delay_seconds",
        lambda _attempt: (_ for _ in ()).throw(
            AssertionError("source hash mismatch must not retry")
        ),
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


def test_prepare_summary_records_claimed_source_revision(tmp_path):
    db, _chapter_repo, chapter, knowledge = _canonical_services(tmp_path)
    if not any(
        row["name"] == "source_content_revision"
        for row in db.fetch_all("PRAGMA table_info(chapter_summaries)")
    ):
        db.execute(
            "ALTER TABLE chapter_summaries ADD COLUMN "
            "source_content_revision INTEGER NOT NULL DEFAULT 0"
        )
    repository = SqliteChapterNarrativeCommitRepository(db)
    content_sha256 = hashlib.sha256(chapter.content.encode("utf-8")).hexdigest()
    claim = repository.claim(
        novel_id="novel-1",
        chapter_number=1,
        content_sha256=content_sha256,
        pipeline_version="chapter-narrative-sync:v1",
    )
    knowledge.upsert_chapter_summary(
        novel_id="novel-1",
        chapter_id=1,
        summary="绑定正文修订号的摘要",
        sync_status="in_progress",
    )

    repository.prepare_summary(
        novel_id="novel-1",
        chapter_number=1,
        content_sha256=content_sha256,
        pipeline_version="chapter-narrative-sync:v1",
        attempt_count=claim.attempt_count,
    )

    summary = db.fetch_one(
        "SELECT source_content_sha256, source_content_revision "
        "FROM chapter_summaries WHERE chapter_number = 1"
    )
    assert dict(summary) == {
        "source_content_sha256": content_sha256,
        "source_content_revision": claim.content_revision,
    }


def test_canonical_commit_rejects_summary_revision_changed_after_prepare(tmp_path):
    db, _chapter_repo, chapter, knowledge = _canonical_services(tmp_path)
    if not any(
        row["name"] == "source_content_revision"
        for row in db.fetch_all("PRAGMA table_info(chapter_summaries)")
    ):
        db.execute(
            "ALTER TABLE chapter_summaries ADD COLUMN "
            "source_content_revision INTEGER NOT NULL DEFAULT 0"
        )
    repository = SqliteChapterNarrativeCommitRepository(db)
    content_sha256 = hashlib.sha256(chapter.content.encode("utf-8")).hexdigest()
    claim = repository.claim(
        novel_id="novel-1",
        chapter_number=1,
        content_sha256=content_sha256,
        pipeline_version="chapter-narrative-sync:v1",
    )
    knowledge.upsert_chapter_summary(
        novel_id="novel-1",
        chapter_id=1,
        summary="准备提交的摘要",
        sync_status="in_progress",
    )
    repository.prepare_summary(
        novel_id="novel-1",
        chapter_number=1,
        content_sha256=content_sha256,
        pipeline_version="chapter-narrative-sync:v1",
        attempt_count=claim.attempt_count,
    )
    db.execute(
        "UPDATE chapter_summaries SET source_content_revision = 0 "
        "WHERE chapter_number = 1"
    )
    db.commit()

    with pytest.raises(RuntimeError, match="canonical_summary_write_missing"):
        repository.commit(
            novel_id="novel-1",
            chapter_number=1,
            content_sha256=content_sha256,
            pipeline_version="chapter-narrative-sync:v1",
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
    db, chapter_repo, chapter, knowledge = _canonical_services(
        tmp_path, "林澈说：城门会在子时关闭。"
    )
    bundle = _canonical_bundle()
    bundle["dialogues"] = [
        {
            "speaker": "林澈",
            "content": "城门会在子时关闭。",
            "context": "城门",
            "evidence_text": "林澈说：城门会在子时关闭。",
        }
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


@pytest.mark.asyncio
async def test_canonical_sync_retries_same_version_exactly_three_times(
    tmp_path, monkeypatch
):
    db, chapter_repo, chapter, knowledge = _canonical_services(tmp_path)
    extract = AsyncMock(side_effect=RuntimeError("provider unavailable"))
    delays = []

    async def capture_sleep(delay):
        delays.append(delay)

    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.llm_chapter_extract_bundle",
        extract,
    )
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.asyncio.sleep",
        capture_sleep,
    )

    terminal = await sync_chapter_narrative_after_save(
        "novel-1", 1, chapter.content, knowledge, None, SimpleNamespace(),
        chapter_repository=chapter_repo,
    )
    repeated = await sync_chapter_narrative_after_save(
        "novel-1", 1, chapter.content, knowledge, None, SimpleNamespace(),
        chapter_repository=chapter_repo,
    )

    assert terminal.get("commit_status") == "failed"
    assert terminal.get("attempt_count") == 3
    assert repeated.get("commit_status") == "failed"
    assert repeated.get("attempt_count") == 3
    assert delays == [1.5, 3.0]
    assert extract.await_count == 3
    claim = db.fetch_one(
        "SELECT status, attempt_count, failure_reason FROM chapter_narrative_commits"
    )
    assert dict(claim) == {
        "status": "failed",
        "attempt_count": 3,
        "failure_reason": "provider unavailable",
    }

    extract.side_effect = None
    extract.return_value = _canonical_bundle()
    chapter = _rewrite(chapter_repo, chapter, "changed content")
    changed = await sync_chapter_narrative_after_save(
        "novel-1", 1, chapter.content, knowledge, None, SimpleNamespace(),
        chapter_repository=chapter_repo,
    )

    assert changed.get("commit_status") == "committed"
    assert changed.get("attempt_count") == 1


@pytest.mark.asyncio
async def test_reused_commit_retries_failed_vector_without_reextracting(
    tmp_path, monkeypatch
):
    db, chapter_repo, chapter, knowledge = _canonical_services(tmp_path)
    extract = AsyncMock(return_value=_canonical_bundle())
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.llm_chapter_extract_bundle",
        extract,
    )

    class FailingIndexer:
        async def ensure_collection(self, novel_id):
            return None

        async def index_chapter_summary(self, *args, **kwargs):
            raise RuntimeError("vector offline")

    class WorkingIndexer:
        def __init__(self):
            self.calls = []

        async def ensure_collection(self, novel_id):
            return None

        async def index_chapter_summary(self, *args, **kwargs):
            self.calls.append((args, kwargs))

    initial = await sync_chapter_narrative_after_save(
        "novel-1", 1, chapter.content, knowledge, FailingIndexer(), SimpleNamespace(),
        chapter_repository=chapter_repo,
    )
    recovered_indexer = WorkingIndexer()
    recovered = await sync_chapter_narrative_after_save(
        "novel-1", 1, chapter.content, knowledge, recovered_indexer, SimpleNamespace(),
        chapter_repository=chapter_repo,
    )

    assert initial.get("commit_status") == "committed"
    assert initial.get("vector_status") == "failed"
    assert recovered.get("commit_status") == "reused"
    assert recovered.get("vector_status") == "stored"
    assert extract.await_count == 1
    assert len(recovered_indexer.calls) == 1
    claim = db.fetch_one(
        "SELECT status, vector_status, attempt_count FROM chapter_narrative_commits"
    )
    assert dict(claim) == {
        "status": "committed",
        "vector_status": "stored",
        "attempt_count": 1,
    }


@pytest.mark.asyncio
async def test_stale_extraction_cannot_replace_the_newer_canonical_summary(
    tmp_path, monkeypatch
):
    """An older async extraction must lose its CAS race before touching summaries."""
    db, chapter_repo, chapter, knowledge = _canonical_services(
        tmp_path, "旧角色说：旧版本事件。"
    )
    old_content = chapter.content
    old_started = asyncio.Event()
    release_old = asyncio.Event()

    async def extract_with_delayed_old_version(_llm, content, _chapter_number, **_kwargs):
        if content == old_content:
            old_started.set()
            await release_old.wait()
            return {
                **_canonical_bundle(summary="旧版本摘要"),
                "dialogues": [
                    {
                        "speaker": "旧角色",
                        "content": "旧版本事件",
                        "context": "旧场景",
                        "evidence_text": "旧角色说：旧版本事件。",
                    }
                ],
            }
        return {
            **_canonical_bundle(summary="新版本摘要"),
            "dialogues": [
                {
                    "speaker": "新角色",
                    "content": "新版本事件",
                    "context": "新场景",
                    "evidence_text": "新角色说：新版本事件。",
                }
            ],
        }

    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.llm_chapter_extract_bundle",
        extract_with_delayed_old_version,
    )
    event_repository = SqliteNarrativeEventRepository(db)

    old_task = asyncio.create_task(
        sync_chapter_narrative_after_save(
            "novel-1",
            1,
            old_content,
            knowledge,
            None,
            SimpleNamespace(),
            chapter_repository=chapter_repo,
            narrative_event_repository=event_repository,
        )
    )
    await old_started.wait()

    chapter = _rewrite(chapter_repo, chapter, "新角色说：新版本事件。")
    newer = await sync_chapter_narrative_after_save(
        "novel-1",
        1,
        chapter.content,
        knowledge,
        None,
        SimpleNamespace(),
        chapter_repository=chapter_repo,
        narrative_event_repository=event_repository,
    )

    release_old.set()
    stale = await old_task

    assert newer.commit_status == "committed"
    assert stale.commit_status == "failed"
    assert stale.failure_reason == "source_hash_mismatch"
    summary = db.fetch_one(
        "SELECT summary, source_content_sha256, sync_status FROM chapter_summaries "
        "WHERE chapter_number = 1"
    )
    assert dict(summary) == {
        "summary": "新版本摘要",
        "source_content_sha256": newer.content_sha256,
        "sync_status": "committed",
    }
    assert [row["event_summary"] for row in db.fetch_all(
        "SELECT event_summary FROM narrative_events WHERE novel_id = ? "
        "AND chapter_number = ? ORDER BY event_summary",
        ("novel-1", 1),
    )] == ["新角色: 新版本事件"]


@pytest.mark.asyncio
async def test_rewrite_after_stale_claim_before_summary_write_persists_no_old_summary(
    tmp_path, monkeypatch
):
    """A source rewrite between the last claim check and SQLite write must win."""
    db, chapter_repo, chapter, knowledge = _canonical_services(tmp_path)
    db.commit()
    old_content = chapter.content
    before_summary_write = threading.Event()
    release_summary_write = threading.Event()
    result = {}
    error = {}
    original_save = knowledge.knowledge_repository.save_canonical_chapter_summary

    def blocked_save(*args, **kwargs):
        before_summary_write.set()
        assert release_summary_write.wait(timeout=5)
        return original_save(*args, **kwargs)

    monkeypatch.setattr(
        knowledge.knowledge_repository, "save_canonical_chapter_summary", blocked_save
    )
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.llm_chapter_extract_bundle",
        AsyncMock(return_value=_canonical_bundle(summary="旧版本摘要")),
    )
    from domain.novel.value_objects.tension_dimensions import TensionDimensions

    class NoopTensionScoringService:
        def __init__(self, _llm):
            pass

        async def score_chapter(self, **_kwargs):
            return TensionDimensions.unevaluated()

    monkeypatch.setattr(
        "application.analyst.services.tension_scoring_service.TensionScoringService",
        NoopTensionScoringService,
    )

    async def skip_tension(*args, **kwargs):
        raise RuntimeError("not relevant to canonical write race")

    monkeypatch.setattr(
        "application.analyst.services.tension_scoring_service.TensionScoringService.score_chapter",
        skip_tension,
    )

    def run_old_sync():
        try:
            result["value"] = asyncio.run(
                sync_chapter_narrative_after_save(
                    "novel-1",
                    1,
                    old_content,
                    knowledge,
                    None,
                    SimpleNamespace(),
                    chapter_repository=chapter_repo,
                )
            )
        except BaseException as exc:
            error["value"] = exc

    old_sync = threading.Thread(target=run_old_sync)
    old_sync.start()
    if not before_summary_write.wait(timeout=5):
        release_summary_write.set()
        old_sync.join(timeout=5)
        pytest.fail(f"old sync did not reach summary write: {error!r} {result!r}")
    _rewrite(chapter_repo, chapter, "第二版正文")
    release_summary_write.set()
    old_sync.join(timeout=5)

    assert not old_sync.is_alive()
    assert error == {}
    assert result["value"].failure_reason == "source_hash_mismatch"
    assert db.fetch_one(
        "SELECT summary FROM chapter_summaries WHERE chapter_number = ?",
        (1,),
    ) is None
