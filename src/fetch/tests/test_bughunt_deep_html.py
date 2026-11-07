"""Bug-hunt repros: malformed-but-realistic HTML that the extractor choked on."""

from __future__ import annotations

import pytest

from mcp_server_fetch.server import extract_content_from_html

FAILED = "<error>Page failed to be simplified from HTML</error>"


@pytest.mark.parametrize("depth", [500, 1500])
def test_deeply_nested_page_reports_a_simplification_failure(depth: int) -> None:
    # readabilipy and markdownify both walk the tree recursively; a page
    # nested deeper than Python's recursion limit used to raise
    # RecursionError out of the tool (as an unexpected-exception isError
    # result) and out of the prompt (as a protocol error), instead of the
    # same "cannot be simplified" report every other unextractable page gets.
    html = "<div>" * depth + "<p>deep text</p>" + "</div>" * depth
    result = extract_content_from_html(html, use_readability_js=False)
    assert result == FAILED


def test_normal_nesting_still_extracts() -> None:
    html = "<html><body><article><h1>A</h1><p>fine text</p></article></body></html>"
    result = extract_content_from_html(html, use_readability_js=False)
    assert "fine text" in result
    assert result != FAILED
