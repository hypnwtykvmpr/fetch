"""Wall-clock budgets: each input would take minutes if the code were quadratic.

The absolute budgets are generous ceilings for slow CI runners; the measured
times are recorded in the commit message."""

from __future__ import annotations

import time
from collections.abc import Callable

import pytest

from mcp_server_fetch import _textsan, _worker
from mcp_server_fetch.server import MAX_RESPONSE_BYTES

CASES: list[tuple[str, Callable[[str], object], str, float]] = []


def case(name: str, fn: Callable[[str], object], budget: float, *inputs: str) -> None:
    CASES.extend((name, fn, text, budget) for text in inputs)


def fill(unit: str, size: int = MAX_RESPONSE_BYTES) -> str:
    return (unit * (size // len(unit) + 1))[:size]


case(
    "neutralize",
    _textsan.neutralize,
    5,
    "<" + " " * 500_000,
    "< " * 250_000,
    "<" + " /" * 250_000,
    "\u27e6" + " " * 500_000,
    "\uff1c" + " " * 500_000,
    "<" + "\u0332" * 500_000,
    "\u301a" + "\u2800" * 500_000,
    "contents of " + "a" * 500_000,
    "contents " * 250_000,
    "\t" * 500_000 + "links on this page",
    "\u00a0" * 500_000 + "links on this page :",
)
case(
    "_html_refs",
    _textsan._html_refs,
    5,
    "&#x41;" * 300_000,
    "&#" * 500_000,
    "&#x200B;" * 300_000,
)
case(
    "flag",
    lambda t: _textsan.flag(t, inline=True),
    5,
    "ignore " * 200_000,
    ("ignore" + "x" * 80 + "\n") * 10_000,
    "<|" * 500_000,
    "<\\|im\\_" * 200_000,
    " " * 500_000 + "system:",
    '"tool_call"' + " " * 500_000,
    "sanitize" + " " * 500_000,
    "x\n" * 500_000,
)
case(
    "strip_invisible",
    _textsan.strip_invisible,
    5,
    "\u200b" * 1_000_000,
    "\u05d0\u2067x" * 300_000,
    "\u2067\n" * 300_000,
    "\u200e\n" * 300_000,
    ("\U0001f3f4" + "\U000e0067" * 5) * 100_000,
    "\x1b[0m" * 300_000,
)
case("strip_controls", _textsan.strip_controls, 2, fill("\x1b[31m\x0b"))
case(
    "_json_escapes",
    _textsan._json_escapes,
    5,
    "\\" * 400_000 + "u0041",
    "\\u200b" * 300_000,
)
case(
    "_strip_css_comments",
    _worker._strip_css_comments,
    2,
    "/*" + "*" * 500_000,
    "/*" * 250_000,
)
case("_css_unescape", _worker._css_unescape, 2, "\\" * 500_000, "\\6f " * 250_000)
case(
    "text_links",  # at the body cap, in the shapes that made the old regex quadratic
    lambda t: list(_worker.text_links(t)),
    10,
    fill("[a](http://"),
    fill("[](http://"),
    fill("[](https://a"),
    fill("[" + "a" * 300),
)


@pytest.mark.parametrize(
    ("name", "fn", "text", "budget"),
    CASES,
    ids=[f"{c[0]}-{i}" for i, c in enumerate(CASES)],
)
def test_budget(
    name: str, fn: Callable[[str], object], text: str, budget: float
) -> None:
    t0 = time.perf_counter()
    fn(text)
    elapsed = time.perf_counter() - t0
    print(f"BUDGET {name} len={len(text)} {elapsed:.3f}s of {budget}s")
    assert elapsed < budget
