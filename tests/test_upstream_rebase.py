"""Keep upstream's normalized robots-path checks in the hardened Fetch fork."""

import httpx
import pytest

from .harness import FakeWeb, connect

ORIGIN = "https://example.com"
ROBOTS = ORIGIN + "/robots.txt"


@pytest.mark.parametrize(
    "path",
    ["/%2fprivate", "/a%2f..%2fprivate", "/x/%2E%2E/private", "//private//report"],
)
async def test_resolved_private_paths_are_refused_before_request(
    web: FakeWeb, path: str
) -> None:
    page = ORIGIN + path
    web.add(ROBOTS, httpx.Response(200, text="User-agent: *\nDisallow: /private"))
    web.add(page, httpx.Response(200, text="visible content"))
    async with connect() as (session, _):
        result = await session.call_tool("fetch", {"url": page})
    assert result.is_error is True
    assert web.urls() == [ROBOTS]


@pytest.mark.parametrize("path", ["//public", "/%2Fpublic", "/pri%20vate", "//Private"])
async def test_resolving_paths_keeps_case_sensitive_public_paths(
    web: FakeWeb, path: str
) -> None:
    page = ORIGIN + path
    web.add(ROBOTS, httpx.Response(200, text="User-agent: *\nDisallow: /private"))
    web.add(
        page,
        httpx.Response(
            200, text="visible content", headers={"content-type": "text/plain"}
        ),
    )
    async with connect() as (session, _):
        result = await session.call_tool("fetch", {"url": page})
    assert result.is_error is False
    assert len(web.urls()) == 2
