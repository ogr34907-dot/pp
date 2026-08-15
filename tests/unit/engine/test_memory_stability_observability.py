"""Observability baselines for the memory-stability rollout."""
from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from pathlib import Path
from types import SimpleNamespace

import pytest

from application.ai_invocation.dtos import InvocationPolicy, InvocationSessionStatus
from application.analyst.services.chapter_indexing_service import ChapterIndexingService
from application.core.services.chapter_rewrite_coordinator import ChapterRewriteCoordinator
from application.engine.services.chapter_aftermath_pipeline import ChapterAftermathPipeline
from application.engine.services.context_budget_allocator import ContextBudgetAllocator
from application.engine.services.candidate_chapter_workflow import CandidateChapterWorkflowService
from application.engine.services.memory_engine import CompletedBeatItem, MemoryEngine
from application.engine.services.memory_engine_settings import MemoryEngineRuntimeSettings
from application.engine.services.worldline_rebuild_service import WorldlineRebuildService
from application.world.services.chapter_narrative_sync import (
    sync_chapter_narrative_after_save,
)
from application.world.services.knowledge_service import KnowledgeService
from domain.ai.services.llm_service import GenerationResult
from domain.ai.value_objects.prompt import Prompt
from domain.ai.value_objects.token_usage import TokenUsage
from domain.novel.candidate_chapter import GenerationRunState, RunMode
from domain.novel.value_objects.novel_id import NovelId
from engine.pipeline.prose_composer import (
    ChapterProseInvocationComposer,
    ProseCompositionRequest,
    ProseCompositionResult,
)
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.sqlite_chapter_repository import (
    SqliteChapterRepository,
)
from infrastructure.persistence.database.sqlite_novel_repository import SqliteNovelRepository
from infrastructure.persistence.database.sqlite_knowledge_repository import (
    SqliteKnowledgeRepository,
)
from infrastructure.persistence.database.chapter_candidate_repository import (
    ChapterCandidateRepository,
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
                                "characters_involved": [],
                                "evidence_text": (
                                    f"第{chapter_number}章完成的记忆节拍"
                                    + (f"；{durable_suffix}" if durable_suffix else "")
                                ),
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
                                "evidence_text": (
                                    f"第{chapter_number}章揭露的记忆线索"
                                    + (f"；{durable_suffix}" if durable_suffix else "")
                                ),
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
                    "subject": "本章",
                    "predicate": "出现",
                    "object": marker,
                    "evidence_text": f"本章出现{marker}",
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
        durable_suffix = "；".join(
            marker
            for marker in (
                "沈青得知内鬼秘密",
                "林澈死亡",
                "沈青与陆宁从敌对转为合作",
                "钟楼暗门伏笔",
                "苍梧城毁灭",
            )
            if marker in scenario
        )
        return ProseCompositionResult(
            content=(
                f"第{request.chapter_number}章正文：本章出现{scenario}；"
                f"第{request.chapter_number}章完成的记忆节拍"
                + (f"；{durable_suffix}" if durable_suffix else "")
                + f"；第{request.chapter_number}章揭露的记忆线索"
                + (f"；{durable_suffix}" if durable_suffix else "")
            )
        )


class _CandidateOutlineService:
    """Published five-level projection used by the real candidate workflow."""

    def next_published_chapter_context(self, novel_id: str, *, after_chapter: int):
        chapter_number = after_chapter + 1
        title = f"第{chapter_number}章"
        return SimpleNamespace(number=chapter_number, title=title), {
            "outline": {"contract_id": "outline", "digest": "outline-v1", "payload": {}},
            "part": {"contract_id": "part", "digest": "part-v1", "payload": {}},
            "volume": {"contract_id": "volume", "digest": "volume-v1", "payload": {}},
            "act": {"contract_id": "act", "digest": "act-v1", "payload": {}},
            "chapter": {
                "contract_id": f"chapter-{chapter_number}",
                "digest": f"chapter-{chapter_number}-v1",
                "payload": {
                    "title": title,
                    "creative_goal": "推进钟楼危机并留下下一章钩子",
                    "entry_state": "沈青仍在追查钟楼秘密",
                    "exit_state": "沈青获得新的危机线索",
                    "required_events": [],
                    "forbidden_events": [],
                    "handoff_conditions": ["危机仍未解除"],
                },
            },
        }


class _CandidateDraftGenerator:
    def __init__(self, allocator: ContextBudgetAllocator, captured_contexts: dict[int, str]):
        self._allocator = allocator
        self._captured_contexts = captured_contexts
        self._composer = _DeterministicComposer()

    async def generate_candidate_draft(
        self,
        *,
        novel_id: str,
        chapter_number: int,
        chapter_title: str,
        outline_text: str,
        **_kwargs,
    ) -> dict[str, str]:
        allocation = self._allocator.allocate(
            novel_id=novel_id,
            chapter_number=chapter_number,
            outline=outline_text,
            total_budget=12000,
        )
        context = allocation.get_final_context()
        self._captured_contexts[chapter_number] = context
        composed = await self._composer.compose(
            ProseCompositionRequest(
                novel_id=novel_id,
                chapter_number=chapter_number,
                chapter_title=chapter_title,
                outline=outline_text,
                context_text=context,
                metadata={"continuity_context": context},
                auto_approve_mode=False,
            )
        )
        return {"content": composed.content, "script": f"第{chapter_number}章剧本"}


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
        "INSERT INTO novels (id, title, slug, target_chapters) "
        "VALUES ('memory-stability', 'Memory Stability', 'memory-stability', ?)",
        (chapter_count,),
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
            novel_repository=SqliteNovelRepository(active_database),
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

    chapter_repository = SqliteChapterRepository(database)
    candidate_repository = ChapterCandidateRepository(database)
    candidate_repository.start_run(
        "memory-stability", run_mode=RunMode.CHAPTER_REVIEW
    )
    vector_store, llm, memory_engine, allocator, aftermath, knowledge = _build_runtime(
        database,
        connection,
        chapter_repository,
    )
    draft_generator = _CandidateDraftGenerator(allocator, captured_contexts)
    workflow = CandidateChapterWorkflowService(
        candidate_repository,
        _CandidateOutlineService(),
        draft_generator,
        aftermath,
    )
    for chapter_number in range(1, chapter_count + 1):
        if chapter_number == 10:
            await aftermath.drain_auxiliary_stages()
            database.close()
            database = DatabaseConnection(str(database_path))
            connection = database.get_connection()
            chapter_repository = SqliteChapterRepository(database)
            candidate_repository = ChapterCandidateRepository(database)
            vector_store, llm, memory_engine, allocator, aftermath, knowledge = _build_runtime(
                database,
                connection,
                chapter_repository,
            )
            draft_generator = _CandidateDraftGenerator(allocator, captured_contexts)
            workflow = CandidateChapterWorkflowService(
                candidate_repository,
                _CandidateOutlineService(),
                draft_generator,
                aftermath,
            )
        candidate = await workflow.generate_next("memory-stability")
        assert candidate is not None
        committed = await workflow.accept_candidate(
            candidate.id, continue_after_commit=True
        )
        if chapter_number == 12:
            assert committed.status.value == "committed"
            recovered = await sync_chapter_narrative_after_save(
                "memory-stability",
                chapter_number,
                candidate.final_content,
                knowledge,
                aftermath._indexing,
                llm,
                chapter_repository=chapter_repository,
            )
            assert recovered["narrative_sync_ok"] is True
            assert recovered["vector_stored"] is True
        if chapter_number == 17:
            assert committed.status.value == "failed"
            failed_bridge_count = connection.execute(
                "SELECT COUNT(*) FROM aftermath_calls "
                "WHERE stage = 'bridge' AND chapter_number = 17"
            ).fetchone()[0]
            assert failed_bridge_count == 0
            chapter = chapter_repository.get_by_novel_and_number(
                NovelId("memory-stability"), chapter_number
            )
            recovered_content = f"{chapter.content}\n人工修复后的补充。"
            persisted_recovery_chapter = ChapterRewriteCoordinator(
                db=database,
                chapter_repository=chapter_repository,
            ).rewrite(chapter, recovered_content).chapter
            assert persisted_recovery_chapter.content_sha256 == hashlib.sha256(
                recovered_content.encode("utf-8")
            ).hexdigest()
            assert persisted_recovery_chapter.content_revision == 2
            rebuilt = await WorldlineRebuildService(database, aftermath).rebuild(
                "memory-stability"
            )
            assert rebuilt["status"] == "completed"
            recovered_bridge_count = connection.execute(
                "SELECT COUNT(*) FROM aftermath_calls "
                "WHERE stage = 'bridge' AND chapter_number = 17"
            ).fetchone()[0]
            assert recovered_bridge_count == 1
            candidate_repository.start_run(
                "memory-stability", run_mode=RunMode.CHAPTER_REVIEW
            )
        else:
            assert committed.status.value == "committed"
        formal_chapter = chapter_repository.get_by_novel_and_number(
            NovelId("memory-stability"), chapter_number
        )
        assert formal_chapter is not None and formal_chapter.status.value == "completed"
        if chapter_number == 17:
            assert llm.extraction_failures == 3

        if chapter_number == 20:
            chapter_ten = chapter_repository.get_by_novel_and_number(
                NovelId("memory-stability"), 10
            )
            ChapterRewriteCoordinator(
                db=database,
                chapter_repository=chapter_repository,
            ).rewrite(chapter_ten, "第10章重写后的正文")
            rebuilt = await WorldlineRebuildService(database, aftermath).rebuild(
                "memory-stability"
            )
            assert rebuilt["status"] == "completed"
            candidate_repository.start_run(
                "memory-stability", run_mode=RunMode.CHAPTER_REVIEW
            )

    await aftermath.drain_auxiliary_stages()
    triple_payloads = [row[0] for row in connection.execute("SELECT payload_json FROM extracted_triples")]
    persisted_chapters = connection.execute(
        "SELECT number, content FROM chapters "
        "WHERE novel_id = ? AND status = 'completed' ORDER BY number",
        ("memory-stability",),
    ).fetchall()
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
    expected_bridge_numbers = list(range(1, chapter_count + 1))
    if chapter_count >= 17:
        expected_bridge_numbers = list(range(1, 17))
        expected_bridge_numbers.extend(range(1, 18))
        if chapter_count >= 20:
            expected_bridge_numbers.extend(range(18, 21))
            expected_bridge_numbers.extend(range(1, 21))
            expected_bridge_numbers.extend(range(21, chapter_count + 1))
        else:
            expected_bridge_numbers.extend(range(18, chapter_count + 1))
    assert [row[1] for row in bridge_calls] == expected_bridge_numbers
    expected_auxiliary_numbers = list(expected_bridge_numbers)
    if chapter_count >= 20:
        # The chapter-17 rebuild queues a now-obsolete chapter-10 auxiliary
        # job.  The chapter-20 rewrite changes that formal version before the
        # queue reaches it, so the version guard must discard it.
        expected_auxiliary_numbers.pop(16 + 9)
    assert [row[1] for row in auxiliary_calls] == expected_auxiliary_numbers
    if chapter_count >= 17:
        assert len([item for item in bridge_calls if item[1] == 17]) == 2
        assert len([item for item in auxiliary_calls if item[1] == 17]) == 2
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


@pytest.mark.asyncio
@pytest.mark.parametrize("target_chapters", [300, 500, 800])
async def test_completed_beats_window_does_not_control_novel_completion_target(
    tmp_path: Path, target_chapters: int
):
    """The fixed working-memory window must not decide when a run completes."""
    db = DatabaseConnection(str(tmp_path / f"target-{target_chapters}.db"))
    db.execute(
        "INSERT INTO novels (id, title, slug, target_chapters) VALUES (?, ?, ?, ?)",
        ("novel-1", "Novel", "novel-1", target_chapters),
    )
    db.commit()

    repository = ChapterCandidateRepository(db)
    run = repository.start_run(
        "novel-1",
        run_mode=RunMode.CHAPTER_REVIEW,
        target_chapters=target_chapters + 1,
    )
    assert run.target_chapters == target_chapters
    db.execute(
        "UPDATE novel_generation_runs SET current_formal_chapter = ? WHERE novel_id = ?",
        (target_chapters, "novel-1"),
    )
    db.commit()

    memory = MemoryEngine(
        llm_service=object(),
        bible_repository=SimpleNamespace(
            get_by_novel_id=lambda _novel_id: SimpleNamespace(
                characters=[], locations=[], timeline_notes=[], world_settings=[], style_notes=[]
            )
        ),
        db_connection=db,
    )
    state = memory._get_or_load_state("novel-1")
    memory._merge_beats(
        state,
        [
            CompletedBeatItem(
                beat_id=f"beat-{chapter}",
                summary=f"无关工作记忆{chapter}",
                chapter=chapter,
            )
            for chapter in range(1, 506)
        ],
        chapter=505,
    )
    assert len(state.completed_beats) == 500

    workflow = CandidateChapterWorkflowService(repository, None, None, None)
    assert await workflow.generate_next("novel-1") is None
    assert repository.get_run("novel-1").state == GenerationRunState.COMPLETED
