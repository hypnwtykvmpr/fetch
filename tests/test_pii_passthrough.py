"""Personal data and all other visible content pass through unchanged."""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest
from mcp.types import TextContent

from .harness import FakeWeb, connect, unwrap
from .test_protocol import PAGE

VALUES = [
    "Ada Lovelace",
    "ada.lovelace@example.org",
    "+44 20 7946 0958",
    "12 St James's Square, London SW1Y 4LE",
    "card 4111 1111 1111 1111",
    "https://example.org/u/ada?id=42",
]
FIXTURE = "Ada Lovelace, ada.lovelace@example.org, +44 20 7946 0958, 12 St James's Square, London SW1Y 4LE, card 4111 1111 1111 1111, https://example.org/u/ada?id=42"


def resp(body: str, ct: str) -> httpx.Response:
    return httpx.Response(200, content=body.encode(), headers={"content-type": ct})


async def run(
    web: FakeWeb, response: httpx.Response, *, prompt: bool = False, **args: Any
) -> str:
    web.add(PAGE, response)
    async with connect(ignore_robots_txt=True) as (session, _):
        if prompt:
            result = await session.get_prompt("fetch", {"url": PAGE})
            return unwrap(result.messages[0].content.text)  # type: ignore[union-attr]
        result = await session.call_tool(
            "fetch", {"url": PAGE, "max_length": 999999, **args}
        )
        assert not result.is_error
        [content] = result.content
        assert isinstance(content, TextContent)
        return content.text


def check(out: str) -> None:
    for value in VALUES:
        assert value in out, value
    assert "fetch-envelope" not in out and "\u2039" not in out and "\u27e6" not in out


ARTICLE = f"<html><body><article><h1>Contact</h1><p>{FIXTURE} and enough more words to be the main content of the page.</p></article></body></html>"


@pytest.mark.usefixtures("python_readability")
async def test_markdown_pure_python(web: FakeWeb) -> None:
    check(await run(web, resp(ARTICLE, "text/html")))


@pytest.mark.usefixtures("node_readability")
async def test_markdown_node(web: FakeWeb) -> None:
    check(await run(web, resp(ARTICLE, "text/html")))


async def test_raw_html(web: FakeWeb) -> None:
    check(await run(web, resp(ARTICLE, "text/html"), raw=True))


async def test_text_plain(web: FakeWeb) -> None:
    check(await run(web, resp(FIXTURE, "text/plain")))


async def test_application_json(web: FakeWeb) -> None:
    check(
        await run(
            web,
            resp(
                json.dumps({"person": FIXTURE}, ensure_ascii=False), "application/json"
            ),
        )
    )


@pytest.mark.usefixtures("python_readability")
async def test_prompt(web: FakeWeb) -> None:
    check(await run(web, resp(ARTICLE, "text/html"), prompt=True))
