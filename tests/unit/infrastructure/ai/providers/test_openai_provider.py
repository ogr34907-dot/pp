"""OpenAIProvider 测试"""
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

import httpx
import openai
import pytest

from domain.ai.services.llm_service import GenerationConfig
from domain.ai.value_objects.prompt import Prompt
from infrastructure.ai.config.settings import Settings
from infrastructure.ai.providers.openai_provider import OpenAIProvider


class _FakeStream:
    def __init__(self, chunks):
        self._chunks = iter(chunks)

    def __aiter__(self):
        return self

    async def __anext__(self):
        try:
            return next(self._chunks)
        except StopIteration as exc:
            raise StopAsyncIteration from exc


def _api_error(error_type, status_code: int, message: str):
    request = httpx.Request("POST", "https://gateway.example/v1/responses")
    return error_type(message, response=httpx.Response(status_code, request=request), body=None)


class TestOpenAIProviderLegacy:
    """use_legacy_chat_completions=True → Chat Completions API"""

    @pytest.fixture
    def settings(self):
        return Settings(api_key="test-api-key", use_legacy_chat_completions=True)

    @pytest.fixture
    def provider(self, settings):
        return OpenAIProvider(settings)

    def test_initialization(self, provider, settings):
        assert provider.settings == settings
        assert provider.async_client is not None
        assert provider._use_legacy is True

    def test_http_timeout_uses_settings(self):
        provider = OpenAIProvider(
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
        assert timeout.read is None
        assert timeout.write == 8
        assert timeout.pool == 2

    @pytest.mark.anyio
    async def test_generate_requires_model_id(self, provider):
        prompt = Prompt(system="s", user="u")
        config = GenerationConfig(model="", max_tokens=32, temperature=0.5)
        with pytest.raises(ValueError, match="未配置模型 ID"):
            await provider.generate(prompt, config)

    @pytest.mark.anyio
    async def test_generate_non_stream(self, provider):
        prompt = Prompt(system="You are helpful", user="Hello")
        config = GenerationConfig(model="gpt-4o", temperature=0.7, max_tokens=4096)
        response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="Hi there!"))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )

        with patch.object(provider.async_client.chat.completions, "create", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = response

            result = await provider.generate(prompt, config)

            assert result.content == "Hi there!"
            assert result.token_usage.input_tokens == 10
            assert result.token_usage.output_tokens == 5

            call_kwargs = mock_create.call_args.kwargs
            assert call_kwargs["model"] == "gpt-4o"
            assert call_kwargs["temperature"] == 0.7
            assert call_kwargs["max_tokens"] == 4096

    @pytest.mark.anyio
    async def test_generate_accepts_message_content_as_list_of_text_parts(self, provider):
        """聚合网关 / 新协议常返回 content 为 [{type,text}] 列表，而非纯字符串。"""
        prompt = Prompt(system="s", user="u")
        config = GenerationConfig(model="gpt-4o", temperature=0, max_tokens=64)
        response = SimpleNamespace(
            choices=[
                SimpleNamespace(
                    message=SimpleNamespace(
                        content=[
                            {"type": "text", "text": '{"a":'},
                            {"type": "text", "text": ' 1}'},
                        ]
                    )
                )
            ],
            usage=SimpleNamespace(prompt_tokens=3, completion_tokens=4),
        )

        with patch.object(provider.async_client.chat.completions, "create", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = response

            result = await provider.generate(prompt, config)

            assert result.content == '{"a":\n 1}'

    @pytest.mark.anyio
    async def test_generate_retries_empty_non_stream_without_stream_replay(self, provider):
        prompt = Prompt(system="You are helpful", user="Hello")
        config = GenerationConfig(model="gpt-5.4", temperature=0, max_tokens=32)
        empty_response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=None))],
            usage=SimpleNamespace(prompt_tokens=19, completion_tokens=15),
        )
        def empty_stream():
            return _FakeStream([
                SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content=None))],
                    usage=SimpleNamespace(prompt_tokens=19, completion_tokens=15),
                ),
            ])

        with (
            patch.object(provider.async_client.chat.completions, "create", new_callable=AsyncMock) as mock_create,
            patch("infrastructure.ai.providers.openai_provider.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_create.side_effect = [item for _ in range(3) for item in (empty_response, empty_stream())]

            with pytest.raises(RuntimeError, match="empty content"):
                await provider.generate(prompt, config)

        assert mock_create.await_count == 3
        assert all(call.kwargs.get("stream") is not True for call in mock_create.await_args_list)

    @pytest.mark.anyio
    async def test_generate_empty_structured_chat_does_not_replay_as_stream(self, provider):
        prompt = Prompt(system="You are helpful", user="Hello")
        config = GenerationConfig(
            model="gpt-5.4",
            response_format={"type": "json_object"},
        )
        empty_response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=None))],
            usage=SimpleNamespace(prompt_tokens=19, completion_tokens=15),
        )
        def empty_stream():
            return _FakeStream([
                SimpleNamespace(
                    choices=[SimpleNamespace(delta=SimpleNamespace(content=None))],
                    usage=SimpleNamespace(prompt_tokens=19, completion_tokens=15),
                ),
            ])

        with (
            patch.object(provider.async_client.chat.completions, "create", new_callable=AsyncMock) as mock_create,
            patch("infrastructure.ai.providers.openai_provider.asyncio.sleep", new_callable=AsyncMock),
        ):
            mock_create.side_effect = [item for _ in range(3) for item in (empty_response, empty_stream())]

            with pytest.raises(RuntimeError, match="empty content"):
                await provider.generate(prompt, config)

        assert mock_create.await_count == 3
        assert all(call.kwargs.get("stream") is not True for call in mock_create.await_args_list)
        assert all(call.kwargs["response_format"] == {"type": "json_object"} for call in mock_create.await_args_list)

    @pytest.mark.anyio
    async def test_stream_generate(self, provider):
        prompt = Prompt(system="You are helpful", user="Hello")
        config = GenerationConfig(model="gpt-4o", temperature=0.7, max_tokens=32)
        stream = _FakeStream([
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content="Hi"))]),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=" there"))]),
            SimpleNamespace(choices=[SimpleNamespace(delta=SimpleNamespace(content=None))]),
        ])

        with patch.object(provider.async_client.chat.completions, "create", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = stream

            chunks = [chunk async for chunk in provider.stream_generate(prompt, config)]

            assert chunks == ["Hi", " there"]
            assert mock_create.await_args.kwargs["stream"] is True

    @pytest.mark.anyio
    async def test_generate_empty_content_raises(self, provider):
        prompt = Prompt(system="You are helpful", user="Hello")
        config = GenerationConfig(model="test-model")
        empty_response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=None))],
            usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        )
        with patch.object(provider.async_client.chat.completions, "create", new_callable=AsyncMock) as mock_create:
            mock_create.side_effect = [empty_response] * 3

            with pytest.raises(RuntimeError, match="empty content"):
                await provider.generate(prompt, config)

        assert mock_create.await_count == 3
        assert all(call.kwargs.get("stream") is not True for call in mock_create.await_args_list)

    def test_missing_api_key(self):
        with pytest.raises(ValueError, match="API key is required"):
            OpenAIProvider(Settings(api_key=None))


class TestOpenAIProviderResponses:
    """use_legacy_chat_completions=False（默认）→ Responses API"""

    @pytest.fixture(autouse=True)
    def _clear_capability_caches(self):
        OpenAIProvider._fallback_to_chat_cache.clear()
        OpenAIProvider._json_schema_unsupported_cache.clear()
        yield
        OpenAIProvider._fallback_to_chat_cache.clear()
        OpenAIProvider._json_schema_unsupported_cache.clear()

    @pytest.fixture
    def settings(self):
        return Settings(api_key="test-api-key", use_legacy_chat_completions=False)

    @pytest.fixture
    def provider(self, settings):
        return OpenAIProvider(settings)

    def test_default_uses_responses(self, provider):
        assert provider._use_legacy is False

    @pytest.mark.anyio
    async def test_generate_non_stream(self, provider):
        prompt = Prompt(system="You are helpful", user="Hello")
        config = GenerationConfig(model="gpt-4o", temperature=0.5, max_tokens=2048)
        response = SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="message",
                    content=[SimpleNamespace(type="text", text="Hi from responses!")],
                )
            ],
            usage=SimpleNamespace(prompt_tokens=8, completion_tokens=4),
        )

        with patch.object(provider.async_client.responses, "create", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = response

            result = await provider.generate(prompt, config)

            assert result.content == "Hi from responses!"
            assert result.token_usage.input_tokens == 8
            assert result.token_usage.output_tokens == 4

            call_kwargs = mock_create.call_args.kwargs
            assert call_kwargs["model"] == "gpt-4o"
            assert call_kwargs["temperature"] == 0.5
            assert call_kwargs["max_output_tokens"] == 2048

    @pytest.mark.anyio
    async def test_generate_responses_joins_multiple_text_parts(self, provider):
        prompt = Prompt(system="s", user="u")
        config = GenerationConfig(model="gpt-4o", temperature=0, max_tokens=32)
        response = SimpleNamespace(
            output=[
                SimpleNamespace(
                    type="message",
                    content=[SimpleNamespace(type="text", text="Line1")],
                ),
                SimpleNamespace(
                    type="message",
                    content=[SimpleNamespace(type="text", text="Line2")],
                ),
            ],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=2),
        )

        with patch.object(provider.async_client.responses, "create", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = response

            result = await provider.generate(prompt, config)

            assert result.content == "Line1\nLine2"

    @pytest.mark.anyio
    async def test_stream_generate(self, provider):
        prompt = Prompt(system="You are helpful", user="Hello")
        config = GenerationConfig(model="gpt-4o", temperature=0.7, max_tokens=32)
        stream = _FakeStream([
            SimpleNamespace(
                type="response.content_part.added",
                part=SimpleNamespace(type="text", text="Hello"),
            ),
            SimpleNamespace(type="response.completed"),
        ])

        with patch.object(provider.async_client.responses, "create", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = stream

            chunks = [chunk async for chunk in provider.stream_generate(prompt, config)]

            assert chunks == ["Hello"]
            assert mock_create.await_args.kwargs["stream"] is True

    @pytest.mark.anyio
    async def test_stream_generate_extracts_output_text_delta(self, provider):
        prompt = Prompt(system="You are helpful", user="Hello")
        config = GenerationConfig(model="gpt-4o", temperature=0.7, max_tokens=32)
        stream = _FakeStream([
            SimpleNamespace(type="response.output_text.delta", delta="Hel"),
            SimpleNamespace(type="response.output_text.delta", delta="lo"),
            SimpleNamespace(type="response.completed"),
        ])

        with patch.object(provider.async_client.responses, "create", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = stream

            chunks = [chunk async for chunk in provider.stream_generate(prompt, config)]

            assert chunks == ["Hel", "lo"]
            assert mock_create.await_args.kwargs["stream"] is True

    @pytest.mark.anyio
    async def test_generate_empty_responses_raises(self, provider):
        prompt = Prompt(system="You are helpful", user="Hello")
        config = GenerationConfig(model="test-model")
        response = SimpleNamespace(
            output=[],
            usage=SimpleNamespace(prompt_tokens=5, completion_tokens=0),
        )

        with patch.object(provider.async_client.responses, "create", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = response

            with pytest.raises(RuntimeError, match="empty content"):
                await provider.generate(prompt, config)

    @pytest.mark.anyio
    async def test_chat_json_schema_does_not_downgrade_on_unrelated_bad_request(self):
        provider = OpenAIProvider(
            Settings(api_key="test-api-key", use_legacy_chat_completions=True)
        )
        config = GenerationConfig(
            model="gpt-4o",
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "payload", "schema": {"type": "object"}},
            },
        )
        error = _api_error(openai.BadRequestError, 400, "max_tokens is invalid")

        with patch.object(provider.async_client.chat.completions, "create", new_callable=AsyncMock) as create:
            create.side_effect = error

            with pytest.raises(RuntimeError, match="max_tokens is invalid"):
                await provider.generate(Prompt(system="s", user="u"), config)

        assert create.await_count == 1

    @pytest.mark.anyio
    async def test_responses_uses_native_usage_and_structured_output_format(self, provider):
        prompt = Prompt(system="You are helpful", user="Hello")
        config = GenerationConfig(
            model="gpt-4o",
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "payload", "schema": {"type": "object"}},
            },
        )
        response = SimpleNamespace(
            output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="text", text="{}")])],
            usage=SimpleNamespace(
                input_tokens=8,
                output_tokens=4,
                input_tokens_details=SimpleNamespace(cached_tokens=3),
                output_tokens_details=SimpleNamespace(reasoning_tokens=2),
            ),
        )

        with patch.object(provider.async_client.responses, "create", new_callable=AsyncMock) as mock_create:
            mock_create.return_value = response
            result = await provider.generate(prompt, config)

        assert result.token_usage.input_tokens == 8
        assert result.token_usage.output_tokens == 4
        assert result.token_usage.cache_hit_tokens == 3
        assert result.token_usage.cache_miss_tokens == 5
        assert result.token_usage.reasoning_tokens == 2
        assert mock_create.call_args.kwargs["text"] == {
            "format": {
                "type": "json_schema",
                "name": "payload",
                "schema": {"type": "object"},
            }
        }

    @pytest.mark.anyio
    async def test_responses_bad_request_does_not_fallback_to_chat(self, provider):
        prompt = Prompt(system="You are helpful", user="Hello")
        config = GenerationConfig(model="gpt-4o")
        OpenAIProvider._fallback_to_chat_cache.clear()

        with (
            patch.object(provider.async_client.responses, "create", new_callable=AsyncMock) as responses_create,
            patch.object(provider.async_client.chat.completions, "create", new_callable=AsyncMock) as chat_create,
        ):
            responses_create.side_effect = _api_error(openai.BadRequestError, 400, "invalid reasoning parameter")

            with pytest.raises(RuntimeError, match="invalid reasoning parameter"):
                await provider.generate(prompt, config)

        chat_create.assert_not_called()
        assert not OpenAIProvider._fallback_to_chat_cache

    @pytest.mark.anyio
    @pytest.mark.parametrize(
        ("status_code", "message"),
        [
            (405, "Responses endpoint method not allowed"),
            (501, "Responses endpoint not implemented"),
        ],
    )
    async def test_responses_endpoint_capability_errors_fallback_to_chat_once(
        self, provider, status_code, message
    ):
        prompt = Prompt(system="You are helpful", user="Hello")
        config = GenerationConfig(model="gpt-4o")
        chat_response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="chat fallback"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

        with (
            patch.object(provider.async_client.responses, "create", new_callable=AsyncMock) as responses_create,
            patch.object(provider.async_client.chat.completions, "create", new_callable=AsyncMock) as chat_create,
        ):
            responses_create.side_effect = _api_error(openai.APIStatusError, status_code, message)
            chat_create.return_value = chat_response

            result = await provider.generate(prompt, config)

        assert result.content == "chat fallback"
        assert responses_create.await_count == 1
        assert chat_create.await_count == 1

    @pytest.mark.anyio
    async def test_responses_model_not_found_does_not_fallback_to_chat(self, provider):
        prompt = Prompt(system="You are helpful", user="Hello")
        config = GenerationConfig(model="gpt-4o")
        chat_response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="must not be used"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )

        with (
            patch.object(provider.async_client.responses, "create", new_callable=AsyncMock) as responses_create,
            patch.object(provider.async_client.chat.completions, "create", new_callable=AsyncMock) as chat_create,
        ):
            responses_create.side_effect = _api_error(
                openai.NotFoundError,
                404,
                "Responses API model deployment not found",
            )
            chat_create.return_value = chat_response

            with pytest.raises(RuntimeError, match="model deployment not found"):
                await provider.generate(prompt, config)

        responses_create.assert_awaited_once()
        chat_create.assert_not_called()

    @pytest.mark.anyio
    async def test_responses_json_schema_falls_back_only_for_explicit_format_unsupported(self, provider):
        prompt = Prompt(system="You are helpful", user="Hello")
        config = GenerationConfig(
            model="gpt-4o",
            response_format={
                "type": "json_schema",
                "json_schema": {"name": "payload", "schema": {"type": "object"}},
            },
        )
        response = SimpleNamespace(
            output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="text", text="{}")])],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )
        error = _api_error(openai.BadRequestError, 400, "json_schema format is not supported")

        with patch.object(provider.async_client.responses, "create", new_callable=AsyncMock) as create:
            create.side_effect = [error, response]
            result = await provider.generate(prompt, config)

        assert result.content == "{}"
        assert create.await_count == 2
        assert create.await_args_list[1].kwargs["text"] == {"format": {"type": "json_object"}}

    @pytest.mark.anyio
    async def test_responses_passes_profile_extra_body_through_sdk_argument(self):
        provider = OpenAIProvider(
            Settings(
                api_key="test-api-key",
                extra_body={"provider_option": {"enabled": True}},
            )
        )
        response = SimpleNamespace(
            output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="text", text="ok")])],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )

        with patch.object(provider.async_client.responses, "create", new_callable=AsyncMock) as create:
            create.return_value = response
            await provider.generate(Prompt(system="s", user="u"), GenerationConfig(model="gpt-4o"))

        kwargs = create.await_args.kwargs
        assert kwargs["extra_body"] == {"provider_option": {"enabled": True}}
        assert "provider_option" not in kwargs

    @pytest.mark.anyio
    async def test_responses_capability_cache_isolated_by_model(self, provider):
        prompt = Prompt(system="You are helpful", user="Hello")
        config_a = GenerationConfig(model="gateway-model-a")
        config_b = GenerationConfig(model="gateway-model-b")
        OpenAIProvider._fallback_to_chat_cache.clear()
        fallback_response = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="chat fallback"))],
            usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1),
        )
        responses_response = SimpleNamespace(
            output=[SimpleNamespace(type="message", content=[SimpleNamespace(type="text", text="responses")])],
            usage=SimpleNamespace(input_tokens=1, output_tokens=1),
        )

        try:
            with (
                patch.object(provider.async_client.responses, "create", new_callable=AsyncMock) as responses_create,
                patch.object(provider.async_client.chat.completions, "create", new_callable=AsyncMock) as chat_create,
            ):
                responses_create.side_effect = [
                    _api_error(openai.NotFoundError, 404, "Responses API endpoint not found"),
                    responses_response,
                ]
                chat_create.return_value = fallback_response

                assert (await provider.generate(prompt, config_a)).content == "chat fallback"
                assert (await provider.generate(prompt, config_b)).content == "responses"

            assert responses_create.await_count == 2
            assert chat_create.await_count == 1
        finally:
            OpenAIProvider._fallback_to_chat_cache.clear()

    def test_openai_request_applies_explicit_reasoning_and_deepseek_thinking(self):
        provider = OpenAIProvider(
            Settings(api_key="test-api-key", base_url="https://api.deepseek.com/v1")
        )
        config = GenerationConfig(
            model="deepseek-v4-pro",
            reasoning_effort="high",
            thinking="enabled",
        )

        chat_kwargs = provider._build_chat_request_kwargs([], config)
        responses_kwargs = provider._build_responses_request_kwargs(Prompt(system="s", user="u"), config)

        assert chat_kwargs["reasoning_effort"] == "high"
        assert chat_kwargs["extra_body"]["thinking"] == {"type": "enabled"}
        assert responses_kwargs["reasoning"] == {"effort": "high"}

    def test_openai_request_does_not_add_reasoning_controls_when_omitted(self):
        provider = OpenAIProvider(Settings(api_key="test-api-key"))
        config = GenerationConfig(model="gpt-4o")

        chat_kwargs = provider._build_chat_request_kwargs([], config)
        responses_kwargs = provider._build_responses_request_kwargs(Prompt(system="s", user="u"), config)

        assert "reasoning_effort" not in chat_kwargs
        assert "reasoning" not in responses_kwargs

    def test_responses_capability_key_normalizes_responses_endpoint_suffix(self):
        provider = OpenAIProvider(
            Settings(api_key="test-api-key", base_url="https://gateway.example/v1/responses")
        )

        assert provider._capability_key_for_model("model") == (
            "https://gateway.example/v1",
            "model",
        )


class TestProfilePassthrough:
    """profile 的 use_legacy_chat_completions 正确透传到 OpenAIProvider"""

    def test_legacy_flag_passed_through(self):
        settings_legacy = Settings(api_key="k", use_legacy_chat_completions=True)
        provider_legacy = OpenAIProvider(settings_legacy)
        assert provider_legacy._use_legacy is True

        settings_new = Settings(api_key="k", use_legacy_chat_completions=False)
        provider_new = OpenAIProvider(settings_new)
        assert provider_new._use_legacy is False

    def test_default_is_responses(self):
        settings = Settings(api_key="k")
        provider = OpenAIProvider(settings)
        assert provider._use_legacy is False
