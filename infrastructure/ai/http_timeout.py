"""Shared HTTP timeout construction for AI providers."""

from __future__ import annotations

from dataclasses import dataclass

import httpx


@dataclass(frozen=True)
class HttpTimeoutSettings:
    timeout_seconds: float = 300.0
    connect_timeout: float = 30.0
    read_timeout: float = 120.0
    write_timeout: float = 60.0
    pool_timeout: float = 30.0


DEFAULT_HTTP_TIMEOUT_SETTINGS = HttpTimeoutSettings()
DEFAULT_EMBEDDING_HTTP_TIMEOUT_SETTINGS = HttpTimeoutSettings(timeout_seconds=120.0)


def build_httpx_timeout(settings: HttpTimeoutSettings) -> httpx.Timeout:
    """Convert semantic timeout settings to httpx's layered timeout object."""
    return httpx.Timeout(
        timeout=settings.timeout_seconds,
        connect=settings.connect_timeout,
        read=settings.read_timeout,
        write=settings.write_timeout,
        pool=settings.pool_timeout,
    )


def build_llm_httpx_timeout(settings: HttpTimeoutSettings) -> httpx.Timeout:
    """Build an LLM timeout without a response/read deadline.

    ``timeout_seconds`` historically acted like a total generation deadline,
    but httpx timeouts are phase-based and an SDK request timeout can replace
    the client-level timeout.  LLMs may spend minutes thinking before the first
    token, or pause between streamed chunks, so neither case should be treated
    as a failed request.  Connection, write, and pool waits remain bounded so
    an unreachable endpoint does not consume a worker forever.
    """
    return httpx.Timeout(
        timeout=None,
        connect=settings.connect_timeout,
        read=None,
        write=settings.write_timeout,
        pool=settings.pool_timeout,
    )
