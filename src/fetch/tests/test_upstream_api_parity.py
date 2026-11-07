"""Compatibility for callers using the upstream positional server API."""

import asyncio
import threading
from unittest.mock import AsyncMock

import httpx
import pytest
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError

from mcp_server_fetch import server

from .harness import FakeWeb

PAGE = "https://example.com/page"
HTML = "<html><head><title>Legacy API</title></head><body><article><h1>Legacy API</h1><p>A useful paragraph with enough words to extract as article content.</p></article></body></html>"


def test_create_server_accepts_all_upstream_positional_arguments(monkeypatch):
    def unexpected_node_probe():
        raise AssertionError("the sixth argument disables Readability.js")

    monkeypatch.setattr(server._worker, "find_node", unexpected_node_probe)
    instance = server.create_server(
        "Caller/1.0", True, None, True, ["Example.com"], False
    )
    assert isinstance(instance, MCPServer)


async def test_serve_forwards_upstream_positional_arguments(monkeypatch):
    stdio = AsyncMock()
    monkeypatch.setattr(MCPServer, "run_stdio_async", stdio)
    await server.serve("Caller/1.0", True, None, True, ["Example.com"], False)
    stdio.assert_awaited_once_with()


async def test_fetch_url_accepts_nine_positional_arguments_and_returns_two_values(
    web: FakeWeb,
):
    seen = []

    def response(request):
        seen.append(request)
        return httpx.Response(200, text=HTML, headers={"content-type": "text/html"})

    web.add(PAGE, response)
    content, prefix = await server.fetch_url(
        PAGE, "Legacy/1.0", True, 1700, None, False, ("example.com",), False, False
    )
    assert content == HTML
    assert (
        prefix
        == "Content type text/html cannot be simplified to markdown, but here is the raw content:\n"
    )
    assert seen[0].headers["user-agent"] == "Legacy/1.0"
    assert seen[0].headers["accept"] == "*/*"
    assert set(seen[0].extensions["timeout"].values()) == {1.7}


async def test_fetch_url_simplifies_html_with_the_bounded_worker(web: FakeWeb):
    web.add(PAGE, httpx.Response(200, text=HTML, headers={"content-type": "text/html"}))
    content, prefix = await server.fetch_url(
        PAGE, "Legacy/1.0", use_readability_js=False
    )
    assert "Legacy API" in content
    assert "useful paragraph" in content
    assert "<article>" not in content
    assert "page-meta" not in content
    assert prefix == ""


async def test_fetch_url_returns_native_markdown_without_a_prefix(web: FakeWeb):
    markdown = "# Native\n\nUse an `<html>` tag.\n"
    web.add(
        PAGE,
        httpx.Response(200, text=markdown, headers={"content-type": "text/markdown"}),
    )
    assert await server.fetch_url(PAGE, "Legacy/1.0") == (markdown, "")


async def test_cold_node_probe_leaves_the_event_loop_responsive(
    web: FakeWeb, monkeypatch
):
    web.add(PAGE, httpx.Response(200, text=HTML, headers={"content-type": "text/html"}))
    loop = asyncio.get_running_loop()
    probe_started = asyncio.Event()
    release = threading.Event()

    def blocked_probe():
        loop.call_soon_threadsafe(probe_started.set)
        if not release.wait(1):
            raise AssertionError("the cold Node probe blocked the event loop")

    monkeypatch.setattr(server._worker, "find_node", blocked_probe)
    call = asyncio.create_task(server.fetch_url(PAGE, "Legacy/1.0"))
    try:
        # The event loop must run this heartbeat while the probe is waiting.
        await asyncio.wait_for(probe_started.wait(), timeout=2)
        release.set()
        content, prefix = await call
        assert "useful paragraph" in content
        assert prefix == ""
    finally:
        release.set()
        await call


@pytest.mark.parametrize(
    ("raw", "content_type"),
    [
        (True, "text/html"),
        (False, "text/markdown"),
        (False, "text/plain"),
        (False, "application/json"),
    ],
)
async def test_raw_and_declared_non_html_responses_never_probe_node(
    web: FakeWeb, monkeypatch, raw, content_type
):
    web.add(
        PAGE, httpx.Response(200, text=HTML, headers={"content-type": content_type})
    )

    def unexpected_probe():
        raise AssertionError("this response does not need Readability.js")

    monkeypatch.setattr(server._worker, "find_node", unexpected_probe)
    content, _ = await server.fetch_url(PAGE, "Legacy/1.0", force_raw=raw)
    assert "useful paragraph" in content


async def test_robots_helper_keeps_allowed_hosts_as_fifth_positional_argument(
    web: FakeWeb,
):
    web.add("https://example.com/robots.txt", httpx.Response(404))
    await server.check_may_autonomously_fetch_url(
        PAGE, "Legacy/1.0", None, False, ("example.com",)
    )
    with pytest.raises(ToolError, match="not one of the hosts"):
        await server.check_may_autonomously_fetch_url(
            "https://other.example/page", "Legacy/1.0", None, False, ("example.com",)
        )
    assert web.urls() == ["https://example.com/robots.txt"]


async def test_concurrent_legacy_html_calls_share_worker_admission(
    web: FakeWeb, monkeypatch
):
    web.add(PAGE, httpx.Response(200, text=HTML, headers={"content-type": "text/html"}))
    release = asyncio.Event()
    all_running = asyncio.Event()
    started = 0

    async def blocked_worker(data):
        nonlocal started
        started += 1
        if started == server._MAX_WORKERS:
            all_running.set()
        await release.wait()
        return {"content": "article", "links": "", "report": {}}

    monkeypatch.setattr(server, "_spawn_worker", blocked_worker)
    calls = [
        asyncio.create_task(
            server.fetch_url(PAGE, "Legacy/1.0", use_readability_js=False)
        )
        for _ in range(server._MAX_WORKERS + server._MAX_WAITERS)
    ]
    try:
        await asyncio.wait_for(all_running.wait(), timeout=2)
        with pytest.raises(ToolError, match="Too many fetched pages"):
            await server.fetch_url(PAGE, "Legacy/1.0", use_readability_js=False)
        assert started == server._MAX_WORKERS
    finally:
        release.set()
        await asyncio.gather(*calls)
