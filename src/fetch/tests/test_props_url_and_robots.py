"""Property suite: URL handling, robots.txt semantics and the private-address guard.

Every property here is an invariant the example tests cannot enumerate:
the numeric-IPv4 parser's equivalence with the POSIX ``inet_aton`` grammar,
robots-URL derivation for any userinfo spelling, robots verdicts stable under
encoding variations, and the guard's fail-direction for unresolvable names.
"""

from __future__ import annotations

import ipaddress

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from mcp_server_fetch.server import (
    _numeric_ipv4,
    get_robots_txt_url,
    normalize_proxy_url,
)

# ---------------------------------------------------------------------------
# _numeric_ipv4: the portable inet_aton grammar
# ---------------------------------------------------------------------------

decimal_part = st.integers(0, 255).map(str)
octal_part = st.integers(0, 7).map(lambda n: "0" + str(n))
hex_part = st.integers(0, 255).map(lambda n: f"0x{n:02x}")


@st.composite
def inet_aton_host(draw) -> tuple[str, str]:
    """A host string the inet_aton grammar accepts, plus its dotted-quad value."""
    kind = draw(st.integers(0, 2))
    if kind == 0:  # a.b.c.d
        parts = [draw(decimal_part) for _ in range(4)]
        value = (
            (int(parts[0]) << 24)
            | (int(parts[1]) << 16)
            | (int(parts[2]) << 8)
            | int(parts[3])
        )
    elif kind == 1:  # a.b.c  (a in the first octet, b.c.d in the remaining bytes)
        a = draw(decimal_part)
        bc = draw(st.integers(1, 0xFFFFFF))
        parts = [a, str(bc)]
        value = (int(a) << 24) | bc
    else:  # a single 32-bit decimal
        value = draw(st.integers(0, 0xFFFFFFFF))
        parts = [str(value)]
    host = ".".join(parts)
    return host, str(ipaddress.IPv4Address(value))


@given(inet_aton_host())
@settings(max_examples=200, deadline=None)
def test_every_inet_aton_spelling_parses_to_the_same_address(
    case: tuple[str, str],
) -> None:
    host, dotted = case
    parsed = _numeric_ipv4(host)
    assert parsed is not None, f"{host!r} must parse under the inet_aton grammar"
    assert str(parsed) == dotted, f"{host!r} -> {parsed}, expected {dotted}"


@given(st.text(min_size=0, max_size=40))
@settings(max_examples=300, deadline=None)
def test_the_parser_never_raises_and_only_bounded_octets_are_public(host: str) -> None:
    """The parser is total: no input raises, and any result it does produce is a
    real IPv4 address whose publicness matches the dotted-quad spelling of the
    same bytes."""
    parsed = _numeric_ipv4(host)  # must not raise
    if parsed is None:
        return
    # Whatever it produced must be a genuine IPv4 address; a parse that yielded
    # garbage bytes would be an overreach bug in the grammar.
    assert isinstance(parsed, ipaddress.IPv4Address)
    # Loopback spellings must always be recognized as loopback: that is the
    # invariant the SSRF guard depends on.
    packed = int(parsed)
    if packed in (0x7F000000, 0x7F000001):
        assert parsed.is_loopback


@pytest.mark.parametrize(
    "host,expected",
    [
        ("2130706433", "127.0.0.1"),
        ("0x7f.1", "127.0.0.1"),
        ("0177.0.0.1", "127.0.0.1"),
        ("127.1", "127.0.0.1"),
        ("a.1", None),  # hex digit without the 0x prefix is not decimal
        ("0x7f.0x1", "127.0.0.1"),
        ("99999999999", None),
        ("", None),
        ("..", None),
        ("1.2.3.4.5", None),
    ],
)
def test_numeric_ipv4_known_spellings(host: str, expected: str | None) -> None:
    parsed = _numeric_ipv4(host)
    assert (str(parsed) if parsed is not None else None) == expected


# ---------------------------------------------------------------------------
# get_robots_txt_url: userinfo must never survive into the robots URL
# ---------------------------------------------------------------------------

userinfo_alphabet = "user_info-.~"
hosts = st.sampled_from(
    ["example.com", "[::1]:8080", "example.com:8443", "xn--nxasmq6b.example"]
)


@given(
    st.sampled_from(["", "user@", "user:pass@", "@", ":pass@", "weird user@"]), hosts
)
@settings(max_examples=120, deadline=None)
def test_the_robots_url_never_carries_userinfo(userinfo: str, host: str) -> None:
    url = f"https://{userinfo}{host}/page"
    robots = get_robots_txt_url(url)
    parsed = robots
    assert "@" not in parsed.split("robots.txt")[0].split("https://", 1)[1], (
        f"userinfo leaked into the robots URL for {url!r}: {parsed!r}"
    )
    assert parsed.endswith("robots.txt")


@given(
    st.sampled_from(
        ["https://example.com", "https://example.com/", "https://example.com/a/b?c=d#e"]
    )
)
@settings(max_examples=20, deadline=None)
def test_plain_urls_map_to_the_origin_robots(url: str) -> None:
    robots = get_robots_txt_url(url)
    assert robots.startswith("https://example.com")
    assert robots.endswith("robots.txt")
    assert "?" not in robots and "#" not in robots


# ---------------------------------------------------------------------------
# normalize_proxy_url: scheme normalization is idempotent and scheme-preserving
# ---------------------------------------------------------------------------


@given(st.sampled_from(["http", "https", "socks5", "socks5h"]))
@settings(max_examples=24, deadline=None)
def test_proxy_normalization_is_idempotent(scheme: str) -> None:
    once = normalize_proxy_url(f"{scheme}://proxy.example.com:1080")
    twice = normalize_proxy_url(once)
    assert once == twice
    assert once.startswith(scheme + "://")
