from infrastructure.ai.config.settings import Settings
from domain.ai.services.llm_service import GenerationConfig
from domain.ai.value_objects.prompt import Prompt
from infrastructure.ai.providers.gemini_provider import GeminiProvider


class _StreamResponse:
    def raise_for_status(self):
        pass

    async def aiter_text(self):
        yield 'data: {"candidates":[{"content":{"parts":[{"text":"ok"}]}}]}\n\n'


class _AsyncContext:
    async def __aenter__(self):
        return _StreamResponse()

    async def __aexit__(self, *args):
        return False


def test_gemini_provider_http_timeout_uses_settings():
    provider = GeminiProvider(
        Settings(
            api_key="test-api-key",
            connect_timeout=4,
            read_timeout=40,
            write_timeout=8,
            pool_timeout=2,
        )
    )

    timeout = provider._http_client.timeout
    assert timeout.connect == 4
    assert timeout.read == 40
    assert timeout.write == 8
    assert timeout.pool == 2


async def test_gemini_provider_passes_task_timeout_to_request():
    provider = GeminiProvider(Settings(api_key="test-api-key", default_model="gemini-test"))
    captured = {}

    async def _post(*args, **kwargs):
        captured.update(kwargs)
        return type("Response", (), {"raise_for_status": lambda self: None, "json": lambda self: {"candidates": [{"content": {"parts": [{"text": "ok"}]}}], "usageMetadata": {}}})()

    provider._http_client.post = _post
    await provider.generate(Prompt(system="s", user="u"), GenerationConfig(timeout_seconds=19))

    assert captured["timeout"] == 19


async def test_gemini_provider_preserves_cache_and_thinking_usage_details():
    provider = GeminiProvider(Settings(api_key="test-api-key", default_model="gemini-test"))

    async def _post(*args, **kwargs):
        return type("Response", (), {
            "raise_for_status": lambda self: None,
            "json": lambda self: {
                "candidates": [{"content": {"parts": [{"text": "ok"}]}}],
                "usageMetadata": {
                    "promptTokenCount": 20,
                    "candidatesTokenCount": 8,
                    "cachedContentTokenCount": 6,
                    "thoughtsTokenCount": 3,
                },
            },
        })()

    provider._http_client.post = _post
    result = await provider.generate(Prompt(system="s", user="u"), GenerationConfig())

    assert result.token_usage.cache_hit_tokens == 6
    assert result.token_usage.cache_miss_tokens == 14
    assert result.token_usage.reasoning_tokens == 3


async def test_gemini_provider_passes_task_timeout_to_stream_request():
    provider = GeminiProvider(Settings(api_key="test-api-key", default_model="gemini-test"))
    captured = {}

    def _stream(*args, **kwargs):
        captured.update(kwargs)
        return _AsyncContext()

    provider._http_client.stream = _stream
    chunks = [
        chunk
        async for chunk in provider.stream_generate(
            Prompt(system="s", user="u"),
            GenerationConfig(timeout_seconds=23),
        )
    ]

    assert chunks == ["ok"]
    assert captured["timeout"] == 23
