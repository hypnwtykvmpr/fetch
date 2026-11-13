"""Resource-exhaustion review: response size limits.

A fetcher's other job is to not be a DoS target. ``fetch_url`` and
``check_may_autonomously_fetch_url`` each read the entire response body into
memory before any limit is applied — and no limit exists at all:

- ``await client.get(...)`` buffers the fully decompressed body; httpx
  transparently decompresses ``Content-Encoding: gzip``, so a ~20 KB response
  decompresses into hundreds of MB of ``response.content`` (a 1000:1 gzip bomb
  decompresses in well under a second). The server then decodes it to ``str``
  (another copy) and hands it to BeautifulSoup/readabilipy, whose object trees
  are an order of magnitude larger again. One URL from the model can exhaust
  the whole server process.
- The robots.txt fetch has the same hole, and protego then parses the whole
  body; the body is also echoed verbatim into the ToolError the model sees.

These tests assert the defense the server should have (a per-response byte cap
applied after decompression, plus a Content-Length precheck). Against the
current code they are RED: each demonstrates an unbounded fetch succeeding.

They run against the real server over the MCP protocol (``harness.connect``)
with HTTP served by ``harness.FakeWeb`` — no test touches the network.
"""

from __future__ import annotations

import zlib
from typing import Any

import httpx
from pydantic import BaseModel

from mcp_server_fetch.server import _page_meta

from .harness import FakeWeb, connect, gz_page

PAGE = "https://example.com/page"
ROBOTS = "https://example.com/robots.txt"
TOOL_ERROR = "Error executing tool fetch: "

# Comfortably above any sane fetch cap, cheap to compress (~1000:1) and fast
# to decompress: the vulnerability is that the server buffers it at all.
BOMB_UNCOMPRESSED = 32 * 1024 * 1024
ROBOTS_BOMB_UNCOMPRESSED = 2 * 1024 * 1024


def _bomb(size: int) -> bytes:
    """gzip of ``size`` zero bytes, built in 1 MiB steps (never held decoded)."""
    c = zlib.compressobj(9, zlib.DEFLATED, 31)
    parts = [c.compress(b"0" * min(1 << 20, size - i)) for i in range(0, size, 1 << 20)]
    parts.append(c.flush())
    return b"".join(parts)


def wire(result: BaseModel) -> dict[str, Any]:
    return result.model_dump(
        by_alias=True, mode="json", exclude_none=True, exclude_unset=True
    )


def text_of(result: BaseModel) -> str:
    data = wire(result)
    [content] = data["content"]
    assert content["type"] == "text"
    return content["text"]


def plain(body: str, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, content=body.encode(), headers={"content-type": "text/plain"}
    )


async def test_gzip_bomb_page_is_refused_not_buffered(web: FakeWeb) -> None:
    # A text/plain page whose gzip-encoded body decompresses to 32 MB. There is
    # no size cap, so the server buffers every byte (twice: bytes and str).
    # Expected after the fix: a clear size error, not a successful fetch.
    web.add(ROBOTS, plain("", status=404))
    web.add(
        PAGE,
        gz_page(_bomb(BOMB_UNCOMPRESSED)),
    )
    async with connect(ignore_robots_txt=True) as (session, _):
        result = await session.call_tool("fetch", {"url": PAGE})
    assert wire(result)["isError"] is True, text_of(result)[:200]
    assert "too large" in text_of(result)


async def test_oversized_declared_page_is_refused_before_reading(web: FakeWeb) -> None:
    # The server should honor a declared Content-Length above the cap without
    # reading the body. Today it buffers whatever arrives.
    web.add(ROBOTS, plain("", status=404))
    web.add(
        PAGE,
        httpx.Response(
            200,
            content=b"pretend this is huge",
            headers={
                "content-type": "text/plain",
                "content-length": str(10 * 1024 * 1024 * 1024),
            },
        ),
    )
    async with connect(ignore_robots_txt=True) as (session, _):
        result = await session.call_tool("fetch", {"url": PAGE})
    assert wire(result)["isError"] is True
    assert "too large" in text_of(result)


async def test_gzip_bomb_robots_is_refused_not_buffered(web: FakeWeb) -> None:
    # robots.txt is fetched and parsed before every autonomous fetch, so a
    # bomb there costs memory *and* protego parse time on every tool call.
    web.add(
        ROBOTS,
        gz_page(_bomb(ROBOTS_BOMB_UNCOMPRESSED)),
    )
    web.add(PAGE, plain("should not be reached"))
    async with connect() as (session, _):
        result = await session.call_tool("fetch", {"url": PAGE})
    assert wire(result)["isError"] is True
    assert "too large" in text_of(result)
    assert web.urls() == [ROBOTS]


async def test_normal_sized_fetches_still_work(web: FakeWeb) -> None:
    # Regression guard for the cap: normal content is unaffected.
    web.add(ROBOTS, plain("", status=404))
    web.add(PAGE, plain("ordinary body"))
    async with connect(ignore_robots_txt=True) as (session, _):
        result = await session.call_tool("fetch", {"url": PAGE})
    assert wire(result)["isError"] is False
    assert text_of(result).endswith(
        "ordinary body" + _page_meta(len("ordinary body"), 0, len("ordinary body"))
    )
