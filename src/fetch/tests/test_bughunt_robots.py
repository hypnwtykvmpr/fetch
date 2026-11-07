"""Bug-hunt repros: robots.txt parsing edges driven through the real server."""

from __future__ import annotations

import httpx
import pytest

from mcp_server_fetch.server import get_robots_txt_url

from .harness import FakeWeb, connect

PAGE = "https://example.com/page"
ROBOTS = "https://example.com/robots.txt"
BOM = "\ufeff"


def text_of(result) -> str:
    data = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    assert len(data["content"]) == 1
    return data["content"][0]["text"]


async def test_robots_txt_is_fetched_once_per_origin_per_call(web: FakeWeb) -> None:
    a = "https://example.com/a"
    b = "https://example.com/b"
    web.add(ROBOTS, httpx.Response(200, content=b"User-agent: *\nDisallow: /private"))
    web.add(PAGE, httpx.Response(302, headers={"location": "/a"}))
    web.add(a, httpx.Response(302, headers={"location": "/b"}))
    web.add(
        b, httpx.Response(200, content=b"done", headers={"content-type": "text/plain"})
    )
    async with connect() as (session, _):
        first = await session.call_tool("fetch", {"url": PAGE})
        second = await session.call_tool("fetch", {"url": PAGE})
    assert "done" in text_of(first)
    assert "done" in text_of(second)
    assert web.urls() == [ROBOTS, PAGE, a, b] * 2


async def test_robots_txt_with_utf8_bom_is_still_enforced(web: FakeWeb) -> None:
    # A robots.txt served with a UTF-8 BOM (common in the wild) is text the
    # RFC says to ignore. The first rule line arrives as "\ufeffUser-agent: *",
    # which no parser reads as a user-agent, so the file currently parses as
    # if it had no rules at all and everything is allowed.
    robots = f"{BOM}User-agent: *\nDisallow: /"
    web.add(ROBOTS, httpx.Response(200, content=robots.encode("utf-8")))
    web.add(PAGE, httpx.Response(200, content=b"secret page"))
    async with connect() as (session, _):
        result = await session.call_tool("fetch", {"url": PAGE})
    data = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    assert data["isError"] is True, text_of(result)
    assert "not allowed" in text_of(result)
    # The page itself is never requested.
    assert web.urls() == [ROBOTS]


@pytest.mark.parametrize(
    ("url", "robots_url"),
    [
        ("https://user:secret@example.com/page", ROBOTS),
        ("https://u:p@[::1]:8080/x", "https://[::1]:8080/robots.txt"),
        ("https://example.com/page", ROBOTS),
    ],
)
def test_the_robots_url_carries_no_userinfo(url: str, robots_url: str) -> None:
    # RFC 9309: the robots.txt URI is "scheme:[//authority]/robots.txt", and
    # userinfo is not part of the authority. The old URL put the page's
    # credentials on the robots.txt request.
    assert get_robots_txt_url(url) == robots_url


async def test_the_robots_request_goes_to_the_credential_free_url(
    web: FakeWeb,
) -> None:
    auth_page = "https://user:secret@example.com/page"
    web.add(ROBOTS, httpx.Response(200, content=b"User-agent: *\nAllow: /"))
    web.add(
        auth_page,
        httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"}),
    )
    async with connect() as (session, _):
        result = await session.call_tool("fetch", {"url": auth_page})
    data = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    assert data["isError"] is False
    # robots.txt was requested at the bare host, not with the page's userinfo.
    assert [str(r.url) for r in web.requests][:1] == [ROBOTS]
    assert web.requests[0].headers.get("authorization") is None
