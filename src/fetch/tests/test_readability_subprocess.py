"""Portable subprocess regressions for the retained runner and active worker."""

from __future__ import annotations

import ctypes
import os
import subprocess
import sys
import time
from contextlib import ExitStack
from pathlib import Path
from typing import Any, cast

import anyio
import httpx
import pytest

import mcp_server_fetch._worker as worker_module
import mcp_server_fetch.server as server_module
from mcp_server_fetch import readability_js

from .harness import FakeWeb, connect

PAGE = "https://example.com/article"
ARTICLE = """<html><head><title>Article</title></head><body><article>
<h1>Heading</h1><p>This is a <a href="https://example.com/x">link</a> in a paragraph with
 enough words to count as the main content of the page.</p></article></body></html>"""
NODE_VERSION = (
    "if sys.argv[1:] == ['-v']:\n    print('v24.0.0')\n    raise SystemExit\n"
)


@pytest.fixture
def bindir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Launch scratch Node/npm stand-ins with native Python, never a shell."""
    path = tmp_path / "bin"
    path.mkdir()
    real_popen = subprocess.Popen

    def which(name: str) -> str | None:
        script = path / name
        return str(script) if script.is_file() else None

    def popen(argv: Any, *args: Any, **kwargs: Any) -> Any:
        if isinstance(argv, list) and Path(argv[0]).parent == path:
            # These stdlib-only stand-ins emulate non-Python executables.
            # Skip site startup so subprocess coverage cannot instrument the
            # shim and consume the external tool's timeout. Product Python
            # workers take the unchanged branch below and retain coverage.
            argv = [sys.executable, "-S", *argv]
        return real_popen(argv, *args, **kwargs)

    monkeypatch.setattr(readability_js.shutil, "which", which)
    monkeypatch.setattr(readability_js.subprocess, "Popen", popen)
    return path


def tool(bindir: Path, name: str, body: str) -> Path:
    script = bindir / name
    script.write_text(
        "import json, os, subprocess, sys, time\nfrom pathlib import Path\n" + body,
        encoding="utf-8",
    )
    return script


@pytest.fixture
def jsdir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    path = tmp_path / "javascript"
    path.mkdir()
    monkeypatch.setattr(readability_js, "javascript_dir", lambda: path)
    monkeypatch.setattr(readability_js, "_probed", False)
    monkeypatch.setattr(readability_js, "_node", None)
    return path


def install(jsdir: Path) -> None:
    for name in readability_js.JS_DEPENDENCIES:
        package = jsdir / "node_modules" / name
        package.mkdir(parents=True)
        (package / "package.json").write_text("{}")


def alive(pid: int) -> bool:
    if sys.platform == "win32":
        kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel.OpenProcess.restype = ctypes.c_void_p
        kernel.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_ulong]
        kernel.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = kernel.OpenProcess(0x100000, False, pid)
        if not handle:
            return False
        try:
            return kernel.WaitForSingleObject(handle, 0) == 258
        finally:
            kernel.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    return True


def wait_dead(pid: int) -> bool:
    deadline = time.monotonic() + 5
    while alive(pid):
        if time.monotonic() > deadline:
            return False
        time.sleep(0.05)
    return True


async def test_hanging_node_is_killed_and_the_call_falls_back(
    web: FakeWeb, bindir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pids = bindir / "pids"
    helper = tool(
        bindir,
        "hung-node",
        "child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)'])\n"
        + f"Path({str(pids)!r}).write_text(f'{{os.getpid()}} {{child.pid}}')\n"
        + "time.sleep(60)\n",
    )
    monkeypatch.setattr(
        worker_module, "find_node", lambda: (sys.executable, str(helper))
    )
    monkeypatch.setattr(server_module, "_NODE_TIMEOUT", 1.0)
    web.add(
        PAGE, httpx.Response(200, text=ARTICLE, headers={"content-type": "text/html"})
    )
    started = time.monotonic()
    async with connect(ignore_robots_txt=True) as (session, _):
        result = await session.call_tool("fetch", {"url": PAGE})
    assert time.monotonic() - started < 10
    assert result.is_error is False
    [content] = result.content
    assert content.type == "text"
    assert "This is a link in a paragraph" in content.text
    assert "https://example.com/x" not in content.text
    for pid in map(int, pids.read_text().split()):
        assert wait_dead(pid), f"process {pid} outlived the timeout"


async def test_extraction_does_not_block_other_requests(
    web: FakeWeb, bindir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    started = bindir / "started"
    helper = tool(
        bindir, "hung-node", f"Path({str(started)!r}).touch()\ntime.sleep(60)\n"
    )
    monkeypatch.setattr(
        worker_module, "find_node", lambda: (sys.executable, str(helper))
    )
    monkeypatch.setattr(server_module, "_NODE_TIMEOUT", 2.0)
    web.add(
        PAGE, httpx.Response(200, text=ARTICLE, headers={"content-type": "text/html"})
    )
    async with connect(ignore_robots_txt=True) as (session, _):
        fetched: list[float] = []
        listed = float("inf")

        async def call() -> None:
            await session.call_tool("fetch", {"url": PAGE})
            fetched.append(time.monotonic())

        async with anyio.create_task_group() as tg:
            tg.start_soon(call)
            with anyio.fail_after(10):
                while not started.exists():
                    await anyio.sleep(0.02)
            tools = await session.list_tools()
            listed = time.monotonic()
            assert [t.name for t in tools.tools] == ["fetch"]
        assert listed < fetched[0] - 1


def test_npm_install_output_goes_to_stderr_not_stdout(
    bindir: Path, jsdir: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    node = tool(bindir, "node", NODE_VERSION)
    deps = [str(jsdir / "node_modules" / n) for n in readability_js.JS_DEPENDENCIES]
    tool(
        bindir,
        "npm",
        "print('added 38 packages, and audited 39 packages in 470ms')\nprint('npm warn deprecated', file=sys.stderr)\n"
        + f"for name in {deps!r}:\n    path = Path(name); path.mkdir(parents=True)\n    (path / 'package.json').write_text('{{}}')\n",
    )
    assert readability_js.node_path() == str(node)
    out, err = capfd.readouterr()
    assert out == ""
    assert "added 38 packages" in err and "npm warn deprecated" in err
    assert "contacts the npm registry" in err


def test_failed_extraction_stderr_goes_to_stderr_not_stdout(
    bindir: Path, jsdir: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    install(jsdir)
    tool(
        bindir,
        "node",
        NODE_VERSION
        + "print('on stdout')\nprint('TypeError: x', file=sys.stderr)\nraise SystemExit(1)\n",
    )
    assert readability_js.extract_article(ARTICLE) is None
    out, err = capfd.readouterr()
    assert out == ""
    assert "TypeError: x" in err and "Readability.js failed (exit 1)" in err


def test_children_do_not_read_the_servers_stdin(
    bindir: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    seen = tmp_path / "stdin"
    script = tool(
        bindir, "reader", f"Path({str(seen)!r}).write_text(sys.stdin.read())\n"
    )
    real_popen = subprocess.Popen
    inputs: list[Any] = []

    def popen(*args: Any, **kwargs: Any) -> Any:
        inputs.append(kwargs["stdin"])
        return real_popen(*args, **kwargs)

    monkeypatch.setattr(readability_js.subprocess, "Popen", popen)
    done = readability_js._run([str(script)], timeout=5)
    assert done is not None and done.returncode == 0
    assert inputs == [subprocess.DEVNULL]
    assert seen.read_text() == ""


def test_npm_is_run_by_the_path_shutil_which_resolves(
    jsdir: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    found = {"node": r"C:\nodejs\node.exe", "npm": r"C:\nodejs\npm.CMD"}
    monkeypatch.setattr(readability_js.shutil, "which", found.get)
    runs: list[list[str]] = []

    def run(argv: list[str], timeout: float, cwd: Path | None = None) -> Any:
        runs.append(argv)
        if argv[1] == "install":
            install(jsdir)
        return subprocess.CompletedProcess(argv, 0, "v24.0.0\n", "")

    monkeypatch.setattr(readability_js, "_run", run)
    assert readability_js.node_path() == found["node"]
    assert runs == [
        [found["node"], "-v"],
        [found["npm"], "install", "--no-audit", "--no-fund"],
    ]


def test_no_node_on_path(
    bindir: Path, jsdir: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    assert readability_js.extract_article(ARTICLE) is None
    assert "Node.js was not found on PATH" in capfd.readouterr().err


@pytest.mark.parametrize(
    "body",
    ["print('v12.22.0')", "print('garbage')", "raise SystemExit(3)", "time.sleep(60)"],
)
def test_old_or_broken_node(
    bindir: Path,
    jsdir: Path,
    body: str,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(readability_js, "VERSION_TIMEOUT_SECONDS", 0.5)
    tool(bindir, "node", body)
    assert readability_js.node_path() is None
    assert "Node.js >= 14 is needed" in capfd.readouterr().err


def test_no_npm_for_missing_dependencies(
    bindir: Path, jsdir: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    tool(bindir, "node", NODE_VERSION)
    assert readability_js.node_path() is None
    assert "npm was not found on PATH" in capfd.readouterr().err


def test_failed_npm_install(
    bindir: Path, jsdir: Path, capfd: pytest.CaptureFixture[str]
) -> None:
    tool(bindir, "node", NODE_VERSION)
    tool(
        bindir,
        "npm",
        "print('npm error network ENOTFOUND', file=sys.stderr)\nraise SystemExit(1)",
    )
    assert readability_js.node_path() is None
    err = capfd.readouterr().err
    assert "npm error network ENOTFOUND" in err and "npm install failed (exit 1)" in err


def test_hanging_npm_install_is_killed(
    bindir: Path,
    jsdir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(readability_js, "INSTALL_TIMEOUT_SECONDS", 0.5)
    tool(bindir, "node", NODE_VERSION)
    tool(bindir, "npm", "time.sleep(60)")
    assert readability_js.node_path() is None
    assert "npm did not finish within 0.5 s and was killed" in capfd.readouterr().err


def test_probe_runs_once_per_process(bindir: Path, jsdir: Path) -> None:
    calls = bindir / "calls"
    node = tool(
        bindir,
        "node",
        f"with Path({str(calls)!r}).open('a') as f: f.write('x\\n')\n" + NODE_VERSION,
    )
    install(jsdir)
    assert readability_js.node_path() == str(node)
    assert readability_js.node_path() == str(node)
    assert calls.read_text() == "x\n"


def test_unstartable_node(
    bindir: Path,
    jsdir: Path,
    monkeypatch: pytest.MonkeyPatch,
    capfd: pytest.CaptureFixture[str],
) -> None:
    tool(bindir, "node", "")

    def unavailable(*args: Any, **kwargs: Any) -> Any:
        raise OSError("unstartable helper")

    monkeypatch.setattr(readability_js.subprocess, "Popen", unavailable)
    assert readability_js.node_path() is None
    assert "could not start" in capfd.readouterr().err


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ("null", ""),
        ('{"content": null}', ""),
        ('{"content": "<div><p>hi</p></div>"}', "<div><p>hi</p></div>"),
        (None, None),
        ("{", None),
    ],
)
def test_extraction_outcomes(
    bindir: Path, jsdir: Path, body: str | None, expected: str | None
) -> None:
    install(jsdir)
    code = f"Path(sys.argv[5]).write_text({body!r})\n" if body is not None else ""
    tool(bindir, "node", NODE_VERSION + code)
    assert readability_js.extract_article(ARTICLE) == expected


def test_extraction_gets_the_page_and_runs_extractarticle(
    bindir: Path, jsdir: Path, tmp_path: Path
) -> None:
    install(jsdir)
    copy, args = tmp_path / "seen.html", tmp_path / "args"
    tool(
        bindir,
        "node",
        NODE_VERSION
        + f"Path({str(args)!r}).write_text(sys.argv[1])\n"
        + f"Path({str(copy)!r}).write_text(Path(sys.argv[3]).read_text(encoding='utf-8'), encoding='utf-8')\n"
        + 'Path(sys.argv[5]).write_text(\'{"content": "<p>ok</p>"}\')\n',
    )
    assert readability_js.extract_article("<p>é</p>") == "<p>ok</p>"
    assert copy.read_text(encoding="utf-8") == "<p>é</p>"
    assert args.read_text().strip() == str(jsdir / "ExtractArticle.js")


def test_kill_tree_uses_taskkill_on_windows(monkeypatch: pytest.MonkeyPatch) -> None:
    runs: list[list[str]] = []
    killed: list[bool] = []

    class Proc:
        pid = 4242

        def kill(self) -> None:
            killed.append(True)

    monkeypatch.setattr(readability_js.sys, "platform", "win32")
    monkeypatch.setattr(
        readability_js.subprocess, "run", lambda argv, **_: runs.append(argv)
    )
    readability_js._kill_tree(cast("subprocess.Popen[str]", Proc()))
    assert runs == [["taskkill", "/F", "/T", "/PID", "4242"]]
    assert killed == [True]


def test_kill_tree_kills_the_child_when_taskkill_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[bool] = []
    timeouts: list[Any] = []

    class Proc:
        pid = 4242

        def kill(self) -> None:
            killed.append(True)

    def run(argv: list[str], **kwargs: Any) -> None:
        timeouts.append(kwargs.get("timeout"))
        raise OSError("taskkill is blocked")

    monkeypatch.setattr(readability_js.sys, "platform", "win32")
    monkeypatch.setattr(readability_js.subprocess, "run", run)
    readability_js._kill_tree(cast("subprocess.Popen[str]", Proc()))
    assert timeouts == [readability_js.KILL_TIMEOUT_SECONDS]
    assert killed == [True]


def test_timeout_returns_while_a_detached_helper_holds_the_pipes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []

    class Proc:
        stdout = stderr = None

        def communicate(self, timeout: float) -> Any:
            events.append("communicate")
            raise subprocess.TimeoutExpired("node", timeout)

        def wait(self, timeout: float) -> None:
            events.append("wait")
            raise subprocess.TimeoutExpired("node", timeout)

    monkeypatch.setattr(readability_js.subprocess, "Popen", lambda *a, **k: Proc())
    monkeypatch.setattr(
        readability_js, "_kill_tree", lambda proc: events.append("kill")
    )
    assert readability_js._run(["node"], 1.0) is None
    assert events == ["communicate", "kill", "wait"]


@pytest.mark.parametrize("platform", ["linux", "darwin"])
@pytest.mark.parametrize("runner", ["worker", "retained"])
@pytest.mark.parametrize("wait_times_out", [False, True])
def test_posix_timeout_closes_owned_pipes_without_draining(
    platform: str,
    runner: str,
    wait_times_out: bool,
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # Real file handles expose leaks immediately; only process operations are
    # simulated so POSIX ownership can also be exercised on Windows.
    events: list[str] = []
    with ExitStack() as stack:
        pipes = [
            stack.enter_context((tmp_path / name).open("w+b"))
            for name in ("stdin", "stdout", "stderr")
        ]

        class Proc:
            stdin = pipes[0] if runner == "worker" else None
            stdout = pipes[1]
            stderr = pipes[2] if runner == "retained" else None
            returncode = 0

            def __class_getitem__(cls, item: Any) -> type[Proc]:
                return cls

            def __init__(self, *args: Any, **kwargs: Any) -> None:
                pass

            def communicate(self, *args: Any, timeout: float) -> Any:
                events.append("communicate")
                raise subprocess.TimeoutExpired("node", timeout)

            def kill(self) -> None:
                events.append("kill")

            def wait(self, timeout: float) -> None:
                assert timeout == 5.0
                events.append("wait")
                if wait_times_out:
                    raise subprocess.TimeoutExpired("node", timeout)

        monkeypatch.setattr(readability_js.sys, "platform", platform)
        monkeypatch.setattr(readability_js.subprocess, "Popen", Proc)
        if runner == "worker":
            # The worker discards stderr rather than owning a third pipe.
            pipes[2].close()
            assert worker_module._node_readability(ARTICLE, ["node"], 1.0) is None
        else:
            # The retained runner sends stdin to the null device.
            pipes[0].close()
            # Tree containment is covered separately; do not signal any native
            # process while exercising the POSIX pipe-ownership branch.
            monkeypatch.setattr(
                readability_js, "_kill_tree", lambda proc: events.append("kill")
            )
            assert readability_js._run(["node"], 1.0) is None
        assert events == ["communicate", "kill", "wait"]
        assert all(pipe.closed for pipe in pipes)


def test_kill_tree_tolerates_a_group_already_gone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    killed: list[bool] = []

    class Proc:
        pid = 4242

        def kill(self) -> None:
            killed.append(True)

    def gone(pid: int, sig: int) -> None:
        raise ProcessLookupError

    monkeypatch.setattr(readability_js.sys, "platform", "linux")
    monkeypatch.setattr(readability_js.os, "killpg", gone, raising=False)
    monkeypatch.setattr(readability_js.signal, "SIGKILL", 9, raising=False)
    readability_js._kill_tree(cast("subprocess.Popen[str]", Proc()))
    assert killed == [True]


def test_a_failed_second_run_keeps_the_first_result(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    page = (
        "<html><body><p>short shell</p><div hidden><p>"
        + "long hidden content " * 20
        + "</p></div></body></html>"
    )
    results = iter(["<p>short shell</p>", None])
    monkeypatch.setattr(worker_module, "_node_readability", lambda *a: next(results))
    monkeypatch.setattr(worker_module, "_python_readability", lambda html: "")
    result = worker_module.run(
        {
            "mode": "markdown",
            "html": page,
            "page_url": PAGE,
            "links": False,
            "sanitize": False,
            "node": ["node"],
            "node_timeout": 5,
            "budget": 30,
        }
    )
    assert result["content"].strip() == "short shell"


def test_unavailable_readability_js_falls_back_to_python(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(worker_module, "find_node", lambda: None)
    text = server_module.extract_content_from_html(ARTICLE)
    assert "This is a link in a paragraph" in text
    assert "https://example.com/x" not in text
