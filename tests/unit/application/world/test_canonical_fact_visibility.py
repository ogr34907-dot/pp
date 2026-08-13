from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from application.world.services.chapter_narrative_sync import (
    _sync_chapter_narrative_after_save_once,
    sync_chapter_narrative_after_save,
)
from application.core.services.chapter_rewrite_coordinator import (
    ChapterRewriteCoordinator,
)
from application.world.services.knowledge_service import KnowledgeService
from domain.novel.entities.chapter import Chapter, ChapterStatus
from domain.novel.value_objects.novel_id import NovelId
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_causal_edge_repository import (
    SqliteCausalEdgeRepository,
)
from infrastructure.persistence.database.sqlite_chapter_repository import (
    SqliteChapterRepository,
)
from infrastructure.persistence.database.sqlite_character_state_repository import (
    SqliteCharacterStateRepository,
)
from infrastructure.persistence.database.sqlite_foreshadowing_repository import (
    SqliteForeshadowingRepository,
)
from infrastructure.persistence.database.sqlite_knowledge_repository import (
    SqliteKnowledgeRepository,
)
from infrastructure.persistence.database.triple_repository import TripleRepository
from application.engine.services.context_budget_allocator import ContextBudgetAllocator
from application.engine.services.memory_engine import MemoryEngine
from application.engine.services.worldline_generation_guard import (
    GenerationEpochUnavailableError,
)


def _chapter_services(tmp_path, content: str):
    db = DatabaseConnection(str(tmp_path / "canonical-visibility.db"))
    db.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'Novel', 'novel-1')"
    )
    chapter_repo = SqliteChapterRepository(db)
    chapter = Chapter(
        id="chapter-1",
        novel_id=NovelId("novel-1"),
        number=1,
        title="Chapter",
        content=content,
    )
    chapter_repo.save(chapter)
    return db, chapter_repo, chapter, KnowledgeService(SqliteKnowledgeRepository(db))


def test_repository_rejects_direct_save_after_canonical_commit(tmp_path):
    """Formal canonical chapters must be rewritten through the coordinator."""
    db = DatabaseConnection(str(tmp_path / "chapter-save-guard.db"))
    db.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'Novel', 'novel-1')"
    )
    chapter_repo = SqliteChapterRepository(db)
    chapter_repo.save(
        Chapter(
            id="chapter-1",
            novel_id=NovelId("novel-1"),
            number=1,
            title="Chapter",
            content="正式旧正文",
            status=ChapterStatus.COMPLETED,
        )
    )
    source = db.fetch_one(
        "SELECT content_sha256, content_revision FROM chapters "
        "WHERE novel_id = 'novel-1' AND number = 1"
    )
    db.execute(
        "INSERT INTO chapter_narrative_commits "
        "(novel_id, chapter_number, content_sha256, pipeline_version, "
        "content_revision, status) VALUES (?, ?, ?, ?, ?, 'committed')",
        (
            "novel-1",
            1,
            source["content_sha256"],
            "chapter-narrative-sync:v1",
            source["content_revision"],
        ),
    )

    with pytest.raises(RuntimeError, match="ChapterRewriteCoordinator"):
        chapter_repo.save(
            Chapter(
                id="chapter-1",
                novel_id=NovelId("novel-1"),
                number=1,
                title="Chapter",
                content="旁路新正文",
                status=ChapterStatus.COMPLETED,
            )
        )

    row = db.fetch_one(
        "SELECT content FROM chapters WHERE novel_id = 'novel-1' AND number = 1"
    )
    assert row["content"] == "正式旧正文"


def test_repository_allows_new_empty_and_uncommitted_draft_updates(tmp_path):
    db = DatabaseConnection(str(tmp_path / "chapter-draft-save.db"))
    db.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'Novel', 'novel-1')"
    )
    chapter_repo = SqliteChapterRepository(db)

    chapter_repo.save(
        Chapter(
            id="chapter-1",
            novel_id=NovelId("novel-1"),
            number=1,
            title="Chapter",
            content="",
        )
    )
    chapter_repo.save(
        Chapter(
            id="chapter-1",
            novel_id=NovelId("novel-1"),
            number=1,
            title="Chapter",
            content="首段草稿",
        )
    )
    chapter_repo.save(
        Chapter(
            id="chapter-1",
            novel_id=NovelId("novel-1"),
            number=1,
            title="Chapter",
            content="首段草稿\n\n追加草稿",
        )
    )
    chapter_repo.save(
        Chapter(
            id="chapter-2",
            novel_id=NovelId("novel-1"),
            number=2,
            title="Chapter 2",
            content="新章草稿",
        )
    )

    rows = db.fetch_all(
        "SELECT number, content, content_revision FROM chapters "
        "WHERE novel_id = 'novel-1' ORDER BY number"
    )
    assert [(row["number"], row["content"], row["content_revision"]) for row in rows] == [
        (1, "首段草稿\n\n追加草稿", 3),
        (2, "新章草稿", 1),
    ]


@pytest.mark.asyncio
async def test_formal_aftermath_persists_authority_rows_after_commit(
    tmp_path, monkeypatch
):
    content = (
        "林澈在苍梧城亲眼看见城门坍塌。玉佩裂纹会引来追兵，"
        "苍梧城毁灭迫使林澈复仇，林澈决定追查真相。"
    )
    db, chapter_repo, chapter, knowledge = _chapter_services(tmp_path, content)
    monkeypatch.setattr(
        "infrastructure.persistence.database.connection.get_database",
        lambda *args, **kwargs: db,
    )
    bundle = {
        "summary": "苍梧城毁灭，林澈决定复仇。",
        "key_events": "城毁与复仇",
        "open_threads": "追查真相",
        "relation_triples": [
            {
                "subject": "林澈",
                "predicate": "见证",
                "object": "苍梧城毁灭",
                "evidence_text": "林澈在苍梧城亲眼看见城门坍塌",
            }
        ],
        "foreshadow_hints": [
            {
                "description": "玉佩裂纹会引来追兵",
                "importance": "high",
                "evidence_text": "玉佩裂纹会引来追兵",
            }
        ],
        "consumed_foreshadows": [],
        "storyline_progress": [],
        "dialogues": [],
        "timeline_events": [
            {
                "event": "苍梧城毁灭",
                "time_point": "第1章",
                "description": "苍梧城毁灭迫使林澈复仇",
                "evidence_text": "苍梧城毁灭",
            }
        ],
        "causal_edges": [
            {
                "source_event": "苍梧城毁灭",
                "target_event": "林澈复仇",
                "causal_type": "causes",
                "state_change": "林澈决定复仇",
                "involved_characters": ["林澈"],
                "evidence_text": "苍梧城毁灭迫使林澈复仇",
            }
        ],
        "character_mutations": [
            {
                "character_name": "林澈",
                "mutation_type": "motivation",
                "source_event": "苍梧城毁灭",
                "impact_or_description": "追查真相并复仇",
                "sensitivity_tags_or_priority": 9,
                "evidence_text": "林澈决定追查真相",
            }
        ],
        "character_states": [],
    }
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.llm_chapter_extract_bundle",
        AsyncMock(return_value=bundle),
    )

    result = await sync_chapter_narrative_after_save(
        "novel-1",
        1,
        content,
        knowledge,
        None,
        SimpleNamespace(),
        triple_repository=TripleRepository(db),
        foreshadowing_repo=SqliteForeshadowingRepository(db),
        chapter_repository=chapter_repo,
        causal_edge_repository=SqliteCausalEdgeRepository(db),
        character_state_repository=SqliteCharacterStateRepository(db),
    )

    assert result.commit_status == "committed", result.failure_reason
    triple = db.fetch_one("SELECT id FROM triples WHERE novel_id = 'novel-1'")
    triple_attrs = {
        row["attr_key"]: row["attr_value"]
        for row in db.fetch_all(
            "SELECT attr_key, attr_value FROM triple_attr WHERE triple_id = ?",
            (triple["id"],),
        )
    }
    assert triple_attrs["evidence_text"] == "林澈在苍梧城亲眼看见城门坍塌"
    commit = db.fetch_one(
        "SELECT status FROM chapter_narrative_commits "
        "WHERE novel_id = 'novel-1' AND chapter_number = 1"
    )
    assert commit["status"] == "committed"
    timeline = db.fetch_one(
        "SELECT source_type, chapter_number FROM bible_timeline_notes "
        "WHERE novel_id = 'novel-1' AND source_type = 'chapter_aftermath'"
    )
    assert dict(timeline) == {"source_type": "chapter_aftermath", "chapter_number": 1}
    for table in ("foreshadows", "causal_edges", "character_states"):
        assert db.fetch_one(f"SELECT 1 FROM {table} WHERE rowid IS NOT NULL") is not None


@pytest.mark.asyncio
async def test_rewrite_removes_only_chapter_aftermath_timeline_rows(tmp_path, monkeypatch):
    content = "苍梧城毁灭迫使林澈复仇。"
    db, chapter_repo, chapter, knowledge = _chapter_services(tmp_path, content)
    bundle = {
        "summary": "城毁",
        "key_events": "城毁",
        "open_threads": "复仇",
        "relation_triples": [],
        "foreshadow_hints": [],
        "consumed_foreshadows": [],
        "storyline_progress": [],
        "dialogues": [],
        "timeline_events": [
            {
                "event": "苍梧城毁灭",
                "time_point": "第1章",
                "description": "苍梧城毁灭迫使林澈复仇",
                "evidence_text": "苍梧城毁灭迫使林澈复仇",
            }
        ],
        "causal_edges": [],
        "character_mutations": [],
        "character_states": [],
    }
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.llm_chapter_extract_bundle",
        AsyncMock(return_value=bundle),
    )
    result = await sync_chapter_narrative_after_save(
        "novel-1",
        1,
        content,
        knowledge,
        None,
        SimpleNamespace(),
        chapter_repository=chapter_repo,
    )
    assert result.commit_status == "committed"

    db.execute(
        "INSERT INTO bible_timeline_notes "
        "(id, novel_id, event, time_point, description, source_type) "
        "VALUES ('authored-1', 'novel-1', '作者设定', '开篇前', '固定背景', 'bible')"
    )
    db.commit()

    ChapterRewriteCoordinator(db=db, chapter_repository=chapter_repo).rewrite(
        chapter,
        "林澈离开废墟，决定寻找新的盟友。",
    )

    rows = db.fetch_all(
        "SELECT id, source_type FROM bible_timeline_notes "
        "WHERE novel_id = 'novel-1' ORDER BY id"
    )
    assert [(row["id"], row["source_type"]) for row in rows] == [
        ("authored-1", "bible")
    ]


def test_allocator_fact_lock_recalls_relevant_canonical_facts_only(tmp_path):
    db = DatabaseConnection(str(tmp_path / "canonical-recall.db"))
    db.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'Novel', 'novel-1')"
    )
    db.execute(
        "INSERT INTO chapters "
        "(id, novel_id, number, title, content, content_sha256, content_revision) "
        "VALUES ('chapter-1', 'novel-1', 1, 'Chapter', '正文', 'hash', 1)"
    )
    db.execute(
        "INSERT INTO chapter_narrative_commits "
        "(novel_id, chapter_number, content_sha256, pipeline_version, content_revision, status) "
        "VALUES ('novel-1', 1, 'hash', 'chapter-narrative-sync:v1', 1, 'committed')"
    )
    db.execute(
        "INSERT INTO triples "
        "(id, novel_id, subject, predicate, object, chapter_number, confidence, source_type) "
        "VALUES ('triple-relevant', 'novel-1', '林澈', '见证', '苍梧城毁灭', 1, 0.9, 'autopilot_extract')"
    )
    db.execute(
        "INSERT INTO triple_attr (triple_id, attr_key, attr_value) "
        "VALUES ('triple-relevant', 'evidence_text', '林澈见证苍梧城毁灭')"
    )
    db.execute(
        "INSERT INTO triples "
        "(id, novel_id, subject, predicate, object, chapter_number, confidence, source_type) "
        "VALUES ('triple-irrelevant', 'novel-1', '无关人', '持有', '无关物', 1, 0.9, 'autopilot_extract')"
    )
    db.execute(
        "INSERT INTO bible_timeline_notes "
        "(id, novel_id, event, time_point, description, source_type, chapter_number) "
        "VALUES ('timeline-relevant', 'novel-1', '苍梧城毁灭', '第1章', '林澈见证苍梧城毁灭', 'chapter_aftermath', 1)"
    )
    db.execute(
        "INSERT INTO bible_timeline_notes "
        "(id, novel_id, event, time_point, description, source_type, chapter_number) "
        "VALUES ('timeline-irrelevant', 'novel-1', '无关灾难', '第1章', '无关人失去无关物', 'chapter_aftermath', 1)"
    )
    db.commit()

    bible = SimpleNamespace(
        characters=[
            SimpleNamespace(
                name="林澈",
                description="",
                relationships=[],
                public_profile="",
                hidden_profile="",
                    reveal_chapter=None,
                    mental_state="NORMAL",
                    is_dead=False,
                status="alive",
            )
        ],
        timeline_notes=[],
        world_settings=[],
        locations=[],
        style_notes=[],
    )
    bible_repository = SimpleNamespace(get_by_novel_id=lambda _novel_id: bible)
    memory = MemoryEngine(
        llm_service=object(),
        bible_repository=bible_repository,
        db_connection=db,
    )
    allocator = ContextBudgetAllocator(
        bible_repository=bible_repository,
        memory_engine=memory,
    )

    slots = allocator._collect_all_slots(
        "novel-1",
        2,
        "林澈回到苍梧城，面对毁灭后的废墟。",
    )

    fact_lock = slots["fact_lock"].content
    assert "林澈" in fact_lock
    assert "苍梧城毁灭" in fact_lock
    assert "无关人" not in fact_lock
    assert "无关物" not in fact_lock


def test_fact_lock_uses_outline_entity_intersection_for_long_run_recall(tmp_path):
    """An early hard fact is recalled only when its entity is in this chapter."""
    db = DatabaseConnection(str(tmp_path / "canonical-recall-boundary.db"))
    db.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'Novel', 'novel-1')"
    )
    for chapter_number in (1, 2, 3):
        db.execute(
            "INSERT INTO chapters "
            "(id, novel_id, number, title, content, content_sha256, content_revision) "
            "VALUES (?, 'novel-1', ?, 'Chapter', '正文', ?, 1)",
            (f"chapter-{chapter_number}", chapter_number, f"hash-{chapter_number}"),
        )
        db.execute(
            "INSERT INTO chapter_narrative_commits "
            "(novel_id, chapter_number, content_sha256, pipeline_version, content_revision, status) "
            "VALUES (?, ?, ?, 'chapter-narrative-sync:v1', 1, 'committed')",
            ("novel-1", chapter_number, f"hash-{chapter_number}"),
        )

    facts = (
        ("fact-death", "甲", "状态", "已死亡", 1),
        ("fact-identity", "甲", "身份", "真正身份", 1),
        ("fact-location", "甲", "前往", "地点A", 1),
        ("fact-乙", "乙", "前往", "地点B", 2),
        ("fact-丙", "丙", "前往", "地点C", 3),
    )
    for fact_id, subject, predicate, object_name, chapter_number in facts:
        db.execute(
            "INSERT INTO triples "
            "(id, novel_id, subject, predicate, object, chapter_number, confidence, source_type) "
            "VALUES (?, 'novel-1', ?, ?, ?, ?, 0.9, 'chapter_inferred')",
            (fact_id, subject, predicate, object_name, chapter_number),
        )
    db.commit()

    bible = SimpleNamespace(
        characters=[
            SimpleNamespace(
                name=name,
                aliases=aliases,
                relationships=[],
                description="",
                public_profile="",
                hidden_profile="",
                reveal_chapter=None,
                mental_state="NORMAL",
                is_dead=False,
                status="alive",
            )
            for name, aliases in (
                ("甲", ["甲别名"]),
                ("乙", ["乙别名"]),
                ("丙", ["丙别名"]),
            )
        ],
        locations=[
            SimpleNamespace(name=name, aliases=[])
            for name in ("地点A", "地点B", "地点C")
        ],
        timeline_notes=[],
        world_settings=[],
        style_notes=[],
    )
    builder = MemoryEngine(
        llm_service=object(),
        bible_repository=SimpleNamespace(get_by_novel_id=lambda _novel_id: bible),
        db_connection=db,
    )

    fact_lock = builder.build_fact_lock_section(
        "novel-1",
        800,
        "第800章，甲前往地点A，处理早期身份和死亡后果。",
    )
    canonical = fact_lock.split("核心 Canonical 硬事实", 1)[-1]

    assert "已死亡" in canonical
    assert "真正身份" in canonical
    assert "地点A" in canonical
    for unrelated in ("乙", "丙", "地点B", "地点C"):
        assert unrelated not in canonical
    assert canonical.count("[第") <= 40
    assert len(canonical) <= 6000


def _fact_lock_bible():
    return SimpleNamespace(
        characters=[
            SimpleNamespace(
                name=name,
                aliases=[],
                relationships=[],
                description="",
                public_profile="",
                hidden_profile="",
                reveal_chapter=None,
                mental_state="NORMAL",
                is_dead=False,
                status="alive",
            )
            for name in ("甲", "乙", "丙")
        ],
        locations=[
            SimpleNamespace(name=name, aliases=[])
            for name in ("地点A", "地点B", "地点C")
        ],
        timeline_notes=[],
        world_settings=[],
        style_notes=[],
    )


def test_fact_lock_relevance_precedes_limit_for_800_chapter_density(tmp_path):
    """A visible chapter-1 hard fact survives 300 later irrelevant facts."""
    db = DatabaseConnection(str(tmp_path / "canonical-recall-density.db"))
    db.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'Novel', 'novel-1')"
    )
    for chapter_number in range(1, 801):
        db.execute(
            "INSERT INTO chapters "
            "(id, novel_id, number, title, content, content_sha256, content_revision) "
            "VALUES (?, 'novel-1', ?, 'Chapter', '正文', ?, 1)",
            (f"chapter-{chapter_number}", chapter_number, f"hash-{chapter_number}"),
        )
        db.execute(
            "INSERT INTO chapter_narrative_commits "
            "(novel_id, chapter_number, content_sha256, pipeline_version, content_revision, status) "
            "VALUES ('novel-1', ?, ?, 'chapter-narrative-sync:v1', 1, 'committed')",
            (chapter_number, f"hash-{chapter_number}"),
        )
    db.execute(
        "INSERT INTO triples "
        "(id, novel_id, subject, predicate, object, chapter_number, confidence, source_type) "
        "VALUES ('fact-death', 'novel-1', '甲', '状态', '已死亡', 1, 0.99, 'chapter_inferred')"
    )
    for index, chapter_number in enumerate(range(2, 302), start=1):
        db.execute(
            "INSERT INTO triples "
            "(id, novel_id, subject, predicate, object, chapter_number, confidence, source_type) "
            "VALUES (?, 'novel-1', ?, '持有', ?, ?, 0.5, 'chapter_inferred')",
            (f"irrelevant-{index}", f"无关人物{index}", f"无关物件{index}", chapter_number),
        )
    db.commit()

    builder = MemoryEngine(
        llm_service=object(),
        bible_repository=SimpleNamespace(get_by_novel_id=lambda _novel_id: _fact_lock_bible()),
        db_connection=db,
    )
    fact_lock = builder.build_fact_lock_section(
        "novel-1", 800, "第800章，甲在地点A处理死亡后果。"
    )
    canonical = fact_lock.split("核心 Canonical 硬事实", 1)[-1]

    assert "甲 —状态→ 已死亡" in canonical
    assert "无关人物" not in canonical
    assert canonical.count("[第") <= 40


def test_fact_lock_visibility_precedes_limit_for_invisible_dense_rows(tmp_path):
    """Pending/failed old-world rows cannot evict an early visible hard fact."""
    db = DatabaseConnection(str(tmp_path / "canonical-recall-visibility.db"))
    db.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'Novel', 'novel-1')"
    )
    for chapter_number in range(1, 252):
        db.execute(
            "INSERT INTO chapters "
            "(id, novel_id, number, title, content, content_sha256, content_revision) "
            "VALUES (?, 'novel-1', ?, 'Chapter', '正文', ?, 1)",
            (f"chapter-{chapter_number}", chapter_number, f"hash-{chapter_number}"),
        )
    db.execute(
        "INSERT INTO chapter_narrative_commits "
        "(novel_id, chapter_number, content_sha256, pipeline_version, content_revision, status) "
        "VALUES ('novel-1', 1, 'hash-1', 'chapter-narrative-sync:v1', 1, 'committed')"
    )
    db.execute(
        "INSERT INTO triples "
        "(id, novel_id, subject, predicate, object, chapter_number, confidence, source_type) "
        "VALUES ('fact-identity', 'novel-1', '甲', '身份', '真正身份', 1, 0.99, 'chapter_inferred')"
    )
    for index, chapter_number in enumerate(range(2, 252), start=1):
        db.execute(
            "INSERT INTO chapter_narrative_commits "
            "(novel_id, chapter_number, content_sha256, pipeline_version, content_revision, status) "
            "VALUES (?, ?, ?, 'chapter-narrative-sync:v1', 1, ?)",
            ("novel-1", chapter_number, f"hash-{chapter_number}", "pending" if index % 2 else "failed"),
        )
        db.execute(
            "INSERT INTO triples "
            "(id, novel_id, subject, predicate, object, chapter_number, confidence, source_type) "
            "VALUES (?, 'novel-1', '甲', '传闻', ?, ?, 0.1, 'chapter_inferred')",
            (f"invisible-{index}", f"旧世界线传闻{index}", chapter_number),
        )
    db.commit()

    builder = MemoryEngine(
        llm_service=object(),
        bible_repository=SimpleNamespace(get_by_novel_id=lambda _novel_id: _fact_lock_bible()),
        db_connection=db,
    )
    fact_lock = builder.build_fact_lock_section(
        "novel-1", 800, "第800章，甲在地点A面对身份后果。"
    )
    canonical = fact_lock.split("核心 Canonical 硬事实", 1)[-1]

    assert "甲 —身份→ 真正身份" in canonical
    assert "旧世界线传闻" not in canonical
    assert canonical.count("[第") <= 40


def test_fact_lock_uses_commit_exists_without_materializing_visible_chapters(
    tmp_path, monkeypatch
):
    db = DatabaseConnection(str(tmp_path / "canonical-recall-barrier.db"))
    db.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'Novel', 'novel-1')"
    )
    db.execute(
        "INSERT INTO chapters "
        "(id, novel_id, number, title, content, content_sha256, content_revision) "
        "VALUES ('chapter-1', 'novel-1', 1, 'Chapter', '正文', 'hash-1', 1)"
    )
    db.execute(
        "INSERT INTO chapter_narrative_commits "
        "(novel_id, chapter_number, content_sha256, pipeline_version, content_revision, status) "
        "VALUES ('novel-1', 1, 'hash-1', 'chapter-narrative-sync:v1', 1, 'committed')"
    )
    db.execute(
        "INSERT INTO triples "
        "(id, novel_id, subject, predicate, object, chapter_number, confidence, source_type) "
        "VALUES ('fact-visible', 'novel-1', '甲', '状态', '已死亡', 1, 0.99, 'chapter_inferred')"
    )
    db.commit()

    import application.engine.services.worldline_generation_guard as guard

    monkeypatch.setattr(
        guard,
        "visible_committed_chapters",
        lambda *_args, **_kwargs: pytest.fail("FactLockBuilder must not materialize all chapters"),
    )
    builder = MemoryEngine(
        llm_service=object(),
        bible_repository=SimpleNamespace(get_by_novel_id=lambda _novel_id: _fact_lock_bible()),
        db_connection=db,
    )
    fact_lock = builder.build_fact_lock_section(
        "novel-1", 800, "第800章，甲在地点A处理死亡后果。"
    )
    assert "甲 —状态→ 已死亡" in fact_lock

    def fail_epoch(*_args, **_kwargs):
        raise GenerationEpochUnavailableError("generation_epoch_unavailable: barrier read failed")

    monkeypatch.setattr(guard, "active_generation_epoch", fail_epoch)
    with pytest.raises(RuntimeError, match="fact_lock_unavailable: generation_epoch_unavailable"):
        builder.build_fact_lock_section(
            "novel-1", 800, "第800章，甲在地点A处理死亡后果。"
        )


def test_fact_lock_round_robins_authority_types_under_shared_cap(tmp_path):
    db = DatabaseConnection(str(tmp_path / "canonical-recall-category-cap.db"))
    db.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'Novel', 'novel-1')"
    )
    for chapter_number, status in ((1, "committed"), (2, "failed")):
        db.execute(
            "INSERT INTO chapters "
            "(id, novel_id, number, title, content, content_sha256, content_revision) "
            "VALUES (?, 'novel-1', ?, 'Chapter', '正文', ?, 1)",
            (f"chapter-{chapter_number}", chapter_number, f"hash-{chapter_number}"),
        )
        db.execute(
            "INSERT INTO chapter_narrative_commits "
            "(novel_id, chapter_number, content_sha256, pipeline_version, content_revision, status) "
            "VALUES (?, ?, ?, 'chapter-narrative-sync:v1', 1, ?)",
            ("novel-1", chapter_number, f"hash-{chapter_number}", status),
        )
    for index in range(50):
        db.execute(
            "INSERT INTO triples "
            "(id, novel_id, subject, predicate, object, chapter_number, confidence, source_type) "
            "VALUES (?, 'novel-1', '甲', '关系', ?, 1, 0.8, 'chapter_inferred')",
            (f"fact-triple-{index}", f"关系事实{index}"),
        )
    db.execute(
        "INSERT INTO triples "
        "(id, novel_id, subject, predicate, object, chapter_number, confidence, source_type) "
        "VALUES ('fact-failed', 'novel-1', '甲', '传闻', '失败事实', 2, 0.99, 'chapter_inferred')"
    )
    db.execute(
        "INSERT INTO triples "
        "(id, novel_id, subject, predicate, object, chapter_number, confidence, source_type) "
        "VALUES ('fact-unrelated', 'novel-1', '乙', '关系', '乙的无关事实', 1, 0.99, 'chapter_inferred')"
    )
    db.execute(
        "INSERT INTO bible_timeline_notes "
        "(id, novel_id, event, time_point, description, source_type, chapter_number) "
        "VALUES ('timeline-related', 'novel-1', '甲在地点A受伤', '第1章', '甲的时间线变化', 'chapter_aftermath', 1)"
    )
    db.execute(
        "INSERT INTO causal_edges "
        "(id, novel_id, source_event_summary, source_chapter, causal_type, "
        "target_event_summary, state_change, involved_characters) "
        "VALUES ('causal-related', 'novel-1', '甲失去退路', 1, 'causes', '甲前往地点A', '甲改变目标', '[\"甲\"]')"
    )
    db.execute(
        "INSERT INTO character_states "
        "(character_id, novel_id, current_state_summary, last_updated_chapter) "
        "VALUES ('甲', 'novel-1', '甲在地点A负伤', 1)"
    )
    db.execute(
        "INSERT INTO foreshadows "
        "(id, novel_id, description, planted_chapter, status) "
        "VALUES ('foreshadow-related', 'novel-1', '甲在地点A留下未解秘密', 1, 'planted')"
    )
    db.commit()

    builder = MemoryEngine(
        llm_service=object(),
        bible_repository=SimpleNamespace(get_by_novel_id=lambda _novel_id: _fact_lock_bible()),
        db_connection=db,
    )
    fact_lock = builder.build_fact_lock_section(
        "novel-1", 800, "第800章，甲在地点A处理关系与死亡后果。"
    )
    canonical = fact_lock.split("核心 Canonical 硬事实", 1)[-1]
    fact_lines = [line for line in canonical.splitlines() if line.strip().startswith("[第")]

    assert len(fact_lines) <= 40
    assert "关系事实" in canonical
    assert "时间线：甲在地点A受伤" in canonical
    assert "因果：甲失去退路" in canonical
    assert "人物状态：甲：甲在地点A负伤" in canonical
    assert "伏笔(planted)：甲在地点A留下未解秘密" in canonical
    assert "失败事实" not in canonical
    assert "乙的无关事实" not in canonical


def test_graph_recall_excludes_canonical_triples_from_retired_generation(tmp_path):
    db = DatabaseConnection(str(tmp_path / "canonical-graph-epoch.db"))
    db.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'Novel', 'novel-1')"
    )
    db.execute(
        "INSERT INTO chapters "
        "(id, novel_id, number, title, content, content_sha256, content_revision) "
        "VALUES ('chapter-1', 'novel-1', 1, 'Chapter', '正文', 'hash-1', 1)"
    )
    db.execute(
        "INSERT INTO chapters "
        "(id, novel_id, number, title, content, content_sha256, content_revision) "
        "VALUES ('chapter-2', 'novel-1', 2, 'Chapter', '正文', 'hash-2', 1)"
    )
    db.execute(
        "INSERT INTO worldline_generation_filters (novel_id, active_generation_epoch) "
        "VALUES ('novel-1', 2)"
    )
    db.execute(
        "INSERT INTO chapter_narrative_commits "
        "(novel_id, chapter_number, content_sha256, pipeline_version, content_revision, status) "
        "VALUES "
        "('novel-1', 1, 'hash-1', 'chapter-narrative-sync:v1', 1, 'stale'), "
        "('novel-1', 2, 'hash-2', 'chapter-narrative-sync:v1', 1, 'committed')"
    )
    for triple_id, chapter_number, object_name in (
        ("triple-stale", 1, "旧世界线地点"),
        ("triple-current", 2, "新世界线地点"),
    ):
        db.execute(
            "INSERT INTO triples "
            "(id, novel_id, subject, predicate, object, chapter_number, confidence, source_type) "
            "VALUES (?, 'novel-1', '林澈', '前往', ?, ?, 0.9, 'autopilot_extract')",
            (triple_id, object_name, chapter_number),
        )
    db.commit()

    bible = SimpleNamespace(
        characters=[
            SimpleNamespace(
                name="林澈",
                character_id=SimpleNamespace(value="character-lin-che"),
            )
        ]
    )
    allocator = ContextBudgetAllocator(
        bible_repository=SimpleNamespace(get_by_novel_id=lambda _novel_id: bible),
        triple_repository=TripleRepository(db),
    )

    graph = allocator._get_graph_subnetwork("novel-1", 2, "林澈前往地点")

    assert "新世界线地点" in graph
    assert "旧世界线地点" not in graph


def test_graph_recall_fails_closed_when_generation_barrier_is_unavailable():
    class BrokenDatabase:
        def fetch_one(self, *_args, **_kwargs):
            raise OSError("worldline database is unavailable")

    class TripleRepository:
        _db = BrokenDatabase()

    allocator = ContextBudgetAllocator(triple_repository=TripleRepository())

    with pytest.raises(GenerationEpochUnavailableError, match="generation_epoch_unavailable"):
        allocator._get_graph_subnetwork("novel-1", 2, "继续上一章冲突")


def test_foreshadow_recall_fails_closed_when_generation_barrier_is_unavailable():
    class BrokenDatabase:
        def fetch_one(self, *_args, **_kwargs):
            raise OSError("worldline database is unavailable")

    class ForeshadowRepository:
        _db = BrokenDatabase()

        def get_by_novel_id(self, _novel_id):
            return SimpleNamespace(
                foreshadowings=[],
                get_t0_eligible_foreshadowings=lambda **_kwargs: [],
                get_pending_subtext_entries=lambda: [],
            )

    allocator = ContextBudgetAllocator(foreshadowing_repository=ForeshadowRepository())

    with pytest.raises(GenerationEpochUnavailableError, match="generation_epoch_unavailable"):
        allocator._get_pending_foreshadowings("novel-1", 2)


@pytest.mark.asyncio
async def test_failed_canonical_aftermath_does_not_expose_partial_facts(
    tmp_path, monkeypatch
):
    content = "林澈在苍梧城看见城门坍塌。"
    db, chapter_repo, _chapter, knowledge = _chapter_services(tmp_path, content)
    bundle = {
        "summary": "城门坍塌",
        "key_events": "林澈见证城门坍塌",
        "open_threads": "追查原因",
        "relation_triples": [
            {
                "subject": "林澈",
                "predicate": "见证",
                "object": "苍梧城城门坍塌",
                "evidence_text": "林澈在苍梧城看见城门坍塌",
            }
        ],
        "foreshadow_hints": [
            {
                "description": "城门坍塌后的追查伏笔",
                "importance": "critical",
                "evidence_text": "林澈在苍梧城看见城门坍塌",
            }
        ],
        "consumed_foreshadows": [],
        "storyline_progress": [],
        "dialogues": [],
        "timeline_events": [],
        "causal_edges": [
            {
                "source_event": "城门坍塌",
                "target_event": "追查原因",
                "state_change": "林澈开始追查",
                "evidence_text": "林澈在苍梧城看见城门坍塌",
            }
        ],
        "character_mutations": [],
        "character_states": [],
    }
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.llm_chapter_extract_bundle",
        AsyncMock(return_value=bundle),
    )

    class FailingCausalRepository:
        def save(self, _edge):
            raise RuntimeError("causal write failed")

    result = await sync_chapter_narrative_after_save(
        "novel-1",
        1,
        content,
        knowledge,
        None,
        SimpleNamespace(),
        triple_repository=TripleRepository(db),
        foreshadowing_repo=SqliteForeshadowingRepository(db),
        chapter_repository=chapter_repo,
        causal_edge_repository=FailingCausalRepository(),
    )

    assert result.commit_status == "failed"
    assert db.fetch_one("SELECT 1 FROM triples WHERE novel_id = 'novel-1'")
    commit = db.fetch_one(
        "SELECT status FROM chapter_narrative_commits "
        "WHERE novel_id = 'novel-1' AND chapter_number = 1"
    )
    assert commit["status"] == "failed"

    knowledge_view = knowledge.get_knowledge("novel-1")
    assert all(
        fact.object != "苍梧城城门坍塌"
        for fact in knowledge_view.facts
    )

    bible = SimpleNamespace(
        characters=[
            SimpleNamespace(
                name="林澈",
                relationships=[],
                description="",
                public_profile="",
                hidden_profile="",
                reveal_chapter=None,
                mental_state="NORMAL",
                is_dead=False,
                status="alive",
            )
        ],
        locations=[SimpleNamespace(name="苍梧城")],
        timeline_notes=[],
        world_settings=[],
        style_notes=[],
    )
    memory = MemoryEngine(
        llm_service=object(),
        bible_repository=SimpleNamespace(get_by_novel_id=lambda _novel_id: bible),
        db_connection=db,
    )
    fact_lock = memory.build_fact_lock_section(
        "novel-1", 2, "林澈回到苍梧城，必须处理城门坍塌。"
    )

    assert "苍梧城城门坍塌" not in fact_lock
    allocator = ContextBudgetAllocator(
        foreshadowing_repository=SqliteForeshadowingRepository(db),
    )
    assert "城门坍塌后的追查伏笔" not in allocator._get_pending_foreshadowings(
        "novel-1", 2
    )


@pytest.mark.asyncio
async def test_changed_chapter_hides_old_and_failed_revision_facts_until_new_commit(
    tmp_path, monkeypatch
):
    """A changed chapter must not expose facts from either revision.

    The canonical rows intentionally have no revision column.  Once ordinary
    chapter saving moves the source to revision 2, revision 1 must therefore
    be hidden as well as the revision-2 partial rows until revision 2 commits.
    """
    old_content = "甲在旧修订正文中失去身份。"
    new_content = "甲在新修订半成品正文中改写了选择。"
    db, chapter_repo, _chapter, knowledge = _chapter_services(tmp_path, old_content)
    monkeypatch.setattr(
        "infrastructure.persistence.database.connection.get_database",
        lambda *args, **kwargs: db,
    )

    def bundle(fact: str, *, causal: bool = False) -> dict:
        return {
            "summary": fact,
            "key_events": fact,
            "open_threads": "继续处理甲的后果",
            "relation_triples": [
                {
                    "subject": "甲",
                    "predicate": "修订状态",
                    "object": fact,
                    "evidence_text": fact,
                }
            ],
            "foreshadow_hints": [],
            "consumed_foreshadows": [],
            "storyline_progress": [],
            "dialogues": [],
            "timeline_events": [],
            "causal_edges": (
                [
                    {
                        "source_event": fact,
                        "target_event": "甲继续行动",
                        "causal_type": "causes",
                        "state_change": "甲的选择发生变化",
                        "involved_characters": ["甲"],
                        "evidence_text": fact,
                    }
                ]
                if causal
                else []
            ),
            "character_mutations": [],
            "character_states": [],
        }

    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.llm_chapter_extract_bundle",
        AsyncMock(return_value=bundle("旧修订事实")),
    )
    first = await sync_chapter_narrative_after_save(
        "novel-1",
        1,
        old_content,
        knowledge,
        None,
        SimpleNamespace(),
        triple_repository=TripleRepository(db),
        chapter_repository=chapter_repo,
        causal_edge_repository=SqliteCausalEdgeRepository(db),
    )
    assert first.commit_status == "committed", first.failure_reason

    chapter = chapter_repo.get_by_novel_and_number(NovelId("novel-1"), 1)
    ChapterRewriteCoordinator(db=db, chapter_repository=chapter_repo).rewrite(
        chapter,
        new_content,
    )
    source = db.fetch_one(
        "SELECT content_sha256, content_revision FROM chapters "
        "WHERE novel_id = 'novel-1' AND number = 1"
    )
    assert source["content_revision"] == 2

    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.llm_chapter_extract_bundle",
        AsyncMock(return_value=bundle(new_content.rstrip("。"), causal=True)),
    )

    class FailingCausalRepository:
        def save(self, _edge):
            raise RuntimeError("causal write failed")

    second = await _sync_chapter_narrative_after_save_once(
        "novel-1",
        1,
        new_content,
        knowledge,
        None,
        SimpleNamespace(),
        triple_repository=TripleRepository(db),
        chapter_repository=chapter_repo,
        causal_edge_repository=FailingCausalRepository(),
    )
    assert second.commit_status == "failed"

    commits = db.fetch_all(
        "SELECT content_revision, status FROM chapter_narrative_commits "
        "WHERE novel_id = 'novel-1' AND chapter_number = 1 "
        "ORDER BY content_revision"
    )
    assert [(row["content_revision"], row["status"]) for row in commits] == [
        (1, "stale"),
        (2, "failed"),
    ]

    knowledge_view = knowledge.get_knowledge("novel-1")
    knowledge_text = "\n".join(
        f"{fact.subject} {fact.predicate} {fact.object}"
        for fact in knowledge_view.facts
    )
    assert "旧修订事实" not in knowledge_text
    assert "新修订半成品" not in knowledge_text

    bible = _fact_lock_bible()
    memory = MemoryEngine(
        llm_service=object(),
        bible_repository=SimpleNamespace(get_by_novel_id=lambda _novel_id: bible),
        db_connection=db,
    )
    fact_lock = memory.build_fact_lock_section(
        "novel-1", 2, "第2章，甲处理新修订后的身份后果。"
    )
    assert "旧修订事实" not in fact_lock
    assert "新修订半成品" not in fact_lock

    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.llm_chapter_extract_bundle",
        AsyncMock(return_value=bundle("成功修订事实", causal=True)),
    )
    retry = await sync_chapter_narrative_after_save(
        "novel-1",
        1,
        new_content,
        knowledge,
        None,
        SimpleNamespace(),
        triple_repository=TripleRepository(db),
        chapter_repository=chapter_repo,
        causal_edge_repository=SqliteCausalEdgeRepository(db),
    )
    assert retry.commit_status == "committed", retry.failure_reason

    retry_knowledge = knowledge.get_knowledge("novel-1")
    retry_text = "\n".join(
        f"{fact.subject} {fact.predicate} {fact.object}"
        for fact in retry_knowledge.facts
    )
    assert "旧修订事实" not in retry_text
    assert "新修订半成品" not in retry_text
    assert "成功修订事实" in retry_text

    retry_fact_lock = memory.build_fact_lock_section(
        "novel-1", 2, "第2章，甲处理成功修订后的身份后果。"
    )
    assert "旧修订事实" not in retry_fact_lock
    assert "新修订半成品" not in retry_fact_lock
    assert "成功修订事实" in retry_fact_lock


@pytest.mark.asyncio
async def test_formal_canonical_facts_survive_working_memory_eviction_and_restart(
    tmp_path, monkeypatch
):
    """Permanent facts remain in authority tables after the bounded memory window moves on."""
    db_path = tmp_path / "canonical-long-run.db"
    db = DatabaseConnection(str(db_path))
    db.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-1', 'Novel', 'novel-1')"
    )
    chapter_repo = SqliteChapterRepository(db)
    knowledge = KnowledgeService(SqliteKnowledgeRepository(db))

    bible = SimpleNamespace(
        characters=[
            SimpleNamespace(
                name=name,
                description="",
                relationships=[],
                public_profile="",
                hidden_profile="",
                reveal_chapter=None,
                mental_state="NORMAL",
                is_dead=False,
                status="alive",
            )
            for name in ("林澈", "沈青", "陆宁")
        ],
        timeline_notes=[],
        world_settings=[],
        locations=[SimpleNamespace(name="苍梧城")],
        style_notes=[],
    )
    bible_repository = SimpleNamespace(get_by_novel_id=lambda _novel_id: bible)

    facts = {
        25: (
            "林澈在第25章死亡",
            {
                "relation_triples": [
                    {
                        "subject": "林澈",
                        "predicate": "状态",
                        "object": "已死亡",
                        "evidence_text": "林澈在第25章死亡",
                    }
                ],
            },
        ),
        70: (
            "沈青的真实身份在第70章揭露",
            {
                "foreshadow_hints": [
                    {
                        "description": "沈青的真实身份在第70章揭露",
                        "importance": "critical",
                        "evidence_text": "沈青的真实身份在第70章揭露",
                    }
                ],
            },
        ),
        120: (
            "沈青与陆宁在第120章从敌对转为合作",
            {
                "causal_edges": [
                    {
                        "source_event": "沈青与陆宁敌对",
                        "target_event": "沈青与陆宁合作",
                        "causal_type": "causes",
                        "state_change": "沈青与陆宁从敌对转为合作",
                        "involved_characters": ["沈青", "陆宁"],
                        "evidence_text": "沈青与陆宁在第120章从敌对转为合作",
                    }
                ],
            },
        ),
        200: (
            "苍梧城在第200章毁灭",
            {
                "timeline_events": [
                    {
                        "event": "苍梧城毁灭",
                        "time_point": "第200章",
                        "description": "苍梧城在第200章毁灭",
                        "evidence_text": "苍梧城在第200章毁灭",
                    }
                ],
            },
        ),
        350: (
            "沈青在第350章带着失去故乡的余波继续追查",
            {
                "character_mutations": [
                    {
                        "character_name": "沈青",
                        "mutation_type": "motivation",
                        "source_event": "苍梧城毁灭",
                        "impact_or_description": "带着失去故乡的余波继续追查",
                        "sensitivity_tags_or_priority": 8,
                        "evidence_text": "沈青在第350章带着失去故乡的余波继续追查",
                    }
                ],
            },
        ),
    }

    async def _extract(_llm, content, chapter_number, **_kwargs):
        evidence, extra = facts[chapter_number]
        bundle = {
            "summary": evidence,
            "key_events": evidence,
            "open_threads": "继续追查",
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
        bundle.update(extra)
        return bundle

    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.llm_chapter_extract_bundle",
        _extract,
    )
    monkeypatch.setattr(
        "infrastructure.persistence.database.connection.get_database",
        lambda *args, **kwargs: db,
    )

    for chapter_number, (evidence, _extra) in facts.items():
        chapter = Chapter(
            id=f"chapter-{chapter_number}",
            novel_id=NovelId("novel-1"),
            number=chapter_number,
            title=f"第{chapter_number}章",
            content=evidence + "。",
        )
        chapter_repo.save(chapter)
        result = await sync_chapter_narrative_after_save(
            "novel-1",
            chapter_number,
            chapter.content,
            knowledge,
            None,
            SimpleNamespace(),
            triple_repository=TripleRepository(db),
            foreshadowing_repo=SqliteForeshadowingRepository(db),
            chapter_repository=chapter_repo,
            causal_edge_repository=SqliteCausalEdgeRepository(db),
            character_state_repository=SqliteCharacterStateRepository(db),
            bible_repository=bible_repository,
        )
        assert result.commit_status == "committed", result.failure_reason

    assert db.fetch_one(
        "SELECT 1 AS found FROM triples WHERE novel_id = 'novel-1' AND chapter_number = 25"
    )
    assert db.fetch_one(
        "SELECT 1 AS found FROM foreshadows WHERE novel_id = 'novel-1' AND planted_chapter = 70"
    )
    assert db.fetch_one(
        "SELECT 1 AS found FROM causal_edges WHERE novel_id = 'novel-1' AND source_chapter = 120"
    )
    assert db.fetch_one(
        "SELECT 1 AS found FROM bible_timeline_notes "
        "WHERE novel_id = 'novel-1' AND source_type = 'chapter_aftermath' AND chapter_number = 200"
    )
    assert db.fetch_one(
        "SELECT 1 AS found FROM character_states "
        "WHERE novel_id = 'novel-1' AND last_updated_chapter = 350"
    )

    db.close()
    restarted_db = DatabaseConnection(str(db_path))
    restarted_memory = MemoryEngine(
        llm_service=object(),
        bible_repository=bible_repository,
        db_connection=restarted_db,
    )
    allocator = ContextBudgetAllocator(
        bible_repository=bible_repository,
        memory_engine=restarted_memory,
    )
    outline = (
        "林澈已经死亡；沈青的真实身份；沈青与陆宁合作；"
        "苍梧城毁灭；沈青继续追查。"
    )
    fact_lock = allocator._collect_all_slots("novel-1", 800, outline)["fact_lock"].content

    for marker in (
        "林澈",
        "沈青的真实身份",
        "从敌对转为合作",
        "苍梧城毁灭",
        "失去故乡的余波",
    ):
        assert marker in fact_lock
    assert "无关" not in fact_lock
    restarted_foreshadows = ContextBudgetAllocator(
        foreshadowing_repository=SqliteForeshadowingRepository(restarted_db),
    )._get_pending_foreshadowings("novel-1", 800)
    assert "沈青的真实身份在第70章揭露" in restarted_foreshadows
    restarted_db.close()
