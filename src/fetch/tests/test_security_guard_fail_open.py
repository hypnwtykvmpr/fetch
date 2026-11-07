"""SSRF review: the private-address guard fails open for unresolvable hosts.

``_refuse_private_destination`` resolves the request host and refuses
non-public addresses — but when ``getaddrinfo`` raises ``socket.gaierror`` it
*returns*, on the theory that "a proxy resolves the name on its side". That
theory has two holes:

1. **A proxy is not required to be configured for the guard to think one is
   coming.** httpx's default ``trust_env=True`` means HTTP(S)_PROXY/ALL_PROXY
   in the environment silently put a proxy in play even though the server was
   started without ``--proxy-url``. Every unresolvable name (and every host
   spelling the local resolver cannot parse) was then handed to that proxy
   unvetted. Implicit proxies are now refused by default and honoured only
   under ``--allow-private-ips``.
2. **Windows' ``getaddrinfo`` does not parse the numeric IPv4 spellings**
   (``2130706433``, ``0x7f.1``, ``0177.0.0.1``) that most resolvers — and
   therefore most proxies — do parse. With a proxy configured (CLI or env), a
   redirect hop to ``http://2130706433/`` (= 127.0.0.1) sails through the
   guard: the guard's resolver says "unresolvable", the proxy connects to
   loopback. The tool path's pydantic normalization catches these only in the
   URL the model typed, never in a redirect ``Location``.

The tests stand up a minimal HTTP proxy on loopback that answers every
absolute-form GET itself, so a request appearing in ``proxy.requests`` proves
the server sent it to the proxy unvetted. Guard resolution is pinned to the
Windows behavior via the ``windows_like_resolver`` fixture. No test touches
the network: every socket stays on 127.0.0.1.

Against the current code the first two tests are RED (the proxy content comes
back to the model); the last two are GREEN (documented current behavior).
"""

from __future__ import annotations

import ipaddress
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import pytest
from mcp.types import GetPromptResult, TextContent

import mcp_server_fetch.server as server_module

from .harness import connect, unwrap

TOOL_ERROR = "Error executing tool fetch: "


def prompt_text(prompt: GetPromptResult) -> str:
    content = prompt.messages[0].content
    assert isinstance(content, TextContent)
    return unwrap(content.text)


# A globally routable IPv4 literal: getaddrinfo parses it locally (no DNS).
PUBLIC_IP = "93.184.215.14"
# Numeric spellings of 127.0.0.1 that a proxy-side resolver still accepts.
LOOPBACK_DECIMAL = "2130706433"


class FakeProxy(threading.Thread):
    """A minimal HTTP proxy on 127.0.0.1 that answers every absolute-form GET.

    It never resolves a name and never opens an upstream connection: a request
    recorded in ``requests`` is one the fetch server sent to its proxy without
    the private-address guard being able to vouch for it.
    """

    def __init__(self) -> None:
        super().__init__(daemon=True)
        self.requests: list[str] = []
        self._ready = threading.Event()
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        self._sock.bind(("127.0.0.1", 0))
        self._sock.listen(16)
        self.port: int = self._sock.getsockname()[1]

    def run(self) -> None:
        self._ready.set()
        while True:
            try:
                conn, _ = self._sock.accept()
            except OSError:
                return  # socket closed: stop serving
            threading.Thread(target=self._serve, args=(conn,), daemon=True).start()

    def _serve(self, conn: socket.socket) -> None:
        buf = b""
        try:
            while True:
                head, buf = self._read_head(conn, buf)
                if head is None:
                    return
                request_line = head.split(b"\r\n", 1)[0].decode("latin-1")
                self.requests.append(request_line)
                target = request_line.split(" ", 2)[1]
                if target.endswith("/robots.txt"):
                    conn.sendall(
                        b"HTTP/1.1 404 Not Found\r\n"
                        b"Content-Type: text/plain\r\nContent-Length: 0\r\n\r\n"
                    )
                elif target.endswith("/page"):
                    conn.sendall(
                        b"HTTP/1.1 302 Found\r\n"
                        b"Location: http://"
                        + LOOPBACK_DECIMAL.encode()
                        + b"/secret\r\n"
                        b"Content-Type: text/plain\r\nContent-Length: 0\r\n\r\n"
                    )
                else:
                    body = b"PROXY-REACHED-UNVETTED-HOST"
                    conn.sendall(
                        b"HTTP/1.1 200 OK\r\n"
                        b"Content-Type: text/plain\r\n"
                        b"Content-Length: "
                        + str(len(body)).encode()
                        + b"\r\n\r\n"
                        + body
                    )
        finally:
            conn.close()

    @staticmethod
    def _read_head(conn: socket.socket, buf: bytes) -> tuple[bytes | None, bytes]:
        while b"\r\n\r\n" not in buf:
            chunk = conn.recv(65536)
            if not chunk:
                return None, buf
            buf += chunk
        head, _, rest = buf.partition(b"\r\n\r\n")
        length = 0
        for line in head.split(b"\r\n")[1:]:
            if line.lower().startswith(b"content-length:"):
                length = int(line.split(b":", 1)[1].strip() or 0)
        while len(rest) < length:
            chunk = conn.recv(65536)
            if not chunk:
                return None, rest
            rest += chunk
        return head, rest[length:]


@contextmanager
def fake_proxy() -> Iterator[FakeProxy]:
    proxy = FakeProxy()
    proxy.start()
    assert proxy._ready.wait(timeout=5)
    try:
        yield proxy
    finally:
        proxy._sock.close()


@pytest.fixture
def windows_like_resolver(monkeypatch: pytest.MonkeyPatch) -> None:
    """Pin ``_resolve_host`` to Windows' actual behavior.

    Dotted IPv4 literals parse locally; the decimal/hex/octal spellings and
    over-long names fail immediately with gaierror, without a DNS query.
    """

    async def resolve(host: str) -> list[str]:
        try:
            ip = ipaddress.ip_address(host)
        except ValueError:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return [str(ip)]

    monkeypatch.setattr(server_module, "_resolve_host", resolve)


def wire(result: Any) -> dict[str, Any]:
    return result.model_dump(
        by_alias=True, mode="json", exclude_none=True, exclude_unset=True
    )


def text_of(result: Any) -> str:
    data = wire(result)
    [content] = data["content"]
    assert content["type"] == "text"
    return content["text"]


async def test_numeric_ip_redirect_hop_reaches_the_proxy(
    windows_like_resolver: None,
) -> None:
    # Tool path with --proxy-url: the model's URL is public, but its redirect
    # Location is a numeric spelling of 127.0.0.1. The per-hop guard cannot
    # resolve that spelling (Windows getaddrinfo refuses it), fails open, and
    # the proxy — which does parse it — connects to loopback for us.
    with fake_proxy() as proxy:
        async with connect(proxy_url=f"http://127.0.0.1:{proxy.port}") as (session, _):
            result = await session.call_tool(
                "fetch", {"url": f"http://{PUBLIC_IP}/page"}
            )
    # The defense: the numeric spelling is refused at the redirect hop,
    # wherever a proxy may be configured. Today (RED against current code)
    # the proxy is asked for http://2130706433/robots.txt and /secret, and
    # the model receives "PROXY-REACHED-UNVETTED-HOST" fetched from loopback
    # through the proxy.
    assert wire(result)["isError"] is True
    assert "Refused to fetch" in text_of(result)
    assert LOOPBACK_DECIMAL in text_of(result)
    # The proxy never receives a request for the numeric host.
    assert not any(LOOPBACK_DECIMAL in request for request in proxy.requests)


async def test_env_proxy_is_refused_in_public_only_mode(
    windows_like_resolver: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # With no --proxy-url, a proxy from the environment (or the OS settings)
    # would choose destinations the private-address guard cannot check, so it
    # is refused instead of silently used or bypassed.
    with fake_proxy() as proxy:
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            monkeypatch.setenv(name, f"http://127.0.0.1:{proxy.port}")
        url = "http://" + "a" * 300 + "/secret"
        async with connect() as (session, _):
            prompt = await session.get_prompt("fetch", {"url": url})
        assert proxy.requests == []
    text = prompt_text(prompt)
    assert "--proxy-url" in text
    assert "PROXY-REACHED-UNVETTED-HOST" not in text


async def test_env_proxy_is_still_used_with_allow_private_ips(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The opt-in keeps httpx's own client, environment proxies included.
    with fake_proxy() as proxy:
        for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
            monkeypatch.setenv(name, f"http://127.0.0.1:{proxy.port}")
        url = "http://" + "a" * 300 + "/secret"
        async with connect(allow_private_ips=True) as (session, _):
            prompt = await session.get_prompt("fetch", {"url": url})
        assert proxy.requests == [f"GET {url} HTTP/1.1"]
    assert "PROXY-REACHED-UNVETTED-HOST" in prompt_text(prompt)


async def test_without_a_proxy_an_unresolvable_host_fails_closed(
    windows_like_resolver: None, monkeypatch: pytest.MonkeyPatch
) -> None:
    # No proxy anywhere: the direct connection fails on its own, so the fetch
    # errors either way. This pins the "no proxy" case as fail-closed in both
    # the current code (ConnectError) and the hardened code (explicit refusal).
    for name in (
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "NO_PROXY",
    ):
        monkeypatch.delenv(name, raising=False)
    url = "http://" + "a" * 300 + "/secret"
    async with connect() as (session, _):
        prompt = await session.get_prompt("fetch", {"url": url})
    assert "PROXY-REACHED-UNVETTED-HOST" not in prompt_text(prompt)
    assert "Failed to fetch" in prompt_text(prompt)


async def test_proxy_url_with_a_file_scheme_is_refused() -> None:
    # "Proxy to file://": a proxy URL must name an http(s)/socks endpoint;
    # httpx rejects anything else at client construction, which make_client
    # turns into a ToolError.
    async with connect(proxy_url="file:///C:/proxy") as (session, _):
        result = await session.call_tool("fetch", {"url": "http://example.com/"})
    assert wire(result)["isError"] is True
    assert "Failed to set up the HTTP client" in text_of(result)
