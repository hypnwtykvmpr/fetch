"""Targeted tests for the pagination meta line and the other new capabilities.

The pagination contract: every successful fetch ends with a machine-readable
``<page-meta>`` line built by ``_page_meta`` (the single source of truth the
pinned tests share), the truncation notice follows it after a single newline,
and an exhausted call (``start_index`` at or past the end) reports
``returned_length="0"`` and ``has_more="false"`` instead of leaving the agent
to burn a call discovering the end. ``include_links`` appends the page's
hyperlink targets, and a binary body is reported as such instead of being
returned as decoded noise.
"""

from __future__ import annotations

import httpx
from mcp.types import CallToolResult

from mcp_server_fetch.server import _page_meta

from .harness import FakeWeb, connect

PAGE = "https://example.com/page"
PREFIX = (
    "Content type text/plain cannot be simplified to markdown, "
    "but here is the raw content:\n"
)
HEADING = f"Contents of {PAGE}:\n"


def plain(body: str, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, content=body.encode(), headers={"content-type": "text/plain"}
    )


def html(body: str) -> httpx.Response:
    return httpx.Response(
        200, content=body.encode(), headers={"content-type": "text/html"}
    )


def text_of(result: CallToolResult) -> str:
    data = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    [block] = data["content"]
    assert block["type"] == "text"
    return block["text"]


async def test_meta_line_states_the_exact_pagination(web: FakeWeb) -> None:
    # A ten-character page read four characters at a time: the meta line
    # reports the whole page, the window that was returned, and where the
    # next window starts.
    page = "0123456789"
    web.add(PAGE, plain(page))
    async with connect(ignore_robots_txt=True) as (session, _):
        result = await session.call_tool("fetch", {"url": PAGE, "max_length": 4})
    assert text_of(result) == (
        f"{PREFIX}{HEADING}0123"
        + _page_meta(len(page), 0, 4)
        + "\n<error>Content truncated. Call the fetch tool with a start_index "
        "of 4 to get more content.</error>"
    )


async def test_exhausted_call_carries_its_meta_line(web: FakeWeb) -> None:
    # The last call of a walk -- start_index at or past the end -- is a
    # successful fetch too, so it carries the meta line: returned_length 0
    # and has_more false, instead of making the agent guess.
    page = "0123456789"
    web.add(PAGE, plain(page))
    async with connect(ignore_robots_txt=True) as (session, _):
        at_end = await session.call_tool("fetch", {"url": PAGE, "start_index": 10})
        past_end = await session.call_tool("fetch", {"url": PAGE, "start_index": 999})
    assert text_of(at_end) == (
        f"{PREFIX}{HEADING}<error>No more content available.</error>"
        + _page_meta(len(page), 10, 0)
    )
    assert text_of(past_end) == (
        f"{PREFIX}{HEADING}<error>No more content available.</error>"
        + _page_meta(len(page), 999, 0)
    )


async def test_truncation_notice_comes_after_the_meta_line(web: FakeWeb) -> None:
    page = "abcdefghijklmnopqrstuvwxyz"
    web.add(PAGE, plain(page))
    async with connect(ignore_robots_txt=True) as (session, _):
        truncated = await session.call_tool(
            "fetch", {"url": PAGE, "start_index": 0, "max_length": 10}
        )
        # A window that runs past the end: no hint to misplace.
        exact = await session.call_tool(
            "fetch", {"url": PAGE, "start_index": 20, "max_length": 100}
        )
    text = text_of(truncated)
    assert text.count("<page-meta") == 1
    # The hint follows the meta line, separated from it by one newline.
    assert text.endswith(
        _page_meta(26, 0, 10)
        + "\n<error>Content truncated. Call the fetch tool with a start_index "
        "of 10 to get more content.</error>"
    )
    # A short final window: the meta line alone, no stale hint.
    assert text_of(exact) == f"{PREFIX}{HEADING}uvwxyz" + _page_meta(26, 20, 6)


async def test_include_links_extracts_absolute_targets_and_skips_virtual_ones(
    web: FakeWeb,
) -> None:
    # The raw path: absolute and relative http(s) targets are listed with
    # their link text, javascript:/data:/# targets are skipped, and a
    # duplicate is listed once, in document order.
    page = (
        "<html><body>"
        '<a href="https://example.com/absolute">Absolute</a>'
        '<a href="/relative">Relative</a>'
        '<a href="javascript:void(0)">Script</a>'
        '<a href="data:text/plain,hi">Data</a>'
        '<a href="#fragment">Fragment</a>'
        '<a href="https://example.com/absolute">Absolute</a>'
        '<a href="https://other.example.org/path">Other</a>'
        "</body></html>"
    )
    web.add(PAGE, html(page))
    async with connect(ignore_robots_txt=True) as (session, _):
        result = await session.call_tool(
            "fetch", {"url": PAGE, "raw": True, "include_links": True}
        )
    assert text_of(result) == (
        "Content type text/html cannot be simplified to markdown, but here is the raw content:\n"
        f"{HEADING}{page}"
        + _page_meta(len(page), 0, len(page))
        + "\n\nLinks on this page:\n"
        + "- [Absolute](<https://example.com/absolute>)"
        + "\n- [Relative](<https://example.com/relative>)"
        + "\n- [Other](<https://other.example.org/path>)"
    )


async def test_include_links_on_the_markdown_path(web: FakeWeb) -> None:
    # The extraction (non-raw) path: the targets the returned markdown text
    # itself carries. A plain-text page holding markdown link syntax reaches
    # the same regex path a markdownified HTML page takes, without needing
    # Readability.js.
    page = (
        "Read [the docs](https://example.com/docs) then "
        "[the source](https://example.com/src)."
    )
    web.add(PAGE, plain(page))
    async with connect(ignore_robots_txt=True) as (session, _):
        result = await session.call_tool("fetch", {"url": PAGE, "include_links": True})
    assert text_of(result) == (
        f"{PREFIX}{HEADING}{page}"
        + _page_meta(len(page), 0, len(page))
        + "\n\nLinks on this page:\n"
        + "- [the docs](<https://example.com/docs>)"
        + "\n- [the source](<https://example.com/src>)"
    )


async def test_binary_content_is_reported_not_returned_as_noise(web: FakeWeb) -> None:
    # A body with NUL bytes decoded as text is replacement-character noise
    # for the model. The tool reports it, with the size, instead of returning
    # the noise; like the truncation notice this is an in-band <error>
    # element on a successful result, and there is no page to paginate.
    body = "MZ\x00\x01\x02binary-ish"
    web.add(PAGE, plain(body))
    async with connect(ignore_robots_txt=True) as (session, _):
        result = await session.call_tool("fetch", {"url": PAGE})
    data = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    assert data["isError"] is False
    assert text_of(result) == (
        f"{PREFIX}{HEADING}<error>Binary content ({len(body)} characters of decoded "
        "noise); the text tools cannot extract this. "
        "Fetch a text/HTML representation instead.</error>"
    )
    assert "<page-meta" not in text_of(result)
