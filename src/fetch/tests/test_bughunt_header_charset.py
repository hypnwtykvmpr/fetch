"""Bug-hunt repros: Content-Type header charsets httpx cannot apply.

A server (or an attacker controlling one) can declare a charset in the
Content-Type header that makes httpx's ``response.text`` raise instead of
decode. A UTF-16/32 label is honored with its endianness; a name that is a
Python codec but not a text encoding (base64, rot13) falls back to the UTF-8
decode, like httpx does for an unknown name, rather than raising through the
tool or prompt.
"""

from __future__ import annotations

import codecs

import httpx
import pytest

from .harness import FakeWeb, connect

PAGE = "https://example.com/page"
ROBOTS = "https://example.com/robots.txt"
BODY = b"<html><body>plain utf-8 body</body></html>"


def text_of(result) -> str:
    data = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    assert len(data["content"]) == 1
    return data["content"][0]["text"]


@pytest.mark.parametrize(
    "charset",
    [
        # Python codecs that are not text encodings: AssertionError/TypeError.
        "base64",
        "rot13",
    ],
)
async def test_a_header_charset_httpx_cannot_apply_falls_back_to_utf8(
    web: FakeWeb, charset: str
) -> None:
    web.add(ROBOTS, httpx.Response(404))
    web.add(
        PAGE,
        httpx.Response(
            200, content=BODY, headers={"content-type": f"text/html; charset={charset}"}
        ),
    )
    async with connect(ignore_robots_txt=True) as (session, _):
        result = await session.call_tool("fetch", {"url": PAGE})
    data = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    assert data["isError"] is False, text_of(result)
    assert "plain utf-8 body" in text_of(result)


async def test_a_hostile_robots_charset_does_not_break_the_check(web: FakeWeb) -> None:
    # The robots.txt fetch decodes its body too; a header it cannot apply must
    # degrade to the UTF-8 decode (and here, no rules means allowed) rather
    # than raise an unexpected exception out of the tool call.
    web.add(
        ROBOTS,
        httpx.Response(
            200,
            content=b"User-agent: *\nAllow: /",
            headers={"content-type": "text/plain; charset=utf-16"},
        ),
    )
    web.add(
        PAGE,
        httpx.Response(200, content=b"ok", headers={"content-type": "text/plain"}),
    )
    async with connect() as (session, _):
        result = await session.call_tool("fetch", {"url": PAGE})
    data = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    assert data["isError"] is False, text_of(result)
    assert "ok" in text_of(result)


async def test_a_prompt_fetch_of_a_hostile_charset_page_is_a_prompt_message(
    web: FakeWeb,
) -> None:
    # The prompt path promises failures as prompt text, never as a raised
    # protocol error.
    web.add(
        PAGE,
        httpx.Response(
            200,
            content=b'{"a": 1}',
            headers={"content-type": "application/json; charset=base64"},
        ),
    )
    async with connect() as (session, _):
        result = await session.get_prompt("fetch", {"url": PAGE})
    data = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    text = data["messages"][0]["content"]["text"]
    assert '{"a": 1}' in text


@pytest.mark.parametrize(
    ("bom", "codec"),
    [
        (codecs.BOM_UTF16_LE, "utf-16-le"),
        (codecs.BOM_UTF16_BE, "utf-16-be"),
        (codecs.BOM_UTF32_LE, "utf-32-le"),
        (codecs.BOM_UTF32_BE, "utf-32-be"),
    ],
    ids=["utf-16-le", "utf-16-be", "utf-32-le", "utf-32-be"],
)
async def test_utf16_utf32_bom_without_header_charset(
    web: FakeWeb, bom: bytes, codec: str
) -> None:
    web.add(
        PAGE,
        httpx.Response(
            200,
            content=bom + "héllo wörld €".encode(codec),
            headers={"content-type": "text/plain"},
        ),
    )
    async with connect(ignore_robots_txt=True) as (session, _):
        result = await session.call_tool("fetch", {"url": PAGE})
    data = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    assert data["isError"] is False, text_of(result)
    assert "héllo wörld €" in text_of(result)


# Bodies that the named codec decodes to text holding a lone surrogate.
SURROGATE_BODIES = {
    "unicode-escape": b"a \\ud800 b",
    "raw-unicode-escape": b"a \\ud800 b",
    "utf-7": b"a +2AA- b",
    "punycode": b"ib9b",
}


@pytest.mark.parametrize("charset", list(SURROGATE_BODIES))
async def test_a_header_charset_that_yields_lone_surrogates_is_ignored(
    web: FakeWeb, charset: str
) -> None:
    body = SURROGATE_BODIES[charset]
    web.add(
        PAGE,
        httpx.Response(
            200,
            content=body,
            headers={"content-type": f"text/plain; charset={charset}"},
        ),
    )
    async with connect(ignore_robots_txt=True) as (session, _):
        result = await session.call_tool("fetch", {"url": PAGE})
        prompt = await session.get_prompt("fetch", {"url": PAGE})
    # What the stdio transport does with every message (mcp/server/stdio.py);
    # a lone surrogate made it raise PydanticSerializationError.
    for message in (result, prompt):
        message.model_dump_json(by_alias=True, exclude_unset=True).encode("utf-8")
    assert result.is_error is False
    assert body.decode() in text_of(result)
