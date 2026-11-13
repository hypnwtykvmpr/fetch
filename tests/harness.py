"""Shared harness for the fetch server tests (conftest.py re-exports the fixtures).

Two pieces:

- ``connect(...)`` runs the real ``serve()`` in-process and yields an
  initialized ``ClientSession`` linked to it over in-memory streams, so tests
  go through the server's validation and error mapping exactly as a client would.
- ``web`` replaces ``httpx.AsyncClient`` with one backed by an
  ``httpx.MockTransport``, so no test touches the network. It records every
  request and the keyword arguments each client was constructed with. It also
  replaces the server's DNS lookup (``_resolve_host``) with ``FakeWeb.dns``:
  a hostname listed there resolves to its addresses, an IP literal resolves to
  itself, and any other name resolves to ``PUBLIC_IP``.
"""

from __future__ import annotations

import asyncio
import asyncio.proactor_events
import ipaddress
import json
import socket
import tempfile
import urllib.request
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from unittest.mock import patch

import anyio
import httpx
import httpx._utils
import pytest
import readabilipy.simple_json
from mcp import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from mcp.shared.message import SessionMessage
from mcp.types import (
    InitializeResult,
    JSONRPCError,
    JSONRPCNotification,
    JSONRPCRequest,
    JSONRPCResponse,
)

import mcp_server_fetch._worker as worker_module
import mcp_server_fetch.server as server_module

Responder = httpx.Response | Callable[[httpx.Request], httpx.Response]

# A globally routable address (documentation ranges are not global, so they
# would be refused by the private-address guard).
PUBLIC_IP = "93.184.215.14"


class WireStream(httpx.AsyncByteStream):
    """A body served the way a socket delivers it: lazily, ``chunk`` bytes at a time.

    Re-iterable, so every response built on it starts again from the first
    byte, and it records whether httpx closed it. Deliberately not
    ``httpx.ByteStream``, which httpx reads eagerly.
    """

    def __init__(self, data: bytes, chunk: int = 65536) -> None:
        self.data = data
        self.chunk = chunk
        self.closed = False

    async def __aiter__(self) -> AsyncIterator[bytes]:
        for start in range(0, len(self.data), self.chunk):
            yield self.data[start : start + self.chunk]

    async def aclose(self) -> None:
        self.closed = True


def _streamed(response: httpx.Response) -> httpx.Response:
    """A fresh streaming copy of ``response``, as the network would deliver it.

    An eager test response (``httpx.Response(content=...)``) was already read,
    and decompressed, at construction, so ``aiter_raw()`` on it raises
    StreamConsumed. Its ``ByteStream`` still holds the bytes as given, which are
    the wire bytes, so those are served again lazily. A response that already
    streams (``gz_page``, a test's own stream) keeps its stream. The Response
    object is always new because httpx rebinds the stream of every response it
    sends, so a route that served the same object twice would find it consumed.
    """
    stream = response.stream
    if isinstance(stream, httpx.ByteStream):
        stream = WireStream(b"".join(stream))
    return httpx.Response(response.status_code, headers=response.headers, stream=stream)


def gz_page(
    wire: bytes,
    enc: str = "gzip",
    *,
    content_type: str = "text/plain",
    status: int = 200,
    chunk: int = 65536,
) -> httpx.Response:
    """A response that sends ``wire`` (bytes already encoded as ``enc``) unread.

    It is built on a WireStream, so httpx decodes nothing at construction:
    truncated, corrupt and bomb bodies reach the server exactly as sent.
    """
    return httpx.Response(
        status,
        headers={
            "content-type": content_type,
            "content-encoding": enc,
            "content-length": str(len(wire)),
        },
        stream=WireStream(wire, chunk),
    )


@dataclass
class FakeWeb:
    """A router of URL -> response used as the transport of every AsyncClient."""

    routes: dict[str, Responder] = field(default_factory=dict)
    requests: list[httpx.Request] = field(default_factory=list)
    client_kwargs: list[dict[str, Any]] = field(default_factory=list)
    # hostname -> addresses; an empty list makes the name unresolvable.
    dns: dict[str, list[str]] = field(default_factory=dict)
    lookups: list[str] = field(default_factory=list)

    def add(self, url: str, response: Responder) -> None:
        self.routes[url] = response

    def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        responder = self.routes.get(str(request.url))
        if responder is None:
            response = httpx.Response(404, content=b"no route in FakeWeb")
        elif isinstance(responder, httpx.Response):
            response = responder
        else:
            response = responder(request)
        return _streamed(response)

    def urls(self) -> list[str]:
        return [str(r.url) for r in self.requests]

    async def resolve(self, host: str) -> list[str]:
        self.lookups.append(host)
        try:
            return [str(ipaddress.ip_address(host))]
        except ValueError:
            pass
        addresses = self.dns.get(host, [PUBLIC_IP])
        if not addresses:
            raise socket.gaierror(socket.EAI_NONAME, "Name or service not known")
        return addresses


@pytest.fixture
def web(monkeypatch: pytest.MonkeyPatch) -> FakeWeb:
    fake = FakeWeb()
    real_async_client = httpx.AsyncClient

    def factory(**kwargs: Any) -> httpx.AsyncClient:
        # make_client's public-only transport is replaced by the mock below;
        # tests/test_public_only_backend.py covers it. It is dropped before the
        # kwargs are recorded, so the proxy pins see what they always saw.
        kwargs.pop("transport", None)
        fake.client_kwargs.append(dict(kwargs))
        # A proxy would mount a real proxy transport in front of ours, so it is
        # recorded above and dropped here. Passing a transport also stops httpx
        # from picking up HTTP(S)_PROXY from the environment.
        kwargs.pop("proxy", None)
        return real_async_client(transport=httpx.MockTransport(fake.handle), **kwargs)

    # server.py imports AsyncClient from httpx inside each function, so
    # patching the attribute on the httpx module is what it picks up.
    monkeypatch.setattr(httpx, "AsyncClient", factory)
    monkeypatch.setattr(server_module, "_resolve_host", fake.resolve)
    return fake


@asynccontextmanager
async def connect(
    **serve_kwargs: Any,
) -> AsyncIterator[tuple[ClientSession, InitializeResult]]:
    """Run ``serve(**serve_kwargs)`` in-process and yield a connected client."""
    async with create_client_server_memory_streams() as (
        client_streams,
        server_streams,
    ):

        @asynccontextmanager
        async def fake_stdio_server() -> AsyncIterator[Any]:
            yield server_streams

        # MCPServer.run_stdio_async() opens the SDK's stdio_server.
        with patch("mcp.server.mcpserver.server.stdio_server", fake_stdio_server):
            async with anyio.create_task_group() as tg:
                tg.start_soon(lambda: server_module.serve(**serve_kwargs))
                async with ClientSession(*client_streams) as session:
                    init = await session.initialize()
                    yield session, init
                tg.cancel_scope.cancel()


_PROXY_VARS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "NO_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
    "no_proxy",
)


def _is_loopback(host: object) -> bool:
    if isinstance(host, bytes):
        host = host.decode("ascii", "replace")
    if not isinstance(host, str):
        return False
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


@pytest.fixture(autouse=True)
def network_guard(monkeypatch: pytest.MonkeyPatch) -> Iterator[list[str]]:
    """Keep every test on loopback: no egress, no real DNS, no ambient proxies.

    - connect/connect_ex to a non-loopback address raises ConnectionRefusedError and is recorded;
      a recorded violation fails the test at teardown even if the code under test swallowed the error.
      Covers the selector loop (sock.connect), the Windows proactor loop (sock_connect -> ConnectEx)
      and TLS sockets (SSLSocket.connect -> socket.connect). AF_UNIX and socketpair stay allowed.
    - getaddrinfo answers only for None, IP literals and "localhost"; any other name fails as an
      offline machine would (gaierror EAI_NONAME), deterministically.
    - Proxy environment variables are cleared, and the OS proxy sources (macOS SystemConfiguration,
      Windows registry) are cut off: getproxies() reads the environment only.
    """
    violations: list[str] = []
    real_connect, real_connect_ex = socket.socket.connect, socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo
    real_sock_connect = asyncio.proactor_events.BaseProactorEventLoop.sock_connect

    def vet(address: object) -> None:
        if isinstance(address, tuple) and not _is_loopback(address[0]):
            violations.append(f"connect {address!r}")
            raise ConnectionRefusedError(
                f"test network guard: {address!r} is not loopback"
            )

    def connect(self: socket.socket, address: Any) -> None:
        vet(address)
        return real_connect(self, address)

    def connect_ex(self: socket.socket, address: Any) -> int:
        vet(address)
        return real_connect_ex(self, address)

    async def sock_connect(self: Any, sock: socket.socket, address: Any) -> None:
        vet(address)
        return await real_sock_connect(self, sock, address)

    def getaddrinfo(host: Any, *args: Any, **kwargs: Any) -> Any:
        # IP literals (any address) resolve locally with no query; connect() vets them afterwards.
        text = host.decode("ascii", "replace") if isinstance(host, bytes) else host
        try:
            ipaddress.ip_address(str(text).split("%", 1)[0])
            literal = True
        except ValueError:
            literal = False
        if host is None or literal or text == "localhost":
            return real_getaddrinfo(host, *args, **kwargs)
        raise socket.gaierror(
            socket.EAI_NONAME, f"test network guard: DNS disabled for {host!r}"
        )

    monkeypatch.setattr(socket.socket, "connect", connect)
    monkeypatch.setattr(socket.socket, "connect_ex", connect_ex)
    monkeypatch.setattr(
        asyncio.proactor_events.BaseProactorEventLoop, "sock_connect", sock_connect
    )
    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    for name in _PROXY_VARS:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(
        urllib.request, "getproxies", urllib.request.getproxies_environment
    )
    # httpx binds getproxies at import, so its module copy is patched as well.
    monkeypatch.setattr(
        httpx._utils, "getproxies", urllib.request.getproxies_environment
    )
    yield violations
    assert not violations, f"test attempted non-loopback connections: {violations}"


@pytest.fixture
def node_readability() -> None:
    """Require the Readability.js path: Node on PATH and provisioned dependencies.

    Never installs anything. Provision once with
    `uv run --locked python scripts/provision_node_deps.py`. A missing capability
    fails any selected Node test; the Node-absent lane explicitly deselects them.
    """
    if worker_module.find_node() is None:
        reason = (
            "Readability.js under Node is unavailable "
            "(node_modules missing or the probe failed); run "
            "`uv run --locked python scripts/provision_node_deps.py`."
        )
        pytest.fail(reason)


@pytest.fixture
def python_readability(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force the pure-Python extractor: the server's startup decision says no Node."""
    monkeypatch.setattr(readabilipy.simple_json, "have_node", lambda: False)
    monkeypatch.setattr(worker_module, "find_node", lambda: None)


def unwrap(text: str) -> str:
    """The text between a result's two envelope lines, which carry the same code."""
    first, _, rest = text.partition("\n")
    body, _, last = rest.rpartition("\n")
    assert first.startswith("\u27e6fetch-envelope "), first
    code = first.removeprefix("\u27e6fetch-envelope ").split("\u27e7", 1)[0]
    assert len(code) == 16 and last == f"\u27e6fetch-envelope-end {code}\u27e7", last
    return body


def page_payload(text: str) -> str:
    """Remove checked transport markers while retaining the page's exact text."""
    if text.startswith("\u27e6fetch-envelope "):
        text = unwrap(text)
    content, marker, metadata = text.rpartition("\n\n<page-meta ")
    if marker:
        assert metadata.endswith("/>") and "total_length=" in metadata, metadata
        return content
    return text


@pytest.fixture
def worker_in_process(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    """Run the content worker's `run()` in this process, JSON round-tripped like the real
    pipe, so in-process patches reach it. Returns the requests the server sent."""
    seen: list[dict[str, Any]] = []

    async def run(request: dict[str, Any], slots: Any, deadline: Any) -> dict[str, Any]:
        seen.append(request)
        return json.loads(
            json.dumps(worker_module.run(json.loads(json.dumps(request))))
        )

    monkeypatch.setattr(server_module, "_run_worker", run)
    return seen


@pytest.fixture(autouse=True)
def private_tempdir(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Give each test its own ``tempfile.gettempdir``.

    readabilipy's Readability.js path writes its input and output to fixed
    names (``full.html``, ``article.json``) in the shared temp directory, so
    two test runs at once (independent test processes) read
    each other's pages.
    """
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))


async def raw_request(
    method: str, params: dict[str, Any]
) -> JSONRPCResponse | JSONRPCError:
    """Send one request exactly as given, bypassing the client's own checks.

    `ClientSession` validates what it sends, so a malformed request (a list as
    a tool name, a number as the arguments) can only reach the server as raw
    JSON-RPC: a 2025-11-25 handshake, then the request, and its response.
    """
    async with create_client_server_memory_streams() as (
        (client_read, client_write),
        server_streams,
    ):

        @asynccontextmanager
        async def fake_stdio_server() -> AsyncIterator[Any]:
            yield server_streams

        async def receive(request_id: int) -> JSONRPCResponse | JSONRPCError:
            async for item in client_read:
                assert isinstance(item, SessionMessage)
                message = item.message
                if (
                    isinstance(message, (JSONRPCResponse, JSONRPCError))
                    and message.id == request_id
                ):
                    return message
            raise AssertionError("the server closed the stream")  # pragma: no cover

        with patch("mcp.server.mcpserver.server.stdio_server", fake_stdio_server):
            async with anyio.create_task_group() as tg:
                tg.start_soon(lambda: server_module.serve())
                await client_write.send(
                    SessionMessage(
                        JSONRPCRequest(
                            jsonrpc="2.0",
                            id=1,
                            method="initialize",
                            params={
                                "protocolVersion": "2025-11-25",
                                "capabilities": {},
                                "clientInfo": {"name": "raw", "version": "0"},
                            },
                        )
                    )
                )
                await receive(1)
                await client_write.send(
                    SessionMessage(
                        JSONRPCNotification(
                            jsonrpc="2.0", method="notifications/initialized"
                        )
                    )
                )
                await client_write.send(
                    SessionMessage(
                        JSONRPCRequest(
                            jsonrpc="2.0", id=2, method=method, params=params
                        )
                    )
                )
                response = await receive(2)
                tg.cancel_scope.cancel()
                return response
    raise AssertionError("unreachable")  # pragma: no cover
