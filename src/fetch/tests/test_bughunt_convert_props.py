"""Property test: the HTML-to-Markdown converter never raises on any input.

Anything a server serves as text/html reaches ``extract_content_from_html``;
whatever it holds, the tool must come back with a string (content or the
"cannot be simplified" report), never an unexpected exception.
"""

from __future__ import annotations

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from mcp_server_fetch.server import extract_content_from_html

FAILED = "<error>Page failed to be simplified from HTML</error>"

TAGS = st.sampled_from(
    [
        "div",
        "span",
        "p",
        "table",
        "tr",
        "td",
        "ul",
        "ol",
        "li",
        "a",
        "img",
        "br",
        "script",
        "style",
        "nav",
        "article",
        "h1",
        "b",
        "i",
    ]
)
TEXT = st.text(
    alphabet=st.characters(exclude_categories=["Cs"], exclude_characters="\x00\r"),
    min_size=0,
    max_size=30,
)


@st.composite
def html_documents(draw):
    parts = ["<html><body>"]
    for _ in range(draw(st.integers(0, 12))):
        tag = draw(TAGS)
        attrs = draw(
            st.sampled_from(
                [
                    "",
                    " hidden",
                    ' style="display:none"',
                    ' href="http://x/?a=1&amp;b=2"',
                    " aria-hidden=true",
                    ' class="a b"',
                ]
            )
        )
        parts.append(f"<{tag}{attrs}>")
        parts.append(draw(TEXT))
        if tag not in ("img", "br") and draw(st.booleans()):
            parts.append(f"</{tag}>")
        # Sometimes leave it unclosed: real markup is malformed.
    parts.append("</body></html>")
    return "".join(parts)


@settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.function_scoped_fixture],
)
@given(page=html_documents())
def test_the_converter_always_returns_a_string(page: str) -> None:
    result = extract_content_from_html(page, use_readability_js=False)
    assert isinstance(result, str)
    if result == FAILED:
        return
    assert result.strip(), "an extraction must not be whitespace-only"
