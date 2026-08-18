"""OpenAI LLM 提供商实现"""
import asyncio
from dataclasses import replace
import logging
import openai
import httpx
from typing import Any, AsyncIterator

from openai import AsyncOpenAI

from domain.ai.services.llm_service import GenerationConfig, GenerationResult
from domain.ai.value_objects.prompt import Prompt
from domain.ai.value_objects.token_usage import TokenUsage
from application.ai.llm_retry_policy import LLM_MAX_TOTAL_ATTEMPTS, is_retryable_llm_error
from infrastructure.ai.config.settings import Settings
from infrastructure.ai.http_timeout import build_llm_httpx_timeout
from infrastructure.ai.url_utils import normalize_openai_base_url
from .base import BaseProvider
from .model_resolution import require_resolved_model_id

logger = logging.getLogger(__name__)


class OpenAIProvider(BaseProvider):
    """OpenAI LLM 提供商实现

    通过 use_legacy_chat_completions 显式选择协议：
    - False（默认）：走 Responses API，失败时自动降级到 Chat Completions
    - True：走 Chat Completions API
    """

    # Responses 与 json_schema 支持度会随网关和模型变化。
    _fallback_to_chat_cache: set[tuple[str, str]] = set()
    _json_schema_unsupported_cache: set[tuple[str, str]] = set()

    def __init__(self, settings: Settings):
        normalized_base_url = normalize_openai_base_url(settings.base_url)
        super().__init__(
            replace(settings, base_url=normalized_base_url)
            if normalized_base_url != settings.base_url
            else settings
        )

        if not self.settings.api_key:
            raise ValueError("API key is required for OpenAIProvider")

        self._use_legacy = settings.use_legacy_chat_completions

        self._llm_timeout = build_llm_httpx_timeout(self.settings.http_timeout_settings)
        client_kwargs = {
            "api_key": settings.api_key,
            "timeout": self._llm_timeout,
            "default_headers": self.settings.extra_headers or None,
            "default_query": self.settings.extra_query or None,
        }
        if self.settings.base_url:
            client_kwargs["base_url"] = self.settings.base_url

        self._http_client = httpx.AsyncClient(
            timeout=self._llm_timeout,
            trust_env=False,
        )
        client_kwargs["http_client"] = self._http_client
        self.async_client = AsyncOpenAI(**client_kwargs)

    async def generate(
        self,
        prompt: Prompt,
        config: GenerationConfig
    ) -> GenerationResult:
        try:
            capability_key = self._responses_capability_key(config)
            use_responses = not self._use_legacy and capability_key not in self.__class__._fallback_to_chat_cache

            if use_responses:
                try:
                    return await self._generate_via_responses(prompt, config)
                except Exception as e:
                    if not self._responses_endpoint_is_unsupported(e):
                        raise
                    logger.info("Responses endpoint unsupported for %s; using Chat Completions", capability_key)
                    self.__class__._fallback_to_chat_cache.add(capability_key)

            # 使用降级的 Chat Completions API
            return await self._generate_via_chat(prompt, config)
        except RuntimeError:
            raise
        except ValueError:
            raise
        except Exception as e:
            raise RuntimeError(f"Failed to generate text: {str(e)}") from e

    async def _generate_via_chat(self, prompt: Prompt, config: GenerationConfig) -> GenerationResult:
        """Chat Completions API 非流式生成

        🔥 自适应容错策略：
        1. 如果指定了 json_schema response_format 但网关返回 400，自动降级到 json_object
        2. 空完成响应按统一 LLM 重试预算有限重试；单次尝试不得重放为流式请求
        """
        messages = self._build_messages(prompt)
        last_error: Exception | None = None

        for attempt in range(LLM_MAX_TOTAL_ATTEMPTS):
            request_kwargs = self._build_chat_request_kwargs(messages, config)
            try:
                try:
                    response = await self.async_client.chat.completions.create(**request_kwargs)
                except Exception as e:
                    if (
                        config.response_format
                        and config.response_format.get("type") == "json_schema"
                        and self._json_schema_format_is_unsupported(e)
                    ):
                        capability_key = self._capability_key_for_model(request_kwargs["model"])
                        logger.info(
                            "json_schema 不支持，自动降级到 json_object: %s (错误: %s)",
                            capability_key, str(e)[:100]
                        )
                        self.__class__._json_schema_unsupported_cache.add(capability_key)
                        request_kwargs["response_format"] = {"type": "json_object"}
                        response = await self.async_client.chat.completions.create(**request_kwargs)
                    else:
                        raise

                content = self._extract_text_from_response(response)

                if not content:
                    raise RuntimeError("API returned empty content")

                return GenerationResult(
                    content=content,
                    token_usage=self._token_usage_from_usage(getattr(response, "usage", None)),
                )
            except Exception as exc:
                last_error = exc
                if attempt >= LLM_MAX_TOTAL_ATTEMPTS - 1 or not is_retryable_llm_error(exc):
                    raise
                delay = min(1.5 * (2 ** attempt), 8.0)
                logger.warning(
                    "OpenAI-compatible provider transient failure; retrying in %.1fs "
                    "(attempt=%d/%d): %s",
                    delay,
                    attempt + 1,
                    LLM_MAX_TOTAL_ATTEMPTS,
                    exc,
                )
                await asyncio.sleep(delay)

        if last_error is not None:
            raise last_error
        raise RuntimeError("API returned empty content")

    async def stream_generate(
        self,
        prompt: Prompt,
        config: GenerationConfig
    ) -> AsyncIterator[str]:
        try:
            capability_key = self._responses_capability_key(config)
            use_responses = not self._use_legacy and capability_key not in self.__class__._fallback_to_chat_cache

            if use_responses:
                request_kwargs = self._build_responses_request_kwargs(prompt, config, stream=True)
                try:
                    stream = await self.async_client.responses.create(**request_kwargs)
                except Exception as e:
                    text_format = self._responses_text_format(config.response_format)
                    if (
                        text_format
                        and text_format.get("type") == "json_schema"
                        and self._json_schema_format_is_unsupported(e)
                    ):
                        self.__class__._json_schema_unsupported_cache.add(capability_key)
                        request_kwargs["text"] = {"format": {"type": "json_object"}}
                        stream = await self.async_client.responses.create(**request_kwargs)
                    else:
                        if not self._responses_endpoint_is_unsupported(e):
                            raise
                        self.__class__._fallback_to_chat_cache.add(capability_key)
                        logger.info("Responses endpoint unsupported for %s; using Chat Completions", capability_key)
                        stream = None
                else:
                    pass
                if stream is not None:
                    async for chunk in stream:
                        content = self._extract_text_from_responses_chunk(chunk)
                        if content:
                            yield content
                    return

            # 降级：走原来的 Chat Completions 流式 API
            messages = self._build_messages(prompt)
            request_kwargs = self._build_chat_request_kwargs(messages, config, stream=True)
            stream = await self.async_client.chat.completions.create(**request_kwargs)
            async for chunk in stream:
                content = self._extract_text_from_stream_chunk(chunk)
                if content:
                    yield content
        except Exception as e:
            logger.error(f"[Stream] Failed: {e}")
            raise RuntimeError(f"Failed to stream text: {str(e)}") from e

    @staticmethod
    def _build_messages(prompt: Prompt) -> list[dict[str, str]]:
        return [
            {"role": "system", "content": prompt.system},
            {"role": "user", "content": prompt.user}
        ]

    def _capability_key_for_model(self, model_id: str) -> tuple[str, str]:
        base_url = normalize_openai_base_url(self.settings.base_url) or "https://api.openai.com/v1"
        return base_url.rstrip("/").lower(), model_id

    def _responses_capability_key(self, config: GenerationConfig) -> tuple[str, str]:
        return self._capability_key_for_model(
            require_resolved_model_id(
                config.model,
                self.settings.default_model,
                provider_label="OpenAI 兼容",
            )
        )

    @staticmethod
    def _responses_endpoint_is_unsupported(exc: Exception) -> bool:
        response = getattr(exc, "response", None)
        status_code = getattr(response, "status_code", None)
        if status_code in (405, 501):
            return True
        if status_code != 404:
            return False
        body = getattr(exc, "body", None)
        response_text = getattr(response, "text", None)
        message = " ".join(
            str(part).lower()
            for part in (str(exc), body, response_text)
            if part not in (None, "")
        )
        resource_markers = ("model", "deployment", "resource")
        missing_markers = (
            "not found",
            "does not exist",
            "doesn't exist",
            "not available",
        )
        if any(marker in message for marker in resource_markers) and any(
            marker in message for marker in missing_markers
        ):
            return False
        endpoint_markers = (
            "responses endpoint",
            "responses api endpoint",
            "/v1/responses",
            "responses path",
        )
        unsupported_markers = (
            "not found",
            "not supported",
            "unsupported",
            "does not support",
            "doesn't support",
        )
        return any(marker in message for marker in endpoint_markers) and any(
            marker in message for marker in unsupported_markers
        )

    @staticmethod
    def _json_schema_format_is_unsupported(exc: Exception) -> bool:
        message = str(exc).lower()
        return (
            ("json_schema" in message or "json schema" in message)
            and any(marker in message for marker in ("not supported", "unsupported", "does not support"))
        )

    @staticmethod
    def _responses_text_format(response_format: dict[str, Any] | None) -> dict[str, Any] | None:
        if not response_format:
            return None
        if response_format.get("type") == "json_object":
            return {"type": "json_object"}
        if response_format.get("type") != "json_schema":
            return None
        schema_config = response_format.get("json_schema") or {}
        schema = schema_config.get("schema")
        if not isinstance(schema, dict):
            return None
        result: dict[str, Any] = {
            "type": "json_schema",
            "name": str(schema_config.get("name") or "response"),
            "schema": schema,
        }
        if "strict" in schema_config:
            result["strict"] = bool(schema_config["strict"])
        return result

    def _request_timeout(self, config: GenerationConfig) -> httpx.Timeout:
        """Return phase limits while leaving model response reads unbounded.

        ``GenerationConfig.timeout_seconds`` is retained for API/storage
        compatibility, but is no longer used as a hard generation deadline.
        """
        return self._llm_timeout

    def _is_deepseek_model(self, model_id: str) -> bool:
        return "deepseek" in model_id.lower() or "deepseek" in (self.settings.base_url or "").lower()

    @staticmethod
    def _usage_field(source: Any, name: str, default: Any = None) -> Any:
        if isinstance(source, dict):
            return source.get(name, default)
        return getattr(source, name, default)

    @classmethod
    def _token_usage_from_usage(cls, usage: Any) -> TokenUsage:
        def number(value: Any) -> int:
            return int(value) if isinstance(value, (int, float)) else 0

        input_tokens = number(cls._usage_field(usage, "input_tokens", None))
        if not input_tokens:
            input_tokens = number(cls._usage_field(usage, "prompt_tokens", 0))
        output_tokens = number(cls._usage_field(usage, "output_tokens", None))
        if not output_tokens:
            output_tokens = number(cls._usage_field(usage, "completion_tokens", 0))

        input_details = cls._usage_field(usage, "input_tokens_details", None)
        if input_details is None:
            input_details = cls._usage_field(usage, "prompt_tokens_details", None)
        output_details = cls._usage_field(usage, "output_tokens_details", None)
        if output_details is None:
            output_details = cls._usage_field(usage, "completion_tokens_details", None)

        cache_hit_tokens = number(cls._usage_field(input_details, "cached_tokens", None))
        if not cache_hit_tokens:
            cache_hit_tokens = number(cls._usage_field(usage, "prompt_cache_hit_tokens", 0))
        cache_miss_value = cls._usage_field(usage, "prompt_cache_miss_tokens", None)
        if cache_miss_value is None:
            cache_miss_value = cls._usage_field(usage, "cache_miss_tokens", None)
        cache_miss_tokens = number(cache_miss_value)
        if cache_miss_value is None:
            cache_miss_tokens = max(0, input_tokens - cache_hit_tokens)
        reasoning_tokens = number(cls._usage_field(output_details, "reasoning_tokens", None))
        if not reasoning_tokens:
            reasoning_tokens = number(cls._usage_field(usage, "reasoning_tokens", 0))

        return TokenUsage(
            input_tokens=input_tokens,
            output_tokens=output_tokens,
            cache_hit_tokens=cache_hit_tokens,
            cache_miss_tokens=cache_miss_tokens,
            reasoning_tokens=reasoning_tokens,
        )

    def _build_chat_request_kwargs(
        self,
        messages: list[dict[str, str]],
        config: GenerationConfig,
        *,
        stream: bool = False,
    ) -> dict[str, Any]:
        model_id = require_resolved_model_id(
            config.model,
            self.settings.default_model,
            provider_label="OpenAI 兼容",
        )
        extra_body = dict(self.settings.extra_body or {})
        if self._is_deepseek_model(model_id) and config.is_explicit("thinking") and config.thinking is not None:
            if isinstance(config.thinking, dict):
                extra_body["thinking"] = config.thinking
            elif isinstance(config.thinking, bool):
                extra_body["thinking"] = {"type": "enabled" if config.thinking else "disabled"}
            else:
                extra_body["thinking"] = {"type": str(config.thinking)}

        kwargs: dict[str, Any] = {
            "model": model_id,
            "messages": messages,
            "temperature": config.temperature,
            "max_tokens": config.max_tokens,
            "extra_headers": self.settings.extra_headers or None,
            "extra_query": self.settings.extra_query or None,
            "extra_body": extra_body or None,
            "timeout": self._request_timeout(config),
        }
        if config.is_explicit("reasoning_effort") and config.reasoning_effort:
            kwargs["reasoning_effort"] = config.reasoning_effort

        # 🔥 response_format 自适应降级策略
        # 不同网关对 response_format 的支持程度不同：
        #   OpenAI 官方: json_schema ✅ | json_object ✅
        #   DeepSeek:     json_schema ❌ | json_object ✅
        #   Qwen/DashScope: json_schema ❌ | json_object ✅ (部分)
        #   豆包/Ark:     json_schema ❌ | json_object ✅ (部分)
        #   智谱/GLM:     json_schema ❌ | json_object ✅
        if config.response_format:
            fmt = config.response_format
            capability_key = self._capability_key_for_model(model_id)

            if fmt.get("type") == "json_schema" and capability_key in self.__class__._json_schema_unsupported_cache:
                # 已知不支持 json_schema，自动降级到 json_object
                logger.debug("json_schema 已知不支持，降级到 json_object: %s", capability_key)
                kwargs["response_format"] = {"type": "json_object"}
            else:
                kwargs["response_format"] = fmt

        if stream:
            kwargs["stream"] = True
        return kwargs

    def _build_responses_request_kwargs(
        self,
        prompt: Prompt,
        config: GenerationConfig,
        *,
        stream: bool = False,
    ) -> dict[str, Any]:
        model_id = require_resolved_model_id(
            config.model,
            self.settings.default_model,
            provider_label="OpenAI 兼容",
        )
        kwargs: dict[str, Any] = {
            "model": model_id,
            "instructions": prompt.system,
            "input": [{"role": "user", "content": prompt.user}],
            "temperature": config.temperature,
            "max_output_tokens": config.max_tokens,
            "extra_headers": self.settings.extra_headers or None,
            "extra_query": self.settings.extra_query or None,
            "extra_body": self.settings.extra_body or None,
            "timeout": self._request_timeout(config),
        }
        text_format = self._responses_text_format(config.response_format)
        if text_format is not None:
            kwargs["text"] = {"format": text_format}
        if config.is_explicit("reasoning_effort") and config.reasoning_effort:
            kwargs["reasoning"] = {"effort": config.reasoning_effort}

        if stream:
            kwargs["stream"] = True
        return kwargs

    async def _generate_via_responses(self, prompt: Prompt, config: GenerationConfig) -> GenerationResult:
        """Responses API 非流式生成"""
        request_kwargs = self._build_responses_request_kwargs(prompt, config)
        try:
            response = await self.async_client.responses.create(**request_kwargs)
        except Exception as exc:
            text_format = self._responses_text_format(config.response_format)
            if not (
                text_format
                and text_format.get("type") == "json_schema"
                and self._json_schema_format_is_unsupported(exc)
            ):
                raise
            capability_key = self._capability_key_for_model(str(request_kwargs["model"]))
            self.__class__._json_schema_unsupported_cache.add(capability_key)
            request_kwargs["text"] = {"format": {"type": "json_object"}}
            response = await self.async_client.responses.create(**request_kwargs)

        output = getattr(response, "output", None)
        content_parts: list[str] = []
        if output:
            for item in output:
                if getattr(item, "type", "") == "message":
                    for part in getattr(item, "content", []):
                        if getattr(part, "type", "") == "text":
                            piece = str(getattr(part, "text", "")).strip()
                            if piece:
                                content_parts.append(piece)
        content = "\n".join(content_parts).strip()
        if not content:
            raise RuntimeError("Responses API returned empty content")

        return GenerationResult(
            content=content,
            token_usage=self._token_usage_from_usage(getattr(response, "usage", None))
        )

    @staticmethod
    def _extract_text_from_responses_chunk(chunk: Any) -> str:
        """原生 Responses stream 解析封装"""
        try:
            event_type = getattr(chunk, "type", "")
            if event_type == "response.output_text.delta":
                delta = getattr(chunk, "delta", None)
                if isinstance(delta, str):
                    return delta
            elif event_type in ("response.content_part.added", "response.content_part.delta"):
                part = getattr(chunk, "part", None)
                if part and getattr(part, "type", "") == "text":
                    return getattr(part, "text", "")
                delta = getattr(chunk, "delta", None)
                if isinstance(delta, str):
                    return delta
            elif event_type == "message.delta":
                delta = getattr(chunk, "delta", None)
                if delta:
                     content = getattr(delta, "content", None)
                     if isinstance(content, str):
                         return content
        except Exception:
            pass
        return ""

    @staticmethod
    def _normalize_chat_completion_content(content: Any) -> str:
        """兼容 message.content 为 str 或多段 content part 列表。

        🔥 自适应兼容多种网关的响应格式：
        1. 标准 OpenAI: content 是 str
        2. OpenAI 新协议: content 是 list[{type, text}]
        3. DeepSeek-R1: content 是 str，但 message.reasoning_content 单独存在
        4. 部分网关: content 是 list，包含 thinking/reasoning 类型 part
        5. 极端情况: content 是 None（空回复）
        """
        if content is None:
            return ""
        if isinstance(content, str):
            return content.strip()

        if isinstance(content, list):
            parts: list[str] = []
            for item in content:
                if isinstance(item, dict):
                    item_type = (item.get("type") or "").lower()
                    # 🔥 跳过推理/思考/拒绝类型的 content part
                    # DeepSeek-R1: reasoning_content 在 message 级别，不在 content part 中
                    # 但部分网关会把 thinking 放在 content part 里
                    if item_type in ("reasoning", "thinking", "refusal", "thought"):
                        continue
                    text_val = item.get("text")
                    if isinstance(text_val, str) and text_val.strip():
                        parts.append(text_val)
                else:
                    text_attr = getattr(item, "text", None)
                    if isinstance(text_attr, str) and text_attr.strip():
                        parts.append(text_attr)
            return "\n".join(parts).strip()

        return str(content).strip()

    @staticmethod
    def _extract_text_from_response(response: Any) -> str:
        if not getattr(response, "choices", None):
            return ""

        message = getattr(response.choices[0], "message", None)
        content = getattr(message, "content", None)
        result = OpenAIProvider._normalize_chat_completion_content(content)

        # 🔥 DeepSeek-R1 等模型：正文在 message.content，思考在 message.reasoning_content
        # reasoning_content 不需要返回给调用方（已经由 llm_output_sanitize 处理）
        # 但如果 content 为空且 reasoning_content 不为空，说明模型只输出了思考没有正文
        if not result.strip():
            reasoning = getattr(message, "reasoning_content", None)
            if reasoning and isinstance(reasoning, str) and reasoning.strip():
                logger.debug("message.content 为空但有 reasoning_content，模型可能只输出了推理")

        return result

    @staticmethod
    def _extract_text_from_stream_chunk(chunk: Any) -> str:
        if not getattr(chunk, "choices", None):
            # 🔥 容错：部分网关返回的流式 chunk 没有 choices 字段
            # 例如 DeepSeek-R1 的 reasoning_content 字段在顶层
            return ""

        choice = chunk.choices[0]
        delta = getattr(choice, "delta", None)
        content = getattr(delta, "content", None)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return OpenAIProvider._normalize_chat_completion_content(content)
        return ""

    @staticmethod
    def _extract_text_from_stream_chunk(chunk: Any) -> str:
        if not getattr(chunk, "choices", None):
            return ""

        delta = getattr(chunk.choices[0], "delta", None)
        content = getattr(delta, "content", None)
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            return OpenAIProvider._normalize_chat_completion_content(content)
        return ""
