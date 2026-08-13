"""AI outline generation is constrained by the published parent contract."""

import asyncio
from types import SimpleNamespace

import pytest

from application.blueprint.services.outline_draft_generation_service import (
    OutlineDraftGenerationError,
    OutlineDraftGenerationService,
)
from application.engine.dag.plan.schema import chapter_rhythm_from_outline_payload
from domain.structure.outline_contract import OutlineLevel, OutlinePayload, OutlineSource
from infrastructure.persistence.database.connection import DatabaseConnection
from infrastructure.persistence.database.outline_contract_repository import OutlineContractRepository
from infrastructure.persistence.database.story_node_repository import StoryNodeRepository
from domain.structure.story_node import StoryNode


class _LLM:
    def __init__(self, content: str):
        self.content = content
        self.prompts = []

    async def generate(self, prompt, _config):
        self.prompts.append(prompt)
        return SimpleNamespace(content=self.content)


class _StreamingLLM(_LLM):
    def __init__(self, chunks: list[str], error: Exception | None = None):
        super().__init__("")
        self.chunks = chunks
        self.error = error

    async def stream_generate(self, prompt, _config):
        self.prompts.append(prompt)
        for chunk in self.chunks:
            yield chunk
        if self.error is not None:
            raise self.error


class _BlockingStreamingLLM(_LLM):
    def __init__(self):
        super().__init__("")
        self.started = asyncio.Event()
        self.release = asyncio.Event()

    async def stream_generate(self, prompt, _config):
        self.prompts.append(prompt)
        self.started.set()
        await self.release.wait()
        yield '{"title":"不应写入"}'


@pytest.mark.asyncio
async def test_ai_draft_is_json_normalized_and_uses_only_synced_parent_context(tmp_path):
    db = DatabaseConnection(str(tmp_path / "outline-draft.db"))
    db.execute("INSERT INTO novels (id, title, slug) VALUES ('novel-1', '大纲小说', 'outline-draft')")
    db.get_connection().commit()
    repo = OutlineContractRepository(db)
    root = repo.ensure_root("novel-1")
    llm = _LLM(
        "```json\n{\"title\":\"AI 总纲\",\"creative_goal\":\"让主角承担代价\",\"required_events\":[\"失去故乡\"]}\n```"
    )
    service = OutlineDraftGenerationService(repo, llm, db)

    drafted = await service.generate_draft(root.id)

    assert drafted.draft.payload.title == "AI 总纲"
    assert drafted.draft.source == OutlineSource.AI
    assert "总纲" in llm.prompts[0].user
    assert "chapter_function" not in llm.prompts[0].user

    repo.publish_and_sync(root.id, expected_revision=drafted.draft.revision)
    child = repo.create_contract(
        novel_id="novel-1", level=OutlineLevel.PART, parent_contract_id=root.id
    )
    llm.content = '{"title":"第一部","narrative_text":"承接总纲推进"}'
    child_draft = await service.generate_draft(child.id)
    assert child_draft.draft.payload.title == "第一部"
    assert "AI 总纲" in llm.prompts[-1].user


@pytest.mark.asyncio
async def test_outline_draft_requires_a_json_object(tmp_path):
    db = DatabaseConnection(str(tmp_path / "outline-draft-invalid.db"))
    db.execute("INSERT INTO novels (id, title, slug) VALUES ('novel-1', '大纲小说', 'outline-draft-invalid')")
    db.get_connection().commit()
    repo = OutlineContractRepository(db)
    root = repo.ensure_root("novel-1")
    service = OutlineDraftGenerationService(repo, _LLM("这不是 JSON"), db)

    with pytest.raises(OutlineDraftGenerationError, match="requires_json_object"):
        await service.generate_draft(root.id)


@pytest.mark.asyncio
async def test_ai_chapter_outline_requires_a_rhythm_contract(tmp_path):
    db = DatabaseConnection(str(tmp_path / "outline-chapter-rhythm-required.db"))
    db.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-1', '节奏小说', 'rhythm-required')"
    )
    db.get_connection().commit()
    repo = OutlineContractRepository(db)
    parent = repo.ensure_root("novel-1")
    parent_payload = OutlinePayload(
        title="已发布父级",
        narrative_text="承接并推进",
        creative_goal="推进人物选择",
        entry_state="当前状态",
        exit_state="新的状态",
        required_events=[],
        state_changes={},
        handoff_conditions=["承接下一章"],
        chapter_start=1,
        chapter_end=1,
    )
    for level in (
        OutlineLevel.PART,
        OutlineLevel.VOLUME,
        OutlineLevel.ACT,
        OutlineLevel.CHAPTER,
    ):
        parent = repo.save_draft(parent.id, parent_payload, source=OutlineSource.AUTHOR)
        repo.publish_and_sync(parent.id, expected_revision=parent.draft.revision)
        parent = repo.create_contract(
            novel_id="novel-1", level=level, parent_contract_id=parent.id
        )

    llm = _LLM(
            '{"title":"第一章","narrative_text":"主角进入雨夜",'
            '"creative_goal":"推进选择","entry_state":"等待消息",'
            '"exit_state":"决定出城","required_events":[],"forbidden_events":[],'
            '"state_changes":{},"foreshadowing":{},"chapter_start":1,'
            '"chapter_end":1,"word_budget":3000,"handoff_conditions":["承接出城"]}'
    )
    service = OutlineDraftGenerationService(repo, llm, db)

    with pytest.raises(OutlineDraftGenerationError):
        await service.generate_draft(parent.id)

    assert repo.get_slot(parent.id).draft is None

    llm.content = (
        '{"title":"第一章","narrative_text":"主角进入雨夜",'
        '"creative_goal":"推进选择","entry_state":"等待消息",'
        '"exit_state":"决定出城","required_events":[],"forbidden_events":[],'
        '"state_changes":{},"foreshadowing":{},"chapter_start":1,'
        '"chapter_end":1,"word_budget":3000,"handoff_conditions":["承接出城"],'
        '"rhythm":{"chapter_function":"setup","intensity_curve":["low","medium"],'
        '"chapter_goal":"找到出城路线","chapter_delta":"获得路线",'
        '"ending_hook":"城门即将关闭"}}'
    )
    drafted = await service.generate_draft(parent.id)
    assert drafted.draft.payload.extra["rhythm"]["chapter_function"] == "setup"
    assert chapter_rhythm_from_outline_payload(drafted.draft.payload).chapter_function == "setup"


@pytest.mark.asyncio
async def test_streaming_outline_draft_persists_replayable_attempt_events(tmp_path):
    db = DatabaseConnection(str(tmp_path / "outline-stream.db"))
    db.execute("INSERT INTO novels (id, title, slug) VALUES ('novel-1', '大纲小说', 'outline-stream')")
    db.get_connection().commit()
    repo = OutlineContractRepository(db)
    root = repo.ensure_root("novel-1")
    llm = _StreamingLLM(['{"title":"流式', '总纲","creative_goal":"推进"}'])
    service = OutlineDraftGenerationService(repo, llm, db)

    events = [event async for event in service.stream_generate_draft(root.id)]

    assert [event["type"] for event in events] == ["started", "delta", "delta", "completed"]
    attempt_id = events[0]["attempt_id"]
    persisted = repo.get_generation_attempt(attempt_id, after_sequence=1)
    assert persisted["status"] == "completed"
    assert persisted["accumulated_text"] == '{"title":"流式总纲","creative_goal":"推进"}'
    assert [event["type"] for event in persisted["events"]] == ["delta", "delta", "completed"]
    assert persisted["draft_revision"] == 1


@pytest.mark.asyncio
async def test_failed_stream_attempt_can_retry_only_with_its_persisted_context(tmp_path):
    db = DatabaseConnection(str(tmp_path / "outline-stream-retry.db"))
    db.execute("INSERT INTO novels (id, title, slug) VALUES ('novel-1', '大纲小说', 'outline-stream-retry')")
    db.get_connection().commit()
    repo = OutlineContractRepository(db)
    root = repo.ensure_root("novel-1")
    llm = _StreamingLLM(['{"title":"中断'], RuntimeError("provider interrupted"))
    service = OutlineDraftGenerationService(repo, llm, db)

    failed_events = [event async for event in service.stream_generate_draft(root.id)]
    attempt_id = failed_events[0]["attempt_id"]
    assert [event["type"] for event in failed_events] == ["started", "delta", "error"]
    assert repo.get_generation_attempt(attempt_id)["status"] == "failed"

    llm.chunks = ['{"title":"重试总纲","creative_goal":"推进"}']
    llm.error = None
    retried_events = [
        event async for event in service.stream_generate_draft(root.id, retry_attempt_id=attempt_id)
    ]

    assert retried_events[-1]["type"] == "completed"
    assert retried_events[0]["retry_of_attempt_id"] == attempt_id
    assert repo.get_generation_attempt(retried_events[0]["attempt_id"])["status"] == "completed"


@pytest.mark.asyncio
async def test_cancelled_stream_marks_the_durable_attempt_cancelled(tmp_path):
    db = DatabaseConnection(str(tmp_path / "outline-stream-cancelled-task.db"))
    db.execute("INSERT INTO novels (id, title, slug) VALUES ('novel-1', '大纲小说', 'outline-stream-cancelled-task')")
    db.get_connection().commit()
    repo = OutlineContractRepository(db)
    root = repo.ensure_root("novel-1")
    llm = _BlockingStreamingLLM()
    service = OutlineDraftGenerationService(repo, llm, db)

    stream = service.stream_generate_draft(root.id)
    started = await anext(stream)
    attempt_id = started["attempt_id"]
    # The same generator owns the running attempt while its provider is blocked.
    pending = asyncio.create_task(anext(stream))
    await llm.started.wait()
    pending.cancel()
    with pytest.raises(asyncio.CancelledError):
        await pending

    assert repo.get_generation_attempt(attempt_id)["status"] == "cancelled"


@pytest.mark.asyncio
async def test_closed_stream_marks_the_durable_attempt_cancelled(tmp_path):
    db = DatabaseConnection(str(tmp_path / "outline-stream-closed-task.db"))
    db.execute("INSERT INTO novels (id, title, slug) VALUES ('novel-1', '大纲小说', 'outline-stream-closed-task')")
    db.get_connection().commit()
    repo = OutlineContractRepository(db)
    root = repo.ensure_root("novel-1")
    stream = OutlineDraftGenerationService(repo, _BlockingStreamingLLM(), db).stream_generate_draft(root.id)

    started = await anext(stream)
    await stream.aclose()

    assert repo.get_generation_attempt(started["attempt_id"])["status"] == "cancelled"


def test_generation_attempt_cancellation_is_durable_and_evented(tmp_path):
    db = DatabaseConnection(str(tmp_path / "outline-stream-cancel.db"))
    db.execute("INSERT INTO novels (id, title, slug) VALUES ('novel-1', '大纲小说', 'outline-stream-cancel')")
    db.get_connection().commit()
    repo = OutlineContractRepository(db)
    root = repo.ensure_root("novel-1")

    attempt = repo.start_generation_attempt(
        root.id,
        prompt_snapshot={"system": "system", "user": "user"},
        context_digest="context-v1",
    )
    cancelled = repo.cancel_generation_attempt(attempt["id"])

    assert cancelled["status"] == "cancelled"
    assert [event["type"] for event in cancelled["events"]] == ["started", "cancelled"]


@pytest.mark.asyncio
async def test_later_sibling_prompt_includes_previous_synced_exit_state(tmp_path):
    db = DatabaseConnection(str(tmp_path / "outline-sibling-context.db"))
    db.execute("INSERT INTO novels (id, title, slug) VALUES ('novel-1', '大纲小说', 'outline-sibling-context')")
    db.get_connection().commit()
    repo = OutlineContractRepository(db)
    root = repo.ensure_root("novel-1")
    root_draft = repo.save_draft(root.id, OutlinePayload(title="总纲"), source=OutlineSource.AUTHOR)
    repo.publish_and_sync(root.id, expected_revision=root_draft.draft.revision)
    nodes = StoryNodeRepository(db)
    first_node = StoryNode(id="part-1", novel_id="novel-1", node_type="part", number=1, title="第一部", order_index=1)
    second_node = StoryNode(id="part-2", novel_id="novel-1", node_type="part", number=2, title="第二部", order_index=2)
    nodes.save_sync(first_node)
    nodes.save_sync(second_node)
    first = repo.create_contract(novel_id="novel-1", level=OutlineLevel.PART, parent_contract_id=root.id, story_node_id=first_node.id)
    first_draft = repo.save_draft(first.id, OutlinePayload(title="第一部", exit_state="主角离开故乡"), source=OutlineSource.AUTHOR)
    repo.publish_and_sync(first.id, expected_revision=first_draft.draft.revision)
    second = repo.create_contract(novel_id="novel-1", level=OutlineLevel.PART, parent_contract_id=root.id, story_node_id=second_node.id)
    llm = _LLM('{"title":"第二部"}')

    await OutlineDraftGenerationService(repo, llm, db).generate_draft(second.id)

    assert "主角离开故乡" in llm.prompts[0].user
