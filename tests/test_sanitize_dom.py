"""The Markdown-only DOM layer against the parser corpus."""

from __future__ import annotations

import gc
import time
from typing import Any

import httpx
import pytest
from mcp.types import CallToolResult, TextContent

from mcp_server_fetch import _worker

from .corpus_parser import CASES, TRUTH, wrap
from .harness import FakeWeb, connect
from .test_protocol import PAGE

IDS = [n for n, _ in CASES]


def text_of(result: CallToolResult) -> str:
    [content] = result.content
    assert isinstance(content, TextContent)
    return content.text


def md(html: str, node: Any = None) -> str:
    out = _worker.run(
        {
            "mode": "markdown",
            "html": html,
            "page_url": PAGE,
            "links": False,
            "sanitize": True,
            "node": node,
            "budget": 30.0,
            "node_timeout": 20.0,
        }
    )
    return out["content"]


@pytest.mark.parametrize(("case", "frag"), CASES, ids=IDS)
def test_pruned_html_matches_the_browser(case: str, frag: str) -> None:
    root = _worker._parse(wrap(frag))
    _worker._prune(root, remove=True)
    out = _worker._serialize(root)
    assert ("INJECT" in out) == ("I" in TRUTH[case]), "leak or wrong removal of INJECT"
    assert ("VISIBLE" in out) == ("V" in TRUTH[case] and "VISIBLE" in wrap(frag)), (
        "wrong removal of VISIBLE"
    )


@pytest.mark.parametrize(("case", "frag"), CASES, ids=IDS)
def test_parser_corpus_markdown_python(case: str, frag: str) -> None:
    content = md(wrap(frag))
    if "I" not in TRUTH[case]:
        assert "INJECT" not in content


@pytest.mark.parametrize(
    "fragment",
    [
        '<div aria-hidden="true">VISIBLE</div>',
        '<div style="visibility:hidden">HIDDEN<span style="visibility:visible">VISIBLE</span></div>',
        '<div style="display:none;display:block">VISIBLE</div>',
        '<div style="display:block!important;display:none">VISIBLE</div>',
        '<div style="display:none\\9">VISIBLE</div>',
    ],
)
def test_visible_content_survives_hiding_declarations(fragment: str) -> None:
    root = _worker._parse(wrap(fragment))
    _worker._prune(root, remove=True)
    assert "VISIBLE" in _worker._serialize(root)


def test_removing_hidden_element_keeps_its_tail_text() -> None:
    root = _worker._parse(
        wrap(
            '<p>Hello <span style="visibility:hidden">x</span> WORLD</p>'
            '<p><span style="visibility:hidden">y</span> AFTER</p>'
        )
    )
    _worker._prune(root, remove=True)
    out = _worker._serialize(root)
    assert "Hello" in out and "WORLD" in out and "AFTER" in out


def test_hidden_image_alt_and_link_are_removed() -> None:
    root = _worker._parse(
        wrap(
            '<img alt="INJECT" style="visibility:hidden">'
            '<div style="visibility:hidden"><a href="https://evil.example/x">x</a></div>'
        )
    )
    _worker._prune(root, remove=True)
    out = _worker._serialize(root)
    assert "INJECT" not in out
    assert "https://evil.example/x" not in out
    assert list(_worker._dom_links(root, "https://example.com/")) == []


def test_visibility_hidden_preserves_only_visible_descendants() -> None:
    root = _worker._parse(
        wrap(
            '<div style="visibility:hidden">HIDDEN'
            '<span style="visibility:visible">VISIBLE</span>TAIL</div>'
        )
    )
    _worker._prune(root, remove=True)
    out = _worker._serialize(root)
    assert "VISIBLE" in out
    assert "HIDDEN" not in out and "TAIL" not in out


def test_root_visibility_hidden_keeps_a_visible_body() -> None:
    root = _worker._parse(
        '<!DOCTYPE html><html style="visibility:hidden">'
        "<head><title>T</title></head>"
        '<body style="visibility:visible"><p>VISIBLE</p></body></html>'
    )
    _worker._prune(root, remove=True)
    assert "VISIBLE" in _worker._serialize(root)


def test_root_visibility_hidden_without_visible_child_stays_empty() -> None:
    root = _worker._parse(
        '<!DOCTYPE html><html style="visibility:hidden">'
        "<body><p>SECRET</p></body></html>"
    )
    _worker._prune(root, remove=True)
    assert "SECRET" not in _worker._serialize(root)


def test_root_display_none_removes_a_visible_body() -> None:
    root = _worker._parse(
        '<!DOCTYPE html><html style="display:none">'
        '<body style="visibility:visible"><p>SECRET</p></body></html>'
    )
    _worker._prune(root, remove=True)
    assert "SECRET" not in _worker._serialize(root)


def test_empty_visible_span_does_not_expose_hidden_text() -> None:
    root = _worker._parse(
        wrap(
            '<div style="visibility:hidden">SECRET'
            '<span style="visibility:visible"></span></div>'
        )
    )
    _worker._prune(root, remove=True)
    assert "SECRET" not in _worker._serialize(root)


def test_visibility_prune_is_iterative_at_depth() -> None:
    root = _worker._parse(
        wrap(
            '<div style="visibility:hidden">SECRET'
            + "<div>" * 3000
            + "</div>" * 3000
            + "</div>"
        )
    )
    started = __import__("time").perf_counter()
    _worker._prune(root, remove=True)
    assert __import__("time").perf_counter() - started < 1.0
    assert "SECRET" not in _worker._serialize(root)


def test_important_with_space_still_wins() -> None:
    root = _worker._parse(
        wrap('<div style="display:none ! important;display:block">HIDDEN</div>')
    )
    _worker._prune(root, remove=True)
    assert "HIDDEN" not in _worker._serialize(root)


def test_genuinely_hidden_content_is_still_removed() -> None:
    root = _worker._parse(wrap('<div style="display:none">INJECT</div>'))
    _worker._prune(root, remove=True)
    assert "INJECT" not in _worker._serialize(root)


@pytest.mark.usefixtures("node_readability")
@pytest.mark.parametrize(("case", "frag"), CASES, ids=IDS)
def test_parser_corpus_markdown_node(case: str, frag: str) -> None:
    content = md(wrap(frag), node=list(_worker.find_node() or ()))
    if "I" not in TRUTH[case]:
        assert "INJECT" not in content


async def test_raw_is_never_pruned(web: FakeWeb) -> None:
    page = "<div hidden>h</div><!-- c --><template>t</template>"
    web.add(
        PAGE,
        httpx.Response(
            200, content=page.encode(), headers={"content-type": "text/html"}
        ),
    )
    async with connect(ignore_robots_txt=True) as (session, _):
        out = text_of(await session.call_tool("fetch", {"url": PAGE, "raw": True}))
    assert page in out and "fetch-envelope" not in out


@pytest.mark.usefixtures("python_readability")
async def test_hidden_anchor_is_not_listed_in_markdown_mode(web: FakeWeb) -> None:
    body = '<html><body><article><p>Plenty of words in this paragraph to make it the main content of the page.</p><a hidden href="/h">h</a><a href="/v">v</a></article></body></html>'
    web.add(
        PAGE,
        httpx.Response(
            200, content=body.encode(), headers={"content-type": "text/html"}
        ),
    )
    async with connect(ignore_robots_txt=True) as (session, _):
        md_out = text_of(
            await session.call_tool("fetch", {"url": PAGE, "include_links": True})
        )
        raw_args = {"url": PAGE, "include_links": True, "raw": True}
        raw_out = text_of(await session.call_tool("fetch", raw_args))
    assert (
        "/v" in md_out.split("Links on this page:")[1]
        and "/h" not in md_out.split("Links on this page:")[1]
    )
    assert (
        "/h" in raw_out.split("Links on this page:")[1]
        and "/v" in raw_out.split("Links on this page:")[1]
    )


def test_serializer_is_linear() -> None:
    roots = [
        _worker._parse("<body>" + "<p><b>x</b></p>" * n) for n in (50_000, 100_000)
    ]
    for root in roots:
        _worker._serialize(root)
    # Compare serializer CPU, excluding host scheduling and unrelated parser cycles.
    # Warm both trees and collect before timing; alternate order to balance drift.
    gc.collect()
    was_enabled = gc.isenabled()
    gc.disable()
    times = [0.0, 0.0]
    try:
        for order in ((0, 1), (1, 0), (0, 1)):
            for index in order:
                t0 = time.process_time()
                _worker._serialize(roots[index])
                times[index] += time.process_time() - t0
    finally:
        if was_enabled:
            gc.enable()

    small, big = times
    print(f"SERIALIZER CPU 50k={small:.3f}s 100k={big:.3f}s ratio={big / small:.2f}")
    assert big / small < 3


def test_noscript_follows_scripting_on() -> None:
    frag = dict(CASES)["noscript_close_trick"]
    assert "INJECT" not in md(wrap(frag))


# text a browser never shows, by element or by inline style.
D20_HIDDEN = {
    "noembed": "<noembed>INJECT</noembed>",
    "noframes": "<noframes>INJECT</noframes>",
    "frameset_noframes": "<frameset><frame src=x></frameset><noframes>INJECT</noframes>",
    "ruby_rp": "<ruby>A<rp>INJECT</rp><rt>a</rt></ruby>",
    "title_in_body": "<title>INJECT</title>",
    "svg_title": "<svg><title>INJECT</title></svg>",
    "visibility_collapse": '<div style="visibility:collapse">INJECT</div>',
    "content_visibility": '<div style="content-visibility:hidden">INJECT</div>',
    "css_escaped_value": '<div style="display:n\\6f ne">INJECT</div>',
    "css_escaped_property": '<div style="d\\69 splay:none">INJECT</div>',
    "hidden_until_found": '<div hidden="until-found">INJECT</div>',
    "uppercase_important": '<div style="DISPLAY : NONE !IMPORTANT">INJECT</div>',
    "textarea_display_none": '<textarea style="display:none">INJECT</textarea>',
}


@pytest.mark.parametrize("frag", list(D20_HIDDEN.values()), ids=list(D20_HIDDEN))
def test_extended_hidden_set_is_removed(frag: str) -> None:
    root = _worker._parse(wrap(frag))
    removed, _ = _worker._prune(root, remove=True)
    assert "INJECT" not in _worker._serialize(root) and removed >= 1
    assert "INJECT" not in md(wrap(frag))


@pytest.mark.parametrize(
    "frag",
    [
        '<a href="https://x.test/" title="INJECT">visible link</a>',
        '<img src="https://x.test/i.png" title="INJECT">',
    ],
)
def test_tooltip_titles_of_links_and_images_are_removed(frag: str) -> None:
    """Readability.js keeps title attributes and markdownify prints them."""
    root = _worker._parse(wrap(frag))
    assert _worker._prune(root, remove=True)[0] == 1
    assert "INJECT" not in _worker._serialize(root)


@pytest.mark.parametrize(
    "frag",
    [
        "<p>VISIBLE</p>",
        '<div style="visibility:visible">VISIBLE</div>',
        '<div style="content-visibility:auto">VISIBLE</div>',
        "<ruby>VISIBLE<rt>v</rt></ruby>",
        '<abbr title="not a link or image title">VISIBLE</abbr>',
    ],
)
def test_visible_neighbours_of_the_extended_set_are_kept(frag: str) -> None:
    assert "VISIBLE" in md(wrap(frag))


def test_closed_details_keeps_summary_and_body() -> None:
    out = md(
        wrap(
            "<details><summary>Install steps</summary><p>Run uv sync</p></details>"
            "<details open><summary>Open steps</summary><p>Run tests</p></details>"
        )
    )
    assert "Install steps" in out and "Run uv sync" in out
    assert "Open steps" in out and "Run tests" in out
