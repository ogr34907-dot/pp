# domain/ai/value_objects/token_usage.py
from dataclasses import dataclass


@dataclass(frozen=True)
class TokenUsage:
    """Token 使用量值对象"""
    input_tokens: int
    output_tokens: int
    cache_hit_tokens: int = 0
    cache_miss_tokens: int = 0
    reasoning_tokens: int = 0

    def __post_init__(self):
        if any(
            value < 0
            for value in (
                self.input_tokens,
                self.output_tokens,
                self.cache_hit_tokens,
                self.cache_miss_tokens,
                self.reasoning_tokens,
            )
        ):
            raise ValueError("Token counts cannot be negative")

    @property
    def total_tokens(self) -> int:
        """总 token 数"""
        return self.input_tokens + self.output_tokens

    def __add__(self, other: 'TokenUsage') -> 'TokenUsage':
        """相加两个 TokenUsage"""
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            cache_hit_tokens=self.cache_hit_tokens + other.cache_hit_tokens,
            cache_miss_tokens=self.cache_miss_tokens + other.cache_miss_tokens,
            reasoning_tokens=self.reasoning_tokens + other.reasoning_tokens,
        )
