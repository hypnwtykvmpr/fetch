"""The suite-wide network guard and proxy scrub (harness.network_guard)."""

import asyncio
import os
import socket
import urllib.request

import httpx._utils
import pytest

PUBLIC = ("93.184.215.14", 80)


def test_non_loopback_connect_is_refused_and_recorded(network_guard: list[str]) -> None:
    with socket.socket() as s, pytest.raises(ConnectionRefusedError):
        s.connect(PUBLIC)
    assert network_guard == [f"connect {PUBLIC!r}"]
    network_guard.clear()


async def test_asyncio_connections_are_guarded(network_guard: list[str]) -> None:
    with pytest.raises(OSError):
        await asyncio.open_connection(*PUBLIC)
    assert network_guard
    network_guard.clear()


def test_dns_for_other_names_fails_offline() -> None:
    with pytest.raises(socket.gaierror):
        socket.getaddrinfo("example.com", 80)


def test_loopback_stays_usable() -> None:
    assert socket.getaddrinfo("localhost", 80)
    with (
        socket.create_server(("127.0.0.1", 0)) as server,
        socket.create_connection(server.getsockname()),
    ):
        pass
    a, b = socket.socketpair()
    a.close()
    b.close()


def test_proxy_settings_come_from_the_cleared_environment_only() -> None:
    assert not {
        k
        for k in os.environ
        if k.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy"}
    }
    assert urllib.request.getproxies() == {}
    assert vars(httpx._utils)["getproxies"] is urllib.request.getproxies_environment
