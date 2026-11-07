"""Independent decoder reference used to check optimized text handling.

`_kept`, `_json_escapes` and `_html_refs` are reference implementations. The escape and
reference loops of the latter two are split out unchanged as `_json_escape_pieces`
and `_html_ref_pieces`. `_strip_chars`, `_ESC` and `_REF` come from the product
module, to share the same character decoding rules.
"""

from __future__ import annotations

from mcp_server_fetch._textsan import _ESC, _REF, _strip_chars


def _kept(source: list[tuple[str, str]], stripped: str) -> tuple[str, int]:
    """Re-emit the source spelling of every character that survived stripping.

    `source` pairs each decoded character with the text it came from (a reference
    or an escape); `stripped` is what `_strip_chars` kept of their concatenation, in
    order, VT/FF turned into spaces. A dropped character vanishes on its own; every
    survivor keeps its original source text. Returns (text, characters changed).
    """
    out: list[str] = []
    j = changed = 0
    for char, spelling in source:
        if j < len(stripped) and stripped[j] == char:
            out.append(spelling)
            j += 1
        elif j < len(stripped) and char in "\x0b\x0c" and stripped[j] == " ":
            out.append(" ")
            j += 1
            changed += 1
        else:
            changed += 1
    return "".join(out), changed


def _json_escape_pieces(text: str) -> list[tuple[str, str]]:
    pieces: list[tuple[str, str]] = []
    last = 0
    for m in _ESC.finditer(text):
        for char in text[last : m.start()]:
            pieces.append((char, char))
        for char in m.group(1):
            pieces.append((char, char))
        seq = m.group(2)
        units = [
            (int(seq[k + 2 : k + 6], 16), seq[k : k + 6]) for k in range(0, len(seq), 6)
        ]
        k = 0
        while k < len(units):
            u, esc = units[k]
            low = units[k + 1][0] if k + 1 < len(units) else 0
            if 0xD800 <= u < 0xDC00 and 0xDC00 <= low < 0xE000:
                code = 0x10000 + ((u - 0xD800) << 10) + (low - 0xDC00)
                pieces.append((chr(code), esc + units[k + 1][1]))
                k += 2
            else:
                pieces.append((chr(u), esc))
                k += 1
        last = m.end()
    for char in text[last:]:
        pieces.append((char, char))
    return pieces


def _html_ref_pieces(text: str) -> list[tuple[str, str]]:
    pieces: list[tuple[str, str]] = []
    last = 0
    for m in _REF.finditer(text):
        for char in text[last : m.start()]:
            pieces.append((char, char))
        try:
            code = int(m.group(1), 16) if m.group(1) else int(m.group(2))
            pieces.append((chr(code), m.group(0)))
        except (ValueError, OverflowError):
            for char in m.group(0):
                pieces.append((char, char))
        last = m.end()
    for char in text[last:]:
        pieces.append((char, char))
    return pieces


def _json_escapes(text: str) -> tuple[str, int]:
    """JSON escapes decoded in document context: adjacent literals count."""
    pieces = _json_escape_pieces(text)
    decoded = "".join(char for char, _ in pieces)
    stripped, dropped = _strip_chars(decoded)
    if dropped == 0:
        return text, 0
    kept, changed = _kept(pieces, stripped)
    return kept, changed


def _html_refs(text: str) -> tuple[str, int]:
    """Numeric character references decoded in document context."""
    pieces = _html_ref_pieces(text)
    decoded = "".join(char for char, _ in pieces)
    stripped, dropped = _strip_chars(decoded)
    if dropped == 0:
        return text, 0
    kept, changed = _kept(pieces, stripped)
    return kept, changed
