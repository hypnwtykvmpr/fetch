"""Connect-time public-only policy.

No `web` fixture: the real transport runs against loopback servers, with a
fake resolver (no real DNS) and a NAT that maps PUBLIC_IP to 127.0.0.1 at the
dial.
"""

from __future__ import annotations

import http.server
import socket
import ssl
import threading
import time
import urllib.request
from collections import Counter
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import anyio
import httpcore
import httpx
import pytest
from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID
from mcp.types import CallToolResult, TextContent

import mcp_server_fetch.server as server_module

from .harness import PUBLIC_IP, connect

PUB6 = "2606:4700::6810:1"  # a blackhole: the NAT never answers it
REBIND = [[PUBLIC_IP], ["127.0.0.1"]]
TOOL_ERROR = "Error executing tool fetch: "


class _Server(http.server.ThreadingHTTPServer):
    daemon_threads = True

    def handle_error(self, request: Any, client_address: Any) -> None:
        pass  # a client closing a kept-alive connection is not a failure


class _Server6(_Server):
    address_family = socket.AF_INET6


class Web:
    """Fake DNS answers, the dial log and the loopback servers of one test."""

    def __init__(self) -> None:
        self.answers: dict[str, list[Any]] = {}
        self.calls: Counter[str] = Counter()
        self.dials: list[str] = []
        self.seen: list[str] = []
        self.routes: dict[str, tuple[int, dict[str, str], bytes]] = {}
        self.servers: list[_Server] = []

    def serve(self, addr: str = "127.0.0.1", ctx: ssl.SSLContext | None = None) -> int:
        web = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def do_GET(self) -> None:
                sni = getattr(self.connection, "sni", None)
                web.seen.append(
                    f"{self.requestline} Host={self.headers['Host']}"
                    + (f" SNI={sni}" if sni else "")
                )
                default = (
                    (404, {}, b"")
                    if self.path == "/robots.txt"
                    else (200, {"Content-Type": "text/plain"}, b"INTERNAL-SECRET")
                )
                code, headers, body = web.routes.get(self.path, default)
                self.send_response(code)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def log_message(self, format: str, *args: Any) -> None:
                pass

        server = (_Server6 if ":" in addr else _Server)((addr, 0), Handler)
        if ctx is not None:
            server.socket = ctx.wrap_socket(server.socket, server_side=True)
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.servers.append(server)
        return int(server.server_address[1])


@pytest.fixture
def net(monkeypatch: pytest.MonkeyPatch) -> Iterator[Web]:
    web = Web()
    real_getaddrinfo = socket.getaddrinfo

    def getaddrinfo(
        host: Any,
        port: Any,
        family: int = 0,
        type: int = 0,
        proto: int = 0,
        flags: int = 0,
    ) -> Any:
        name = host.decode() if isinstance(host, bytes) else host
        try:
            return real_getaddrinfo(
                host, port, family, type, proto, flags | socket.AI_NUMERICHOST
            )
        except socket.gaierror:
            pass
        if any(len(label) > 63 for label in name.split(".")):
            # The idna codec raises UnicodeError before any lookup.
            return real_getaddrinfo(host, port)
        if name == "slow.test":
            time.sleep(2)
            raise socket.gaierror(socket.EAI_AGAIN, "slow")
        if name not in web.answers:
            raise socket.gaierror(socket.EAI_NONAME, "fake: unknown")
        sequence = web.answers[name]
        answer = sequence[web.calls[name] % len(sequence)]
        web.calls[name] += 1
        if isinstance(answer, BaseException):
            raise answer
        out: list[Any] = []
        for address in answer:
            family = socket.AF_INET6 if ":" in address else socket.AF_INET
            sockaddr = (
                (address, port or 0)
                if family == socket.AF_INET
                else (address, port or 0, 0, 0)
            )
            # One answer per socket type, as the real resolver gives.
            for socktype in (socket.SOCK_DGRAM, socket.SOCK_STREAM):
                out.append((family, socktype, 0, "", sockaddr))
        return out

    monkeypatch.setattr(socket, "getaddrinfo", getaddrinfo)
    real_connect = httpcore.AnyIOBackend.connect_tcp  # pyright: ignore[reportAttributeAccessIssue]

    async def nat(
        self: Any,
        host: str,
        port: int,
        timeout: float | None = None,
        local_address: str | None = None,
        socket_options: Any = None,
    ) -> httpcore.AsyncNetworkStream:
        web.dials.append(host)
        if host == PUB6:
            await anyio.sleep(3600)
        if ":" not in host and not host.replace(".", "").isdigit():
            # A name: the connector resolves it itself, as anyio would.
            answers = [
                info[4][0]
                for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
            ]
            host = PUBLIC_IP if PUBLIC_IP in answers else str(answers[0])
        target = "127.0.0.1" if host == PUBLIC_IP else host
        return await real_connect(
            self, target, port, timeout, local_address, socket_options
        )

    monkeypatch.setattr(httpcore.AnyIOBackend, "connect_tcp", nat)
    yield web
    for server in web.servers:
        server.shutdown()
        server.server_close()


def _tls_cert(tmp_path: Path) -> Path:
    """A self-signed certificate for pinned.test (its key is next to it)."""
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    name = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, "pinned.test")])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(name)
        .issuer_name(name)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(minutes=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(
            x509.SubjectAlternativeName([x509.DNSName("pinned.test")]), False
        )
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), True)
        .add_extension(
            x509.KeyUsage(True, False, True, False, False, True, True, False, False),
            True,
        )
        .add_extension(
            x509.SubjectKeyIdentifier.from_public_key(key.public_key()), False
        )
        .add_extension(
            x509.AuthorityKeyIdentifier.from_issuer_public_key(key.public_key()), False
        )
        .sign(key, hashes.SHA256())
    )
    (tmp_path / "k").write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.TraditionalOpenSSL,
            serialization.NoEncryption(),
        )
    )
    (tmp_path / "c").write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    return tmp_path / "c"


@pytest.fixture
def tls_cert(tmp_path: Path) -> Path:
    return _tls_cert(tmp_path)


def test_tls_cert_needs_no_external_tools(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("PATH", str(tmp_path))
    certificate = _tls_cert(tmp_path)
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.load_verify_locations(cafile=certificate)


def text_of(result: CallToolResult) -> str:
    [block] = result.content
    assert isinstance(block, TextContent)
    return block.text


def is_refused(result: CallToolResult) -> bool:
    return result.is_error and "not a public address" in text_of(result)


async def test_rebinding_after_the_check_is_refused_tool_and_robots(net: Web) -> None:
    port = net.serve()
    net.answers["a.test"] = REBIND
    async with connect() as (session, _):
        result = await session.call_tool(
            "fetch", {"url": f"http://a.test:{port}/secret"}
        )
    assert text_of(result) == (
        f"{TOOL_ERROR}Refused to connect to a.test: it resolves to "
        "127.0.0.1, which is not a public address. Start the server with "
        "--allow-private-ips to allow private, loopback and link-local addresses."
    )
    assert result.is_error
    assert net.seen == []
    assert "127.0.0.1" not in net.dials


async def test_rebinding_on_a_redirect_hop_is_refused(net: Web) -> None:
    port = net.serve()
    net.answers["a.test"] = [[PUBLIC_IP]]
    net.answers["b.test"] = REBIND
    net.routes["/hop"] = (302, {"Location": f"http://b.test:{port}/secret"}, b"")
    async with connect(ignore_robots_txt=True) as (session, _):
        result = await session.call_tool("fetch", {"url": f"http://a.test:{port}/hop"})
    assert is_refused(result), result
    assert all("b.test" not in line for line in net.seen)


async def test_rebinding_on_the_prompt_path_is_refused(net: Web) -> None:
    port = net.serve()
    net.answers["p.test"] = REBIND
    async with connect() as (session, _):
        prompt = await session.get_prompt(
            "fetch", {"url": f"http://p.test:{port}/secret"}
        )
    content = prompt.messages[0].content
    assert isinstance(content, TextContent)
    assert "INTERNAL-SECRET" not in content.text
    assert "not a public address" in content.text


async def test_aaaa_rebinding_to_ipv6_loopback_is_refused(net: Web) -> None:
    port = net.serve("::1")
    net.answers["a6.test"] = [[PUBLIC_IP], ["::1"]]
    async with connect(ignore_robots_txt=True) as (session, _):
        result = await session.call_tool(
            "fetch", {"url": f"http://a6.test:{port}/secret"}
        )
    assert is_refused(result), result
    assert net.seen == []


async def test_a_name_the_server_cannot_resolve_fails_closed(net: Web) -> None:
    port = net.serve()
    # The hook's lookup fails; a second lookup would give loopback.
    net.answers["flaky.test"] = [
        socket.gaierror(socket.EAI_AGAIN, "again"),
        ["127.0.0.1"],
    ]
    async with connect(ignore_robots_txt=True) as (session, _):
        result = await session.call_tool(
            "fetch", {"url": f"http://flaky.test:{port}/secret"}
        )
    assert result.is_error
    assert net.seen == []


async def test_only_the_vetted_address_is_dialled_and_tls_keeps_the_name(
    net: Web, tls_cert: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(tls_cert, tls_cert.with_name("k"))
    ctx.sni_callback = lambda sock, name, _: setattr(sock, "sni", name)
    port = net.serve(ctx=ctx)
    net.answers["pinned.test"] = [[PUBLIC_IP]]
    # Verification stays on: `transport=` keeps httpx's trust_env for certificates.
    monkeypatch.setenv("SSL_CERT_FILE", str(tls_cert))
    async with connect(ignore_robots_txt=True) as (session, _):
        result = await session.call_tool(
            "fetch", {"url": f"https://pinned.test:{port}/p"}
        )
    assert not result.is_error, result
    assert net.dials == [PUBLIC_IP]
    assert net.seen == [f"GET /p HTTP/1.1 Host=pinned.test:{port} SNI=pinned.test"]


@pytest.mark.parametrize("source", ["env", "os"])
async def test_implicit_proxies_are_refused_in_public_only_mode(
    net: Web, monkeypatch: pytest.MonkeyPatch, source: str
) -> None:
    proxy = f"http://127.0.0.1:{net.serve()}"
    if source == "env":
        monkeypatch.setenv("HTTP_PROXY", proxy)
    else:
        # What urllib.request.getproxies reads from macOS System Settings or
        # the Windows registry.
        monkeypatch.setattr(urllib.request, "getproxies", lambda: {"http": proxy})
    async with connect(ignore_robots_txt=True) as (session, _):
        result = await session.call_tool(
            "fetch", {"url": "http://metadata.internal/latest"}
        )
    assert result.is_error
    assert "--proxy-url" in text_of(result)
    assert net.seen == []


async def test_explicit_proxy_url_is_still_honoured(net: Web) -> None:
    proxy = f"http://127.0.0.1:{net.serve()}"
    async with connect(ignore_robots_txt=True, proxy_url=proxy) as (session, _):
        result = await session.call_tool("fetch", {"url": f"http://{PUBLIC_IP}/page"})
    assert not result.is_error
    assert net.seen == [f"GET http://{PUBLIC_IP}/page HTTP/1.1 Host={PUBLIC_IP}"]


async def test_allow_private_ips_still_reaches_loopback(net: Web) -> None:
    port = net.serve()
    async with connect(allow_private_ips=True, ignore_robots_txt=True) as (session, _):
        result = await session.call_tool("fetch", {"url": f"http://127.0.0.1:{port}/x"})
    assert not result.is_error
    assert "INTERNAL-SECRET" in text_of(result)


async def test_public_only_client_is_wired_through_the_backend() -> None:
    client = server_module.make_client(None)
    transport: Any = client._transport  # pyright: ignore[reportPrivateUsage]
    assert type(transport._pool._network_backend).__name__ == "_PublicOnlyBackend"
    assert client._mounts == {}  # pyright: ignore[reportPrivateUsage]
    await client.aclose()


async def test_dual_stack_with_a_dead_ipv6_path_falls_back_quickly(net: Web) -> None:
    port = net.serve()
    net.answers["dual.test"] = [[PUB6, PUBLIC_IP]]
    async with server_module.make_client(None) as client:
        started = time.monotonic()
        response = await client.get(
            f"http://dual.test:{port}/", timeout=httpx.Timeout(30, connect=10)
        )
        elapsed = time.monotonic() - started
    assert response.status_code == 200
    # About 0.25 s locally. Attempting the dead address first and alone would
    # take the whole 10 s connect timeout and fail.
    assert elapsed < 5.0


async def test_slow_dns_is_a_connect_timeout(net: Web) -> None:
    async with server_module.make_client(None) as client:
        with pytest.raises(httpx.ConnectTimeout):
            await client.get(
                "http://slow.test/", timeout=httpx.Timeout(10, connect=0.5)
            )


@pytest.mark.parametrize("path", ["tool", "prompt"])
async def test_overlong_label_is_a_tool_error(net: Web, path: str) -> None:
    url = "http://" + "a" * 300 + "/"
    async with connect(ignore_robots_txt=True) as (session, _):
        if path == "tool":
            result = await session.call_tool("fetch", {"url": url})
            assert result.is_error
            assert text_of(result) != "Error executing tool fetch"
            assert "Failed to fetch" in text_of(result)
        else:
            prompt = await session.get_prompt("fetch", {"url": url})
            content = prompt.messages[0].content
            assert isinstance(content, TextContent)
            assert "Failed to fetch" in content.text
