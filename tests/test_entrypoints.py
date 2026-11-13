"""Tests for the command-line entry points: ``main()``, ``__main__`` and the console script.

``main()`` and ``__main__`` run in-process with ``serve`` patched, so coverage
sees them; only the console-script smoke spawns a process, to prove the
installed entry point boots and speaks MCP over real stdio.
"""

from __future__ import annotations

import importlib
import importlib.metadata
import importlib.util
import os
import runpy
import sys
import tomllib
from importlib.metadata import version
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

import mcp_server_fetch
import mcp_server_fetch.server


def run_main(monkeypatch: pytest.MonkeyPatch, *argv: str) -> AsyncMock:
    serve = AsyncMock()
    monkeypatch.setattr(mcp_server_fetch, "serve", serve)
    monkeypatch.setattr(sys, "argv", ["mcp-server-fetch", *argv])
    mcp_server_fetch.main()
    return serve


def test_main_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    serve = run_main(monkeypatch)
    serve.assert_awaited_once_with(
        custom_user_agent=None,
        ignore_robots_txt=False,
        proxy_url=None,
        allow_private_ips=False,
        use_readability_js=True,
        lock_sanitization=False,
    )


def test_main_passes_every_flag(monkeypatch: pytest.MonkeyPatch) -> None:
    serve = run_main(
        monkeypatch,
        "--user-agent",
        "MyBot/1.0",
        "--ignore-robots-txt",
        "--proxy-url",
        "http://proxy.example.com:8080",
        "--allow-private-ips",
        "--lock-sanitization",
    )
    serve.assert_awaited_once_with(
        custom_user_agent="MyBot/1.0",
        ignore_robots_txt=True,
        proxy_url="http://proxy.example.com:8080",
        allow_private_ips=True,
        use_readability_js=True,
        lock_sanitization=True,
    )


def test_main_help_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        run_main(monkeypatch, "--help")
    assert excinfo.value.code == 0
    out = capsys.readouterr().out
    assert "give a model the ability to make web requests" in out
    for flag in (
        "--user-agent",
        "--ignore-robots-txt",
        "--proxy-url",
        "--allow-private-ips",
        "--lock-sanitization",
    ):
        assert flag in out


def test_main_rejects_unknown_flag(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as excinfo:
        run_main(monkeypatch, "--bogus")
    assert excinfo.value.code == 2
    assert "unrecognized arguments: --bogus" in capsys.readouterr().err


def test_dunder_main_calls_main(monkeypatch: pytest.MonkeyPatch) -> None:
    # `python -m mcp_server_fetch`, in-process.
    main = MagicMock()
    monkeypatch.setattr(mcp_server_fetch, "main", main)
    monkeypatch.delitem(sys.modules, "mcp_server_fetch.__main__", raising=False)
    runpy.run_module("mcp_server_fetch", run_name="__main__")
    main.assert_called_once_with()


def test_running_dunder_main_calls_main(monkeypatch: pytest.MonkeyPatch) -> None:
    main = MagicMock()
    monkeypatch.setattr(mcp_server_fetch, "main", main)
    monkeypatch.delitem(sys.modules, "mcp_server_fetch.__main__", raising=False)
    runpy.run_module("mcp_server_fetch.__main__", run_name="__main__")
    main.assert_called_once_with()


def test_importing_dunder_main_does_not_start_the_server(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    main = MagicMock()
    monkeypatch.setattr(mcp_server_fetch, "main", main)
    monkeypatch.delitem(sys.modules, "mcp_server_fetch.__main__", raising=False)
    importlib.import_module("mcp_server_fetch.__main__")
    main.assert_not_called()


def test_package_init_run_as_main_calls_main(monkeypatch: pytest.MonkeyPatch) -> None:
    # Covers the `if __name__ == "__main__"` guard at the foot of __init__.py.
    serve = AsyncMock()
    monkeypatch.setattr(mcp_server_fetch.server, "serve", serve)
    monkeypatch.setattr(sys, "argv", ["mcp-server-fetch"])
    runpy.run_module("mcp_server_fetch.__init__", run_name="__main__")
    serve.assert_awaited_once_with(
        custom_user_agent=None,
        ignore_robots_txt=False,
        proxy_url=None,
        allow_private_ips=False,
        use_readability_js=True,
        lock_sanitization=False,
    )


def test_installed_version_is_this_fork_release() -> None:
    pyproject = tomllib.loads(
        (Path(__file__).parents[1] / "pyproject.toml").read_text(encoding="utf-8")
    )
    expected = pyproject["project"]["version"]
    assert "+fork." in expected
    assert version("mcp-server-fetch") == expected


def test_server_version_falls_back_without_distribution_metadata() -> None:
    def missing(name: str) -> str:
        raise importlib.metadata.PackageNotFoundError(name)

    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(importlib.metadata, "version", missing)
        module = importlib.reload(mcp_server_fetch.server)
        assert module.SERVER_VERSION == "0+unknown"
    importlib.reload(mcp_server_fetch.server)


def console_script() -> Path:
    bin_dir = Path(sys.executable).parent
    name = "mcp-server-fetch.exe" if os.name == "nt" else "mcp-server-fetch"
    return bin_dir / name


async def test_console_script_serves_over_stdio() -> None:
    # The one subprocess smoke: the installed console script boots and answers
    # tools/list over real stdio. Everything else runs in-process.
    script = console_script()
    assert script.exists(), f"console script not installed at {script}"
    params = StdioServerParameters(command=str(script), args=[])
    with open(os.devnull, "w") as devnull:  # noqa: ASYNC230 — the stdio session needs the handle held open
        try:
            async with (
                stdio_client(params, errlog=devnull) as (read, write),
                ClientSession(read, write) as session,
            ):
                await session.initialize()
                tools = await session.list_tools()
        finally:
            devnull.close()
    assert [t.name for t in tools.tools] == ["fetch"]
