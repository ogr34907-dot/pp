"""LLM retry policy shared by provider and structured-output pipelines.

The limits here intentionally stay small: callers may retry transient transport
or empty-response failures, but no path should loop indefinitely or spend user
tokens without a bounded budget.
"""

import anthropic
import httpx
import openai


# 含首次调用在内，同一结构化/解析流程最多调用 LLM 的次数
LLM_MAX_TOTAL_ATTEMPTS = 3

_RETRYABLE_MARKERS = (
    "api returned empty content",
    "returned empty content",
    "empty non-stream content",
    "empty content",
    "empty response",
    "overloaded_error",
    "rate limit",
    "timeout",
    "temporar",
    "connection reset",
    "connection error",
    "service unavailable",
)

_RETRYABLE_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504, 529}
_RETRYABLE_TRANSPORT_ERRORS = (
    TimeoutError,
    ConnectionError,
    httpx.TimeoutException,
    httpx.TransportError,
    openai.APITimeoutError,
    openai.APIConnectionError,
    anthropic.APITimeoutError,
    anthropic.APIConnectionError,
)


def is_retryable_llm_error(exc: Exception | str) -> bool:
    """Return True for transient LLM/provider failures worth retrying.

    This deliberately includes empty upstream content. Several OpenAI-compatible
    gateways occasionally return a syntactically successful response with no
    text; treating that as retryable keeps module-specific callers from each
    inventing their own empty-response handling.
    """
    response = getattr(exc, "response", None)
    status_code = getattr(response, "status_code", getattr(exc, "status_code", None))
    if status_code is not None:
        return status_code in _RETRYABLE_STATUS_CODES
    if isinstance(exc, _RETRYABLE_TRANSPORT_ERRORS):
        return True
    message = str(exc).lower()
    return any(marker in message for marker in _RETRYABLE_MARKERS)
