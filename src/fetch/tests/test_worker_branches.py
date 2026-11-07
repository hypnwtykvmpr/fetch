"""Focused branch coverage for worker helpers (no Node, no network)."""

from __future__ import annotations

from typing import Any

import pytest

from mcp_server_fetch import _worker


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("display:/**/none", "display:none"),
        ("a/*one*/b/**/c", "abc"),
        ("a/*unterminated", "a"),
        ("a/***//**/b", "ab"),
    ],
)
def test_css_comment_removal(source: str, expected: str) -> None:
    assert _worker._strip_css_comments(source) == expected


def test_which_node_ignores_empty_and_relative_entries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(
        "PATH", f"{_worker.os.pathsep}relative{_worker.os.pathsep}/no/such"
    )
    assert _worker._which_node() is None


def test_dom_links_skips_empty_hash_invalid_and_non_http() -> None:
    root = _worker._parse(
        '<html><body><a href="">a</a><a href="#x">b</a>'
        '<a href="http://[">c</a><a href="mailto:x@y">d</a>'
        '<a href="/ok">e</a></body></html>'
    )
    assert list(_worker._dom_links(root, "https://example.com/base/")) == [
        ("https://example.com/ok", "e")
    ]


def test_links_section_sanitize_false_and_duplicate() -> None:
    section, report = _worker.links_section(
        iter(
            [
                ("https://example.com/a", "same"),
                ("https://example.com/a", "same"),
                ("https://example.com/b", "  text  "),
            ]
        ),
        sanitize=False,
    )
    assert "https://example.com/a" in section
    assert "https://example.com/b" in section
    assert section.count("https://example.com/a") == 1
    assert report == {}


def test_prune_hidden_html_root_returns_empty() -> None:
    root = _worker._parse("<html hidden><body>hidden page</body></html>")
    assert _worker._prune(root, remove=True)[0] == 1
    assert "hidden page" not in _worker._serialize(root)


def test_run_links_and_unknown_modes() -> None:
    request = {
        "mode": "links",
        "html": '<a href="https://example.com/x">x</a>',
        "page_url": "https://example.com/",
        "links": True,
        "sanitize": True,
        "node": None,
        "node_timeout": 1,
        "budget": 10,
    }
    out = _worker.run(request)
    assert out["content"] == "" and "https://example.com/x" in out["links"]
    with pytest.raises(ValueError, match="unknown mode"):
        _worker.run({**request, "mode": "bogus"})


def test_limit_self_linux_data_branch(
    monkeypatch: pytest.MonkeyPatch, posix_resource: Any
) -> None:
    calls: list[int] = []

    def setrlimit(kind: int, limits: tuple[int, int]) -> None:
        calls.append(kind)

    unlimited = posix_resource.RLIM_INFINITY
    monkeypatch.setattr(posix_resource, "getrlimit", lambda kind: (unlimited,) * 2)
    monkeypatch.setattr(posix_resource, "setrlimit", setrlimit)
    monkeypatch.setattr(_worker.sys, "platform", "linux")
    _worker._limit_self(5)
    assert posix_resource.RLIMIT_DATA in calls
