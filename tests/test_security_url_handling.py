"""URL-handling review: schemes, host spellings, credentials, robustness.

Verified defenses (GREEN, pinned so they stay):

- only http(s) is fetchable — file://, ftp://, gopher://, data:, ws:// are
  refused by httpx before any connection, on both the tool and prompt paths;
- pydantic normalizes numeric IPv4 spellings (2130706433, 0x7f.1, 0177.0.0.1,
  127.1) to dotted form before the guard sees them;
- IPv6 zone-id spellings are refused (pydantic rejects them on the tool path;
  the guard parses the scoped address and refuses loopback/link-local);
- raw=True does not bypass robots.txt;
- URL credentials are sent only to the URL's own host: httpx derives
  ``Authorization: Basic`` from userinfo, and strips it on cross-host
  redirects (pinned here at the wire level with loopback servers);
- an https→http redirect downgrade is currently followed (characterized, with
  an optional hardening noted in the security report).

One robustness hole is RED: a URL httpx itself cannot parse (an embedded NUL)
raises ``httpx.InvalidURL``, which is neither ``ToolError`` nor ``HTTPError``,
so it escapes ``fetch_url`` and ``fetch_prompt`` instead of becoming the clean
"Failed to fetch" failure every other bad URL produces.

All HTTP in this module is on loopback or ``harness.FakeWeb`` — no test
touches the network.
"""

from __future__ import annotations

import http.server
import logging
import socketserver
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import httpx
import pytest
from mcp.shared.exceptions import MCPError
from mcp.types import INVALID_PARAMS, GetPromptResult, TextContent

from mcp_server_fetch.server import _fetch_response

from .harness import FakeWeb, connect, unwrap

TOOL_ERROR = "Error executing tool fetch: "


def prompt_text(prompt: GetPromptResult) -> str:
    content = prompt.messages[0].content
    assert isinstance(content, TextContent)
    return unwrap(content.text)


def wire(result: Any) -> dict[str, Any]:
    return result.model_dump(
        by_alias=True, mode="json", exclude_none=True, exclude_unset=True
    )


def text_of(result: Any) -> str:
    data = wire(result)
    [content] = data["content"]
    assert content["type"] == "text"
    return content["text"]


def plain(body: str, status: int = 200) -> httpx.Response:
    return httpx.Response(
        status, content=body.encode(), headers={"content-type": "text/plain"}
    )


# --------------------------------------------------------------------------
# Schemes
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "file:///C:/Windows/win.ini",
        "ftp://example.com/pub/file",
        "gopher://example.com/1page",
        "data:text/plain,hello",
    ],
)
async def test_non_http_schemes_are_refused_on_the_tool_path(url: str) -> None:
    # No `web` fixture: the real transport is what refuses these, before any
    # connection is attempted. (MockTransport serves any scheme, which is a
    # harness artifact, not production behavior. Note: httpx aliases ws:// to
    # plain http:// for a GET, which the guard then checks as usual.)
    async with connect(ignore_robots_txt=True) as (session, _):
        result = await session.call_tool("fetch", {"url": url})
    assert wire(result)["isError"] is True
    assert "UnsupportedProtocol" in text_of(result)


async def test_non_http_schemes_fail_cleanly_on_the_prompt_path() -> None:
    # No `web` fixture: the real client rejects the scheme before any I/O, and
    # the prompt shows the failure as text rather than raising.
    async with connect() as (session, _):
        prompt = await session.get_prompt(
            "fetch", {"url": "file:///C:/Windows/win.ini"}
        )
    assert "Failed to fetch" in prompt_text(prompt)


@pytest.mark.parametrize(
    "url",
    [
        "http://2130706433/",
        "http://0x7f000001/",
        "http://0177.0.0.1/",
        "http://127.1/",
        "http://[::ffff:7f00:1]/",
    ],
)
async def test_numeric_host_spellings_reach_the_guard_normalized(
    web: FakeWeb, url: str
) -> None:
    # pydantic canonicalizes these to 127.0.0.1 / ::ffff:7f00:1 before the
    # guard runs, so the guard refuses them whatever the platform's resolver
    # would have done with the original spelling.
    async with connect(ignore_robots_txt=True) as (session, _):
        result = await session.call_tool("fetch", {"url": url})
    assert wire(result)["isError"] is True
    assert "which is not a public address" in text_of(result)
    assert web.requests == []


async def test_zone_id_urls_are_refused(web: FakeWeb) -> None:
    # pydantic rejects the %25 zone spelling outright on the tool path...
    async with connect(ignore_robots_txt=True) as (session, _):
        result = await session.call_tool("fetch", {"url": "http://[fe80::1%25en0]/"})
    assert wire(result)["isError"] is True
    assert web.requests == []
    # ...and the prompt applies the same URL validation before HTTP begins.
    async with connect(ignore_robots_txt=True) as (session, _):
        with pytest.raises(MCPError) as excinfo:
            await session.get_prompt("fetch", {"url": "http://[::1%25lo]/"})
    assert excinfo.value.error.code == INVALID_PARAMS
    assert "invalid IPv6 address" in excinfo.value.error.message
    assert web.requests == []


# --------------------------------------------------------------------------
# Credentials
# --------------------------------------------------------------------------


class _RecordingServer:
    """A tiny loopback HTTP server recording what actually arrives."""

    def __init__(self) -> None:
        self.seen: list[dict[str, str | None]] = []
        self.routes: dict[str, tuple[int, dict[str, str], bytes]] = {}
        outer = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                outer.seen.append(
                    {
                        "path": self.path,
                        "host": self.headers.get("Host"),
                        "authorization": self.headers.get("Authorization"),
                        "cookie": self.headers.get("Cookie"),
                    }
                )
                route = outer.routes.get(self.path)
                if route is None:
                    self.send_response(404)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                status, headers, body = route
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:  # silence
                pass

        class Server(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        self._server = Server(("127.0.0.1", 0), Handler)
        self.port: int = self._server.server_address[1]  # type: ignore[attr-defined]
        self._thread = threading.Thread(target=self._server.serve_forever, daemon=True)
        self._thread.start()

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self._thread.join(timeout=5)


@contextmanager
def recording_servers(count: int = 2) -> Iterator[list[_RecordingServer]]:
    servers = [_RecordingServer() for _ in range(count)]
    try:
        yield servers
    finally:
        for server in servers:
            server.close()


async def test_url_credentials_stay_on_their_own_host() -> None:
    # URL userinfo becomes a Basic Authorization header on the wire (httpx
    # behavior, pinned here), but a cross-host redirect must not carry it to
    # the redirect target.
    with recording_servers() as servers:
        first, second = servers
        first.routes["/cross"] = (
            302,
            {"Location": f"http://127.0.0.1:{second.port}/after"},
            b"",
        )
        second.routes["/after"] = (200, {"Content-Type": "text/plain"}, b"arrived")
        content, _, _ = await _fetch_response(
            f"http://user:pass@127.0.0.1:{first.port}/cross",
            user_agent="test-agent",
            allow_private_ips=True,  # loopback targets, no guard needed
        )
        assert content == "arrived"
        assert [(req["path"], req["authorization"]) for req in first.seen] == [
            ("/cross", "Basic dXNlcjpwYXNz")
        ]
        assert [(req["path"], req["authorization"]) for req in second.seen] == [
            ("/after", None)
        ]


# --------------------------------------------------------------------------
# robots.txt vs raw
# --------------------------------------------------------------------------


async def test_raw_true_does_not_bypass_robots(web: FakeWeb) -> None:
    web.add("https://example.com/robots.txt", plain("", status=403))
    web.add("https://example.com/page", plain("secret page"))
    async with connect() as (session, _):
        result = await session.call_tool(
            "fetch", {"url": "https://example.com/page", "raw": True}
        )
    assert wire(result)["isError"] is True
    assert web.urls() == ["https://example.com/robots.txt"]


# --------------------------------------------------------------------------
# Redirect scheme downgrades (refused: an https hop must not downgrade to http)
# --------------------------------------------------------------------------


async def test_https_to_http_downgrade_is_refused(web: FakeWeb) -> None:
    web.add(
        "https://example.com/page",
        httpx.Response(302, headers={"location": "http://other.example.org/secret"}),
    )
    web.add("http://other.example.org/secret", plain("downgraded content"))
    async with connect(ignore_robots_txt=True) as (session, _):
        result = await session.call_tool("fetch", {"url": "https://example.com/page"})
    assert wire(result)["isError"] is True
    text = text_of(result)
    assert "downgrade" in text
    assert "downgraded content" not in text


# --------------------------------------------------------------------------
# Robustness: URLs httpx itself cannot parse
# --------------------------------------------------------------------------


async def test_a_url_httpx_cannot_parse_fails_cleanly_on_the_prompt_path(
    web: FakeWeb,
) -> None:
    # Reject an embedded NUL as a malformed argument before httpx builds a
    # request, preserving the protocol's distinction from fetch failures.
    async with connect() as (session, _):
        with pytest.raises(MCPError) as excinfo:
            await session.get_prompt("fetch", {"url": "http://ex\x00ample.com/"})
    assert excinfo.value.error.code == INVALID_PARAMS
    assert "invalid international domain name" in excinfo.value.error.message
    assert web.requests == []


async def test_request_urls_are_not_logged(
    web: FakeWeb, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO)
    page = "https://example.com/page?token=SECRET123"
    web.add(page, plain("body"))
    async with connect(ignore_robots_txt=True) as (session, _):
        result = await session.call_tool("fetch", {"url": page})
    assert wire(result)["isError"] is False
    assert "SECRET123" not in caplog.text
