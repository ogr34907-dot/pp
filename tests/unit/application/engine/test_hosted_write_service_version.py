from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

import pytest

from application.core.dtos.chapter_dto import ChapterDTO
from application.engine.services.hosted_write_service import HostedWriteService
from domain.shared.exceptions import EntityNotFoundError


@pytest.mark.asyncio
async def test_hosted_new_chapter_schedules_aftermath_with_persisted_version():
    async def generate_chapter_stream(*_args, **_kwargs):
        yield {"type": "done", "content": "新正文"}

    persisted = ChapterDTO(
        id="chapter-novel-1-1",
        novel_id="novel-1",
        number=1,
        title="第1章",
        content="新正文",
        word_count=3,
        status="draft",
        content_sha256="persisted-hash",
        content_revision=4,
    )
    chapter_service = SimpleNamespace(
        update_chapter_by_novel_and_number=Mock(
            side_effect=EntityNotFoundError("Chapter", "novel-1/chapter-1")
        ),
        get_chapter_by_novel_and_number=Mock(return_value=persisted),
    )
    novel_service = SimpleNamespace(add_chapter=Mock())
    workflow = SimpleNamespace(
        suggest_outline=AsyncMock(return_value="大纲"),
        generate_chapter_stream=generate_chapter_stream,
    )
    service = HostedWriteService(workflow, chapter_service, novel_service)
    service._schedule_chapter_aftermath = Mock()

    events = [
        event
        async for event in service.stream_hosted_write(
            "novel-1", 1, 1, auto_save=True, auto_outline=True
        )
    ]

    assert any(event["type"] == "saved" and event["ok"] for event in events)
    chapter_service.get_chapter_by_novel_and_number.assert_called_once_with("novel-1", 1)
    service._schedule_chapter_aftermath.assert_called_once_with(
        "novel-1",
        1,
        "新正文",
        expected_content_sha256="persisted-hash",
        expected_content_revision=4,
    )
