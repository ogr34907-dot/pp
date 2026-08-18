from abc import ABC, abstractmethod
from typing import Any, AsyncIterator, Dict, Optional
from domain.ai.value_objects.prompt import Prompt
from domain.ai.value_objects.token_usage import TokenUsage


DEFAULT_MAX_OUTPUT_TOKENS = 120000
_UNSET = object()


class GenerationConfig:
    """生成配置"""
    def __init__(
        self,
        model: str | object = _UNSET,
        max_tokens: int | object = _UNSET,
        temperature: float | object = _UNSET,
        response_format: Optional[Dict] | object = _UNSET,
        *,
        timeout_seconds: float | None | object = _UNSET,
        reasoning_effort: str | None | object = _UNSET,
        thinking: Any = _UNSET,
    ):
        values = {
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "response_format": response_format,
            "timeout_seconds": timeout_seconds,
            "reasoning_effort": reasoning_effort,
            "thinking": thinking,
        }
        self._explicit_fields = {name for name, value in values.items() if value is not _UNSET}
        self.model = "" if model is _UNSET else str(model or "")
        self.max_tokens = DEFAULT_MAX_OUTPUT_TOKENS if max_tokens is _UNSET else max_tokens
        self.temperature = 1.0 if temperature is _UNSET else temperature
        self.response_format = None if response_format is _UNSET else response_format
        self.timeout_seconds = None if timeout_seconds is _UNSET else timeout_seconds
        self.reasoning_effort = None if reasoning_effort is _UNSET else reasoning_effort
        self.thinking = None if thinking is _UNSET else thinking
        self.__post_init__()

    def __post_init__(self):
        """验证配置参数"""
        if not (0.0 <= self.temperature <= 2.0):
            raise ValueError("Temperature must be between 0.0 and 2.0")
        if self.max_tokens <= 0:
            raise ValueError("max_tokens must be greater than 0")
        if self.timeout_seconds is not None and self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0")
        # Kept for API/storage compatibility. Providers intentionally do not
        # use this value as a wall-clock generation deadline.
        self.max_tokens = int(self.max_tokens)

    def is_explicit(self, name: str) -> bool:
        """Return whether a caller supplied this value instead of using a default."""
        return name in self._explicit_fields


class GenerationResult:
    """生成结果"""
    def __init__(self, content: str, token_usage: TokenUsage):
        self.content = content
        self.token_usage = token_usage
        self.__post_init__()

    def __post_init__(self):
        """验证结果参数"""
        if not self.content or not self.content.strip():
            raise ValueError("Content cannot be empty")


class LLMService(ABC):
    """LLM 服务接口（领域服务）"""

    @abstractmethod
    async def generate(
        self,
        prompt: Prompt,
        config: GenerationConfig
    ) -> GenerationResult:
        """生成内容"""
        pass

    @abstractmethod
    async def stream_generate(
        self,
        prompt: Prompt,
        config: GenerationConfig
    ) -> AsyncIterator[str]:
        """流式生成内容"""
        pass
