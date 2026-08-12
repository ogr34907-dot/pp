import httpx

from application.ai.llm_retry_policy import is_retryable_llm_error


def _status_error(status_code: int, message: str = "upstream failure") -> httpx.HTTPStatusError:
    request = httpx.Request("POST", "https://example.test/v1/chat/completions")
    response = httpx.Response(status_code, request=request)
    return httpx.HTTPStatusError(message, request=request, response=response)


def test_retry_policy_uses_http_status_before_message_markers():
    assert is_retryable_llm_error(_status_error(429)) is True
    assert is_retryable_llm_error(_status_error(400, "timeout parameter is invalid")) is False
