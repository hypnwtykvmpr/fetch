"""Property tests for the fetch tool's pagination (hypothesis).

The walk a client performs is: read the returned text, strip the meta line
and the truncation hint that follows it, advance ``start_index`` by the
length of the chunk, repeat until the server says there is no more content.
For any page and any ``max_length`` the chunks must reassemble the page
exactly once, with no gaps and no repeats, whatever characters the page
holds.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from mcp.types import CallToolResult

from mcp_server_fetch._textsan import count_notices, sanitize_text
from mcp_server_fetch.server import _page_meta

from .harness import FakeWeb, connect

PAGE = "https://example.com/page"
PREFIX = "Content type text/plain cannot be simplified to markdown, but here is the raw content:\n"
HEADING = f"Contents of {PAGE}:\n"
NO_MORE = "<error>No more content available.</error>"
# The meta line separates the chunk from what may follow it (the truncation
# hint). "<" and ">" are excluded from the page alphabet, so the page itself
# never contains the markers the walk has to split on (the server must not be
# asked to page through a page that quotes its own error text; that is a
# client parsing concern). "\x00" is excluded because a body holding a NUL
# is no longer paginated text at all: the tool reports it as binary content
# instead of returning a page.
META = "\n\n<page-meta "
HINT = "\n<error>Content truncated. Call the fetch tool with a start_index of "

SAFE_TEXT = st.text(
    alphabet=st.characters(exclude_characters="<>&\x00", exclude_categories=("Cs",)),
    min_size=0,
    max_size=400,
)


def body_of(result: CallToolResult, active: bool) -> str:
    data = result.model_dump(by_alias=True, mode="json", exclude_none=True)
    assert data["isError"] is False
    assert len(data["content"]) == 1
    text = data["content"][0]["text"]
    if active:
        first, _, rest = text.partition("\n")
        assert first.startswith("⟦fetch-envelope ")
        code = first.split(" ")[1].rstrip("⟧")
        assert rest.endswith(f"\n⟦fetch-envelope-end {code}⟧")
        text = rest[: -len(f"\n⟦fetch-envelope-end {code}⟧")]
    else:
        assert "⟦fetch-envelope" not in text
    assert text.startswith(PREFIX + HEADING)
    return text[len(PREFIX + HEADING) :]


@settings(
    max_examples=25,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)
@given(content=SAFE_TEXT, max_length=st.integers(min_value=1, max_value=60))
async def test_pagination_walk_reassembles_any_document(
    web: FakeWeb, content: str, max_length: int
) -> None:
    # One server across examples; the route is re-pointed and the request log
    # cleared for each, so the examples share nothing but the client setup.
    web.routes.clear()
    web.requests.clear()
    web.add(PAGE, httpx_plain(content))
    doc, report = sanitize_text(
        content, "text"
    )  # the server paginates the sanitized text
    active = any(report.values())
    async with connect(ignore_robots_txt=True) as (session, _):
        start = 0
        pieces: list[str] = []
        for _ in range(len(doc) + 2):
            result = await session.call_tool(
                "fetch", {"url": PAGE, "start_index": start, "max_length": max_length}
            )
            body = body_of(result, active)
            if body.startswith(NO_MORE):
                # Exhausted exactly at the end of the document, never early.
                assert start >= len(doc)
                break
            meta_at = body.find(META)
            assert meta_at != -1, f"content page without a meta line: {body!r}"
            chunk = body[:meta_at]
            hint_at = body.find(HINT)
            if hint_at == -1:
                next_start = None
            else:
                next_start = int(
                    body[hint_at + len(HINT) : body.index(" to get more content.")]
                )
            # The chunk is exactly the requested window of the document.
            assert chunk == doc[start : start + max_length]
            # The meta line states exactly the pagination this page performed.
            assert body.startswith(
                _page_meta(
                    len(doc),
                    start,
                    len(chunk),
                    report=report if active else None,
                    notices=count_notices(chunk),
                ),
                meta_at,
            )
            # The hint, when present, points at the exact next byte offset.
            if next_start is None:
                assert len(doc) <= start + max_length
            else:
                assert next_start == start + len(chunk)
                assert len(chunk) == max_length
            pieces.append(chunk)
            start = next_start if next_start is not None else start + len(chunk)
            assert next_start is None or start <= len(doc)
        else:
            raise AssertionError("pagination never reached the end")
    assert "".join(pieces) == doc


def httpx_plain(body: str):
    import httpx

    return httpx.Response(
        200, content=body.encode("utf-8"), headers={"content-type": "text/plain"}
    )
