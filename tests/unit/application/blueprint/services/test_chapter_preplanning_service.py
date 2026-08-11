from types import SimpleNamespace

import pytest

from application.blueprint.services.chapter_preplanning_service import ChapterPreplanningService


async def _empty_stream():
    if False:
        yield ""


class _EmptyStreamThenFallbackLLM:
    def __init__(self, fallback_content: str) -> None:
        self.fallback_content = fallback_content
        self.fallback_calls = 0

    def stream_generate(self, _prompt, _config):
        return _empty_stream()

    async def generate(self, _prompt, _config):
        self.fallback_calls += 1
        return SimpleNamespace(content=self.fallback_content)


@pytest.mark.asyncio
async def test_empty_preplan_stream_retries_with_non_stream_generation():
    """PREPLAN-001: a zero-chunk provider stream must not become a char-0 JSON parse failure."""
    llm = _EmptyStreamThenFallbackLLM('{"chapter_plan": {"opening": "落点"}}')
    service = ChapterPreplanningService(llm_service=llm)

    raw = await service._generate_text("preplan prompt", object())

    assert raw == '{"chapter_plan": {"opening": "落点"}}'
    assert llm.fallback_calls == 1


@pytest.mark.asyncio
async def test_empty_preplan_stream_and_empty_fallback_raise_a_diagnostic_error():
    """PREPLAN-002: an empty model response must name the upstream condition, not pretend it was JSON."""
    llm = _EmptyStreamThenFallbackLLM("")
    service = ChapterPreplanningService(llm_service=llm)

    with pytest.raises(ValueError, match="chapter_preplan_empty_model_response"):
        await service._generate_text("preplan prompt", object())

    assert llm.fallback_calls == 1
