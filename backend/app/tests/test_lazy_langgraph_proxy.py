"""Regression tests for the Gateway external LangGraph ASGI proxy."""

from __future__ import annotations

import asyncio

import httpx

from app.gateway.lazy_langgraph import ExternalLangGraphProxy, _upstream_path


def test_upstream_path_strips_starlette_mount_prefix() -> None:
    scope = {"type": "http", "path": "/api/langgraph/ok", "root_path": "/api/langgraph"}
    assert _upstream_path(scope) == "/ok"


def test_upstream_path_preserves_the_server_root_endpoint() -> None:
    scope = {"type": "http", "path": "/api/langgraph", "root_path": "/api/langgraph"}
    assert _upstream_path(scope) == "/"


def test_external_proxy_forwards_path_relative_to_mount() -> None:
    seen: list[str] = []

    class _SingleChunkStream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b'{"ok":true}'

        async def aclose(self) -> None:
            return None

    async def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(200, stream=_SingleChunkStream())

    async def run() -> list[dict]:
        proxy = ExternalLangGraphProxy(upstream_base="http://langgraph.test")
        proxy._client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        messages: list[dict] = []
        sent = False

        async def receive() -> dict:
            nonlocal sent
            if sent:
                return {"type": "http.disconnect"}
            sent = True
            return {"type": "http.request", "body": b"", "more_body": False}

        async def send(message: dict) -> None:
            messages.append(message)

        await proxy(
            {
                "type": "http",
                "method": "GET",
                "path": "/api/langgraph/ok",
                "root_path": "/api/langgraph",
                "query_string": b"probe=1",
                "headers": [],
            },
            receive,
            send,
        )
        await proxy.aclose()
        return messages

    messages = asyncio.run(run())
    assert seen == ["http://langgraph.test/ok?probe=1"]
    assert messages[0]["status"] == 200
