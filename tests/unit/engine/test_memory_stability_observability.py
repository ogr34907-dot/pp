"""Observability baselines for the memory-stability rollout."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from application.ai_invocation.dtos import InvocationPolicy, InvocationSessionStatus
from application.analyst.services.chapter_indexing_service import ChapterIndexingService
from application.engine.services.chapter_aftermath_pipeline import ChapterAftermathPipeline
from application.engine.services.context_budget_allocator import ContextBudgetAllocator
from application.engine.services.memory_engine import MemoryEngine
from application.engine.services.memory_engine_settings import MemoryEngineRuntimeSettings
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
        memory_chapter = re.search(r"第\s*(\d+)\s*章", body)
        if "【待分析的章节】" in body and memory_chapter:
            chapter_number = int(memory_chapter.group(1))
            chapter_content_match = re.search(
                r"正文如下：\s*(.*?)(?:\n━━━|\Z)",
                body,
                flags=re.DOTALL,
            )
            chapter_content = chapter_content_match.group(1) if chapter_content_match else body
            durable_markers = [
                marker
                for marker in (
                    "沈青得知内鬼秘密",
                    "林澈死亡",
                    "沈青与陆宁从敌对转为合作",
                    "钟楼暗门伏笔",
                    "苍梧城毁灭",
                )
                if marker in chapter_content
            ]
            durable_suffix = "；".join(durable_markers)
            return GenerationResult(
                json.dumps(
                    {
                        "completed_beats": [
                            {
                                "beat_id": f"ch{chapter_number}-durable-beat",
                                "summary": (
                                    f"第{chapter_number}章完成的记忆节拍"
                                    + (f"；{durable_suffix}" if durable_suffix else "")
                                ),
                                "chapter": chapter_number,
                                "characters_involved": ["沈青"],
                            }
                        ],
                        "revealed_clues": [
                            {
                                "clue_id": f"ch{chapter_number}-durable-clue",
                                "content": (
                                    f"第{chapter_number}章揭露的记忆线索"
                                    + (f"；{durable_suffix}" if durable_suffix else "")
                                ),
                                "revealed_at_chapter": chapter_number,
                                "category": "truth",
                                "is_still_valid": True,
                            }
                        ],
                        "fact_violations": [],
                    },
                    ensure_ascii=False,
                ),
                TokenUsage(input_tokens=3, output_tokens=3),
            )
        # Exhaust the canonical retry budget before the explicit recovery call.
        if "[EXTRACTION_FAIL]" in body and self.extraction_failures < 3:
            self.extraction_failures += 1
            raise RuntimeError("injected extraction failure for chapter 17")
        markers = [
            marker
            for marker in (
                "林澈死亡",
                "赤铜钥匙",
                "钟楼",
                "内鬼秘密",
                "钟楼暗门伏笔",
                "卷一结束",
                "卷二开始",
                "幕二转场",
                "沈青得知内鬼秘密",
                "沈青与陆宁从敌对转为合作",
                "苍梧城毁灭",
            )
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
                {
                    "subject": marker,
                    "predicate": "已发生",
                    "object": marker,
                    "evidence_text": marker,
                }
                for marker in markers
            ],
            "foreshadow_hints": (
                [
                    {
                        "description": "钟楼暗门伏笔",
                        "suggested_resolve_offset": 5,
                        "importance": "high",
                        "evidence_text": "钟楼暗门伏笔",
                    }
                ]
                if "钟楼暗门伏笔" in markers
                else []
            ),
            "character_mutations": (
                [
                    {
                        "character_name": "沈青",
                        "mutation_type": "scar",
                        "source_event": "林澈死亡",
                        "impact_or_description": "目睹林澈死亡",
                        "intensity": 8,
                        "evidence_text": "林澈死亡",
                    }
                ]
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
            25: "沈青得知内鬼秘密",
            70: "林澈死亡",
            120: "沈青与陆宁从敌对转为合作",
            200: "钟楼暗门伏笔",
            350: "苍梧城毁灭",
        }.get(request.chapter_number, "常规推进")
        return ProseCompositionResult(
            content=(
                f"第{request.chapter_number}章正文：{scenario}；"
                f"记忆节拍-{request.chapter_number}；"
                f"记忆线索-{request.chapter_number}"
            )
        )


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

    def list_by_novel(self, novel_id):
        chapters = self._repository.list_by_novel(novel_id)
        for chapter in chapters:
            self.chapters[chapter.number] = chapter
        return sorted(self.chapters.values(), key=lambda chapter: chapter.number)

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
    def __init__(self, target_chapters: int):
        self.novel = SimpleNamespace(
            target_chapters=target_chapters,
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
    def __init__(self, allocator: ContextBudgetAllocator | None = None, captured_contexts=None):
        self.context_builder = (
            SimpleNamespace(budget_allocator=allocator) if allocator is not None else None
        )
        self._allocator = allocator
        self._captured_contexts = captured_contexts

    def prepare_chapter_generation(self, novel_id, chapter_number, outline, scene_director=None):
        if self._allocator is not None:
            allocation = self._allocator.allocate(
                novel_id=novel_id,
                chapter_number=chapter_number,
                outline=outline,
                total_budget=12000,
            )
            context = allocation.get_final_context()
            if self._captured_contexts is not None:
                self._captured_contexts[chapter_number] = context
            return {
                "context": context,
                "context_tokens": self._allocator.estimate_tokens(context),
                "context_budget_tokens": 12000,
                "voice_anchors": "",
            }
        return {
            "context": f"T0 locked fact / T1 summary / T2 bridge / T3 evidence for {chapter_number}",
            "context_tokens": 16,
            "voice_anchors": "",
        }


class _MemoryBibleRepository:
    def __init__(self):
        self._bible = SimpleNamespace(
            characters=[
                SimpleNamespace(
                    character_id=SimpleNamespace(value="lin-che"),
                    name="林澈",
                    description="林澈的父亲已经死亡。",
                    status="",
                    is_dead=False,
                    relationships=[],
                    public_profile="",
                    hidden_profile="",
                    reveal_chapter=None,
                    mental_state="NORMAL",
                ),
                SimpleNamespace(
                    character_id=SimpleNamespace(value="shen-qing"),
                    name="沈青",
                    description="",
                    status="alive",
                    is_dead=False,
                    relationships=[],
                    public_profile="",
                    hidden_profile="",
                    reveal_chapter=None,
                    mental_state="NORMAL",
                ),
            ],
            timeline_notes=[],
            world_settings=[],
            style_notes=[],
        )

    def get_by_novel_id(self, novel_id):
        return self._bible


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


async def _run_memory_stability_regression(
    tmp_path: Path,
    monkeypatch,
    *,
    chapter_count: int,
) -> dict[int, str]:
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

    bible_repository = _MemoryBibleRepository()
    captured_contexts: dict[int, str] = {}

    def _build_runtime(active_database, active_connection, active_chapter_repository):
        vector_store = _ControllableVectorStore(database_path, fail_chapters={12})
        llm = _DeterministicLLM()
        memory_engine = MemoryEngine(
            llm_service=llm,
            bible_repository=bible_repository,
            db_connection=active_database,
            runtime_settings=MemoryEngineRuntimeSettings(
                state_cache_ttl_seconds=0,
                state_cache_max_size=0,
            ),
        )
        allocator = ContextBudgetAllocator(
            chapter_repository=active_chapter_repository,
            bible_repository=bible_repository,
            memory_engine=memory_engine,
        )
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
            memory_engine=memory_engine,
        )
        return vector_store, llm, memory_engine, allocator, aftermath, knowledge

    async def _observed_bridge(self, novel_id, chapter_number, content):
        connection.execute("INSERT INTO aftermath_calls (stage, chapter_number) VALUES ('bridge', ?)", (chapter_number,))
        connection.commit()

    async def _observed_auxiliary(self, novel_id, chapter_number, content, evidence):
        connection.execute("INSERT INTO aftermath_calls (stage, chapter_number) VALUES ('auxiliary', ?)", (chapter_number,))
        connection.commit()

    monkeypatch.setattr(ChapterAftermathPipeline, "_extract_chapter_bridge", _observed_bridge)
    monkeypatch.setattr(ChapterAftermathPipeline, "_run_auxiliary_stages", _observed_auxiliary)

    chapter_repository = _ChapterRepository(database)
    vector_store, llm, memory_engine, allocator, aftermath, knowledge = _build_runtime(
        database,
        connection,
        chapter_repository,
    )
    story_node_repository = _CurrentStoryNodeRepository()
    novel_repository = _NovelRepository(chapter_count)
    pipeline = BaseStoryPipeline()
    for chapter_number in range(1, chapter_count + 1):
        if chapter_number == 10:
            database.close()
            database = DatabaseConnection(str(database_path))
            connection = database.get_connection()
            chapter_repository = _ChapterRepository(database)
            story_node_repository = _CurrentStoryNodeRepository()
            novel_repository = _NovelRepository(chapter_count)
            vector_store, llm, memory_engine, allocator, aftermath, knowledge = _build_runtime(
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
            chapter_workflow=_ChapterWorkflow(allocator, captured_contexts),
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
            persisted_recovery_chapter = chapter_repository.chapters[chapter_number]
            assert persisted_recovery_chapter.content_sha256 == hashlib.sha256(
                recovered_content.encode("utf-8")
            ).hexdigest()
            assert persisted_recovery_chapter.content_revision == 2
            recovered = await aftermath.run_after_chapter_saved(
                "memory-stability",
                chapter_number,
                recovered_content,
                expected_content_sha256=persisted_recovery_chapter.content_sha256,
                expected_content_revision=persisted_recovery_chapter.content_revision,
            )
            assert recovered["narrative_sync_ok"] is True
            assert recovered["content_revision"] == 2
            assert recovered["attempt_count"] == 1
            assert recovered["auxiliary_deferred"] is True
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
    memory_state = connection.execute(
        "SELECT state_json, last_updated_chapter FROM memory_engine_state WHERE novel_id = ?",
        ("memory-stability",),
    ).fetchone()
    database.close()

    assert database_path.exists()
    assert len(persisted_chapters) == chapter_count
    assert {row[0] for row in persisted_chapters} == set(range(1, chapter_count + 1))
    assert memory_state is not None
    memory_payload = json.loads(memory_state[0])
    assert memory_state[1] == chapter_count
    assert f"ch{chapter_count}-durable-beat" in {
        beat["beat_id"] for beat in memory_payload["completed_beats"]
    }
    assert f"ch{chapter_count}-durable-clue" in {
        clue["clue_id"] for clue in memory_payload["revealed_clues"]
    }
    if chapter_count >= 17:
        assert not any('"chapter_number": 17' in payload for payload in triple_payloads)
    persisted_text = "\n".join(triple_payloads)
    expected_markers = {
        3: "林澈死亡",
        5: "赤铜钥匙",
        7: "钟楼",
        8: "内鬼秘密",
        10: "卷一结束",
        11: "卷二开始",
        20: "幕二转场",
        25: "沈青得知内鬼秘密",
        70: "林澈死亡",
        120: "沈青与陆宁从敌对转为合作",
        200: "钟楼暗门伏笔",
        350: "苍梧城毁灭",
    }
    for chapter_number, marker in expected_markers.items():
        if chapter_number <= chapter_count:
            assert marker in persisted_text
    if chapter_count >= 9:
        assert "钟楼暗门伏笔" in foreshadows
    bridge_calls = [item for item in aftermath_calls if item[0] == "bridge"]
    auxiliary_calls = [item for item in aftermath_calls if item[0] == "auxiliary"]
    assert len(bridge_calls) == chapter_count + (chapter_count >= 17) + (chapter_count >= 20)
    assert len(auxiliary_calls) == chapter_count + (chapter_count >= 17)
    if chapter_count >= 17:
        assert len([item for item in bridge_calls if item[1] == 17]) == 2
        assert len([item for item in auxiliary_calls if item[1] == 17]) == 1
    if chapter_count >= 20:
        assert dict(persisted_chapters)[10] == "第10章重写后的正文"
        assert vector_store.records["memory-stability_ch10_summary"]["text"] == "第10章重写后的正文已被重新抽取"
    if chapter_count >= 12:
        assert "memory-stability_ch12_summary" in vector_store.records

    return captured_contexts, memory_payload


@pytest.mark.asyncio
async def test_default_story_pipeline_persists_memory_and_evolves_next_context(tmp_path: Path, monkeypatch):
    """The default pipeline carries chapter-one memory into chapter two's real context."""
    contexts, _memory_payload = await _run_memory_stability_regression(
        tmp_path,
        monkeypatch,
        chapter_count=2,
    )

    assert "第1章完成的记忆节拍" in contexts[2]
    assert "第1章揭露的记忆线索" in contexts[2]
    assert "禁止: 林澈(" not in contexts[2]


@pytest.mark.asyncio
async def test_thirty_chapter_regression_recovers_injected_vector_failure(tmp_path: Path, monkeypatch):
    """Exercise persisted extraction evidence across restart, failure, and rewrite boundaries."""
    await _run_memory_stability_regression(
        tmp_path,
        monkeypatch,
        chapter_count=30,
    )


@pytest.mark.asyncio
@pytest.mark.slow
async def test_hundred_chapter_memory_stability_regression(tmp_path: Path, monkeypatch):
    """Run 100 real pipeline iterations with durable memory and evolving context."""
    contexts, _memory_payload = await _run_memory_stability_regression(
        tmp_path,
        monkeypatch,
        chapter_count=100,
    )

    assert "第99章完成的记忆节拍" in contexts[100]
    assert "第99章揭露的记忆线索" in contexts[100]
    assert "禁止: 林澈(" not in contexts[100]


@pytest.mark.asyncio
@pytest.mark.slow
@pytest.mark.parametrize("chapter_count", [300, 500, 800])
async def test_long_run_memory_stability_preserves_durable_story_facts(
    tmp_path: Path,
    monkeypatch,
    chapter_count: int,
):
    """Check the same persisted Canonical chain at every required long-run stop."""
    contexts, memory_payload = await _run_memory_stability_regression(
        tmp_path,
        monkeypatch,
        chapter_count=chapter_count,
    )

    current_context = contexts[chapter_count]
    assert f"第{chapter_count - 1}章完成的记忆节拍" in current_context
    assert f"第{chapter_count - 1}章揭露的记忆线索" in current_context
    durable_memory = "\n".join(
        [
            *(str(beat.get("summary") or "") for beat in memory_payload["completed_beats"]),
            *(str(clue.get("content") or "") for clue in memory_payload["revealed_clues"]),
        ]
    )
    for marker in (
        "林澈死亡",
        "沈青得知内鬼秘密",
        "沈青与陆宁从敌对转为合作",
    ):
        assert marker in durable_memory
    if chapter_count >= 500:
        assert "钟楼暗门伏笔" in durable_memory
        assert "苍梧城毁灭" in durable_memory
