import asyncio
from contextlib import suppress

import httpx
import pytest

from infrastructure.ai.http_timeout import (
    DEFAULT_HTTP_TIMEOUT_SETTINGS,
    build_httpx_timeout,
    build_llm_httpx_timeout,
)


def test_regular_http_timeout_keeps_read_deadline():
    timeout = build_httpx_timeout(DEFAULT_HTTP_TIMEOUT_SETTINGS)

    assert timeout.read == DEFAULT_HTTP_TIMEOUT_SETTINGS.read_timeout


def test_llm_http_timeout_does_not_apply_total_or_read_deadline():
    timeout = build_llm_httpx_timeout(DEFAULT_HTTP_TIMEOUT_SETTINGS)

    assert timeout.connect == DEFAULT_HTTP_TIMEOUT_SETTINGS.connect_timeout
    assert timeout.read is None
    assert timeout.write == DEFAULT_HTTP_TIMEOUT_SETTINGS.write_timeout
    assert timeout.pool == DEFAULT_HTTP_TIMEOUT_SETTINGS.pool_timeout


def test_llm_timeout_is_an_httpx_timeout_object_supported_by_sdks():
    timeout = build_llm_httpx_timeout(DEFAULT_HTTP_TIMEOUT_SETTINGS)

    assert isinstance(timeout, httpx.Timeout)


async def _start_delayed_server():
    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            request_head = await reader.readuntil(b"\r\n\r\n")
            request_line = request_head.splitlines()[0]
            if b"/stream" in request_line:
                writer.write(
                    b"HTTP/1.1 200 OK\r\n"
                    b"Transfer-Encoding: chunked\r\n"
                    b"Content-Type: text/event-stream\r\n"
                    b"Connection: close\r\n\r\n"
                )
                await writer.drain()
                for payload in (b"data: first\n\n", b"data: second\n\n"):
                    writer.write(f"{len(payload):X}\r\n".encode() + payload + b"\r\n")
                    await writer.drain()
                    await asyncio.sleep(0.03)
                writer.write(b"0\r\n\r\n")
                await writer.drain()
            else:
                body = b"delayed response"
                writer.write(
                    f"HTTP/1.1 200 OK\r\nContent-Length: {len(body)}\r\n"
                    "Content-Type: text/plain\r\nConnection: close\r\n\r\n".encode()
                )
                await writer.drain()
                await asyncio.sleep(0.03)
                writer.write(body)
                await writer.drain()
        except (ConnectionError, asyncio.IncompleteReadError):
            # The short-timeout client is expected to disconnect mid-response.
            pass
        finally:
            writer.close()
            with suppress(Exception):
                await writer.wait_closed()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    return server, f"http://127.0.0.1:{port}"


@pytest.mark.asyncio
async def test_llm_timeout_completes_after_slow_first_response_byte():
    server, base_url = await _start_delayed_server()
    settings = DEFAULT_HTTP_TIMEOUT_SETTINGS.__class__(
        timeout_seconds=0.01,
        connect_timeout=0.5,
        read_timeout=0.005,
        write_timeout=0.5,
        pool_timeout=0.5,
    )
    try:
        async with httpx.AsyncClient(
            timeout=build_httpx_timeout(settings)
        ) as bounded_client:
            with pytest.raises(httpx.ReadTimeout):
                await bounded_client.get(base_url + "/slow")

        async with httpx.AsyncClient(
            timeout=build_llm_httpx_timeout(settings)
        ) as llm_client:
            response = await llm_client.get(base_url + "/slow")

        assert response.text == "delayed response"
    finally:
        server.close()
        await server.wait_closed()


@pytest.mark.asyncio
async def test_llm_timeout_keeps_stream_alive_between_sse_chunks():
    server, base_url = await _start_delayed_server()
    settings = DEFAULT_HTTP_TIMEOUT_SETTINGS.__class__(
        timeout_seconds=0.01,
        connect_timeout=0.5,
        read_timeout=0.005,
        write_timeout=0.5,
        pool_timeout=0.5,
    )
    try:
        bounded_chunks: list[str] = []
        async with httpx.AsyncClient(timeout=build_httpx_timeout(settings)) as bounded_client:
            with pytest.raises(httpx.ReadTimeout):
                async with bounded_client.stream("GET", base_url + "/stream") as response:
                    async for chunk in response.aiter_text():
                        bounded_chunks.append(chunk)

        chunks: list[str] = []
        async with httpx.AsyncClient(timeout=build_llm_httpx_timeout(settings)) as llm_client:
            async with llm_client.stream("GET", base_url + "/stream") as response:
                async for chunk in response.aiter_text():
                    chunks.append(chunk)

        assert "".join(chunks) == "data: first\n\ndata: second\n\n"
        assert bounded_chunks
    finally:
        server.close()
        await server.wait_closed()
