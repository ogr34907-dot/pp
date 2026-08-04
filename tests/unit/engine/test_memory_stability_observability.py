"""Observability baselines for the memory-stability rollout.

The strict xfail below is intentional: it preserves the recovery contract that
later memory work must satisfy without pretending the current implementation
already recovers a failed vector write.
"""
from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import pytest

from application.ai_invocation.dtos import InvocationPolicy, InvocationRequest, InvocationSpec, VariableBinding
from application.ai_invocation.gateway import AIInvocationGateway
from application.ai_invocation.prompt_assembler import CPMSPromptAssembler
from application.ai_invocation.services import InvocationSessionService
from application.ai_invocation.spec_service import InMemoryInvocationSpecRepository, InvocationSpecService
from application.ai_invocation.variable_hub import InMemoryVariableHubRepository, VariableResolver
from application.analyst.services.chapter_indexing_service import ChapterIndexingService
from application.engine.services.chapter_aftermath_pipeline import ChapterAftermathPipeline
from application.workflows.auto_novel_generation_workflow import assemble_chapter_bundle_context_text
from domain.ai.services.llm_service import GenerationResult
from domain.ai.value_objects.prompt import Prompt
from domain.ai.value_objects.token_usage import TokenUsage
from domain.novel.entities.chapter import Chapter, ChapterStatus
from domain.novel.value_objects.novel_id import NovelId
from engine.pipeline.base import BaseStoryPipeline
from engine.pipeline.context import PipelineContext
from engine.pipeline.prose_composer import ProseCompositionResult


class _FinalPromptNode:
    active_version_id = "memory-observability-v1"

    def get_active_system(self):
        return "You write a continuous novel chapter."

    def get_active_user_template(self):
        return "{{ continuity_context }}\n\n{{ chapter_outline }}"


class _FinalPromptRegistry:
    def get_node(self, node_key: str, use_cache: bool = True):
        return _FinalPromptNode() if node_key == "memory-observability" else None


class _TemplateEngine:
    def render(self, system_template, user_template, variables, variable_schemas=None):
        class _Rendered:
            system = system_template
            user = user_template
            warnings = []
            missing_variables = []

        for key, value in variables.items():
            _Rendered.system = _Rendered.system.replace("{{ " + key + " }}", str(value))
            _Rendered.user = _Rendered.user.replace("{{ " + key + " }}", str(value))
        return _Rendered()


class _CapturedLLM:
    def __init__(self):
        self.prompts: list[Prompt] = []

    async def generate(self, prompt: Prompt, config):
        self.prompts.append(prompt)
        return GenerationResult("deterministic prose", TokenUsage(input_tokens=4, output_tokens=2))


@pytest.mark.asyncio
async def test_final_model_prompt_contains_all_memory_tiers():
    """A lost tier must be visible at the model boundary, not only in a builder."""
    t0 = "[T0] BIBLE_LOCK: 林澈已经死亡，赤铜钥匙归沈青。"
    t1 = "[T1] EFFECTIVE_SUMMARY: 密室真相尚未公开。"
    t2 = "[T2] RECENT_BRIDGE: 沈青在雨夜带着钥匙离开钟楼。"
    t3 = "[T3] RETRIEVED_EVIDENCE: 钟楼地窖有第二道暗门。"
    continuity_context = assemble_chapter_bundle_context_text(
        {"layer1_text": t0 + "\n" + t1, "layer2_text": t2, "layer3_text": t3}
    )
    hub = InMemoryVariableHubRepository()
    hub.set_bindings(
        "memory-observability-input",
        "memory-observability",
        [
            VariableBinding(alias="continuity_context", required=True),
            VariableBinding(alias="chapter_outline", required=True),
        ],
    )
    llm = _CapturedLLM()
    spec = InvocationSpec(
        operation="memory.observability.chapter",
        node_key="memory-observability",
        prompt_node_version_id="memory-observability-v1",
        input_binding_set_id="memory-observability-input",
        default_policy=InvocationPolicy.DIRECT,
    )
    gateway = AIInvocationGateway(
        spec_service=InvocationSpecService(InMemoryInvocationSpecRepository([spec])),
        variable_resolver=VariableResolver(hub),
        prompt_assembler=CPMSPromptAssembler(
            registry=_FinalPromptRegistry(), template_engine=_TemplateEngine()
        ),
        llm_service=llm,
        session_service=InvocationSessionService(),
    )

    await gateway.invoke(
        InvocationRequest(
            operation=spec.operation,
            node_key=spec.node_key,
            variables={
                "continuity_context": continuity_context,
                "chapter_outline": "沈青进入钟楼地窖。",
            },
            context={"novel_id": "memory-observability"},
        )
    )

    assert len(llm.prompts) == 1
    sent_to_model = llm.prompts[0].user
    for expected in (t0, t1, t2, t3):
        assert expected in sent_to_model


class _ControllableVectorStore:
    def __init__(self, fail_chapters: set[int]):
        self.fail_chapters = fail_chapters
        self.collections: set[str] = set()
        self.records: dict[str, dict] = {}

    async def list_collections(self):
        return sorted(self.collections)

    async def create_collection(self, collection, dimension):
        self.collections.add(collection)

    async def insert(self, collection, id, vector, payload):
        if payload["chapter_number"] in self.fail_chapters:
            raise RuntimeError(f"injected vector failure for chapter {payload['chapter_number']}")
        self.records[id] = dict(payload)


class _DeterministicEmbeddingService:
    def get_dimension(self):
        return 3

    async def embed(self, text):
        return [float(len(text)), 0.0, 1.0]


class _DeterministicLLM:
    async def generate(self, prompt: Prompt, config):
        return GenerationResult("deterministic chapter content", TokenUsage(input_tokens=3, output_tokens=3))


class _DeterministicComposer:
    def __init__(self, llm):
        self._llm = llm

    async def compose(self, request):
        result = await self._llm.generate(
            Prompt(system="deterministic regression fixture", user=request.outline), None
        )
        return ProseCompositionResult(content=f"第{request.chapter_number}章：{result.content}")


class _ChapterRepository:
    def __init__(self, connection):
        self._connection = connection
        self.chapters: dict[int, Chapter] = {}

    def get_by_novel_and_number(self, novel_id, number):
        return self.chapters.get(int(number))

    def save(self, chapter):
        self.chapters[chapter.number] = chapter
        self._connection.execute(
            """
            INSERT INTO chapter_snapshots (chapter_number, content, status)
            VALUES (?, ?, ?)
            ON CONFLICT(chapter_number) DO UPDATE SET content = excluded.content, status = excluded.status
            """,
            (chapter.number, chapter.content, str(chapter.status)),
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


def _record_event(connection, chapter_number: int, event: str) -> None:
    connection.execute(
        "INSERT INTO regression_events (chapter_number, event) VALUES (?, ?)",
        (chapter_number, event),
    )
    connection.commit()


@pytest.mark.asyncio
@pytest.mark.xfail(
    strict=True,
    reason="memory stability follow-up must replay the failed chapter-12 vector write after recovery",
)
async def test_thirty_chapter_regression_recovers_injected_vector_failure(tmp_path: Path, monkeypatch):
    """Acceptance fixture: restart, failures, rewrite, and act/volume transitions stay observable."""
    database_path = tmp_path / "memory-stability-regression.sqlite"
    connection = sqlite3.connect(database_path)
    connection.execute(
        "CREATE TABLE regression_events (chapter_number INTEGER NOT NULL, event TEXT NOT NULL)"
    )
    connection.execute(
        "CREATE TABLE chapter_snapshots (chapter_number INTEGER PRIMARY KEY, content TEXT NOT NULL, status TEXT NOT NULL)"
    )
    vector_store = _ControllableVectorStore(fail_chapters={12})
    indexing_service = ChapterIndexingService(vector_store, _DeterministicEmbeddingService())
    chapter_repository = _ChapterRepository(connection)
    story_node_repository = _CurrentStoryNodeRepository()
    novel_repository = _NovelRepository()
    llm = _DeterministicLLM()
    aftermath = ChapterAftermathPipeline(
        knowledge_service=object(),
        chapter_indexing_service=indexing_service,
        llm_service=llm,
    )

    async def _extract_bridge(self, novel_id, chapter_number, content):
        return None

    async def _sync_narrative(
        novel_id,
        chapter_number,
        content,
        knowledge_service,
        indexing_svc,
        llm_service,
        **kwargs,
    ):
        if chapter_number == 17:
            raise RuntimeError("injected extraction failure for chapter 17")
        await indexing_svc.index_chapter_summary(
            novel_id, chapter_number, f"chapter {chapter_number} deterministic summary"
        )
        return {
            "vector_stored": True,
            "foreshadow_stored": True,
            "triples_extracted": True,
            "causal_edges_stored": True,
            "character_mutations_stored": True,
            "debt_updated": True,
            "tension_composite": 60.0,
        }

    async def _skip_auxiliary(self, novel_id, chapter_number, content, evidence):
        return None

    monkeypatch.setattr(ChapterAftermathPipeline, "_extract_chapter_bridge", _extract_bridge)
    monkeypatch.setattr(ChapterAftermathPipeline, "_run_auxiliary_stages", _skip_auxiliary)
    monkeypatch.setattr(
        "application.world.services.chapter_narrative_sync.sync_chapter_narrative_after_save",
        _sync_narrative,
    )

    pipeline = BaseStoryPipeline()
    scenario_events = {
        3: "character_death",
        5: "key_item",
        7: "location",
        8: "secret",
        9: "foreshadow",
    }
    for chapter_number in range(1, 31):
        if chapter_number == 10:
            pipeline = BaseStoryPipeline()
            _record_event(connection, 9, "restart_after_chapter_9")
        if chapter_number in {10, 11, 20}:
            _record_event(connection, chapter_number, "act_or_volume_transition")
        if chapter_number in scenario_events:
            _record_event(connection, chapter_number, scenario_events[chapter_number])
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
            prose_composer=_DeterministicComposer(llm),
            llm_service=llm,
            aftermath_pipeline=aftermath,
        )
        result = await pipeline.run_chapter(context)
        assert result.success
        assert chapter_repository.chapters[chapter_number].status == ChapterStatus.COMPLETED
        _record_event(connection, chapter_number, "chapter_saved")

        if chapter_number == 20:
            chapter_repository.chapters[10].update_content("第10章重写后的正文")
            _record_event(connection, 10, "rewrite_after_chapter_20")
            rewrite_result = await aftermath.run_after_chapter_saved(
                "memory-stability", 10, "第10章重写后的正文"
            )
            assert rewrite_result["vector_stored"] is True

    await aftermath.drain_auxiliary_stages()
    events = connection.execute("SELECT chapter_number, event FROM regression_events").fetchall()
    connection.close()

    assert database_path.exists()
    assert len([event for _, event in events if event == "chapter_saved"]) == 30
    assert (9, "restart_after_chapter_9") in events
    assert (10, "rewrite_after_chapter_20") in events
    assert len([event for _, event in events if event == "act_or_volume_transition"]) == 3
    assert set(scenario_events.values()) <= {event for _, event in events}
    assert "memory-stability_ch12_summary" in vector_store.records
