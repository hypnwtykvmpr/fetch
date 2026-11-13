"""Bug-hunt repros: legacy single-byte charsets the meta scan must map.

The WHATWG encoding standard — what every browser implements — maps the
``iso-8859-1`` and ``us-ascii`` labels to windows-1252, so bytes 0x80-0x9F
decode as the typography they almost always are (“ ” • …), not as the C1
control characters (or U+FFFD replacements) the plain Python codecs produce.
A page that declares ``iso-8859-1`` and carries windows-1252 smart quotes is
the classic legacy-web page.
"""

from __future__ import annotations

import httpx
import pytest

from mcp_server_fetch.server import decode_page

HEAD_ISO = b'<html><head><meta charset="iso-8859-1"><title>t</title></head><body><p>'
HEAD_ASCII = b'<html><head><meta charset="us-ascii"><title>t</title></head><body><p>'
TAIL = b"</p></body></html>"
# windows-1252 bytes for U+201C, U+201D (smart quotes) and U+2026 (ellipsis).
SMART = b"\x93smart\x94 and\x85"


@pytest.mark.parametrize("head", [HEAD_ISO, HEAD_ASCII], ids=["iso-8859-1", "us-ascii"])
def test_iso_8859_1_and_ascii_labels_decode_as_windows_1252(head: bytes) -> None:
    response = httpx.Response(
        200, content=head + SMART + TAIL, headers={"content-type": "text/html"}
    )
    text = decode_page(response)
    assert "\u201csmart\u201d and\u2026" in text, ascii(text)


def test_genuine_iso_8859_1_text_is_unchanged() -> None:
    # Bytes below 0x80 and the shared 0xA0-0xFF range decode the same under
    # both codecs, so a truly Latin-1 page is unaffected by the mapping.
    body = HEAD_ISO + "caf\xe9 na\xefve".encode("latin-1") + TAIL
    response = httpx.Response(200, content=body, headers={"content-type": "text/html"})
    assert "café naïve" in decode_page(response)


def test_a_windows_1252_declaration_itself_is_untouched() -> None:
    response = httpx.Response(
        200,
        content=b'<html><head><meta charset="windows-1252"></head><body><p>'
        + SMART
        + TAIL,
        headers={"content-type": "text/html"},
    )
    assert "\u201csmart\u201d and\u2026" in decode_page(response)


@pytest.mark.parametrize("label", ["iso-8859-1", "latin1", "us-ascii"])
def test_latin1_and_ascii_header_labels_decode_as_windows_1252(label: str) -> None:
    response = httpx.Response(
        200,
        content=b"\x93smart\x94 and\x85 \x80",
        headers={"content-type": f"text/plain; charset={label}"},
    )
    text = decode_page(response)
    assert "\u201csmart\u201d and\u2026 \u20ac" in text, ascii(text)
    assert not any("\x80" <= ch <= "\x9f" for ch in text)
