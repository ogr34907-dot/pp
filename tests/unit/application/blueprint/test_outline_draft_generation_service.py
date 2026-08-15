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
async def test_root_outline_prompt_includes_author_bible_but_child_uses_published_parent(tmp_path):
    db = DatabaseConnection(str(tmp_path / "outline-root-bible.db"))
    db.execute("INSERT INTO novels (id, title, slug) VALUES ('novel-1', '大纲小说', 'outline-root-bible')")
    db.execute("INSERT INTO bibles (id, novel_id) VALUES ('bible-1', 'novel-1')")
    db.execute(
        "INSERT INTO bible_world_settings (id, novel_id, name, description) "
        "VALUES ('world-1', 'novel-1', '灵潮规则', '灵脉枯竭后不得无代价施法')"
    )
    db.execute(
        "INSERT INTO unified_characters (id, novel_id, name, description) "
        "VALUES ('character-1', 'novel-1', '沈烬', '为救妹妹不得不与仇家同行')"
    )
    db.execute(
        "INSERT INTO bible_locations (id, novel_id, name, description) "
        "VALUES ('location-1', 'novel-1', '落日城', '最后一座仍能交易灵石的边城')"
    )
    db.execute(
        "INSERT INTO bible_timeline_notes (id, novel_id, event, time_point, description) "
        "VALUES ('timeline-1', 'novel-1', '灵潮枯竭', '三年前', '沈烬失去修为后才踏上复仇路')"
    )
    db.get_connection().commit()
    repo = OutlineContractRepository(db)
    root = repo.ensure_root("novel-1")
    llm = _LLM('{"title":"总纲","creative_goal":"让沈烬付出代价"}')
    service = OutlineDraftGenerationService(repo, llm, db)

    drafted = await service.generate_draft(root.id)

    root_prompt = llm.prompts[-1].user
    assert "灵潮规则" in root_prompt
    assert "沈烬" in root_prompt
    assert "落日城" in root_prompt
    assert "不得无代价施法" in root_prompt
    assert "灵潮枯竭" in root_prompt

    repo.publish_and_sync(root.id, expected_revision=drafted.draft.revision)
    child = repo.create_contract(
        novel_id="novel-1", level=OutlineLevel.PART, parent_contract_id=root.id
    )
    llm.content = '{"title":"第一部","narrative_text":"承接总纲"}'
    await service.generate_draft(child.id)

    child_prompt = llm.prompts[-1].user
    assert "总纲" in child_prompt
    assert "灵潮规则" not in child_prompt
    assert "灵潮枯竭" not in child_prompt


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
    assert [event["type"] for event in persisted["events"]] == ["delta", "completed"]
    assert persisted["events"][0]["text"] == '{"title":"流式总纲","creative_goal":"推进"}'
    assert persisted["draft_revision"] == 1


@pytest.mark.asyncio
async def test_streaming_outline_delta_commits_once_and_preserves_text_and_events(tmp_path):
    db = DatabaseConnection(str(tmp_path / "outline-stream-commit-count.db"))
    db.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-1', '大纲小说', 'outline-stream-commit-count')"
    )
    db.get_connection().commit()
    repo = OutlineContractRepository(db)
    root = repo.ensure_root("novel-1")
    sql_trace: list[str] = []
    db.get_connection().set_trace_callback(sql_trace.append)
    llm = _StreamingLLM(['{"title":"流式', '总纲","creative_goal":"推进"}'])

    events = [event async for event in OutlineDraftGenerationService(repo, llm, db).stream_generate_draft(root.id)]

    attempt_id = events[0]["attempt_id"]
    persisted = repo.get_generation_attempt(attempt_id)
    assert [event["type"] for event in events] == ["started", "delta", "delta", "completed"]
    assert persisted["status"] == "completed"
    assert persisted["accumulated_text"] == '{"title":"流式总纲","creative_goal":"推进"}'
    assert [event["type"] for event in persisted["events"]] == ["started", "delta", "completed"]
    assert [event["text"] for event in persisted["events"] if event["type"] == "delta"] == [
        '{"title":"流式总纲","creative_goal":"推进"}',
    ]
    assert events[-1]["payload"]["title"] == "流式总纲"

    # Fixed non-delta commits: the attempt and its started event are one
    # durable transaction, followed by draft and terminal event commits. The
    # two live fragments share one durable delta transaction.
    commit_count = sum(1 for statement in sql_trace if statement.strip().upper() == "COMMIT")
    assert commit_count == 4


@pytest.mark.asyncio
async def test_streaming_outline_batches_tiny_deltas_without_losing_live_or_durable_text(tmp_path):
    """A thousand provider fragments must not become a thousand SQLite commits."""

    db = DatabaseConnection(str(tmp_path / "outline-stream-batched.db"))
    db.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-1', '大纲小说', 'outline-stream-batched')"
    )
    db.get_connection().commit()
    repo = OutlineContractRepository(db)
    root = repo.ensure_root("novel-1")
    payload = '{"title":"' + ("长" * 1000) + '","creative_goal":"推进"}'
    sql_trace: list[str] = []
    db.get_connection().set_trace_callback(sql_trace.append)

    events = [
        event
        async for event in OutlineDraftGenerationService(
            repo, _StreamingLLM(list(payload)), db
        ).stream_generate_draft(root.id)
    ]

    attempt_id = events[0]["attempt_id"]
    persisted = repo.get_generation_attempt(attempt_id)
    durable_deltas = [event for event in persisted["events"] if event["type"] == "delta"]
    commit_count = sum(1 for statement in sql_trace if statement.strip().upper() == "COMMIT")

    assert "".join(event["text"] for event in events if event["type"] == "delta") == payload
    assert persisted["accumulated_text"] == payload
    assert 1 < len(durable_deltas) < 10
    assert commit_count < 20


def test_streaming_outline_delta_event_failure_rolls_back_text(tmp_path, monkeypatch):
    db = DatabaseConnection(str(tmp_path / "outline-stream-delta-rollback.db"))
    db.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-1', '大纲小说', 'outline-stream-delta-rollback')"
    )
    db.get_connection().commit()
    repo = OutlineContractRepository(db)
    root = repo.ensure_root("novel-1")
    attempt = repo.start_generation_attempt(
        root.id,
        prompt_snapshot={"system": "outline", "user": "draft"},
        context_digest="outline-delta-rollback",
    )

    def fail_delta_event(*_args, **_kwargs):
        raise RuntimeError("delta event write failed")

    monkeypatch.setattr(repo, "_append_generation_attempt_event", fail_delta_event)

    with pytest.raises(RuntimeError, match="delta event write failed"):
        repo.append_generation_attempt_delta(attempt["id"], "未完成片段")

    persisted = repo.get_generation_attempt(attempt["id"])
    assert persisted["accumulated_text"] == ""
    assert [event["type"] for event in persisted["events"]] == ["started"]


@pytest.mark.asyncio
async def test_terminal_event_failure_does_not_leave_completed_status_without_event(
    tmp_path, monkeypatch
):
    db = DatabaseConnection(str(tmp_path / "outline-stream-terminal-rollback.db"))
    db.execute(
        "INSERT INTO novels (id, title, slug) VALUES ('novel-1', '大纲小说', 'outline-stream-terminal-rollback')"
    )
    db.get_connection().commit()
    repo = OutlineContractRepository(db)
    root = repo.ensure_root("novel-1")
    original_append_event = repo._append_generation_attempt_event

    def fail_completed_event(attempt_id, event, **kwargs):
        if event.get("type") == "completed":
            raise RuntimeError("completed event write failed")
        return original_append_event(attempt_id, event, **kwargs)

    monkeypatch.setattr(repo, "_append_generation_attempt_event", fail_completed_event)
    llm = _StreamingLLM(['{"title":"终态大纲","creative_goal":"推进"}'])

    events = [event async for event in OutlineDraftGenerationService(repo, llm, db).stream_generate_draft(root.id)]

    attempt = repo.get_generation_attempt(events[0]["attempt_id"])
    slot = repo.get_slot(root.id)
    assert slot.draft is not None
    assert slot.active is None
    assert attempt["status"] == "failed"
    assert [event["type"] for event in attempt["events"]] == ["started", "delta", "error"]
    assert all(event["type"] != "completed" for event in attempt["events"])
    assert events[-1]["type"] == "error"


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
