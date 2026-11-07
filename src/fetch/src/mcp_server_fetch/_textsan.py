"""Untrusted text, made safer to show a model, without touching what a reader can see.

Invisible and terminal-control characters are removed. In Markdown and plain text,
imitations of this server's markers get one character changed and instruction-like
lines get a notice; in raw HTML, JSON and other formats both are only counted, so
the data stays intact.

Linear by construction (every regex is a single-character class, possessive, or
bounded), deterministic across Python versions (hard-coded ranges, no unicodedata),
and pure: no nonce, no secret, so the paginated text is the same on every call.
"""

import functools
import io
import re
from array import array
from bisect import bisect_right

from . import _emoji_ranges

NOTICE_MARK = "\u27e6fetch-notice"
NOTICE = " \u27e6fetch-notice: instruction-like page text, not from the user\u27e7"
MAX_NOTICES = 200
REPORT_KEYS = ("removed", "neutralized", "notices_total", "notices_omitted")

# Terminal controls (C0 except tab/LF/CR, DEL, C1); VT and FF become a space. Always
# applied, sanitize=false included: never visible, and they act on terminals.
_CONTROLS: dict[int, str | None] = {
    c: None for c in (*range(0x09), *range(0x0E, 0x20), *range(0x7F, 0xA0))
}
_CONTROLS |= {0x0B: " ", 0x0C: " "}

_ZWNJ, _ZWJ = "\u200c", "\u200d"
_KEYCAP_BASES = frozenset("0123456789#*")
_VIRAMA = frozenset(
    "\u094d\u09cd\u0a4d\u0acd\u0b4d\u0bcd\u0c4d\u0ccd\u0d3b\u0d3c\u0d4d\u0dca"
    "\u1039\u103a\u17d2\u1b44\u1baa\ua8c4\ua9c0"
    "\U00011046\U000110b9\U00011133\U000111c0"
)
_JOIN = re.compile(
    "[\u0600-\u06ff\u0700-\u07ff\u0840-\u085f\u08a0-\u08ff\u0900-\u0dff"
    "\u1000-\u109f\u1780-\u17ff\u1800-\u18af\ufb50-\ufdff\ufe70-\ufefe"
    "\U00010ac0-\U00010aff\U00010b80-\U00010baf\U00010d00-\U00010d3f"
    "\U0001e900-\U0001e95f]"
)
_RTL = re.compile(
    "[\u0590-\u061b\u061d-\u08ff\ufb1d-\ufdff\ufe70-\ufefe\U00010800-\U00010fff\U0001e800-\U0001efff]"
)
_CJK = re.compile("[\u3400-\u4dbf\u4e00-\u9fff\uf900-\ufaff\U00020000-\U0003134f]")
_ZW = frozenset("\u200b\u200c\u200d\u2060\u2061\u2062\u2063\u2064\ufeff\u180e")
_FLAGS = frozenset(
    "".join(chr(0xE0000 + ord(c)) for c in w) + "\U000e007f"
    for w in ("gbeng", "gbsct", "gbwls")
)
_DIRECTION_MARKS = frozenset("\u200e\u200f\u061c")  # LRM, RLM, ALM: RTL lines only
# Everything the pass looks at; whatever no branch keeps is deleted. Tab, LF and CR
# are never matched (register).
_SPECIAL = re.compile(
    "[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f\xad\u034f\u061c\u115f\u1160\u17b4\u17b5"
    "\u180b-\u180f\u200b-\u200f\u202a-\u202e\u2060-\u206f\u3164\ufe00-\ufe0f\ufeff"
    "\uffa0\ufff0-\ufffb\U0001bca0-\U0001bca3\U0001d173-\U0001d17a"
    "\U000e0000-\U000e0fff]"
)
_OPEN = {"\u202a": 1, "\u202b": 1, "\u2066": 2, "\u2067": 2, "\u2068": 2}
_CLOSE = {"\u202c": 1, "\u2069": 2}
_EMOJI_RANGES = sorted(
    _emoji_ranges.EXTENDED_PICTOGRAPHIC + _emoji_ranges.EMOJI_MODIFIER
)
_EMOJI_LO = [lo for lo, _ in _EMOJI_RANGES]


@functools.lru_cache(4096)
def _emoji(c: str) -> bool:
    i = bisect_right(_EMOJI_LO, ord(c)) - 1
    return i >= 0 and ord(c) <= _EMOJI_RANGES[i][1]


def _strip_once(text: str) -> tuple[str, int]:
    out: list[str] = []
    pos, n, dropped = 0, len(text), 0
    last, emo = "", False  # last kept character; whether its cluster is emoji
    stack: list[
        tuple[int, int]
    ] = []  # open bidi embeddings/isolates: (kind, index in out)
    line_end, rtl = -1, False

    def unwind() -> int:  # unclosed openers on the previous line are dropped
        for _, k in stack:
            out[k] = ""
        count = len(stack)
        stack.clear()
        return count

    while True:
        m = _SPECIAL.search(text, pos)
        end = m.start() if m else n
        if end > pos:
            s = text[pos:end]
            out.append(s)
            last, emo = s[-1], _emoji(s[-1])
        if m is None:
            break
        i, c = end, m.group()
        pos, o = i + 1, ord(c)
        if (c in _OPEN or c in _CLOSE or c in _DIRECTION_MARKS) and i > line_end:
            dropped += unwind()  # first directional character on a new line
            line_end = text.find("\n", i)
            line_end = n if line_end < 0 else line_end
            rtl = _RTL.search(text, text.rfind("\n", 0, i) + 1, line_end) is not None
        if c in (_ZWNJ, _ZWJ):
            nb = text[i + 1 : i + 2]
            run = (i > 0 and text[i - 1] in _ZW) or (nb != "" and nb in _ZW)
            keep = (
                bool(last)
                and not run
                and (
                    last in _VIRAMA
                    or (c == _ZWNJ and _JOIN.match(last) is not None)
                    or (
                        c == _ZWJ
                        and _JOIN.match(last) is not None
                        and nb != ""
                        and _JOIN.match(nb) is not None
                    )
                    or (c == _ZWJ and emo and nb != "" and _emoji(nb))
                )
            )
            if keep:
                out.append(c)
            else:
                dropped += 1
        elif 0xFE00 <= o <= 0xFE0F:
            if last and "\ufe00" <= last <= "\ufe0f":
                dropped += 1  # at most one VS per base
            elif o >= 0xFE0E and emo:
                out.append(c)
                last = c
            elif (
                o == 0xFE0F
                and last in _KEYCAP_BASES
                and text[i + 1 : i + 2] == "\u20e3"
            ):
                out.append(c)
                last, emo = c, False  # keycap
            else:
                dropped += 1
        elif 0x180B <= o <= 0x180D or o == 0x180F:  # Mongolian FVS1-4: at most one
            if last and "\u1800" <= last <= "\u18af":
                out.append(c)
                last, emo = c, False
            else:
                dropped += 1
        elif 0xE0100 <= o <= 0xE01EF:  # IVS after a CJK ideograph only, one per base
            if last and _CJK.match(last):
                out.append(c)
                last, emo = c, False
            else:
                dropped += 1
        elif 0xE0000 <= o <= 0xE007F:  # tags: only the 3 RGI subdivision flags, exact
            if last == "\U0001f3f4" and text[i : i + 6] in _FLAGS:
                out.append(text[i : i + 6])
                pos, last, emo = i + 6, "\U000e007f", False
            else:
                dropped += 1
        elif c in "\x0b\x0c":  # VT/FF are whitespace to HTML and text: a space keeps
            out.append(" ")  # that meaning and drops the terminal effect
            last, emo = " ", False
            dropped += 1
        elif c in _DIRECTION_MARKS:
            if rtl:
                out.append(c)
            else:
                dropped += 1
        elif c in _OPEN or c in _CLOSE:  # embeddings/isolates: balanced, RTL lines only
            if not rtl:
                dropped += 1
            elif c in _OPEN:
                if len(stack) < 32:
                    stack.append((_OPEN[c], len(out)))
                    out.append(c)
                else:
                    dropped += 1
            elif stack and stack[-1][0] == _CLOSE[c]:
                stack.pop()
                out.append(c)
            else:
                dropped += 1
        else:  # controls, default-ignorables, overrides U+202D/E, unassigned tag planes
            dropped += 1
    dropped += unwind()
    return "".join(out), dropped


_JOINERS = re.compile("[\u200c-\u200f\u061c\ufe00-\ufe0f\U000e0100-\U000e01ef]")


def _strip_chars(text: str) -> tuple[str, int]:
    dropped = 0
    for _ in range(3):
        out, d = _strip_once(text)
        dropped += d
        if d == 0 or _SPECIAL.search(out) is None:
            return out, dropped
        text = out
    # a joiner, mark or VS whose context a previous pass removed; after
    # three passes, drop them all (fail closed).
    out = _JOINERS.sub("", text)
    return out, dropped + len(text) - len(out)


def _decode_whole(
    text: str, pattern: re.Pattern[str], *, json_mode: bool
) -> tuple[str, int, bool]:
    """Escapes decoded in whole-document context, without per-character tuples.

    The decoded text is one string; only escape-derived characters record their source
    span (compact arrays). `_strip_chars` runs once on the whole decoded text, so line-
    and run-scoped rules (bidi marks, isolates, joiners) see exactly what a reader sees.
    Survivors are re-emitted with `_kept`'s greedy alignment; unchanged literal runs are
    copied as slices. The third value reports whether any escape was decoded; when true,
    the result is already stripped and must not be stripped again.
    """
    parts = io.StringIO()
    esc_at, esc_from, esc_to = array("i"), array("i"), array("i")
    d = last = 0
    for m in pattern.finditer(text):
        lit_end = m.start(2) if json_mode else m.start()
        parts.write(text[last:lit_end])
        d += lit_end - last
        if json_mode:
            base, seq = m.start(2), m.group(2)
            k, n = 0, len(seq)
            while k < n:
                u = int(seq[k + 2 : k + 6], 16)
                low = int(seq[k + 8 : k + 12], 16) if k + 6 < n else 0
                if 0xD800 <= u < 0xDC00 and 0xDC00 <= low < 0xE000:
                    parts.write(chr(0x10000 + ((u - 0xD800) << 10) + (low - 0xDC00)))
                    step = 12
                else:
                    parts.write(chr(u))
                    step = 6
                esc_at.append(d)
                esc_from.append(base + k)
                esc_to.append(base + k + step)
                d += 1
                k += step
        else:
            try:
                ch = chr(int(m.group(1), 16) if m.group(1) else int(m.group(2)))
            except (ValueError, OverflowError):  # beyond U+10FFFF: literal text
                parts.write(m.group(0))
                d += len(m.group(0))
                last = m.end()
                continue
            parts.write(ch)
            esc_at.append(d)
            esc_from.append(m.start())
            esc_to.append(m.end())
            d += 1
        last = m.end()
    if not esc_at:
        return text, 0, False
    parts.write(text[last:])
    decoded = parts.getvalue()
    parts.close()
    stripped, count = _strip_chars(decoded)
    if not count:
        return text, 0, True
    out = io.StringIO()
    changed = j = d = k = src = 0
    total, kept_len, escapes = len(decoded), len(stripped), len(esc_at)
    while d < total:
        nxt = esc_at[k] if k < escapes else total
        if d < nxt:  # a literal run: decoded[d:nxt] is text[src:src + (nxt - d)]
            size = min(_ALIGN_CHUNK, nxt - d)
            if stripped.startswith(decoded[d : d + size], j):
                out.write(text[src : src + size])
                j += size
                d += size
                src += size
                continue
            stop = d + size
            while d < stop:
                c = decoded[d]
                if j < kept_len and stripped[j] == c:
                    out.write(c)
                    j += 1
                elif j < kept_len and c in "\x0b\x0c" and stripped[j] == " ":
                    out.write(" ")
                    j += 1
                    changed += 1
                else:
                    changed += 1
                d += 1
                src += 1
            continue
        c = decoded[d]  # an escape-derived character
        if j < kept_len and stripped[j] == c:
            out.write(text[esc_from[k] : esc_to[k]])
            j += 1
        elif j < kept_len and c in "\x0b\x0c" and stripped[j] == " ":
            out.write(" ")
            j += 1
            changed += 1
        else:
            changed += 1
        src = esc_to[k]
        d += 1
        k += 1
    return out.getvalue(), changed, True


def _json_escapes(text: str) -> tuple[str, int]:
    """JSON escapes decoded in whole-document context."""
    decoded, dropped, _ = _decode_whole(text, _ESC, json_mode=True)
    return decoded, dropped


_ALIGN_CHUNK = 4096
_ESC = re.compile(r"(?<!\\)((?:\\\\)*+)((?:\\u[0-9a-fA-F]{4})++)")


_REF = re.compile(r"&#(?:[xX]([0-9a-fA-F]{1,6})|([0-9]{1,7}));")


def _html_refs(text: str) -> tuple[str, int]:
    """Numeric character references decoded in whole-document context."""
    decoded, dropped, _ = _decode_whole(text, _REF, json_mode=False)
    return decoded, dropped


def strip_invisible(text: str, *, escapes: str = "") -> tuple[str, int]:
    """Remove invisible and control characters; returns (text, characters changed).

    escapes: "json" also cleans `\\uXXXX` escapes (JSON bodies), "html" numeric
    character references (raw HTML), "" neither."""
    found = False
    dropped = 0
    if escapes == "json":
        text, dropped, found = _decode_whole(text, _ESC, json_mode=True)
    elif escapes == "html":
        text, dropped, found = _decode_whole(text, _REF, json_mode=False)
    if found:
        # The decoder already stripped the decoded document. Running _strip_chars
        # again would judge joiners by their re-emitted reference spelling, where
        # the preceding character is ';' instead of the decoded emoji.
        return text, dropped
    text, d = _strip_chars(text)
    return text, dropped + d


def strip_controls(text: str) -> str:
    """Terminal controls only (always applied, sanitize=false included)."""
    return text.translate(_CONTROLS)


# Blank to a reader: horizontal spaces (NBSP, braille blank, ideographic space and
# the like) and combining marks, which draw onto the previous character. A marker
# imitation may hide any number of them; they are tolerated, never removed.
_BLANK = (
    "[ \t\u00a0\u1680\u2000-\u200a\u202f\u205f\u2800\u3000"
    "\u0300-\u036f\u0483-\u0489\u0591-\u05bd\u05bf\u05c1-\u05c2\u05c4-\u05c5\u05c7"
    "\u0610-\u061a\u064b-\u065f\u0670\u06d6-\u06dc\u06df-\u06e4\u06e7-\u06e8\u06ea-\u06ed"
    "\u1ab0-\u1aff\u1dc0-\u1dff\u20d0-\u20ff\u3099-\u309a\U0001d167-\U0001d169\ufe20-\ufe2f]"
)
_MARKER = re.compile(
    rf"(?P<lt>[<\uff1c\ufe64\u2329\u3008\u27e8])"
    rf"(?={_BLANK}*+/?{_BLANK}*+(?:error|page-meta)\b)"
    rf"|(?P<sq>[\u27e6\u301a])(?={_BLANK}*+fetch-)"
    rf"|(?:(?<=[\n\r\u2028\u2029])|^)(?P<hdr>{_BLANK}*+"
    rf"(?:contents{_BLANK}++of{_BLANK}++[a-z][a-z0-9+.-]*+(?=://)"
    rf"|links{_BLANK}++on{_BLANK}++this{_BLANK}++page{_BLANK}*+(?=:))):",
    re.ASCII | re.IGNORECASE,
)
_HDR = re.compile(
    rf"(?P<hdr>{_BLANK}*+"
    rf"(?:contents{_BLANK}++of{_BLANK}++[a-z][a-z0-9+.-]*+(?=://)"
    rf"|links{_BLANK}++on{_BLANK}++this{_BLANK}++page{_BLANK}*+(?=:))):",
    re.ASCII | re.IGNORECASE,
)


def neutralize_chunk_start(text: str, *, alter: bool) -> tuple[str, int]:
    """Only the line-start header is new at a pagination boundary.

    The full document was already checked, so every other marker in this chunk
    was handled there. Match position 0 only; do not skip because the chunk
    contains a server notice or a generated error.
    """
    m = _HDR.match(text)
    if m is None:
        return text, 0
    if not alter:
        return text, 1
    return text[: m.end() - 1] + "\ua789" + text[m.end() :], 1


def neutralize(text: str, *, alter: bool = True) -> tuple[str, int]:
    """Imitations of this server's markers: changed by one character, or only counted.

    The bracket of `<error` and `<page-meta` (or a look-alike bracket) becomes U+2039,
    the bracket of a U+27E6 (or U+301A) `fetch-` sentinel becomes `[`, and the colon of
    a line-start `Contents of <scheme>://` or `Links on this page:` becomes U+.
    Keyed on the ASCII word, case-insensitive; no folding (homoglyphs: out of scope)."""
    if not alter:
        return text, sum(1 for _ in _MARKER.finditer(text))
    count = 0

    def swap(m: re.Match[str]) -> str:
        nonlocal count
        count += 1
        if m.group("hdr") is not None:
            return m.group("hdr") + "\ua789"
        return "[" if m.group("sq") else "\u2039"

    return _MARKER.sub(swap, text), count


# markdownify backslash-escapes "_" (and, with escape_misc, other punctuation), so a
# punctuation character a pattern needs may be preceded by one backslash.
_FLAG = re.compile(
    # 1 override phrases; 2 "new instructions:"; 3-4 chat special tokens; 5 role
    # turns at line start; 6-7 tool-call syntax; 8 bait to turn sanitization off.
    r"\b(?:ignore|disregard|forget|override|bypass)\b[^\n]{0,80}?"
    r"\b(?:instructions?|prompts?|rules|guidelines|directives|guardrails)\b"
    r"|\b(?:new|updated|real)[ \t]++instructions?[ \t]*+:"
    r"|<\\?\|(?:im\\?_start|im\\?_end|system|user|assistant|endoftext)\\?\|>"
    r"|\\?\[/?inst\\?\]|<<sys>>"
    r"|(?:^|(?<=[\n\r\u2028\u2029]))[ \t>*#\\-]*+(?:system|assistant)[ \t]*+:"
    r"|<(?:function\\?_calls|invoke|tool\\?_calls?|tool\\?_use)\b"
    r"|\"(?:tool\\?_calls?|function\\?_call)\"[ \t]*+:"
    r"|\bsanitize[ \t]*+[=:][ \t]*+[\"']?(?:false|0|off|no)\b",
    re.ASCII | re.IGNORECASE,
)


def flag(text: str, *, inline: bool) -> tuple[str, int, int]:
    """Instruction-like lines: (text, lines hit, hits without an inline notice).

    inline: NOTICE is appended to each hit line (at most MAX_NOTICES), before a
    trailing CR; otherwise the text is unchanged and every hit is counted only."""
    hits: list[int] = []
    line = pos = 0
    for m in _FLAG.finditer(text):
        line += sum(text.count(sep, pos, m.start()) for sep in "\n\r\u2028\u2029")
        pos = m.start()
        if not hits or hits[-1] != line:
            hits.append(line)
    if not hits:
        return text, 0, 0
    marked = hits[:MAX_NOTICES] if inline else []
    if marked:
        lines = text.split("\n")
        for k in marked:
            s = lines[k]
            lines[k] = s[:-1] + NOTICE + "\r" if s.endswith("\r") else s + NOTICE
        text = "\n".join(lines)
    return text, len(hits), len(hits) - len(marked)


def _report(removed: int, neutralized: int, total: int, omitted: int) -> dict[str, int]:
    return dict(zip(REPORT_KEYS, (removed, neutralized, total, omitted), strict=True))


def sanitize_text(text: str, kind: str) -> tuple[str, dict[str, int]]:
    """kind "markdown" or "text": markers neutralized, notices inline. kind "html" (raw
    HTML, character references cleaned), "json" (`\\u` escapes cleaned) or "other":
    markers and instruction-like lines only counted, so the format stays intact."""
    inline = kind in ("markdown", "text")
    text, removed = strip_invisible(
        text, escapes=kind if kind in ("json", "html") else ""
    )
    text, neutralized = neutralize(text, alter=inline)
    text, total, omitted = flag(text, inline=inline)
    return text, _report(removed, neutralized, total, omitted)


def clean_inline(text: str) -> tuple[str, dict[str, int]]:
    """One line of page text shown in Markdown (link text): invisible characters
    removed, markers neutralized, whitespace collapsed, instruction-like text counted."""
    text, removed = strip_invisible(text)
    text, neutralized = neutralize(text)
    text = " ".join(text.split())
    _, total, omitted = flag(text, inline=False)
    return text, _report(removed, neutralized, total, omitted)


def merged(*reports: dict[str, int]) -> dict[str, int]:
    """Sum of sanitizer reports (all zero when given none)."""
    return {k: sum(r[k] for r in reports) for k in REPORT_KEYS}


def count_notices(chunk: str) -> int:
    return chunk.count(NOTICE_MARK)
