"""Observability baselines for the memory-stability rollout."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from application.ai_invocation.dtos import InvocationPolicy, InvocationSessionStatus
from application.analyst.services.chapter_indexing_service import ChapterIndexingService
from application.engine.services.chapter_aftermath_pipeline import ChapterAftermathPipeline
from application.world.services.chapter_narrative_sync import (
    sync_chapter_narrative_after_save,
)
from application.world.services.knowledge_service import KnowledgeService
from domain.ai.services.llm_service import GenerationResult
from domain.ai.value_objects.prompt import Prompt
from domain.ai.value_objects.token_usage import TokenUsage
from domain.novel.entities.chapter import Chapter, ChapterStatus
from domain.novel.value_objects.novel_id import NovelId
from engine.pipeline.base import BaseStoryPipeline
from engine.pipeline.context import PipelineContext
from engine.pipeline.prose_composer import (
    ChapterProseInvocationComposer,
    ProseCompositionRequest,
    ProseCompositionResult,
)
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_chapter_repository import (
    SqliteChapterRepository,
)
from infrastructure.persistence.database.sqlite_knowledge_repository import (
    SqliteKnowledgeRepository,
)


class _CapturedAutopilotOrchestrator:
    def __init__(self):
        self.intents = []
        self.prepared = SimpleNamespace(
            session=SimpleNamespace(status=InvocationSessionStatus.COMPLETED)
        )

    async def prepare(self, intent):
        self.intents.append(intent)
        return self.prepared

    async def generate_prepared_streaming(self, **kwargs):
        return SimpleNamespace(
            accepted_content="deterministic prose",
            session_id="memory-observability-session",
            status="completed",
            next_action="succeeded",
        )


class _NoCommittedContentDatabase:
    def fetch_one(self, sql, params=()):
        return None


@pytest.mark.asyncio
async def test_story_pipeline_final_intent_contains_all_memory_tiers(monkeypatch):
    """The real ChapterProseInvocationComposer must carry all tiers to its final intent."""
    t0 = "[T0] BIBLE_LOCK: 林澈已经死亡，赤铜钥匙归沈青。"
    t1 = "[T1] EFFECTIVE_SUMMARY: 密室真相尚未公开。"
    t2 = "[T2] RECENT_BRIDGE: 沈青在雨夜带着钥匙离开钟楼。"
    t3 = "[T3] RETRIEVED_EVIDENCE: 钟楼地窖有第二道暗门。"
    orchestrator = _CapturedAutopilotOrchestrator()
    monkeypatch.setattr(
        "infrastructure.persistence.database.connection.get_database",
        lambda: _NoCommittedContentDatabase(),
    )
    monkeypatch.setattr(
        "application.ai_invocation.contracts.ensure_invocation_contract", lambda *args, **kwargs: None
    )
    monkeypatch.setattr(
        "application.ai_invocation.autopilot.factory.get_or_create_autopilot_orchestrator",
        lambda host: orchestrator,
    )
    monkeypatch.setattr(
        "application.ai_invocation.autopilot.policy.AutopilotInvocationPolicyResolver.resolve",
        lambda self, **kwargs: InvocationPolicy.DIRECT,
    )

    await ChapterProseInvocationComposer().compose(
        ProseCompositionRequest(
            novel_id="memory-observability",
            chapter_number=30,
            outline="沈青进入钟楼地窖。",
            context_text="\n".join((t0, t1, t2, t3)),
            metadata={"continuity_context": "仅章前规划用的紧凑摘要"},
            auto_approve_mode=True,
        )
    )

    assert len(orchestrator.intents) == 1
    final_context = orchestrator.intents[0].explicit_variables["continuity_context"]
    for expected in (t0, t1, t2, t3):
        assert expected in final_context


class _ControllableVectorStore:
    def __init__(self, database_path: Path, fail_chapters: set[int]):
        self.database_path = database_path
        self.fail_chapters = fail_chapters

    async def list_collections(self):
        with sqlite3.connect(self.database_path) as connection:
            return [row[0] for row in connection.execute("SELECT name FROM vector_collections")]

    async def create_collection(self, collection, dimension):
        with sqlite3.connect(self.database_path) as connection:
            connection.execute("INSERT OR IGNORE INTO vector_collections (name) VALUES (?)", (collection,))

    async def insert(self, collection, id, vector, payload):
        if payload["chapter_number"] in self.fail_chapters:
            self.fail_chapters.remove(payload["chapter_number"])
            raise RuntimeError(f"injected vector failure for chapter {payload['chapter_number']}")
        with sqlite3.connect(self.database_path) as connection:
            connection.execute(
                "INSERT OR REPLACE INTO vectors (id, payload_json) VALUES (?, ?)",
                (id, json.dumps(payload, ensure_ascii=False)),
            )

    @property
    def records(self):
        with sqlite3.connect(self.database_path) as connection:
            return {
                row[0]: json.loads(row[1])
                for row in connection.execute("SELECT id, payload_json FROM vectors")
            }


class _DeterministicEmbeddingService:
    def get_dimension(self):
        return 3

    async def embed(self, text):
        return [float(len(text)), 0.0, 1.0]


class _DeterministicLLM:
    def __init__(self):
        self.extraction_failures = 0

    async def generate(self, prompt: Prompt, config):
        body = prompt.user
        # Exhaust the canonical retry budget before the explicit recovery call.
        if "[EXTRACTION_FAIL]" in body and self.extraction_failures < 3:
            self.extraction_failures += 1
            raise RuntimeError("injected extraction failure for chapter 17")
        markers = [
            marker
            for marker in ("林澈死亡", "赤铜钥匙", "钟楼", "内鬼秘密", "钟楼暗门伏笔", "卷一结束", "卷二开始", "幕二转场")
            if marker in body
        ]
        summary = "；".join(markers) or "常规推进"
        if "第10章重写后的正文" in body:
            summary = "第10章重写后的正文已被重新抽取"
        payload = {
            "summary": summary,
            "key_events": summary,
            "open_threads": "钟楼暗门伏笔" if "钟楼暗门伏笔" in markers else "",
            "relation_triples": [
                {"subject": marker, "predicate": "已发生", "object": marker}
                for marker in markers
            ],
            "foreshadow_hints": (
                [{"description": "钟楼暗门伏笔", "suggested_resolve_offset": 5, "importance": "high"}]
                if "钟楼暗门伏笔" in markers
                else []
            ),
            "character_mutations": (
                [{"character_name": "沈青", "mutation_type": "scar", "source_event": "林澈死亡", "impact_or_description": "目睹林澈死亡", "intensity": 8}]
                if "林澈死亡" in markers
                else []
            ),
            "character_states": [],
            "causal_edges": [],
            "storyline_progress": [],
            "dialogues": [],
            "timeline_events": [],
            "plot_tension": 60,
            "emotional_tension": 60,
            "pacing_tension": 60,
        }
        return GenerationResult(json.dumps(payload, ensure_ascii=False), TokenUsage(input_tokens=3, output_tokens=3))


class _DeterministicComposer:
    async def compose(self, request):
        scenario = {
            3: "林澈死亡",
            5: "赤铜钥匙",
            7: "钟楼",
            8: "内鬼秘密",
            9: "钟楼暗门伏笔",
            10: "卷一结束",
            11: "卷二开始",
            17: "[EXTRACTION_FAIL]",
            20: "幕二转场",
        }.get(request.chapter_number, "常规推进")
        return ProseCompositionResult(content=f"第{request.chapter_number}章正文：{scenario}")


class _ChapterRepository:
    def __init__(self, db):
        self.db = db
        self._connection = db.get_connection()
        self._repository = SqliteChapterRepository(db)
        self.chapters: dict[int, Chapter] = {}

    def get_by_novel_and_number(self, novel_id, number):
        number = int(number)
        if number in self.chapters:
            return self.chapters[number]
        chapter = self._repository.get_by_novel_and_number(novel_id, number)
        if chapter is None:
            return None
        self.chapters[number] = chapter
        return chapter

    def save(self, chapter):
        self._repository.save(chapter)
        self.db.commit()
        persisted = self._repository.get_by_novel_and_number(
            chapter.novel_id,
            chapter.number,
        )
        self.chapters[chapter.number] = persisted
        self._connection.execute(
            """
            INSERT INTO chapter_snapshots (chapter_number, content, status)
            VALUES (?, ?, ?)
            ON CONFLICT(chapter_number) DO UPDATE SET content = excluded.content, status = excluded.status
            """,
            (persisted.number, persisted.content, persisted.status.value),
        )
        self._connection.commit()


class _NovelRepository:
    def __init__(self):
        self.novel = SimpleNamespace(
            current_beat_index=0,
            generation_prefs=SimpleNamespace(inline_prose_aggregation_enabled=False),
        )

    def get_by_id(self, novel_id):
        return self.novel


@dataclass
class _CurrentStoryNodeRepository:
    node: object | None = None

    async def get_by_novel(self, novel_id):
        return [self.node] if self.node is not None else []


class _ChapterWorkflow:
    def prepare_chapter_generation(self, novel_id, chapter_number, outline, scene_director=None):
        return {
            "context": f"T0 locked fact / T1 summary / T2 bridge / T3 evidence for {chapter_number}",
            "context_tokens": 16,
            "voice_anchors": "",
        }


class _SqliteKnowledgeService:
    def __init__(self, connection):
        self._connection = connection

    def get_knowledge(self, novel_id):
        return SimpleNamespace(chapters=[])

    def upsert_chapter_summary(self, novel_id, chapter_id, summary="", key_events="", open_threads="", **kwargs):
        self._connection.execute(
            "INSERT OR REPLACE INTO chapter_knowledge (chapter_number, summary, key_events, open_threads) VALUES (?, ?, ?, ?)",
            (chapter_id, summary, key_events, open_threads),
        )
        self._connection.commit()


class _SqliteTripleRepository:
    def __init__(self, connection):
        self._connection = connection
        self._kr = self

    def save_triple(self, novel_id, row):
        self._connection.execute(
            "INSERT OR REPLACE INTO extracted_triples (id, chapter_number, payload_json) VALUES (?, ?, ?)",
            (row["id"], row["chapter_number"], json.dumps(row, ensure_ascii=False)),
        )
        self._connection.commit()


class _SqliteForeshadowingRepository:
    def __init__(self, connection):
        self._connection = connection
        self._registry = None

    def get_by_novel_id(self, novel_id):
        return self._registry

    def save(self, registry):
        self._registry = registry
        for item in registry.get_unresolved():
            self._connection.execute(
                "INSERT OR IGNORE INTO extracted_foreshadows (description) VALUES (?)",
                (item.description,),
            )
        self._connection.commit()


class _SqliteCharacterStateRepository:
    def __init__(self, connection):
        self._connection = connection
        self._states = {}

    def get(self, character_id, novel_id):
        return self._states.get((str(character_id), str(novel_id)))

    def save(self, state):
        self._states[(state.character_id, state.novel_id)] = state
        self._connection.execute(
            "INSERT OR REPLACE INTO character_state_snapshots (character_id, summary) VALUES (?, ?)",
            (state.character_id, state.current_state_summary),
        )
        self._connection.commit()


class _SqliteCausalEdgeRepository:
    def __init__(self, connection):
        self._connection = connection
        self._edges = []

    def save(self, edge):
        self._edges.append(edge)

    def get_unresolved(self, novel_id):
        return [edge for edge in self._edges if not edge.is_resolved]

    def resolve(self, edge_id, chapter_number):
        for edge in self._edges:
            if edge.id == edge_id:
                edge.is_resolved = True


def _execution_plan(chapter_number: int) -> str:
    return "\n".join(
        [
            "一、开篇切入点：继续钟楼危机",
            "二、场景转换列表：钟楼、地窖",
            "三、关键对话：沈青与守门人对峙",
            f"四、剧情事件链：第{chapter_number}章推进死亡、物品、地点、秘密与伏笔",
            "五、角色关键决策：沈青保留赤铜钥匙",
            "六、爽点/反转设计：地窖暗门开启",
            "七、主角状态变化：沈青掌握新的线索",
        ]
    )


@pytest.mark.asyncio
async def test_thirty_chapter_regression_recovers_injected_vector_failure(tmp_path: Path, monkeypatch):
    """Exercise persisted extraction evidence across restart, failure, and rewrite boundaries."""
    database_path = tmp_path / "memory-stability-regression.sqlite"
    database = DatabaseConnection(str(database_path))
    connection = database.get_connection()
    database.execute(
        "INSERT INTO novels (id, title, slug) "
        "VALUES ('memory-stability', 'Memory Stability', 'memory-stability')"
    )
    connection.execute(
        "CREATE TABLE chapter_snapshots (chapter_number INTEGER PRIMARY KEY, content TEXT NOT NULL, status TEXT NOT NULL)"
    )
    connection.executescript(
        """
        CREATE TABLE chapter_knowledge (chapter_number INTEGER PRIMARY KEY, summary TEXT NOT NULL, key_events TEXT NOT NULL, open_threads TEXT NOT NULL);
        CREATE TABLE extracted_triples (id TEXT PRIMARY KEY, chapter_number INTEGER NOT NULL, payload_json TEXT NOT NULL);
        CREATE TABLE extracted_foreshadows (description TEXT PRIMARY KEY);
        CREATE TABLE character_state_snapshots (character_id TEXT PRIMARY KEY, summary TEXT NOT NULL);
        CREATE TABLE vector_collections (name TEXT PRIMARY KEY);
        CREATE TABLE vectors (id TEXT PRIMARY KEY, payload_json TEXT NOT NULL);
        CREATE TABLE aftermath_calls (stage TEXT NOT NULL, chapter_number INTEGER NOT NULL);
        """
    )
    connection.commit()

    def _build_runtime(active_database, active_connection, active_chapter_repository):
        vector_store = _ControllableVectorStore(database_path, fail_chapters={12})
        llm = _DeterministicLLM()
        knowledge = KnowledgeService(SqliteKnowledgeRepository(active_database))
        aftermath = ChapterAftermathPipeline(
            knowledge_service=knowledge,
            chapter_indexing_service=ChapterIndexingService(vector_store, _DeterministicEmbeddingService()),
            llm_service=llm,
            triple_repository=_SqliteTripleRepository(active_connection),
            foreshadowing_repository=_SqliteForeshadowingRepository(active_connection),
            causal_edge_repository=_SqliteCausalEdgeRepository(active_connection),
            character_state_repository=_SqliteCharacterStateRepository(active_connection),
            chapter_repository=active_chapter_repository,
        )
        return vector_store, llm, aftermath, knowledge

    async def _observed_bridge(self, novel_id, chapter_number, content):
        connection.execute("INSERT INTO aftermath_calls (stage, chapter_number) VALUES ('bridge', ?)", (chapter_number,))
        connection.commit()

    async def _observed_auxiliary(self, novel_id, chapter_number, content, evidence):
        connection.execute("INSERT INTO aftermath_calls (stage, chapter_number) VALUES ('auxiliary', ?)", (chapter_number,))
        connection.commit()

    monkeypatch.setattr(ChapterAftermathPipeline, "_extract_chapter_bridge", _observed_bridge)
    monkeypatch.setattr(ChapterAftermathPipeline, "_run_auxiliary_stages", _observed_auxiliary)

    chapter_repository = _ChapterRepository(database)
    vector_store, llm, aftermath, knowledge = _build_runtime(
        database,
        connection,
        chapter_repository,
    )
    story_node_repository = _CurrentStoryNodeRepository()
    novel_repository = _NovelRepository()
    pipeline = BaseStoryPipeline()
    for chapter_number in range(1, 31):
        if chapter_number == 10:
            database.close()
            database = DatabaseConnection(str(database_path))
            connection = database.get_connection()
            chapter_repository = _ChapterRepository(database)
            story_node_repository = _CurrentStoryNodeRepository()
            novel_repository = _NovelRepository()
            vector_store, llm, aftermath, knowledge = _build_runtime(
                database,
                connection,
                chapter_repository,
            )
            pipeline = BaseStoryPipeline()
        story_node_repository.node = SimpleNamespace(
            node_type=SimpleNamespace(value="chapter"),
            number=chapter_number,
            title=f"第{chapter_number}章",
            outline=_execution_plan(chapter_number),
            description="",
            metadata={},
        )
        context = PipelineContext(novel_id="memory-stability", auto_approve_mode=True)
        context.inject(
            novel_repository=novel_repository,
            chapter_repository=chapter_repository,
            story_node_repo=story_node_repository,
            chapter_workflow=_ChapterWorkflow(),
            prose_composer=_DeterministicComposer(),
            llm_service=llm,
            aftermath_pipeline=aftermath,
        )
        result = await pipeline.run_chapter(context)
        if chapter_number == 17:
            assert result.success is False
            recovered_content = f"{context.chapter_content}\n人工修复后的补充。"
            chapter = chapter_repository.chapters[chapter_number]
            chapter.update_content(recovered_content)
            chapter_repository.save(chapter)
            recovered = await sync_chapter_narrative_after_save(
                "memory-stability",
                chapter_number,
                recovered_content,
                knowledge,
                aftermath._indexing,
                llm,
                triple_repository=aftermath._triple_repository,
                foreshadowing_repo=aftermath._foreshadowing_repository,
                chapter_repository=chapter_repository,
                causal_edge_repository=aftermath._causal_edge_repository,
                character_state_repository=aftermath._character_state_repository,
            )
            assert recovered["narrative_sync_ok"] is True
            assert recovered["content_revision"] == 2
            assert recovered["attempt_count"] == 1
        else:
            assert result.success
        assert chapter_repository.chapters[chapter_number].status == ChapterStatus.COMPLETED
        if chapter_number == 17:
            assert llm.extraction_failures == 3

        if chapter_number == 20:
            chapter_repository.chapters[10].update_content("第10章重写后的正文")
            chapter_repository.save(chapter_repository.chapters[10])
            rewrite_result = await aftermath.run_after_chapter_saved(
                "memory-stability", 10, "第10章重写后的正文"
            )
            assert rewrite_result["vector_stored"] is True

        if chapter_number == 12:
            vector_recovery = await sync_chapter_narrative_after_save(
                "memory-stability",
                chapter_number,
                context.chapter_content,
                knowledge,
                aftermath._indexing,
                llm,
                chapter_repository=chapter_repository,
            )
            assert vector_recovery["vector_stored"] is True

    await aftermath.drain_auxiliary_stages()
    triple_payloads = [row[0] for row in connection.execute("SELECT payload_json FROM extracted_triples")]
    persisted_chapters = connection.execute("SELECT chapter_number, content FROM chapter_snapshots").fetchall()
    foreshadows = {row[0] for row in connection.execute("SELECT description FROM extracted_foreshadows")}
    aftermath_calls = connection.execute("SELECT stage, chapter_number FROM aftermath_calls").fetchall()
    database.close()

    assert database_path.exists()
    assert len(persisted_chapters) == 30
    assert dict(persisted_chapters)[10] == "第10章重写后的正文"
    assert not any('"chapter_number": 17' in payload for payload in triple_payloads)
    persisted_text = "\n".join(triple_payloads)
    for marker in ("林澈死亡", "赤铜钥匙", "钟楼", "内鬼秘密", "卷一结束", "卷二开始", "幕二转场"):
        assert marker in persisted_text
    assert "钟楼暗门伏笔" in foreshadows
    assert len([item for item in aftermath_calls if item[0] == "bridge"]) == 31
    assert len([item for item in aftermath_calls if item[0] == "auxiliary"]) == 31
    assert vector_store.records["memory-stability_ch10_summary"]["text"] == "第10章重写后的正文已被重新抽取"
    assert "memory-stability_ch12_summary" in vector_store.records
