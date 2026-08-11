import asyncio
from types import SimpleNamespace

from application.audit.services.chapter_review_service import ChapterReviewService
from domain.novel.value_objects.foreshadowing import (
    Foreshadowing,
    ForeshadowingStatus,
    ImportanceLevel,
)


def test_extract_characters_from_content_uses_cast_names_and_aliases():
    service = ChapterReviewService(
        chapter_repo=None,
        cast_repo=None,
        timeline_repo=None,
        storyline_repo=None,
        foreshadowing_repo=None,
        vector_store=None,
        llm_service=None,
    )
    characters = [
        SimpleNamespace(name="沈岚", aliases=["老沈"]),
        SimpleNamespace(name="顾明", aliases=[]),
    ]

    found = service._extract_characters_from_content(
        "老沈把证据递给顾明，顾明没有立刻接。",
        characters,
    )

    assert found == ["沈岚", "顾明"]


def test_chapter_review_service_uses_injected_model():
    service = ChapterReviewService(
        chapter_repo=None,
        cast_repo=None,
        timeline_repo=None,
        storyline_repo=None,
        foreshadowing_repo=None,
        vector_store=None,
        llm_service=None,
        model="system-test-model",
    )

    assert service.model == "system-test-model"


def test_foreshadowing_review_reads_canonical_registry_without_vector_query():
    class ForeshadowingRepository:
        def __init__(self):
            self.requested_novel_id = None

        def get_by_novel_id(self, novel_id):
            self.requested_novel_id = novel_id
            return SimpleNamespace(
                get_unresolved=lambda: [
                    Foreshadowing(
                        id="promise-1",
                        planted_in_chapter=1,
                        description="门后的铃声",
                        importance=ImportanceLevel.HIGH,
                        status=ForeshadowingStatus.PLANTED,
                    )
                ]
            )

    class LLM:
        async def generate(self, _prompt, _config):
            return SimpleNamespace(
                content=(
                    '{"missed_opportunities": [{"description": "铃声尚未回收", '
                    '"suggestion": "在下一场景回应铃声"}]}'
                )
            )

    foreshadowing_repo = ForeshadowingRepository()
    service = ChapterReviewService(
        chapter_repo=None,
        cast_repo=None,
        timeline_repo=None,
        storyline_repo=None,
        foreshadowing_repo=foreshadowing_repo,
        vector_store=None,
        llm_service=LLM(),
        model="system-test-model",
    )
    service._render_review_prompt = lambda _kind, _variables: "review prompt"

    issues = asyncio.run(
        service._check_foreshadowing_usage(
            "novel-1",
            SimpleNamespace(content="门外又响起一阵铃声。", chapter_number=4),
        )
    )

    assert foreshadowing_repo.requested_novel_id.value == "novel-1"
    assert [(issue.description, issue.suggestion) for issue in issues] == [
        ("铃声尚未回收", "在下一场景回应铃声")
    ]
