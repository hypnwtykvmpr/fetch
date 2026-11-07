"""Property suite: body decoding totality and the WHATWG legacy-charset mapping.

Invariants: ``decode_page`` is total over arbitrary bytes for every header/meta
charset declaration the real world produces (it never raises), pure-ASCII bodies
survive every declaration unchanged, and the iso-8859-1/us-ascii labels map the
C1 byte range to windows-1252 — the WHATWG standard every browser applies.
"""

from __future__ import annotations

import httpx
import hypothesis.strategies as st
from hypothesis import HealthCheck, given, settings

from mcp_server_fetch.server import decode_page

META_TEMPLATE = (
    b"<!doctype html><html><head><meta charset=%s></head><body>%s</body></html>"
)

CHARSET_LABELS = [
    b"utf-8",
    b"UTF-8",
    b"iso-8859-1",
    b"ISO-8859-1",
    b"latin1",
    b"us-ascii",
    b"ascii",
    b"windows-1252",
    b"cp1252",
    b"shift_jis",
    b"utf-16",
    b"utf-32",
    b"x-nonexistent",
    b"",
    b"'utf-8'",
    b'"utf-8"',
    b"utf-8;",
    b"unicode-1-1-utf-8",
    b"csisolatin1",
    b"IBM866",
]

body_bytes = st.binary(max_size=200)
header_labels = st.one_of(st.none(), st.sampled_from(CHARSET_LABELS))


def _response(body: bytes, content_type: bytes | None) -> httpx.Response:
    headers = {}
    if content_type is not None:
        headers["content-type"] = content_type.decode("latin-1")
    return httpx.Response(200, headers=headers, content=body)


@given(body=body_bytes, header=header_labels, meta=header_labels)
@settings(
    max_examples=250,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow, HealthCheck.filter_too_much],
)
def test_decode_page_is_total(body: bytes, header, meta) -> None:
    """decode_page never raises on any (body, header, meta) combination."""
    page = body if meta is None else META_TEMPLATE % (meta or b"utf-8", body)
    response = _response(
        page, b"text/html; charset=" + header if header is not None else None
    )
    text = decode_page(response)  # must not raise, whatever the bytes declare
    assert isinstance(text, str)
    declared_header = (header or b"").decode("ascii", "ignore").lower()
    if (
        page
        and all(b < 0x80 for b in page)
        and not declared_header.startswith(("utf-16", "utf-32"))
    ):
        # A pure-ASCII body survives every ASCII-compatible declaration.
        assert "\ufffd" not in text, f"ASCII body mangled by {(header, meta)!r}"


@given(st.binary(max_size=120))
@settings(max_examples=120, deadline=None)
def test_iso_8859_1_meta_label_maps_c1_range_to_windows_1252(body: bytes) -> None:
    """The WHATWG rule: an iso-8859-1/us-ascii declaration decodes 0x80-0x9F as
    windows-1252, never as raw C1 control characters."""
    marker = bytes(range(0x80, 0xA0))
    html = META_TEMPLATE % (b"iso-8859-1", marker)
    response = _response(html, None)
    text = decode_page(response)
    # Windows-1252's C1 slot: 0x80 -> € (U+20AC), 0x93/0x94 are curly quotes,
    # 0x85 is the horizontal ellipsis. No raw C1 control (U+0080-U+009F) survives.
    assert "\u20ac" in text  # € from 0x80
    assert "\u201c" in text and "\u201d" in text  # “ ” from 0x93/0x94
    assert "\u2026" in text  # … from 0x85
    for ch in text:
        assert not (0x80 <= ord(ch) <= 0x9F), (
            f"raw C1 control survived: U+{ord(ch):04X}"
        )


@given(st.binary(max_size=120))
@settings(max_examples=120, deadline=None)
def test_bodies_with_a_utf8_bom_decode_cleanly(body: bytes) -> None:
    html = b"\xef\xbb\xbf<html><body>caf\xc3\xa9</body></html>" + body
    response = _response(html, b"text/html")
    text = decode_page(response)
    assert "café" in text
    assert not text.startswith("\ufeff"), "the BOM must not leak into the text"
