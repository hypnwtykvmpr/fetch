"""Property suite: the capped fetch, the redirect chain and the robots-per-hop audit.

Invariants exercised over generated redirect chains and body sizes:
- no fetch path ever buffers more than the declared cap;
- an https hop is never downgraded to http (the fetch refuses);
- chains longer than the redirect budget fail as a typed error, never a crash;
- every actually-requested URL's origin had a robots verdict fetched before it,
  for any chain shape (the robots-per-hop audit property).
"""

from __future__ import annotations

from typing import Any

import httpx
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import BaseModel

from mcp_server_fetch.server import MAX_RESPONSE_BYTES

from .harness import FakeWeb, connect


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
        status,
        content=body.encode("utf-8"),
        headers={"content-type": "text/html; charset=utf-8"},
    )


def chain(n: int, scheme_end: str = "https") -> list[str]:
    """URLs PAGE -> hop1 -> ... -> hop{n-1} -> final, each a different path."""
    urls = [f"https://example.com/hop{i}" for i in range(n)] + [
        f"{scheme_end}://example.com/final"
    ]
    return urls


@given(
    depth=st.integers(1, 6),
    declared=st.sampled_from([None, "0", "1", "100", "5000000", "999999999999"]),
)
@settings(
    max_examples=40,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
async def test_the_cap_is_respected_over_any_chain(
    web: FakeWeb, depth: int, declared: str | None
) -> None:
    """Whatever the chain depth or the declared length, the response never
    buffers more than MAX_RESPONSE_BYTES: either a typed too-large error or
    content within the cap."""
    urls = chain(depth)
    big = b"x" * (MAX_RESPONSE_BYTES + 1)
    for i, url in enumerate(urls[:-1]):
        web.add(url, httpx.Response(302, headers={"location": urls[i + 1]}))
    web.add(
        urls[-1],
        httpx.Response(
            200,
            content=big,
            headers={"content-length": declared} if declared is not None else {},
        ),
    )
    async with connect(ignore_robots_txt=True) as (session, _):
        result = await session.call_tool("fetch", {"url": urls[0]})
    text = text_of(result)
    assert wire(result)["isError"] is True
    assert "too large" in text or "declares" in text


@given(downgrade_at=st.integers(0, 2), depth=st.integers(1, 3))
@settings(
    max_examples=20,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
async def test_https_never_lands_on_http(
    web: FakeWeb, downgrade_at: int, depth: int
) -> None:
    """For any chain that downgrades https->http at any position, the fetch is
    refused and no content fetched over http after an https hop reaches the model."""
    urls = [f"https://example.com/hop{i}" for i in range(depth)]
    urls.append("http://example.com/final")
    for i, url in enumerate(urls[:-1]):
        web.add(url, httpx.Response(302, headers={"location": urls[i + 1]}))
    web.add(urls[-1], plain("plaintext secret"))
    async with connect(ignore_robots_txt=True) as (session, _):
        result = await session.call_tool("fetch", {"url": urls[0]})
    assert wire(result)["isError"] is True
    assert "downgrade" in text_of(result)
    assert "plaintext secret" not in text_of(result)


@given(depth=st.integers(25, 30))
@settings(
    max_examples=6,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
async def test_chains_beyond_the_budget_fail_as_a_typed_error(
    web: FakeWeb, depth: int
) -> None:
    urls = [f"https://example.com/hop{i}" for i in range(depth)]
    urls.append("https://example.com/final")
    for i, url in enumerate(urls[:-1]):
        web.add(url, httpx.Response(302, headers={"location": urls[i + 1]}))
    web.add(urls[-1], plain("unreachable"))
    async with connect(ignore_robots_txt=True) as (session, _):
        result = await session.call_tool("fetch", {"url": urls[0]})
    assert wire(result)["isError"] is True
    text = text_of(result)
    assert "redirect" in text.lower() or "too many" in text.lower()
    assert "unreachable" not in text


@given(depth=st.integers(0, 4))
@settings(
    max_examples=12,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
async def test_every_requested_origin_had_a_robots_verdict_first(
    web: FakeWeb, depth: int
) -> None:
    """The robots-per-hop audit: walking any redirect chain, every origin the
    server actually requests content from has had a robots.txt fetch earlier in
    the observed request sequence (with robots enforcement on)."""
    urls = [f"https://example.com/hop{i}" for i in range(depth)] + [
        f"https://example.com/final{depth}"
    ]
    origins: list[str] = []
    for url in urls:
        origin = "/".join(url.split("/")[:3])
        if origin not in origins:
            origins.append(origin)
    for i, url in enumerate(urls[:-1]):
        web.add(url, httpx.Response(302, headers={"location": urls[i + 1]}))
    web.add(urls[-1], plain("final content"))
    for origin in origins:
        web.add(f"{origin}/robots.txt", plain("", status=404))
    # hypothesis reuses the function-scoped FakeWeb across examples: audit only
    # the requests THIS example made, not the accumulated history.
    start = len(web.requests)
    async with connect() as (session, _):
        result = await session.call_tool("fetch", {"url": urls[0]})
    assert wire(result)["isError"] is False
    requested = web.urls()[start:]
    for i, url in enumerate(requested):
        if url.endswith("/robots.txt"):
            continue
        origin = "/".join(url.split("/")[:3])
        robots_url = f"{origin}/robots.txt"
        # Some robots verdict for this origin must exist EARLIER in the sequence
        # (hypothesis reuses routes across examples, so robots.txt may appear
        # more than once; the dict above keeps only the last occurrence).
        earlier = [j for j, u in enumerate(requested) if u == robots_url and j < i]
        assert earlier, f"{url} was fetched with no prior robots verdict for {origin}"
